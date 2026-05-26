# Utility USD Per Day — Data Gap Resolution

**Author:** Métis (OHM-od01.6)
**Date:** 2026-05-26

## Summary

All 4 decision nodes in the OHM knowledge graph previously had `utility_usd_per_day = NULL`, blocking VoI (Value of Information) calculations, game theory payoff USD normalization, and POMDP decision intelligence. This document records the assigned values, sources, and reasoning.

---

## 1. `decision-hormuz-response` — Hormuz Strait Response

| Field | Value |
|---|---|
| `utility_usd_per_day` | $5,000,000,000 (5B) |
| `utility_scale` | 0.85 (unchanged) |
| `utility_currency` | USD |

**Reasoning:**
- EIA reports **20 million barrels/day** transited the Strait of Hormuz in 2024 (20% of global petroleum consumption).
- At Q2 2025 Brent crude spot prices (~$74/bbl), the direct oil transit value is **~$1.48B/day**.
- Broader supply chain disruption costs (alternative routes, insurance premiums, spot-market price spikes, shipping delays, manufacturing slowdowns) add 2-3x the direct oil impact.
- UNCTAD and LSE analyses estimate total economic impact of a Hormuz closure at $5-8B/day including inflation and growth shocks.
- **Conservative estimate:** $5B/day represents the lower bound of total economic impact.

**Sources:**
- EIA Today in Energy, June 2025 — 20M b/d through Hormuz
- UNCTAD — Hormuz disruption deepens global economic strain
- LSE Business Review — "A short closure is an oil shock; a long closure is an inflation and growth shock"
- Reuters — Regional impact analysis

---

## 2. `decision-agent-governance-standard` — Agent Governance Standard

| Field | Value |
|---|---|
| `utility_usd_per_day` | $167,000 |
| `utility_scale` | 0.90 (unchanged) |
| `utility_currency` | USD |

**Reasoning:**
- IBM Ponemon 2024: Average cost of a data breach is $4.88M.
- For AI agent-specific breaches (autonomous code execution, data exfiltration, tool misuse), costs are higher due to autonomous action potential and speed.
- CrowdStrike 2025 Threat Hunting Report: 136% increase in AI-targeted attacks. PraisonAI CVE weaponized within 4 hours of disclosure (May 2026).
- A single AI agent governance failure costs $3-7M (incident response, remediation, regulatory fines, reputational damage, business interruption).
- Using $5M as the base cost, amortized over a 30-day decision window: **$167K/day**.
- This is conservative — repeated breaches at scale could be 10x higher, but governance standards also have implementation costs.

**Sources:**
- IBM / Ponemon Cost of a Data Breach Report 2024
- CrowdStrike 2025 Threat Hunting Report
- PraisonAI CVE-2026 weaponization in 4 hours (May 2026)

---

## 3. `decision-devenex-agent-execution` — Devenex Agent Execution Control

| Field | Value |
|---|---|
| `utility_usd_per_day` | $500,000 |
| `utility_scale` | 0.75 (was NULL — set to 0.75) |
| `utility_currency` | USD |

**Reasoning:**
- Devenex enforces AND-gate execution: verified identity AND approved tool AND scoped action AND audit trail.
- Without this AND-gate, an uncontrolled agent could execute unauthorized trades, deploy code to production, access sensitive APIs, or self-modify.
- The AND-gate prevents OR-gate degradation under operational pressure — each condition is independently necessary.
- Cost of a single uncontrolled agent execution incident: $5-50M depending on scope (unauthorized trading, infrastructure compromise, data exfiltration, regulatory liability).
- Using $45M as base incident cost (upper range for infrastructure compromise), amortized over a 90-day decision window: **$500K/day**.
- The AND-gate is a high-stakes decision because it controls the difference between controlled and uncontrolled execution, not just a governance policy.

**Sources:**
- CrowdStrike 2025 Threat Hunting Report — AI agent targeting
- PraisonAI CVE — agent framework exploitation
- Industry average: autonomous agent incidents at enterprise scale

---

## 4. `ariana_college_decision_2026` — Ariana College Decision

| Field | Value |
|---|---|
| `utility_usd_per_day` | $137 |
| `utility_scale` | 0.30 (unchanged) |
| `utility_currency` | USD |

**Reasoning:**
- Test/example decision node with low utility scale (0.30 — personal decision, not systemic).
- $50K/year typical college cost differential (tuition, room/board) between options.
- Daily: $50,000 / 365 ≈ **$137/day**.

**Sources:**
- Common college cost estimates, 2026

---

## VoI Verification

The VoI endpoint now returns non-zero USD-normalized scores:

```
GET /voi?target=hormuz_and_gate
→ {
    "decision_nodes": ["ariana_college_decision_2026", "decision-hormuz-response", ...],
    "rankings": [{
        "node_id": "concept-tiered-transit-system",
        "voi_score": 2,441,409,371,
        "uncertainty": 0.5744,
        "sensitivity": 4,250,050,219,
        ...
    }]
}
```

The $2.4B VoI score on `concept-tiered-transit-system` reflects its USD-normalized impact via the `decision-hormuz-response` decision node — confirming utility data propagation works.

---

## Impact

With `utility_usd_per_day` populated on all decision nodes:
1. **VoI scores are now USD-normalized** — research priorities reflect actual dollar impact
2. **Game theory payoffs** can compute USD-denominated payoff matrices
3. **POMDP decision intelligence** can use daily utility as reward function
4. **Downstream decisions** correctly inherit sensitivity-weighted utility from upstream concepts
