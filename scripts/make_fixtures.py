#!/usr/bin/env python3
"""Generate the replay fixtures.

READ THIS BEFORE THE DEMO.

These fixtures are SYNTHETIC and are labelled as such in every file they write
(`"synthetic": true`, and a `provenance` string on every registry fact). They
exist so the pipeline, the scoring curve, the hysteresis logic and the
disconfirmation step can be exercised and tested deterministically without
spending credits or waiting 180 days.

They are NOT evidence that the model works on real Indian vendors, and they must
not be presented as such. Saying "we backtested on real CIRP cases" while
showing invented data is the single fastest way to lose the Facts and
Verification criterion in Q&A, and it would be a lie.

To run a REAL backtest, use scripts/build_real_case.py, which takes a genuine
CIN that entered CIRP, pulls the IBBI announcement date live, enriches the
company through Crustdata, and writes a fixture with `"synthetic": false`.
The curve shapes below are modelled on the documented precursor sequence —
payroll stress, then finance-leadership exit, then hiring stop, then headcount
decline, then adverse news — but the numbers are invented.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "replay"
OUT.mkdir(parents=True, exist_ok=True)

EVENT = date(2026, 10, 15)          # the public registry event, "day 0"
OFFSETS = [240, 210, 180, 150, 120, 90, 60, 30, 0]


def d(days_before: int) -> str:
    return (EVENT - timedelta(days=days_before)).isoformat()


def review(rid: str, title: str, text: str) -> dict:
    return {"review_id": rid, "title": title, "text": text}


def news(url: str, title: str, published: str) -> dict:
    return {"article_url": url, "title": title, "published_at": published}


def person(name: str, title: str) -> dict:
    return {"name": name, "title": title}


def snap(as_of: str, payload: dict) -> dict:
    return {
        "as_of": as_of,
        "source": "replay_fixture_synthetic",
        "fetched_at": as_of + "T06:00:00+00:00",
        "payload": payload,
    }


# =========================================================================== #
# CASE 1 — the catch. A real operating vendor sliding into insolvency.
# Precursor order: payroll stress -> CFO exit -> hiring stop -> headcount
# decline -> adverse news -> registry.
# =========================================================================== #

def case_distress() -> dict:
    base_people = [
        person("A. Deshmukh", "Chief Executive Officer"),
        person("S. Iyer", "Chief Financial Officer"),
        person("R. Kulkarni", "Chief Operating Officer"),
        person("M. Bhatt", "VP Manufacturing"),
    ]
    heads = {240: 268, 210: 272, 180: 265, 150: 251, 120: 236,
             90: 208, 60: 171, 30: 139, 0: 118}
    openings = {240: 14, 210: 16, 180: 11, 150: 9, 120: 4, 90: 0, 60: 0, 30: 0, 0: 0}
    traffic = {240: 42000, 210: 44500, 180: 41000, 150: 38000, 120: 33000,
               90: 27000, 60: 19500, 30: 14000, 0: 11000}

    snapshots = []
    for i, off in enumerate(OFFSETS):
        prev_off = OFFSETS[i - 1] if i else off
        people = list(base_people)
        if off <= 150:
            people = [p for p in people if p["title"] != "Chief Financial Officer"]
        if off <= 60:
            people = [p for p in people if p["title"] != "Chief Operating Officer"]

        reviews = []
        if off <= 180:
            reviews.append(review("r1", "Decent work, poor management",
                                  "Salary delayed by two weeks this month, again."))
        if off <= 120:
            reviews += [
                review("r2", "Leaving soon",
                       "Salary not credited on time for three months. PF not deposited."),
                review("r3", "Avoid",
                       "Full and final pending since I resigned. Reimbursement pending too."),
            ]
        if off <= 60:
            reviews.append(review("r4", "Payroll is broken",
                                  "No salary this month. Notice period dues unpaid."))

        items = []
        if off <= 90:
            items.append(news("https://example-news.test/a/supplier-dues-1",
                              "Vendors allege unpaid dues at auto components maker",
                              d(90)))
        if off <= 30:
            items.append(news("https://example-news.test/a/cheque-bounce-1",
                              "Section 138 cheque bounce complaint filed against supplier",
                              d(30)))
        if off == 0:
            items.append(news("https://example-news.test/a/nclt-admits",
                              "NCLT admits insolvency plea; CIRP commences", d(0)))

        mom = round((heads[off] - heads[prev_off]) / heads[prev_off] * 100, 1) if i else 0.0
        qoq_ref = OFFSETS[max(i - 3, 0)]
        qoq = round((heads[off] - heads[qoq_ref]) / heads[qoq_ref] * 100, 1) if i >= 3 else 0.0
        yoy = round((heads[off] - heads[240]) / heads[240] * 100, 1)

        snapshots.append(snap(d(off), {
            "basic_info": {
                "name": "Shree Precision Components Pvt Ltd",
                "website": "shreeprecision.test",
                "year_founded": 2004,
                "employee_count_range": "201-500",
            },
            "headcount": {"total": heads[off],
                          "growth_percent": {"mom": mom, "qoq": qoq, "yoy": yoy}},
            "hiring": {"openings_count": openings[off]},
            "web_traffic": {"monthly_visits": traffic[off],
                            "growth_percent": {"qoq": round(
                                (traffic[off] - traffic[qoq_ref]) / traffic[qoq_ref] * 100, 1
                            ) if i >= 3 else 0.0}},
            "people": {"cxos": people, "founders": [person("A. Deshmukh", "Founder")],
                       "decision_makers": people},
            "employee_reviews": reviews,
            "news": items,
            "followers": {"count": 3100},
            "locations": {"country": "India", "hq_address": "Plot 42, MIDC Chakan, Pune",
                          "all_office_addresses": ["Plot 42, MIDC Chakan, Pune",
                                                   "Unit 7, Ranjangaon, Pune"]},
            "funding": {"last_fundraise_date": "2021-03-18"},
            "revenue": {"annual_revenue_inr": 1_850_000_000},
            "taxonomy": {"industries": ["Automotive Components"]},
            "_prahari": {"first_footprint_year": 2006},
        }))

    return {
        "synthetic": True,
        "label": "cirp",
        "expected_outcome": ("page well before the day-0 registry event, led by payroll "
                             "stress and the CFO exit"),
        "notes": ("SYNTHETIC fixture. Curve shape follows the documented precursor "
                  "sequence; the numbers are invented. Replace with a real CIN via "
                  "scripts/build_real_case.py before claiming a validated lead time."),
        "vendor": {
            "vendor_id": "V101", "name": "Shree Precision Components Pvt Ltd",
            "domain": "shreeprecision.test", "cin": "U29100MH2004PTC148812",
            "gstin": "27AABCS1429B1ZQ", "open_po_value": 82_000_000,
            "sole_source": True, "lead_time_days": 120, "critical_component": True,
            "category_manager": "Priya Nair",
        },
        "snapshots": snapshots,
        "registry_facts": [{
            "provider": "ibbi", "kind": "ibbi_cirp_admitted", "status": "CIRP_ADMITTED",
            "as_of": d(0), "live": False,
            "source_url": "https://ibbi.gov.in/en/claims/pub-process/U29100MH2004PTC148812",
            "detail": {"provenance": "SYNTHETIC — not a real IBBI announcement"},
        }],
    }


# =========================================================================== #
# CASE 2 — the true negative. Headcount collapses 30%, and it is a divestiture.
# A correctly suppressed alert is a better demo than three catches.
# =========================================================================== #

def case_confounded() -> dict:
    heads = {240: 540, 210: 548, 180: 551, 150: 544, 120: 389,
             90: 381, 60: 386, 30: 392, 0: 398}
    snapshots = []
    for i, off in enumerate(OFFSETS):
        prev_off = OFFSETS[i - 1] if i else off
        items = []
        if off <= 120:
            items.append(news("https://example-news.test/a/demerger",
                              "Company completes demerger of its packaging division "
                              "via slump sale", d(135)))
        mom = round((heads[off] - heads[prev_off]) / heads[prev_off] * 100, 1) if i else 0.0
        qoq_ref = OFFSETS[max(i - 3, 0)]
        qoq = round((heads[off] - heads[qoq_ref]) / heads[qoq_ref] * 100, 1) if i >= 3 else 0.0
        snapshots.append(snap(d(off), {
            "basic_info": {"name": "Konkan Industrial Supplies Ltd",
                           "website": "konkanindustrial.test", "year_founded": 1998,
                           "employee_count_range": "201-500"},
            "headcount": {"total": heads[off],
                          "growth_percent": {"mom": mom, "qoq": qoq,
                                             "yoy": round((heads[off] - heads[240])
                                                          / heads[240] * 100, 1)}},
            # A divestiture plausibly drags hiring and traffic down too — which is
            # exactly why this case is hard: it wears the whole distress costume.
            "hiring": {"openings_count": 9 if off > 120 else 0},
            "web_traffic": {"monthly_visits": 88000 if off > 120 else 61000,
                            "growth_percent": {"qoq": -2.0 if off > 120 else -31.0}},
            "people": {"cxos": [person("V. Rane", "Chief Executive Officer"),
                                person("D. Pai", "Chief Financial Officer")],
                       "founders": [], "decision_makers": []},
            "employee_reviews": [review("k1", "Good place",
                                        "Stable team, salary always on time.")],
            "news": items,
            "followers": {"count": 9400},
            "locations": {"country": "India", "hq_address": "MIDC Ratnagiri",
                          "all_office_addresses": ["MIDC Ratnagiri", "Navi Mumbai",
                                                   "Hyderabad"]},
            "funding": {"last_fundraise_date": "2024-08-01"},
            "revenue": {"annual_revenue_inr": 4_100_000_000},
            "taxonomy": {"industries": ["Industrial Supplies"]},
            "_prahari": {"first_footprint_year": 1999},
        }))
    return {
        "synthetic": True,
        "label": "confounded",
        "expected_outcome": ("headcount falls ~29% and the scorer reacts, but the "
                             "investigation must find the demerger and suppress"),
        "notes": "SYNTHETIC. The hardest case: a divestiture that looks exactly like distress.",
        "vendor": {
            "vendor_id": "V102", "name": "Konkan Industrial Supplies Ltd",
            "domain": "konkanindustrial.test", "cin": "L51900MH1998PLC117432",
            "gstin": "27AAACK5566C1Z8", "open_po_value": 31_000_000,
            "sole_source": False, "lead_time_days": 45, "critical_component": False,
            "category_manager": "Priya Nair",
        },
        "snapshots": snapshots,
        "registry_facts": [],
    }


# =========================================================================== #
# CASE 3 — the control. A healthy vendor that must never page.
# =========================================================================== #

def case_control() -> dict:
    snapshots = []
    for i, off in enumerate(OFFSETS):
        h = 120 + (len(OFFSETS) - i) * 3
        snapshots.append(snap(d(off), {
            "basic_info": {"name": "Deccan Polymers Pvt Ltd",
                           "website": "deccanpolymers.test", "year_founded": 2011,
                           "employee_count_range": "51-200"},
            "headcount": {"total": h, "growth_percent": {"mom": 1.8, "qoq": 5.1, "yoy": 19.0}},
            "hiring": {"openings_count": 6},
            "web_traffic": {"monthly_visits": 25000, "growth_percent": {"qoq": 4.0}},
            "people": {"cxos": [person("N. Shetty", "Chief Executive Officer"),
                                person("G. Rao", "Chief Financial Officer")],
                       "founders": [person("N. Shetty", "Founder")], "decision_makers": []},
            "employee_reviews": [review("c1", "Solid", "Pay is on time, good growth.")],
            "news": [],
            "followers": {"count": 2600},
            "locations": {"country": "India", "hq_address": "Hinjewadi, Pune",
                          "all_office_addresses": ["Hinjewadi, Pune"]},
            "funding": {"last_fundraise_date": "2025-06-10"},
            "revenue": {"annual_revenue_inr": 620_000_000},
            "taxonomy": {"industries": ["Polymers"]},
            "_prahari": {"first_footprint_year": 2012},
        }))
    return {
        "synthetic": True, "label": "control",
        "expected_outcome": "never pages",
        "notes": "SYNTHETIC. Healthy incumbent — the false-positive check.",
        "vendor": {
            "vendor_id": "V103", "name": "Deccan Polymers Pvt Ltd",
            "domain": "deccanpolymers.test", "cin": "U25200MH2011PTC219004",
            "gstin": "27AADCD8812K1ZV", "open_po_value": 14_500_000,
            "sole_source": False, "lead_time_days": 21, "critical_component": False,
            "category_manager": "Arjun Mehta",
        },
        "snapshots": snapshots, "registry_facts": [],
    }


# =========================================================================== #
# CASE 4 — the shell. Legitimacy archetype, not distress. Opposite signature:
# nothing is DECLINING, because there was never anything there.
# =========================================================================== #

def case_shell() -> dict:
    snapshots = []
    for off in OFFSETS:
        snapshots.append(snap(d(off), {
            "basic_info": {"name": "Veridian Global Trading Pvt Ltd",
                           "website": "veridianglobaltrading.test", "year_founded": 1998,
                           "employee_count_range": "1-10"},
            "headcount": {"total": 3, "growth_percent": {"mom": 0.0, "qoq": 0.0, "yoy": 0.0}},
            "hiring": {"openings_count": 0},
            "web_traffic": {"monthly_visits": 110, "growth_percent": {"qoq": 0.0}},
            "people": {"cxos": [person("K. Jain", "Director")],
                       "founders": [person("K. Jain", "Director")],
                       "decision_makers": [person("K. Jain", "Director")]},
            "employee_reviews": [],
            "news": [],
            "followers": {"count": 11},
            "locations": {"country": "India",
                          "hq_address": "Office 3B, Sagar Plaza, Andheri East, Mumbai",
                          "all_office_addresses": ["Office 3B, Sagar Plaza, Andheri East, Mumbai"]},
            "funding": {},
            "revenue": {},
            "taxonomy": {"industries": ["Trading"]},
            "_prahari": {
                "first_footprint_year": 2025,
                "address_collisions": {"Office 3B, Sagar Plaza, Andheri East, Mumbai": 9},
                "director_overlap": {"K. Jain": 6},
            },
        }))
    return {
        "synthetic": True, "label": "shell",
        "expected_outcome": ("legitimacy score collapses to the critical band; the vendor "
                             "is escalated as a SHELL, not as a distressed incumbent — "
                             "opposite signature, opposite remedy"),
        "notes": ("SYNTHETIC. Shell signature: Rs 4.6cr of open POs against 3 staff, never "
                  "hires, no traffic, 9 entities at one address, one director across 6 "
                  "entities, 'since 1998' with a 2025 footprint. Nothing is declining — "
                  "which is exactly why a single composite risk score would miss it."),
        "vendor": {
            "vendor_id": "V104", "name": "Veridian Global Trading Pvt Ltd",
            "domain": "veridianglobaltrading.test", "cin": "U51909MH1998PTC114477",
            "gstin": "27AAFCV3321H1ZD", "open_po_value": 46_000_000,
            "sole_source": False, "lead_time_days": 30, "critical_component": False,
            "category_manager": "Arjun Mehta",
        },
        "snapshots": snapshots,
        "registry_facts": [{
            "provider": "mca21", "kind": "mca_under_strike_off",
            "status": "UNDER PROCESS OF STRIKING OFF", "as_of": d(30), "live": False,
            "source_url": "https://www.mca.gov.in/mcafoportal/viewCompanyMasterData.do",
            "detail": {"provenance": "SYNTHETIC — cached-snapshot shape, invented value"},
        }],
    }


def main() -> None:
    cases = {
        "case_01_distress_cirp.json": case_distress(),
        "case_02_confounded_divestiture.json": case_confounded(),
        "case_03_control_healthy.json": case_control(),
        "case_04_shell_legitimacy.json": case_shell(),
    }
    for fname, data in cases.items():
        (OUT / fname).write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"wrote {OUT / fname}  ({len(data['snapshots'])} snapshots)")


if __name__ == "__main__":
    main()
