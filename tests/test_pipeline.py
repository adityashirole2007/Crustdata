"""Tests for the parts that must not be wrong.

The theme of this file: the deterministic core is the product's credibility, so
it gets tested like it. The LLM layer is not tested here because it is not
allowed to affect any fact.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from prahari.alerts import apply_hysteresis, effective_band
from prahari.config import load_config
from prahari.diff import diff_payloads
from prahari.evidence import CitationError, CitationGuard
from prahari.exposure import exposure_factor, priority
from prahari.models import Claim, DeltaKind, Evidence, Snapshot, Vendor
from prahari.registry.cached import CachedRegistryProvider
from prahari.registry.ibbi import IBBIProvider
from prahari.scoring import score_distress, score_legitimacy
from prahari.signals import run_all
from prahari.store import Store
from prahari.webhook import verify_signature


@pytest.fixture()
def cfg():
    return load_config()


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "test.db")


# =========================================================================== #
# diff engine — determinism is the whole claim
# =========================================================================== #

def test_diff_is_deterministic():
    a = {"headcount": {"total": 120}, "news": [{"article_url": "u1", "title": "x"}]}
    b = {"headcount": {"total": 68}, "news": [{"article_url": "u2", "title": "y"}]}
    first = [d.to_dict() for d in diff_payloads(a, b, "2026-01-01", "2026-02-01")]
    for _ in range(20):
        assert [d.to_dict() for d in diff_payloads(a, b, "2026-01-01", "2026-02-01")] == first


def test_diff_detects_numeric_change_with_pct():
    d = diff_payloads({"headcount": {"total": 120}}, {"headcount": {"total": 68}})
    hc = [x for x in d if x.field_path == "headcount.total"]
    assert len(hc) == 1
    assert hc[0].from_value == 120 and hc[0].to_value == 68
    assert hc[0].pct_change == pytest.approx(-43.33, abs=0.1)


def test_baseline_snapshot_emits_no_deltas():
    """A first observation is not a change — otherwise every vendor 'restructures'
    on the day we start watching it."""
    assert diff_payloads(None, {"basic_info": {"name": "X"}, "headcount": {"total": 10}}) == []


def test_news_keyed_on_article_url_so_reposts_do_not_refire():
    a = {"news": [{"article_url": "https://x/1", "title": "Original"}]}
    b = {"news": [{"article_url": "https://x/1", "title": "Original — syndicated repost"}]}
    added = [d for d in diff_payloads(a, b) if d.kind == DeltaKind.ARRAY_ADDED]
    assert added == []


def test_array_reorder_is_not_a_change():
    p = [{"name": "A", "title": "CFO"}, {"name": "B", "title": "CEO"}]
    a = {"people": {"cxos": p}}
    b = {"people": {"cxos": list(reversed(p))}}
    assert [d for d in diff_payloads(a, b) if d.field_path == "people.cxos"] == []


def test_cxo_departure_is_detected_as_removal():
    a = {"people": {"cxos": [{"name": "S. Iyer", "title": "Chief Financial Officer"},
                             {"name": "A. D", "title": "Chief Executive Officer"}]}}
    b = {"people": {"cxos": [{"name": "A. D", "title": "Chief Executive Officer"}]}}
    removed = [d for d in diff_payloads(a, b) if d.kind == DeltaKind.ARRAY_REMOVED]
    assert len(removed) == 1
    assert removed[0].from_value["name"] == "S. Iyer"


# =========================================================================== #
# citation guard — must refuse, with no escape hatch
# =========================================================================== #

def test_guard_accepts_a_cited_claim(cfg):
    g = CitationGuard(cfg)
    c = Claim("Headcount fell 214 -> 139.", "numeric_change",
              (Evidence("crustdata_enrich", "https://api.crustdata.com/company/enrich",
                        "2026-08-01T06:00:00+00:00", "headcount.total",
                        from_value=214, to_value=139),))
    assert "headcount.total" in g.render([c])


def test_guard_refuses_an_uncited_claim(cfg):
    g = CitationGuard(cfg)
    with pytest.raises(CitationError):
        g.render([Claim("They are going under.", "judgement")])


def test_guard_refuses_evidence_missing_a_required_field(cfg):
    g = CitationGuard(cfg)
    c = Claim("Status changed.", "status_change",
              (Evidence("ibbi_live", "", "2026-01-01T00:00:00+00:00", "ibbi.status",
                        to_value="CIRP"),))        # empty source_url
    with pytest.raises(CitationError):
        g.render([c])


def test_guard_refuses_a_delta_claim_with_no_from_to(cfg):
    g = CitationGuard(cfg)
    c = Claim("Headcount changed.", "numeric_change",
              (Evidence("crustdata_enrich", "https://x", "2026-01-01T00:00:00+00:00",
                        "headcount.total"),))
    with pytest.raises(CitationError):
        g.render([c])


def test_guard_failure_renders_nothing(cfg):
    """Partial output would be worse than none — one uncited claim kills the whole
    render, it does not get silently dropped from an otherwise-complete alert."""
    g = CitationGuard(cfg)
    ok = Claim("Cited.", "observation",
               (Evidence("crustdata_enrich", "https://x", "2026-01-01T00:00:00+00:00",
                         "headcount.total", to_value=1),))
    with pytest.raises(CitationError) as exc:
        g.render([ok, Claim("Uncited.", "observation")])
    assert "Nothing was rendered" in str(exc.value)


# =========================================================================== #
# scoring — the two archetypes must stay apart
# =========================================================================== #

def _snap(as_of, payload):
    return Snapshot("V1", as_of, payload, "test", as_of + "T00:00:00+00:00")


def test_shell_hits_legitimacy_not_distress(cfg):
    """A shell is static-but-fake: nothing is declining, so distress must stay low
    while legitimacy collapses. Conflating them is the #1 design mistake."""
    shell = {
        "basic_info": {"name": "X", "year_founded": 1998, "website": "x.test"},
        "headcount": {"total": 3, "growth_percent": {"mom": 0, "qoq": 0, "yoy": 0}},
        "hiring": {"openings_count": 0}, "web_traffic": {"monthly_visits": 90},
        "followers": {"count": 8}, "employee_reviews": [], "news": [],
        "_prahari": {"first_footprint_year": 2025,
                     "address_collisions": {"addr": 9},
                     "director_overlap": {"K. Jain": 6}},
    }
    v = Vendor("V1", "X", "x.test", open_po_value_inr=46_000_000)
    hist = [_snap("2026-01-01", shell), _snap("2026-02-01", shell), _snap("2026-03-01", shell)]
    sig = run_all(hist, diff_payloads(shell, shell), v, cfg)
    assert score_legitimacy(sig, cfg).value < 40
    assert score_distress(sig, cfg).value < 25


