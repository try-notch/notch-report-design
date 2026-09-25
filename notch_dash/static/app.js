'use strict';
// notch_dash page: polls api/snapshot (DASHBOARD.md › Snapshot contract) and renders it.
// Log text, paths and user agents come from the internet, so data reaches the DOM only as text
// nodes, constant attributes, or numbers through CSSOM custom properties. Never innerHTML.

const NS = 'http://www.w3.org/2000/svg';
const $ = id => document.getElementById(id);
const st = x => ['ok', 'warn', 'critical', 'unknown'].includes(x) ? x : 'unknown';
let S = null, G = 0, lastAt = 0, fails = 0, timer = 0, inflight = false, dead = false;

// ---- DOM: build fresh nodes, then morph them into the page so focus, open <details>,
// text selection and running animations survive a poll.
function h(tag, attrs, ...kids) {
  const el = tag.startsWith('svg:') ? document.createElementNS(NS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('--')) el.style.setProperty(k, v); else el.setAttribute(k, v);
  }
  el.append(...norm(kids));
  return el;
}
const norm = xs => xs.flat(Infinity).filter(x => x != null && x !== false && x !== '')
  .map(x => x instanceof Node ? x : document.createTextNode(String(x)));
function kids(el, list) {
  list = norm(list);
  list.forEach((k, i) => el.childNodes[i] ? morph(el.childNodes[i], k) : el.append(k));
  while (el.childNodes.length > list.length) el.lastChild.remove();
}
function morph(o, k) {
  if (o.nodeName !== k.nodeName) return o.replaceWith(k);
  if (o.nodeType !== 1) { if (o.data !== k.data) o.data = k.data; return; }
  for (const { name } of [...o.attributes]) if (!k.hasAttribute(name) && name !== 'open') o.removeAttribute(name);
  for (const { name, value } of k.attributes) {
    if (o.getAttribute(name) !== value) name === 'style' ? (o.style.cssText = k.style.cssText) : o.setAttribute(name, value);
  }
  kids(o, [...k.childNodes]);
}
// Breathing and skeleton pulses share one clock, so a re-rendered element never restarts mid-breath.
const sync = () => document.getAnimations?.().forEach(a => a instanceof CSSAnimation && (a.startTime = 0));

