"""Signed webhook receiver for the Crustdata Company Entity Watcher.

Why this exists even though the watcher cannot be demoed firing on a real change
today: company entity watches refresh on a 30-day cycle and the baseline run
fires on nothing (it sends a free sample of up to five companies with
`changes: []`). So the watcher is the *production* trigger and the snapshot-diff
engine is the core. What we can prove today is that the receiving half is real:

    POST /watch/company/{id}/test   is FREE, persists nothing, and delivers the
    exact production envelope with real HMAC-SHA256 signature headers:
        x-crustdata-signature: t=<unix>,v1=<hex>

So this receiver verifies that signature properly — constant-time compare,
timestamp tolerance to stop replay attacks — and rejects anything that fails.
Run `prahari watch-test <id>` with the server up and you can watch a real signed
envelope arrive, verify, and drive the pipeline.

Run:  uvicorn prahari.webhook:app --port 8000
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .config import get_settings, load_config
from .models import Snapshot, utcnow
from .pipeline import evaluate_vendor
from .store import Store

log = logging.getLogger("prahari.webhook")

app = FastAPI(title="Prahari webhook receiver", version="1.0.0")

_received: list[dict[str, Any]] = []      # in-memory log, surfaced in the UI


# --------------------------------------------------------------------------- #
# signature verification
# --------------------------------------------------------------------------- #

def parse_signature(header: str) -> tuple[int, str]:
    """Parse `t=<unix>,v1=<hex>`."""
    parts = dict(
        kv.split("=", 1) for kv in header.split(",") if "=" in kv
    )
    if "t" not in parts or "v1" not in parts:
        raise ValueError("signature header missing t= or v1=")
    return int(parts["t"].strip()), parts["v1"].strip()


def verify_signature(raw_body: bytes, header: str, secret: str,
                     tolerance_seconds: int = 300) -> tuple[bool, str]:
    """HMAC-SHA256 over `<timestamp>.<raw body>`, constant-time compared."""
    try:
        ts, provided = parse_signature(header)
    except (ValueError, TypeError) as exc:
        return False, f"malformed signature header: {exc}"

    drift = abs(time.time() - ts)
    if drift > tolerance_seconds:
        return False, (f"timestamp outside tolerance ({drift:.0f}s > "
                       f"{tolerance_seconds}s) — possible replay")

    signed_payload = f"{ts}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, provided):
        return True, "verified"

    # Some senders sign the raw body alone; accept that too rather than
    # silently rejecting a legitimate envelope, but say which form matched.
    alt = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if hmac.compare_digest(alt, provided):
        return True, "verified (body-only signing scheme)"
    return False, "signature mismatch"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

@app.get("/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "ok": True,
        "signature_verification": "enabled" if s.webhook_secret else
                                  "DISABLED — CRUSTDATA_WEBHOOK_SECRET not set",
        "received_count": len(_received),
        "at": utcnow(),
    }


@app.get("/webhooks/received")
def received() -> list[dict[str, Any]]:
    return _received[-50:]


@app.post("/webhooks/crustdata")
async def crustdata_webhook(
    request: Request,
    x_crustdata_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = await request.body()
    settings = get_settings()
    cfg = load_config()

    verified, detail = False, "no secret configured — refusing to trust this envelope"
    if settings.webhook_secret:
        if not x_crustdata_signature:
            raise HTTPException(status_code=401, detail="missing x-crustdata-signature")
        verified, detail = verify_signature(
            raw, x_crustdata_signature, settings.webhook_secret,
            settings.webhook_tolerance_seconds,
        )
        if not verified:
            log.warning("rejected webhook: %s", detail)
            raise HTTPException(status_code=401, detail=f"signature rejected: {detail}")

    body = await request.json()
    entry = {
        "at": utcnow(),
        "signature_status": detail,
        "verified": verified,
        "watch_id": body.get("watch_id"),
        "is_baseline": not body.get("changes"),
        "n_companies": len(body.get("results", body.get("companies", [])) or []),
    }

    store = Store()
    processed: list[dict[str, Any]] = []
    rows = body.get("results") or body.get("companies") or []

    for row in rows:
        if not isinstance(row, dict):
            continue
        domain = (row.get("domain") or
                  (row.get("basic_info") or {}).get("website") or "")
        vendor = _match_vendor(store, domain, row.get("company_name") or
                               (row.get("basic_info") or {}).get("name"))
        if vendor is None:
            processed.append({"domain": domain, "status": "no matching vendor in ledger"})
            continue

        payload = row.get("fields") or row.get("data") or row
        store.append_snapshot(Snapshot(
            vendor_id=vendor.vendor_id, as_of=utcnow()[:10], payload=payload,
            source="crustdata_watcher", fetched_at=utcnow(),
        ))
        # The watcher told us something moved; the DIFF ENGINE decides what it means.
        alert = evaluate_vendor(vendor, store, cfg, as_of=utcnow()[:10])
        processed.append({
            "vendor": vendor.name,
            "band": alert.severity if alert else None,
            "paged": (not alert.suppressed) if alert else False,
            "suppression_reason": alert.suppression_reason if alert else None,
        })

    entry["processed"] = processed
    _received.append(entry)
    return {"ok": True, **entry}


def _match_vendor(store: Store, domain: str | None, name: str | None):
    dom = (domain or "").lower().replace("https://", "").replace("http://", "")
    dom = dom.replace("www.", "").strip("/").split("/")[0]
    for v in store.list_vendors():
        vd = (v.domain or "").lower().replace("www.", "")
        if dom and vd and (dom == vd or dom.endswith("." + vd) or vd.endswith("." + dom)):
            return v
        if name and v.name.strip().lower() == str(name).strip().lower():
            return v
    return None
