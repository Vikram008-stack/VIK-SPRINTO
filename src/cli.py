"""Command-line entry point:  python -m src.cli <command>

Milestone 1 commands: check, init-db, quota, domains, purge-contacts.
`run` and `eval` arrive in milestones 10-11.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.common.config import config_hash, load_settings
from src.common.domains import parse_domains
from src.common.safety import UnsafeRecipientError, demo_recipient
from src.store.db import Database

app = typer.Typer(add_completion=False, help="Sprinto cold-outreach pipeline")
console = Console()


def _db(settings) -> Database:
    db = Database(settings.pipeline.paths.abs("db"))
    db.init()
    return db


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
    if not p.llm.pricing_usd_per_1m:
        console.print("  [yellow]llm pricing not set -> LLM cost will be recorded as unknown (set at milestone 3)[/yellow]")

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
