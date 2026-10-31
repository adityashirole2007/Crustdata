"""Prahari operator console — one page.

Built last, on purpose. Building the dashboard before the pipeline works is one
of the named ways to lose a day.

Five tabs, in the order a procurement lead actually uses them:
  Queue        — every vendor ranked by Distress x Exposure, suppressed ones greyed
  Case file    — one vendor: score, per-signal contribution, evidence, memo
  Replay       — the backtest curve and the lead-time table
  Guard        — the citation guard refusing an uncited claim, live
  System       — entitlements, registry provenance, webhook log, known gaps
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prahari.config import REPO_ROOT, get_settings, load_config          # noqa: E402
from prahari.crustdata import CrustdataClient                            # noqa: E402
from prahari.evidence import CitationError, CitationGuard                # noqa: E402
from prahari.exposure import explain as explain_exposure                 # noqa: E402
from prahari.models import Claim, Evidence                               # noqa: E402
from prahari.pipeline import evaluate_vendor                             # noqa: E402
from prahari.registry.cached import CachedRegistryProvider               # noqa: E402
from prahari.replay import run_backtest                                  # noqa: E402
from prahari.scoring import contribution_table                           # noqa: E402
from prahari.store import Store                                          # noqa: E402

st.set_page_config(page_title="Prahari — Vendor Risk", page_icon="🛡", layout="wide")

BAND_COLOUR = {"critical": "#b3261e", "high": "#c25700", "elevated": "#8a6d00",
               "watch": "#0b6b75", "none": "#4a5568", "clear": "#1b5e20",
               "medium": "#8a6d00"}


@st.cache_resource
def _store() -> Store:
    return Store()


@st.cache_data(ttl=30)
def _cfg() -> dict:
    return load_config()


def chip(text: str, colour: str) -> str:
    return (f"<span style='background:{colour};color:#fff;padding:2px 10px;"
            f"border-radius:10px;font-size:0.8rem;font-weight:600'>{text}</span>")


store, cfg = _store(), _cfg()
settings = get_settings()

st.title("Prahari — Continuous Vendor Risk Agent")
st.caption("Procurement verifies a vendor once, at onboarding. Risk is a time series. "
           "Facts are deterministic code; only investigation and narration are LLM.")

with st.sidebar:
    st.subheader("Run")
    as_of = st.text_input("Evaluate as of (YYYY-MM-DD, blank = latest)", "")
    live_reg = st.checkbox("Live IBBI lookup", value=bool(settings.has_crustdata))
    st.divider()
    st.caption("**Data sources**")
    st.write("Crustdata", "✅" if settings.has_crustdata else "⚠️ no key — offline mode")
    st.write("LLM narration", "✅" if settings.has_llm else "⚠️ rule-based only")
    st.write("IBBI", "✅ live, public, CIN-keyed")
    st.write("MCA21 / GST", "📦 cached snapshot, dated")
    if st.button("Reload fixtures"):
        from prahari.replay import load_all_fixtures
        load_all_fixtures(store)
        st.success("fixtures reloaded")

tab_q, tab_case, tab_replay, tab_guard, tab_sys = st.tabs(
    ["Alert queue", "Case file", "Replay / backtest", "Citation guard", "System"]
)


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #
with tab_q:
    vendors = store.list_vendors()
    if not vendors:
        st.warning("No vendors. Run `prahari seed --fresh`.")
    else:
        client = CrustdataClient(config=cfg) if settings.has_crustdata else None
        rows, alerts = [], {}
        prog = st.progress(0.0, text="scoring vendors…")
        for i, v in enumerate(vendors, 1):
            a = evaluate_vendor(v, store, cfg, as_of or None, client,
                                live_registry=live_reg and bool(client))
            prog.progress(i / len(vendors), text=f"scoring {v.name}")
            if not a:
                continue
            alerts[v.vendor_id] = a
            rows.append({
                "Vendor": v.name, "ID": v.vendor_id,
                "Distress": round(a.distress.value, 1) if a.distress else 0,
                "Legitimacy": round(a.legitimacy.value, 1) if a.legitimacy else 100,
                "Band": a.severity, "Priority": a.priority,
                "Priority score": round(a.priority_score, 1),
                "Exposure": round(a.exposure_factor, 2),
                "Open PO (₹)": f"{v.open_po_value_inr:,.0f}",
                "Sole source": "yes" if v.sole_source else "",
                "Paged": "PAGED" if not a.suppressed else "suppressed",
                "Why not": (a.suppression_reason or "")[:90],
                "Coverage": f"{a.distress.coverage:.0%}" if a.distress else "",
            })
        prog.empty()
        if client:
            client.close()

        df = pd.DataFrame(rows).sort_values("Priority score", ascending=False)
        paged = (df["Paged"] == "PAGED").sum()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Vendors monitored", len(df))
        c2.metric("Paged a human", int(paged))
        c3.metric("Suppressed", int(len(df) - paged),
                  help="Logged to the case file. Silence is not forgetting.")
        c4.metric("P1", int((df["Priority"] == "P1").sum()))

        st.dataframe(
            df.style.apply(
                lambda r: ["background-color:#2a1215" if r["Paged"] == "PAGED"
                           else "opacity:0.55"] * len(r), axis=1),
            use_container_width=True, hide_index=True,
        )
        st.caption("Priority = Distress × Exposure. Exposure comes from the procurement "
                   "ledger — open PO value, sole-source flag, lead time, criticality. "
                   "A distressed vendor you buy ₹5L/yr from is a log line; the sole-source "
                   "supplier of a critical casting is a fire.")
        st.session_state["alerts"] = {k: a.to_dict() for k, a in alerts.items()}


# --------------------------------------------------------------------------- #
# Case file
# --------------------------------------------------------------------------- #
with tab_case:
    vendors = store.list_vendors()
    if vendors:
        pick = st.selectbox("Vendor", vendors, format_func=lambda v: f"{v.name} ({v.vendor_id})")
        client = CrustdataClient(config=cfg) if settings.has_crustdata else None
        alert = evaluate_vendor(pick, store, cfg, as_of or None, client,
                                live_registry=live_reg and bool(client))
        if client:
            client.close()

        if not alert:
            st.warning("No snapshots for this vendor.")
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Distress", f"{alert.distress.value:.0f}", alert.severity)
            c2.metric("Legitimacy", f"{alert.legitimacy.value:.0f}", alert.legitimacy.band)
            c3.metric("Priority", alert.priority, f"score {alert.priority_score:.0f}")
            c4.metric("Signal coverage", f"{alert.distress.coverage:.0%}",
                      help="Fraction of distress signals with data available. "
                           "Low coverage is not a clean bill of health.")

            if alert.suppressed:
                st.info(f"**Not paged.** {alert.suppression_reason}")
            else:
                st.error(f"**PAGED — {alert.priority} / {alert.severity.upper()}**")

            st.subheader("Score history")
            hist = store.snapshots(pick.vendor_id, as_of_max=as_of or None)
            if len(hist) > 1:
                pts = []
                for s in hist:
                    a2 = evaluate_vendor(pick, store, cfg, s.as_of, None,
                                         live_registry=False, run_investigation=False)
                    if a2:
                        pts.append({"as_of": s.as_of,
                                    "distress": a2.distress.value,
                                    "legitimacy": a2.legitimacy.value})
                if pts:
                    st.line_chart(pd.DataFrame(pts).set_index("as_of"))

            st.subheader("Per-signal contribution")
            st.caption("This is the whole score. Nothing is hidden in a model.")
            ct = pd.DataFrame(contribution_table(alert.distress))
            st.dataframe(ct, use_container_width=True, hide_index=True)

            st.subheader("Investigation — disconfirmation attempted before firing")
            inv = alert.investigation or {}
            st.markdown(f"**{inv.get('verdict','')}**  ·  engine: `{inv.get('engine','')}`")
            conf = inv.get("confounders_checked") or {}
            if conf:
                st.dataframe(
                    pd.DataFrame([{"Confounder": k.replace("_", " "), "Result": v}
                                  for k, v in conf.items()]),
                    use_container_width=True, hide_index=True)
            if inv.get("narration"):
                st.write(inv["narration"])
            if inv.get("data_unavailable"):
                st.warning("**data_unavailable** — these were not checked. "
                           "They are not clean findings.\n\n"
                           + "\n".join(f"- {x}" for x in inv["data_unavailable"]))

            st.subheader("Evidence-backed claims")
            st.caption("The renderer refuses to emit a sentence with no evidence record.")
            guard = CitationGuard(cfg)
            try:
                st.markdown(guard.render(alert.claims) if alert.claims
                            else "_No cited findings._")
            except CitationError as exc:
                st.error(str(exc))

            if alert.registry_facts:
                st.subheader("Registry")
                st.dataframe(pd.DataFrame([{
                    "Provider": f.provider, "Status": f.status, "As of": f.as_of,
                    "Lookup": "LIVE" if f.live else "cached snapshot",
                    "Source": f.source_url,
                } for f in alert.registry_facts]), use_container_width=True, hide_index=True)

            st.subheader("Exposure breakdown")
            st.json(explain_exposure(pick, cfg))

            st.subheader("Recommended action")
            st.success(alert.recommended_action)
            if alert.drafted_memo:
                st.subheader("Drafted memo to the category manager")
                st.code(alert.drafted_memo, language="text")

            case = store.open_case(pick.vendor_id)
            if case:
                st.subheader(f"Running case file — {case['case_id']}")
                st.caption("Alerts merge into one case per vendor. They do not open a new "
                           "ticket every run.")
                st.dataframe(pd.DataFrame([{
                    "At": e["at"], "Band": e["band"], "Distress": e["distress"],
                    "Paged": e["paged"], "Signals": ", ".join(e["fired_signals"]),
                    "Note": (e["suppression_reason"] or "")[:70],
                } for e in case["entries"]]), use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
with tab_replay:
    st.caption("Scores computed using only data dated on or before each evaluation date. "
               "Registry facts are replayed from dated observations, never fetched live, "
               "so a backtest cannot leak the outcome it is predicting.")
    if st.button("Run backtest", type="primary"):
        with st.spinner("replaying…"):
            rep = run_backtest(store, cfg)
        st.session_state["backtest"] = rep

    rep = st.session_state.get("backtest")
    if not rep:
        p = REPO_ROOT / "data" / "backtest_report.json"
        if p.exists():
            rep = json.loads(p.read_text(encoding="utf-8"))

    if rep:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Caught before the event", f"{rep['caught']}/{rep['n_distress_cases']}")
        c2.metric("Median lead time", f"{rep.get('median_lead_time_days') or '—'} days")
        c3.metric("False positives", f"{rep['false_positives']}/{rep['n_control_cases']}")
        c4.metric("Shells caught", f"{rep.get('shells_caught',0)}/{rep.get('n_shell_cases',0)}")

        for r in rep["results"]:
            ok = "✅" if r["correct"] else "❌"
            with st.expander(f"{ok}  {r['vendor_name']}  ·  {r['label']}", expanded=False):
                st.write(f"**Expected:** {r['expected_outcome']}")
                st.write(f"**Actual:** {r['actual_outcome']}")
                if r.get("notes"):
                    st.caption(r["notes"])
                pts = pd.DataFrame(r["points"])
                if not pts.empty:
                    st.line_chart(pts.set_index("as_of")[["distress", "legitimacy"]])
                    st.dataframe(
                        pts[["as_of", "distress", "legitimacy", "band", "priority",
                             "paged", "fired_signals", "suppression_reason"]],
                        use_container_width=True, hide_index=True)
        st.warning("**Provenance:** the bundled fixtures are SYNTHETIC and labelled as such. "
                   "They prove the pipeline, not real-world accuracy. Run "
                   "`scripts/build_real_case.py` with a genuine CIN to produce a real "
                   "validated lead time before quoting one.")


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
with tab_guard:
    st.caption("Every team will write 'every decision cites its reasons' in their README. "
               "The only way to prove it is to make the renderer structurally incapable "
               "of emitting an uncited sentence. There is no force flag.")
    guard = CitationGuard(cfg)
    good = Claim(
        text="Headcount fell from 214 to 139 between the May and August snapshots.",
        kind="numeric_change",
        evidence=(Evidence(source="crustdata_enrich",
                           source_url="https://api.crustdata.com/company/enrich",
                           fetched_at="2026-08-01T06:00:00+00:00",
                           field_path="headcount.total", as_of="2026-08-01",
                           from_value=214, to_value=139),), confidence="high")
    bad = Claim(text="The company is probably about to go under.", kind="judgement")

    st.subheader("1 · A properly cited claim")
    st.markdown(guard.render([good]))

    st.subheader("2 · The same renderer, handed an uncited claim")
    if st.button("Try to render an uncited claim", type="primary"):
        try:
            st.markdown(guard.render([good, bad]))
            st.error("Guard did not fire — that would be a bug.")
        except CitationError as exc:
            st.error(str(exc))
            st.success("Nothing was rendered. The alert cannot be emitted.")


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
with tab_sys:
    st.subheader("Crustdata field-group entitlements")
    ent = REPO_ROOT / "data" / "entitlements.json"
    if ent.exists():
        st.json(json.loads(ent.read_text(encoding="utf-8")))
    else:
        st.info("Run `prahari doctor` to probe which field groups this key is entitled to. "
                "technographics and social_posts are gated add-ons — a key without "
                "entitlement gets a 403, not an empty payload.")

    st.subheader("Registry provenance")
    cov = CachedRegistryProvider().coverage()
    st.write(f"IBBI — **live**, public, free, CIN-keyed.")
    st.write(f"MCA21 / GST — **cached snapshot**: {cov['mca_records']} MCA, "
             f"{cov['gst_records']} GST records, each stamped with an `as_of` date.")
    st.caption("MCA21 and GST have no public API, are captcha-gated, and their terms "
               "forbid scraping. The production path is a licensed reseller (Surepass / "
               "Signzy / Karza / Perfios) behind the same RegistryProvider interface. "
               "We do not fabricate registry data: a dated cache is honest, an invented "
               "live lookup is not.")

    st.subheader("Watcher")
    st.caption("Company entity watches refresh on a 30-day cycle and the baseline run "
               "fires on nothing, so the watcher is the production trigger and the "
               "snapshot-diff engine is the core. `POST /watch/company/{id}/test` is free "
               "and delivers a real HMAC-signed envelope — that is what the receiver "
               "below verifies.")
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8000/webhooks/received", timeout=2)
        st.json(r.json())
    except Exception:
        st.info("Receiver not running. Start it with "
                "`uvicorn prahari.webhook:app --port 8000`.")

    st.subheader("Known gaps")
    st.markdown(
        "- Indian SME coverage in the canonical graph is thin below ~50 employees. A "
        "₹20cr injection-moulding shop in Rajkot is close to invisible in a "
        "professional-network-shaped graph. The India registry layer exists to cover that.\n"
        "- EPFO establishment-wise contributions would give payroll distress roughly three "
        "months earlier than anything else public. Not built.\n"
        "- MCA charges / CERSAI (new secured borrowing, unsatisfied charges) — not built.\n"
        "- GSTR-3B filing-gap detection is modelled in the cache schema but needs a "
        "licensed feed to run live.\n"
        "- Seasonality disconfirmation needs 13+ months of history; with fewer snapshots "
        "it reports `data_unavailable` rather than guessing.\n"
        "- ERP write-back (SAP Ariba, Coupa, Zoho) so a hold actually blocks the PO."
    )