def test_thin_coverage_cannot_produce_a_clean_bill_of_health(cfg):
    v = Vendor("V1", "X", "x.test")
    hist = [_snap("2026-01-01", {"basic_info": {"name": "X"}})]
    sig = run_all(hist, [], v, cfg)
    s = score_legitimacy(sig, cfg)
    assert s.coverage < 0.5
    assert s.value <= 70, "an unknown vendor is not a clean vendor"


def test_registry_override_beats_the_model(cfg):
    from prahari.models import RegistryFact
    v = Vendor("V1", "X", "x.test")
    sig = run_all([_snap("2026-01-01", {"headcount": {"total": 400}})], [], v, cfg)
    fact = RegistryFact("ibbi", "ibbi_cirp_admitted", "CIRP_ADMITTED", "2026-01-01",
                        "https://ibbi.gov.in/x", "2026-01-01T00:00:00+00:00", True)
    s = score_distress(sig, cfg, [fact])
    assert s.value >= 95 and s.band == "critical"
    assert s.overridden_by == "ibbi:ibbi_cirp_admitted"


# =========================================================================== #
# exposure
# =========================================================================== #

def test_sole_source_critical_outranks_a_bigger_but_replaceable_vendor(cfg):
    big = Vendor("A", "Big replaceable", open_po_value_inr=120_000_000,
                 sole_source=False, lead_time_days=10)
    small = Vendor("B", "Small sole-source", open_po_value_inr=8_000_000,
                   sole_source=True, lead_time_days=180, critical_component=True)
    assert exposure_factor(small, cfg) > exposure_factor(big, cfg)


def test_same_distress_different_priority(cfg):
    tiny = Vendor("A", "Tiny", open_po_value_inr=200_000)
    huge = Vendor("B", "Huge", open_po_value_inr=180_000_000, sole_source=True,
                  lead_time_days=150, critical_component=True)
    assert priority(80.0, huge, cfg)[1] > priority(80.0, tiny, cfg)[1]
    assert priority(80.0, tiny, cfg)[0] == "LOG"


# =========================================================================== #
# alert fatigue
# =========================================================================== #

