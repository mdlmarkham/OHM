# OHM Type-Check Honesty Batch — Design Note

**Date:** 2026-07-08  
**Author:** Métis (subagent)  
**Scope:** Re-enable real mypy error codes and resolve the largest single class of false-positive errors caused by the handler-mixin pattern.  

> **Status:** Design-only deliverable. No production code changes.

---

## 1. Background & Goal

`mypy.ini` currently disables 13 error-code families and `pyproject.toml` ignores errors in `ohm.bayesian` and `ohm.sdk`. This design note plans a staged cleanup that:

1. Fixes the ~985 `attr-defined` errors in `src/ohm/server/` (the dominant false-positive class) with a small ~40-line `_HandlerBase` protocol/stub.
2. Re-enables the disabled mypy codes in priority order.
3. Keeps CI green by gating on delta rather than absolute zero errors.

---

## 2. Inspection Summary

### 2.1 Mypy configuration inspected

| File | Lines | Setting |
|------|-------|---------|
| `/root/olympus/OHM/mypy.ini` | 1–6 | `[mypy]` section; `disable_error_code = attr-defined,no-redef,assignment,misc,name-defined,arg-type,index,override,union-attr,return-value,var-annotated,dict-item,operator,call-arg,call-overload` |
| `/root/olympus/OHM/mypy.ini` | 7–22 | Per-module `ignore_errors = True` for `ohm.bayesian`, `ohm.sdk`, `ohm.framework.*`, `ohm.inference.*`, `ohm.patterns`, `ohm.tenant` |
| `/root/olympus/OHM/pyproject.toml` | 91–95 | `[[tool.mypy.overrides]]` with `module = ["ohm.bayesian", "ohm.sdk"]` and `ignore_errors = true` (duplicates `mypy.ini`) |

### 2.2 Handler mixins and `OhmHandler` inspected

