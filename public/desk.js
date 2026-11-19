/* Review Desk renderer. Shared by the live web app (public/index.html) and the offline console.html.
   Desk.init({title, run_id, generated, today, outcomes}) renders; Desk.upsert(outcome) adds/updates one row. */
(function () {
  let D = { title: '', outcomes: [] };
  let OUT = [];
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const link = (u, t) => u ? `<a class="src" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(t || String(u).replace(/^https?:\/\//,''))}</a>` : '';
  const STATUS = {
    DRAFTED:['Draft ready for review','g','ready'], SENT:['Sent to demo inbox','g','ready'], QUALIFIED:['Qualified','g','ready'],
    NEEDS_RESEARCH:['Needs a human look','w','human'], INSUFFICIENT_DATA:['Not enough evidence yet','w','human'],
    DRAFT_NEEDS_HUMAN:['Draft needs a rewrite','w','human'], NO_CONTACT_FOUND:['No decision-maker found','w','human'],
    REJECTED:['Not a fit','b','nofit'], DUPLICATE:['Duplicate input','m','dup'], INVALID:['Invalid input','m','dup'],
    FAILED:['Something broke','b','human'], SEND_FAILED:['Send failed','b','human'],
    RUNNING:['Researching…','i','running'], QUEUED:['Queued','m','running'], PENDING:['Pending','m','running']};
  const REASON = {
    bot_blocked:'The site blocked our reader', captcha:'The site showed a CAPTCHA (we never bypass these)',
    login_wall:'Content sits behind a login', empty_shell:'The page was an empty app shell', geo_blocked:'Blocked in our region',
    timeout:'The site took too long to answer', dns_error:'The domain did not resolve', robots_disallowed:'robots.txt asks crawlers not to read this page',
    thin_content:'Very little text on the page', rate_limited:'Rate limited', domain_mismatch:'Redirected to a different company\'s site',
    http_error:'Page returned an error', fetch_cap_reached:'Fetch cap reached (by design)', parked_domain:'Parked / for-sale domain',
    duplicate_input:'Same company appeared twice in the input', invalid_domain:'Not a valid domain', llm_schema_error:'Model output failed validation twice',
    llm_error:'AI model unavailable or over quota', llm_timeout:'AI model timed out',
    budget_exceeded:'Per-company cost/time budget reached', hunter_quota_exhausted:'Hunter monthly quota reached',
    hunter_no_results:'Hunter found nobody', hunter_live_not_enabled:'Hunter in mock mode with no recording for this domain',
    hunter_error:'Hunter request failed', no_contact_found:'No suitable decision-maker', search_unavailable:'Web search unavailable; used company pages only',
    fewer_than_two_pain_points:'Fewer than two evidence-backed pain points (we never pad)', guardrail_failed:'Email failed quality rules twice',
    score_diff:'Model\'s suggested score differed; the rule-based score is final', no_pain_evidence:'No evidence to base a pain point on',
    url_provenance_rejected:'Model cited a page we never read (rejected)', snippet_not_found:'Model quote not found on the page (rejected)',
    gmail_auth_error:'Gmail is not connected', gmail_quota_error:'Gmail quota reached', gmail_send_error:'Gmail send failed',
    unsafe_recipient:'Blocked a non-demo recipient', unexpected_error:'Unexpected error', homepage_unusable:'Homepage unusable',
    email_unverified:'Email failed verification', request_failed:'The request did not complete', missing_config:'Server is missing an API key'};
  const COMP = {saas_fit:['SaaS fit','Is this a software subscription business?'], compliance_trigger:['Compliance trigger','Is there a real, sourced reason to talk now?'],
    stage:['Stage & scale','Funding / growth stage'], enterprise:['Enterprise exposure','Do they sell to bigger companies?'], recency:['Trigger recency','How fresh is the trigger?']};
  const ROW = {already_certified:'Already certified?', b2b_saas:'What they sell & to whom', stage:'Company stage', trigger:'Compliance trigger'};
  const ORDER = {DRAFTED:0, SENT:0, QUALIFIED:1, RUNNING:2, QUEUED:3, DRAFT_NEEDS_HUMAN:4, NO_CONTACT_FOUND:4, NEEDS_RESEARCH:5,
                 INSUFFICIENT_DATA:5, SEND_FAILED:5, FAILED:5, REJECTED:6, DUPLICATE:7, INVALID:8};
  const ringColor = s => ({g:'var(--good)',w:'var(--warn)',b:'var(--bad)',m:'var(--muted)',i:'var(--info)'})[(STATUS[s]||['','m'])[1]];
  const name = o => o.company_name || o.domain;
  const fmtDate = d => { if(!d) return 'undated'; const x = new Date(d); return isNaN(x) ? d : x.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'}); };
  const daysAgo = d => { if(!d) return ''; const t = (D.today ? new Date(D.today) : new Date()); const n = Math.round((t - new Date(d))/864e5); return n<0? `in ${-n} days` : n===0?'today':`${n} days ago`; };
  let filter = 'all', currentKey = null, query = '';
  const key = o => o._key || o.domain;
  const opts = { onSend: null, sendLabel: null, onFollowup: null, onReview: null };

  function groups(){ const c = {ready:0,human:0,nofit:0,dup:0,running:0}; OUT.forEach(o => c[(STATUS[o.status]||['','','human'])[2]]++); return c; }
  function sortOut(){ OUT.sort((a,b) => (ORDER[a.status]??9)-(ORDER[b.status]??9) || (((b.qualification||{}).score)||0)-(((a.qualification||{}).score)||0)); }

  function renderTop(){
    document.body.classList.toggle('empty', OUT.length === 0);   // calm first screen: no zero-cards before any run
    const t = D.generated ? new Date(D.generated).toLocaleString() : '';
    const sub = document.getElementById('sub');
    if (sub) sub.textContent = `${D.title || 'Review'} · ${OUT.length} compan${OUT.length===1?'y':'ies'}${t?' · '+t:''}`;
    const times = OUT.filter(o=>o.seconds).map(o=>o.seconds); const avg = times.length? times.reduce((a,b)=>a+b,0)/times.length : 0;
    const cost = OUT.reduce((a,o)=>a+((o.cost||{}).cost_usd||0),0);
    const g = groups();
    const card = (k,n,l,s) => `<button class="card" type="button" data-f="${k}" aria-pressed="${filter===k}"><div class="n">${n}</div><div class="l">${l}</div><div class="s">${s}</div></button>`;
    const el = document.getElementById('cards'); if (!el) return;
    el.innerHTML =
      card('all', OUT.length, 'All companies', g.running? `${g.running} still researching` : 'everything in this run') +
      card('ready', g.ready, 'Ready for your review', 'qualified, person chosen, draft passed checks') +
      card('human', g.human, 'Need a human', 'open questions are listed, nothing guessed') +
      card('nofit', g.nofit, 'Not a fit', 'with the evidence for why') +
      `<div class="card static"><div class="n">${avg?avg.toFixed(0)+'s':'–'}</div><div class="l">Avg time per company</div><div class="s">target under 120 s${avg?(avg<120?' · on target':' · over target'):''}</div></div>` +
      `<div class="card static"><div class="n">$${cost.toFixed(3)}</div><div class="l">Model + scraping cost</div><div class="s">${g.dup} duplicate/invalid input${g.dup===1?'':'s'}</div></div>`;
    el.querySelectorAll('.card[data-f]').forEach(b => b.onclick = () => { filter = b.dataset.f; renderTop(); renderList(true); });
  }
  function visible(){
    return OUT.filter(o => (filter==='all' || (STATUS[o.status]||['','','human'])[2]===filter)
      && (!query || (name(o)+' '+o.domain).toLowerCase().includes(query)));
  }
  function renderList(selectFirst){
    const v = visible();
    if ((selectFirst || !currentKey) && v.length && !v.some(o=>key(o)===currentKey)) currentKey = key(v[0]);
    const items = document.getElementById('items'); if (!items) return;
    items.innerHTML = v.map(o => {
      const s = STATUS[o.status]||[o.status,'m']; const sc = (o.qualification||{}).score;
      const running = o.status==='RUNNING';
      return `<button class="item" type="button" data-k="${esc(key(o))}" aria-current="${key(o)===currentKey}">
        <div class="ring${running?' spin':''}" style="--p:${running?25:(sc??0)};--rc:${ringColor(o.status)}"><span>${running?'':(sc??'–')}</span></div>
        <div style="min-width:0;flex:1"><div class="nm">${esc(name(o))}</div><div class="dm">${esc(o.domain||o.input_raw||'')}</div></div>
        <span class="pill ${s[1]}">${esc(s[0])}</span></button>`; }).join('') || '<p class="empty" style="padding:16px">No companies yet.</p>';
    items.querySelectorAll('.item').forEach(b => b.onclick = () => { currentKey = b.dataset.k; renderList(); });
    renderDetail();
  }
  function verdict(o){
    const s = STATUS[o.status]||[o.status,'m']; const q = o.qualification||{}; const sc = q.score;
    const conf = o.confidence || q.confidence;
    const running = o.status==='RUNNING'||o.status==='QUEUED';
    return `<section class="panel"><div class="verdict">
      <div class="big${running?' spin':''}" style="--p:${running?25:(sc??0)};--rc:${ringColor(o.status)}"><span><div style="text-align:center">${running?'…':(sc??'–')}${running?'':'<small>of 100</small>'}</div></span></div>
      <div><div class="row"><span class="pill ${s[1]}">${esc(s[0])}</span>${conf?`<span class="pill i" title="How sure we are of the call (separate from score)">${esc(conf.toLowerCase())} confidence</span>`:''}</div>
        <h3>${esc(name(o))} <span class="small">${esc(o.domain||'')}</span></h3>
        <p class="why">${esc(o.status_reason || (running?'Reading the site, extracting facts and scoring. This usually takes 30-120 seconds.':''))}</p>
        ${o.next_action?`<div class="next"><b>Next step</b><span>${esc(o.next_action)}</span></div>`:''}</div></div></section>`;
  }
  function breakdown(o){
    const q = o.qualification||{}; const comps = (q.breakdown||{}).components||[];
    if(!comps.length) return '';
    const qc = {STRONG:'g',MEDIUM:'i',WEAK:'w'};
    const rows = comps.map(c => { const [t,sub] = COMP[c.component]||[c.component,''];
      const pct = c.max_points? Math.round(100*c.points/c.max_points):0;
      const col = pct>=75?'var(--good)':pct>=40?'var(--accent)':pct>0?'var(--warn)':'var(--line)';
      const ev = (c.evidence||[]);
      const evHtml = ev.length ? ev.map(e => `<div class="fl-ev${e.counted?' on':''}">
          <div class="top">${e.counted?`<span class="pill g">counted +${e.points}</span>`:`<span class="pill m">not counted</span>`}
            ${e.quality?`<span class="pill ${qc[e.quality]||'m'}">${esc(String(e.quality).toLowerCase())}</span>`:''}
            ${e.event_date?`<span class="small">happened ${fmtDate(e.event_date)}</span>`:''}</div>
          <div class="txt">${esc(e.text)}</div>${e.quote?`<q>${esc(e.quote)}</q>`:''}
          <div class="small fl-why">${esc(e.why)}${e.source_url?' · '+link(e.source_url):''}</div></div>`).join('')
        : `<div class="fl-ev none"><div class="txt">No evidence found for this part of the score.</div><div class="small">Missing facts stay empty: nothing is guessed.</div></div>`;
      return `<div class="fl-row">
        <div class="fl-score"><div class="fl-k">Score</div><div class="lbl">${esc(t)}<small>${esc(sub)}</small></div>
          <div class="pts">${c.points}<span>/${c.max_points}</span></div>
          <div class="track" role="img" aria-label="${c.points} of ${c.max_points}"><div class="fill" style="width:${pct}%;background:${col}"></div></div>
          <div class="small">bucket: ${esc(String(c.bucket).replace(/_/g,' '))}</div></div>
        <div class="fl-arrow" aria-hidden="true">←</div>
        <div class="fl-col"><div class="fl-k">Evidence</div>${evHtml}</div>
        <div class="fl-arrow" aria-hidden="true">→</div>
        <div class="fl-col"><div class="fl-k">Inference (rule applied)</div><div class="fl-inf">${esc(c.inference || c.reason)}</div></div>
      </div>`; }).join('');
    const total = comps.reduce((a,c)=>a+c.points,0);
    const gates = (q.gates||[]);
    const s = STATUS[o.status]||[o.status,'m'];
    const verdictPill = `<span class="pill ${(STATUS[q.status]||s)[1]}">${esc((STATUS[q.status]||s)[0])}</span>`;
    const decision = gates.length ? `<div class="fl-decision">
        <div class="fl-step"><div class="fl-k">Total score</div><div class="fl-big">${total}<span>/100</span></div>
          <div class="small">${comps.map(c=>`${(COMP[c.component]||[c.component])[0]} ${c.points}`).join(' + ')}</div></div>
        <div class="fl-arrow" aria-hidden="true">→</div>
        <div class="fl-step grow"><div class="fl-k">Qualification gates (all must pass)</div>
          <ul class="fl-gates">${gates.map(g=>`<li class="${g.passed?'ok':'no'}"><span aria-hidden="true">${g.passed?'✓':'✗'}</span> ${esc(g.gate)} <span class="small">(${esc(g.detail)})</span></li>`).join('')}</ul></div>
        <div class="fl-arrow" aria-hidden="true">→</div>
        <div class="fl-step"><div class="fl-k">Verdict</div>${verdictPill}<div class="small" style="margin-top:6px">${esc((q.reasons||[])[0]||'')}</div></div>
      </div>` : '';
    const mp = q.model_proposed_score!=null ? `<p class="small" style="margin-top:12px">The AI model suggested ${q.model_proposed_score}; the rule-based score is final${q.score_diff?` (difference ${q.score_diff>0?'+':''}${q.score_diff})`:''}. The model judges evidence; code decides the points.</p>` : '';
    return `<section class="panel"><h2>How the score was built: score ← evidence → inference</h2>
      <p class="small" style="margin-top:-6px">Each part of the score shows the evidence the agent found (with the exact quote and source), which pieces counted, and the rubric rule that turned them into points.</p>
      <div class="fl">${rows}</div>${decision}${mp}</section>`;
  }
  function evidence(o){
    const f = o.facts||{}; const sigs = [...(f.compliance_signals||[]), ...(f.enterprise_signals||[])];
    const hires = f.hiring_signals||[];
    if(!sigs.length && !hires.length && !(f.certifications_claimed||[]).length && !(f.conflicts||[]).length) return '';
    const qc = {STRONG:'g',MEDIUM:'i',WEAK:'w'};
    const cards = sigs.map(s => `<div class="evc"><div class="top"><span class="pill ${qc[s.evidence_quality]||'m'}">${esc(String(s.evidence_quality).toLowerCase())}</span>
        ${s.trigger_type?`<span class="chip">${esc(s.trigger_type.replace(/_/g,' '))}</span>`:''}
        <span class="small">happened: ${fmtDate(s.event_date)}${s.event_date?` (${daysAgo(s.event_date)})`:''} · seen: ${fmtDate((s.observed_at||'').slice(0,10))}</span></div>
        <div class="txt">${esc(s.signal)}</div>${s.snippet?`<q>${esc(s.snippet)}</q>`:''}<div class="small" style="margin-top:6px">${link(s.source_url)}</div></div>`).join('');
    const hl = hires.length? `<div class="evc"><div class="top"><span class="pill i">open roles</span><span class="small">${hires.length} listed</span></div>
        <div class="chips">${hires.slice(0,24).map(h=>`<span class="chip">${esc(h.title)}${h.event_date?' · '+fmtDate(h.event_date):''}</span>`).join('')}</div>
        <div class="small" style="margin-top:6px">${link(hires[0].source_url)}</div></div>`:'';
    const certs = (f.certifications_claimed||[]).length? `<div class="evc"><div class="top"><span class="pill m">already claims</span></div><div class="txt">${f.certifications_claimed.map(esc).join(', ')}</div></div>`:'';
    const conflicts = (f.conflicts||[]).length? `<div class="evc"><div class="top"><span class="pill w">conflict</span></div><div class="txt">${f.conflicts.map(esc).join('<br>')}</div></div>`:'';
    return `<section class="panel"><h2>All evidence collected</h2><div class="ev">${cards}${hl}${certs}${conflicts}</div></section>`;
  }
  function research(o){
    const q = o.qualification||{}; const rounds = q.rounds||[];
    const tl = rounds.map(r => `<li><div class="rt">${r.round===0?'Read the homepage':'Research round '+r.round}${r.score!=null?` · score ${r.score}`:''}</div>
       ${r.round>0?`<div class="small">Looked for: ${(r.missing_before||[]).map(x=>`<span class="chip miss">${esc(ROW[x]||x)}</span>`).join(' ')||'-'}</div>
       <div class="small" style="margin-top:4px">Fetched only: ${(r.fetched_urls||[]).map(u=>String(u).startsWith('search:')?'<span class="chip">web search</span>':link(u)).join(' · ')||'nothing'}</div>`:''}
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
    const pp = pains.length? `<div class="ev">${pains.map((p,i)=>`<div class="evc"><div class="top"><span class="pill ${p.confidence==='HIGH'?'g':p.confidence==='MEDIUM'?'i':'w'}">hypothesis ${i+1} · ${esc(String(p.confidence).toLowerCase())}</span><span class="small">${fmtDate(p.event_date)}</span></div>
        <div class="txt">${esc(p.hypothesis)}</div><div class="small" style="margin-top:4px">Why it matters to them: ${esc(p.why_it_matters_to_this_role)}</div>
        <div class="small" style="margin-top:4px">Based on: ${esc(p.signal)} · ${link(p.source_url)}</div></div>`).join('')}</div>`:'';
    const flags = (o.pain_flags||[]).filter(f=>String(f).startsWith('fewer')).length? `<p class="small">Only ${pains.length} evidence-backed pain point${pains.length===1?'':'s'}; we don't pad with generic filler.</p>`:'';
    if(!d && !pains.length) return '';
    const att = o.draft_attempts||[]; const last = att[att.length-1];
    const guard = last ? (last.errors.length? `<div class="guard"><span class="pill w">failed checks</span><span class="small">${last.errors.map(esc).join(' · ')}</span></div>`
                                   : `<div class="guard"><span class="pill g">passed all checks</span><span class="small">${last.words} words · attempt ${last.attempt} of 2</span></div>`) : '';
    const s = o.send||{};
    const canSend = opts.onSend && d && last && !last.errors.length && o.status==='DRAFTED';
    const sent = s.status==='SENT' ? `<span class="pill g">sent · ${esc(s.message_id||'')}</span>` : (s.status==='FAILED'?`<span class="pill b">${esc(REASON[s.reason]||s.reason||'send failed')}</span><span class="small">${esc(s.detail||'')}</span>`:'');
    const letter = d ? `<div class="letter"><div class="hdr"><b>To:</b> ${esc(s.to||'demo inbox')} <span class="small">(demo inbox only)</span><br>
        <b>Intended for:</b> ${esc((o.contact&&o.contact.chosen)?(o.contact.chosen.name+', '+(o.contact.chosen.position||'')):'–')}<br>
        <b>Subject:</b> ${esc(d.subject)}</div><div class="body" id="emailbody">${esc(d.email_body)}</div></div>
        <div class="row" style="margin-top:12px"><button class="btn" type="button" id="copy">Copy email text</button>
        ${canSend?`<button class="btn primary" type="button" id="sendbtn">${esc(opts.sendLabel||'Send to my demo inbox')}</button>`:''}${sent}${guard}</div>
        <details><summary>Every claim and its source (${(d.personalisation_evidence||[]).length})</summary><ul>${(d.personalisation_evidence||[]).map(e=>`<li>${esc(e.claim)} · ${link(e.source_url)}</li>`).join('')}</ul></details>` : '';
    return `<section class="panel"><h2>The email</h2>${pains.length?`<div style="margin-bottom:16px">${pp}${flags}</div>`:''}${letter}</section>`;
  }

  const FU_FIELDS = [['reply_status','\u2709'],['calendar_checked','\u25A6'],['times_proposed','\u2630'],
                     ['reply_round','\u21BB'],['meeting_status','\u25F7'],['crm_logged','\u25CE']];
  const FU_SCEN = [['accepts','Interested \u2192 accepts'],['counter_proposes','Interested \u2192 counter-proposes'],['not_interested','Not interested'],['no_reply','No reply']];
  const fuVal = v => Array.isArray(v) ? (v.length ? '[' + v.join(', ') + ']' : 'none') : String(v);
  function followup(o){
    if (!opts.onFollowup || !['DRAFTED','SENT'].includes(o.status)) return '';
    const f = o._followup;
    const ctl = `<div class="fu-ctl"><div class="small" style="margin-bottom:8px">Simulate the prospect's reply, or paste a real one. The agent classifies it (a branch, no tool call), then runs its tools.</div>
      <div class="row">${FU_SCEN.map(([k,l])=>`<button class="btn${f&&f._scenario===k&&!f._reply?' primary':''}" type="button" data-sc="${k}">${esc(l)}</button>`).join('')}</div>
      <div class="row" style="margin-top:10px;align-items:flex-start"><textarea id="fu-reply" rows="2" placeholder="...or paste the prospect's reply, e.g. Interested! Can we do Friday 3pm instead?" aria-label="Prospect reply">${esc((f&&f._reply)||'')}</textarea>
      <button class="btn" type="button" id="fu-run">Run on this reply</button></div></div>`;
    if (!f) return `<section class="panel"><h2>Follow-up agent (after the email)</h2>${ctl}</section>`;
    if (f.error) return `<section class="panel"><h2>Follow-up agent (after the email)</h2>${ctl}<p class="notice show err">${esc(f.error)}</p></section>`;
    const head = `<tr><th class="fu-step">Checkpoint <span>(workflow step)</span></th>${FU_FIELDS.map(([k,i])=>`<th><span class="fu-ico" aria-hidden="true">${i}</span>${esc(k)}</th>`).join('')}</tr>`;
    const body = f.checkpoints.map(cp => `<tr><td class="fu-step"><span class="fu-n">${cp.n}</span><div><b>${esc(cp.title)}</b><div class="small">(${esc(cp.subtitle)})</div></div></td>
      ${FU_FIELDS.map(([k])=>`<td><span class="fu-cell${cp.changed.includes(k)?' on':''}">${esc(fuVal(cp.state[k]))}</span></td>`).join('')}</tr>
      ${cp.detail?`<tr class="fu-det"><td></td><td colspan="${FU_FIELDS.length}">${esc(cp.detail)}</td></tr>`:''}`).join('');
    const fin = f.final_state;
    const cls = f.classified_reply ? `<p class="small">Reply classified as <b>${esc(f.classified_reply.intent)}</b> (evidence: "${esc(f.classified_reply.evidence)}")${f.classified_reply.proposed_time?` \u00b7 proposed ${esc(f.classified_reply.proposed_time)}`:''}.</p>` : '';
    const extra = (f.emails||[]).filter(e=>e.kind!=='cold_email');
    const emails = extra.map(e=>`<div class="letter" style="margin-top:10px"><div class="hdr"><b>To:</b> demo inbox only \u00b7 <b>Intended for:</b> ${esc(e.intended_recipient)}<br><b>Subject:</b> ${esc(e.subject)}</div><div class="body">${esc(e.body)}</div></div>`).join('');
    const crm = f.crm_record ? `<div class="evc" style="margin-top:10px"><div class="top"><span class="pill i">demo CRM record</span></div><div class="small">${Object.entries(f.crm_record).filter(([k,v])=>v!==null&&v!=='').map(([k,v])=>`<b>${esc(k)}</b>: ${esc(v)}`).join(' \u00b7 ')}</div></div>` : '';
    return `<section class="panel"><h2>Follow-up agent: the state object at every checkpoint</h2>
      <p class="small" style="margin-top:-6px">Same six fields. Watch them fill in as the agent moves through the workflow. Green = changed from the previous step.
        Calendar and CRM here are <b>demo systems</b> (nothing real is connected); emails go only to the demo inbox.</p>
      ${ctl}${cls}
      <div class="tablewrap"><table class="fu-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>
      <div class="fu-final"><span class="fu-legend"><span class="fu-cell on">&nbsp;</span> Changed from previous step</span>
        <span><b>Final state:</b> ${FU_FIELDS.map(([k])=>`<b>${esc(k)}</b>: ${esc(fuVal(fin[k]))}`).join(' \u00b7 ')}</span></div>
      ${emails||crm?`<details open><summary>What the agent produced (${extra.length} email${extra.length===1?'':'s'}${f.booked_slot?', 1 booking':''}${f.crm_record?', 1 CRM record':''})</summary>${emails}${crm}</details>`:''}
    </section>`;
  }

  const REVIEWABLE = ['NEEDS_RESEARCH','INSUFFICIENT_DATA','REJECTED','NO_CONTACT_FOUND','DRAFT_NEEDS_HUMAN'];
  function review(o){
    const h = o.human_override;
    const banner = h ? `<section class="panel override ${h.decision==='approve'?'ok':'no'}"><div class="row">
        <span class="pill ${h.decision==='approve'?'g':'b'}">${h.decision==='approve'?'Approved by a human':'Rejected by a human'}</span>
        <span class="small">${esc(h.by||'reviewer')} \u00b7 ${esc(new Date(h.at).toLocaleString())}</span></div>
        <p style="margin:8px 0 0">"${esc(h.reason)}"</p>
        <p class="small" style="margin:6px 0 0">The rules said <b>${esc((STATUS[h.original_status]||[h.original_status])[0])}</b>${h.original_score!=null?` (score ${h.original_score}/100)`:''}. That verdict and its evidence stay on record below.</p></section>` : '';
    if (!opts.onReview || h || !REVIEWABLE.includes(o.status)) return banner;
    const q = o.qualification||{};
    const hd = q.hard_disqualifier && q.hard_disqualifier !== 'already_certified' ? q.hard_disqualifier : null;
    const failed = (q.gates||[]).filter(g=>!g.passed).map(g=>g.gate);
    return banner + `<section class="panel review"><h2>Your decision</h2>
      <p style="margin:0 0 10px;color:var(--ink-2)">The rules did not qualify this company${q.score!=null?` (score ${q.score}/100`:''}${failed.length?`; not met: ${esc(failed.join(', '))}`:''}${q.score!=null?')':''}.
        If you know something the public evidence doesn't show, approve it and the agent will pick the decision-maker,
        write the pain points and draft the email. Your reason is logged with the decision.</p>
      ${hd?`<p class="notice show warn">Approval is blocked: "${esc(hd.replace(/_/g,' '))}" is a hard disqualifier. You can still reject it.</p>`:''}
      <textarea id="rv-reason" rows="2" placeholder="Why? e.g. Their CTO told us they start SOC 2 in Q1; renewal is coming up" aria-label="Reason for your decision"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" type="button" id="rv-approve"${hd?' disabled':''}>Approve &amp; continue: contact \u2192 draft email</button>
        <button class="btn" type="button" id="rv-reject">Reject</button>
        <span class="small" id="rv-msg"></span></div></section>`;
  }
  function issues(o){
    const f = o.failures||[]; const flags = o.flags||[];
    if(!f.length && !flags.length) return '';
    return `<section class="panel"><h2>Flags & reason codes</h2><div class="flags">${f.map(x=>`<div class="flag"><span class="pill ${x.reason==='score_diff'?'i':'w'}">${esc(x.step)}</span><div>${esc(REASON[x.reason]||x.reason)} <code>${esc(x.reason)}</code>${x.detail?`<div class="small">${esc(x.detail)}</div>`:''}</div></div>`).join('')}
      ${flags.map(x=>`<div class="flag"><span class="pill m">site</span><div>${esc(REASON[x]||x)} <code>${esc(x)}</code></div></div>`).join('')}</div></section>`;
  }
  function renderDetail(){
    const det = document.getElementById('detail'); if (!det) return;
    const o = OUT.find(x => key(x)===currentKey);
    if(!o){ det.innerHTML = `<section class="panel"><p class="empty">Results appear here as each company finishes.</p></section>`; return; }
    det.innerHTML = verdict(o) + review(o) + breakdown(o) + `<div class="two"><div class="detail">${person(o)}${research(o)}</div><div class="detail">${evidence(o)}</div></div>` + email(o) + followup(o) + issues(o);
    const c = document.getElementById('copy');
    if(c) c.onclick = () => { navigator.clipboard?.writeText(document.getElementById('emailbody').textContent).then(()=>{ c.textContent='Copied'; setTimeout(()=>c.textContent='Copy email text',1400); }); };
    det.querySelectorAll('[data-sc]').forEach(b => b.onclick = async () => { b.disabled = true; b.textContent = 'Running...';
      await opts.onFollowup(o, { scenario: b.dataset.sc }); renderList(); });
    const fr = document.getElementById('fu-run');
    if (fr) fr.onclick = async () => { const t = document.getElementById('fu-reply').value;
      fr.disabled = true; fr.textContent = 'Running...'; await opts.onFollowup(o, { reply_text: t }); renderList(); };
    const ra = document.getElementById('rv-approve'), rj = document.getElementById('rv-reject');
    const doReview = async (decision, btn) => {
      const reason = (document.getElementById('rv-reason').value || '').trim();
      const msg = document.getElementById('rv-msg');
      if (reason.length < 3) { msg.textContent = 'Please write a short reason first (it is logged).'; document.getElementById('rv-reason').focus(); return; }
      [ra, rj].forEach(b => b && (b.disabled = true));
      btn.textContent = decision === 'approve' ? 'Finding the contact and drafting… (up to 2 min)' : 'Saving…';
      await opts.onReview(o, decision, reason); renderList(); };
    if (ra) ra.onclick = () => doReview('approve', ra);
    if (rj) rj.onclick = () => doReview('reject', rj);
    const sb = document.getElementById('sendbtn');
    if(sb) sb.onclick = async () => { sb.disabled = true; sb.textContent = 'Sending…'; try { await opts.onSend(o); } finally { renderList(); } };
  }
  function wireGlobal(){
    const q = document.getElementById('q');
    if (q) q.addEventListener('input', e => { query = e.target.value.toLowerCase().trim(); renderList(true); });
    document.addEventListener('keydown', e => {
      if (['INPUT','TEXTAREA'].includes(e.target.tagName)) { if (e.key==='Escape') e.target.blur(); return; }
      const v = visible().map(key); const pos = v.indexOf(currentKey);
      if (e.key==='j' && pos < v.length-1){ currentKey = v[pos+1]; renderList(); }
      if (e.key==='k' && pos > 0){ currentKey = v[pos-1]; renderList(); }
      if (e.key==='/' && q){ e.preventDefault(); q.focus(); }
    });
    const th = document.getElementById('theme');
    if (th) th.onclick = () => { const r = document.documentElement;
      r.dataset.theme = r.dataset.theme === 'dark' ? 'light' : 'dark'; };
  }
  window.Desk = {
    init(payload, options) {
      D = payload || {}; OUT = (D.outcomes || []).slice(); Object.assign(opts, options || {});
      sortOut();
      const firstReady = OUT.find(o => (STATUS[o.status]||['','','x'])[2]==='ready');
      currentKey = firstReady ? key(firstReady) : (OUT[0] ? key(OUT[0]) : null);
      wireGlobal(); renderTop(); renderList();
    },
    reset(title) { OUT = []; D.title = title || D.title; D.generated = new Date().toISOString(); currentKey = null; filter = 'all'; renderTop(); renderList(); },
    upsert(o, select) {
      const i = OUT.findIndex(x => key(x)===key(o));
      if (i >= 0) OUT[i] = o; else OUT.push(o);
      sortOut();
      if (select || !currentKey) currentKey = key(o);
      renderTop(); renderList();
    },
    all() { return OUT.slice(); },
    setToday(t) { D.today = t; },
    today() { return D.today || null; },
    esc
  };
})();
