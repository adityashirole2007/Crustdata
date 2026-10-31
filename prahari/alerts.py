"""Alert engine: hysteresis, cooldown, case-file merging, drafted procurement action.

Alert fatigue is the real failure mode of every monitoring product ever built.
A vendor shedding 3% of headcount every month for a year must not generate
twelve tickets — it must generate one case file that gets twelve entries. Three
mechanisms enforce that:

  1. HYSTERESIS   — fire on a BAND TRANSITION upward, never on a raw value.
                    Coming back down requires clearing the band floor by a
                    buffer, so a vendor oscillating on a boundary does not flap.
  2. COOLDOWN     — per (vendor, signal) and per vendor. The same signal cannot
                    re-page inside its cooldown window even if the band moves.
  3. CASE MERGING — an alert for a vendor with an open case appends an entry to
                    that case. New tickets are the exception, not the default.

Everything a suppressed alert knows is still written to the case file. Silence
is not forgetting.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from .evidence import CitationGuard, claims_from_registry, claims_from_signals, evidence_pack
from .exposure import explain as explain_exposure, priority
from .models import Alert, Claim, RegistryFact, Score, Vendor, utcnow
from .store import Store


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(ts[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _alert_id(vendor_id: str, as_of: str, band: str) -> str:
    h = hashlib.sha256(f"{vendor_id}|{as_of}|{band}".encode()).hexdigest()[:12]
    return f"ALT-{h}"


def _case_id(vendor_id: str, opened: str) -> str:
    h = hashlib.sha256(f"{vendor_id}|{opened}".encode()).hexdigest()[:10]
    return f"CASE-{h}"


# --------------------------------------------------------------------------- #
# hysteresis
# --------------------------------------------------------------------------- #

def band_order(cfg: dict) -> list[str]:
    bands = cfg["distress"]["bands"]
    return ["none", *sorted(bands, key=lambda k: bands[k])]


def effective_band(score: float, cfg: dict) -> str:
    bands = cfg["distress"]["bands"]
    out = "none"
    for name in sorted(bands, key=lambda k: bands[k]):
        if score >= float(bands[name]):
            out = name
    return out


def apply_hysteresis(vendor_id: str, raw_band: str, score: float,
                     store: Store, cfg: dict) -> tuple[str, bool, str]:
    """Return (band_after_hysteresis, should_fire, reason).

    should_fire is True only on an UPWARD band transition.
    """
    hc = cfg["alerts"]["hysteresis"]
    order = band_order(cfg)
    prev_band, _prev_score = store.get_band_state(vendor_id)
    prev_band = prev_band or "none"

    prev_i = order.index(prev_band) if prev_band in order else 0
    new_i = order.index(raw_band) if raw_band in order else 0

    if new_i < prev_i:
        # Downgrade requires clearing the floor of the band we are leaving by a buffer.
        buffer = float(hc.get("downgrade_buffer_points", 8))
        leaving_floor = float(cfg["distress"]["bands"].get(prev_band, 0))
        if score > leaving_floor - buffer:
            return prev_band, False, (
                f"held at '{prev_band}': score {score:.1f} has not cleared "
                f"{leaving_floor - buffer:.1f} (floor {leaving_floor:.0f} minus "
                f"{buffer:.0f}-point buffer) — avoids flapping"
            )
        return raw_band, False, f"downgraded {prev_band} -> {raw_band}; no page on a downgrade"

    if new_i == prev_i:
        return raw_band, False, (
            f"no band transition (still '{raw_band}') — value changes alone do not page"
        )

    if hc.get("fire_on_band_increase_only", True):
        return raw_band, True, f"band transition {prev_band} -> {raw_band}"
    return raw_band, True, "band changed"


# --------------------------------------------------------------------------- #
# cooldown
# --------------------------------------------------------------------------- #

def cooldown_block(vendor_id: str, fired_signal_ids: list[str], now: str,
                   store: Store, cfg: dict) -> str | None:
    """Return a suppression reason, or None if the alert may proceed."""
    cc = cfg["alerts"]["cooldown"]
    now_dt = _parse(now) or datetime.now(timezone.utc)

    per_signal_days = int(cc.get("per_signal_days", 21))
    fresh: list[str] = []
    for sid in fired_signal_ids:
        last = _parse(store.last_signal_fire(vendor_id, sid))
        if last is None or (now_dt - last) > timedelta(days=per_signal_days):
            fresh.append(sid)
    if fired_signal_ids and not fresh:
        return (f"cooldown: every firing signal ({', '.join(fired_signal_ids)}) already "
                f"paged within {per_signal_days} days — appended to the case file instead")

    window_days = int(cc.get("per_vendor_max_pages_per_days", 7))
    max_pages = int(cc.get("per_vendor_max_pages", 1))
    recent = [
        a for a in store.list_alerts(include_suppressed=False)
        if a["vendor_id"] == vendor_id
        and (p := _parse(a["created_at"])) is not None
        and (now_dt - p) <= timedelta(days=window_days)
    ]
    if len(recent) >= max_pages:
        return (f"cooldown: {len(recent)} page(s) already sent for this vendor in the last "
                f"{window_days} days (cap {max_pages}) — appended to the case file instead")
    return None


# --------------------------------------------------------------------------- #
# recommended action + memo
# --------------------------------------------------------------------------- #

def recommend_action(band: str, prio: str, vendor: Vendor,
                     registry_facts: list[RegistryFact], disconfirmed: bool) -> str:
    if disconfirmed:
        return ("No action. A benign explanation was corroborated during investigation; "
                "logged to the case file and will continue to be monitored.")
    hard = {f.kind for f in registry_facts}
    if hard & {"ibbi_cirp_admitted", "ibbi_liquidation", "mca_strike_off"}:
        return ("STOP. Registry confirms an insolvency or strike-off process. Freeze all "
                "open POs and outgoing payments immediately, file a claim with the "
                "resolution professional if CIRP, and activate the second source today.")
    if band == "critical" and prio == "P1":
        return ("Place a payment hold on open invoices pending a solvency confirmation call, "
                "request the latest GST filing proof and a bank reference, and start "
                "qualifying an alternate supplier this week. Do not place new POs.")
    if band in ("critical", "high"):
        return ("Request updated financials and a GST filing confirmation within 7 days. "
                f"Cap new PO exposure{' and begin second-source qualification' if vendor.sole_source else ''}. "
                "Escalate to a payment hold if the next run shows no improvement.")
    if band == "elevated":
        return ("Add to the weekly watch list. Confirm the delivery schedule for open POs "
                "directly with the vendor's operations contact and record the response.")
    return "Monitor. No procurement action required at this score."


def draft_memo(vendor: Vendor, band: str, prio: str, rendered_claims: str,
               action: str, as_of: str, disconfirmed: bool) -> str:
    if disconfirmed:
        subject = f"[FYI — no action] {vendor.name}: risk signal investigated and cleared"
    else:
        subject = (f"[{prio} / {band.upper()}] {vendor.name}: vendor risk escalation — "
                   f"action required")
    to = vendor.category_manager or "Category Manager"
    po = f"Rs {vendor.open_po_value_inr:,.0f}" if vendor.open_po_value_inr else "not recorded"
    ss = "YES — single qualified source" if vendor.sole_source else "no"
    return f"""To: {to}
