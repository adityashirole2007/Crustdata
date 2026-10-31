# Prahari (प्रहरी, "sentinel") — Continuous Vendor Risk Agent

**Theme: Trust and Compliance · ContextCon 03, IIT Bombay · 31 October 2026**

> Procurement verifies a vendor **once**, at onboarding. Risk is a **time series**.
> Prahari runs the vendor-risk analyst's whole job continuously — baseline, watch,
> detect, investigate, **disconfirm**, and escalate — and every claim it makes
> carries a citation.

---

## The service

Vendor risk monitoring. Today that job is done by in-house vendor-management
analysts running annual re-KYC, by paid tools (D&B, CRISIL, Karza, Signzy), or —
for most Indian mid-market firms — by nobody at all.

**Who pays:** procurement and vendor-risk teams at mid-to-large Indian
enterprises; supply-chain-finance desks at NBFCs and banks; marketplace
seller-trust teams.

Onboarding diligence is a snapshot. Risk is a time series. We close that gap.

**The thesis, in one line:** salary delay → supplier payment default →
insolvency. All three precursors live in the digital footprint. The registry
only tells you after it is too late to act.

---

## What it does now, end to end

```
procurement ledger (name, domain, CIN, GSTIN, open PO, sole-source, lead time)
   → identity resolution        /company/identify → /company/enrich (exact_match)
   → append-only snapshot store sqlite; never overwritten; this IS the memory
   → deterministic diff engine  plain code, no LLM, typed delta events
   → signal extractors          one pure function per signal, each with evidence
   → explainable scoring        TWO scores: Legitimacy and Distress
   → registry overlay           IBBI live; MCA/GST cached with dates
   → bounded investigation      ≤5 steps, and it must TRY TO DISCONFIRM first
   → citation guard             renderer REFUSES any claim with no evidence
   → Distress × Exposure        priority from the procurement ledger
   → alert + running case file  hysteresis, cooldown, merge — not a new ticket
```

A person is needed for exactly two things: **final hold-payment authorisation**,
and any case the agent marks `confidence: low`.

---

## Five design decisions, and why

### 1. Facts are deterministic. Only investigation and narration are LLM.

The diff and the scoring are plain code in `diff.py` and `scoring.py`. An LLM is
never the thing that decides whether headcount went 120 → 68. Feed the same two
snapshots in twice and you get byte-identical deltas out — there is a test that
asserts exactly that, twenty times in a row.

The model's only jobs are to investigate (bounded, with tools) and to narrate.
It is forbidden by its system prompt from introducing a fact that is not in its
input, and it cannot change a number, a status or a verdict.

### 2. Two scores, never one.

| | **Legitimacy** | **Distress** |
|---|---|---|
| Question | Is this a real trading business? | Will it fail to deliver / fail to get paid? |
| Shape | static but fake | was real, now sinking |
| Signature | nothing is declining, because nothing was ever there | everything is declining |
| Remedy | offboard, report, claw back | payment hold, second-source |
| Cadence | monthly | every run |

Conflating them is the single biggest design mistake available here. A shell
company scores *healthy* on every distress signal — its headcount is not
falling, because it only ever had three people. A single composite number would
tell the category manager to do precisely the wrong thing.

### 3. A hard citation guard, with no escape hatch.

`evidence.CitationGuard.render()` takes claims and raises `CitationError` if any
claim lacks an adequate evidence record. There is no `force=True`. There is no
warn-and-continue. A delta claim ("headcount fell") additionally has to carry a
from/to pair, or it is refused.

Run `make guard` to watch it refuse, or open the **Citation guard** tab in the UI
and press the button. Four tests cover it, including the one that matters: a
single uncited claim kills the *entire* render rather than being quietly dropped
from an otherwise-complete alert.

### 4. Alert fatigue is the real failure mode.

A vendor shedding 3% of headcount every month must not fire twelve times. Three
mechanisms:

- **Hysteresis** — fire on a *band transition* upward, never on a raw value.
  Coming back down requires clearing the band floor by an 8-point buffer, so a
  vendor oscillating on a boundary does not flap.
- **Cooldown** — per `(vendor, signal)` at 21 days, and a hard cap of one page
  per vendor per 7 days.
- **Case merging** — an alert for a vendor with an open case *appends an entry*
  to that case. New tickets are the exception.

