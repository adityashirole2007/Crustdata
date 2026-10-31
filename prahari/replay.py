"""Time-travel replay — the backtest.

"Continuous monitoring" has no two-minute demo. Nothing changes in two minutes.
So we replay: feed the agent only the data that existed at T-180, then T-150,
T-120, T-90, T-60, T-30, T-0, and watch the distress score climb and the alert
fire at some day X before the public insolvency notice at day 0.

The guardrail that makes this honest is in the store, not here: every read is
`as_of <= cutoff`. The scorer is physically unable to see a snapshot dated after
the evaluation date. Registry facts are replayed the same way — we read the
observations recorded at or before the cutoff rather than hitting IBBI live, so
a backtest cannot leak the outcome it is supposed to be predicting.

Lead time is reported as: (date of the IBBI/registry event) - (date of the first
alert that actually paged). Suppressed alerts do not count as a catch; if the
alert fatigue machinery held it back, the analyst never saw it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, load_config
from .models import Alert, RegistryFact, Snapshot, Vendor, utcnow
from .pipeline import evaluate_vendor
from .store import Store

REPLAY_DIR = REPO_ROOT / "data" / "replay"

GROUND_TRUTH_KINDS = {
    "ibbi_cirp_admitted", "ibbi_liquidation", "mca_strike_off",
}


# --------------------------------------------------------------------------- #

@dataclass
class ReplayPoint:
    as_of: str
    distress: float
    legitimacy: float
    band: str
    priority: str
    paged: bool
    suppression_reason: str | None
    fired_signals: list[str] = field(default_factory=list)
    registry_hit: str | None = None


@dataclass
class ReplayResult:
    vendor_id: str
    vendor_name: str
    label: str                       # "cirp" | "control" | "confounded"
    event_date: str | None
    first_page_date: str | None
    lead_time_days: int | None
    points: list[ReplayPoint] = field(default_factory=list)
    expected_outcome: str = ""
    actual_outcome: str = ""
    correct: bool | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id, "vendor_name": self.vendor_name,
            "label": self.label, "event_date": self.event_date,
            "first_page_date": self.first_page_date, "lead_time_days": self.lead_time_days,
            "expected_outcome": self.expected_outcome, "actual_outcome": self.actual_outcome,
            "correct": self.correct, "notes": self.notes,
            "points": [p.__dict__ for p in self.points],
        }


# --------------------------------------------------------------------------- #
# fixture loading
# --------------------------------------------------------------------------- #

def load_fixture(path: str | Path, store: Store) -> tuple[Vendor, dict[str, Any]]:
    """Load one replay case into the store as dated snapshots + registry facts."""
    p = Path(path)
    if not p.is_absolute():
        p = REPLAY_DIR / p
    with p.open("r", encoding="utf-8") as fh:
        fx = json.load(fh)

    vmeta = fx["vendor"]
    vendor = Vendor(
        vendor_id=vmeta["vendor_id"], name=vmeta["name"], domain=vmeta.get("domain"),
        cin=vmeta.get("cin"), gstin=vmeta.get("gstin"),
        open_po_value_inr=float(vmeta.get("open_po_value", 0)),
        sole_source=bool(vmeta.get("sole_source", False)),
        lead_time_days=int(vmeta.get("lead_time_days", 0)),
        critical_component=bool(vmeta.get("critical_component", False)),
        category_manager=vmeta.get("category_manager"),
    )
    store.upsert_vendor(vendor)

    for snap in fx["snapshots"]:
        store.append_snapshot(Snapshot(
            vendor_id=vendor.vendor_id, as_of=snap["as_of"], payload=snap["payload"],
            source=snap.get("source", "replay_fixture"),
            fetched_at=snap.get("fetched_at", snap["as_of"] + "T00:00:00+00:00"),
        ))

    for fact in fx.get("registry_facts", []):
        store.append_registry_fact(vendor.vendor_id, {
            "provider": fact["provider"], "kind": fact["kind"], "status": fact["status"],
            "as_of": fact["as_of"], "source_url": fact["source_url"],
            "fetched_at": fact.get("fetched_at", fact["as_of"] + "T00:00:00+00:00"),
            "live": bool(fact.get("live", False)),
            "detail": fact.get("detail", {}),
        })
    return vendor, fx


def load_all_fixtures(store: Store, directory: str | Path | None = None
                      ) -> list[tuple[Vendor, dict[str, Any]]]:
    d = Path(directory) if directory else REPLAY_DIR
    out = []
    for p in sorted(d.glob("*.json")):
        if p.name.startswith("_"):
            continue
        out.append(load_fixture(p, store))
    return out


# --------------------------------------------------------------------------- #
# the replay
# --------------------------------------------------------------------------- #

def _days_between(a: str, b: str) -> int | None:
    from datetime import date
    try:
        da = date.fromisoformat(a[:10])
        db = date.fromisoformat(b[:10])
    except ValueError:
        return None
    return (db - da).days


def replay_vendor(vendor: Vendor, fx: dict[str, Any], store: Store,
                  cfg: dict | None = None) -> ReplayResult:
    """Step through every evaluation date, scoring with only what was knowable."""
    cfg = cfg or load_config()

    # Reset per-vendor alert state so a replay is reproducible.
    with store.conn() as c:
        c.execute("DELETE FROM band_state WHERE vendor_id=?", (vendor.vendor_id,))
        c.execute("DELETE FROM signal_fires WHERE vendor_id=?", (vendor.vendor_id,))
        c.execute("DELETE FROM alerts WHERE vendor_id=?", (vendor.vendor_id,))
        c.execute("DELETE FROM cases WHERE vendor_id=?", (vendor.vendor_id,))

    dates = sorted({s["as_of"] for s in fx["snapshots"]})
    event_date = None
    for f in fx.get("registry_facts", []):
        if f["kind"] in GROUND_TRUTH_KINDS:
            event_date = f["as_of"] if event_date is None else min(event_date, f["as_of"])

    result = ReplayResult(
        vendor_id=vendor.vendor_id, vendor_name=vendor.name,
        label=fx.get("label", "unknown"), event_date=event_date,
        first_page_date=None, lead_time_days=None,
        expected_outcome=fx.get("expected_outcome", ""),
        notes=fx.get("notes", ""),
    )

    for d in dates:
        alert = evaluate_vendor(
            vendor, store, cfg, as_of=d, client=None,
            live_registry=False,              # <- the no-lookahead guardrail
            run_investigation=True,
        )
        if alert is None:
            continue
        reg_hit = next(
            (f.kind for f in alert.registry_facts if f.kind in GROUND_TRUTH_KINDS), None
        )
        result.points.append(ReplayPoint(
            as_of=d,
            distress=round(alert.distress.value, 1) if alert.distress else 0.0,
            legitimacy=round(alert.legitimacy.value, 1) if alert.legitimacy else 100.0,
            band=alert.severity, priority=alert.priority,
            paged=not alert.suppressed, suppression_reason=alert.suppression_reason,
            fired_signals=[s.signal_id for s in (alert.distress.signals if alert.distress else [])
                           if s.fired],
            registry_hit=reg_hit,
        ))
        if not alert.suppressed and result.first_page_date is None:
            result.first_page_date = d

    # ---- scoring the backtest ----------------------------------------- #
    if result.label == "cirp":
        if result.first_page_date and event_date:
            lead = _days_between(result.first_page_date, event_date)
            result.lead_time_days = lead
            # A "catch" means we paged strictly before the public notice.
            result.correct = bool(lead and lead > 0)
            result.actual_outcome = (
                f"paged on {result.first_page_date}, {lead} days before the public "
                f"{event_date} registry event"
                if lead and lead > 0 else
                f"paged on {result.first_page_date}, at or after the {event_date} event — "
                f"too late to act"
            )
        else:
            result.correct = False
            result.actual_outcome = "never paged before the registry event — MISS"
    elif result.label in ("control", "confounded"):
        paged = [p for p in result.points if p.paged]
        result.correct = not paged
        result.actual_outcome = (
            "correctly never paged" if not paged
            else f"FALSE POSITIVE: paged on {paged[0].as_of}"
        )
    elif result.label == "shell":
        # A shell is judged on the LEGITIMACY axis, not the distress axis.
        # Paging is the correct behaviour here; scoring it as a false positive
        # would be scoring the wrong archetype.
        worst_legit = min((p.legitimacy for p in result.points), default=100.0)
        crit = float(cfg["legitimacy"]["bands"]["critical"])
        result.correct = worst_legit <= crit
        paged = [p for p in result.points if p.paged]
        result.actual_outcome = (
            f"legitimacy bottomed at {worst_legit:.0f} "
            f"({'below' if result.correct else 'ABOVE'} the critical floor of {crit:.0f})"
            + (f"; escalated on {paged[0].as_of}" if paged else "; never escalated")
        )
    return result


def run_backtest(store: Store, cfg: dict | None = None,
                 directory: str | Path | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    cases = load_all_fixtures(store, directory)
    results = [replay_vendor(v, fx, store, cfg) for v, fx in cases]

    cirp = [r for r in results if r.label == "cirp"]
    controls = [r for r in results if r.label in ("control", "confounded")]
    shells = [r for r in results if r.label == "shell"]
    leads = sorted(r.lead_time_days for r in cirp
                   if r.lead_time_days and r.lead_time_days > 0)
    median = leads[len(leads) // 2] if leads else None

    return {
        "generated_at": utcnow(),
        "n_cases": len(results),
        "n_distress_cases": len(cirp),
        "n_control_cases": len(controls),
        "n_shell_cases": len(shells),
        "shells_caught": sum(1 for r in shells if r.correct),
        "caught": sum(1 for r in cirp if r.correct),
        "missed": sum(1 for r in cirp if not r.correct),
        "false_positives": sum(1 for r in controls if not r.correct),
        "median_lead_time_days": median,
        "lead_times": leads,
        "results": [r.to_dict() for r in results],
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        "VALIDATION — time-travel replay",
        "=" * 72,
        f"Cases: {report['n_cases']}  "
        f"({report['n_distress_cases']} distress, {report['n_control_cases']} control, "
        f"{report.get('n_shell_cases', 0)} shell)",
        f"Caught before the registry event : {report['caught']}/{report['n_distress_cases']}",
        f"Missed                           : {report['missed']}",
        f"False positives on controls      : {report['false_positives']}/{report['n_control_cases']}",
        f"Shells caught on legitimacy      : "
        f"{report.get('shells_caught', 0)}/{report.get('n_shell_cases', 0)}",
        f"Median lead time                 : "
        f"{report['median_lead_time_days'] or 'n/a'} days",
        "",
        f"{'Vendor':<34}{'Label':<12}{'Paged':<13}{'Event':<13}{'Lead':>6}",
        "-" * 72,
    ]
    for r in report["results"]:
        lines.append(
            f"{r['vendor_name'][:33]:<34}{r['label']:<12}"
            f"{(r['first_page_date'] or '—'):<13}{(r['event_date'] or '—'):<13}"
            f"{(str(r['lead_time_days']) if r['lead_time_days'] else '—'):>6}"
        )
    lines += ["", "All scores computed using only data dated on or before each evaluation",
              "date. Registry facts are replayed from dated observations, never fetched live."]
    return "\n".join(lines)