Subject: {subject}

{vendor.name} crossed into the '{band}' distress band on the {as_of} evaluation.

Exposure: open PO value {po}; sole source: {ss}; lead time {vendor.lead_time_days} days.

What changed, and where each statement comes from:

{rendered_claims}

Recommended action:
{action}

This assessment was produced by Prahari from the vendor's live digital footprint and
public registry data. Every statement above carries its source, the field it came from
and the timestamp it was fetched. Items marked data_unavailable were not checked — they
are not clean findings.

-- Prahari, Continuous Vendor Risk Agent
"""


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #

def build_alert(vendor: Vendor, distress: Score, legitimacy: Score,
                registry_facts: list[RegistryFact], investigation: Any,
                as_of: str, cfg: dict, store: Store) -> Alert:
    """Assemble, guard, suppress-or-fire, and merge into the case file."""
    guard = CitationGuard(cfg)

    raw_band = effective_band(distress.value, cfg)
    if distress.overridden_by:
        raw_band = distress.band      # a registry override wins outright

    band, transition_fire, hyst_reason = apply_hysteresis(
        vendor.vendor_id, raw_band, distress.value, store, cfg
    )
    prio, prio_score, ef = priority(distress.value, vendor, cfg)

    fired_signals = [s for s in distress.signals if s.fired]
    fired_ids = [s.signal_id for s in fired_signals]

    claims: list[Claim] = claims_from_signals(fired_signals)
    claims += claims_from_registry(registry_facts)

    # Legitimacy findings ride along only when they are material.
    if legitimacy.band in ("critical", "high"):
        claims += claims_from_signals([s for s in legitimacy.signals if s.fired])

    rendered = guard.render(claims) if claims else "_No cited findings._"

    disconfirmed = bool(getattr(investigation, "disconfirmed", False))
    suppressed = False
    reason: str | None = None

    if disconfirmed:
        suppressed, reason = True, (
            f"investigation disconfirmed: {getattr(investigation, 'verdict', '')}"
        )
    elif not transition_fire:
        suppressed, reason = True, hyst_reason
    else:
        cd = cooldown_block(vendor.vendor_id, fired_ids, as_of, store, cfg)
        if cd:
            suppressed, reason = True, cd

    action = recommend_action(band, prio, vendor, registry_facts, disconfirmed)
    override = getattr(investigation, "_recommended", None)
    if override and not disconfirmed:
        action = override

    memo = draft_memo(vendor, band, prio, rendered, action, as_of, disconfirmed) \
        if not suppressed else ""

    alert = Alert(
        alert_id=_alert_id(vendor.vendor_id, as_of, band),
        vendor_id=vendor.vendor_id, vendor_name=vendor.name,
        created_at=utcnow(), as_of=as_of, severity=band, priority=prio,
        priority_score=prio_score, distress=distress, legitimacy=legitimacy,
        exposure_factor=ef, claims=claims, recommended_action=action,
        drafted_memo=memo,
        investigation=(investigation.to_dict() if hasattr(investigation, "to_dict")
                       else dict(investigation or {})),
        suppressed=suppressed, suppression_reason=reason,
        registry_facts=registry_facts,
    )

    # --- persist state ------------------------------------------------- #
    store.set_band_state(vendor.vendor_id, band, distress.value)
    if not suppressed:
        for sid in fired_ids:
            store.record_signal_fire(vendor.vendor_id, sid, as_of)

    case_id = _merge_case(alert, store, cfg, hyst_reason, explain_exposure(vendor, cfg))
    store.save_alert(alert.to_dict(), case_id)
    return alert


def _merge_case(alert: Alert, store: Store, cfg: dict, hyst_reason: str,
                exposure_detail: dict[str, Any]) -> str:
    """Append to the open case for this vendor, or open one."""
    existing = store.open_case(alert.vendor_id) if cfg["alerts"]["case_file"].get(
        "merge_into_open_case", True) else None
    if existing:
        case_id = existing["case_id"]
        entries = list(existing["entries"])
    else:
        case_id = _case_id(alert.vendor_id, alert.as_of)
        entries = []
    entries.append({
        "at": alert.as_of,
        "alert_id": alert.alert_id,
        "band": alert.severity,
        "priority": alert.priority,
        "distress": round(alert.distress.value, 1) if alert.distress else None,
        "legitimacy": round(alert.legitimacy.value, 1) if alert.legitimacy else None,
        "paged": not alert.suppressed,
        "suppression_reason": alert.suppression_reason,
        "hysteresis": hyst_reason,
        "exposure": exposure_detail,
        "fired_signals": [s.signal_id for s in (alert.distress.signals if alert.distress else [])
                          if s.fired],
        "investigation_verdict": alert.investigation.get("verdict"),
        "evidence": evidence_pack(alert.claims),
    })
    store.upsert_case(case_id, alert.vendor_id, alert.severity, entries, status="open")
    return case_id