`test_a_vendor_shedding_headcount_monthly_does_not_fire_twelve_times` runs that
exact scenario and asserts ≤3 pages. Suppressed alerts are still written to the
case file in full: **silence is not forgetting.**

### 5. Priority = Distress × Exposure.

A distressed vendor you buy ₹5L a year from is a log line. The sole-source
supplier of a critical casting at ₹8cr a year with a 120-day lead time is a
fire. The risk model cannot tell those apart — only the procurement ledger can.
Exposure combines open PO value, the sole-source flag, lead time and
criticality. This is what makes Prahari a procurement product instead of a data
toy.

---

## Validation — time-travel replay

"Continuous monitoring" has no two-minute demo, because nothing changes in two
minutes. So we replay: feed the agent only the data that existed at T-240,
T-210, … T-0, and watch the score climb.

The guardrail that makes this honest lives in the store, not the replay: every
read is `as_of <= cutoff`, so the scorer is *physically unable* to see a
snapshot dated after the evaluation date. Registry facts are replayed from dated
observations rather than fetched live, so a backtest cannot leak the outcome it
is supposed to be predicting. `test_backtest_cannot_see_the_future` asserts it.

```
Cases: 4  (1 distress, 2 control, 1 shell)
Caught before the registry event : 1/1
False positives on controls      : 0/2
Shells caught on legitimacy      : 1/1
Median lead time                 : 150 days
```

The distress case climbs `0 → 30 → 35 → 63 → 65 → 74 → 95`, paging at **T-150**
on payroll-stress reviews plus the CFO departure, 150 days before the day-0
registry event.

### ⚠️ Read this before you quote that number

**The bundled fixtures are SYNTHETIC and labelled as such** — `"synthetic": true`
in every file, and a `provenance` string on every registry fact. They prove the
pipeline, the scoring curve, the hysteresis logic and the disconfirmation step.
They prove **nothing** about real-world accuracy.

The curve *shapes* follow the documented precursor sequence (payroll stress →
finance-leadership exit → hiring stop → headcount decline → adverse news →
registry). The *numbers are invented*.

To produce a real validated lead time, run:

```bash
python scripts/build_real_case.py --cin <REAL_CIN> --domain <domain> --derive-from-growth
```

That hits IBBI live for the genuine announcement date and enriches the company
through Crustdata. Note its honest limitation, which the script prints and the
fixture records: a live enrich returns a *current* snapshot, not a history, so
earlier headcount points are **reconstructed** from the reported growth map
(`mom`/`qoq`/`six_months`/`yoy`). That is arithmetic on reported figures, not
invented figures — but it is a reconstruction, and you should say the word
"reconstructed" out loud when you present it.

Saying "we backtested on real CIRP cases" while showing invented data is the
fastest way to lose the Facts-and-Verification criterion in Q&A.

---

## The hardest case: a true negative

`case_02_confounded_divestiture.json` is a vendor whose headcount collapses 29%,
whose hiring goes to zero, and whose web traffic drops 31% — the full distress
costume. The score crosses into the `watch` band.

Then the investigation finds the demerger in the vendor's own news feed and
**suppresses the alert**. Nobody is paged. The reasoning is written to the case
file:

```
2026-06-17  distress=40.1  band=watch  paged=False
   signals: ['headcount_decline', 'hiring_freeze', 'web_traffic_decline']
   reason : investigation disconfirmed: SUPPRESS — a benign explanation is
            supported: divestiture or spinoff, profile page split by region
```

The investigation agent is required to attempt disconfirmation **before** it
fires, against a fixed list of named confounders:

- headcount drop from a divestiture or spin-off
- professional-network page split by region → phantom headcount collapse
- hiring freeze from a normal budget cycle or post-funding discipline
- a rebrand reading as a name change reading as a shell
- seasonal businesses
- **identity-resolution error — are we watching the wrong "Shree Industries"?**

That last one is checked *first*, because every downstream signal is void if we
resolved the wrong legal entity.

Confounder checks read the news already in the graph payload before spending a
web search — cheaper, more citable, and it means the suppression behaviour works
and is testable with no network at all.

---

## Architecture

