# Two-minute Loom script

Record in a private window. Check it plays before you submit.

Have three terminals ready and warm:
1. `make ui` (Streamlit, already open on the Queue tab)
2. a shell at the repo root
3. `uvicorn prahari.webhook:app --port 8000` (only if you wired the tunnel)

Run `make seed && make backtest` **before** you hit record so the report is
cached and nothing takes 20 seconds on camera.

---

| Time | Beat | What's on screen | What you say |
|---|---|---|---|
| 0:00–0:10 | **The one sentence** | Queue tab, 4 vendors | "Procurement verifies a vendor once, at onboarding. Risk is continuous. This is the agent that closes the gap." |
| 0:10–0:30 | **The ledger goes in** | Queue tab — point at the Priority and Exposure columns | "Ten real vendor domains and CINs in. It resolves identities, baselines them, and ranks by Distress times Exposure — open PO value, sole-source flag, lead time. A distressed vendor you buy five lakh a year from is a log line. The sole-source supplier of a critical casting is a fire." |
| 0:30–1:15 | **The replay** ⭐ | Replay tab → expand the CIRP case → the climbing chart | "Continuous monitoring has no two-minute demo, so we backtest. Feed it only what was knowable on each date — the store physically cannot read a snapshot dated after the evaluation date. Score climbs: payroll-delay reviews first, then the CFO leaves, then hiring stops, then headcount falls. It pages **150 days** before the registry event." **Then, out loud:** "These fixtures are synthetic and labelled synthetic — they prove the pipeline, not real-world accuracy. There's a script in the repo that builds a real case off a live IBBI lookup." |
| 1:15–1:40 | **The alert** | Case file tab → scroll: contribution table → evidence → memo | "Every claim is clickable to its source — the field path, the from and to values, the fetch timestamp. The score is fully decomposed; nothing is hidden in a model. And it drafts the hold-payment memo to the category manager." |
| 1:40–1:55 | **The true negative** ⭐ | Replay tab → the confounded case | "This vendor's headcount collapsed 29%, hiring went to zero, traffic down 31%. It crosses the band. Then the investigation finds the demerger and **suppresses it**. Nobody gets paged. The agent has to try to disconfirm before it fires." |
| 1:55–2:00 | **Robustness** | Guard tab → press the button | "And the renderer structurally cannot emit an uncited sentence. There's no force flag." |

---

## If you have an extra 20 seconds

The alert-fatigue point is the one almost nobody else will have built, and it is
the difference between a demo and a product:

> "A vendor shedding 3% of headcount every month must not fire twelve times.
> Hysteresis bands, per-signal cooldowns, and alerts that merge into a running
> case file instead of opening a new ticket. There's a test that runs that exact
> twelve-month decline and asserts at most three pages."

---

## Q&A — the questions you will actually get

**"Where did the T-180 headcount come from?"**
On the bundled fixtures: it's synthetic, and the file says so. On a real case
built with `build_real_case.py --derive-from-growth`: it's reconstructed from
the growth map Crustdata itself reports — mom, qoq, six_months, yoy — which is
arithmetic on reported figures, not invented figures. Say "reconstructed".

**"Is the MCA status live?"**
No. MCA21 has no public API, it's captcha'd, and the ToS forbids scraping. It's
a cached snapshot with an explicit `as_of` date, served as
`registry_cached_snapshot`. IBBI *is* live — public, free, CIN-keyed. The
production path is a licensed reseller behind the same interface. We don't
fabricate registry data.

**"What stops the LLM hallucinating a number?"**
It never sees the job. The diff and the scoring are plain code; there's a test
that runs the same two payloads through the differ twenty times and asserts
byte-identical output. The model investigates and narrates, and its system
prompt forbids introducing any fact not in its input.

**"What if Crustdata is down?"**
The signal returns `data_available=False`, which is not the same as
`fired=False`. The score reports its coverage, and a legitimacy score under 50%
coverage is capped at 70 — an unknown vendor is not a clean vendor. Nothing is
answered from the model's memory.

**"Why two scores?"**
Because a shell company is *healthy* on every distress signal. Its headcount
isn't falling — it only ever had three people. Static-but-fake and
was-real-now-sinking have opposite signatures and opposite remedies. One
composite number tells the category manager to do the wrong thing.

**"Did you demo the watcher firing?"**
No, and nobody can today — company watches refresh on a 30-day cycle and the
baseline run fires on nothing. We wired the watcher as the production trigger
and proved the receiving half with `/test`, which is free and sends real
HMAC-signed envelopes. The receiver verifies the signature with a constant-time
compare and rejects replayed timestamps.