// ---- Formats (DASHBOARD.md › Formats)
const n = x => x == null ? '—' : Number(x).toLocaleString('en-US');
const pct = f => `${(Math.min(1, Math.max(0, f || 0)) * 100).toFixed(2)}%`;
const sum = o => Object.values(o || {}).reduce((a, b) => a + (+b || 0), 0);
const dot = (...xs) => xs.filter(x => x != null && x !== false && x !== '').join(' · ') || null;
const pad = x => String(x).padStart(2, '0');
const plural = (c, w) => `${n(c)} ${w}${c === 1 ? '' : 's'}`;
function dur(ms) {
  if (ms == null) return '—';
  ms = Math.max(0, +ms);
  if (ms < 10) return `${ms ? ms.toFixed(1) : 0} ms`;
  if (ms < 1e3) return `${Math.round(ms)} ms`;
  if (ms < 6e4) return `${(ms / 1e3).toFixed(1)} s`;
  const m = Math.floor(ms / 6e4), hr = Math.floor(m / 60), d = Math.floor(hr / 24);
  return m < 60 ? `${m} min` : d ? `${plural(d, 'day')} ${hr % 24} h` : `${hr} h ${m % 60} min`;
}
function ago(s) {
  s = Math.max(0, s);
  return s < 60 ? `${Math.floor(s)} s` : s < 3600 ? `${Math.floor(s / 60)} min`
    : s < 86400 ? `${Math.floor(s / 3600)} h` : plural(Math.floor(s / 86400), 'day');
}
const since = at => ago(G - at);
const hm = at => { const d = new Date(at * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const clock = at => `${hm(at)}:${pad(new Date(at * 1000).getSeconds())}`;
function when(at) {
  if (at == null) return '—';
  const d = new Date(at * 1000);
  return d.toDateString() === new Date(G * 1000).toDateString() ? clock(at)
    : `${d.toLocaleString('en-US', { month: 'short' })} ${d.getDate()} ${hm(at)}`;
}
const usd = x => x == null ? '—' : x > 0 && x < .01 ? '<$0.01' : `$${Number.isInteger(+x) ? x : (+x).toFixed(2)}`;
const usd4 = x => x == null ? '—' : x > 0 && x < 1e-4 ? '<$0.0001' : `$${(+x).toFixed(4)}`;
function bytes(b) {
  if (b == null) return '—';
  let i = 0;
  while (b >= 1000 && i < 4) { b /= 1000; i++; }
  return `${i ? b.toFixed(1) : b} ${['B', 'kB', 'MB', 'GB', 'TB'][i]}`;
}

// ---- Pieces
const glyph = (k, label, cls = '') => h('svg:svg', { class: `g s-${k} ${cls}`, ...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': 'true' }) }, h('svg:use', { href: `#g-${k}` }));
const pill = (k, word) => h('span', { class: `pill s-${k}` }, glyph(k), h('span', {}, word));
const card = (...xs) => h('div', { class: 'card' }, xs);
const none = text => h('p', { class: 'none' }, glyph('unknown'), h('span', {}, text));
const empty = text => h('p', { class: 'empty' }, text);
// Attacker-length paths keep their full text in the DOM but show at most a few lines.
const clamp = (text, lines = 2) => h('span', { class: `clamp l${lines}` }, text);
const head = (title, aside) => h('div', { class: 'card-head' }, h('h3', {}, title), aside && h('span', { class: 'aside' }, aside));
const reason = key => S.sources?.[key]?.reason;
const unit = v => { const m = /^(.*\d) (\D+)$/.exec(v); return m ? [m[1], h('small', {}, ` ${m[2]}`)] : [v]; };
const metric = (v, cap, sub, flag) => h('div', { class: 'metric' },
  h('span', { class: 'v' }, flag && glyph(flag, 'some failed'), unit(v)), h('span', { class: 'c' }, cap), sub && h('span', { class: 'c' }, sub));
// cols: [label, class]; rows: [[value, class, attrs], …]. Cells carry data-l labels and --o order for the narrow reflow.
const table = (caption, cols, rows) => h('table', { class: 'tbl' }, h('caption', { class: 'vh' }, caption),
  h('thead', {}, h('tr', {}, cols.map(([t, c]) => h('th', { scope: 'col', class: c }, t)))),
  h('tbody', {}, rows.map(r => h('tr', {}, r.map(([v, c, a]) => h('td', { class: c, ...a }, v))))));

// ---- Status
const CHECKS = [['phone', 'Phone'], ['tunnel', 'Tunnel'], ['gate', 'The gate'], ['server', 'Server'], ['worker', 'Worker'], ['openrouter', 'OpenRouter']];
const FACTS = {
  phone: c => [dot(c.last_route, c.requests_1h != null && `${n(c.requests_1h)} in the last hour`),
    c.device && dot(c.device.model, c.device.os && `iOS ${c.device.os}`, c.device.pairing,
      c.device.last_connected_at != null && `last on this Mac ${since(c.device.last_connected_at)} ago`)],
  tunnel: c => [c.host,
    dot(c.edge, c.rtt_ms != null && `rtt ${dur(c.rtt_ms)}`, c.probe?.latency_ms != null && `e2e ${dur(c.probe.latency_ms)}`),
    dot(c.version && `cloudflared ${c.version}`, c.requests_total != null && `${n(c.requests_total)} requests`,
      c.request_errors != null && `${n(c.request_errors)} errors`),
    c.phone_host && c.host && c.phone_host !== c.host && `phone uses ${c.phone_host}`],
  gate: c => [c.integrity && dot(`/healthz ${c.integrity.healthz ?? 'no answer'}`, `/docs ${c.integrity.docs ?? 'no answer'}`,
    `checked ${since(c.integrity.at)} ago`), c.blocked_24h != null && `${n(c.blocked_24h)} blocked in 24 h`],
  server: c => [dot(`127.0.0.1:${c.port}`, c.latency_ms != null && dur(c.latency_ms)), c.started_at != null && `up ${dur((G - c.started_at) * 1e3)}`],
  worker: c => c.captures ? [
    dot(`${n(c.captures.queued)} waiting`, `${n(c.captures.transcribing)} hearing`, `${n(c.captures.analyzing)} making sense`,
      sum(c.reports) > 0 && `${plural(sum(c.reports), 'report')} working`),
    c.oldest_pending_ms != null && `oldest ${dur(c.oldest_pending_ms)}`,
    `${n(c.done_24h)} done · ${n(c.failed_24h)} failed in 24 h`] : [],
  openrouter: c => [c.limit_usd > 0 && c.total_usd != null &&
    h('div', { class: `track s-${st(c.status)}`, 'aria-hidden': 'true' }, h('span', { '--w': pct(c.total_usd / c.limit_usd) })),
    c.week_usd != null && `${usd(c.week_usd)} this week`],
};
const strip = s => CHECKS.map(([key, label]) => {
  const c = s.checks[key] || {};
  return h('div', { class: 'tile' },
    h('div', { class: 'tile-head' }, h('h3', {}, label), pill(c.busy && c.status === 'ok' ? 'busy' : st(c.status), c.word)),
    h('p', { class: 'reason' }, c.reason),
    FACTS[key](c).map(f => f instanceof Node ? f : f && h('p', { class: 'fact mono' }, f)));
});

// ---- Notches
const RECORD_DOWN = 'Couldn’t read the record. Your notches are safe. Only this read failed.';
const ROW = { queued: 'Waiting', transcribing: 'Hearing it…', analyzing: 'Making sense of it…', complete: 'Written', failed: 'Not written' };
const PHASE = { wait: ['Waiting', 'Waited', 'Couldn’t start'], stt: ['Hearing it…', 'Heard', 'Couldn’t hear it'],
  classify: ['Sorting it…', 'Sorted', 'Couldn’t sort it'], chat: ['Writing it up…', 'Written', 'Couldn’t write it up'],
  run: ['Making sense of it…', 'Done', 'Couldn’t finish'] };
const MOOD = { up: 'Up', flat: 'Flat', down: 'Down' };
function notches(s) {
  const p = s.pipeline;
  if (!p) return card(none(reason('db') || RECORD_DOWN));
  const c = p.counts_24h;
  return h('div', { class: 'card clip' },
    h('p', { class: 'lead' }, `${n(c.notches)} in 24 h · ${n(c.in_flight)} working · ${n(c.failed)} not written`),
    p.rows.length ? h('ol', { class: 'rows' }, p.rows.map(row)) : empty('No notches yet. The next one shows up here as it happens.'));
}
function row(r) {
  const k = r.state === 'complete' ? 'ok' : r.state === 'failed' ? 'critical' : ROW[r.state] ? 'busy' : 'unknown';
  const meta = norm([MOOD[r.mood] && h('span', {}, MOOD[r.mood]), (r.tags || []).map(t => h('span', { class: 'tag' }, t)),
    r.attempts > 1 && h('span', {}, `attempt ${r.attempts}`), r.audio_on_disk === false && h('span', {}, 'audio not on disk'),
    r.note && h('span', { class: 'n-note' }, r.note)]);
  return h('li', { class: `nrow${k === 'busy' ? ' busy' : ''}` },
    h('span', { class: 'n-t mono' }, when(r.submitted_at ?? r.recorded_at)),
    pill(k, ROW[r.state] || r.state),
    fall(r),
    h('span', { class: 'n-el mono' }, dur(r.elapsed_ms)),
    h('span', { class: 'n-rec mono' }, r.recording_ms != null && `rec ${dur(r.recording_ms)}`),
    h('span', { class: 'n-w mono' }, r.words != null && plural(r.words, 'word')),
    (r.summary || meta.length > 0) && h('div', { class: 'n-more' }, r.summary && h('p', { class: 'n-sum' }, r.summary),
      meta.length > 0 && h('p', { class: 'n-meta' }, meta)));
}
function fall(r) {
  const ph = r.phases || [], name = p => PHASE[p.name] ? p.name : 'run';
  const i = p => ({ running: 0, done: 1, failed: 2 })[p.state] ?? 1;
  const max = Math.max(r.elapsed_ms || 0, ...ph.map(p => p.start_ms + p.ms), 1);
  return h('div', { class: 'fall' },
    h('div', { class: 'lanes', 'aria-hidden': 'true' }, ph.map(p => h('div', { class: 'lane' },
      h('span', { class: `seg p-${name(p)} ${['running', 'done', 'failed'][i(p)]}`, '--x': pct(p.start_ms / max), '--w': pct(p.ms / max) })))),
    ph.length > 0 && h('p', { class: 'cap mono', 'aria-hidden': 'true' }, ph.map(p => `${p.name} ${dur(p.ms)}${['…', '', ' failed'][i(p)]}`).join(' · ')),
    ph.length > 0 && h('p', { class: 'vh' }, ph.map(p => `${PHASE[name(p)][i(p)]} ${dur(p.ms)}`).join(', ')));
}

// ---- Traffic
const CLASSES = [['2xx', 'ok'], ['3xx', 'n3'], ['4xx', 'warn'], ['5xx', 'critical']];
function traffic(s) {
  const t = s.traffic;
  if (!t) return card(none(reason('caddy_log') || 'The Caddy log isn’t available.'));
  return [card(head('Requests per minute'),
      h('p', { class: 'lead' }, `${plural(t.requests_1h, 'request')} in the last hour · ${n(t.errors_1h)} with an error`),
      h('p', { class: 'fact mono' }, `${n(t.by_source_1h?.gate)} through the gate · ${n(t.by_source_1h?.local)} on this Mac`),
      chart(t.hour)),
    card(head('Routes', 'Last 24 hours'), t.routes.length ? table('Routes in the last 24 hours',
      [['Route', ''], ['Count', 'num w5'], ['Errors', 'num w5'], ['p50', 'num w6'], ['p95', 'num w6']],
      t.routes.map(r => [[clamp(r.route), 'mono full'], [n(r.count), 'num', { 'data-l': 'count' }],
        [n(r.errors), `num${r.errors ? '' : ' z'}`, { 'data-l': 'errors' }], [dur(r.p50_ms), 'num d-only'], [dur(r.p95_ms), 'num d-only'],
        [`${dur(r.p50_ms)} / ${dur(r.p95_ms)}`, 'num m-only', { 'data-l': 'p50 / p95' }]]))
      : empty('No requests in the last 24 hours.'))];
}
function chart(hr) {
  const bc = hr.by_class || {}, H = 96, N = Math.max(1, ...CLASSES.map(([c]) => bc[c]?.length || 0));
  const col = i => CLASSES.map(([c, k]) => [c, k, +bc[c]?.[i] || 0]).filter(x => x[2]);
  const tot = Array.from({ length: N }, (_, i) => col(i).reduce((a, x) => a + x[2], 0));
  const all = tot.reduce((a, b) => a + b, 0), total = c => (bc[c] || []).reduce((a, b) => a + b, 0);
  if (!all) return empty('Quiet for the last hour.');
  const max = Math.max(...tot), last = tot.findLastIndex(v => v > 0), tops = [];
  const bars = tot.map((v, i) => {
    if (!v) return null;
    let top = H - 1;  // the baseline owns the bottom pixel; stacked segments keep 1 px card gaps
    const segs = col(i).map(([, k, c], j) => { top -= Math.max(2, c / max * (H - 8)) + (j ? 1 : 0);
      return h('svg:rect', { class: `b-${k}`, x: i + .15, width: .7, y: top, height: Math.max(2, c / max * (H - 8)) }); });
    tops[i] = top;
    return h('svg:g', { class: 'col' }, h('svg:title', {}, `${hm(hr.start_at + i * hr.step_s)} · ${plural(v, 'request')} (${col(i).map(([c, , x]) => `${x} ${c}`).join(', ')})`),
      h('svg:rect', { class: 'hit', x: i, width: 1, y: 0, height: H }), segs);
  });
  const label = `Requests per minute over the last hour: ${all} in all (${CLASSES.map(([c]) => `${total(c)} ${c}`).join(', ')}). Busiest minute: ${max}.`;
  return h('div', { class: 'chart' },
    h('div', { class: 'plot' },
      h('div', { class: 'bars' },
        h('svg:svg', { viewBox: `0 0 ${N} ${H}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': label }, bars,
          h('svg:line', { class: 'base', x1: 0, x2: N, y1: H - .5, y2: H - .5 })),
        h('span', { class: 'val mono', 'aria-hidden': 'true', '--x': pct((last + .5) / N), '--y': `${H - tops[last] + 3}px` }, n(tot[last]))),
      h('div', { class: 'axis mono', 'aria-hidden': 'true' }, h('span', {}, hm(hr.start_at)), h('span', {}, 'now'))),
    h('ul', { class: 'legend' }, CLASSES.map(([c, k]) => h('li', {}, h('span', { class: `sw b-${k}` }), c, h('span', { class: 'mono' }, n(total(c)))))));
}

// ---- Models
function models(s) {
  const m = s.models || {};
  if (!m.source) return card(none(m.note || 'No model calls counted yet.'));
  return [card(m.note && h('p', { class: 'note' }, m.note),
      m.kinds.length ? h('div', { class: 'kinds' }, m.kinds.map(k => h('div', { class: 'kind' },
        h('h3', { class: 'eyebrow mono' }, k.kind), k.model && h('p', { class: 'fact mono' }, k.model),
        h('div', { class: 'metrics' }, metric(n(k.calls), 'calls'), metric(n(k.failed), `of ${n(k.calls)} failed`, null, k.failed > 0 && 'warn'),
          metric(dur(k.p50_ms), 'p50'), metric(dur(k.p95_ms), 'p95'), metric(n(k.total_tokens), 'tokens'), metric(usd(k.cost_usd), 'cost')))))
        : empty('No model calls in the last 24 hours.')),
    m.recent.length > 0 && card(head('Recent calls', 'Newest first'), table('Recent model calls, newest first',
      [['Time', 'w6'], ['Kind', 'w6'], ['Tool', ''], ['Status', 'num w6'], ['Latency', 'num w6'], ['Try', 'num w4'], ['Tokens', 'num w6'], ['Cost', 'num w6']],
      m.recent.map(c => [[when(c.at), 'mono', { '--o': 1 }], [c.kind, 'mono', { '--o': 2 }], [c.tool ?? '—', `mono${c.tool ? '' : ' z'}`, { '--o': 3 }],
        [[!c.ok && glyph('warn', 'failed'), c.status], 'num', { '--o': 4 }], [dur(c.latency_ms), `num${c.latency_ms == null ? ' z' : ''}`],
        [n(c.attempt), `num${c.attempt > 1 ? '' : ' z'}`, { 'data-l': 'attempt' }],
        [n(c.total_tokens), `num${c.total_tokens == null ? ' z' : ''}`, { 'data-l': 'tokens' }], [usd4(c.cost_usd), `num${c.cost_usd == null ? ' z' : ''}`]])))];
}

// ---- The gate: a blocked probe is the gate working, so nothing here is coloured as trouble.
function gate(s) {
  const b = s.blocked;
  if (!b) return card(none(reason('caddy_log') || 'The Caddy log isn’t available.'));
  return card(h('div', { class: 'gate-head' }, metric(n(b.count_24h), 'blocked in 24 h', `${n(b.count_1h)} in the last hour`),
      b.top_paths.length > 0 && h('div', { class: 'paths' }, h('p', { class: 'eyebrow' }, 'Most tried'),
        h('ul', {}, b.top_paths.map(p => h('li', {}, h('span', { class: 'mono path clamp' }, p.path), h('span', { class: 'mono' }, `×${n(p.count)}`)))))),
    b.recent.length ? [head('Recent', 'Newest first'), table('Blocked requests, newest first',
      [['Time', 'w6'], ['Method', 'w6'], ['Path', ''], ['Country', 'w6'], ['User agent', ''], ['IP', 'w9']],
      b.recent.map(x => [[when(x.at), 'mono', { '--o': 3 }], [x.method, 'mono', { '--o': 1 }], [clamp(x.path, 3), 'mono path grow', { '--o': 2 }],
        [x.country ?? '—', '', { '--o': 4 }], [x.user_agent ?? '—', 'ua full', { '--o': 6 }], [x.client_ip ?? '—', 'mono', { '--o': 5 }]]))]
      : empty('No one has knocked in the last 24 hours.'));
}

// ---- Your record
const ENTRY = { pending: 'waiting', transcribing: 'hearing', analyzing: 'making sense', complete: 'written', failed: 'not written' };
const RJOB = { queued: 'waiting', counting: 'counting', writing: 'writing', complete: 'done', failed: 'failed' };
const states = (o, words) => dot(...Object.entries(o || {}).filter(([, v]) => v).map(([k, v]) => `${n(v)} ${words[k] || k}`));
function record(s) {
  const r = s.record;
  if (!r) return card(none(reason('db') || RECORD_DOWN));
  const p = r.profile || {}, a = r.audio || {};
  return card(h('p', { class: 'profile' }, dot(p.name, p.time_zone, p.weekly_goal != null && `${p.weekly_goal} a week`)),
    h('div', { class: 'metrics' },
      metric(n(r.entries.total), 'entries', dot(`${n(r.entries.last_7d)} in 7 days`, states(r.entries.by_state, ENTRY))),
      metric(n(r.projects), 'projects'),
      metric(n(r.reports.total), 'reports', r.reports.last_generated_at != null && `last written ${since(r.reports.last_generated_at)} ago`),
      metric(n(sum(r.report_jobs)), 'report jobs', states(r.report_jobs, RJOB))),
    h('p', { class: 'fact mono' }, a.disk_bytes != null
      ? dot(`${bytes(a.disk_bytes)} of audio in ${plural(a.disk_files, 'file')}`, a.past_retention > 0 && `${n(a.past_retention)} past retention`)
      : [`${plural(a.objects, 'recording')} · ${bytes(a.db_bytes)}`, reason('audio_dir') && h('span', { class: 'plain' }, ` · ${reason('audio_dir')}`)]));
}

// ---- Errors
const errors = s => [['Server', s.errors?.server, 'server_log', 'Not set up. Set NOTCH_DASH_SERVER_LOG to read server errors.'],
  ['Tunnel', s.errors?.tunnel, 'tunnel_log', 'Not set up. Set NOTCH_DASH_TUNNEL_LOG to read tunnel warnings.']].map(([title, list, src, off]) =>
  card(head(title), list == null ? none(reason(src) || off) : !list.length ? empty('Nothing’s gone wrong in the last 24 hours.')
    : h('ul', { class: 'errs' }, list.map(x => h('li', {}, h('details', {},
      h('summary', {}, h('span', { class: 'lvl' }, glyph(/^(ERR|CRIT|FATAL)/i.test(x.level) ? 'critical' : 'warn'), x.level, x.where && h('span', { class: 'mono' }, x.where)),
        h('span', { class: 'msg' }, x.message), h('span', { class: 'cnt mono' }, `×${n(x.count)}`),
        x.exception && h('code', { class: 'exc' }, x.exception),
        h('span', { class: 'when mono' }, `first ${since(x.first_at)} ago · last ${since(x.last_at)} ago`)),
      h('pre', {}, x.sample)))))));

// ---- Where this comes from
const SRC = { db: 'Record', audio_dir: 'Audio folder', metrics: 'Model call log', caddy_log: 'Caddy log', server_log: 'Server log',
  tunnel_log: 'Tunnel log', tunnel_metrics: 'cloudflared metrics', gate_secret: 'Gate secret', openrouter: 'OpenRouter spend', device: 'Phone (devicectl)' };
const SRC_STATE = { ok: ['ok', 'Reading'], off: ['unknown', 'Off'], unreachable: ['warn', 'Can’t reach it'] };
const sources = s => h('ul', { class: 'card grouped' }, Object.entries(s.sources || {}).map(([key, x]) => {
  const [k, word] = SRC_STATE[x.state] || ['unknown', 'Unknown'];
  return h('li', { class: 'srow' }, glyph(k, word), h('span', { class: 'sname' }, SRC[key] || key), h('span', { class: 'mono where' }, x.where ?? '—'),
    h('span', { class: 'sstate' }, x.state === 'ok' ? (x.read_at != null ? `read ${since(x.read_at)} ago` : 'not read yet') : x.reason || word));
}));

// ---- Page
const PANELS = [['status-body', strip], ['notches-body', notches], ['traffic-body', traffic], ['models-body', models],
  ['gate-body', gate], ['record-body', record], ['errors-body', errors], ['sources-body', sources]];
function render(s) {
  S = s; G = s.generated_at;
  const o = s.overall, k = st(o.status);
  for (const [id, t] of [['overall-word', o.word], ['overall-reason', o.reason]]) if ($(id).textContent !== t) $(id).textContent = t;
  $('overall').setAttribute('class', `word s-${k}`);
  $('overall-glyph').setAttribute('href', `#g-${k}`);
  document.title = `${o.word} · Notch stack`;
  const crit = (o.problems || []).filter(p => p.status === 'critical');
  $('notice').hidden = !crit.length;
  kids($('notice-list'), crit.map(p => h('li', {}, p.reason)));
  for (const [id, fn] of PANELS) {
    try { kids($(id), [fn(s)]); } catch (e) { console.error(e); kids($(id), [card(none('Couldn’t show this part. The rest of the page is current.'))]); }
  }
  sync();
}
function fresh() {
  const el = $('fresh'), stale = dead || fails > 0;
  el.classList.toggle('stale', stale);
  kids(el, [stale && glyph('unknown', null, 'calm'), h('span', {}, dead ? 'The dashboard was updated. Reload the page.'
    : !S ? (fails ? 'Couldn’t reach the dashboard. Trying again.' : 'Reading…')
    : fails ? ['Showing what we had at ', h('span', { class: 'mono' }, clock(G)), '. Trying again.']
    : ['Updated ', h('span', { class: 'mono' }, `${Math.floor((Date.now() - lastAt) / 1e3)} s`), ' ago'])]);
}
async function poll() {
  clearTimeout(timer);
  if (inflight || dead) return;
  inflight = true;
  const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 5000);
  try {
    const r = await fetch('api/snapshot', { cache: 'no-store', signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    if (s.v !== 1) dead = true; else { fails = 0; lastAt = Date.now(); render(s); }
  } catch { fails++; }
  clearTimeout(t);
  inflight = false;
  fresh();
  // Every 2 s; after a failure back off 4, 8, 16, then 30 s. Hidden tabs don't poll.
  if (!dead && !document.hidden) timer = setTimeout(poll, fails ? Math.min(2000 * 2 ** fails, 30000) : 2000);
}
document.addEventListener('visibilitychange', () => document.hidden ? clearTimeout(timer) : poll());
setInterval(() => document.hidden || fresh(), 1000);
poll();
