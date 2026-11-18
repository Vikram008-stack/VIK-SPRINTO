/* Live app: input -> /api/parse -> one /api/process call per company (2 at a time) -> Desk renders as results land. */
(function () {
  const $ = id => document.getElementById(id);
  const EXAMPLE = ['chargebee.com', 'thoughtworks.com', 'documenso.com', 'www.cal.com', 'cal.com', 'not a domain'].join('\n');
  const CONCURRENCY = 2;
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
      try { data = await r.json(); } catch (_) { data = { error: 'bad_response', message: `Server returned HTTP ${r.status}` }; }
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
    ['run', 'demo', 'example', 'file'].forEach(id => { const el = $(id); if (el) el.disabled = b; });
    $('run').textContent = b ? 'Researching…' : 'Research these companies';
    const has = Desk.all().some(o => !['QUEUED', 'RUNNING'].includes(o.status));
    $('dl-json').disabled = b || !has; $('dl-csv').disabled = b || !has;
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
    const text = $('domains').value;
    notice('', '');
    const p = await api('/api/parse', { text }, 20000);
    if (!p.ok) { notice('err', Desk.esc(p.data.message || 'Could not read the input.')); $('domains').focus(); return; }
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
      'Each takes about 30-120 seconds; results appear as they finish.').trim());
    unique.forEach(u => Desk.upsert({ domain: u.domain, input_raw: u.raw, status: 'QUEUED', status_reason: 'Waiting for a free slot' }));
    setBusy(true);
    let done = 0; progress(0, unique.length);
    const queue = unique.slice();
    async function worker() {
      while (queue.length) {
        const u = queue.shift();
        Desk.upsert({ domain: u.domain, input_raw: u.raw, status: 'RUNNING' });
        const r = await api('/api/process', { domain: u.raw }, TIMEOUT_MS);
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
    notice('ok', 'Offline demo loaded. Open a company on the left to see the verdict, evidence, person and email.');
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
  $('example').onclick = () => { $('domains').value = EXAMPLE; $('domains').focus(); };
  $('file').onchange = e => {
    const f = e.target.files[0]; if (!f) return;
    if (f.size > 200000) { notice('err', 'That file is too large (max 200 KB).'); return; }
    const rd = new FileReader();
    rd.onload = () => { $('domains').value = String(rd.result); notice('ok', `Loaded ${Desk.esc(f.name)}. Press Research.`); };
    rd.onerror = () => notice('err', 'Could not read that file.');
    rd.readAsText(f); e.target.value = '';
  };
  $('domains').addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') runBatch(); });
  $('dl-json').onclick = () => download('sprinto-outreach-results.json', JSON.stringify(Desk.all(), null, 2), 'application/json');
  $('dl-csv').onclick = () => download('sprinto-outreach-summary.csv', csv(), 'text/csv');

  Desk.init({ title: 'Ready', outcomes: [] }, { onSend: sendOne, sendLabel: 'Send to my demo inbox' });
  loadHealth();
})();
