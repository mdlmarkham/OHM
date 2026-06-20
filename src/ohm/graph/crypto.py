"""TELOS cryptographic signing for agent writes (OHM-enwb, ADR-035).

Provides canonical payload serialization and HMAC-SHA256 signing/verification
for node and edge writes. Ed25519 supported optionally via pynacl.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    pass

NODE_FIELDS = ("id", "label", "type", "content", "created_by", "confidence", "visibility", "provenance", "source_tier")
EDGE_FIELDS = ("id", "from_node", "to_node", "layer", "edge_type", "created_by", "confidence", "probability", "source_tier")


def canonical_payload(record: dict[str, Any], *, kind: str = "node") -> bytes:
    if kind not in ("node", "edge"):
        raise ValueError(f"kind must be 'node' or 'edge', got {kind}")
    fields = NODE_FIELDS if kind == "node" else EDGE_FIELDS
    payload = {}
    for f in fields:
        if f in record and record[f] is not None:
            payload[f] = record[f]
    if kind == "node" and "connects_to" in record and record["connects_to"]:
        payload["connects_to"] = sorted(record["connects_to"])
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_hmac(payload: bytes, key: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_hmac(payload: bytes, signature: str, key: bytes) -> bool:
    expected = sign_hmac(payload, key)
    return hmac.compare_digest(expected, signature)


def sign_write(
    record: dict[str, Any],
    *,
    kind: str,
    key: bytes,
    algorithm: str = "hmac-sha256",
    key_id: str = "default",
) -> dict[str, str]:
    if algorithm == "hmac-sha256":
        payload = canonical_payload(record, kind=kind)
        sig = sign_hmac(payload, key)
        return {
            "write_signature": f"hmac-sha256:{sig}",
            "signing_key_id": key_id,
            "signed_at": datetime.now().isoformat(),
        }
    elif algorithm == "ed25519":
        try:
            from nacl.signing import SigningKey
            from nacl.encoding import HexEncoder
        except ImportError:
            raise ImportError("ed25519 signing requires pynacl: pip install pynacl")
        payload = canonical_payload(record, kind=kind)
        sk = SigningKey(key)
        sig = sk.sign(payload).signature.decode("ascii")
        return {
            "write_signature": f"ed25519:{sig}",
            "signing_key_id": key_id,
            "signed_at": datetime.now().isoformat(),
        }
    else:
        raise ValueError(f"Unknown algorithm: {algorithm}")


def verify_write(
    record: dict[str, Any],
    *,
    kind: str,
    key: bytes,
) -> bool:
    sig = record.get("write_signature")
    if not sig:
        return False
    if ":" not in sig:
        return False
    algorithm, sig_hex = sig.split(":", 1)
    payload = canonical_payload(record, kind=kind)
    if algorithm == "hmac-sha256":
        return verify_hmac(payload, sig_hex, key)
    elif algorithm == "ed25519":
        try:
            from nacl.signing import VerifyKey
            from nacl.encoding import HexEncoder
        except ImportError:
            return False
        try:
            vk = VerifyKey(key)
            vk.verify(payload, HexEncoder.decode(sig_hex.encode("ascii")))
            return True
        except Exception:
            return False
    return False


__all__ = [
    "canonical_payload",
    "sign_hmac",
    "verify_hmac",
    "sign_write",
    "verify_write",
    "NODE_FIELDS",
    "EDGE_FIELDS",
]
