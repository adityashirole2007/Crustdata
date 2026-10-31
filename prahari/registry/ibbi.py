"""IBBI CIRP registry — LIVE, public, free, CIN-keyed.

This is the highest-severity binary signal available in India and it is the one
public source that needs no licence and no captcha: the Insolvency and
Bankruptcy Board of India publishes CIRP and liquidation public announcements
at ibbi.gov.in, keyed on CIN.

Honesty rules baked in:
  * A fetch failure returns ok=False. It NEVER returns "no insolvency found",
    because "we couldn't reach the registry" and "this company is clean" are
    completely different statements to put in front of a procurement lead.
  * Parsing is conservative. If the page shape changes and we cannot confidently
    classify it, we return ok=False with the raw URL so a human can look.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from ..models import RegistryFact, utcnow
from .base import RegistryProvider, RegistryResult

IBBI_BASE = "https://ibbi.gov.in"
IBBI_CIN_URL = IBBI_BASE + "/en/claims/pub-process/{cin}"
IBBI_SEARCH_URL = IBBI_BASE + "/en/claims/public-announcement"

CIN_RE = re.compile(r"^[LUu]\d{5}[A-Za-z]{2}\d{4}[A-Za-z]{3}\d{6}$")

# Conservative classification. Order matters: liquidation outranks CIRP.
CLASSIFIERS: list[tuple[str, str, tuple[str, ...]]] = [
    ("ibbi_liquidation", "LIQUIDATION",
     ("liquidation commencement", "liquidation order", "under liquidation",
      "voluntary liquidation")),
    ("ibbi_cirp_admitted", "CIRP_ADMITTED",
     ("corporate insolvency resolution process", "cirp", "insolvency commencement date",
      "interim resolution professional", "public announcement of corporate insolvency")),
]


class IBBIProvider(RegistryProvider):
    name = "ibbi"
    live = True

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    # ------------------------------------------------------------------ #

    @staticmethod
    def valid_cin(cin: str | None) -> bool:
        return bool(cin) and bool(CIN_RE.match(cin.strip()))

    def lookup(self, *, cin: str | None = None, gstin: str | None = None,
               name: str | None = None, as_of: str | None = None) -> RegistryResult:
        if not cin:
            return RegistryResult(
                ok=False, provider=self.name,
                reason="no CIN on the vendor record — IBBI lookup is CIN-keyed",
            )
        cin = cin.strip().upper()
        if not self.valid_cin(cin):
            return RegistryResult(
                ok=False, provider=self.name,
                reason=f"'{cin}' is not a well-formed 21-character CIN",
            )

        url = IBBI_CIN_URL.format(cin=cin)
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as c:
                r = c.get(url, headers={"user-agent": "Prahari/1.0 (vendor-risk monitor)"})
        except httpx.HTTPError as exc:
            return RegistryResult(
                ok=False, provider=self.name,
                reason=f"could not reach IBBI ({exc}) — status unknown, NOT clean",
            )

        if r.status_code == 404:
            # A 404 on the CIN-keyed process page is the registry's own way of
            # saying "no published process for this CIN". That IS informative.
            return RegistryResult(
                ok=True, provider=self.name,
                facts=[RegistryFact(
                    provider="ibbi", kind="ibbi_no_process", status="NO_PUBLISHED_PROCESS",
                    as_of=(as_of or utcnow()[:10]), source_url=url,
                    fetched_at=utcnow(), live=True,
                    detail={"http_status": 404, "cin": cin},
                )],
            )
        if r.status_code >= 400:
            return RegistryResult(
                ok=False, provider=self.name,
                reason=f"IBBI returned HTTP {r.status_code} — status unknown, NOT clean",
            )

        text = _strip_html(r.text).lower()
        for kind, status, needles in CLASSIFIERS:
            if any(n in text for n in needles):
                return RegistryResult(
                    ok=True, provider=self.name,
                    facts=[RegistryFact(
                        provider="ibbi", kind=kind, status=status,
                        as_of=_extract_date(text) or (as_of or utcnow()[:10]),
                        source_url=url, fetched_at=utcnow(), live=True,
                        detail={"cin": cin, "matched": [n for n in needles if n in text]},
                    )],
                )

        if _looks_empty(text):
            return RegistryResult(
                ok=True, provider=self.name,
                facts=[RegistryFact(
                    provider="ibbi", kind="ibbi_no_process", status="NO_PUBLISHED_PROCESS",
                    as_of=(as_of or utcnow()[:10]), source_url=url,
                    fetched_at=utcnow(), live=True, detail={"cin": cin},
                )],
            )

        # Page returned something we cannot classify. Say so; do not guess.
        return RegistryResult(
            ok=False, provider=self.name,
            reason=("IBBI page fetched but its shape was not recognised — refusing to "
                    f"classify. Check manually: {url}"),
        )

    # ------------------------------------------------------------------ #

    def search_by_name(self, name: str) -> RegistryResult:
        """Name search is advisory only — IBBI's authoritative key is the CIN.

        Never used to assert a status; only to hand a human a link.
        """
        return RegistryResult(
            ok=False, provider=self.name,
            reason=(f"name search is advisory only; verify '{name}' manually at "
                    f"{IBBI_SEARCH_URL}"),
        )


# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})")


def _strip_html(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _extract_date(text: str) -> str | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    if "-" in raw and len(raw.split("-")[0]) == 4:
        return raw
    parts = re.split(r"[/-]", raw)
    if len(parts) == 3:
        d, mo, y = parts
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def _looks_empty(text: str) -> bool:
    markers = ("no record", "no records found", "not found", "no data available",
               "0 results", "no public announcement")
    return any(m in text for m in markers) or len(text) < 400
