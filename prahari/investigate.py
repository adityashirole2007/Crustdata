"""The investigation agent — and the disconfirmation step that makes it honest.

Bounded by design: three to five tools, at most five steps. An unbounded agent
loop is the fastest way to lose a hackathon demo.

The important behaviour here is the order of operations. The agent does not
build a case FOR the alert. It is handed an alert that the deterministic scorer
has already decided to fire, and its first job is to try to KILL it, by working
through a fixed list of named confounders:

    divestiture / spin-off            — headcount fell but the business didn't
    profile page split by region      — phantom headcount collapse
    budget-cycle hiring freeze        — normal discipline, not distress
    rebrand mistaken for a shell      — a name change is not a shell
    seasonal business                 — the dip is annual
    identity resolution error         — we are watching the wrong "Shree Industries"

Only if it fails to disconfirm does the alert page anybody. A correctly
suppressed alert is a better demo than three catches.

Degradation: with no GEMINI_API_KEY the agent falls back to a deterministic
rule-based disconfirmer. It does not go quiet and it does not pretend. With a
Crustdata outage, every affected check is marked `data_unavailable` and the
confidence drops — the model is never allowed to answer from its own memory.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import get_settings
from .crustdata import CrustdataClient
from .diff import get_path, _as_float
from .models import Evidence, Snapshot, Vendor, utcnow
from .registry.ibbi import IBBIProvider

log = logging.getLogger("prahari.investigate")

CONFOUNDERS = {
    "divestiture_or_spinoff":
        "Did the headcount fall because a division was sold, spun off or transferred?",
    "profile_page_split_by_region":
        "Did a single company profile split into regional or per-entity profiles, "
        "making headcount look like it collapsed when it did not?",
    "budget_cycle_hiring_freeze":
        "Is the hiring stop a routine budget-cycle or post-funding discipline measure "
        "rather than a cash problem?",
    "rebrand_mistaken_for_shell":
        "Is the name/domain change a rebrand of a continuing business rather than a "
        "shell signature?",
    "seasonal_business":
        "Is this dip the vendor's normal seasonal pattern, visible in prior years?",
    "identity_resolution_error":
        "Are we certain this is the right legal entity, and not a same-named company?",
}


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    ok: bool
    result_summary: str
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class InvestigationOutcome:
    disconfirmed: bool
    confidence: str                      # high | medium | low
    verdict: str
    confounders_checked: dict[str, str]  # name -> ruled_out | supported | unknown
    tool_calls: list[ToolCall] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    data_unavailable: list[str] = field(default_factory=list)
    narration: str = ""
    engine: str = "rule_based"

    def to_dict(self) -> dict[str, Any]:
        return {
            "disconfirmed": self.disconfirmed,
            "confidence": self.confidence,
            "verdict": self.verdict,
            "confounders_checked": self.confounders_checked,
            "engine": self.engine,
            "narration": self.narration,
            "data_unavailable": self.data_unavailable,
            "tool_calls": [
                {"tool": t.tool, "args": t.args, "ok": t.ok, "result": t.result_summary}
                for t in self.tool_calls
            ],
            "evidence": [e.to_dict() for e in self.evidence],
        }


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

class InvestigationTools:
    """The bounded toolbelt. Every method returns (ok, summary, evidence)."""

    def __init__(self, client: CrustdataClient | None, vendor: Vendor) -> None:
        self.client = client
        self.vendor = vendor
        self.calls: list[ToolCall] = []

    def _record(self, tool: str, args: dict, ok: bool, summary: str,
                ev: list[Evidence] | None = None) -> ToolCall:
        tc = ToolCall(tool, args, ok, summary, ev or [])
        self.calls.append(tc)
        return tc

    # -- web -------------------------------------------------------------- #

    def web_search(self, query: str) -> ToolCall:
        if not self.client or not self.client.available:
            return self._record("web_search", {"query": query}, False,
                                "data_unavailable: no Crustdata key")
        res = self.client.web_search(query, limit=6)
        if not res.ok:
            return self._record("web_search", {"query": query}, False,
                                res.unavailable_note)
        items = res.data if isinstance(res.data, list) else res.data.get("results", [])
        ev = [
            Evidence(source="crustdata_web_search",
                     source_url=(i.get("url") or "") if isinstance(i, dict) else "",
                     fetched_at=res.fetched_at, field_path="web_search.result",
                     to_value=(i.get("title") or "")[:160] if isinstance(i, dict) else str(i)[:160])
            for i in (items or [])[:5] if isinstance(i, dict) and i.get("url")
        ]
        titles = "; ".join(e.to_value or "" for e in ev) or "no results"
        return self._record("web_search", {"query": query}, True, titles[:400], ev)

    def web_fetch(self, url: str) -> ToolCall:
        if not self.client or not self.client.available:
            return self._record("web_fetch", {"url": url}, False,
                                "data_unavailable: no Crustdata key")
        res = self.client.web_fetch(url)
        if not res.ok:
            return self._record("web_fetch", {"url": url}, False, res.unavailable_note)
        text = res.data.get("text", "") if isinstance(res.data, dict) else str(res.data)
        ev = [Evidence(source="crustdata_web_fetch", source_url=url,
                       fetched_at=res.fetched_at, field_path="web_fetch.text",
                       to_value=text[:200])]
        return self._record("web_fetch", {"url": url}, True, text[:800], ev)

    # -- graph ------------------------------------------------------------ #

    def company_search(self, query: dict[str, Any]) -> ToolCall:
        if not self.client or not self.client.available:
            return self._record("company_search", query, False,
                                "data_unavailable: no Crustdata key")
        res = self.client.company_search(query)
        if not res.ok:
            return self._record("company_search", query, False, res.unavailable_note)
        rows = res.data if isinstance(res.data, list) else res.data.get("companies", [])
        names = [r.get("name") for r in (rows or []) if isinstance(r, dict)]
        ev = [Evidence(source="crustdata_search",
                       source_url="https://api.crustdata.com/company/search",
                       fetched_at=res.fetched_at, field_path="company_search.results",
                       to_value=f"{len(names)} entities: {', '.join(filter(None, names))[:200]}")]
        return self._record("company_search", query, True,
                            f"{len(names)} match(es): {', '.join(filter(None, names))[:300]}", ev)

    def person_enrich(self, name: str) -> ToolCall:
        if not self.client or not self.client.available:
            return self._record("person_enrich", {"name": name}, False,
                                "data_unavailable: no Crustdata key")
        res = self.client.person_enrich(names=[name])
        if not res.ok:
            return self._record("person_enrich", {"name": name}, False, res.unavailable_note)
        rows = res.data if isinstance(res.data, list) else [res.data]
        first = rows[0] if rows else {}
        cur = (first or {}).get("current_company") or (first or {}).get("current_title") or "?"
        ev = [Evidence(source="crustdata_person_enrich",
                       source_url="https://api.crustdata.com/person/enrich",
                       fetched_at=res.fetched_at, field_path="person.current_company",
                       to_value=str(cur)[:160])]
        return self._record("person_enrich", {"name": name}, True,
                            f"{name} now at: {cur}", ev)

    # -- registry --------------------------------------------------------- #

    def ibbi_lookup(self, cin: str | None) -> ToolCall:
        res = IBBIProvider().lookup(cin=cin)
        if not res.ok:
            return self._record("ibbi_lookup", {"cin": cin}, False, res.reason or "failed")
        ev = [f.to_evidence() for f in res.facts]
        summary = "; ".join(f"{f.kind}={f.status}" for f in res.facts)
        return self._record("ibbi_lookup", {"cin": cin}, True, summary, ev)


# --------------------------------------------------------------------------- #
# rule-based disconfirmation (always runs; the LLM narrates on top of it)
# --------------------------------------------------------------------------- #

SPINOFF_TERMS = ("divest", "divestiture", "spin off", "spin-off", "spinoff",
                 "sells unit", "sells division", "hives off", "demerger", "demerges",
                 "transfers business", "slump sale", "carve-out", "carve out")
REBRAND_TERMS = ("rebrand", "renamed", "new name", "changes name", "now known as",
                 "formerly known as")
SEASONAL_TERMS = ("seasonal", "off-season", "monsoon shutdown", "annual maintenance shutdown")


def _news_match(snap: Snapshot | None, terms: tuple[str, ...]) -> dict[str, Any] | None:
    """Look for confounder language in news we already hold.

    Cheaper and more citable than a web search, and it means the disconfirmation
    step still works with no network — which is what makes the suppression
    behaviour testable and demoable offline.
    """
    if snap is None:
        return None
    items = get_path(snap.payload, "news")
    if not isinstance(items, list):
        return None
    for it in items:
        if not isinstance(it, dict):
            continue
        blob = f"{it.get('title', '')} {it.get('summary', '')}".lower()
        hits = [t for t in terms if t in blob]
        if hits:
            return {
                "title": str(it.get("title", ""))[:200],
                "url": it.get("article_url") or "",
                "published_at": it.get("published_at"),
                "matched": ", ".join(hits),
            }
    return None


def rule_based_disconfirm(history: list[Snapshot], vendor: Vendor,
                          fired_signal_ids: set[str],
                          tools: InvestigationTools) -> InvestigationOutcome:
    checked: dict[str, str] = {k: "unknown" for k in CONFOUNDERS}
    evidence: list[Evidence] = []
    unavailable: list[str] = []
    notes: list[str] = []

    curr = history[-1] if history else None
    prev = history[-2] if len(history) >= 2 else None

    # 1 — identity resolution. Check this FIRST; everything downstream is void
    #     if we are watching the wrong legal entity.
    if vendor.domain and curr is not None:
        site = get_path(curr.payload, "basic_info.website") or ""
        dom = vendor.domain.lower().replace("www.", "")
        if dom and dom.split("/")[0] in str(site).lower().replace("www.", ""):
            checked["identity_resolution_error"] = "ruled_out"
            notes.append(f"Identity confirmed: resolved profile website matches {dom}.")
        else:
            checked["identity_resolution_error"] = "supported"
            notes.append(
                f"Identity NOT confirmed: ledger domain {dom} does not match the "
                f"resolved profile website {site!r}. Treat every downstream signal "
                f"as suspect until resolved."
            )
    else:
        unavailable.append("identity_resolution_error: no domain on ledger or no snapshot")

    # 2 — divestiture / spin-off. Only relevant if headcount actually fell.
    if "headcount_decline" in fired_signal_ids:
        # Check the news we already hold before spending a web search. The graph
        # payload is cited evidence in its own right, and it works offline.
        local_hit = _news_match(curr, SPINOFF_TERMS)
        if local_hit:
            checked["divestiture_or_spinoff"] = "supported"
            notes.append(
                f"A divestiture/demerger is reported in the vendor's own news feed: "
                f"{local_hit['title']!r}. The headcount fall is structural, not distress."
            )
            evidence.append(Evidence(
                source="crustdata_enrich", source_url=local_hit["url"],
                fetched_at=curr.fetched_at if curr else "",
                field_path="news[].article_url",
                as_of=local_hit.get("published_at"),
                to_value=local_hit["title"][:180],
                note=f"matched divestiture language: {local_hit['matched']}",
            ))
        else:
            tc = tools.web_search(
                f'"{vendor.name}" (divestiture OR demerger OR "spin off" OR '
                f'"sells division" OR "slump sale")'
            )
            if not tc.ok:
                unavailable.append(f"divestiture_or_spinoff: {tc.result_summary}")
            elif any(t in tc.result_summary.lower() for t in SPINOFF_TERMS):
                checked["divestiture_or_spinoff"] = "supported"
                notes.append("Public reporting references a divestiture or demerger — the "
                             "headcount fall may be structural, not distress.")
                evidence.extend(tc.evidence)
            else:
                checked["divestiture_or_spinoff"] = "ruled_out"
                notes.append("No divestiture, demerger or slump-sale reporting found.")
                evidence.extend(tc.evidence[:2])

        # 3 — profile page split. A split shows headcount collapsing while the
        #     office/location count stays put or grows.
        if prev is not None and curr is not None:
            a = len(get_path(prev.payload, "locations.all_office_addresses") or [])
            b = len(get_path(curr.payload, "locations.all_office_addresses") or [])
            h_prev = _as_float(get_path(prev.payload, "headcount.total")) or 0
            h_curr = _as_float(get_path(curr.payload, "headcount.total")) or 0
            if h_prev and h_curr < h_prev * 0.75 and b >= a and a > 0:
                checked["profile_page_split_by_region"] = "supported"
                notes.append(
                    f"Headcount fell {h_prev:.0f}->{h_curr:.0f} while office count held at "
                    f"{a}->{b}. Consistent with a profile split rather than a real reduction."
                )
            else:
                checked["profile_page_split_by_region"] = "ruled_out"
                notes.append("Office footprint moved with headcount — not a profile split.")
        else:
            unavailable.append("profile_page_split_by_region: need two snapshots")

        # 4 — seasonality. Needs at least a year of history to assert anything.
        if len(history) >= 13:
            checked["seasonal_business"] = "unknown"
            notes.append("Seasonality check requires a same-month prior-year comparison; "
                         "history present but not yet modelled.")
        else:
            unavailable.append(
                f"seasonal_business: only {len(history)} snapshots, need 13+ for a "
                f"year-on-year month comparison"
            )

    # 5 — budget-cycle hiring freeze
    if "hiring_freeze" in fired_signal_ids:
        if "salary_delay_reviews" in fired_signal_ids or "leadership_exodus" in fired_signal_ids:
            checked["budget_cycle_hiring_freeze"] = "ruled_out"
            notes.append("Hiring stop coincides with payroll-stress or finance-leadership "
                         "signals — this is not routine budget discipline.")
        else:
            checked["budget_cycle_hiring_freeze"] = "unknown"
            notes.append("Hiring stop is not corroborated by any cash-stress signal; it may "
                         "be ordinary budget discipline.")

    # 6 — rebrand mistaken for a shell
    if "restructuring_markers" in fired_signal_ids:
        local = _news_match(curr, REBRAND_TERMS)
        if local:
            checked["rebrand_mistaken_for_shell"] = "supported"
            notes.append(f"Name change corroborated as a public rebrand: {local['title']!r}.")
            evidence.append(Evidence(
                source="crustdata_enrich", source_url=local["url"],
                fetched_at=curr.fetched_at if curr else "",
                field_path="news[].article_url", to_value=local["title"][:180],
                note=f"matched rebrand language: {local['matched']}"))
            tc = None
        else:
            tc = tools.web_search(f'"{vendor.name}" (rebrand OR renamed OR "now known as")')
        if tc is None:
            pass
        elif not tc.ok:
            unavailable.append(f"rebrand_mistaken_for_shell: {tc.result_summary}")
        elif any(t in tc.result_summary.lower() for t in REBRAND_TERMS):
            checked["rebrand_mistaken_for_shell"] = "supported"
            notes.append("Name change is corroborated as a public rebrand of a continuing "
                         "business.")
            evidence.extend(tc.evidence)
        else:
            checked["rebrand_mistaken_for_shell"] = "ruled_out"
            notes.append("No public rebrand announcement found for the name change.")

    # 7 — registry corroboration. Late but authoritative.
    if vendor.cin:
        tc = tools.ibbi_lookup(vendor.cin)
        if tc.ok:
            evidence.extend(tc.evidence)
            notes.append(f"IBBI (live): {tc.result_summary}")
        else:
            unavailable.append(f"ibbi: {tc.result_summary}")

    # ---- verdict -------------------------------------------------------- #
    supported = [k for k, v in checked.items() if v == "supported"]
    # An identity failure alone is enough to hold the alert: we may be looking
    # at the wrong company entirely.
    disconfirmed = bool(supported)
    if disconfirmed:
        verdict = (
            "SUPPRESS — a benign explanation is supported: "
            + ", ".join(s.replace("_", " ") for s in supported)
        )
        confidence = "medium"
    else:
        ruled_out = sum(1 for v in checked.values() if v == "ruled_out")
        verdict = (f"FIRE — attempted to disconfirm on {len(CONFOUNDERS)} named confounders; "
                   f"{ruled_out} ruled out, none supported a benign explanation.")
        confidence = "high" if ruled_out >= 3 and not unavailable else "medium"
    if unavailable and not disconfirmed:
        confidence = "low" if len(unavailable) >= 3 else confidence

    return InvestigationOutcome(
        disconfirmed=disconfirmed, confidence=confidence, verdict=verdict,
        confounders_checked=checked, tool_calls=tools.calls, evidence=evidence,
        data_unavailable=unavailable, narration=" ".join(notes), engine="rule_based",
    )


# --------------------------------------------------------------------------- #
# LLM narration layer (optional, never authoritative over the facts)
# --------------------------------------------------------------------------- #

NARRATION_SYSTEM = """You are the narration layer of a vendor-risk system used by \
procurement teams.

