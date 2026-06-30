"""Hyperdimensional (HD) computing module for node fingerprinting.

ADR-031: Tastebud-style HD fingerprints for OHM nodes.
Provides bind/disbind, majority-rule bundling, Hamming similarity,
and text/node fingerprinting. Pure computation — no DB connection needed.

Reference: github.com/Mikhail-Za/tastebud-memory
Kanerva 2009: 10,000-bit binary hypervectors with XOR binding.
"""

from __future__ import annotations

import hashlib
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class HDError(ValueError):
    pass


DEFAULT_DIM = 10000
DEFAULT_SEED = 42
FP_VERSION = "tastebud_hd_v1"


def _seeded_bitstream(seed: int, length: int) -> bytes:
    h = hashlib.sha256(struct.pack(">II", seed, 0)).digest()
    buf = bytearray()
    counter = 0
    while len(buf) < length:
        h = hashlib.sha256(struct.pack(">II", seed, counter) + h).digest()
        buf.extend(h)
        counter += 1
    return bytes(buf[:length])


def random_vector(*, dim: int = DEFAULT_DIM, seed: int = DEFAULT_SEED) -> bytearray:
    if dim <= 0:
        raise HDError(f"dim must be positive, got {dim}")
    n_bytes = (dim + 7) // 8
    raw = _seeded_bitstream(seed, n_bytes)
    vec = bytearray(raw)
    vec[-1] &= (1 << (dim % 8)) - 1 if dim % 8 else 0xFF
    return vec


def _base_vector(primitive: str, *, dim: int = DEFAULT_DIM, seed: int = DEFAULT_SEED) -> bytearray:
    if not primitive:
        raise HDError("primitive must be non-empty")
    tok_seed = seed + hash(primitive) % (2**31)
    return random_vector(dim=dim, seed=tok_seed)


def bind(a: bytearray, b: bytearray) -> bytearray:
    if len(a) != len(b):
        raise HDError(f"vector length mismatch: {len(a)} vs {len(b)}")
    return bytearray(x ^ y for x, y in zip(a, b))


def disbind(composite: bytearray, pattern: bytearray) -> bytearray:
    return bind(composite, pattern)


def majority_rule(vectors: list[bytearray], *, dim: int = DEFAULT_DIM) -> bytearray:
    if not vectors:
        raise HDError("majority_rule requires at least one vector")
    n_bytes = (dim + 7) // 8
    for v in vectors:
        if len(v) != n_bytes:
            raise HDError(f"vector length {len(v)} != expected {n_bytes}")
    half = len(vectors) / 2.0
    result = bytearray(n_bytes)
    last_byte_bits = dim - (n_bytes - 1) * 8
    for b in range(n_bytes):
        bits_in_byte = 8 if b < n_bytes - 1 else last_byte_bits
        counts0 = 0
        counts1 = 0
        counts2 = 0
        counts3 = 0
        counts4 = 0
        counts5 = 0
        counts6 = 0
        counts7 = 0
        for v in vectors:
            byte = v[b]
            counts0 += (byte >> 7) & 1
            counts1 += (byte >> 6) & 1
            counts2 += (byte >> 5) & 1
            counts3 += (byte >> 4) & 1
            counts4 += (byte >> 3) & 1
            counts5 += (byte >> 2) & 1
            counts6 += (byte >> 1) & 1
            if bits_in_byte == 8:
                counts7 += byte & 1
        result_byte = 0
        if counts0 > half:
            result_byte |= 1 << 7
        if counts1 > half:
            result_byte |= 1 << 6
        if counts2 > half:
            result_byte |= 1 << 5
        if counts3 > half:
            result_byte |= 1 << 4
        if counts4 > half:
            result_byte |= 1 << 3
        if counts5 > half:
            result_byte |= 1 << 2
        if counts6 > half:
            result_byte |= 1 << 1
        if bits_in_byte == 8 and counts7 > half:
            result_byte |= 1 << 0
        result[b] = result_byte
    return result


def hamming_similarity(a: bytearray, b: bytearray) -> float:
    if len(a) != len(b):
        raise HDError(f"vector length mismatch: {len(a)} vs {len(b)}")
    total_bits = len(a) * 8
    diff = 0
    for x, y in zip(a, b):
        diff += (x ^ y).bit_count()
    return 1.0 - diff / total_bits


def fingerprint_text(
    text: str,
    *,
    dim: int = DEFAULT_DIM,
    seed: int = DEFAULT_SEED,
) -> bytearray:
    if not text or not text.strip():
        return bytearray((dim + 7) // 8)
    tokens = text.strip().split()
    if not tokens:
        return bytearray((dim + 7) // 8)
    vecs = [_base_vector(tok, dim=dim, seed=seed) for tok in tokens]
    return majority_rule(vecs, dim=dim)


def fingerprint_node(
    *,
    label: str,
    node_type: str,
    content: str | None = None,
    tags: list[str] | None = None,
    provenance: str | None = None,
    dim: int = DEFAULT_DIM,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    type_vec = _base_vector(node_type, dim=dim, seed=seed)
    label_vec = fingerprint_text(label, dim=dim, seed=seed)
    composite = bind(type_vec, label_vec)
    components = [composite]
    if content and content.strip():
        content_vec = fingerprint_text(content, dim=dim, seed=seed)
        components.append(content_vec)
    if tags:
        tag_vecs = [_base_vector(t, dim=dim, seed=seed) for t in tags]
        tag_bundle = majority_rule(tag_vecs, dim=dim)
        components.append(tag_bundle)
    if provenance and provenance.strip():
        prov_vec = _base_vector(provenance, dim=dim, seed=seed)
        components.append(prov_vec)
    if len(components) > 1:
        result = majority_rule(components, dim=dim)
    else:
        result = components[0]
    return {
        "fingerprint_hex": result.hex(),
        "dimension": dim,
        "seed": seed,
        "method": FP_VERSION,
        "components": ["type", "label"] + (["content"] if content else []) + (["tags"] if tags else []) + (["provenance"] if provenance else []),
    }


from typing import Any

__all__ = [
    "HDError",
    "DEFAULT_DIM",
    "DEFAULT_SEED",
    "FP_VERSION",
    "random_vector",
    "bind",
    "disbind",
    "majority_rule",
    "hamming_similarity",
    "fingerprint_text",
    "fingerprint_node",
]
