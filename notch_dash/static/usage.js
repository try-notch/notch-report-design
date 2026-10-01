'use strict';
// usage.js: polls api/usage (notch_dash/usage.py; DASHBOARD.md › The usage page) and renders it.
// Model and provider names come from OpenRouter, so data reaches the DOM only as text nodes and
// constant attributes. Never innerHTML. The DOM helpers are app.js's: fresh nodes morphed into
// the page, so a poll does not drop a selection or restart an animation.

const V = 2, REFRESH_MS = 15000, NS = 'http://www.w3.org/2000/svg';
const $ = id => document.getElementById(id);
let U = null, G = 0, lastAt = 0, fails = 0, timer = 0, inflight = false, dead = false, unread = null;

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
const sync = () => document.getAnimations?.().forEach(a => a instanceof CSSAnimation && (a.startTime = 0));

// ---- Formats (DASHBOARD.md › Formats). Instants are the viewer's local time; "now" is the server's.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const n = x => x == null ? '—' : Number(x).toLocaleString('en-US');
const count = (c, one, many) => `${n(c)} ${c === 1 ? one : many || `${one}s`}`;
const dot = (...xs) => xs.filter(x => x != null && x !== false && x !== '').join(' · ') || null;
const pad = x => String(x).padStart(2, '0');
// Under a dollar, four decimals: a notch costs a quarter of a cent, and "$0.00" says nothing.
function usd(x) {
  if (x == null) return '—';
  x = +x;
  if (x === 0) return '$0';
  if (x < 1e-4) return '<$0.0001';
  return x >= 1 ? `$${x.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : `$${x.toFixed(4)}`;
}
const cap = x => `$${Number.isInteger(+x) ? n(x) : (+x).toFixed(2)}`;  // a limit someone set: $20, $1, $0.50
function secs(s) {
  if (s == null) return '—';
  const ms = Math.max(0, s * 1e3);
  if (ms < 10) return `${ms ? ms.toFixed(1) : 0} ms`;
  if (ms < 1e3) return `${Math.round(ms)} ms`;
  if (ms < 6e4) return `${(ms / 1e3).toFixed(1)} s`;
  const m = Math.floor(ms / 6e4);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}
function ago(s) {
  s = Math.max(0, s);
  return s < 60 ? `${Math.floor(s)} s` : s < 3600 ? `${Math.floor(s / 60)} min`
    : s < 86400 ? `${Math.floor(s / 3600)} h` : count(Math.floor(s / 86400), 'day');
}
const hm = at => { const d = new Date(at * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
function when(at) {
  if (at == null) return '—';
  const d = new Date(at * 1000);
  return d.toDateString() === new Date(G * 1000).toDateString() ? `${hm(at)}:${pad(d.getSeconds())}`
    : `${MONTHS[d.getMonth()]} ${d.getDate()} ${hm(at)}`;
}
const utcDay = at => new Date(at * 1000).toISOString().slice(0, 10);
const dayLabel = day => { const [, m, d] = day.split('-'); return `${MONTHS[m - 1]} ${+d}`; };  // a UTC day, as named

// ---- Pieces
const KIND = { transcribe: 'Transcribe', analyze: 'Write-up', takeaways: 'Rewrite', reports: 'Report' };
const ROUTE = { '/v2/transcribe': 'Transcribe', '/v2/analyze': 'Write-up', '/v2/takeaways': 'Rewrite', '/v2/reports': 'Report',
  '/v2/config': 'Config', '/v2/account': 'Account' };
const OUTCOME = { failed: 'Failed', refused: 'Refused', turned_away: 'Turned away' };
const PLATFORM = { ios: 'iOS', android: 'Android', web: 'Web' };
const kind = k => KIND[k] || k;
const where = w => KIND[w] || ROUTE[w] || (String(w).startsWith('/v2/cloud') ? 'Notch Cloud' : w);
const words = code => (code || 'no code').replace(/_/g, ' ');
const glyph = (k, label, cls = '') => h('svg:svg', { class: `g s-${k} ${cls}`, ...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': 'true' }) }, h('svg:use', { href: `#g-${k}` }));
const pill = (k, word) => h('span', { class: `pill s-${k}` }, glyph(k), h('span', {}, word));
const none = text => h('p', { class: 'none' }, glyph('unknown'), h('span', {}, text));
const empty = text => h('p', { class: 'empty' }, text);
const fact = (...xs) => h('p', { class: 'fact' }, xs);
const metric = (label, value, caption) => h('div', { class: 'metric' },
  h('div', { class: 'c' }, label), h('div', { class: 'v' }, value), caption && h('div', { class: 'c' }, caption));
// cols: [label, class]; a row is its cells, or {a: attributes, c: cells}; a cell is [value, class, attributes].
// In a `tbl` each cell carries data-l, its column's name, for the narrow reflow; a `mini` table never reflows.
const table = (cls, caption, cols, rows) => h('table', { class: cls }, h('caption', { class: 'vh' }, caption),
  h('thead', {}, h('tr', {}, cols.map(([t, c]) => h('th', { scope: 'col', class: c }, t)))),
  h('tbody', {}, rows.map(r => h('tr', r.a || {}, (r.c || r).filter(Boolean).map(([v, c, a]) => h('td', { class: c, ...a }, v))))));
const num = (v, label) => [n(v), `num${v ? '' : ' z'}`, { 'data-l': label }];

// ---- The headline: the last 24 hours on the clock, then the last 7 UTC days
function day(u) {
  const l = u.last_24h, lim = u.limits, missed = l.failed + l.refused + l.turned_away;
  return [metric('Accounts', n(l.accounts), 'made a call'),
    metric('Notches', n(l.notches), dot(count(l.reports, 'report'), count(l.rewrites, 'rewrite'))),
    metric('Spend', usd(l.spend_usd), `${usd(lim.spend_today_usd)} of the ${cap(lim.global_usd_per_day)} cap this UTC day`),
    metric('Didn’t go through', n(missed), `of ${count(l.calls, 'call')}`)];
}
function week(u) {
  const w = u.week;
  return [metric('Active accounts', n(w.active), `${n(w.active_30d)} in 30 days`),
    metric('Notches', n(w.notches), dot(count(w.reports, 'report'), count(w.rewrites, 'rewrite'))),
    metric('Spend', usd(w.spend_usd), w.spend_usd > 0 && `${usd(w.spend_usd / u.window_days)} a day`),
    metric('A notch costs', usd(w.notch_cost_usd), w.report_cost_usd != null && `a report ${usd(w.report_cost_usd)}`)];
}
const since = u => u.all_time.since
  ? `All time, since ${dayLabel(u.all_time.since)}: ${dot(count(u.all_time.notches, 'notch', 'notches'), count(u.all_time.reports, 'report'), usd(u.all_time.spend_usd))}.`
  : 'Nothing yet.';

// ---- Day by day: from the first UTC day anything happened, so a gap shows and two weeks of zeros don't
const quiet = d => !(d.active || d.notches || d.rewrites || d.reports || d.failed || d.refused || d.turned_away || d.cost_usd);
function daily(u) {
  if (!u.daily.length) return none(`Nothing in the last ${n(u.daily_days)} days.`);
  return [table('tbl', 'Each UTC day since the first one with any use, newest first',
      [['Day', 'w9'], ['Active', 'num'], ['Notches', 'num'], ['Rewrites', 'num'], ['Reports', 'num'], ['Failed', 'num'], ['Refused', 'num'], ['Spend', 'num']],
      u.daily.map(d => ({ a: { 'data-day': d.day }, c: [[[dayLabel(d.day), d.day === u.today && h('span', { class: 'aside' }, ' so far')], 'full'],
        num(d.active, 'active'), num(d.notches, 'notches'), num(d.rewrites, 'rewrites'), num(d.reports, 'reports'), num(d.failed, 'failed'),
        num(d.refused + d.turned_away, 'refused'), [usd(d.cost_usd), `num${d.cost_usd ? '' : ' z'}`, { 'data-l': 'spend' }],
        quiet(d) && ['Nothing.', 'm-only']] }))),
    fact('Active: opened the app signed in, or made a call. Refused: stopped by a limit, or turned away before processing.')];
}

// ---- Problems: what failed, what a limit refused, what was turned away before the meter saw it
function problems(u) {
  const p = u.problems, f = u.in_flight, w = u.week;
  return [fact(`${n(w.failed)} of ${count(w.attempts, 'call')} that started failed. ${count(f.now, 'call')} in flight now${f.overdue ? `, ${n(f.overdue)} past ${f.overdue === 1 ? 'its' : 'their'} deadline` : ''}.`),
    p.length ? table('tbl', 'Calls that failed or were refused in the last 7 days, the most frequent first',
      [['Call', 'w6'], ['What happened', ''], ['Calls', 'num w5'], ['Accounts', 'num w6'], ['Last', 'num w9']],
      p.map(x => [[where(x.where), 'full'], [[OUTCOME[x.outcome] || x.outcome, ' · ', words(x.code), x.status && ` (${x.status})`], 'grow'],
        [n(x.calls), 'num', { 'data-l': 'calls' }], [x.accounts == null ? '—' : n(x.accounts), `num${x.accounts == null ? ' z' : ''}`, { 'data-l': 'accounts' }],
        [when(x.last_at), 'num', { 'data-l': 'last' }]]))
      : empty('Nothing’s gone wrong in the last 7 days.'),
    p.some(x => x.outcome === 'turned_away') && fact('Turned away: answered before processing began (sign-in, app version, a switch, unreadable audio), so no account is recorded.'),
    u.probes_7d > 0 && fact(`Not listed: ${count(u.probes_7d, 'request')} to paths or methods the API doesn’t have.`)];
}

// ---- Models: one row a model, with who served it
function models(u) {
  if (!u.models.length) return none('No model calls in the last 7 days.');
  return h('ul', { class: 'list' }, u.models.map(m => h('li', {},
    h('div', { class: 'card-head' }, h('h3', {}, kind(m.kind)), h('span', { class: 'aside mono' }, dot(count(m.calls, 'call'), usd(m.cost_usd)))),
    h('p', { class: 'fact mono' }, m.model),
    fact(m.providers.length ? ['Served by ', m.providers.map((p, i) => [i > 0 && ', ', p.name, ` ×${n(p.calls)}`])] : 'OpenRouter didn’t name the provider.'))));
}

// ---- Accounts: the totals, then each account active this week
function accounts(u) {
  const a = u.accounts, rows = u.account_rows, limit = u.limits.notches_per_day;
  const seen = r => r.last_call_at != null && utcDay(r.last_call_at) >= r.last_active_day ? `${ago(G - r.last_call_at)} ago` : dayLabel(r.last_active_day);
  return [h('div', { class: 'metrics' }, metric('Accounts', n(a.total), `${n(a.new_7d)} new this week`), metric('Have made a notch', n(a.notched), `of ${n(a.total)}`)),
    fact(dot(`Notch Cloud on: ${n(a.cloud)}`, `blocked: ${n(a.blocked)}`, `deleted: ${n(a.deleted)}`)),
    rows.length ? table('mini', 'Accounts active in the last 7 days, the most recent first',
      [['Account', ''], ['Last seen', ''], ['Build', 'opt'], ['Notches', 'num'], ['Spend', 'num'], ['Today', 'num opt']],
      rows.map(r => [[r.account, 'mono'], [seen(r), ''], [r.app_version ?? '—', 'mono opt'], [n(r.notches_7d), 'num'], [usd(r.spend_7d_usd), 'num'],
        [`${n(r.notches_today)}/${n(limit)}`, 'num opt']]))
      : none('No account has been active in the last 7 days.'),
    u.account_rows_more > 0 && fact(`And ${count(u.account_rows_more, 'more account')}.`),
    rows.length > 0 && fact('Notches and spend are the last 7 days. Today is notches against the daily cap, by UTC day.')];
}
function versions(u) {
  const v = u.versions;
  if (!v.length) return none('No signed-in app has checked in over the last 7 days.');
  return [table('mini', 'The build each active account is on', [['Platform', ''], ['Build', ''], ['Accounts', 'num']],
      v.map(x => [[PLATFORM[x.platform] || x.platform, ''], [x.app_version, 'mono'], [n(x.accounts), 'num']])),
    fact('Each account counts once, on the newest build it ran on its latest day.')];
}

// ---- Zero data retention: what the audit could confirm, said as a state
function zdr(u) {
  const z = u.zdr;
  const [state, line] = z.miss ? [pill('critical', 'Not zero retention'), `${count(z.miss, 'call')} went to a provider that isn’t on OpenRouter’s zero-retention list.`]
    : z.unknown ? [pill('warn', 'Unverified'), `${n(z.unknown)} of ${count(z.audited, 'audited call')} couldn’t be checked: OpenRouter didn’t name the provider.`]
    : z.audited ? [pill('ok', 'Confirmed'), `All ${count(z.audited, 'audited call')} went to zero-retention providers.`]
    : [pill('unknown', 'Nothing to audit'), 'No recording has been transcribed in the last 7 days.'];
  return [h('div', { class: 'card-head' }, state), h('p', {}, line),
    z.kinds.length > 0 && table('mini', 'Zero-retention verdicts by kind of call', [['Call', ''], ['Confirmed', 'num'], ['Unverified', 'num'], ['Missed', 'num']],
      z.kinds.map(k => [[kind(k.kind), ''], [n(k.hit), 'num'], [n(k.unknown), 'num'], [n(k.miss), 'num']])),
    fact('Recordings are audited after each transcription. Write-ups, rewrites and reports are sent with zero retention required, so OpenRouter can’t route them anywhere else.')];
}

// ---- Server: is it up, what config it runs, the limits, and OpenRouter's own count of the spend
function server(u) {
  const s = u.server || {}, c = u.config, lim = u.limits, api = s.api, key = s.openrouter;
  const label = text => h('h3', { class: 'eyebrow' }, text);
  return [h('div', { class: 'card-head' }, label('API'), !api ? pill('unknown', 'Not checked yet') : api.ok ? pill('ok', 'Answering') : pill('critical', 'Down')),
    api && h('p', { class: 'fact mono' }, api.ok ? dot(`/healthz in ${secs(api.latency_ms / 1e3)}`, `checked ${ago(G - api.checked_at)} ago`) : api.error),
    label(`Config v${n(c.version)}`),
    fact(dot(c.note, c.created_by && `by ${c.created_by}`, c.created_at != null && when(c.created_at)) || 'The defaults: nothing has been pushed.'),
    h('p', { class: 'fact mono' }, dot(`write-ups ${c.prompts.analyze}`, `rewrites ${c.prompts.takeaways}`, `check ${c.prompts.check}`, `reports ${c.prompts.reports}`, `classifier ${c.classifier}`)),
    label('Limits'),
    fact(dot(`${n(lim.notches_per_day)} notches a day each`, `${cap(lim.account_usd_per_day)} a day an account`, `${cap(lim.global_usd_per_day)} a day overall`)),
    fact(`Days are UTC: the next one starts at ${hm(u.resets_at)} here. ${lim.accounts_at_cap_today ? `${count(lim.accounts_at_cap_today, 'account is', 'accounts are')} at the notch cap today.` : 'No account is at the notch cap today.'}`),
    key && [label('OpenRouter key'), fact(dot(key.week_usd != null && `${usd(key.week_usd)} this week`, key.total_usd != null && `${usd(key.total_usd)} in all`,
      key.remaining_usd != null && key.limit_usd != null && `${usd(key.remaining_usd)} of ${cap(key.limit_usd)} left`) || 'No spend reported.')]];
}

// ---- Speed and cost: each kind of call, by the prompt that wrote it
function calls(u) {
  if (!u.calls.length) return none('No calls in the last 7 days.');
  const tokens = c => c.prompt_tokens == null ? '—' : `${n(Math.round(c.prompt_tokens))} → ${n(Math.round(c.completion_tokens || 0))}`;
  return [table('tbl', 'Each kind of call in the last 7 days, by the prompt that wrote it',
      [['Call', ''], ['Prompt', 'w9'], ['OK', 'num w6'], ['Failed', 'num w6'], ['Typical', 'num w6'], ['p95', 'num w6'], ['Slowest', 'num w6'], ['Each', 'num w6'], ['Tokens in → out', 'num w9']],
      u.calls.map(c => [[kind(c.kind), 'full'], [c.prompt_version ?? '—', `mono${c.prompt_version ? '' : ' z'}`, { 'data-l': 'prompt' }],
        [n(c.calls), 'num', { 'data-l': 'ok' }], num(c.failed, 'failed'), [secs(c.p50_s), 'num', { 'data-l': 'typical' }],
        [secs(c.p95_s), `num${c.p95_s == null ? ' z' : ''}`, { 'data-l': 'p95' }], [secs(c.max_s), 'num', { 'data-l': 'slowest' }],
        [usd(c.cost_usd), 'num', { 'data-l': 'each' }], [tokens(c), `num${c.prompt_tokens == null ? ' z' : ''}`, { 'data-l': 'tokens' }]])),
    fact(dot('Times and costs are of the calls that finished; typical is the median, on the server from start to finish.', u.calls.some(c => c.p95_s == null) && 'A p95 shows once a row has 20 calls.',
      u.audio_p50_s != null && `A typical recording is ${secs(u.audio_p50_s)} long.`))];
}

// ---- Recent calls: one row each, newest first
function recent(u) {
  if (!u.recent.length) return none('Nothing yet. The next call shows up here as it happens.');
  const size = x => x.audio_seconds != null ? secs(x.audio_seconds) : x.input_chars != null ? `${n(x.input_chars)} chars`
    : x.entry_count != null ? count(x.entry_count, 'notch', 'notches') : '—';
  const outcome = x => x.status === 'ok' ? 'OK' : x.status === 'failed' ? [glyph('warn', 'failed'), ' Failed · ', words(x.code)]
    : x.status === 'rejected' ? ['Refused · ', words(x.code)]
    : x.overdue ? [glyph('warn', 'past its deadline'), ' Past its deadline'] : [glyph('busy'), ' In flight'];
  const tokens = x => x.prompt_tokens == null ? '—' : `${n(x.prompt_tokens)} → ${n(x.completion_tokens || 0)}`;
  return table('tbl', `The last ${n(u.recent.length)} calls, newest first`,
    [['When', 'w9'], ['Call', 'w6'], ['Account', 'w6'], ['Size', 'num w9'], ['Took', 'num w6'], ['Cost', 'num w6'], ['Tokens in → out', 'num w9'], ['Served by', ''], ['Prompt', 'w6'], ['Build', 'w5'], ['Outcome', '']],
    u.recent.map(x => [[when(x.at), 'mono', { '--o': 1 }], [[kind(x.kind), x.attempt > 1 && ` (try ${n(x.attempt)})`], '', { '--o': 2 }], [x.account, 'mono', { '--o': 3 }],
      [size(x), `num${size(x) === '—' ? ' z' : ''}`, { 'data-l': 'size' }], [secs(x.took_s), `num${x.took_s == null ? ' z' : ''}`, { 'data-l': 'took' }],
      [usd(x.cost_usd), `num${x.cost_usd ? '' : ' z'}`, { 'data-l': 'cost' }], [tokens(x), `num${x.prompt_tokens == null ? ' z' : ''}`, { 'data-l': 'tokens' }],
      [x.providers.join(', ') || '—', x.providers.length ? '' : 'z', { 'data-l': 'served by' }], [x.prompt_version ?? '—', `mono${x.prompt_version ? '' : ' z'}`, { 'data-l': 'prompt' }],
      [x.app_version ?? '—', 'mono', { 'data-l': 'build' }], [outcome(x), 'grow', { '--o': 4 }]]));
}

// ---- Page
const PANELS = [['day', day], ['week', week], ['since', since], ['daily', daily], ['problems', problems], ['models', models], ['accounts', accounts],
  ['versions', versions], ['zdr', zdr], ['server', server], ['calls', calls], ['recent', recent]];
function notice() {
  const lines = unread ? [unread] : (U?.attention || []).map(a => a.text);
  const critical = unread != null || (U?.attention || []).some(a => a.level === 'critical');
  $('notice').hidden = !lines.length;
  $('notice').setAttribute('class', `notice${critical ? '' : ' warn'}`);
  $('notice-glyph').setAttribute('href', critical ? '#g-critical' : '#g-warn');
  $('notice-title').textContent = unread ? 'Can’t read the numbers' : 'Needs a look';
  kids($('notice-list'), lines.map(text => h('li', {}, text)));
}
function render(u) {
  U = u; G = u.generated_at;
  for (const [id, fn] of PANELS) {
    try { kids($(id), [fn(u)]); } catch (e) { console.error(e); kids($(id), [none('Couldn’t show this part. The rest of the page is current.')]); }
  }
  kids($('harness'), [u.server?.harness && h('a', { href: 'harness' }, 'This stack’s health →')]);
  sync();
}
function fresh() {
  const el = $('fresh'), stale = dead || fails > 0;
  el.classList.toggle('stale', stale);
  kids(el, [stale && glyph('unknown', null, 'calm'), h('span', {}, dead ? 'The dashboard was updated. Reload the page.'
    : !U ? (fails ? 'Couldn’t reach the dashboard. Trying again.' : 'Reading…')
    : fails ? ['Showing what we had at ', h('span', { class: 'mono' }, when(G)), '. Trying again.']
    : ['Updated ', h('span', { class: 'mono' }, `${Math.floor((Date.now() - lastAt) / 1e3)} s`), ' ago'])]);
}
async function poll() {
  clearTimeout(timer);
  if (inflight || dead) return;
  inflight = true;
  const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 5000);
  try {
    const r = await fetch('api/usage', { cache: 'no-store', signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const u = await r.json();
    if (u.error) { unread = u.error; fails++; }  // the meter couldn't be read: say so, keep what we had
    else if (u.v !== V) dead = true;
    else { unread = null; fails = 0; lastAt = Date.now(); render(u); }
  } catch { unread = null; fails++; }
  clearTimeout(t);
  inflight = false;
  notice();
  fresh();
  // Every 15 s (the server caches a read for 10); after a failure 15, 30, then 60 s. Hidden tabs don't poll.
  if (!dead && !document.hidden) timer = setTimeout(poll, fails ? Math.min(REFRESH_MS * 2 ** (fails - 1), 60000) : REFRESH_MS);
}
document.addEventListener('visibilitychange', () => document.hidden ? clearTimeout(timer) : poll());
setInterval(() => document.hidden || fresh(), 1000);
poll();
