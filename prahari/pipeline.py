"""The end-to-end run.

    ledger -> identity resolution -> snapshot -> deterministic diff -> signals
           -> explainable scoring -> registry overlay -> bounded investigation
           -> citation-guarded alert -> priority by Distress x Exposure -> case file

A person is needed for exactly two things: authorising a payment hold, and any
case the agent marks confidence: low.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import signals as signal_mod
from .alerts import build_alert
from .config import REPO_ROOT, load_config
from .crustdata import CrustdataClient
from .diff import diff_snapshots
from .investigate import investigate
from .models import Alert, RegistryFact, Snapshot, Vendor, utcnow
from .registry import all_providers
from .scoring import contribution_table, score_distress, score_legitimacy
from .store import Store

log = logging.getLogger("prahari.pipeline")


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #

def load_ledger(path: str | Path) -> list[Vendor]:
    """Read vendors.csv. Tolerant of missing optional columns; strict on name."""
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    out: list[Vendor] = []
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh), 1):
            row = { (k or "").strip(): (v or "").strip() for k, v in row.items() }
            name = row.get("name")
            if not name:
                log.warning("ledger row %d has no name — skipped", i)
                continue
            out.append(Vendor(
                vendor_id=row.get("vendor_id") or f"V{i:03d}",
                name=name,
                domain=row.get("domain") or None,
                cin=row.get("cin") or None,
                gstin=row.get("gstin") or None,
                open_po_value_inr=_num(row.get("open_po_value")),
                sole_source=_bool(row.get("sole_source")),
                lead_time_days=int(_num(row.get("lead_time_days"))),
                critical_component=_bool(row.get("critical_component")),
                category_manager=row.get("category_manager") or None,
            ))
    return out


def _num(v: Any) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _bool(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y")


# --------------------------------------------------------------------------- #
# identity + baseline
# --------------------------------------------------------------------------- #

@dataclass
class IngestReport:
    resolved: int = 0
    failed: list[str] = None            # type: ignore[assignment]
    snapshots_written: int = 0

    def __post_init__(self) -> None:
        if self.failed is None:
            self.failed = []


def resolve_and_baseline(vendors: Iterable[Vendor], store: Store,
                         client: CrustdataClient, as_of: str | None = None,
                         cfg: dict | None = None) -> IngestReport:
    """/company/identify -> /company/enrich(exact_match) -> append snapshot."""
    cfg = cfg or load_config()
    as_of = as_of or utcnow()[:10]
    rep = IngestReport()

    for v in vendors:
        store.upsert_vendor(v)
        if not client.available:
            rep.failed.append(f"{v.name}: data_unavailable — CRUSTDATA_API_KEY not set")
            continue

        if not v.crustdata_company_id:
            ident = client.identify(name=v.name, domain=v.domain)
            if ident.ok and ident.data:
                d = ident.data[0] if isinstance(ident.data, list) and ident.data else ident.data
                if isinstance(d, dict):
                    v.crustdata_company_id = str(
                        d.get("company_id") or d.get("id") or ""
                    ) or None
                    v.domain = v.domain or d.get("domain")
                    store.upsert_vendor(v)
            else:
                rep.failed.append(f"{v.name}: identify failed — {ident.reason}")

        res = client.enrich(domains=[v.domain] if v.domain else None,
                            company_ids=[v.crustdata_company_id] if v.crustdata_company_id else None)
        if not res.ok:
            rep.failed.append(f"{v.name}: enrich failed — {res.unavailable_note}")
            continue
        payload = res.data[0] if isinstance(res.data, list) and res.data else res.data
        if not isinstance(payload, dict):
            rep.failed.append(f"{v.name}: enrich returned no usable payload")
            continue

        store.append_snapshot(Snapshot(
            vendor_id=v.vendor_id, as_of=as_of, payload=payload,
            source="crustdata_enrich", fetched_at=res.fetched_at,
        ))
        rep.resolved += 1
        rep.snapshots_written += 1
    return rep


# --------------------------------------------------------------------------- #
# registry overlay
# --------------------------------------------------------------------------- #

def collect_registry_facts(vendor: Vendor, store: Store, as_of: str | None = None,
                           live: bool = True) -> tuple[list[RegistryFact], list[str]]:
    """Returns (facts, unavailable_reasons). Never invents a status."""
    facts: list[RegistryFact] = []
    problems: list[str] = []

    # Replay mode: read what was already observed on or before the cutoff rather
    # than hitting a live registry, so a backtest cannot leak the future.
    if not live:
        for row in store.registry_facts(vendor.vendor_id, as_of_max=as_of):
            facts.append(RegistryFact(
                provider=row["provider"], kind=row["kind"], status=row["status"],
                as_of=row["as_of"], source_url=row["source_url"],
                fetched_at=row["fetched_at"], live=bool(row["live"]),
                detail=row.get("detail", {}),
            ))
        return facts, problems

    for provider in all_providers():
        res = provider.lookup(cin=vendor.cin, gstin=vendor.gstin, name=vendor.name,
                              as_of=as_of)
        if res.ok:
            facts.extend(res.facts)
            for f in res.facts:
                store.append_registry_fact(vendor.vendor_id, f.to_dict())
            if res.reason:
                problems.append(f"{provider.name}: {res.reason}")
        else:
            problems.append(f"{provider.name}: {res.reason}")
    return facts, problems


# --------------------------------------------------------------------------- #
# evaluate one vendor
# --------------------------------------------------------------------------- #

def evaluate_vendor(vendor: Vendor, store: Store, cfg: dict,
                    as_of: str | None = None, client: CrustdataClient | None = None,
                    live_registry: bool = True,
                    run_investigation: bool = True) -> Alert | None:
    """Score a single vendor as of a date. Returns None if we have no data at all."""
    cutoff = as_of
    history = store.snapshots(vendor.vendor_id, as_of_max=cutoff)
    if not history:
        log.warning("no snapshots for %s at or before %s", vendor.name, cutoff)
        return None

    # Dedupe by as_of so a re-fetch does not create a phantom zero-delta step.
    by_date: dict[str, Snapshot] = {s.as_of: s for s in history}
    history = [by_date[k] for k in sorted(by_date)]
    prev = history[-2] if len(history) >= 2 else None
    curr = history[-1]
    eval_date = cutoff or curr.as_of

    deltas = diff_snapshots(prev, curr)

    all_signals = signal_mod.run_all(history, deltas, vendor, cfg)
    facts, reg_problems = collect_registry_facts(vendor, store, eval_date, live=live_registry)

    distress = score_distress(all_signals, cfg, facts)
    legitimacy = score_legitimacy(all_signals, cfg, facts)

    fired_ids = {s.signal_id for s in all_signals if s.fired}
    rows = contribution_table(distress)

    if run_investigation and (fired_ids or distress.value > 0):
        outcome = investigate(history, vendor, fired_ids, rows, distress.band, client)
    else:
        from .investigate import InvestigationOutcome
        outcome = InvestigationOutcome(
            disconfirmed=False, confidence="high",
            verdict="no signals fired — nothing to investigate",
            confounders_checked={}, narration="", engine="skipped",
        )
    for p in reg_problems:
        outcome.data_unavailable.append(p)

    return build_alert(vendor, distress, legitimacy, facts, outcome, eval_date, cfg, store)


def run_all_vendors(store: Store, cfg: dict, as_of: str | None = None,
                    client: CrustdataClient | None = None,
                    live_registry: bool = True) -> list[Alert]:
    out: list[Alert] = []
    for v in store.list_vendors():
        a = evaluate_vendor(v, store, cfg, as_of, client, live_registry)
        if a:
            out.append(a)
    out.sort(key=lambda a: -a.priority_score)
    return out
