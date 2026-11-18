"""Review console: one self-contained HTML file (no server, no external requests).

Designed for a human reviewer (the Growth Lead): plain-language verdict first, then the evidence behind every
point, then the person and the email. Data is embedded as JSON and rendered with vanilla JS.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outreach Review Desk</title>
<style>
:root{
  --bg:#f7f5f1; --panel:#ffffff; --panel-2:#fbfaf7; --ink:#1d2430; --ink-2:#4a5565; --ink-3:#7c8695;
  --line:#e8e4dc; --line-2:#f0ede7; --accent:#2f6f5e; --accent-soft:#e3f0eb;
  --good:#2f7a4f; --good-soft:#e5f3ea; --warn:#a0620d; --warn-soft:#fbf0dc; --bad:#a2423d; --bad-soft:#f8e6e4;
  --muted:#6b7280; --muted-soft:#eef0f2; --info:#355f9a; --info-soft:#e6eef9;
  --shadow:0 1px 2px rgba(30,30,20,.04),0 4px 16px rgba(30,30,20,.05);
  --radius:14px; --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --sans:Inter,"Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;
  --mono:"Cascadia Code",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --bg:#14171c; --panel:#1b1f26; --panel-2:#20252d; --ink:#e8eaee; --ink-2:#b4bac4; --ink-3:#8790a0;
  --line:#2b313b; --line-2:#252a33; --accent:#6cc2a6; --accent-soft:#1f3a33;
  --good:#7fd19e; --good-soft:#1d3427; --warn:#f0b65a; --warn-soft:#3a2d17; --bad:#f08f88; --bad-soft:#3d2321;
  --muted:#9aa3b2; --muted-soft:#262b33; --info:#8fb5ec; --info-soft:#1f2b3d; --shadow:none; } }
:root[data-theme="dark"]{
  --bg:#14171c; --panel:#1b1f26; --panel-2:#20252d; --ink:#e8eaee; --ink-2:#b4bac4; --ink-3:#8790a0;
  --line:#2b313b; --line-2:#252a33; --accent:#6cc2a6; --accent-soft:#1f3a33;
  --good:#7fd19e; --good-soft:#1d3427; --warn:#f0b65a; --warn-soft:#3a2d17; --bad:#f08f88; --bad-soft:#3d2321;
  --muted:#9aa3b2; --muted-soft:#262b33; --info:#8fb5ec; --info-soft:#1f2b3d; --shadow:none; }
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
.wrap{max-width:1320px;margin:0 auto;padding:28px 24px 64px}
header.top{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:22px}
.brand{display:flex;gap:12px;align-items:center}
.mark{width:38px;height:38px;border-radius:11px;background:var(--accent);display:grid;place-items:center;color:#fff;font-weight:700}
.brand h1{font-size:20px;margin:0;letter-spacing:-.01em}
.brand p{margin:2px 0 0;color:var(--ink-3);font-size:13px}
.meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tag{font-size:12px;padding:4px 10px;border-radius:999px;background:var(--muted-soft);color:var(--ink-2);border:1px solid var(--line)}
.tag.safe{background:var(--accent-soft);color:var(--accent);border-color:transparent}
button.theme{font:inherit;font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);background:var(--panel);color:var(--ink-2);cursor:pointer}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
@media (max-width:520px){.wrap{padding:20px 16px 48px}.cards{grid-template-columns:1fr 1fr;gap:10px}.card{padding:12px}
  .card .n{font-size:22px}.panel{padding:16px}.verdict{grid-template-columns:1fr}.letter{padding:16px;font-size:15.5px}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;box-shadow:var(--shadow);cursor:pointer;text-align:left;font:inherit;color:inherit}
.card.static{cursor:default}
.card[aria-pressed="true"]{outline:2px solid var(--accent);outline-offset:-2px}
.card .n{font-size:26px;font-weight:650;letter-spacing:-.02em}
.card .l{font-size:13px;color:var(--ink-2)}
.card .s{font-size:12px;color:var(--ink-3);margin-top:2px}
.layout{display:grid;grid-template-columns:330px 1fr;gap:18px;align-items:start}
@media (max-width:900px){.layout{grid-template-columns:1fr}.list{position:static}}
.list{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;position:sticky;top:16px}
.list .search{padding:12px;border-bottom:1px solid var(--line)}
.list input{width:100%;font:inherit;padding:8px 12px;border-radius:10px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink)}
.item{display:flex;gap:12px;align-items:center;padding:12px 14px;border:0;border-bottom:1px solid var(--line-2);cursor:pointer;width:100%;background:none;border-left:3px solid transparent;font:inherit;color:inherit;text-align:left}
.item:hover{background:var(--panel-2)}
.item[aria-current="true"]{background:var(--panel-2);border-left-color:var(--accent)}
.item .nm{font-weight:600;font-size:14px}
.item .dm{font-size:12px;color:var(--ink-3)}
.ring{--p:0;width:40px;height:40px;border-radius:50%;flex:none;display:grid;place-items:center;font-size:12px;font-weight:650;
  background:conic-gradient(var(--rc,var(--accent)) calc(var(--p)*1%),var(--line-2) 0)}
.ring span{width:30px;height:30px;border-radius:50%;background:var(--panel);display:grid;place-items:center}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;white-space:nowrap}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.85}
.g{background:var(--good-soft);color:var(--good)} .w{background:var(--warn-soft);color:var(--warn)}
.b{background:var(--bad-soft);color:var(--bad)} .m{background:var(--muted-soft);color:var(--muted)} .i{background:var(--info-soft);color:var(--info)}
.detail{display:flex;flex-direction:column;gap:16px;min-width:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:20px 22px}
.panel h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3);margin:0 0 12px;font-weight:650}
.verdict{display:grid;grid-template-columns:auto 1fr;gap:20px;align-items:center}
.verdict .big{--p:0;width:92px;height:92px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--rc,var(--accent)) calc(var(--p)*1%),var(--line-2) 0)}
.verdict .big span{width:74px;height:74px;border-radius:50%;background:var(--panel);display:grid;place-items:center;font-size:24px;font-weight:700;letter-spacing:-.02em}
.verdict .big small{display:block;font-size:11px;color:var(--ink-3);font-weight:500;margin-top:-4px}
.verdict h3{margin:0 0 4px;font-size:22px;letter-spacing:-.015em}
.verdict .why{color:var(--ink-2);margin:6px 0 10px;max-width:70ch}
.next{display:flex;gap:10px;align-items:flex-start;background:var(--panel-2);border:1px solid var(--line);border-radius:12px;padding:10px 12px;font-size:14px}
.next b{white-space:nowrap}
.bars{display:flex;flex-direction:column;gap:12px}
.bar{display:grid;grid-template-columns:160px 1fr 56px;gap:12px;align-items:center}
@media (max-width:640px){.bar{grid-template-columns:1fr 52px}.bar .track{grid-column:1/-1;order:3}}
.bar .lbl{font-size:14px;font-weight:600}
.bar .lbl small{display:block;font-weight:400;color:var(--ink-3);font-size:12px}
.track{height:10px;background:var(--line-2);border-radius:999px;overflow:hidden}
.fill{height:100%;border-radius:999px;background:var(--accent)}
.pts{font-variant-numeric:tabular-nums;text-align:right;font-weight:650;font-size:14px}
.reason{grid-column:2/-1;font-size:13px;color:var(--ink-2);margin-top:-6px}
@media (max-width:640px){.reason{grid-column:1/-1}}
.src{font-size:12px;word-break:break-all}
.ev{display:grid;gap:10px}
.evc{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--panel-2)}
.evc .top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.evc .txt{font-size:14px}
.evc q{display:block;font-family:var(--serif);color:var(--ink-2);margin-top:6px;font-style:italic;quotes:"\201C" "\201D"}
.small{font-size:12px;color:var(--ink-3)}
.timeline{list-style:none;margin:0;padding:0;position:relative}
.timeline li{position:relative;padding:0 0 16px 26px}
.timeline li::before{content:"";position:absolute;left:7px;top:6px;bottom:-6px;width:2px;background:var(--line)}
.timeline li:last-child::before{display:none}
.timeline li::after{content:"";position:absolute;left:2px;top:4px;width:12px;height:12px;border-radius:50%;background:var(--panel);border:2px solid var(--accent)}
.timeline .rt{font-weight:600;font-size:14px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.chip{font-size:12px;padding:2px 8px;border-radius:8px;background:var(--muted-soft);color:var(--ink-2);border:1px solid var(--line)}
.chip.miss{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.chip.ok{background:var(--good-soft);color:var(--good);border-color:transparent}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line-2);vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);font-weight:650}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto}
.person{display:flex;gap:14px;align-items:center;margin-bottom:10px}
.avatar{width:48px;height:48px;border-radius:50%;background:var(--accent-soft);color:var(--accent);display:grid;place-items:center;font-weight:700;font-size:17px;flex:none}
.person .pn{font-size:17px;font-weight:650}
.person .pt{color:var(--ink-2);font-size:14px}
.letter{background:var(--panel-2);border:1px solid var(--line);border-radius:14px;padding:22px 26px;font-family:var(--serif);font-size:16.5px;line-height:1.7;color:var(--ink);max-width:68ch}
.letter .hdr{font-family:var(--sans);font-size:13px;color:var(--ink-3);border-bottom:1px dashed var(--line);padding-bottom:10px;margin-bottom:14px;line-height:1.6}
.letter .hdr b{color:var(--ink-2);font-weight:600}
.letter .body{white-space:pre-wrap}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.btn{font:inherit;font-size:13px;padding:6px 12px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
.btn:hover{border-color:var(--accent)}
.guard{display:flex;gap:8px;align-items:center;font-size:13px}
.flags{display:grid;gap:8px}
.flag{display:flex;gap:10px;align-items:flex-start;font-size:14px}
.flag code{font-family:var(--mono);font-size:11.5px;background:var(--muted-soft);padding:1px 6px;border-radius:6px;color:var(--ink-2)}
.empty{color:var(--ink-3);font-size:14px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px} @media (max-width:1100px){.two{grid-template-columns:1fr}}
details summary{cursor:pointer;color:var(--ink-2);font-size:13px;margin-top:8px}
.kbd{font-family:var(--mono);font-size:11px;border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;color:var(--ink-3)}
footer{margin-top:26px;color:var(--ink-3);font-size:12px;text-align:center}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media print{.list,.cards,header .meta{display:none}.layout{grid-template-columns:1fr}.panel{box-shadow:none;break-inside:avoid}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand"><div class="mark" aria-hidden="true">S</div>
      <div><h1>Outreach Review Desk</h1><p id="sub"></p></div></div>
    <div class="meta" id="meta"></div>
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
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const OUT = D.outcomes;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const link = (u, t) => u ? `<a class="src" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(t || u.replace(/^https?:\/\//,''))}</a>` : '';
const STATUS = {
  DRAFTED:['Draft ready for review','g','ready'], SENT:['Sent to demo inbox','g','ready'], QUALIFIED:['Qualified','g','ready'],
  NEEDS_RESEARCH:['Needs a human look','w','human'], INSUFFICIENT_DATA:['Not enough evidence yet','w','human'],
  DRAFT_NEEDS_HUMAN:['Draft needs a rewrite','w','human'], NO_CONTACT_FOUND:['No decision-maker found','w','human'],
  REJECTED:['Not a fit','b','nofit'], DUPLICATE:['Duplicate input','m','dup'], FAILED:['Something broke','b','human'],
  SEND_FAILED:['Send failed','b','human'], PENDING:['Pending','m','human']};
const REASON = {
  bot_blocked:'The site blocked our reader', captcha:'The site showed a CAPTCHA (we never bypass these)',
  login_wall:'Content sits behind a login', empty_shell:'The page was an empty app shell', geo_blocked:'Blocked in our region',
  timeout:'The site took too long to answer', dns_error:'The domain did not resolve', robots_disallowed:'robots.txt asks crawlers not to read this page',
  thin_content:'Very little text on the page', rate_limited:'Rate limited', domain_mismatch:'Redirected to a different company\'s site',
  http_error:'Page returned an error', fetch_cap_reached:'Fetch cap reached (by design)', parked_domain:'Parked / for-sale domain',
  duplicate_input:'Same company appeared twice in the input', llm_schema_error:'Model output failed validation twice',
  budget_exceeded:'Per-company cost/time budget reached', hunter_quota_exhausted:'Hunter monthly quota reached',
  hunter_no_results:'Hunter found nobody', hunter_live_not_enabled:'Hunter in mock mode with no recording for this domain',
  no_contact_found:'No suitable decision-maker', search_unavailable:'Web search unavailable; used company pages only',
  fewer_than_two_pain_points:'Fewer than two evidence-backed pain points (we never pad)', guardrail_failed:'Email failed quality rules twice',
  score_diff:'Model\'s suggested score differed; Python\'s rule-based score is final', no_pain_evidence:'No evidence to base a pain point on',
  url_provenance_rejected:'Model cited a page we never read (rejected)', snippet_not_found:'Model quote not found on the page (rejected)',
  gmail_auth_error:'Gmail sign-in failed', gmail_quota_error:'Gmail quota reached', unsafe_recipient:'Blocked a non-demo recipient',
  unexpected_error:'Unexpected error', homepage_unusable:'Homepage unusable', email_unverified:'Email failed verification'};
const COMP = {saas_fit:['SaaS fit','Is this a software subscription business?'], compliance_trigger:['Compliance trigger','Is there a real, sourced reason to talk now?'],
  stage:['Stage & scale','Funding / growth stage'], enterprise:['Enterprise exposure','Do they sell to bigger companies?'], recency:['Trigger recency','How fresh is the trigger?']};
const ROW = {already_certified:'Already certified?', b2b_saas:'What they sell & to whom', stage:'Company stage', trigger:'Compliance trigger'};
const ringColor = s => ({g:'var(--good)',w:'var(--warn)',b:'var(--bad)',m:'var(--muted)'})[(STATUS[s]||['','m'])[1]];
const name = o => o.company_name || o.domain;
const fmtDate = d => { if(!d) return 'undated'; const x = new Date(d); return isNaN(x) ? d : x.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'}); };
const daysAgo = d => { if(!d) return ''; const t = (D.today ? new Date(D.today) : new Date()); const n = Math.round((t - new Date(d))/864e5); return n<0? `in ${-n} days` : n===0?'today':`${n} days ago`; };
let filter = 'all', current = 0, query = '';

function groups(){
  const c = {ready:0,human:0,nofit:0,dup:0};
  OUT.forEach(o => c[(STATUS[o.status]||['','','human'])[2]]++);
  return c;
}
function renderTop(){
  const t = D.generated ? new Date(D.generated).toLocaleString() : '';
  document.getElementById('sub').textContent = `${D.title} · ${OUT.length} companies · built ${t}`;
  const times = OUT.filter(o=>o.seconds).map(o=>o.seconds); const avg = times.length? times.reduce((a,b)=>a+b,0)/times.length : 0;
  const cost = OUT.reduce((a,o)=>a+((o.cost||{}).cost_usd||0),0);
  document.getElementById('meta').innerHTML = `<span class="tag safe">Demo-safe: emails only reach the demo inbox</span>
    ${D.run_id?`<span class="tag">run ${esc(D.run_id)}</span>`:''}<button class="theme" id="theme" type="button">Theme</button>`;
  document.getElementById('theme').onclick = () => { const r = document.documentElement; const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    const cur = r.dataset.theme || (dark?'dark':'light'); r.dataset.theme = cur==='dark'?'light':'dark'; };
  const g = groups();
  const card = (key,n,l,s) => `<button class="card" type="button" data-f="${key}" aria-pressed="${filter===key}"><div class="n">${n}</div><div class="l">${l}</div><div class="s">${s}</div></button>`;
  document.getElementById('cards').innerHTML =
    card('all', OUT.length, 'All companies', 'everything in this run') +
    card('ready', g.ready, 'Ready for your review', 'qualified, person chosen, draft passed checks') +
    card('human', g.human, 'Need a human', 'open questions are listed, nothing guessed') +
    card('nofit', g.nofit, 'Not a fit', 'with the evidence for why') +
    `<div class="card static"><div class="n">${avg?avg.toFixed(1)+'s':'–'}</div><div class="l">Avg time per company</div><div class="s">target under 120 s · ${avg && avg<120?'on target':'check'}</div></div>` +
    `<div class="card static"><div class="n">$${cost.toFixed(3)}</div><div class="l">Model + scraping cost</div><div class="s">${g.dup} duplicate input${g.dup===1?'':'s'} skipped</div></div>`;
  document.querySelectorAll('.card[data-f]').forEach(b => b.onclick = () => { filter = b.dataset.f; renderTop(); renderList(true); });
}
function visible(){
  return OUT.map((o,i)=>[o,i]).filter(([o]) => (filter==='all' || (STATUS[o.status]||['','','human'])[2]===filter)
    && (!query || (name(o)+' '+o.domain).toLowerCase().includes(query)));
}
function renderList(selectFirst){
  const v = visible();
  if (selectFirst && v.length && !v.some(([,i])=>i===current)) current = v[0][1];
  document.getElementById('items').innerHTML = v.map(([o,i]) => {
    const s = STATUS[o.status]||[o.status,'m']; const sc = (o.qualification||{}).score;
    return `<button class="item" type="button" data-i="${i}" aria-current="${i===current}">
      <div class="ring" style="--p:${sc??0};--rc:${ringColor(o.status)}"><span>${sc??'–'}</span></div>
      <div style="min-width:0;flex:1"><div class="nm">${esc(name(o))}</div><div class="dm">${esc(o.domain)}</div></div>
      <span class="pill ${s[1]}">${esc(s[0])}</span></button>`; }).join('') || '<p class="empty" style="padding:16px">No companies match.</p>';
  document.querySelectorAll('.item').forEach(b => b.onclick = () => { current = +b.dataset.i; renderList(); renderDetail(); });
  renderDetail();
}
function verdict(o){
  const s = STATUS[o.status]||[o.status,'m']; const q = o.qualification||{}; const sc = q.score;
  const conf = o.confidence || q.confidence;
  return `<section class="panel"><div class="verdict">
    <div class="big" style="--p:${sc??0};--rc:${ringColor(o.status)}"><span><div style="text-align:center">${sc??'–'}<small>of 100</small></div></span></div>
    <div><div class="row"><span class="pill ${s[1]}">${esc(s[0])}</span>${conf?`<span class="pill i" title="How sure we are of the call (separate from score)">${esc(conf.toLowerCase())} confidence</span>`:''}</div>
      <h3>${esc(name(o))} <span class="small">${esc(o.domain)}</span></h3>
      <p class="why">${esc(o.status_reason || '')}</p>
      <div class="next"><b>Next step</b><span>${esc(o.next_action||'')}</span></div></div></div></section>`;
}
function breakdown(o){
  const q = o.qualification||{}; const comps = (q.breakdown||{}).components||[];
  if(!comps.length) return '';
  const rows = comps.map(c => { const [t,sub] = COMP[c.component]||[c.component,''];
    const pct = c.max_points? Math.round(100*c.points/c.max_points):0;
    const col = pct>=75?'var(--good)':pct>=40?'var(--accent)':pct>0?'var(--warn)':'var(--line)';
    return `<div class="bar"><div class="lbl">${esc(t)}<small>${esc(sub)}</small></div>
      <div class="track" role="img" aria-label="${c.points} of ${c.max_points}"><div class="fill" style="width:${pct}%;background:${col}"></div></div>
      <div class="pts">${c.points}/${c.max_points}</div>
      <div class="reason">${esc(c.reason)}${(c.source_urls||[]).length?' · '+c.source_urls.map(u=>link(u)).join(' · '):''}</div></div>`; }).join('');
  const mp = q.model_proposed_score!=null ? `<p class="small" style="margin-top:12px">The model suggested ${q.model_proposed_score}; the rule-based score above is final${q.score_diff?` (difference ${q.score_diff>0?'+':''}${q.score_diff})`:''}.</p>` : '';
  const reasons = (q.reasons||[]).length? `<details><summary>Decision rules applied</summary><ul>${q.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul></details>`:'';
  return `<section class="panel"><h2>Why this verdict</h2><div class="bars">${rows}</div>${mp}${reasons}</section>`;
}
function evidence(o){
  const f = o.facts||{}; const sigs = [...(f.compliance_signals||[]), ...(f.enterprise_signals||[])];
  const hires = f.hiring_signals||[];
  if(!sigs.length && !hires.length && !(f.certifications_claimed||[]).length) return '';
  const qc = {STRONG:'g',MEDIUM:'i',WEAK:'w'};
  const cards = sigs.map(s => `<div class="evc"><div class="top"><span class="pill ${qc[s.evidence_quality]||'m'}">${esc(s.evidence_quality.toLowerCase())}</span>
      ${s.trigger_type?`<span class="chip">${esc(s.trigger_type.replace(/_/g,' '))}</span>`:''}
      <span class="small">happened: ${fmtDate(s.event_date)}${s.event_date?` (${daysAgo(s.event_date)})`:''} · seen: ${fmtDate((s.observed_at||'').slice(0,10))}</span></div>
      <div class="txt">${esc(s.signal)}</div>${s.snippet?`<q>${esc(s.snippet)}</q>`:''}<div class="small" style="margin-top:6px">${link(s.source_url)}</div></div>`).join('');
  const hl = hires.length? `<div class="evc"><div class="top"><span class="pill i">open roles</span><span class="small">${hires.length} listed</span></div>
      <div class="chips">${hires.map(h=>`<span class="chip">${esc(h.title)}${h.event_date?' · '+fmtDate(h.event_date):''}</span>`).join('')}</div>
      <div class="small" style="margin-top:6px">${link(hires[0].source_url)}</div></div>`:'';
  const certs = (f.certifications_claimed||[]).length? `<div class="evc"><div class="top"><span class="pill m">already claims</span></div><div class="txt">${f.certifications_claimed.map(esc).join(', ')}</div></div>`:'';
  const conflicts = (f.conflicts||[]).length? `<div class="evc"><div class="top"><span class="pill w">conflict</span></div><div class="txt">${f.conflicts.map(esc).join('<br>')}</div></div>`:'';
  return `<section class="panel"><h2>Evidence we found</h2><div class="ev">${cards}${hl}${certs}${conflicts}</div></section>`;
}
function research(o){
  const q = o.qualification||{}; const rounds = q.rounds||[];
  const tl = rounds.map(r => `<li><div class="rt">${r.round===0?'Read the homepage':'Research round '+r.round}${r.score!=null?` · score ${r.score}`:''}</div>
     ${r.round>0?`<div class="small">Looked for: ${(r.missing_before||[]).map(x=>`<span class="chip miss">${esc(ROW[x]||x)}</span>`).join(' ')||'-'}</div>
     <div class="small" style="margin-top:4px">Fetched only: ${(r.fetched_urls||[]).map(u=>u.startsWith('search:')?'<span class="chip">web search</span>':link(u)).join(' · ')||'nothing'}</div>`:''}
     <div class="chips">${(r.missing_after||[]).length? r.missing_after.map(x=>`<span class="chip miss">still open: ${esc(ROW[x]||x)}</span>`).join(''):'<span class="chip ok">all questions answered</span>'}</div>
     ${r.note?`<div class="small">${esc(r.note)}</div>`:''}</li>`).join('');
  const pages = (o.pages||[]).length? `<details><summary>Pages read (${o.pages.length}) · tokens before → after cleaning</summary><div class="tablewrap"><table>
     <thead><tr><th>Page</th><th>Mode</th><th>Result</th><th class="num">Raw</th><th class="num">Clean</th><th class="num">Credits</th></tr></thead><tbody>
     ${o.pages.map(p=>`<tr><td>${link(p.url)}</td><td>${esc(p.mode)}</td><td>${p.ok?'<span class="pill g">read</span>':`<span class="pill w">${esc(REASON[p.reason]||p.reason||'not read')}</span>`}${p.ok&&p.reason?`<div class="small">${esc(REASON[p.reason]||p.reason)}</div>`:''}</td>
     <td class="num">${p.raw_tokens??'–'}</td><td class="num">${p.clean_tokens??'–'}</td><td class="num">${p.credits??0}${p.from_cache?' (cache)':''}</td></tr>`).join('')}</tbody></table></div></details>`:'';
  const notes = (o.loop_notes||[]).length? `<details><summary>Loop notes</summary><ul>${o.loop_notes.map(n=>`<li class="small">${esc(n)}</li>`).join('')}</ul></details>`:'';
  if(!tl && !pages) return '';
  return `<section class="panel"><h2>How we researched</h2><ul class="timeline">${tl}</ul>${pages}${notes}</section>`;
}
function person(o){
  const c = o.contact; if(!c) return '';
  const ch = c.chosen;
  const cand = (o.contact_candidates||[]).length? `<details><summary>Everyone we considered (${o.contact_candidates.length})</summary><div class="tablewrap"><table><thead><tr><th>Name</th><th>Title</th><th>Tier</th><th>Outcome</th></tr></thead><tbody>
      ${o.contact_candidates.map(k=>`<tr><td>${esc(k.name)}</td><td>${esc(k.position||'–')}</td><td>${k.tier??'–'}</td><td>${k.excluded?`<span class="small">skipped: ${esc(k.excluded)}</span>`:(ch&&k.name===ch.name?'<span class="pill g">chosen</span>':'eligible')}</td></tr>`).join('')}</tbody></table></div></details>`:'';
  if(!ch) return `<section class="panel"><h2>Who we'd write to</h2><p class="empty">${esc(c.why_this_person||'No suitable person found.')}</p>${cand}</section>`;
  const ini = (ch.first_name||ch.name||'?')[0] + ((ch.last_name||'')[0]||'');
  return `<section class="panel"><h2>Who we'd write to</h2><div class="person"><div class="avatar">${esc(ini)}</div>
    <div><div class="pn">${esc(ch.name)}</div><div class="pt">${esc(ch.position||'')} · <span class="small">source: ${esc(ch.source)}${ch.confidence!=null?` · Hunter confidence ${ch.confidence}`:''}${ch.verification_status?` · email ${esc(ch.verification_status)}`:''}</span></div></div></div>
    <p style="margin:0;color:var(--ink-2);font-size:14px">${esc(c.why_this_person)}</p>${cand}</section>`;
}
function email(o){
  const d = o.draft; const pains = o.pain_points||[];
  const pp = pains.length? `<div class="ev">${pains.map((p,i)=>`<div class="evc"><div class="top"><span class="pill ${p.confidence==='HIGH'?'g':p.confidence==='MEDIUM'?'i':'w'}">hypothesis ${i+1} · ${esc(p.confidence.toLowerCase())}</span><span class="small">${fmtDate(p.event_date)}</span></div>
      <div class="txt">${esc(p.hypothesis)}</div><div class="small" style="margin-top:4px">Why it matters to them: ${esc(p.why_it_matters_to_this_role)}</div>
      <div class="small" style="margin-top:4px">Based on: ${esc(p.signal)} · ${link(p.source_url)}</div></div>`).join('')}</div>`:'';
  const flags = (o.pain_flags||[]).filter(f=>f.startsWith('fewer')).length? `<p class="small">Only ${pains.length} evidence-backed pain point${pains.length===1?'':'s'}; we don't pad with generic filler.</p>`:'';
  if(!d && !pains.length) return '';
  const att = o.draft_attempts||[]; const last = att[att.length-1];
  const guard = last ? (last.errors.length? `<div class="guard"><span class="pill w">failed checks</span><span class="small">${last.errors.map(esc).join(' · ')}</span></div>`
                                 : `<div class="guard"><span class="pill g">passed all checks</span><span class="small">${last.words} words · attempt ${last.attempt} of 2</span></div>`) : '';
  const s = o.send||{};
  const letter = d ? `<div class="letter"><div class="hdr"><b>To:</b> ${esc(s.to||'demo inbox')} <span class="small">(demo inbox only)</span><br>
      <b>Intended for:</b> ${esc((o.contact&&o.contact.chosen)?(o.contact.chosen.name+', '+(o.contact.chosen.position||'')):'–')}<br>
      <b>Subject:</b> ${esc(d.subject)}</div><div class="body" id="emailbody">${esc(d.email_body)}</div></div>
      <div class="row" style="margin-top:12px"><button class="btn" type="button" id="copy">Copy email text</button>${guard}</div>
      <details><summary>Every claim and its source (${(d.personalisation_evidence||[]).length})</summary><ul>${(d.personalisation_evidence||[]).map(e=>`<li>${esc(e.claim)} · ${link(e.source_url)}</li>`).join('')}</ul></details>` : '';
  return `<section class="panel"><h2>The email</h2>${pains.length?`<div style="margin-bottom:16px">${pp}${flags}</div>`:''}${letter}</section>`;
}
function issues(o){
  const f = o.failures||[]; const flags = o.flags||[];
  if(!f.length && !flags.length) return '';
  return `<section class="panel"><h2>Flags & reason codes</h2><div class="flags">${f.map(x=>`<div class="flag"><span class="pill ${x.reason==='score_diff'?'i':'w'}">${esc(x.step)}</span><div>${esc(REASON[x.reason]||x.reason)} <code>${esc(x.reason)}</code>${x.detail?`<div class="small">${esc(x.detail)}</div>`:''}</div></div>`).join('')}
    ${flags.map(x=>`<div class="flag"><span class="pill m">site</span><div>${esc(REASON[x]||x)} <code>${esc(x)}</code></div></div>`).join('')}</div></section>`;
}
function renderDetail(){
  const o = OUT[current]; if(!o){ document.getElementById('detail').innerHTML=''; return; }
  document.getElementById('detail').innerHTML = verdict(o) + `<div class="two"><div class="detail">${breakdown(o)}${person(o)}</div><div class="detail">${evidence(o)}${research(o)}</div></div>` + email(o) + issues(o);
  const c = document.getElementById('copy');
  if(c) c.onclick = () => { navigator.clipboard?.writeText(document.getElementById('emailbody').textContent).then(()=>{ c.textContent='Copied'; setTimeout(()=>c.textContent='Copy email text',1400); }); };
}
document.getElementById('q').addEventListener('input', e => { query = e.target.value.toLowerCase().trim(); renderList(true); });
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') { if (e.key==='Escape') e.target.blur(); return; }
  const v = visible().map(([,i])=>i); const pos = v.indexOf(current);
  if (e.key==='j' && pos < v.length-1){ current = v[pos+1]; renderList(); }
  if (e.key==='k' && pos > 0){ current = v[pos-1]; renderList(); }
  if (e.key==='/'){ e.preventDefault(); document.getElementById('q').focus(); }
});
const firstReady = OUT.findIndex(o => (STATUS[o.status]||['','','x'])[2]==='ready'); current = firstReady>=0?firstReady:0;
renderTop(); renderList();
</script>
</body>
</html>
"""


def build_console(outcomes: list[dict], out_path: Path, title: str = "Outreach review", run_id: str = "",
                  summary_md: Optional[str] = None, today: Optional[str] = None) -> Path:
    order = {"DRAFTED": 0, "SENT": 0, "QUALIFIED": 1, "DRAFT_NEEDS_HUMAN": 2, "NO_CONTACT_FOUND": 2,
             "NEEDS_RESEARCH": 3, "INSUFFICIENT_DATA": 3, "SEND_FAILED": 3, "FAILED": 3, "REJECTED": 4,
             "DUPLICATE": 5}
    data = sorted(outcomes, key=lambda o: (order.get(o.get("status"), 9), -((o.get("qualification") or {})
                                                                           .get("score") or 0)))
    payload = {"title": title, "run_id": run_id, "generated": datetime.now(timezone.utc).isoformat(),
               "today": today, "outcomes": data}
    blob = json.dumps(payload, default=str).replace("</", "<\\/").replace("<!--", "<\\!--")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(TEMPLATE.replace("__DATA__", blob), encoding="utf-8")
    return out_path
