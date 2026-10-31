"""The RegistryProvider interface.

Written as a real interface rather than a one-off function because the
production path is a licensed data reseller (Surepass / Signzy / Karza /
Perfios / InstaFinancials) and swapping one in should be a new subclass and a
config line, not a rewrite. The honest hackathon answer is: IBBI is live here,
MCA and GST are cached with dates, and the seam between them is this class.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..models import RegistryFact, utcnow


@dataclass
class RegistryResult:
    ok: bool
    facts: list[RegistryFact] = field(default_factory=list)
    reason: str | None = None
    provider: str = ""
    checked_at: str = field(default_factory=utcnow)

    @property
    def unavailable(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "provider": self.provider, "reason": self.reason,
            "checked_at": self.checked_at,
            "facts": [f.to_dict() for f in self.facts],
        }


class RegistryProvider(ABC):
    name: str = "registry"
    live: bool = False

    @abstractmethod
    def lookup(self, *, cin: str | None = None, gstin: str | None = None,
               name: str | None = None, as_of: str | None = None) -> RegistryResult:
        """Return facts, or ok=False with a reason.

        Implementations MUST NOT guess. If a lookup cannot be performed, return
        ok=False. A provider that returns a plausible-looking invented status is
        worse than no provider at all.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name} live={self.live}>"
