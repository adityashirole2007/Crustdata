"""Deterministic diff engine.

NO LLM TOUCHES THIS FILE. That is the point.

An LLM must never be the thing that decides whether headcount went 120 -> 68.
Given two snapshot payloads this emits typed Delta events, and given the same
two payloads it emits byte-identical output every time. The investigation agent
downstream gets to interpret these deltas; it does not get to produce them.

Array handling: arrays of objects are keyed (news by article_url, people by a
name+title identity) so a syndicated repost or a re-ordering does not read as a
change. Scalars are compared directly; numerics also carry pct_change.
"""
from __future__ import annotations

from typing import Any, Iterable

from .models import Delta, DeltaKind, Snapshot

# Which paths we diff, and how. Anything not listed is ignored, which keeps the
# delta stream small and auditable rather than a firehose of noise.
NUMERIC_PATHS = [
    "headcount.total",
    "hiring.openings_count",
    "web_traffic.monthly_visits",
    "seo.organic_traffic",
    "followers.count",
    "revenue.annual_revenue_inr",
    "revenue.annual_revenue_usd",
]

SCALAR_PATHS = [
    "basic_info.name",
    "basic_info.year_founded",
    "basic_info.employee_count_range",
    "basic_info.website",
    "locations.country",
    "locations.hq_address",
    "funding.last_fundraise_date",
]

# (path, identity-key) — identity may be a field name or a tuple of field names
KEYED_ARRAY_PATHS: list[tuple[str, Any]] = [
    ("news", "article_url"),
    ("people.cxos", ("name", "title")),
    ("people.founders", ("name",)),
    ("people.decision_makers", ("name", "title")),
    ("employee_reviews", ("review_id",)),
    ("locations.all_office_addresses", None),      # list of plain strings
    ("taxonomy.industries", None),
]

GROWTH_PATHS = [
    "headcount.growth_percent",
    "hiring.openings_growth_percent",
    "web_traffic.growth_percent",
]


# --------------------------------------------------------------------------- #
# path helpers
# --------------------------------------------------------------------------- #

def get_path(payload: dict[str, Any] | None, path: str) -> Any:
    """Safe dotted lookup. Returns None for any missing link in the chain."""
    if not payload:
        return None
    cur: Any = payload
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _identity(item: Any, key: Any) -> str:
    """Stable identity for an array member, so re-orderings are not changes."""
    if key is None:
        return str(item).strip().lower()
    if isinstance(item, dict):
        if isinstance(key, str):
            return str(item.get(key, "")).strip().lower()
        return "|".join(str(item.get(k, "")).strip().lower() for k in key)
    return str(item).strip().lower()


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #

def diff_payloads(
    prev: dict[str, Any] | None,
    curr: dict[str, Any] | None,
    prev_as_of: str | None = None,
    curr_as_of: str | None = None,
) -> list[Delta]:
    """Compare two enrich payloads. Pure function: no I/O, no model, no clock."""
    deltas: list[Delta] = []
    if curr is None:
        return deltas
    if prev is None:
        # A baseline observation is not a change. Without this, every field in the
        # first-ever snapshot reads as None -> value and the vendor appears to have
        # restructured on the day we started watching it. (The Crustdata watcher
        # takes the same position: its baseline run fires on nothing.)
        return deltas

    # --- numerics -------------------------------------------------------- #
    for path in NUMERIC_PATHS:
        a, b = _as_float(get_path(prev, path)), _as_float(get_path(curr, path))
        if a is None or b is None or a == b:
            continue
        pct = ((b - a) / abs(a) * 100.0) if a else None
        deltas.append(Delta(path, DeltaKind.NUMERIC, a, b, pct, prev_as_of, curr_as_of))

    # --- growth maps (normalise search keys onto enrich keys) ------------ #
    for base in GROWTH_PATHS:
        pa, pb = get_path(prev, base), get_path(curr, base)
        if not isinstance(pb, dict):
            continue
        from .crustdata import CrustdataClient as _C
        na = _C.normalise_growth_map(pa if isinstance(pa, dict) else {})
        nb = _C.normalise_growth_map(pb)
        for k, v in nb.items():
            old = na.get(k)
            if old is None or old == v:
                continue
            deltas.append(
                Delta(f"{base}.{k}", DeltaKind.NUMERIC, old, v, None, prev_as_of, curr_as_of)
            )

    # --- scalars --------------------------------------------------------- #
    for path in SCALAR_PATHS:
        a, b = get_path(prev, path), get_path(curr, path)
        if a is None and b is None:
            continue
        if _norm_scalar(a) == _norm_scalar(b):
            continue
        deltas.append(Delta(path, DeltaKind.SCALAR, a, b, None, prev_as_of, curr_as_of))

    # --- keyed arrays ----------------------------------------------------- #
    for path, key in KEYED_ARRAY_PATHS:
        a_items = _as_list(get_path(prev, path))
        b_items = _as_list(get_path(curr, path))
        if a_items is None and b_items is None:
            continue
        a_map = {_identity(i, key): i for i in (a_items or [])}
        b_map = {_identity(i, key): i for i in (b_items or [])}
        for ident, item in b_map.items():
            if ident and ident not in a_map:
                deltas.append(
                    Delta(path, DeltaKind.ARRAY_ADDED, None, item, None, prev_as_of, curr_as_of)
                )
        # removals only make sense when we actually had a previous array
        if a_items is not None:
            for ident, item in a_map.items():
                if ident and ident not in b_map:
                    deltas.append(
                        Delta(path, DeltaKind.ARRAY_REMOVED, item, None, None,
                              prev_as_of, curr_as_of)
                    )

    return deltas


def _norm_scalar(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def _as_list(v: Any) -> list[Any] | None:
    if v is None:
        return None
    if isinstance(v, list):
        return v
    return [v]


def diff_snapshots(prev: Snapshot | None, curr: Snapshot | None) -> list[Delta]:
    if curr is None:
        return []
    return diff_payloads(
        prev.payload if prev else None,
        curr.payload,
        prev.as_of if prev else None,
        curr.as_of,
    )


def summarise(deltas: Iterable[Delta]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in deltas:
        out[d.kind.value] = out.get(d.kind.value, 0) + 1
    return out