```
prahari/
  config.py      settings + config.yaml loading (no magic numbers in code)
  models.py      Evidence, Claim, Delta, SignalResult, Score, RegistryFact, Alert
  crustdata.py   API client: explicit field groups, 429 backoff, gated-add-on probe
  store.py       append-only sqlite; as_of<=cutoff reads are the backtest guardrail
  diff.py        DETERMINISTIC. no LLM. typed deltas, keyed arrays
  signals.py     one pure function per signal, each carrying its own evidence
  scoring.py     Legitimacy + Distress, weighted, explainable, registry-overridable
  exposure.py    Distress × Exposure from the procurement ledger
  evidence.py    the citation guard
  investigate.py bounded agent + mandatory disconfirmation + optional LLM narration
  alerts.py      hysteresis, cooldown, case-file merging, drafted memo
  registry/      base.py (interface) · ibbi.py (LIVE) · cached.py (MCA/GST, dated)
  replay.py      time-travel backtest
  webhook.py     HMAC-SHA256 verified Crustdata watcher receiver
  pipeline.py    the end-to-end run
  cli.py         typer CLI
app/streamlit_app.py    one-page operator console
```

---

## Where it calls Crustdata

`/company/identify` · `/company/enrich` · `/company/search` (address and director
collision) · `/person/enrich` (departed executives) · `/jobs/search` ·
`/web/search` · `/web/fetch` · `/watch/company` + a signed webhook receiver ·
`/account/credits`

### Things the docs tell you that will otherwise cost you an hour

- `/company/enrich` **defaults to `basic_info` only.** Every group must be listed
  explicitly. Request the full section name (`"locations"`), never a subfield —
  subfields return their siblings as `null` placeholders.
- `technographics` and `social_posts` are **gated add-ons**. A key without
  entitlement gets **403, not an empty payload**. `prahari doctor` probes for
  this and writes the result to `data/entitlements.json`. **Run it first — your
  whole design depends on which groups you actually have.**
- Growth maps are keyed `mom/qoq/six_months/yoy/two_years` on enrich but
  `1m/3m/6m/12m` on search. `CrustdataClient.normalise_growth_map` maps them.
- Watcher operators use the **reversed** forms `=>` and `=<`. `>=` returns
  `unknown operator`.
- `news` is watched with `keys: ["article_url"]` so a syndicated repost does not
  re-fire and re-bill. The diff engine keys arrays the same way.
- 429 means slow down, not fail. The client has a token bucket at 15 req/min.

### Why the watcher is not the core engine

Company entity watches refresh on a **30-day** cycle, and the **baseline run
fires on nothing** (it sends a free sample of up to five companies with
`changes: []`). So you cannot demo a watcher firing on a real change on hackathon
day, and anyone who claims to is confused about what they saw.

Prahari treats the watcher as the **production trigger** and the snapshot-diff
engine as the **core**. What we can prove today is that the receiving half is
real: `POST /watch/company/{id}/test` is free, persists nothing, and delivers the
exact production envelope with genuine HMAC-SHA256 headers
(`x-crustdata-signature: t=<unix>,v1=<hex>`). `webhook.py` verifies that
signature with a constant-time compare and a timestamp tolerance that rejects
replays. Three tests cover good signature, tampered body, and replayed timestamp.

---

## India-specific layer

Crustdata's graph is professional-network-shaped. A ₹20cr injection-moulding shop
in Rajkot is close to invisible in it. **That is a real limitation and we are
saying so plainly** — the India registry layer exists precisely to cover it.

| Source | Status here | Why |
|---|---|---|
| **IBBI CIRP registry** | **LIVE** | Public, free, deterministic, CIN-keyed. Highest-severity binary signal. |
| MCA21 master data | cached, dated | No public API. Captcha'd. ToS forbids scraping. |
| GST (GSTIN status, GSTR-3B regularity) | cached, dated | Captcha'd publicly; paid APIs only. |
| EPFO establishment contributions | roadmap | Payroll distress ~3 months earlier than anything public. |
| MCA charges / CERSAI | roadmap | New charge = fresh secured borrowing. |
| Udyam | roadmap | MSME registration for small vendors. |

**Composite model:** digital footprint is *early and noisy*; the registry is
*late and authoritative*. An alert needs both — the footprint to be early enough
to act on, the registry to make it actionable. Registry facts therefore
**override** the model rather than scoring alongside it.

**We do not fabricate registry data.** Every cached record is stamped with an
`as_of` date and served as `source: registry_cached_snapshot`. The IBBI provider
returns `ok=False` on a fetch failure rather than "no insolvency found", because
*"we could not reach the registry"* and *"this company is clean"* are completely
different statements to put in front of a procurement lead. A cached value with
a date is honest; an invented live lookup is not.

