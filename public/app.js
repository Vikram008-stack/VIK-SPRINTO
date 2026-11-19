/* Live app: input -> /api/parse -> one /api/process call per company (2 at a time) -> Desk renders as results land. */
(function () {
  const $ = id => document.getElementById(id);
  const EXAMPLE = ['chargebee.com', 'thoughtworks.com', 'documenso.com', 'www.cal.com', 'cal.com', 'not a domain'].join('\n');
  const CONCURRENCY = 1;   // one company per request at a time: parallel heavy pages exhausted a serverless instance
  const TIMEOUT_MS = 295000;
  let running = false;
  let health = null;

  function notice(kind, html) {
    const n = $('notice');
    n.className = 'notice' + (html ? ' show ' + kind : '');
    n.innerHTML = html || '';
  }
  function progress(done, total) {
    const p = $('progress');
    p.classList.toggle('show', total > 0 && done < total);
    p.firstElementChild.style.width = total ? `${Math.round(100 * done / total)}%` : '0';
  }
  async function api(path, body, timeoutMs) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeoutMs || 30000);
    try {
      const r = await fetch(path, { method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined, signal: ctl.signal });
      let data = null;
      try { data = await r.json(); } catch (_) { data = { error: r.status === 504 ? 'timeout' : 'bad_response', message: r.status === 504 ? 'The server took too long on this company (HTTP 504). Run it again.' : `Server returned HTTP ${r.status}` }; }
      return { ok: r.ok, status: r.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: { error: e.name === 'AbortError' ? 'timeout' : 'network',
        message: e.name === 'AbortError' ? 'The request took too long and was stopped.' : 'Could not reach the server.' } };
    } finally { clearTimeout(t); }
  }
  function failRow(domain, raw, reason, message) {
    return { domain, input_raw: raw, status: 'FAILED', status_reason: message,
      next_action: 'Run this company again; if it repeats, check the server status',
      failures: [{ step: 'request', reason, detail: message }], cost: {}, seconds: 0 };
  }
  function setBusy(b) {
    running = b;
    ['demo', 'example', 'search'].forEach(id => { const el = $(id); if (el) el.disabled = b; });
    renderQueue();
    const has = Desk.all().some(o => !['QUEUED', 'RUNNING'].includes(o.status));
    $('results-bar').hidden = !has;
    $('results-title').textContent = has ? `${Desk.all().length} result${Desk.all().length === 1 ? '' : 's'}` : '';
    $('dl-json').disabled = b; $('dl-csv').disabled = b;
  }

  /* ---------- search box: names -> suggestions, links -> chips, file -> chips ---------- */
  const queue = [];                       // [{raw, domain, label, verified}]
  let sugg = [], active = -1, seq = 0, timer = null;
  const looksLikeLink = t => /^https?:\/\//i.test(t) || /\.[a-z]{2,}(\/|$)/i.test(t);
  function addItem(item, quiet) {
    const key = (item.domain || item.raw).toLowerCase();
    if (queue.some(x => (x.domain || x.raw).toLowerCase() === key)) { if (!quiet) notice('warn', `${Desk.esc(key)} is already in the list.`); return 'dup'; }
    if (queue.length >= (health ? health.max_domains : 10)) { if (!quiet) notice('warn', `Up to ${health ? health.max_domains : 10} companies per batch.`); return 'full'; }
    queue.push(item); renderQueue(); return 'added';
  }
  const normLink = t => t.replace(/^https?:\/\//i, '').replace(/^www\./i, '').split(/[/?#]/)[0].toLowerCase();
  async function addRaw(text) {
    const tokens = text.split(/[\n,;\t]+/).map(t => t.trim().replace(/^["']|["']$/g, ''))
      .filter(t => t && !/^(domains?|websites?|urls?|company|companies|name)$/i.test(t));
    const c = { added: 0, dup: 0, full: 0, notfound: 0 };
    for (const t of tokens) {
      let item;
      if (looksLikeLink(t)) item = { raw: t, domain: normLink(t), label: null, verified: true };
      else {                                                     // a company name: look it up
        const r = await api('/api/resolve?q=' + encodeURIComponent(t), null, 15000);
        const top = r.ok && (r.data.suggestions || []).find(x => x.verified);
        if (top) item = { raw: top.domain, domain: top.domain, label: top.name, verified: true };
        else { item = { raw: t, domain: null, label: null, verified: false }; c.notfound++; }
      }
      const res = addItem(item, true); c[res] = (c[res] || 0) + 1;
    }
    const parts = [`Added ${c.added}`];
    if (c.dup) parts.push(`${c.dup} duplicate${c.dup > 1 ? 's' : ''} skipped`);
    if (c.full) parts.push(`${c.full} over the limit`);
    if (c.notfound) parts.push(`${c.notfound} not found (kept so you can see how it's handled)`);
    notice(c.notfound || c.dup || c.full ? 'warn' : 'ok', parts.join(' · ') + '.');
    return c;
  }
  function renderQueue() {
    const q = $('queue');
    q.innerHTML = queue.map((x, i) => `<span class="qchip${x.verified ? '' : ' unv'}" title="${x.verified ? '' : 'Unverified: check this is the right company'}">
        ${x.label ? `<b>${Desk.esc(x.label)}</b>` : ''}<span>${Desk.esc(x.domain || x.raw)}</span>${x.verified ? '' : (x.domain ? '<em>unverified</em>' : '<em>not found</em>')}
        <button type="button" data-rm="${i}" aria-label="Remove ${Desk.esc(x.domain || x.raw)}"${running ? ' disabled' : ''}>&times;</button></span>`).join('');
    q.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => { queue.splice(+b.dataset.rm, 1); renderQueue(); $('search').focus(); });
    const run = $('run');
    run.disabled = running || !queue.length;
    run.textContent = running ? 'Researching…' : (queue.length ? `Research ${queue.length} compan${queue.length === 1 ? 'y' : 'ies'}` : 'Research companies');
  }
  function showSuggest(list, msg) {
    const ul = $('suggest'); sugg = list; active = list.length ? 0 : -1;
    if (!list.length && !msg) { ul.hidden = true; $('search').setAttribute('aria-expanded', 'false'); return; }
    ul.innerHTML = list.map((x, i) => `<li role="option" id="sg-${i}" data-i="${i}" aria-selected="${i === active}">
        <span class="sg-name">${Desk.esc(x.name)}</span><span class="sg-dom">${Desk.esc(x.domain)}</span>
        ${x.verified ? `<span class="sg-src">${Desk.esc(x.source === 'link' ? 'link' : 'official site · ' + x.source)}</span>` : '<span class="sg-src unv">unverified guess</span>'}
        ${x.description ? `<span class="sg-desc">${Desk.esc(x.description)}</span>` : ''}</li>`).join('')
      + (msg ? `<li class="sg-msg">${Desk.esc(msg)}</li>` : '');
    ul.hidden = false; $('search').setAttribute('aria-expanded', 'true');
    ul.querySelectorAll('li[data-i]').forEach(li => li.onmousedown = e => { e.preventDefault(); pick(+li.dataset.i); });
  }
  function highlight() {
    $('suggest').querySelectorAll('li[data-i]').forEach(li => li.setAttribute('aria-selected', String(+li.dataset.i === active)));
  }
  function pick(i) {
    const x = sugg[i]; if (!x) return;
    addItem({ raw: x.domain, domain: x.domain, label: x.source === 'link' ? null : x.name, verified: !!x.verified });
    $('search').value = ''; $('sclear').hidden = true; showSuggest([]); $('search').focus();
  }
  async function lookup(q) {
    const my = ++seq;
    showSuggest([], 'Searching…');
    const r = await api('/api/resolve?q=' + encodeURIComponent(q), null, 15000);
    if (my !== seq) return;                                   // a newer keystroke won
    if (!r.ok) { showSuggest([], (r.data && r.data.message) || 'Lookup failed. Paste the website link instead.'); return; }
    showSuggest(r.data.suggestions || [], r.data.message || null);
  }
  function onSearchInput() {
    const v = $('search').value; $('sclear').hidden = !v;
    clearTimeout(timer);
    const t = v.trim();
    if (t.length < 2 || looksLikeLink(t)) { showSuggest(looksLikeLink(t) ? [] : [], looksLikeLink(t) ? 'Press Enter to add this link' : null); return; }
    timer = setTimeout(() => lookup(t), 350);
  }
  function onSearchKey(e) {
    const v = $('search').value.trim();
    if (e.key === 'ArrowDown' && sugg.length) { e.preventDefault(); active = (active + 1) % sugg.length; highlight(); }
    else if (e.key === 'ArrowUp' && sugg.length) { e.preventDefault(); active = (active - 1 + sugg.length) % sugg.length; highlight(); }
    else if (e.key === 'Escape') { showSuggest([]); }
    else if (e.key === 'Backspace' && !v && queue.length) { queue.pop(); renderQueue(); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (!v && queue.length) { runBatch(); return; }
      if (looksLikeLink(v)) { addItem({ raw: v, domain: normLink(v), label: null, verified: true }); $('search').value = ''; $('sclear').hidden = true; showSuggest([]); return; }
      if (sugg.length && active >= 0) { pick(active); return; }
      if (v.length >= 2) lookup(v);
    }
  }
  function onSearchPaste(e) {
    const t = (e.clipboardData || window.clipboardData).getData('text');
    if (/[\n,;\t]/.test(t.trim())) { e.preventDefault(); addRaw(t); }
  }

  async function loadHealth() {
    const r = await api('/api/health', null, 15000);
    const el = $('svc');
    if (!r.ok) { el.textContent = 'server unreachable'; el.className = 'tag'; return; }
    health = r.data; const s = health.services || {};
    $('maxd').textContent = health.max_domains;
    const missing = [];
    if (!s.gemini) missing.push('AI model');
    if (!s.scrapingbee) missing.push('ScrapingBee (using plain HTTP)');
    if (!s.hunter_live) missing.push('live Hunter (mock contacts)');
    if (!s.gmail) missing.push('Gmail send (drafts only)');
    el.textContent = missing.length ? `limited: ${missing.join(', ')}` : 'all services connected';
    el.title = JSON.stringify(s);
    if (!s.gemini) notice('warn', 'The AI model key is not configured on this server, so live research is unavailable. ' +
      'Use <b>Run offline demo</b> to see the full flow on fictional companies.');
  }

  async function runBatch() {
    if (running) return;
    if (!queue.length) { notice('warn', 'Add at least one company first: search a name or paste a link.'); $('search').focus(); return; }
    const text = queue.map(x => x.domain || x.raw).join('\n');
    notice('', '');
    const p = await api('/api/parse', { text }, 20000);
    if (!p.ok) { notice('err', Desk.esc(p.data.message || 'Could not read the input.')); $('search').focus(); return; }
    queue.length = 0; renderQueue();
    const { unique, duplicates, invalid, truncated, message } = p.data;
    Desk.reset('Live research');
    invalid.forEach(x => Desk.upsert({ _key: 'invalid:' + x.raw, domain: x.raw, input_raw: x.raw, status: 'INVALID',
      status_reason: x.error, next_action: 'Check the spelling; use a bare domain like acme.com',
      failures: [{ step: 'input', reason: 'invalid_domain', detail: x.error }] }));
    duplicates.forEach(x => Desk.upsert({ _key: 'dup:' + x.raw, domain: x.domain, input_raw: x.raw, status: 'DUPLICATE',
      status_reason: `"${x.raw}" repeats "${(unique.find(u => u.domain === x.domain) || {}).raw || x.domain}" (both are ${x.domain}); researched once`,
      next_action: 'No action needed',
      failures: [{ step: 'input', reason: 'duplicate_input', detail: `${x.raw} normalises to ${x.domain}` }] }));
    const msgs = [];
    if (message) msgs.push(Desk.esc(message));
    if (invalid.length) msgs.push(`${invalid.length} line${invalid.length > 1 ? 's were' : ' was'} not a valid domain and ${invalid.length > 1 ? 'were' : 'was'} skipped.`);
    if (duplicates.length) msgs.push(`${duplicates.length} duplicate${duplicates.length > 1 ? 's' : ''} removed.`);
    if (!unique.length) { notice('warn', msgs.join(' ') || 'No valid domains to research.'); setBusy(false); return; }
    notice(msgs.length ? 'warn' : 'ok', (msgs.join(' ') + ` Researching ${unique.length} compan${unique.length > 1 ? 'ies' : 'y'}. ` +
      'Companies are researched one at a time (about 15-90 seconds each); results appear as they finish.').trim());
    unique.forEach(u => Desk.upsert({ domain: u.domain, input_raw: u.raw, status: 'QUEUED', status_reason: 'Waiting for a free slot' }));
    setBusy(true);
    let done = 0; progress(0, unique.length);
    const work = unique.slice();            // NOT named `queue`: that would shadow the chip list above (TDZ crash)
    async function worker() {
      while (work.length) {
        const u = work.shift();
        Desk.upsert({ domain: u.domain, input_raw: u.raw, status: 'RUNNING' });
        let r = await api('/api/process', { domain: u.raw }, TIMEOUT_MS);
        const transient = x => !x.ok && (!x.data || !x.data.outcome) && (x.status === 0 || x.status >= 500 ||
          ['bad_response', 'network', 'timeout'].includes((x.data || {}).error));
        if (transient(r)) {                     // one automatic retry for a dropped or timed-out request
          Desk.upsert({ domain: u.domain, input_raw: u.raw, status: 'RUNNING', status_reason: 'Retrying once…' });
          r = await api('/api/process', { domain: u.raw }, TIMEOUT_MS);
        }
        let o;
        if (r.ok && r.data && r.data.status) o = r.data;
        else if (r.data && r.data.outcome) o = r.data.outcome;
        else o = failRow(u.domain, u.raw, r.data.error === 'timeout' ? 'timeout' : 'request_failed',
          (r.data && r.data.message) || 'The request failed.');
        o.domain = o.domain || u.domain;
        Desk.upsert(o, done === 0);
        done++; progress(done, unique.length);
      }
    }
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, unique.length) }, worker));
    setBusy(false);
    const all = Desk.all();
    const ready = all.filter(o => ['DRAFTED', 'SENT'].includes(o.status)).length;
    notice('ok', `Done. ${ready} draft${ready === 1 ? '' : 's'} ready for review, ` +
      `${all.filter(o => ['NEEDS_RESEARCH', 'INSUFFICIENT_DATA', 'NO_CONTACT_FOUND', 'DRAFT_NEEDS_HUMAN', 'FAILED'].includes(o.status)).length} need a human, ` +
      `${all.filter(o => o.status === 'REJECTED').length} not a fit.`);
  }

  async function runDemo() {
    if (running) return;
    setBusy(true); notice('ok', 'Running the offline demo: fictional companies, recorded model output, zero API calls…');
    const r = await api('/api/demo', {}, 60000);
    setBusy(false);
    if (!r.ok) { notice('err', Desk.esc(r.data.message || 'Demo failed.')); return; }
    Desk.reset('Offline demo (fictional companies)');
    Desk.setToday(r.data.today);
    r.data.outcomes.forEach((o, i) => { if (o.status === 'DUPLICATE') o._key = 'dup:' + o.input_raw; Desk.upsert(o, i === 0); });
    setBusy(false);
    notice('ok', 'Offline demo loaded: fictional companies, zero API calls. Open one on the left.');
    setBusy(false);
  }

  async function sendOne(o) {
    const r = await api('/api/send', { outcome: o }, 60000);
    if (r.ok && r.data.status === 'SENT') {
      o.send = r.data; o.status = 'SENT'; o.status_reason = `Sent to the demo inbox (message ${r.data.message_id})`;
      notice('ok', `Sent to the demo inbox. ${Desk.esc(r.data.intended_recipient || '')} appears only as a header line.`);
    } else {
      const d = r.data || {};
      o.send = { status: 'FAILED', reason: d.reason || d.error, detail: d.detail || d.message || (d.errors || []).join('; '), to: d.to };
      notice('err', Desk.esc(d.message || d.detail || 'Send failed.'));
    }
    Desk.upsert(o, true);
  }

  async function reviewOne(o, decision, reason) {
    notice('ok', decision === 'approve' ? `Approved ${Desk.esc(o.domain)}: the agent is choosing the contact and drafting the email…` : `Rejecting ${Desk.esc(o.domain)}…`);
    const r = await api('/api/approve', { outcome: o, decision, reason }, TIMEOUT_MS);
    if (r.ok && r.data && r.data.status) {
      const n = r.data; n._key = o._key; Desk.upsert(n, true);
      notice(n.status === 'DRAFTED' ? 'ok' : (n.status === 'REJECTED' ? 'ok' : 'warn'),
        `${Desk.esc(o.domain)}: ${Desk.esc(n.status_reason || n.status)}`);
    } else {
      notice('err', Desk.esc((r.data && r.data.message) || 'The decision could not be saved.'));
      Desk.upsert(o, true);
    }
  }

  async function followupOne(o, args) {
    const body = Object.assign({ outcome: o, today: Desk.today() }, args);
    const r = await api('/api/followup', body, 60000);
    if (r.ok) { o._followup = Object.assign(r.data, { _scenario: args.scenario || null, _reply: args.reply_text || null }); }
    else { o._followup = { error: (r.data && r.data.message) || 'Follow-up failed.', _reply: args.reply_text || null }; }
    Desk.upsert(o, true);
  }

  function download(name, text, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function csv() {
    const rows = [['domain', 'status', 'score', 'confidence', 'reason', 'contact', 'contact_title', 'subject', 'next_action']];
    Desk.all().forEach(o => {
      const ch = ((o.contact || {}).chosen) || {};
      rows.push([o.domain, o.status, (o.qualification || {}).score ?? '', o.confidence || '', o.status_reason || '',
        ch.name || '', ch.position || '', (o.draft || {}).subject || '', o.next_action || '']);
    });
    return rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n');
  }

  $('run').onclick = runBatch;
  $('demo').onclick = runDemo;
  $('example').onclick = () => addRaw(EXAMPLE);
  $('search').addEventListener('input', onSearchInput);
  $('search').addEventListener('keydown', onSearchKey);
  $('search').addEventListener('paste', onSearchPaste);
  $('search').addEventListener('blur', () => setTimeout(() => showSuggest([]), 150));
  $('sclear').onclick = () => { $('search').value = ''; $('sclear').hidden = true; showSuggest([]); $('search').focus(); };
  $('file').onchange = e => {
    const f = e.target.files[0]; if (!f) return;
    if (f.size > 200000) { notice('err', 'That file is too large (max 200 KB).'); return; }
    const rd = new FileReader();
    rd.onload = () => { notice('ok', `Reading ${Desk.esc(f.name)}…`); addRaw(String(rd.result)); };
    rd.onerror = () => notice('err', 'Could not read that file.');
    rd.readAsText(f); e.target.value = '';
  };
  $('dl-json').onclick = () => download('sprinto-outreach-results.json', JSON.stringify(Desk.all(), null, 2), 'application/json');
  $('dl-csv').onclick = () => download('sprinto-outreach-summary.csv', csv(), 'text/csv');

  Desk.init({ title: 'Ready', outcomes: [] }, { onSend: sendOne, sendLabel: 'Send to my demo inbox', onFollowup: followupOne, onReview: reviewOne });
  renderQueue();
  loadHealth();
})();
