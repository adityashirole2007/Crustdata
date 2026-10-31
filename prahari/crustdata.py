"""Crustdata API client.

Encodes the behaviours confirmed from the docs that will otherwise burn you:

  * `fields` defaults to basic_info only — every group must be listed explicitly.
  * Request the FULL section name ("locations"), never a subfield, or the
    siblings come back as null placeholders.
  * technographics / social_posts are gated add-ons: a key without entitlement
    gets 403, not an empty payload. We probe once and drop them.
  * Growth maps are keyed mom/qoq/six_months/yoy/two_years on enrich but
    1m/3m/6m/12m on search. We normalise both into the enrich keys.
  * Watcher operators use the REVERSED forms `=>` and `=<`. `>=` is rejected
    with "unknown operator".
  * 429 means slow down, not fail. 15 req/min on enrich.

Failure policy: every method returns a result object carrying `ok`. When a call
fails we surface `data_unavailable` with a reason. We never substitute the
model's memory for a failed lookup.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from .config import get_settings, load_config
from .models import utcnow

log = logging.getLogger("prahari.crustdata")

# Enrich-key <- search-key normalisation for growth maps.
GROWTH_KEY_MAP = {
    "1m": "mom",
    "3m": "qoq",
    "6m": "six_months",
    "12m": "yoy",
    "24m": "two_years",
}


class CrustdataUnavailable(RuntimeError):
    """Raised only where a caller explicitly wants hard failure."""


@dataclass
class ApiResult:
    ok: bool
    data: Any = None
    status_code: int | None = None
    reason: str | None = None
    url: str = ""
    fetched_at: str = field(default_factory=utcnow)

    @property
    def unavailable_note(self) -> str:
        return f"data_unavailable: {self.reason or 'unknown'} (HTTP {self.status_code})"


class _RateLimiter:
    """Simple token-bucket, shared across threads."""

    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / max(per_minute, 1)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self.interval


class CrustdataClient:
    def __init__(self, api_key: str | None = None, config: dict | None = None) -> None:
        settings = get_settings()
        self.cfg = config or load_config()
        self.api_key = api_key or settings.crustdata_api_key
        self.base_url = settings.crustdata_base_url
        self.api_version = settings.crustdata_api_version
        cd = self.cfg.get("crustdata", {})
        self.field_groups: list[str] = list(cd.get("field_groups", ["basic_info"]))
        self.gated_groups: list[str] = list(cd.get("gated_field_groups", []))
        self.max_per_request: int = int(cd.get("max_companies_per_request", 25))
        self._limiter = _RateLimiter(int(cd.get("requests_per_minute", 15)))
        self._entitled_gated: list[str] | None = None
        self._client = httpx.Client(timeout=45.0)

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "x-api-version": self.api_version,
        }

    def _post(self, path: str, payload: dict, retries: int = 3) -> ApiResult:
        url = f"{self.base_url}{path}"
        if not self.api_key:
            return ApiResult(
                ok=False, url=url, status_code=None,
                reason="CRUSTDATA_API_KEY is not set",
            )
        last: ApiResult | None = None
        for attempt in range(retries):
            self._limiter.acquire()
            try:
                r = self._client.post(url, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                last = ApiResult(ok=False, url=url, reason=f"transport error: {exc}")
                time.sleep(1.5 * (attempt + 1))
                continue

            if r.status_code == 429:
                # documented: wait a few seconds and retry
                retry_after = float(r.headers.get("retry-after", 5))
                log.warning("429 from %s — backing off %.1fs", path, retry_after)
                time.sleep(retry_after)
                last = ApiResult(ok=False, url=url, status_code=429, reason="rate limited")
                continue

            if r.status_code == 403:
                return ApiResult(
                    ok=False, url=url, status_code=403,
                    reason="forbidden — key likely lacks entitlement for a requested "
                           "field group (technographics / social_posts are gated add-ons)",
                )
            if r.status_code >= 500:
                last = ApiResult(ok=False, url=url, status_code=r.status_code,
                                 reason="upstream error")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                return ApiResult(ok=False, url=url, status_code=r.status_code,
                                 reason=r.text[:400])
            try:
                return ApiResult(ok=True, data=r.json(), status_code=r.status_code, url=url)
            except ValueError:
                return ApiResult(ok=False, url=url, status_code=r.status_code,
                                 reason="response was not JSON")
        return last or ApiResult(ok=False, url=url, reason="exhausted retries")

    def _get(self, path: str, params: dict | None = None) -> ApiResult:
        url = f"{self.base_url}{path}"
        if not self.api_key:
            return ApiResult(ok=False, url=url, reason="CRUSTDATA_API_KEY is not set")
        self._limiter.acquire()
        try:
            r = self._client.get(url, params=params or {}, headers=self._headers())
        except httpx.HTTPError as exc:
            return ApiResult(ok=False, url=url, reason=f"transport error: {exc}")
        if r.status_code >= 400:
            return ApiResult(ok=False, url=url, status_code=r.status_code,
                             reason=r.text[:400])
        return ApiResult(ok=True, data=r.json(), status_code=r.status_code, url=url)

    # ------------------------------------------------------------------ #
    # entitlement probe — run this FIRST, your whole design depends on it
    # ------------------------------------------------------------------ #

    def probe_entitlements(self, probe_domain: str = "stripe.com") -> dict[str, Any]:
        """Find out which field groups this key is actually entitled to.

        Returns {"base_ok": bool, "gated_entitled": [...], "gated_denied": [...]}.
        Costs ~2 credits per gated probe. Run once at startup; cached in-process.
        """
        report: dict[str, Any] = {
            "base_ok": False, "gated_entitled": [], "gated_denied": [],
            "base_reason": None, "probed_at": utcnow(),
        }
        base = self._post("/company/enrich", {
            "domains": [probe_domain], "fields": self.field_groups, "exact_match": True,
        })
        report["base_ok"] = base.ok
        report["base_reason"] = base.reason
        if base.ok:
            report["sections_returned"] = sorted(
                (base.data[0] if isinstance(base.data, list) and base.data else base.data or {}).keys()
            ) if isinstance(base.data, (list, dict)) else []
        for g in self.gated_groups:
            res = self._post("/company/enrich", {
                "domains": [probe_domain], "fields": [*self.field_groups, g],
                "exact_match": True,
            })
            (report["gated_entitled"] if res.ok else report["gated_denied"]).append(g)
        self._entitled_gated = list(report["gated_entitled"])
        return report

    def effective_fields(self) -> list[str]:
        return [*self.field_groups, *(self._entitled_gated or [])]

    # ------------------------------------------------------------------ #
    # endpoints
    # ------------------------------------------------------------------ #

    def identify(self, name: str | None = None, domain: str | None = None) -> ApiResult:
        """Resolve a free-text name or a domain to one canonical company."""
        payload: dict[str, Any] = {}
        if domain:
            payload["domain"] = domain
        if name:
            payload["query"] = name
        return self._post("/company/identify", payload)

    def enrich(
        self,
        domains: Iterable[str] | None = None,
        company_ids: Iterable[str] | None = None,
        fields: list[str] | None = None,
    ) -> ApiResult:
        """Enrich up to 25 companies. Always sends the full explicit field list."""
        payload: dict[str, Any] = {
            "fields": fields or self.effective_fields(),
            "exact_match": True,
        }
        if domains:
            payload["domains"] = list(domains)[: self.max_per_request]
        if company_ids:
            payload["company_ids"] = list(company_ids)[: self.max_per_request]
        return self._post("/company/enrich", payload)

    def company_search(self, query: dict[str, Any], limit: int = 25) -> ApiResult:
        """Used for address collision + director overlap reverse lookups."""
        return self._post("/company/search", {**query, "limit": limit})

    def person_enrich(
        self, linkedin_urls: list[str] | None = None, names: list[str] | None = None
    ) -> ApiResult:
        payload: dict[str, Any] = {}
        if linkedin_urls:
            payload["linkedin_urls"] = linkedin_urls
        if names:
            payload["names"] = names
        return self._post("/person/enrich", payload)

    def jobs_search(self, company_domain: str, limit: int = 25) -> ApiResult:
        return self._post("/jobs/search", {"company_domain": company_domain, "limit": limit})

    def web_search(self, query: str, limit: int = 8) -> ApiResult:
        return self._post("/web/search", {"query": query, "limit": limit})

    def web_fetch(self, url: str) -> ApiResult:
        return self._post("/web/fetch", {"url": url})

    # ------------------------------------------------------------------ #
    # watcher
    # ------------------------------------------------------------------ #

    def create_company_watch(self, domains: list[str], webhook_url: str) -> ApiResult:
        """Create a company entity watch.

        Note the operator forms: `=>` and `=<`. `>=` returns "unknown operator".
        `track` and `fields` are IMMUTABLE after creation; `entities` is PATCHable.
        Company watches refresh on a 30-day cycle and the baseline run fires on
        nothing — which is exactly why the watcher is our production trigger and
        the snapshot-diff engine is our core.
        """
        payload = {
            "entities": {"domains": domains},
            "track": {
                "op": "or",
                "conditions": [
                    {"field": "headcount.total", "type": "changed"},
                    {"field": "hiring.openings_count", "type": "changed"},
                    {"field": "basic_info.name", "type": "changed"},
                    {"field": "locations.country", "type": "changed"},
                    {"field": "people.cxos", "type": "changed"},
                    {"field": "news", "type": "added", "keys": ["article_url"]},
                ],
            },
            "fields": [
                "basic_info", "headcount", "hiring", "locations",
                "people", "news", "web_traffic", "employee_reviews",
            ],
            "config": {
                "trigger": {"type": "interval", "every_hours": 24},
                "max_results_per_run": 100,
            },
            "notifications": [{"type": "webhook", "url": webhook_url}],
        }
        return self._post("/watch/company", payload)

    def patch_watch_entities(self, watch_id: str, domains: list[str]) -> ApiResult:
        url = f"{self.base_url}/watch/company/{watch_id}"
        if not self.api_key:
            return ApiResult(ok=False, url=url, reason="CRUSTDATA_API_KEY is not set")
        self._limiter.acquire()
        try:
            r = self._client.patch(
                url, json={"entities": {"domains": domains}}, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            return ApiResult(ok=False, url=url, reason=str(exc))
        if r.status_code >= 400:
            return ApiResult(ok=False, url=url, status_code=r.status_code, reason=r.text[:400])
        return ApiResult(ok=True, data=r.json(), status_code=r.status_code, url=url)

    def test_watch(self, watch_id: str) -> ApiResult:
        """POST /watch/company/{id}/test — FREE, persists nothing, and delivers the
        exact production envelope with real HMAC-SHA256 signature headers."""
        return self._post(f"/watch/company/{watch_id}/test", {})

    def credits(self) -> ApiResult:
        return self._get("/account/credits")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def normalise_growth_map(raw: dict[str, Any] | None) -> dict[str, float]:
        """Map search-shaped growth keys (1m/3m/6m/12m) onto enrich-shaped keys."""
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for k, v in raw.items():
            if v is None:
                continue
            key = GROWTH_KEY_MAP.get(str(k).lower(), str(k).lower())
            try:
                out[key] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CrustdataClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
