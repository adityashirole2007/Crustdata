"""Prahari CLI.

    prahari doctor                  # auth + entitlement probe. RUN THIS FIRST.
    prahari seed --fresh            # load the ledger + replay fixtures
    prahari baseline                # identity resolution + first snapshots (live)
    prahari run                     # score everything, emit the alert queue
    prahari backtest                # time-travel replay + validation table
    prahari show <vendor_id>        # full case file for one vendor
    prahari guard-demo              # prove the citation guard refuses uncited claims
    prahari watch-create            # create the Crustdata company watch
    prahari watch-test <watch_id>   # FREE signed test envelope -> your receiver
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import REPO_ROOT, get_settings, load_config
from .crustdata import CrustdataClient
from .evidence import CitationError, CitationGuard
from .models import Claim, Evidence
from .pipeline import evaluate_vendor, load_ledger, resolve_and_baseline, run_all_vendors
from .replay import format_report, load_all_fixtures, run_backtest
from .scoring import contribution_table
from .store import Store

app = typer.Typer(add_completion=False, help="Prahari — Continuous Vendor Risk Agent")
console = Console()


def _store() -> Store:
    return Store()


# --------------------------------------------------------------------------- #

@app.command()
def doctor(probe_domain: str = typer.Option("stripe.com", help="domain to probe with")) -> None:
    """Auth check + field-group entitlement probe. Your whole design depends on this."""
    s = get_settings()
    cfg = load_config()
    console.print(Panel.fit("[bold]Prahari doctor[/bold]", border_style="cyan"))

    t = Table(show_header=False, box=None)
    t.add_row("Crustdata key", "[green]set[/green]" if s.has_crustdata else "[red]MISSING[/red]")
    t.add_row("Gemini key",
              "[green]set[/green]" if s.has_llm else "[yellow]absent — rule-based only[/yellow]")
    t.add_row("Webhook secret",
              "[green]set[/green]" if s.webhook_secret else "[yellow]absent[/yellow]")
    t.add_row("Database", str(s.db_path))
    t.add_row("API version", s.crustdata_api_version)
    console.print(t)

    if s.has_llm:
        from .investigate import llm_chat, working_model
        try:
            out = llm_chat("Reply with exactly the word OK.", "ping", max_tokens=256)
            console.print(f"Gemini ({working_model()}): [green]ok[/green] -> {out.strip()[:40]!r}")
        except Exception as exc:
            console.print(f"Gemini: [red]FAILED[/red] {str(exc)[:600]}\n"
                          "Narration will fall back to rule-based text. Check GEMINI_API_KEY "
                          "or set PRAHARI_LLM_MODEL in .env.")

    if not s.has_crustdata:
        console.print("\n[red]No CRUSTDATA_API_KEY. Put it in .env and re-run.[/red]")
        console.print("Everything offline still works: [bold]seed[/bold], [bold]backtest[/bold], "
                      "[bold]guard-demo[/bold], [bold]ui[/bold].")
        raise typer.Exit(code=1)

    with CrustdataClient(config=cfg) as client:
        cr = client.credits()
        console.print(f"\nCredits endpoint: "
                      f"{'[green]ok[/green]' if cr.ok else '[red]' + str(cr.reason) + '[/red]'}")
        if cr.ok:
            console.print(json.dumps(cr.data, indent=2)[:400])

        console.print("\n[bold]Probing field-group entitlements…[/bold] "
                      "(a few credits; worth every one)")
        rep = client.probe_entitlements(probe_domain)
        console.print(f"  base groups      : "
                      f"{'[green]OK[/green]' if rep['base_ok'] else '[red]FAILED[/red]'}"
                      f"{'' if rep['base_ok'] else ' — ' + str(rep['base_reason'])}")
        if rep.get("sections_returned"):
            console.print(f"  sections returned: {', '.join(rep['sections_returned'])}")
        console.print(f"  gated entitled   : {rep['gated_entitled'] or '[dim]none[/dim]'}")
        console.print(f"  gated denied     : {rep['gated_denied'] or '[dim]none[/dim]'} "
                      f"[dim](403 = not entitled; expected)[/dim]")

        out = REPO_ROOT / "data" / "entitlements.json"
        out.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        console.print(f"\nWritten to {out}")


@app.command()
def seed(
    ledger: str = typer.Option("data/vendors.csv", help="procurement ledger CSV"),
    fresh: bool = typer.Option(False, "--fresh", help="wipe the database first"),
    fixtures: bool = typer.Option(True, help="also load the replay fixtures"),
) -> None:
    """Load the procurement ledger and the replay fixtures. No network needed."""
    store = _store()
    if fresh:
        store.reset()
        console.print("[yellow]database reset[/yellow]")
    vendors = load_ledger(ledger)
    for v in vendors:
        store.upsert_vendor(v)
    console.print(f"[green]{len(vendors)}[/green] vendors loaded from the ledger")
    if fixtures:
        loaded = load_all_fixtures(store)
        console.print(f"[green]{len(loaded)}[/green] replay fixtures loaded "
                      f"({sum(len(fx['snapshots']) for _, fx in loaded)} dated snapshots)")


@app.command()
def baseline(
    ledger: str = typer.Option("data/vendors.csv"),
    as_of: Optional[str] = typer.Option(None, help="YYYY-MM-DD, defaults to today"),
) -> None:
    """Resolve identities and write the first live snapshots. Needs a Crustdata key."""
    cfg = load_config()
    store = _store()
    vendors = load_ledger(ledger)
    with CrustdataClient(config=cfg) as client:
        if not client.available:
            console.print("[red]CRUSTDATA_API_KEY not set — nothing to baseline.[/red]")
            raise typer.Exit(code=1)
        client.probe_entitlements()
        rep = resolve_and_baseline(vendors, store, client, as_of, cfg)
    console.print(f"resolved [green]{rep.resolved}[/green], "
                  f"snapshots [green]{rep.snapshots_written}[/green]")
    for f in rep.failed:
        console.print(f"  [yellow]{f}[/yellow]")


@app.command()
def run(
    as_of: Optional[str] = typer.Option(None, help="evaluate as of this date"),
    live_registry: bool = typer.Option(True, help="hit IBBI live"),
    show_suppressed: bool = typer.Option(True, help="include suppressed alerts"),
    json_out: Optional[str] = typer.Option(None, help="write the queue to a JSON file"),
) -> None:
    """Score every vendor and print the priority queue."""
    cfg = load_config()
    store = _store()
    client = CrustdataClient(config=cfg) if get_settings().has_crustdata else None
    alerts = run_all_vendors(store, cfg, as_of, client, live_registry)
    if client:
        client.close()

    if not alerts:
        console.print("[yellow]No vendors with snapshots. Run `prahari seed` first.[/yellow]")
        raise typer.Exit()

    t = Table(title="Alert queue — ranked by Distress x Exposure")
    for col, j in (("Vendor", "left"), ("Distress", "right"), ("Legit", "right"),
                   ("Band", "left"), ("Prio", "left"), ("Score", "right"),
                   ("Paged", "center"), ("Why not", "left")):
        t.add_column(col, justify=j)                      # type: ignore[arg-type]
    for a in alerts:
        if a.suppressed and not show_suppressed:
            continue
        colour = {"critical": "red", "high": "orange3",
                  "elevated": "yellow", "watch": "cyan"}.get(a.severity, "white")
        t.add_row(
            a.vendor_name[:30],
            f"[{colour}]{a.distress.value:.0f}[/{colour}]" if a.distress else "—",
            f"{a.legitimacy.value:.0f}" if a.legitimacy else "—",
            f"[{colour}]{a.severity}[/{colour}]",
            a.priority, f"{a.priority_score:.0f}",
            "[green]YES[/green]" if not a.suppressed else "[dim]no[/dim]",
            (a.suppression_reason or "")[:52],
        )
    console.print(t)

    paged = [a for a in alerts if not a.suppressed]
    console.print(f"\n[bold]{len(paged)}[/bold] of {len(alerts)} vendors paged a human. "
                  f"The rest were logged to their case files.")
    if json_out:
        Path(json_out).write_text(
            json.dumps([a.to_dict() for a in alerts], indent=2, default=str), encoding="utf-8"
        )
        console.print(f"queue written to {json_out}")


@app.command()
def show(vendor_id: str, as_of: Optional[str] = typer.Option(None)) -> None:
    """Full case file for one vendor: signals, contributions, evidence, memo."""
    cfg = load_config()
    store = _store()
    v = store.get_vendor(vendor_id)
    if not v:
        console.print(f"[red]no vendor {vendor_id}[/red]")
        raise typer.Exit(code=1)
    client = CrustdataClient(config=cfg) if get_settings().has_crustdata else None
    alert = evaluate_vendor(v, store, cfg, as_of, client, live_registry=bool(client))
    if client:
        client.close()
    if not alert:
        console.print("[yellow]no snapshots for this vendor[/yellow]")
        raise typer.Exit()

    console.print(Panel.fit(
        f"[bold]{v.name}[/bold]  ({v.vendor_id})\n"
        f"distress [bold]{alert.distress.value:.0f}[/bold] ({alert.severity})   "
        f"legitimacy [bold]{alert.legitimacy.value:.0f}[/bold] ({alert.legitimacy.band})   "
        f"priority {alert.priority} ({alert.priority_score:.0f})\n"
        f"exposure factor {alert.exposure_factor:.2f}   "
        f"coverage {alert.distress.coverage:.0%}",
        border_style="cyan"))

    t = Table(title="Per-signal contribution")
    for c in ("Signal", "Fired", "Value", "Points", "Share", "Conf", "Why"):
        t.add_column(c)
    for row in contribution_table(alert.distress):
        t.add_row(row["signal"], "YES" if row["fired"] else "-", str(row["value"])[:18],
                  f"{row['points']:.1f}", f"{row['share_pct']:.0f}%", row["confidence"],
                  row["why"][:60])
    console.print(t)

    inv = alert.investigation or {}
    console.print(Panel(
        f"[bold]{inv.get('verdict', '')}[/bold]\n\n"
        + "\n".join(f"  {k.replace('_', ' '):<34} {val}"
                    for k, val in (inv.get("confounders_checked") or {}).items())
        + (f"\n\n[yellow]data_unavailable:[/yellow]\n  "
           + "\n  ".join(inv.get("data_unavailable") or []) if inv.get("data_unavailable") else "")
        + (f"\n\n{inv.get('narration','')}" if inv.get("narration") else ""),
        title=f"Investigation ({inv.get('engine','')})", border_style="magenta"))

    guard = CitationGuard(cfg)
    console.print(Panel(guard.render(alert.claims) if alert.claims else "_no cited findings_",
                        title="Evidence-backed claims", border_style="green"))
    if alert.drafted_memo:
        console.print(Panel(alert.drafted_memo, title="Drafted memo", border_style="blue"))
    else:
        console.print(f"[dim]No memo drafted — {alert.suppression_reason}[/dim]")


@app.command()
def backtest(json_out: Optional[str] = typer.Option("data/backtest_report.json")) -> None:
    """Time-travel replay. Scores using only what was knowable on each date."""
    cfg = load_config()
    store = _store()
    load_all_fixtures(store)
    report = run_backtest(store, cfg)
    console.print(format_report(report))
    for r in report["results"]:
        mark = "[green]PASS[/green]" if r["correct"] else "[red]FAIL[/red]"
        console.print(f"\n{mark} [bold]{r['vendor_name']}[/bold] ({r['label']})")
        console.print(f"  expected: {r['expected_outcome']}")
        console.print(f"  actual  : {r['actual_outcome']}")
        for p in r["points"]:
            bar = "#" * int(p["distress"] / 4)
            flag = " <== PAGED" if p["paged"] else ""
            console.print(f"    {p['as_of']}  {p['distress']:>5.1f} {bar:<25} "
                          f"{p['band']:<9}{flag}")
    if json_out:
        Path(json_out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        console.print(f"\nreport written to {json_out}")


@app.command("guard-demo")
def guard_demo() -> None:
    """Prove the citation guard. Feeds it one good claim and one uncited claim."""
    cfg = load_config()
    guard = CitationGuard(cfg)
    good = Claim(
        text="Headcount fell from 214 to 139 between the May and August snapshots.",
        kind="numeric_change",
        evidence=(Evidence(source="crustdata_enrich",
                           source_url="https://api.crustdata.com/company/enrich",
                           fetched_at="2026-08-01T06:00:00+00:00",
                           field_path="headcount.total", as_of="2026-08-01",
                           from_value=214, to_value=139),),
        confidence="high")
    bad = Claim(text="The company is probably about to go under.", kind="judgement")

    console.print(Panel("[bold]1. A properly cited claim[/bold]", border_style="green"))
    console.print(guard.render([good]))

    console.print(Panel("[bold]2. The same renderer, given an uncited claim[/bold]",
                        border_style="red"))
    try:
        console.print(guard.render([good, bad]))
        console.print("[red]!! guard did not fire — this is a bug[/red]")
        raise typer.Exit(code=1)
    except CitationError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("\n[green]Nothing was rendered. There is no force flag.[/green]")


@app.command("watch-create")
def watch_create(
    webhook_url: str = typer.Argument(..., help="public URL of your receiver"),
    limit: int = typer.Option(10, help="how many ledger domains to watch"),
) -> None:
    """Create the Crustdata company entity watch."""
    cfg = load_config()
    store = _store()
    domains = [v.domain for v in store.list_vendors() if v.domain][:limit]
    if not domains:
        console.print("[red]no vendor domains in the ledger[/red]")
        raise typer.Exit(code=1)
    with CrustdataClient(config=cfg) as client:
        res = client.create_company_watch(domains, webhook_url)
    if not res.ok:
        console.print(f"[red]{res.reason}[/red]")
        raise typer.Exit(code=1)
    console.print(json.dumps(res.data, indent=2)[:1200])
    console.print("\n[dim]Note: company watches refresh on a 30-day cycle and the baseline "
                  "run fires on nothing. Use `watch-test` to exercise the signed path now."
                  "[/dim]")


@app.command("watch-test")
def watch_test(watch_id: str) -> None:
    """POST /watch/company/{id}/test — free, persists nothing, real HMAC headers."""
    cfg = load_config()
    with CrustdataClient(config=cfg) as client:
        res = client.test_watch(watch_id)
    if not res.ok:
        console.print(f"[red]{res.reason}[/red]")
        raise typer.Exit(code=1)
    console.print("[green]test envelope dispatched to your webhook URL[/green]")
    console.print(json.dumps(res.data, indent=2)[:800])


@app.command()
def ui() -> None:
    """Launch the one-page operator console."""
    import subprocess
    subprocess.run(["streamlit", "run", str(REPO_ROOT / "app" / "streamlit_app.py")])


def main() -> None:
    app()


if __name__ == "__main__":
    main()
