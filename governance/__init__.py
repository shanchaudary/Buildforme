"""Stage 5.6 — AI Constitution & Governance Engine package.

Provider-independent constitutional law. Not Stage 5.5 run validators
(those live in buildforme.governance). Boundaries stay separate (LAW-008).
"""

from __future__ import annotations

from governance.constitution_engine import (
    ConstitutionEngine,
    get_engine,
    load_constitution,
)

__all__ = [
    "ConstitutionEngine",
    "get_engine",
    "load_constitution",
]