The production path is a licensed reseller (Surepass / Signzy / Karza / Perfios /
InstaFinancials) behind the same `RegistryProvider` interface — a new subclass
and a config line, not a rewrite.

---

## Robustness

- **No Crustdata key** → the whole offline path still runs: `seed`, `backtest`,
  `guard-demo`, `ui`. Live calls report `data_unavailable` with a reason.
- **Key dies mid-run** → the affected signal returns `data_available=False`,
  which is *not* the same as `fired=False`. The score reports its **coverage**,
  and a legitimacy score with under 50% coverage is capped at 70 — an unknown
  vendor is not a clean vendor.
- **No LLM key, or Gemini is down / rate-limited** → the rule-based disconfirmer runs and the alert is narrated
  from its own templates. Nothing goes quiet and nothing is faked.
- **A signal extractor throws** → it is caught, reported as `data_unavailable`
  with the exception, and the run continues.
- **Malformed ledger rows** → skipped with a warning, not a crash.
- **A re-fetch for the same date** → appended, never overwritten, and
  deduplicated by `as_of` before diffing so it cannot create a phantom change.
- **A first-ever snapshot** → emits *no* deltas. Without that, every field reads
  as `None → value` and the vendor appears to have restructured on the day you
  started watching it.

---

## Approaches we tried and rejected

- **LLM-as-differ.** Rejected: non-deterministic on numeric facts. A model that
  occasionally reads 120 → 68 as "roughly flat" destroys the whole evidence chain.
- **A single composite risk score.** Rejected: conflates "fake" with "failing".
  Opposite signatures, opposite remedies. See the table above.
- **The watcher as the core engine.** Rejected: 30-day refresh, baseline fires on
  nothing. It is the production trigger; the snapshot-diff engine is the core.
- **Scraping MCA/GST live.** Rejected: captcha'd, and the ToS forbids it. Would
  have burned 90 minutes and produced a liability.
- **Web search before checking our own data** in the disconfirmation step.
  Rejected: slower, costs credits, and breaks offline. We check the news already
  in the graph payload first.

---

## Known gaps / what's next

- Indian SME coverage in the canonical graph is thin below ~50 employees.
- **EPFO establishment-wise contribution data** — payroll distress roughly three
  months earlier than anything else public. The single highest-value thing to
  build next.
- MCA charges / CERSAI; GSTR-3B filing-gap detection against a licensed feed.
- Seasonality disconfirmation needs 13+ months of history; with fewer snapshots
  it reports `data_unavailable` rather than guessing.
- Address-collision and director-overlap extractors read from
  `_prahari.address_collisions` / `_prahari.director_overlap`, which the
  investigation layer writes back. The reverse-lookup sweep that populates them
  at scale is not wired into the batch run yet.
- ERP write-back (SAP Ariba, Coupa, Zoho) so a hold actually blocks the PO.

---

## Run it

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # paste CRUSTDATA_API_KEY and GEMINI_API_KEY into .env

make doctor                     # auth + entitlement probe — DO THIS FIRST
make seed                       # ledger + replay fixtures (works with no key)
make backtest                   # time-travel replay + validation table
make guard                      # watch the citation guard refuse
make run                        # the priority queue
make ui                         # one-page operator console
make test                       # 28 tests
```

Everything except `doctor`, `baseline` and `run --live-registry` works with no
API key at all.

### The webhook path

```bash
uvicorn prahari.webhook:app --port 8000     # terminal 1
# expose it: ngrok http 8000 / cloudflared tunnel --url http://localhost:8000
python -m prahari.cli watch-create https://<tunnel>/webhooks/crustdata
python -m prahari.cli watch-test <watch_id>     # free, real HMAC headers
```

`GET /webhooks/received` shows the verified envelopes; the **System** tab in the
UI renders them.

### Model

Narration runs on **Google Gemini** (`gemini-3.5-flash-lite` by default,
`temperature: 0`) via the REST `generateContent` endpoint with plain `httpx` — no
extra SDK. If the configured model is refused, it tries current fallbacks in order
(`gemini-3.8-flash`, `gemini-3.6-flash`, …). The model only rephrases a verdict
that code already made; if every call fails, the rule-based text is used.
`python -m prahari.cli doctor` pings Gemini and prints which model answered.

### Requirements

Python 3.10+. SQLite (stdlib). No other services. Gemini key optional.
