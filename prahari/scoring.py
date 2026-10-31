"""Explainable scoring.

Two scores, never one. Conflating them is the design mistake the whole product
is built to avoid:

  * Legitimacy  — "is this a real trading business?"  Static but fake.
  * Distress    — "will it fail to deliver or fail to get paid?"  Was real, now sinking.

They have opposite signatures and opposite remedies. A shell gets offboarded and
reported; a distressed incumbent gets a payment hold and a second-source plan.
A single composite number would tell a category manager to do the wrong thing.

Registry facts are not signals. They are authoritative and they OVERRIDE the
model: digital footprint is early and noisy, the registry is late and certain.
"""
from __future__ import annotations

from typing import Any

from .models import RegistryFact, Score, SignalResult


def _band_for(value: float, bands: dict[str, float], ascending: bool) -> str:
    """ascending=True  -> higher value means worse (distress).
       ascending=False -> lower value means worse (legitimacy)."""
    if ascending:
        for name in sorted(bands, key=lambda k: bands[k], reverse=True):
            if value >= bands[name]:
                return name
        return "clear"
    for name in sorted(bands, key=lambda k: bands[k]):
        if value <= bands[name]:
            return name
    return "clear"


def score_distress(signals: list[SignalResult], cfg: dict,
                   registry_facts: list[RegistryFact] | None = None) -> Score:
    """0 = healthy, 100 = failing. Sum of weighted contributions, normalised."""
    dcfg = cfg["distress"]
    relevant = [s for s in signals if s.archetype == "distress"]
    total_weight = sum(s.weight for s in relevant) or 1.0
    earned = sum(s.contribution for s in relevant)
    value = max(0.0, min(100.0, earned / total_weight * 100.0))

    available = [s for s in relevant if s.data_available]
    coverage = len(available) / len(relevant) if relevant else 0.0

    band = _band_for(value, dcfg["bands"], ascending=True)
    overridden_by = None

    for fact in registry_facts or []:
        rule = dcfg.get("registry_overrides", {}).get(fact.kind)
        if not rule:
            continue
        floor = float(rule.get("force_score_floor", 0))
        if floor > value:
            value = floor
            band = rule.get("force_band", band)
            overridden_by = f"{fact.provider}:{fact.kind}"
        elif rule.get("force_band"):
            band = rule["force_band"]
            overridden_by = f"{fact.provider}:{fact.kind}"

    return Score("distress", value, band, relevant, overridden_by, coverage)


def score_legitimacy(signals: list[SignalResult], cfg: dict,
                     registry_facts: list[RegistryFact] | None = None) -> Score:
    """100 = certainly a real trading business, 0 = presumed shell.

    Signals contribute PENALTIES here, so the arithmetic is inverted relative to
    distress. Coverage matters more for legitimacy than anywhere else: a vendor
    with no data is not a clean vendor, it is an unknown one, and the band is
    capped accordingly.
    """
    lcfg = cfg["legitimacy"]
    relevant = [s for s in signals if s.archetype == "legitimacy"]
    total_weight = sum(s.weight for s in relevant) or 1.0
    penalty = sum(s.contribution for s in relevant)
    value = max(0.0, min(100.0, 100.0 - (penalty / total_weight * 100.0)))

    available = [s for s in relevant if s.data_available]
    coverage = len(available) / len(relevant) if relevant else 0.0

    # Thin coverage cannot produce a confident clean bill of health.
    if coverage < 0.5:
        value = min(value, 70.0)

    band = _band_for(value, lcfg["bands"], ascending=False)
    overridden_by = None
    for fact in registry_facts or []:
        if fact.kind in ("mca_strike_off", "mca_under_strike_off", "gstin_cancelled"):
            value = min(value, 25.0)
            band = "critical"
            overridden_by = f"{fact.provider}:{fact.kind}"

    return Score("legitimacy", value, band, relevant, overridden_by, coverage)


def contribution_table(score: Score) -> list[dict[str, Any]]:
    """Per-signal contribution, for the 'show your working' panel in the UI."""
    rows = []
    for s in sorted(score.signals, key=lambda x: -x.contribution):
        rows.append({
            "signal": s.signal_id,
            "fired": s.fired,
            "value": s.value,
            "weight": s.weight,
            "points": round(s.contribution, 2),
            "share_pct": round(
                s.contribution / (sum(x.contribution for x in score.signals) or 1) * 100, 1
            ),
            "confidence": s.confidence,
            "data_available": s.data_available,
            "why": s.rationale,
            "evidence_count": len(s.evidence),
        })
    return rows


def band_rank(band: str, cfg_bands: dict[str, float], ascending: bool) -> int:
    """Ordinal rank of a band, used by the hysteresis logic."""
    names = sorted(cfg_bands, key=lambda k: cfg_bands[k], reverse=not ascending)
    ordered = names if ascending else list(reversed(names))
    try:
        return ordered.index(band)
    except ValueError:
        return -1
