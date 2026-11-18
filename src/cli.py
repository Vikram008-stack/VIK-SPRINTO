"""Command-line entry point:  python -m src.cli <command>

  run      --domains FILE [--dry-run|--send] [--refresh] [--limit N] [--live-hunter] [--transport direct]
  demo     full pipeline offline (fixtures, recorded model responses, zero API calls)
  eval     run eval/domains.txt and compare to eval/expected.yaml
  console  rebuild the review console (reports/console.html) from stored outcomes
  scrape-demo  raw vs cleaned token counts for a few domains
  check | init-db | quota | domains | purge-contacts
There is deliberately NO flag to set the recipient: email only ever goes to DEMO_RECIPIENT_EMAIL.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.common.config import config_hash, load_settings
from src.common.domains import parse_domains
from src.common.logging import setup_logging
from src.common.safety import UnsafeRecipientError, demo_recipient, startup_safety_check
from src.store.db import Database

app = typer.Typer(add_completion=False, help="Sprinto cold-outreach pipeline")
console = Console(width=140)
ROOT = Path(__file__).resolve().parents[1]


def _db(settings) -> Database:
    db = Database(settings.pipeline.paths.abs("db"))
    db.init()
    return db


def _print_quota(db, s, label: str) -> None:
    u = db.hunter_usage()
    console.print(f"[dim]{label}: Hunter searches {u['searches_used']}/{s.pipeline.hunter.hard_stop} used this month "
                  f"(budget {s.pipeline.hunter.monthly_budget}); verifications {u['verifications_used']}[/dim]")


def _results_table(outcomes) -> None:
    t = Table("Domain", "Status", "Score", "Contact", "Why", "Sec", "Cost $")
    colors = {"DRAFTED": "green", "SENT": "green", "QUALIFIED": "green", "REJECTED": "red", "DUPLICATE": "dim",
              "NEEDS_RESEARCH": "yellow", "INSUFFICIENT_DATA": "yellow", "DRAFT_NEEDS_HUMAN": "yellow",
              "NO_CONTACT_FOUND": "yellow", "FAILED": "red", "SEND_FAILED": "red"}
    for o in outcomes:
        q = o.qualification or {}
        ch = ((o.contact or {}).get("chosen") or {})
        c = colors.get(o.status, "white")
        t.add_row(o.domain, f"[{c}]{o.status}[/{c}]", str(q.get("score", "-")),
                  escape(f"{ch.get('name')} ({ch.get('position')})") if ch else "-",
                  escape((o.status_reason or "")[:70]), f"{o.seconds:.1f}",
                  f"{(o.cost or {}).get('cost_usd', 0) or 0:.4f}")
    console.print(t)


def _finish_run(s, db, pipe, outcomes, title: str) -> Path:
    from src.report.console import build_console
    from src.report.summary import defence_sheets, run_summary
    data = [json.loads(o.to_json()) for o in outcomes]
    summary = run_summary(data, db.hunter_usage(), s.pipeline.hunter.hard_stop, s.pipeline.run.max_seconds_per_company)
    pipe.reports_dir.mkdir(parents=True, exist_ok=True)
    (pipe.reports_dir / "run_summary.md").write_text(summary, encoding="utf-8")
    (pipe.reports_dir / "defence_sheets.md").write_text(defence_sheets(data), encoding="utf-8")
    db.finish_run(pipe.run_id, {"statuses": {o.status: sum(1 for x in outcomes if x.status == o.status)
                                             for o in outcomes}})
    html = build_console(data, s.pipeline.paths.abs("reports") / "console.html", title=title, run_id=pipe.run_id,
                         summary_md=summary, today=pipe.today.isoformat())
    return html


@app.command()
def run(domains: Path = typer.Option(..., "--domains", exists=True, readable=True, help="File with one domain per line"),
        dry_run: bool = typer.Option(True, "--dry-run/--send", help="Default dry run. --send delivers to the demo inbox"),
        refresh: bool = typer.Option(False, "--refresh", help="Redo companies already processed"),
        limit: Optional[int] = typer.Option(None, "--limit", help="Process at most N domains"),
        live_hunter: bool = typer.Option(False, "--live-hunter", help="Allow live Hunter calls (spends quota)"),
        transport: Optional[str] = typer.Option(None, "--transport", help="scrapingbee | direct (dev only)")) -> None:
    """Run the full pipeline on a list of domains."""
    from src.factory import SetupError, build_live, new_run_id
    from src.pipeline import RunOptions
    s = load_settings()
    try:
        startup_safety_check(sending=not dry_run)
    except UnsafeRecipientError as e:
        console.print(f"[red]Refusing to start: {e}[/red]")
        raise typer.Exit(2)
    if live_hunter:
        s.pipeline.hunter.mode = "live"
    run_id = new_run_id()
    setup_logging(s.pipeline.paths.abs("logs"), run_id)
    db = _db(s)
    _print_quota(db, s, "start")
    try:
        pipe = build_live(s, db, run_id, dry_run=dry_run, live_hunter=live_hunter, transport=transport)
    except SetupError as e:
        console.print(f"[red]{e}[/red]\nTip: `python -m src.cli demo` runs the whole flow offline with no keys.")
        raise typer.Exit(2)
    console.print(f"Run [bold]{run_id}[/bold]  mode={'DRY RUN' if dry_run else 'SEND to ' + demo_recipient()}  "
                  f"config={config_hash()}")
    outcomes = pipe.run(domains.read_text(encoding="utf-8").splitlines(),
                        RunOptions(dry_run=dry_run, refresh=refresh, limit=limit))
    _results_table(outcomes)
    html = _finish_run(s, db, pipe, outcomes, "Outreach run")
    _print_quota(db, s, "end")
    console.print(f"Reports: {pipe.reports_dir}\nReview console: {html}")


@app.command()
def demo(fresh: bool = typer.Option(True, "--fresh/--keep", help="Start from an empty demo database")) -> None:
    """Run the WHOLE pipeline offline: fixture sites, recorded model output, mock Hunter, dry-run email."""
    from src.factory import build_offline_demo, new_run_id
    from src.pipeline import RunOptions
    from src import demo_offline
    s = load_settings()
    demo_dir = s.pipeline.paths.abs("db").parent / "demo"
    if fresh and demo_dir.exists():
        shutil.rmtree(demo_dir)
    demo_dir.mkdir(parents=True, exist_ok=True)
    s.pipeline.paths.db = str(demo_dir / "demo.db")
    run_id = "demo-" + new_run_id()
    setup_logging(demo_dir / "logs", run_id)
    db = _db(s)
    reports = s.pipeline.paths.abs("reports") / run_id
    pipe = build_offline_demo(s, db, run_id, reports_dir=reports, outcomes_dir=demo_dir / "outcomes")
    console.print(f"[bold]Offline demo[/bold] {run_id}: fictional companies, zero API calls, dry run")
    outcomes = pipe.run(demo_offline.DOMAINS, RunOptions(dry_run=True, command="demo"))
    _results_table(outcomes)
    html = _finish_run(s, db, pipe, outcomes, "Offline demo")
    console.print(f"Reports: {reports}\nReview console: {html}")


@app.command("eval")
def eval_cmd(report_only: bool = typer.Option(False, "--report-only", help="Re-score stored outcomes, no API calls"),
             live_hunter: bool = typer.Option(False, "--live-hunter"),
             transport: Optional[str] = typer.Option(None, "--transport")) -> None:
    """Run eval/domains.txt, compare with eval/expected.yaml, print accuracy and kill-criterion warnings."""
    import yaml

    from src.factory import SetupError, build_live, new_run_id
    from src.pipeline import RunOptions
    from src.report.summary import defence_sheets, eval_table, load_expected
    s = load_settings()
    expected, frozen = load_expected(ROOT / "eval" / "expected.yaml")
    rewrites_path = ROOT / "eval" / "rewrite_judgements.yaml"
    rewrites = yaml.safe_load(rewrites_path.read_text(encoding="utf-8")) if rewrites_path.exists() else None
    db = _db(s)
    if report_only:
        outdir = s.pipeline.paths.abs("db").parent / "outcomes"
        lines = (ROOT / "eval" / "domains.txt").read_text(encoding="utf-8").splitlines()
        parsed = parse_domains(lines)
        data = [json.loads((outdir / f"{d}.json").read_text(encoding="utf-8"))
                for d, _ in parsed.unique if (outdir / f"{d}.json").exists()]
        data += [{"domain": d, "input_raw": raw, "status": "DUPLICATE", "failures": [], "cost": {}, "seconds": 0}
                 for d, raw in parsed.duplicates]
        reports = s.pipeline.paths.abs("reports") / "eval-latest"
    else:
        if not frozen:
            console.print("[yellow]WARNING: eval/expected.yaml is not frozen. Freeze it before the first eval run."
                          "[/yellow]")
        if live_hunter:
            s.pipeline.hunter.mode = "live"
        run_id = "eval-" + new_run_id()
        setup_logging(s.pipeline.paths.abs("logs"), run_id)
        _print_quota(db, s, "start")
        try:
            pipe = build_live(s, db, run_id, dry_run=True, live_hunter=live_hunter, transport=transport)
        except SetupError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(2)
        outcomes = pipe.run((ROOT / "eval" / "domains.txt").read_text(encoding="utf-8").splitlines(),
                            RunOptions(dry_run=True, refresh=True, command="eval"))
        _results_table(outcomes)
        _finish_run(s, db, pipe, outcomes, "Evaluation run")
        data = [json.loads(o.to_json()) for o in outcomes]
        reports = pipe.reports_dir
        _print_quota(db, s, "end")
    md, acc = eval_table(data, expected, frozen, rewrites)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "eval.md").write_text(md, encoding="utf-8")
    (reports / "defence_sheets.md").write_text(defence_sheets(data), encoding="utf-8")
    console.print(md)
    if acc < 0.60:
        console.print("[bold red]KILL CRITERION: accuracy below 60%[/bold red]")
    console.print(f"Saved: {reports / 'eval.md'} and defence_sheets.md")


@app.command("console")
def console_cmd(demo_data: bool = typer.Option(False, "--demo", help="Use the offline demo outcomes")) -> None:
    """Rebuild the review console from stored outcomes (no API calls)."""
    from src.report.console import build_console
    s = load_settings()
    base = s.pipeline.paths.abs("db").parent
    outdir = base / ("demo/outcomes" if demo_data else "outcomes")
    data = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(outdir.glob("*.json"))]
    if not data:
        console.print(f"No outcomes in {outdir}. Run `demo` or `run` first.")
        raise typer.Exit(1)
    html = build_console(data, s.pipeline.paths.abs("reports") / "console.html",
                         title="Offline demo" if demo_data else "Outreach review", run_id=data[0].get("run_id", ""))
    console.print(f"Review console: {html}")


@app.command("scrape-demo")
def scrape_demo(domains: list[str] = typer.Argument(None),
                transport: Optional[str] = typer.Option(None, "--transport", help="scrapingbee | direct")) -> None:
    """Fetch homepages (robots-checked) and show raw vs cleaned token counts. No LLM calls."""
    from src.scrape.fetcher import PageFetcher
    from src.scrape.http_cache import HttpCache
    from src.scrape.robots import RobotsChecker, httpx_robots_fetch
    from src.scrape.transport import DirectTransport, ScrapingBeeTransport
    s = load_settings()
    p = s.pipeline
    tname = transport or p.scrape.transport
    if tname == "scrapingbee" and not s.secrets.scrapingbee_api_key:
        console.print("[yellow]SCRAPINGBEE_API_KEY not set; using the dev-only direct transport (0 credits).[/yellow]")
        tname = "direct"
    db = _db(s)
    cache = HttpCache(db, p.paths.abs("cache"), p.scrape.cache_ttl_days)
    tr = ScrapingBeeTransport(s.secrets.scrapingbee_api_key, p.scrape.timeout_s) if tname == "scrapingbee" \
        else DirectTransport(p.scrape.timeout_s)
    f = PageFetcher(p, db, tr, RobotsChecker(httpx_robots_fetch(), cache), cache)
    t = Table("Domain", "Mode", "OK", "Reason", "Raw tokens", "Clean tokens", "Reduction", "Credits", "Discovered pages")
    for d in domains or ["chargebee.com", "thoughtworks.com", "cal.com"]:
        sess = f.start(d)
        pg = f.fetch_homepage(sess)
        m = pg.meta
        red = f"{(1 - (m.clean_tokens or 0) / m.raw_tokens):.0%}" if m.raw_tokens else "-"
        disc = ", ".join(k for k, v in sess.discovered.items() if v) or "-"
        t.add_row(d, m.mode.value, "yes" if m.ok else "no", m.failure_reason.value if m.failure_reason else "-",
                  str(m.raw_tokens or "-"), str(m.clean_tokens or "-"), red, str(m.credits), disc)
    console.print(t)


@app.command()
def check() -> None:
    """Validate config files and show which secrets are set (values never shown)."""
    s = load_settings()
    p = s.pipeline
    console.print(f"[bold]Config OK[/bold]  rubric={s.rubric.rubric_version}  config_hash={config_hash()}")
    console.print(f"  models: {dict(p.llm.models)}")
    console.print(f"  hunter: mode={p.hunter.mode} hard_stop={p.hunter.hard_stop}/{p.hunter.monthly_budget}")
    console.print(f"  email: mode={p.email.mode}  already_certified_policy={p.qualification.already_certified_policy}")
    console.print(f"  caps: {p.scrape.max_fetches_per_company} fetches, {p.qualification.max_extra_rounds} extra rounds, "
                  f"${p.run.max_cost_usd_per_company}/company, {p.run.max_seconds_per_company}s/company")
    t = Table("secret", "set?")
    for k, v in s.secrets.present().items():
        t.add_row(k, "[green]yes[/green]" if v else "[red]no[/red]")
    console.print(t)
    try:
        demo_recipient()
        console.print("  DEMO_RECIPIENT_EMAIL: valid")
    except UnsafeRecipientError as e:
        console.print(f"  [yellow]{e} (only needed for --send)[/yellow]")


@app.command("init-db")
def init_db() -> None:
    """Create the SQLite database and all audit tables."""
    s = load_settings()
    db = _db(s)
    console.print(f"DB ready at {db.path}")
    console.print("tables: " + ", ".join(db.tables()))


@app.command()
def quota() -> None:
    """Show Hunter usage for the current calendar month."""
    s = load_settings()
    db = _db(s)
    u = db.hunter_usage()
    h = s.pipeline.hunter
    console.print(f"Hunter searches: {u['searches_used']}/{h.hard_stop} usable "
                  f"(monthly budget {h.monthly_budget}, {h.monthly_budget - h.hard_stop} held for emergencies)")
    console.print(f"Hunter verifications: {u['verifications_used']}/{h.verifier_monthly_budget}")


@app.command()
def domains(file: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Preview how an input file is normalised and deduplicated (no API calls)."""
    parsed = parse_domains(file.read_text(encoding="utf-8").splitlines())
    t = Table("domain", "raw input", "status")
    for d, raw in parsed.unique:
        t.add_row(d, escape(raw), "queued")
    for d, raw in parsed.duplicates:
        t.add_row(d, escape(raw), "[yellow]DUPLICATE (duplicate_input)[/yellow]")
    for raw, err in parsed.invalid:
        t.add_row("-", escape(raw), f"[red]invalid_domain: {escape(err)}[/red]")
    console.print(t)


@app.command("purge-contacts")
def purge_contacts(domain: Optional[str] = typer.Option(None, help="Only this domain"),
                   yes: bool = typer.Option(False, "--yes", help="Skip confirmation")) -> None:
    """Delete stored contact data (privacy: keep it minimal)."""
    if not yes and not typer.confirm(f"Delete contacts for {domain or 'ALL domains'}?"):
        raise typer.Abort()
    s = load_settings()
    n = _db(s).purge_contacts(domain)
    console.print(f"Deleted {n} contact rows")


if __name__ == "__main__":
    app()
