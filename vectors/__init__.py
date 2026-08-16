"""Attack/vector plugin registry for pba-bench.

A *vector* is a family of worst-case validation constructions. The registry lets
the benchmark evolve beyond a single scriptPubKey construction. Each vector
exposes name, description, preparation requirements, generation, expected
properties, theoretical counters, and safety constraints.

Registry::

    from vectors import get_vector, registered_vectors
    v = get_vector("scriptpubkey")
    print(v.name, v.description)
"""

from __future__ import annotations

from vectors.base import Vector
from vectors.scriptpubkey import ScriptPubKeyVector
from vectors.scriptsig import ScriptSigVector

#: name -> Vector instance (ordered, deterministic).
_VECTORS = {}
for _v in (ScriptPubKeyVector, ScriptSigVector):
    _VECTORS[_v.name] = _v


def registered_vectors() -> list:
    return sorted(_VECTORS.keys())


def get_vector(name: str) -> Vector:
    if name not in _VECTORS:
        raise KeyError(
            f"unknown vector {name!r}; choose from {registered_vectors()}")
    return _VECTORS[name]


__all__ = ["Vector", "registered_vectors", "get_vector"]
