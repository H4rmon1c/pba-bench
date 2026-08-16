"""Vector abstraction: a family of worst-case validation constructions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Vector:
    """Base class for a worst-case validation vector.

    Subclasses set the class attributes below. A vector does not itself execute
    anything against a node; it describes *what* a construction does and *why*,
    so that the benchmark's measurements and documentation stay honest about
    which consensus/policy rules are being exercised.
    """

    #: Unique machine name (used for --vector).
    name: str
    #: One-line human description.
    description: str
    #: Whether this vector is actually implemented and testable end-to-end.
    implemented: bool = False
    #: Whether this vector is a byte-for-byte reproduction of a public demo.
    reproducible_from_public_info: bool = False
    #: Preparation requirements (UTXOs, prep blocks, scripts, ...).
    preparation_requirements: list = field(default_factory=list)
    #: The cost drivers and their complexity, as a short list of strings.
    theoretical_counters: list = field(default_factory=list)
    #: Expected consensus/policy properties (e.g. which rules it triggers).
    expected_properties: list = field(default_factory=list)
    #: Safety constraints specific to this vector.
    safety_constraints: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "implemented": self.implemented,
            "reproducible_from_public_info": self.reproducible_from_public_info,
            "preparation_requirements": list(self.preparation_requirements),
            "theoretical_counters": list(self.theoretical_counters),
            "expected_properties": list(self.expected_properties),
            "safety_constraints": list(self.safety_constraints),
        }
