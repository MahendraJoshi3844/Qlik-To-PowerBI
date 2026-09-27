"""Fidelity tiers and findings - the vocabulary of the migration report.

Every object the migration touches ends in exactly one fidelity tier. The tier
is a promise to the customer about how much they can trust the output:

* EXACT    - semantics preserved; no review needed.
* ASSUMED  - converted, but the result depends on a stated assumption about how
             the MicroStrategy project behaves (a VLDB setting, a filter-merge
             rule, a calendar). Emitted, and the assumption is written down.
* MANUAL   - not converted. A placeholder is emitted (so the model still opens)
             and the finding says what a person has to build.
* UNSUPPORTED - no Power BI equivalent; a redesign decision is required.
* INFO     - advice, not a defect (e.g. "use sync slicers across these pages").

Nothing is dropped silently: an object that is not in the output has a finding
that says why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Fidelity(str, Enum):
    EXACT = "exact"
    ASSUMED = "assumed"
    MANUAL = "manual"
    UNSUPPORTED = "unsupported"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Higher is worse - used to combine tiers (the worst one wins)."""
        return {
            Fidelity.INFO: 0,
            Fidelity.EXACT: 1,
            Fidelity.ASSUMED: 2,
            Fidelity.MANUAL: 3,
            Fidelity.UNSUPPORTED: 4,
        }[self]


def worst(*tiers: Fidelity) -> Fidelity:
    real = [t for t in tiers if t is not Fidelity.INFO]
    if not real:
        return Fidelity.EXACT
    return max(real, key=lambda t: t.rank)


@dataclass(frozen=True)
class Finding:
    """One line of the migration report."""

    stage: str  # acquire | assess | model | translate | layout | emit | validate
    object_type: str  # metric, attribute, dossier, visualization, prompt, ...
    object_name: str
    fidelity: Fidelity
    message: str
    #: What a person should do about it (empty for EXACT / INFO).
    action: str = ""
    #: The original MicroStrategy text, where there is one.
    source: str = ""

    def sort_key(self) -> tuple:
        return (-self.fidelity.rank, self.stage, self.object_type, self.object_name, self.message)


@dataclass
class FindingLog:
    items: list[Finding] = field(default_factory=list)

    def add(
        self,
        stage: str,
        object_type: str,
        object_name: str,
        fidelity: Fidelity,
        message: str,
        action: str = "",
        source: str = "",
    ) -> Finding:
        f = Finding(stage, object_type, object_name, fidelity, message, action, source)
        if f not in self.items:  # the same fact reported twice is noise, not emphasis
            self.items.append(f)
        return f

    def sorted(self) -> list[Finding]:
        return sorted(self.items, key=Finding.sort_key)

    def count(self, fidelity: Fidelity) -> int:
        return sum(1 for f in self.items if f.fidelity is fidelity)
