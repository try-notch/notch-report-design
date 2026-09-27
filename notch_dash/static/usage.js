// usage.js — renders /api/usage (notch_dash/usage.py): the fleet's counts, timings and costs.
// Everything is set as text; nothing from the server is ever parsed as HTML.
"use strict";

const REFRESH_MS = 15000;
const $ = (id) => document.getElementById(id);

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

const n = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-US"));
const usd = (v, digits = 2) => (v === null || v === undefined ? "—" : "$" + Number(v).toFixed(digits));
const secs = (v) => (v === null || v === undefined ? "—" : Number(v) < 10 ? Number(v).toFixed(2) + " s" : Number(v).toFixed(1) + " s");
const pct = (part, whole) => (whole ? Math.round((100 * part) / whole) + "%" : "—");
const KIND = { transcribe: "Transcribe", analyze: "Write-up", takeaways: "Rewrite", reports: "Report" };

function metrics(target, items) {
  const box = $(target);
  box.replaceChildren();
  for (const [label, value, caption] of items) {
    const cell = el("div", null, "metric");
    cell.append(el("div", label, "c"), el("div", value, "v"));
    if (caption) cell.append(el("div", caption, "c"));
    box.append(cell);
  }
}

function table(target, head, body, numeric) {
  const t = $(target);
  t.replaceChildren();
  const thead = el("thead");
  const hr = el("tr");
  head.forEach((h, i) => hr.append(el("th", h, numeric.includes(i) ? "num" : "")));
  thead.append(hr);
  const tbody = el("tbody");
  if (!body.length) {
    const tr = el("tr");
    const td = el("td", "Nothing yet.", "none");
    td.colSpan = head.length;
    tr.append(td);
    tbody.append(tr);
  }
  for (const row of body) {
    const tr = el("tr");
    row.forEach((cell, i) => {
      // Narrow cards drop the header row (style.css); each cell then carries its column's name.
      const td = el("td", cell, i === 0 ? "full" : numeric.includes(i) ? "num" : "");
      if (i > 0) td.dataset.l = head[i];
      tr.append(td);
    });
    tbody.append(tr);
  }
  t.append(thead, tbody);
}

function render(u) {
  const t = u.totals, a = u.active, lim = u.limits;
  metrics("today", [
    ["Active accounts", n(a.today)],
    ["Notches", n(t.notches_today)],
    ["Spend", usd(t.spend_today), "of " + usd(lim.global_usd_per_day, 0) + " a day overall"],
    ["At the daily cap", n(lim.accounts_at_cap_today), lim.notches_per_day + " notches each"],
  ]);
  metrics("week", [
    ["Active accounts", n(a["7d"]), n(a["30d"]) + " in 30 days"],
    ["Notches", n(t.notches_7d), n(t.reports_7d) + " reports"],
    ["Spend", usd(t.spend_7d), usd(t.spend_total) + " all time"],
    ["Per notch", usd(t.cost_per_notch_7d, 4), "failed " + pct(t.failed_7d, t.attempts_7d) + " of calls"],
  ]);
  metrics("accounts", [
    ["Accounts", n(u.accounts.total), n(u.accounts.new_7d) + " new this week"],
    ["Notch Cloud on", n(u.accounts.cloud)],
    ["Blocked", n(u.accounts.blocked)],
    ["Deleted", n(u.accounts.deleted)],
  ]);
  table("daily", ["Day", "Active", "Notches", "Write-ups", "Reports", "Failed", "Limited", "Spend"],
    u.daily.map((d) => [d.day.slice(5), n(d.active), n(d.notches), n(d.analyses), n(d.reports), n(d.failed),
      n(d.limited), usd(d.cost_usd)]), [1, 2, 3, 4, 5, 6, 7]);
  table("latency", ["Call", "Calls", "p50", "p95", "Slowest"],
    u.latency.map((l) => [KIND[l.kind] || l.kind, n(l.calls), secs(l.p50_s), secs(l.p95_s), secs(l.max_s)]),
    [1, 2, 3, 4]);
  $("audio").textContent = u.audio_p50_s ? "A typical recording is " + secs(u.audio_p50_s) + " long." : "";
  $("inflight").textContent = n(u.in_flight.now) + " calls in flight now" +
    (u.in_flight.overdue ? ", " + n(u.in_flight.overdue) + " past their deadline." : ".");
  table("problems", ["Call", "Outcome", "Code", "Calls", "Accounts"],
    u.problems.map((p) => [KIND[p.kind] || p.kind, p.status, p.code, n(p.calls), n(p.accounts)]), [3, 4]);
  table("top", ["Account", "Notches", "Calls", "Spend"],
    u.top_accounts.map((x) => [x.account, n(x.notches), n(x.calls),
      usd(x.cost_usd, 4) + (x.cost_usd >= lim.account_usd_per_day ? " — at cap" : "")]), [1, 2, 3]);
  table("models", ["Call", "Model", "Provider", "Calls", "Spend"],
    u.models.map((m) => [KIND[m.kind] || m.kind, m.models, m.providers, n(m.calls), usd(m.cost_usd, 4)]), [3, 4]);
  table("zdr", ["Call", "Verdict", "Calls"],
    u.zdr.map((z) => [KIND[z.kind] || z.kind, z.verdict, n(z.calls)]), [2]);
  table("versions", ["Platform", "Version", "Accounts"],
    u.versions.map((v) => [v.platform, v.app_version, n(v.accounts)]), [2]);
}

async function refresh() {
  try {
    const response = await fetch("api/usage", { cache: "no-store" });
    const u = await response.json();
    if (u.error) {
      $("notice").hidden = false;
      $("notice-text").textContent = u.error;
      $("fresh").textContent = "Couldn’t read at " + new Date().toLocaleTimeString();
      return;
    }
    $("notice").hidden = true;
    render(u);
    $("fresh").textContent = "Updated " + new Date(u.generated_at * 1000).toLocaleTimeString();
  } catch (error) {
    $("fresh").textContent = "Couldn’t reach the dashboard at " + new Date().toLocaleTimeString();
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