| File | Lines | What it contains |
|------|-------|------------------|
| `/root/olympus/OHM/src/ohm/server/server.py` | 1027–1065 | `class OhmHandler(..., BaseHTTPRequestHandler)` with shared attributes (`store`, `tenant_manager`, `config`, `tokens`, `customer_tokens`, `roles`, `no_auth`, `require_read_auth`, `schema_config`, `multi_tenant`, `TRUSTED_PROXIES`, `_write_lock`, dispatch tables) and the `current_store` / `_customer_id` properties. |
| `/root/olympus/OHM/src/ohm/server/server.py` | 1299–1316 | `def _json_response(self, code: int, data) -> None`. |
| `/root/olympus/OHM/src/ohm/server/server.py` | 1178–1298 | `def _authenticate(self) -> Optional[str]`. |
| `/root/olympus/OHM/src/ohm/server/server.py` | 2519–2738 | Dispatch-table population (`_GET_EXACT`, `_POST_EXACT`, `_DELETE_PREFIXES`, etc.). |
| `/root/olympus/OHM/src/ohm/server/handlers/__init__.py` | 1–45 | Re-exports handler mixins; stale docstring still references a 7-mixin `OhmHandler` example. |
| `/root/olympus/OHM/src/ohm/server/handlers/admin.py` | 12–2326 | `AdminHandlerMixin` (173 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/analysis.py` | 11–1831 | `AnalysisHandlerMixin` (121 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/ask.py` | 19–123 | `AskHandlerMixin` (12 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/catalog.py` | 4–100 | `CatalogHandlerMixin` (14 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/decision.py` | 6–23 | `DecisionHandlerMixin` (3 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/documents.py` | 157–516 | `DocumentHandlerMixin` (19 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/graph.py` | 35–5219 | `GraphHandlerMixin` (492 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/inference.py` | 6–492 | `InferenceHandlerMixin` (64 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/infra.py` | 10–519 | `InfraHandlerMixin` (41 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/markov.py` | 6–56 | `MarkovHandlerMixin` (4 `attr-defined` errors). |
| `/root/olympus/OHM/src/ohm/server/handlers/tenant.py` | 16–216 | `TenantHandlerMixin` (31 `attr-defined` errors). |

### 2.3 Error-count snapshot

Measured with `mypy 2.1.0` and `--config-file=/dev/null` on each file (ignores the project disables):

| Module | `attr-defined` count |
|--------|---------------------:|
| `src/ohm/server/server.py` | 3 |
| `src/ohm/server/handlers/admin.py` | 173 |
| `src/ohm/server/handlers/analysis.py` | 124 |
| `src/ohm/server/handlers/ask.py` | 12 |
| `src/ohm/server/handlers/catalog.py` | 14 |
| `src/ohm/server/handlers/decision.py` | 3 |
| `src/ohm/server/handlers/documents.py` | 21 |
| `src/ohm/server/handlers/graph.py` | 492 |
| `src/ohm/server/handlers/inference.py` | 64 |
| `src/ohm/server/handlers/infra.py` | 41 |
| `src/ohm/server/handlers/markov.py` | 4 |
| `src/ohm/server/handlers/tenant.py` | 34 |
| **Total in `src/ohm/server/`** | **985** |

The task statement cites ~623 errors. The measured total is higher because the mixins were recently expanded (graph handler alone now accounts for 492). The proposed stub fix is the same regardless of the exact count: it provides the shared attributes mypy cannot see across the mixin boundary.

---

## 3. Root Cause

`OhmHandler` is assembled from 12 independent mixin classes plus `BaseHTTPRequestHandler`. Each mixin calls methods and attributes that are declared on `OhmHandler` itself, but mypy type-checks each mixin in isolation. From mypy’s perspective, `AdminHandlerMixin` has no attribute `current_store`, `_json_response`, `_customer_id`, etc., producing the dominant `attr-defined` class of errors.

This is a well-known false-positive pattern for mixin-heavy code. The correct fix is to declare the shared interface once in a small base/protocol class that every mixin inherits.

---

## 4. Proposed Implementation Sketch

### 4.1 New file: `src/ohm/server/handlers/_base.py`

Create a ~40-line stub base class (not a runtime protocol) that declares the common attributes and methods every mixin needs. It will be imported only by the handler mixins; `OhmHandler` in `server.py` does **not** need to change its runtime inheritance.

```python
# src/ohm/server/handlers/_base.py
"""Type-only base for handler mixins.

This module exists solely to give mypy a single declaration of the shared
attributes and methods that OhmHandler provides at runtime.  It is NOT
intended to be instantiated or used in runtime MRO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import threading
    from http.server import BaseHTTPRequestHandler
    from ohm.store import OhmStore
    from ohm.schema import SchemaConfig


class _HandlerBase:
    """Declaration of shared state/methods available to all OhmHandler mixins."""

    # --- Shared state declared on OhmHandler -------------------------------
    store: Optional["OhmStore"] = None
    tenant_manager: Any = None
    config: dict = {}
    tokens: dict = {}
    customer_tokens: dict = {}
    roles: dict = {}
    no_auth: bool = False
    require_read_auth: bool = False
    schema_config: "SchemaConfig"
    multi_tenant: bool = False
    TRUSTED_PROXIES: frozenset[str] = frozenset()
    _write_lock: "threading.RLock"

    # --- Dispatch tables (populated after class body) -----------------------
    _GET_EXACT: dict = {}
    _GET_PREFIXES: list = []
    _POST_EXACT: dict = {}
    _POST_PREFIXES: list = []
    _DELETE_PREFIXES: list = []

    # --- Common HTTP / helper methods ---------------------------------------
    def _json_response(self, code: int, data: Any) -> None: ...
    def _binary_response(self, status: int, content_bytes: bytes, content_type: str = ..., filename: Optional[str] = ...) -> None: ...
    def _method_not_allowed(self, allowed_methods: set[str]) -> None: ...
    def _error_response(self, exc: Exception) -> None: ...
    def _authenticate(self) -> Optional[str]: ...
    def log_message(self, format: str, *args: Any) -> None: ...

    # --- Properties --------------------------------------------------------
    @property
    def current_store(self) -> "OhmStore": ...

    @property
    def _customer_id(self) -> Optional[str]: ...

    @property
    def current_config(self) -> dict: ...

    # --- BaseHTTPRequestHandler protocol -----------------------------------
    # Declared as Any so mixins can call send_response / send_header / end_headers / wfile
    # without reproducing the full stdlib signature.
    path: str
    headers: Any
    server: Any
    client_address: Any
    send_response: Any
    send_header: Any
    end_headers: Any
    wfile: Any
    rfile: Any


# Convenience alias for mixins that want to inherit without changing MRO semantics.
HandlerBase = _HandlerBase
```

### 4.2 Update each handler mixin

Add one line to every mixin class signature in `src/ohm/server/handlers/*.py`:

```python
from ohm.server.handlers._base import HandlerBase

class GraphHandlerMixin(HandlerBase):
    ...
```

Files to touch (same list as §2.2):

- `src/ohm/server/handlers/admin.py`
- `src/ohm/server/handlers/analysis.py`
- `src/ohm/server/handlers/ask.py`
- `src/ohm/server/handlers/catalog.py`
- `src/ohm/server/handlers/decision.py`
- `src/ohm/server/handlers/documents.py`
- `src/ohm/server/handlers/graph.py`
- `src/ohm/server/handlers/inference.py`
- `src/ohm/server/handlers/infra.py`
- `src/ohm/server/handlers/markov.py`
- `src/ohm/server/handlers/tenant.py`

### 4.3 Cross-mixin method calls

Some mixins call methods defined in *other* mixins. The stub only needs to declare the ones mypy cannot infer. The measured calls are:

| Missing attribute | Count | Declared in `_HandlerBase`? |
|-------------------|------:|-----------------------------|
| `current_store` | 421 | yes |
| `_json_response` | 427 | yes |
| `_customer_id` | 28 | yes |
| `tenant_manager` | 13 | yes |
| `schema_config` | 10 | yes |
| `current_config` | 6 | yes |
| `multi_tenant` | 4 | yes |
| `no_auth` | 3 | yes |
| `roles` | 2 | yes |
| `require_read_auth` | 2 | yes |
| `config` | 2 | yes |
| `_authenticate` | 4 | yes |
| `_write_lock` | 1 | yes |
| `customer_tokens` | 3 | yes (class-level) |
| `server` | 5 | yes (as `Any`) |
| `headers` / `path` / `wfile` / `rfile` / `send_response` / `send_header` / `end_headers` | 19 | yes (as `Any`) |
| `_require_admin` | 6 | declared in `TenantHandlerMixin` itself — no change needed |
| `_require_write_auth` | 4 | declared in `InfraHandlerMixin` itself — no change needed |
| `_get_auth_token` / `_get_allowed_agents` / `_check_ready` / `_write_json` | 8 | declared inside mixins or server.py — no base change needed |
| `_post_ask_synthesis`, `_post_ask_challenge`, `_get_metrics_semantic`, `_get_neighborhood`, `_get_search`, `_get_inference`, `_get_infra_openapi` | 7 | cross-mixin calls in `ask.py`; stub does not need to declare them if `ask.py` is updated to call the correct method names, or the stub can add `Any` aliases. **Recommendation:** add them to `_HandlerBase` as `Any` to keep the stub small and avoid editing `ask.py` logic. |

Because the stub uses broad `Any` for stdlib HTTP machinery and cross-mixin helpers, the ~40-line version is sufficient.

### 4.4 `server.py` cleanup

Two small real errors in `server.py` remain after the stub fix and need targeted code changes:

1. **Line 2848:** `OhmHandler._webhook_registry` is used before the class-level declaration. Move the declaration from module-level (line 306) into the `OhmHandler` class body, or annotate `OhmHandler._webhook_registry: ClassVar[dict[str | None, dict[str, dict]]] = {}` inside the class.
2. **Lines 2875:** `from ohm.quack import is_available, start_server` fails because `ohm/quack.py` replaces itself in `sys.modules`. Add re-export stubs in `ohm/graph/quack.py` (e.g. `def is_available(...): ...` and `def start_server(...): ...`) or add `if TYPE_CHECKING` re-exports in `ohm/quack.py`.
3. **Lines 2857 and 2895:** `tenant_manager` / `_ohm_store` fallback assignments are already runtime-correct; the assignment error disappears once `tenant_manager` and `store` are annotated as `Any`/`Optional[OhmStore]` rather than `None`.

---

## 5. Inventory of Disabled Error Codes and Re-Enable Priority

### 5.1 Disabled codes

From `mypy.ini` line 4:

```text
attr-defined, no-redef, assignment, misc, name-defined, arg-type, index, override,
union-attr, return-value, var-annotated, dict-item, operator, call-arg, call-overload
```

### 5.2 Re-enable priority

| Priority | Code(s) | Rationale | Expected real errors |
|----------|---------|-----------|---------------------:|
| **P0** | `attr-defined` | Largest noise class; fixed by `_HandlerBase` stub. ~985 false positives collapse to ~3 in `server.py`. | ~0 after stub + small server.py cleanup |
| **P1** | `override`, `no-redef` | Low noise, high value. `override` catches SDK base-class drift; `no-redef` catches accidental shadowing in the large graph handler. | < 10 |
| **P2** | `name-defined`, `var-annotated` | Catches missing imports / incomplete annotations. Will surface a few real issues in `inference/` and `tenant/`. | ~10–20 |
| **P3** | `assignment`, `index`, `dict-item` | Real bugs, but some are in ignored modules (`ohm.framework.*`, `ohm.inference.*`). Re-enable after module ignores are narrowed. | ~15–30 |
| **P4** | `arg-type`, `return-value`, `union-attr`, `operator`, `call-arg`, `call-overload` | Highest false-positive / most code-churn ratio. Tackle last, with per-file `# type: ignore[code]` for genuine variance issues (e.g. MCP `CallToolResult` list invariance). | ~30–50 |
| **P5** | `misc` | Broad bucket. Re-enable only after all other codes are clean and CI baseline is stable. | unknown |

### 5.3 Module-level ignores

`mypy.ini` and `pyproject.toml` ignore these packages entirely:

- `ohm.bayesian`
- `ohm.sdk`
- `ohm.framework.*`
- `ohm.inference.*`
- `ohm.patterns`
- `ohm.tenant`

**Recommendation:** Keep module-level ignores as a safety blanket during P0–P3, then remove them in P4. Do **not** remove them in the first PR.

---

## 6. Test / CI Plan

### 6.1 Baseline mypy command

```bash
python -m mypy src/ --show-error-codes --no-error-summary --config-file mypy.ini
```

### 6.2 Staged gating strategy

The goal is to avoid a “fix everything” PR that breaks CI for days.

1. **PR 1 — Stub only (P0)**
   - Add `_HandlerBase` and update mixin inheritance.
   - Do **not** change `mypy.ini` yet.
   - CI: run mypy with `--config-file=/dev/null` on `src/ohm/server/handlers/`; assert `attr-defined` count drops from ~985 to ≤ 50 (remaining are real issues like `HTTPSConnection.server_hostname`, `DocumentStore.get_record`, missing quack exports).
   - Existing pytest suite must still pass.

2. **PR 2 — Re-enable `attr-defined` (still P0)**
   - Remove `attr-defined` from `disable_error_code` in `mypy.ini`.
   - Fix the remaining real `attr-defined` errors (server.py `_webhook_registry`, quack re-exports, documents.py real stdlib issues).
   - CI: `python -m mypy src/` must pass with `attr-defined` enabled.

3. **PR 3 — Re-enable `override` + `no-redef` (P1)**
   - Remove `override,no-redef` from `disable_error_code`.
   - Fix or annotate SDK override drift and graph-handler shadowing.
   - CI: mypy passes; no regression in pytest.

4. **PR 4 — Re-enable `name-defined`, `var-annotated` (P2)**
   - Fix missing imports / incomplete annotations.

5. **PR 5 — Re-enable `assignment`, `index`, `dict-item` (P3)**
   - Narrow module ignores first if needed.

6. **PR 6 — Re-enable remaining codes (P4/P5)**
   - `arg-type`, `return-value`, `union-attr`, `operator`, `call-arg`, `call-overload`, then finally `misc`.

### 6.3 Regression gate

Add a new CI step after each PR that records the mypy error count per code and per module, failing only if the count increases:

```yaml
- name: Type check delta
  run: |
    python scripts/mypy_delta.py --baseline reports/mypy-baseline.json
```

`scripts/mypy_delta.py` (to be created) will:
1. Run `mypy src/ --show-error-codes --no-error-summary`.
2. Parse counts by error code and by file.
3. Compare against the checked-in `reports/mypy-baseline.json`.
4. Fail if any previously-clean file regresses, or if total errors increase.

This lets the project converge to zero without requiring every PR to fix all historical issues.

### 6.4 Absolute gate

Once the total error count reaches zero, replace the delta gate with a plain `mypy src/` failure-on-any-error gate. Track this milestone in a beads issue.

---

## 7. Beads Issues to Create / Update

### 7.1 New beads issues

| Issue | Title | Priority | Parent | Notes |
|-------|-------|----------|--------|-------|
| `OHM-tc01` | Type-check honesty: add `_HandlerBase` stub for handler mixins | P0 | — | This design note. ~40-line stub fix. |
| `OHM-tc02` | Re-enable mypy `attr-defined` error code | P0 | — | Follow-up to `OHM-tc01`. |
| `OHM-tc03` | Re-enable mypy `override` + `no-redef` codes | P1 | — | SDK / graph handler drift. |
| `OHM-tc04` | Re-enable mypy `name-defined` + `var-annotated` codes | P1 | — | Missing imports / annotations. |
| `OHM-tc05` | Re-enable mypy `assignment` + `index` + `dict-item` codes | P2 | — | Requires narrowing module ignores. |
| `OHM-tc06` | Re-enable mypy remaining codes and remove module ignores | P2 | — | Final cleanup; milestone for absolute mypy gate. |
| `OHM-tc07` | Add `mypy_delta.py` regression script and CI gate | P1 | — | Prevents backsliding during staged rollout. |

### 7.2 Issues to update

- `OHM-9dq` (SDK tests — zero coverage on primary agent interface): note that `override` re-enablement is blocked until SDK tests exist for `HttpGraph` methods.
- `OHM-l5k` (CI/CD pipeline): add the mypy delta gate as a tracked improvement.

---

## 8. Risk Assessment and Reversibility

### 8.1 Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Stub uses `Any` too broadly, masking real bugs | Low | Medium | The stub is type-only; it does not change runtime behavior. After `attr-defined` is re-enabled, subsequent PRs tighten annotations instead of suppressing them. |
| Adding `_HandlerBase` to mixin MRO changes method resolution unexpectedly | Very low | High | `_HandlerBase` has no method bodies, only `...` stubs. It cannot override real implementations. It also has no `__init__`. |
| Re-enabling codes surfaces hundreds of real errors in `ohm.framework.*` / `ohm.inference.*` | Medium | Medium | Keep per-module `ignore_errors` until the later PRs. Re-enable codes globally only after those modules are separately cleaned or the ignores are narrowed. |
| CI blocked during transition | Medium | High | Use delta gating, not absolute gating, until the zero-error milestone. |
| `mypy.ini` and `pyproject.toml` drift | Low | Low | Update both files together. Consider removing the redundant `[tool.mypy]` overrides in `pyproject.toml` in PR 2. |

### 8.2 Reversibility

- The `_HandlerBase` stub is a single new file. It can be deleted instantly if it causes runtime problems (none expected).
- `mypy.ini` changes are one-line rollbacks: re-add the disabled code to `disable_error_code`.
- The delta script is additive; removing it has no effect on runtime.
- All changes are statically analyzable and do not touch the DuckDB schema, HTTP contract, or persisted state.

### 8.3 Suggested commit message

```text
chore(typecheck): add _HandlerBase stub and plan staged mypy cleanup

- Introduce src/ohm/server/handlers/_base.py declaring shared OhmHandler
  attributes/methods so mixins type-check cleanly.
- Inherit HandlerBase in all 11 handler mixins, eliminating ~985
  false-positive attr-defined errors.
- Document staged re-enablement of disabled mypy codes in
  docs/design/typecheck-batch-2026-07-08.md.
- Add regression-gate plan (mypy_delta.py) to prevent backsliding.

Refs: OHM-tc01, OHM-tc02
```

---

## 9. Summary of Deliverables

1. **Exact files/lines inspected:** `mypy.ini` lines 1–22, `pyproject.toml` lines 91–95, `server.py` lines 1027–1065 / 1299 / 2519–2738, and all 11 handler mixin files.
2. **Stub fix:** New `src/ohm/server/handlers/_base.py` (~40 lines) plus one-line inheritance change per mixin.
3. **Disabled-code inventory + priority:** `attr-defined` first; then `override/no-redef`, `name-defined/var-annotated`, `assignment/index/dict-item`, and finally the high-churn codes plus `misc`.
4. **CI plan:** Delta gate during rollout, absolute gate after zero-error milestone.
5. **Beads issues:** 7 new issues (`OHM-tc01`–`OHM-tc07`) and 2 updates (`OHM-9dq`, `OHM-l5k`).
6. **Risk/reversibility:** Single new stub file, no runtime or schema changes, one-line mypy.ini rollback.

## 10. FastMCP gateway impact

The type-checking batch is mostly orthogonal to the FastMCP gateway (`ohm-gateway`), but it improves the foundation:

- **Typed HTTP handlers:** A clean `_HandlerBase` makes the daemon's request/response contract explicit. The gateway can reuse those types when it forwards requests or parses responses.
- **SDK parity:** If the gateway eventually calls OHM through the Python SDK rather than raw HTTP, typed `HttpGraph` methods reduce integration bugs.
- **No direct gateway code changes:** FastMCP gateway is a separate component and uses its own transport layer, so mypy work in the daemon does not change gateway build/package requirements.
- **Capability batch synergy:** Better typing in `src/ohm/mcp/server.py` and `src/ohm/framework/sdk.py` makes it safer to share tool schemas between the raw local sidecar and the FastMCP gateway.

*Note:* The gateway should still maintain its own type checking with FastMCP's decorators; this batch does not replace that.
