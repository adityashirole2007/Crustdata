#!/usr/bin/env python3
"""Build a REAL backtest case from a genuine Indian company that entered CIRP.

Why you want to run this before the demo
----------------------------------------
The bundled fixtures are synthetic and labelled as such. They prove the pipeline
works; they prove nothing about real-world accuracy. The single highest-leverage
thing you can do with a spare twenty minutes is replace one of them with a real
case, because "we flagged it N days before the public insolvency notice" is only
worth saying if N is real.

What this does
--------------
1. Hits IBBI live for the CIN you give it and reads the actual announcement date.
2. Enriches the company through Crustdata with every entitled field group.
3. Writes a fixture with "synthetic": false and real source URLs.

The honest limitation, which you should state in the demo rather than hide
-------------------------------------------------------------------------
Crustdata returns a CURRENT snapshot, not a historical time series. One live
enrich gives you one point, dated today. So a real multi-point backtest needs
one of:

  (a) `--from-watcher-history` : if you created a watcher 30+ days ago, its
      change history gives you genuine dated observations. Best option, needs
      foresight you probably did not have this morning.
  (b) `--derive-from-growth`   : reconstruct earlier headcount points from the
      graph's own growth_percent map (mom/qoq/six_months/yoy). This is a real
      reconstruction from real reported growth, not an invention — but it is a
      reconstruction, it is lossy, and the fixture records that in
      `derivation_method` so you can say so out loud.
  (c) one real point only      : honest, and still shows the registry overlay
      and the identity resolution working on a real company.

Option (b) is what most teams should use today. Say the word "reconstructed"
when you present it. A judge who asks "where did the T-180 headcount come from?"
and gets a straight answer scores you higher than one who catches a dodge.

Usage
-----
    python scripts/build_real_case.py \
        --cin U74999MH2015PTC268865 \
        --domain somecompany.com \
        --name "Some Company Pvt Ltd" \
        --derive-from-growth \
        --out data/replay/case_90_real.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prahari.config import load_config                      # noqa: E402
from prahari.crustdata import CrustdataClient               # noqa: E402
from prahari.models import utcnow                           # noqa: E402
from prahari.registry.ibbi import IBBIProvider              # noqa: E402

OFFSETS = [180, 150, 120, 90, 60, 30, 0]


def derive_headcount_series(total: int, growth: dict[str, float]) -> dict[int, int]:
    """Reconstruct earlier headcount points from the reported growth map.

    growth keys are mom / qoq / six_months / yoy, each a percent change ENDING at
    the current total. So the value six months ago is total / (1 + six_months/100).
    Intermediate points are interpolated geometrically between the anchors we have.
    This is arithmetic on reported figures, not a guess at the figures themselves —
    but it is still a reconstruction and the fixture says so.
    """
    anchors: dict[int, float] = {0: float(total)}
    for days, key in ((30, "mom"), (90, "qoq"), (180, "six_months"), (365, "yoy")):
        g = growth.get(key)
        if g is not None and g != -100:
            anchors[days] = float(total) / (1.0 + g / 100.0)

    known = sorted(anchors)
    out: dict[int, int] = {}
    for off in OFFSETS:
        if off in anchors:
            out[off] = int(round(anchors[off]))
            continue
        lo = max((k for k in known if k <= off), default=known[0])
        hi = min((k for k in known if k >= off), default=known[-1])
        if lo == hi:
            out[off] = int(round(anchors[lo]))
        else:
            frac = (off - lo) / (hi - lo)
            out[off] = int(round(anchors[lo] + (anchors[hi] - anchors[lo]) * frac))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cin", required=True, help="21-character CIN")
    ap.add_argument("--domain", help="company domain")
    ap.add_argument("--name", help="legal name")
    ap.add_argument("--gstin")
    ap.add_argument("--vendor-id", default="V900")
    ap.add_argument("--open-po", type=float, default=0.0)
    ap.add_argument("--sole-source", action="store_true")
    ap.add_argument("--lead-time", type=int, default=0)
    ap.add_argument("--derive-from-growth", action="store_true",
                    help="reconstruct earlier points from the reported growth map")
    ap.add_argument("--out", default="data/replay/case_90_real.json")
    args = ap.parse_args()

    cfg = load_config()

    # ---- 1. the registry event, live ---------------------------------- #
    print(f"[1/3] IBBI live lookup for {args.cin} …")
    reg = IBBIProvider().lookup(cin=args.cin)
    if not reg.ok:
        print(f"      IBBI said: {reg.reason}")
        print("      Refusing to invent a status. Fix the CIN or check the page by hand.")
        return 1
    facts = [f for f in reg.facts]
    event_kind = next((f.kind for f in facts
                       if f.kind in ("ibbi_cirp_admitted", "ibbi_liquidation")), None)
    if not event_kind:
        print(f"      No CIRP/liquidation process published for this CIN "
              f"({facts[0].status if facts else 'no facts'}).")
        print("      That makes this a CONTROL case, not a distress case. "
              "Labelling it accordingly.")
    event_date = facts[0].as_of if facts else utcnow()[:10]
    print(f"      -> {facts[0].kind} / {facts[0].status} as of {event_date}")

    # ---- 2. the live enrich -------------------------------------------- #
    print("[2/3] Crustdata enrich …")
    with CrustdataClient(config=cfg) as client:
        if not client.available:
            print("      CRUSTDATA_API_KEY not set.")
            return 1
        client.probe_entitlements()
        if not args.domain and args.name:
            ident = client.identify(name=args.name)
            if ident.ok and ident.data:
                d = ident.data[0] if isinstance(ident.data, list) else ident.data
                args.domain = (d or {}).get("domain")
                print(f"      resolved domain: {args.domain}")
        if not args.domain:
            print("      Could not resolve a domain. Pass --domain.")
            return 1
        res = client.enrich(domains=[args.domain])
        if not res.ok:
            print(f"      enrich failed: {res.unavailable_note}")
            return 1
    payload = res.data[0] if isinstance(res.data, list) and res.data else res.data
    if not isinstance(payload, dict):
        print("      enrich returned no usable payload")
        return 1
    print(f"      sections: {', '.join(sorted(payload.keys()))}")

    # ---- 3. build the fixture ------------------------------------------ #
    print("[3/3] writing fixture …")
    base = date.fromisoformat(event_date[:10])
    snapshots, method = [], "single_live_point"

    if args.derive_from_growth:
        total = (payload.get("headcount") or {}).get("total")
        growth = CrustdataClient.normalise_growth_map(
            (payload.get("headcount") or {}).get("growth_percent")
        )
        if total and growth:
            series = derive_headcount_series(int(total), growth)
            method = "reconstructed_from_reported_growth_map"
            for off in OFFSETS:
                p = json.loads(json.dumps(payload))
                p.setdefault("headcount", {})["total"] = series[off]
                p["_prahari"] = {
                    **(p.get("_prahari") or {}),
                    "_derivation": ("headcount reconstructed from the reported "
                                    "growth map; other fields are the CURRENT "
                                    "snapshot and are NOT historical"),
                }
                snapshots.append({
                    "as_of": (base - timedelta(days=off)).isoformat(),
                    "source": "crustdata_enrich_reconstructed",
                    "fetched_at": res.fetched_at, "payload": p,
                })
        else:
            print("      no headcount growth map — falling back to a single point")

    if not snapshots:
        snapshots = [{
            "as_of": utcnow()[:10], "source": "crustdata_enrich",
            "fetched_at": res.fetched_at, "payload": payload,
        }]

    fixture = {
        "synthetic": False,
        "derivation_method": method,
        "label": "cirp" if event_kind else "control",
        "expected_outcome": ("page before the real IBBI announcement"
                             if event_kind else "no published process — should not page"),
        "notes": (
            "REAL case. The IBBI fact below was fetched live from ibbi.gov.in and the "
            "enrich payload is a real Crustdata response. "
            + ("Earlier headcount points are RECONSTRUCTED from the reported growth map "
               "(mom/qoq/six_months/yoy); all other fields in those snapshots are the "
               "current values and are not historical. State this when presenting the "
               "lead time." if method.startswith("reconstructed")
               else "Only one real observation point is available from a live enrich.")
        ),
        "vendor": {
            "vendor_id": args.vendor_id,
            "name": args.name or (payload.get("basic_info") or {}).get("name") or args.domain,
            "domain": args.domain, "cin": args.cin, "gstin": args.gstin,
            "open_po_value": args.open_po, "sole_source": args.sole_source,
            "lead_time_days": args.lead_time, "critical_component": False,
            "category_manager": None,
        },
        "snapshots": snapshots,
        "registry_facts": [
            {**f.to_dict(), "detail": {**f.detail, "provenance": "LIVE IBBI lookup"}}
            for f in facts
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    print(f"      wrote {out}  ({len(snapshots)} snapshots, method={method})")
    print("\nNow run:  python -m prahari.cli seed --fresh && python -m prahari.cli backtest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
