"""MCA21 + GST via dated cached snapshots.

Why this file exists in this shape:

  MCA21 master data (Active / Under Process of Striking Off / Strike Off /
  Dissolved, directors, charges, last balance-sheet filing date) and GST
  (GSTIN status, GSTR-3B filing regularity) are the two highest-value Indian
  registry sources for vendor risk. Neither has a public API. Both are
  captcha-gated, and the MCA terms of service forbid scraping. The production
  path is a licensed reseller — Surepass, Signzy, Karza, Perfios or
  InstaFinancials — behind the same RegistryProvider interface.

  So in this build they are served from a cache file, every record stamped with
  an explicit `as_of` date and `source: registry_cached_snapshot`. Nothing is
  invented. If a CIN or GSTIN is not in the cache, this provider returns
  ok=False — it does not fall back to a guess, and it does not report "Active"
  by default. "We have no registry observation for this vendor" is a legitimate
  and useful thing to tell a procurement lead; a fabricated status is not.

  Swap in a licensed provider by implementing lookup() against their API and
  registering it in registry/__init__.all_providers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT
from ..models import RegistryFact, utcnow
from .base import RegistryProvider, RegistryResult

DEFAULT_CACHE = REPO_ROOT / "data" / "registry_cache" / "registry_snapshot.json"

# MCA status string -> our canonical kind
MCA_KINDS = {
    "active": "mca_active",
    "under process of striking off": "mca_under_strike_off",
    "strike off": "mca_strike_off",
    "struck off": "mca_strike_off",
    "dissolved": "mca_strike_off",
    "amalgamated": "mca_amalgamated",
    "under liquidation": "mca_under_liquidation",
}

GST_KINDS = {
    "active": "gstin_active",
    "suspended": "gstin_suspended",
    "cancelled": "gstin_cancelled",
    "inactive": "gstin_cancelled",
}


class CachedRegistryProvider(RegistryProvider):
    name = "mca_gst_cached"
    live = False

    def __init__(self, cache_path: str | Path | None = None) -> None:
        self.cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE
        self._cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            if not self.cache_path.exists():
                self._cache = {"mca": {}, "gst": {}, "meta": {}}
            else:
                with self.cache_path.open("r", encoding="utf-8") as fh:
                    self._cache = json.load(fh)
        return self._cache

    def lookup(self, *, cin: str | None = None, gstin: str | None = None,
               name: str | None = None, as_of: str | None = None) -> RegistryResult:
        cache = self._load()
        facts: list[RegistryFact] = []
        missing: list[str] = []

        if cin:
            rec = cache.get("mca", {}).get(cin.strip().upper())
            if rec:
                facts.append(self._mca_fact(cin.strip().upper(), rec))
            else:
                missing.append(f"CIN {cin} not in the MCA cache")
        if gstin:
            rec = cache.get("gst", {}).get(gstin.strip().upper())
            if rec:
                facts.append(self._gst_fact(gstin.strip().upper(), rec))
            else:
                missing.append(f"GSTIN {gstin} not in the GST cache")

        if not cin and not gstin:
            return RegistryResult(ok=False, provider=self.name,
                                  reason="vendor record carries neither CIN nor GSTIN")
        if not facts:
            return RegistryResult(
                ok=False, provider=self.name,
                reason=("; ".join(missing) +
                        " — no registry observation available. This is NOT a clean "
                        "status. Production path: licensed MCA/GST provider."),
            )
        # Partial hits are fine, but we say what we missed.
        return RegistryResult(ok=True, provider=self.name, facts=facts,
                              reason="; ".join(missing) or None)

    # ------------------------------------------------------------------ #

    def _mca_fact(self, cin: str, rec: dict[str, Any]) -> RegistryFact:
        status = str(rec.get("company_status", "UNKNOWN"))
        kind = MCA_KINDS.get(status.strip().lower(), "mca_other")
        return RegistryFact(
            provider="mca21", kind=kind, status=status.upper(),
            as_of=str(rec.get("as_of", "unknown")),
            source_url=str(rec.get("source_url",
                                   "https://www.mca.gov.in/mcafoportal/viewCompanyMasterData.do")),
            fetched_at=utcnow(), live=False,
            detail={
                "cin": cin,
                "last_bs_filing_date": rec.get("last_bs_filing_date"),
                "last_agm_date": rec.get("last_agm_date"),
                "directors": rec.get("directors", []),
                "open_charges": rec.get("open_charges"),
                "registered_address": rec.get("registered_address"),
                "provenance": rec.get("provenance", "cached_snapshot"),
                "_caveat": "cached snapshot, not a live lookup",
            },
        )

    def _gst_fact(self, gstin: str, rec: dict[str, Any]) -> RegistryFact:
        status = str(rec.get("gstin_status", "UNKNOWN"))
        kind = GST_KINDS.get(status.strip().lower(), "gstin_other")
        return RegistryFact(
            provider="gst", kind=kind, status=status.upper(),
            as_of=str(rec.get("as_of", "unknown")),
            source_url=str(rec.get("source_url", "https://services.gst.gov.in/services/searchtp")),
            fetched_at=utcnow(), live=False,
            detail={
                "gstin": gstin,
                "gstr3b_filed_months": rec.get("gstr3b_filed_months"),
                "gstr3b_gap_months": rec.get("gstr3b_gap_months"),
                "provenance": rec.get("provenance", "cached_snapshot"),
                "_caveat": "cached snapshot, not a live lookup",
            },
        )

    # ------------------------------------------------------------------ #

    def coverage(self) -> dict[str, int]:
        c = self._load()
        return {"mca_records": len(c.get("mca", {})), "gst_records": len(c.get("gst", {}))}
