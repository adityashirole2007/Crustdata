"""Typed objects passed between pipeline stages.

Everything that crosses a stage boundary is one of these. The citation guard
depends on Evidence being a first-class object rather than a free-text string.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

EvidenceSource = Literal[
    "crustdata_enrich",
    "crustdata_search",
    "crustdata_person_enrich",
    "crustdata_jobs_search",
    "crustdata_web_search",
    "crustdata_web_fetch",
    "crustdata_watcher",
    "ibbi_live",
    "registry_cached_snapshot",
    "procurement_ledger",
    "replay_fixture",
]


@dataclass(frozen=True)
class Evidence:
    """A single verifiable provenance record.

    `field_path` is the dotted path inside the source payload the value came
    from — e.g. "headcount.total". `from_value`/`to_value` are set for deltas.
    """

    source: EvidenceSource
    source_url: str
    fetched_at: str
    field_path: str
    as_of: str | None = None
    from_value: Any = None
    to_value: Any = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Claim:
    """A sentence the product is willing to put in front of a procurement lead.

    Nothing reaches the renderer without `evidence`. See evidence.CitationGuard.
    """

    text: str
    kind: Literal[
        "numeric_change", "status_change", "membership_change", "observation", "judgement"
    ]
    evidence: tuple[Evidence, ...] = ()
    confidence: Literal["high", "medium", "low"] = "medium"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [e.to_dict() for e in self.evidence]
        return d


# --------------------------------------------------------------------------- #
# Vendor / snapshot / diff
# --------------------------------------------------------------------------- #


@dataclass
class Vendor:
    vendor_id: str
    name: str
    domain: str | None = None
    cin: str | None = None
    gstin: str | None = None
    open_po_value_inr: float = 0.0
    sole_source: bool = False
    lead_time_days: int = 0
    critical_component: bool = False
    category_manager: str | None = None
    crustdata_company_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Snapshot:
    vendor_id: str
    as_of: str
    payload: dict[str, Any]
    source: str
    fetched_at: str
    snapshot_id: int | None = None


class DeltaKind(str, Enum):
    NUMERIC = "numeric"
    SCALAR = "scalar"
    ARRAY_ADDED = "array_added"
    ARRAY_REMOVED = "array_removed"


@dataclass(frozen=True)
class Delta:
    """One typed change emitted by the deterministic diff engine."""

    field_path: str
    kind: DeltaKind
    from_value: Any
    to_value: Any
    pct_change: float | None = None
    from_as_of: str | None = None
    to_as_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


# --------------------------------------------------------------------------- #
# Signals / scores
# --------------------------------------------------------------------------- #


@dataclass
class SignalResult:
    signal_id: str
    archetype: Literal["legitimacy", "distress"]
    fired: bool
    value: Any
    direction: Literal["worsening", "improving", "neutral"]
    weight: float
    contribution: float            # weighted points this signal contributed
    confidence: Literal["high", "medium", "low"]
    rationale: str
    evidence: list[Evidence] = field(default_factory=list)
    data_available: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [e.to_dict() for e in self.evidence]
        return d


@dataclass
class Score:
    archetype: Literal["legitimacy", "distress"]
    value: float
    band: str
    signals: list[SignalResult] = field(default_factory=list)
    overridden_by: str | None = None
    coverage: float = 1.0          # fraction of signals with data available

    def to_dict(self) -> dict[str, Any]:
        return {
            "archetype": self.archetype,
            "value": round(self.value, 1),
            "band": self.band,
            "overridden_by": self.overridden_by,
            "coverage": round(self.coverage, 2),
            "signals": [s.to_dict() for s in self.signals],
        }


@dataclass
class RegistryFact:
    """An authoritative registry observation. Never inferred, never invented."""

    provider: str
    kind: str                       # ibbi_cirp_admitted | mca_strike_off | ...
    status: str
    as_of: str
    source_url: str
    fetched_at: str
    live: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def to_evidence(self) -> Evidence:
        return Evidence(
            source="ibbi_live" if self.live else "registry_cached_snapshot",
            source_url=self.source_url,
            fetched_at=self.fetched_at,
            field_path=f"{self.provider}.status",
            as_of=self.as_of,
            to_value=self.status,
            note=None if self.live else "cached_snapshot — not a live lookup",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Alerts / case files
# --------------------------------------------------------------------------- #


@dataclass
class Alert:
    alert_id: str
    vendor_id: str
    vendor_name: str
    created_at: str
    as_of: str
    severity: str                  # band name
    priority: str                  # P1 | P2 | P3 | LOG
    priority_score: float
    distress: Score | None
    legitimacy: Score | None
    exposure_factor: float
    claims: list[Claim] = field(default_factory=list)
    recommended_action: str = ""
    drafted_memo: str = ""
    investigation: dict[str, Any] = field(default_factory=dict)
    suppressed: bool = False
    suppression_reason: str | None = None
    registry_facts: list[RegistryFact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "created_at": self.created_at,
            "as_of": self.as_of,
            "severity": self.severity,
            "priority": self.priority,
            "priority_score": round(self.priority_score, 1),
            "exposure_factor": round(self.exposure_factor, 3),
            "distress": self.distress.to_dict() if self.distress else None,
            "legitimacy": self.legitimacy.to_dict() if self.legitimacy else None,
            "claims": [c.to_dict() for c in self.claims],
            "recommended_action": self.recommended_action,
            "drafted_memo": self.drafted_memo,
            "investigation": self.investigation,
            "suppressed": self.suppressed,
            "suppression_reason": self.suppression_reason,
            "registry_facts": [r.to_dict() for r in self.registry_facts],
        }