You are given: (a) deterministic signal results computed in code, (b) the output of a \
rule-based disconfirmation pass, (c) evidence records.

Hard rules:
- You may NOT introduce any fact that is not present in the input. No outside knowledge.
- You may NOT change any number, status or verdict. You rephrase and prioritise only.
- If the input says a check was data_unavailable, say so plainly. Never fill the gap.
- Write for a category manager who has 30 seconds: what changed, why it matters to \
supply continuity or payment risk, what to do.
- Three short paragraphs maximum. No bullet lists. No preamble.

Return JSON: {"narration": str, "recommended_action": str}"""


FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash",
                   "gemini-3.1-flash-lite", "gemini-2.5-flash"]
_working_model: str | None = None


def _gemini_once(model: str, system: str, user: str, max_tokens: int) -> str:
    import httpx
    settings = get_settings()
    resp = httpx.post(
        f"{settings.gemini_base_url}/models/{model}:generateContent",
        headers={"x-goog-api-key": settings.gemini_api_key or "",
                 "Content-Type": "application/json"},
        json={"systemInstruction": {"parts": [{"text": system}]},
              "contents": [{"role": "user", "parts": [{"text": user}]}],
              "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}},
        timeout=45,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini {model} HTTP {resp.status_code}: {resp.text[:200]}")
    cands = resp.json().get("candidates") or []
    if not cands:
        raise RuntimeError(f"Gemini {model}: no candidates (blocked or empty)")
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def llm_chat(system: str, user: str, max_tokens: int = 2048) -> str:
    """Call Gemini. Tries the configured model, then current fallbacks on 404/403/400.
    Raises if nothing works — callers fall back to rule-based text."""
    global _working_model
    settings = get_settings()
    order = [m for m in [_working_model, settings.llm_model, *FALLBACK_MODELS] if m]
    seen, errors = set(), []
    for model in order:
        if model in seen:
            continue
        seen.add(model)
        try:
            out = _gemini_once(model, system, user, max_tokens)
            _working_model = model
            return out
        except RuntimeError as exc:
            errors.append(str(exc))
            if "HTTP 429" in str(exc) or "HTTP 401" in str(exc):
                break        # rate limit / bad key: other models will not help
    raise RuntimeError(" | ".join(errors))


def working_model() -> str | None:
    return _working_model


def llm_narrate(outcome: InvestigationOutcome, vendor: Vendor,
                signal_rows: list[dict[str, Any]], distress_band: str) -> dict[str, str] | None:
    settings = get_settings()
    if not settings.has_llm:
        return None
    payload = {
        "vendor": {"name": vendor.name, "sole_source": vendor.sole_source,
                   "open_po_value_inr": vendor.open_po_value_inr,
                   "lead_time_days": vendor.lead_time_days},
        "distress_band": distress_band,
        "signals": signal_rows,
        "disconfirmation": {
            "disconfirmed": outcome.disconfirmed, "verdict": outcome.verdict,
            "confounders": outcome.confounders_checked,
            "data_unavailable": outcome.data_unavailable,
            "notes": outcome.narration,
        },
    }
    try:
        text = llm_chat(NARRATION_SYSTEM, json.dumps(payload, default=str))
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as exc:                      # narration is never load-bearing
        log.warning("LLM narration unavailable (%s) — using rule-based text", exc)
        return None


# --------------------------------------------------------------------------- #

def investigate(history: list[Snapshot], vendor: Vendor, fired_signal_ids: set[str],
                signal_rows: list[dict[str, Any]], distress_band: str,
                client: CrustdataClient | None = None) -> InvestigationOutcome:
    """Run the bounded investigation. Disconfirmation is mandatory."""
    tools = InvestigationTools(client, vendor)
    outcome = rule_based_disconfirm(history, vendor, fired_signal_ids, tools)
    narr = llm_narrate(outcome, vendor, signal_rows, distress_band)
    if narr:
        outcome.engine = "rule_based+llm_narration"
        outcome.narration = narr.get("narration", outcome.narration)
        outcome._recommended = narr.get("recommended_action")   # type: ignore[attr-defined]
    return outcome