def test_hysteresis_fires_only_on_an_upward_band_transition(store, cfg):
    band, fire, _ = apply_hysteresis("V1", "elevated", 50.0, store, cfg)
    assert fire and band == "elevated"
    store.set_band_state("V1", "elevated", 50.0)
    _, fire2, reason = apply_hysteresis("V1", "elevated", 58.0, store, cfg)
    assert not fire2 and "no band transition" in reason


def test_hysteresis_does_not_flap_on_a_boundary(store, cfg):
    store.set_band_state("V1", "elevated", 46.0)
    band, fire, reason = apply_hysteresis("V1", "watch", 44.0, store, cfg)
    assert band == "elevated" and not fire and "buffer" in reason


def test_a_vendor_shedding_headcount_monthly_does_not_fire_twelve_times(store, cfg):
    """The actual alert-fatigue scenario from the brief."""
    pages = 0
    band_prev = None
    for month, score in enumerate([28, 31, 34, 37, 40, 43, 46, 49, 52, 55, 58, 61]):
        raw = effective_band(score, cfg)
        _, fire, _ = apply_hysteresis("V1", raw, float(score), store, cfg)
        if fire:
            pages += 1
        store.set_band_state("V1", raw, float(score))
        band_prev = raw
    assert pages <= 3, f"{pages} pages for one slow decline is alert fatigue"


# =========================================================================== #
# registry honesty
# =========================================================================== #

def test_ibbi_rejects_a_malformed_cin():
    r = IBBIProvider().lookup(cin="NOT-A-CIN")
    assert not r.ok and "well-formed" in (r.reason or "")


def test_ibbi_without_a_cin_does_not_guess():
    r = IBBIProvider().lookup(cin=None)
    assert not r.ok


def test_cached_registry_returns_not_ok_for_an_unknown_cin(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"mca": {}, "gst": {}}), encoding="utf-8")
    r = CachedRegistryProvider(p).lookup(cin="U29100MH2004PTC148812")
    assert not r.ok
    assert "NOT a clean status" in (r.reason or "")


def test_cached_registry_marks_facts_as_not_live():
    r = CachedRegistryProvider().lookup(cin="U51909MH1998PTC114477")
    if r.ok:
        assert all(not f.live for f in r.facts)
        assert all("cached" in str(f.detail.get("_caveat", "")) for f in r.facts)


# =========================================================================== #
# webhook signature
# =========================================================================== #

def test_webhook_verifies_a_good_signature():
    import hashlib, hmac, time
    secret, body = "s3cr3t", b'{"watch_id":"w1","changes":[]}'
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    ok, _ = verify_signature(body, f"t={ts},v1={sig}", secret)
    assert ok


def test_webhook_rejects_a_tampered_body():
    import hashlib, hmac, time
    secret, body = "s3cr3t", b'{"watch_id":"w1"}'
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    ok, reason = verify_signature(b'{"watch_id":"EVIL"}', f"t={ts},v1={sig}", secret)
    assert not ok and "mismatch" in reason


def test_webhook_rejects_a_replayed_timestamp():
    import hashlib, hmac
    secret, body = "s3cr3t", b"{}"
    old = 1_700_000_000
    sig = hmac.new(secret.encode(), f"{old}.".encode() + body, hashlib.sha256).hexdigest()
    ok, reason = verify_signature(body, f"t={old},v1={sig}", secret, tolerance_seconds=300)
    assert not ok and "replay" in reason


# =========================================================================== #
# store / no-lookahead
# =========================================================================== #

def test_store_is_append_only(store):
    v = Vendor("V1", "X")
    store.upsert_vendor(v)
    store.append_snapshot(_snap("2026-01-01", {"headcount": {"total": 10}}))
    store.append_snapshot(_snap("2026-01-01", {"headcount": {"total": 99}}))
    assert len(store.snapshots("V1")) == 2, "a re-fetch must never overwrite history"


def test_backtest_cannot_see_the_future(store):
    v = Vendor("V1", "X")
    store.upsert_vendor(v)
    for d_, h in [("2026-01-01", 100), ("2026-06-01", 60), ("2026-12-01", 10)]:
        store.append_snapshot(_snap(d_, {"headcount": {"total": h}}))
    visible = store.snapshots("V1", as_of_max="2026-06-01")
    assert [s.as_of for s in visible] == ["2026-01-01", "2026-06-01"]
    assert all(s.as_of <= "2026-06-01" for s in visible)
