"""Offline review console: one self-contained HTML file (no server, no external requests).

Uses the same design and renderer as the live web app (public/desk.css + public/desk.js), inlined.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PUBLIC = Path(__file__).resolve().parents[2] / "public"

SKELETON = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outreach Review Desk</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand"><div class="mark" aria-hidden="true">S</div>
      <div><h1>Outreach Review Desk</h1><p id="sub"></p></div></div>
    <div class="meta"><span class="tag safe">Demo-safe: emails only reach the demo inbox</span>__RUN__
      <button class="theme" id="theme" type="button">Theme</button></div>
  </header>
  <section class="cards" id="cards" aria-label="Summary"></section>
  <div class="layout">
    <nav class="list" aria-label="Companies">
      <div class="search"><input id="q" type="search" placeholder="Search companies" aria-label="Search companies"></div>
      <div id="items"></div>
    </nav>
    <main class="detail" id="detail" aria-live="polite"></main>
  </div>
  <footer>Every point links to its source. Emails go only to the demo inbox; a human approves anything real.
    Keys: <span class="kbd">j</span>/<span class="kbd">k</span> next/previous, <span class="kbd">/</span> search.</footer>
</div>
<script>__JS__</script>
<script id="data" type="application/json">__DATA__</script>
<script>Desk.init(JSON.parse(document.getElementById('data').textContent));</script>
</body>
</html>
"""

# Kept for backwards compatibility with earlier imports.
TEMPLATE = SKELETON


def build_console(outcomes: list[dict], out_path: Path, title: str = "Outreach review", run_id: str = "",
                  summary_md: Optional[str] = None, today: Optional[str] = None) -> Path:
    payload = {"title": title, "run_id": run_id, "generated": datetime.now(timezone.utc).isoformat(),
               "today": today, "outcomes": outcomes}
    blob = json.dumps(payload, default=str).replace("</", "<\\/").replace("<!--", "<\\!--")
    css = (PUBLIC / "desk.css").read_text(encoding="utf-8")
    js = (PUBLIC / "desk.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    run = f'<span class="tag">run {run_id}</span>' if run_id else ""
    html = (SKELETON.replace("__CSS__", css).replace("__JS__", js).replace("__RUN__", run)
            .replace("__DATA__", blob))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
