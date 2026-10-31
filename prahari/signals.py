"""Signal extractors.

One pure function per signal. Each takes (history, deltas, vendor, cfg) and
returns a SignalResult carrying its own Evidence. No function here performs I/O
and none of them calls a model.

A signal that cannot be evaluated because the data is missing returns
`data_available=False` rather than `fired=False`. That distinction matters: a
vendor we know nothing about is not a safe vendor, and the score reports its
coverage so a thin-data vendor never looks confidently clean.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from .diff import get_path, _as_float
from .models import Delta, DeltaKind, Evidence, SignalResult, Snapshot, Vendor

ENRICH_URL = "https://api.crustdata.com/company/enrich"

Extractor = Callable[..., SignalResult]
_REGISTRY: dict[str, tuple[str, Extractor]] = {}


def signal(signal_id: str, archetype: str) -> Callable[[Extractor], Extractor]:
    def deco(fn: Extractor) -> Extractor:
        _REGISTRY[signal_id] = (archetype, fn)
        fn.signal_id = signal_id        # type: ignore[attr-defined]
        fn.archetype = archetype        # type: ignore[attr-defined]
        return fn
    return deco


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

def _ev(snap: Snapshot | None, field_path: str, *, from_value: Any = None,
        to_value: Any = None, note: str | None = None) -> Evidence:
    return Evidence(
        source="replay_fixture" if snap and snap.source.startswith("replay") else "crustdata_enrich",
        source_url=ENRICH_URL,
        fetched_at=snap.fetched_at if snap else "",
        field_path=field_path,
        as_of=snap.as_of if snap else None,
        from_value=from_value,
        to_value=to_value,
        note=note,
    )


def _miss(signal_id: str, archetype: str, weight: float, why: str) -> SignalResult:
    return SignalResult(
        signal_id=signal_id, archetype=archetype, fired=False, value=None,
        direction="neutral", weight=weight, contribution=0.0, confidence="low",
        rationale=f"data_unavailable: {why}", evidence=[], data_available=False,
    )


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    s = str(v)[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _text_of(item: Any) -> str:
    if isinstance(item, str):
        return item.lower()
    if isinstance(item, dict):
        return " ".join(
            str(item.get(k, "")) for k in
            ("title", "headline", "text", "body", "review", "summary", "cons", "pros", "snippet")
        ).lower()
    return str(item).lower()


def _matches_any(text: str, keywords: Iterable[str]) -> list[str]:
    return [k for k in keywords if k in text]


# ===========================================================================
# ARCHETYPE A — LEGITIMACY (shell-ification)
# ===========================================================================

@signal("headcount_vs_turnover", "legitimacy")
def headcount_vs_turnover(history: list[Snapshot], deltas: list[Delta],
                          vendor: Vendor, cfg: dict) -> SignalResult:
    """Rs 40cr of invoices through a 3-person company is the classic shell shape."""
    c = cfg["legitimacy"]["signals"]["headcount_vs_turnover"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    heads = _as_float(get_path(curr.payload if curr else None, "headcount.total"))
    revenue = _as_float(get_path(curr.payload if curr else None, "revenue.annual_revenue_inr"))
    # Fall back to the procurement ledger: our own open PO value is a revenue floor.
    claimed = revenue if revenue else (vendor.open_po_value_inr or 0.0)
    if heads is None or heads < c["min_headcount_for_check"] or not claimed:
        return _miss("headcount_vs_turnover", "legitimacy", w,
                     "headcount or revenue/PO basis missing")

    per_head = claimed / max(heads, 1.0)
    threshold = float(c["inr_revenue_per_head_implausible"])
    fired = per_head > threshold
    # scale: 1x threshold -> partial, 3x -> full
    ratio = min(per_head / threshold, 3.0) if fired else 0.0
    contribution = w * (ratio / 3.0) if fired else 0.0
    ev = [_ev(curr, "headcount.total", to_value=heads)]
    if revenue:
        ev.append(_ev(curr, "revenue.annual_revenue_inr", to_value=revenue))
    else:
        ev.append(Evidence(
            source="procurement_ledger", source_url="file://data/vendors.csv",
            fetched_at=curr.fetched_at if curr else "", field_path="open_po_value_inr",
            to_value=vendor.open_po_value_inr,
            note="revenue absent from graph; open PO value used as a turnover floor",
        ))
    return SignalResult(
        "headcount_vs_turnover", "legitimacy", fired, round(per_head, 0),
        "worsening" if fired else "neutral", w, contribution,
        "high" if revenue else "medium",
        f"Rs {per_head:,.0f} of turnover per employee against {int(heads)} staff "
        f"(implausible above Rs {threshold:,.0f}).",
        ev,
    )


@signal("never_hires", "legitimacy")
def never_hires(history: list[Snapshot], deltas: list[Delta],
                vendor: Vendor, cfg: dict) -> SignalResult:
    """Real operating firms post roles. A firm that has never posted one is odd."""
    c = cfg["legitimacy"]["signals"]["never_hires"]
    w = float(c["weight"])
    need = int(c["consecutive_zero_snapshots"])
    vals = [(s, _as_float(get_path(s.payload, "hiring.openings_count"))) for s in history]
    observed = [(s, v) for s, v in vals if v is not None]
    if len(observed) < need:
        return _miss("never_hires", "legitimacy", w,
                     f"only {len(observed)} snapshots with hiring data, need {need}")
    tail = observed[-need:]
    fired = all(v == 0 for _, v in tail)
    return SignalResult(
        "never_hires", "legitimacy", fired, 0 if fired else tail[-1][1],
        "worsening" if fired else "neutral", w, w if fired else 0.0, "medium",
        f"Zero job openings across the last {need} monthly observations."
        if fired else f"{int(tail[-1][1])} openings in the latest observation.",
        [_ev(s, "hiring.openings_count", to_value=v) for s, v in tail],
    )


@signal("no_web_presence", "legitimacy")
def no_web_presence(history: list[Snapshot], deltas: list[Delta],
                    vendor: Vendor, cfg: dict) -> SignalResult:
    c = cfg["legitimacy"]["signals"]["no_web_presence"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    visits = _as_float(get_path(curr.payload if curr else None, "web_traffic.monthly_visits"))
    if visits is None:
        return _miss("no_web_presence", "legitimacy", w, "web_traffic absent")
    floor = float(c["monthly_visits_floor"])
    fired = visits < floor
    return SignalResult(
        "no_web_presence", "legitimacy", fired, visits,
        "worsening" if fired else "neutral", w, w if fired else 0.0, "medium",
        f"{int(visits)} monthly visits against a {int(floor)} floor for a trading business."
        if fired else f"{int(visits)} monthly visits — a normal trading footprint.",
        [_ev(curr, "web_traffic.monthly_visits", to_value=visits)],
    )


@signal("address_collision", "legitimacy")
def address_collision(history: list[Snapshot], deltas: list[Delta],
                      vendor: Vendor, cfg: dict) -> SignalResult:
    """N entities at one registered address is the signature of a ring.

    The collision count is injected by the investigation layer (which does the
    /company/search reverse lookup); this extractor only reads what was written
    back into the snapshot under `_prahari.address_collisions`.
    """
    c = cfg["legitimacy"]["signals"]["address_collision"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    coll = get_path(curr.payload if curr else None, "_prahari.address_collisions")
    if not isinstance(coll, dict) or not coll:
        return _miss("address_collision", "legitimacy", w,
                     "no reverse address lookup has been run for this vendor")
    worst_addr, worst_n = max(coll.items(), key=lambda kv: kv[1])
    thresh = int(c["entities_at_address_threshold"])
    fired = worst_n >= thresh
    return SignalResult(
        "address_collision", "legitimacy", fired, worst_n,
        "worsening" if fired else "neutral", w, w if fired else 0.0, "medium",
        f"{worst_n} distinct registered entities resolve to '{worst_addr}'."
        if fired else f"Highest address concentration is {worst_n} entities.",
        [Evidence(source="crustdata_search",
                  source_url="https://api.crustdata.com/company/search",
                  fetched_at=curr.fetched_at if curr else "",
                  field_path="locations.all_office_addresses",
                  to_value=f"{worst_n} entities at {worst_addr}")],
    )


@signal("director_overlap", "legitimacy")
def director_overlap(history: list[Snapshot], deltas: list[Delta],
                     vendor: Vendor, cfg: dict) -> SignalResult:
    c = cfg["legitimacy"]["signals"]["director_overlap"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    overlap = get_path(curr.payload if curr else None, "_prahari.director_overlap")
    if not isinstance(overlap, dict) or not overlap:
        return _miss("director_overlap", "legitimacy", w,
                     "no cross-entity director lookup has been run")
    worst_person, worst_n = max(overlap.items(), key=lambda kv: kv[1])
    thresh = int(c["shared_people_threshold"])
    fired = worst_n >= thresh
    return SignalResult(
        "director_overlap", "legitimacy", fired, worst_n,
        "worsening" if fired else "neutral", w, w if fired else 0.0, "medium",
        f"{worst_person} holds a directorship or C-level role at {worst_n} related entities."
        if fired else "No director appears across multiple related entities.",
        [Evidence(source="crustdata_person_enrich",
                  source_url="https://api.crustdata.com/person/enrich",
                  fetched_at=curr.fetched_at if curr else "",
                  field_path="people.decision_makers",
                  to_value=f"{worst_person}: {worst_n} entities")],
    )


@signal("incorporation_vs_footprint_age", "legitimacy")
def incorporation_vs_footprint_age(history: list[Snapshot], deltas: list[Delta],
                                   vendor: Vendor, cfg: dict) -> SignalResult:
    """'Established 1998' with a digital footprint that starts in 2025."""
    c = cfg["legitimacy"]["signals"]["incorporation_vs_footprint_age"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    founded = _as_float(get_path(curr.payload if curr else None, "basic_info.year_founded"))
    first_seen = get_path(curr.payload if curr else None, "_prahari.first_footprint_year")
    first_seen = _as_float(first_seen)
    if founded is None or first_seen is None:
        return _miss("incorporation_vs_footprint_age", "legitimacy", w,
                     "year_founded or first observable footprint year missing")
    gap = first_seen - founded
    thresh = float(c["implausible_gap_years"])
    fired = gap >= thresh
    return SignalResult(
        "incorporation_vs_footprint_age", "legitimacy", fired, gap,
        "worsening" if fired else "neutral", w, w if fired else 0.0, "medium",
        f"Claims incorporation in {int(founded)} but the earliest observable digital "
        f"footprint is {int(first_seen)} — a {int(gap)} year gap."
        if fired else f"Incorporation {int(founded)}, footprint from {int(first_seen)}.",
        [_ev(curr, "basic_info.year_founded", to_value=founded),
         _ev(curr, "_prahari.first_footprint_year", to_value=first_seen)],
    )


@signal("zero_employee_footprint", "legitimacy")
def zero_employee_footprint(history: list[Snapshot], deltas: list[Delta],
                            vendor: Vendor, cfg: dict) -> SignalResult:
    c = cfg["legitimacy"]["signals"]["zero_employee_footprint"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    followers = _as_float(get_path(curr.payload if curr else None, "followers.count"))
    reviews = get_path(curr.payload if curr else None, "employee_reviews")
    n_reviews = len(reviews) if isinstance(reviews, list) else None
    if followers is None and n_reviews is None:
        return _miss("zero_employee_footprint", "legitimacy", w,
                     "followers and employee_reviews both absent")
    floor = float(c["followers_floor"])
    fired = (followers is not None and followers < floor) and not n_reviews
    return SignalResult(
        "zero_employee_footprint", "legitimacy", fired, followers,
        "worsening" if fired else "neutral", w, w if fired else 0.0, "low",
        f"{int(followers or 0)} followers and no employee reviews — effectively nobody "
        f"publicly works there." if fired else "Some employee footprint is visible.",
        [_ev(curr, "followers.count", to_value=followers)],
    )


# ===========================================================================
# ARCHETYPE B — DISTRESS (operational)
# ===========================================================================

@signal("leadership_exodus", "distress")
def leadership_exodus(history: list[Snapshot], deltas: list[Delta],
                      vendor: Vendor, cfg: dict) -> SignalResult:
    """CFO out is the single best 90-180 day leading indicator of payment failure."""
    c = cfg["distress"]["signals"]["leadership_exodus"]
    w = float(c["weight"])
    titles = [t.lower() for t in c["critical_titles"]]
    removed = [
        d for d in deltas
        if d.kind == DeltaKind.ARRAY_REMOVED
        and d.field_path.startswith("people.")
        and any(t in str((d.from_value or {}).get("title", "")).lower() for t in titles)
    ]
    if not deltas and len(history) < 2:
        return _miss("leadership_exodus", "distress", w,
                     "need at least two snapshots to observe a departure")
    n = len(removed)
    full_at = int(c["full_weight_departures"])
    fired = n >= 1
    contribution = w * min(n / full_at, 1.0) if fired else 0.0
    names = [str((d.from_value or {}).get("name", "unknown")) for d in removed]
    roles = [str((d.from_value or {}).get("title", "")) for d in removed]
    ev = [
        Evidence(source="crustdata_enrich", source_url=ENRICH_URL,
                 fetched_at=history[-1].fetched_at if history else "",
                 field_path=d.field_path, as_of=d.to_as_of,
                 from_value=d.from_value, to_value=None,
                 note="present in prior snapshot, absent in current")
        for d in removed
    ]
    return SignalResult(
        "leadership_exodus", "distress", fired, n,
        "worsening" if fired else "neutral", w, contribution,
        "high" if n >= full_at else "medium",
        (f"{n} critical finance/executive departure(s) observed: "
         + "; ".join(f"{nm} ({rl})" for nm, rl in zip(names, roles)))
        if fired else "No critical executive departures between snapshots.",
        ev,
    )


@signal("headcount_decline", "distress")
def headcount_decline(history: list[Snapshot], deltas: list[Delta],
                      vendor: Vendor, cfg: dict) -> SignalResult:
    """Decline matters; ACCELERATING decline matters much more."""
    c = cfg["distress"]["signals"]["headcount_decline"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    if curr is None:
        return _miss("headcount_decline", "distress", w, "no snapshot")

    from .crustdata import CrustdataClient as _C
    growth = _C.normalise_growth_map(get_path(curr.payload, "headcount.growth_percent"))
    total_now = _as_float(get_path(curr.payload, "headcount.total"))

    # Prefer the graph's own growth map; fall back to our own computed delta.
    observed_mom = growth.get("mom")
    if observed_mom is None and len(history) >= 2:
        prev_total = _as_float(get_path(history[-2].payload, "headcount.total"))
        if prev_total and total_now is not None and prev_total > 0:
            observed_mom = (total_now - prev_total) / prev_total * 100.0

    if observed_mom is None and not growth:
        return _miss("headcount_decline", "distress", w, "no headcount growth data")

    triggers = {
        "mom": (observed_mom, float(c["mom_trigger_pct"])),
        "qoq": (growth.get("qoq"), float(c["qoq_trigger_pct"])),
        "yoy": (growth.get("yoy"), float(c["yoy_trigger_pct"])),
    }
    hits = {k: v for k, (v, t) in triggers.items() if v is not None and v <= t}
    fired = bool(hits)

    # acceleration: this month steeper than the quarterly run-rate
    accelerating = False
    if observed_mom is not None and growth.get("qoq") is not None:
        qoq_monthly = growth["qoq"] / 3.0
        accelerating = observed_mom < qoq_monthly < 0
    severity = min(abs(min(hits.values())) / 25.0, 1.0) if hits else 0.0
    contribution = w * severity * (float(c["acceleration_bonus"]) if accelerating else 1.0)
    contribution = min(contribution, w * 1.35)

    ev = [_ev(curr, "headcount.total", to_value=total_now)]
    for k in hits:
        ev.append(_ev(curr, f"headcount.growth_percent.{k}", to_value=hits[k]))
    if len(history) >= 2:
        ev.append(_ev(history[-2], "headcount.total",
                      to_value=_as_float(get_path(history[-2].payload, "headcount.total"))))

    rationale = (
        "Headcount falling: " + ", ".join(f"{k} {v:+.1f}%" for k, v in hits.items())
        + (" — and the decline is accelerating relative to the quarterly run-rate."
           if accelerating else ".")
    ) if fired else "Headcount stable or growing."
    return SignalResult(
        "headcount_decline", "distress", fired, hits or observed_mom,
        "worsening" if fired else "neutral", w, contribution,
        "high" if len(hits) >= 2 else "medium", rationale, ev,
    )


@signal("hiring_freeze", "distress")
def hiring_freeze(history: list[Snapshot], deltas: list[Delta],
                  vendor: Vendor, cfg: dict) -> SignalResult:
    """Openings collapsing to zero AFTER sustained hiring. Zero-always is a
    legitimacy signal, not a distress signal — keep them apart."""
    c = cfg["distress"]["signals"]["hiring_freeze"]
    w = float(c["weight"])
    series = [(s, _as_float(get_path(s.payload, "hiring.openings_count"))) for s in history]
    series = [(s, v) for s, v in series if v is not None]
    if len(series) < 2:
        return _miss("hiring_freeze", "distress", w, "need two hiring observations")
    floor = float(c["openings_floor"])
    prior_min = float(c["prior_activity_min"])
    now = series[-1][1]
    had_activity = any(v >= prior_min for _, v in series[:-1])
    fired = had_activity and now <= floor
    peak = max((v for _, v in series[:-1]), default=0.0)
    return SignalResult(
        "hiring_freeze", "distress", fired, now,
        "worsening" if fired else "neutral", w, w if fired else 0.0, "medium",
        f"Open roles went from a peak of {int(peak)} to {int(now)} — a hiring stop, "
        f"not a vendor that never hired." if fired
        else f"{int(now)} open roles; no freeze pattern.",
        [_ev(s, "hiring.openings_count", to_value=v) for s, v in series[-3:]],
    )


@signal("salary_delay_reviews", "distress")
def salary_delay_reviews(history: list[Snapshot], deltas: list[Delta],
                         vendor: Vendor, cfg: dict) -> SignalResult:
    """The thesis: salary delay -> supplier payment default -> insolvency.

    This is the earliest honest cash signal available in a public footprint,
    and it is the one the registry will only confirm months too late.
    """
    c = cfg["distress"]["signals"]["salary_delay_reviews"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    reviews = get_path(curr.payload if curr else None, "employee_reviews")
    if not isinstance(reviews, list):
        return _miss("salary_delay_reviews", "distress", w, "employee_reviews absent")
    kws = [k.lower() for k in c["keywords"]]
    matched: list[tuple[Any, list[str]]] = []
    for r in reviews:
        hits = _matches_any(_text_of(r), kws)
        if hits:
            matched.append((r, hits))
    need = int(c["min_matching_reviews"])
    fired = len(matched) >= need
    severity = min(len(matched) / (need * 2.0), 1.0)
    ev = [
        Evidence(source="crustdata_enrich", source_url=ENRICH_URL,
                 fetched_at=curr.fetched_at if curr else "",
                 field_path="employee_reviews",
                 as_of=curr.as_of if curr else None,
                 to_value=(r.get("title") or _text_of(r)[:140]) if isinstance(r, dict)
                 else str(r)[:140],
                 note=f"matched: {', '.join(h)}")
        for r, h in matched[:5]
    ]
    return SignalResult(
        "salary_delay_reviews", "distress", fired, len(matched),
        "worsening" if fired else "neutral", w, w * severity if fired else 0.0,
        "high" if len(matched) >= need * 2 else "medium",
        f"{len(matched)} employee reviews reference delayed salary or unpaid dues — "
        f"payroll stress typically precedes supplier payment default by 60-90 days."
        if fired else "No payroll-stress language in employee reviews.",
        ev,
    )


@signal("web_traffic_decline", "distress")
def web_traffic_decline(history: list[Snapshot], deltas: list[Delta],
                        vendor: Vendor, cfg: dict) -> SignalResult:
    c = cfg["distress"]["signals"]["web_traffic_decline"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    if curr is None:
        return _miss("web_traffic_decline", "distress", w, "no snapshot")
    from .crustdata import CrustdataClient as _C
    growth = _C.normalise_growth_map(get_path(curr.payload, "web_traffic.growth_percent"))
    qoq = growth.get("qoq")
    if qoq is None and len(history) >= 2:
        a = _as_float(get_path(history[-2].payload, "web_traffic.monthly_visits"))
        b = _as_float(get_path(curr.payload, "web_traffic.monthly_visits"))
        if a and b is not None and a > 0:
            qoq = (b - a) / a * 100.0
    if qoq is None:
        return _miss("web_traffic_decline", "distress", w, "no web traffic trend")
    trig = float(c["qoq_trigger_pct"])
    fired = qoq <= trig
    severity = min(abs(qoq) / 60.0, 1.0)
    return SignalResult(
        "web_traffic_decline", "distress", fired, round(qoq, 1),
        "worsening" if fired else "neutral", w, w * severity if fired else 0.0, "low",
        f"Web traffic down {abs(qoq):.0f}% quarter on quarter."
        if fired else f"Web traffic {qoq:+.0f}% QoQ.",
        [_ev(curr, "web_traffic.monthly_visits",
             to_value=_as_float(get_path(curr.payload, "web_traffic.monthly_visits")))],
    )


@signal("adverse_news", "distress")
def adverse_news(history: list[Snapshot], deltas: list[Delta],
                 vendor: Vendor, cfg: dict) -> SignalResult:
    """Keyed on article_url so a syndicated repost does not re-fire."""
    c = cfg["distress"]["signals"]["adverse_news"]
    w = float(c["weight"])
    kws = [k.lower() for k in c["keywords"]]
    added = [d for d in deltas if d.field_path == "news" and d.kind == DeltaKind.ARRAY_ADDED]
    curr = history[-1] if history else None
    candidates = [d.to_value for d in added]
    if not candidates and curr is not None and len(history) == 1:
        raw = get_path(curr.payload, "news")
        candidates = raw if isinstance(raw, list) else []
    if not candidates:
        return SignalResult(
            "adverse_news", "distress", False, 0, "neutral", w, 0.0, "medium",
            "No new news items since the previous snapshot.", [],
        )
    matched = [(a, _matches_any(_text_of(a), kws)) for a in candidates]
    matched = [(a, h) for a, h in matched if h]
    fired = bool(matched)
    severity = min(len(matched) / 2.0, 1.0)
    ev = [
        Evidence(source="crustdata_enrich",
                 source_url=(a.get("article_url") if isinstance(a, dict) else ENRICH_URL) or ENRICH_URL,
                 fetched_at=curr.fetched_at if curr else "",
                 field_path="news[].article_url",
                 as_of=(a.get("published_at") if isinstance(a, dict) else None),
                 to_value=(a.get("title") if isinstance(a, dict) else str(a))[:180],
                 note=f"matched: {', '.join(h)}")
        for a, h in matched[:5]
    ]
    return SignalResult(
        "adverse_news", "distress", fired, len(matched),
        "worsening" if fired else "neutral", w, w * severity if fired else 0.0, "high",
        f"{len(matched)} new adverse news item(s) matched the distress lexicon."
        if fired else "New news items carry no distress language.",
        ev,
    )


@signal("funding_drought", "distress")
def funding_drought(history: list[Snapshot], deltas: list[Delta],
                    vendor: Vendor, cfg: dict) -> SignalResult:
    c = cfg["distress"]["signals"]["funding_drought"]
    w = float(c["weight"])
    curr = history[-1] if history else None
    last = _parse_date(get_path(curr.payload if curr else None, "funding.last_fundraise_date"))
    if last is None or curr is None:
        return _miss("funding_drought", "distress", w, "no fundraise history")
    asof = _parse_date(curr.as_of) or date.today()
    months = (asof - last).days / 30.44
    thresh = float(c["months_since_last_raise"])
    from .crustdata import CrustdataClient as _C
    growth = _C.normalise_growth_map(get_path(curr.payload, "headcount.growth_percent"))
    burn = (growth.get("yoy") or 0.0) < float(c["requires_headcount_growth_below_pct"])
    fired = months >= thresh and burn
    return SignalResult(
        "funding_drought", "distress", fired, round(months, 1),
        "worsening" if fired else "neutral", w, w if fired else 0.0, "low",
        f"{months:.0f} months since the last raise while headcount is contracting."
        if fired else f"{months:.0f} months since the last raise.",
        [_ev(curr, "funding.last_fundraise_date", to_value=str(last))],
    )


@signal("restructuring_markers", "distress")
def restructuring_markers(history: list[Snapshot], deltas: list[Delta],
                          vendor: Vendor, cfg: dict) -> SignalResult:
    """Rebrand / HQ relocation / industry reclassification.

    Deliberately low weight and never sufficient on its own: a rebrand reads
    exactly like a shell if you let it, and that is a named confounder.
    """
    c = cfg["distress"]["signals"]["restructuring_markers"]
    w = float(c["weight"])
    watched = set(c["fields"])
    hits = [d for d in deltas if d.field_path in watched or
            any(d.field_path.startswith(f) for f in watched)]
    fired = bool(hits)
    return SignalResult(
        "restructuring_markers", "distress", fired, len(hits),
        "worsening" if fired else "neutral", w, w if fired else 0.0, "low",
        "; ".join(f"{d.field_path}: {d.from_value!r} -> {d.to_value!r}" for d in hits[:3])
        if fired else "No name, country or industry reclassification.",
        [Evidence(source="crustdata_enrich", source_url=ENRICH_URL,
                  fetched_at=history[-1].fetched_at if history else "",
                  field_path=d.field_path, as_of=d.to_as_of,
                  from_value=d.from_value, to_value=d.to_value)
         for d in hits[:3]],
    )


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #

def run_all(history: list[Snapshot], deltas: list[Delta], vendor: Vendor,
            cfg: dict, archetype: str | None = None) -> list[SignalResult]:
    """Evaluate every registered extractor. Order is stable and deterministic."""
    out: list[SignalResult] = []
    for sid, (arch, fn) in sorted(_REGISTRY.items()):
        if archetype and arch != archetype:
            continue
        try:
            out.append(fn(history, deltas, vendor, cfg))
        except Exception as exc:                      # a broken extractor must not
            weight = float(                            # take the whole run down
                cfg.get(arch, {}).get("signals", {}).get(sid, {}).get("weight", 0)
            )
            out.append(_miss(sid, arch, weight, f"extractor error: {exc!r}"))
    return out


def registered() -> dict[str, str]:
    return {sid: arch for sid, (arch, _) in _REGISTRY.items()}
