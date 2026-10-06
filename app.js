/* A.I. Options Command Centre - frontend. No API keys ever reach this file; it only talks to our own /api. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const V = () => $("#view");
const S = { sel: null, reports: {}, filter: "All", search: "", mode: "GROWTH", job: null, sort: {}, range: "3M",
            wsort: { k: "score", d: -1 }, wfilter: "All", wsearch: "", ptab: "Options", dash: null };

// ------------------------------------------------------------------ helpers
async function api(url, opt = {}) {
  const o = { headers: { "Content-Type": "application/json" }, ...opt };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch(url, o);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ok = x => x !== null && x !== undefined && !Number.isNaN(x);
const money = (x, d = 2) => ok(x) ? (x < 0 ? "-$" : "$") + Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : "—";
const smoney = (x, d = 0) => ok(x) ? (x >= 0 ? "+" : "-") + "$" + Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : "—";
const pct = (x, d = 0, sign = false) => ok(x) ? (sign && x > 0 ? "+" : "") + (x * 100).toFixed(d) + "%" : "—";
const cls = x => !ok(x) ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "";
function ago(ts) {
  if (!ts) return "never";
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 60) return "just now"; if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago"; return Math.round(s / 86400) + "d ago";
}
const COLOR = {
  "BUY NOW": "green", "BUY IF TRIGGERED": "green", "ADD / BUY": "green", "BUY TRIGGERED": "green", "HOLD": "green", "YES": "green",
  "WAIT": "yellow", "HOLD / WATCH": "yellow", "ONLY AT LOWER PRICE": "yellow", "PENDING": "blue",
  "TAKE 25% PROFIT": "orange", "TAKE 50% PROFIT": "orange",
  "SELL": "red", "EXIT NOW": "red", "NO": "red", "AVOID": "black", "DATA UNAVAILABLE": "black",
};
const EMOJI = { green: "🟢", yellow: "🟡", orange: "🟠", red: "🔴", black: "⚫", blue: "🔵" };
const badge = (s, lg = false) => `<span class="bd ${COLOR[s] || "blue"} ${lg ? "lg" : ""}">${esc(s === "BUY IF TRIGGERED" && !lg ? "BUY IF ≤" : s)}</span>`;
const verdictText = s => `${EMOJI[COLOR[s]] || ""} ${esc(s)}`;
const frCls = f => ({ "15-MIN DELAY": "DELAY" }[f] || f || "UNAVAILABLE");
const fr = f => `<span class="fr ${frCls(f)}">${esc(f || "UNAVAILABLE")}</span>`;
const scoreColor = s => s >= 80 ? "var(--green)" : s >= 70 ? "#8fdc3c" : s >= 60 ? "var(--yellow)" : s >= 50 ? "var(--orange)" : "var(--red)";
function toast(msg, ms = 3500) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), ms);
}
const typing = () => { const a = document.activeElement; return a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName); };

// ------------------------------------------------------------------ small SVG components
function ring(score, size = 34) {
  const r = size / 2 - 3, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, score || 0));
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="#14305f" stroke-width="3"/>
  <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${scoreColor(v)}" stroke-width="3" stroke-dasharray="${c * v / 100} ${c}"
  transform="rotate(-90 ${size / 2} ${size / 2})" stroke-linecap="round"/><text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle"
  fill="#fff" font-size="${size * 0.36}" font-weight="700" font-family="Rajdhani">${ok(score) ? Math.round(score) : "—"}</text></svg>`;
}
function probRing(p, color, label, size = 66) {
  const r = size / 2 - 6, c = 2 * Math.PI * r, v = Math.max(0, Math.min(1, p || 0));
  return `<div><svg width="${size}" height="${size}"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="#14305f" stroke-width="6"/>
  <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="6" stroke-dasharray="${c * v} ${c}"
  transform="rotate(-90 ${size / 2} ${size / 2})"/><text x="50%" y="53%" text-anchor="middle" dominant-baseline="middle" fill="#fff"
  font-size="17" font-weight="700" font-family="Rajdhani">${pct(p)}</text></svg><div>${esc(label)}</div></div>`;
}
function spark(vals, w = 110, h = 34, color) {
  vals = (vals || []).filter(ok);
  if (vals.length < 2) return `<svg width="${w}" height="${h}"></svg>`;
  const mn = Math.min(...vals), mx = Math.max(...vals), rg = mx - mn || 1;
  const col = color || (vals[vals.length - 1] >= vals[0] ? "var(--green)" : "var(--red)");
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * w},${h - 3 - ((v - mn) / rg) * (h - 6)}`).join(" ");
  return `<svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.8"/></svg>`;
}
function gauge(score) {
  const v = Math.max(0, Math.min(100, score || 0)), a0 = Math.PI * 0.8, a1 = Math.PI * 2.2, a = a0 + (a1 - a0) * v / 100;
  const P = t => `${80 + 62 * Math.cos(t)},${80 + 62 * Math.sin(t)}`;
  const arc = (from, to, col) => `<path d="M${P(from)} A62 62 0 ${to - from > Math.PI ? 1 : 0} 1 ${P(to)}" fill="none" stroke="${col}" stroke-width="12" stroke-linecap="round"/>`;
  return `<svg viewBox="0 0 160 150" width="100%" style="max-width:170px">${arc(a0, a1, "#14305f")}${v > 0 ? arc(a0, a, scoreColor(v)) : ""}
  <text x="80" y="82" text-anchor="middle" fill="#fff" font-size="42" font-weight="700" font-family="Rajdhani">${ok(score) ? Math.round(score) : "—"}</text>
  <text x="80" y="108" text-anchor="middle" fill="#8ea6cf" font-size="16" font-family="Rajdhani">/100</text></svg>`;
}
function candles(ch, range) {
  if (!ch || !ch.c || !ch.c.length) return `<div class="empty">No chart data</div>`;
  const n = { "1M": 22, "3M": 66, "6M": 130, "1Y": 260 }[range] || 66;
  const s = Math.max(0, ch.c.length - n), d = ch.d.slice(s), o = ch.o.slice(s), h = ch.h.slice(s), l = ch.l.slice(s), c = ch.c.slice(s);
  const ma = (k) => ch.c.map((_, i) => i >= k - 1 ? ch.c.slice(i - k + 1, i + 1).reduce((a, b) => a + b, 0) / k : null).slice(s);
  const m20 = ma(20), m50 = ma(50);
  const W = 560, H = 200, pad = 44, all = [...h, ...l, ...m20.filter(ok), ...m50.filter(ok)];
  const mn = Math.min(...all), mx = Math.max(...all), rg = mx - mn || 1;
  const y = v => 8 + (1 - (v - mn) / rg) * (H - 28), bw = (W - pad) / c.length, x = i => i * bw + bw / 2;
  let g = "";
  for (let k = 0; k <= 4; k++) { const v = mn + rg * k / 4; g += `<line x1="0" x2="${W - pad}" y1="${y(v)}" y2="${y(v)}" stroke="#12305f" stroke-dasharray="2 4"/><text x="${W - pad + 4}" y="${y(v) + 4}" fill="#8ea6cf" font-size="11">${v.toFixed(v > 100 ? 0 : 2)}</text>`; }
  let lastM = "";
  d.forEach((dd, i) => { const m = dd.slice(0, 7); if (m !== lastM) { lastM = m; g += `<text x="${x(i)}" y="${H - 4}" fill="#8ea6cf" font-size="11">${new Date(dd).toLocaleString("en", { month: "short" })}</text>`; } });
  const cs = c.map((cv, i) => { const up = cv >= o[i], col = up ? "#22d66b" : "#ff3b47";
    return `<line x1="${x(i)}" x2="${x(i)}" y1="${y(h[i])}" y2="${y(l[i])}" stroke="${col}"/><rect x="${x(i) - Math.max(1, bw * 0.32)}" y="${y(Math.max(o[i], cv))}" width="${Math.max(1.5, bw * 0.64)}" height="${Math.max(1, Math.abs(y(o[i]) - y(cv)))}" fill="${col}"/>`; }).join("");
  const line = (arr, col) => `<polyline fill="none" stroke="${col}" stroke-width="1.4" points="${arr.map((v, i) => ok(v) ? `${x(i)},${y(v)}` : "").filter(Boolean).join(" ")}"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-height:300px">${g}${cs}${line(m20, "#ffc928")}${line(m50, "#36a3ff")}</svg>
  <div class="mut" style="font-size:12px"><span style="color:#ffc928">━</span> 20-day avg &nbsp; <span style="color:#36a3ff">━</span> 50-day avg</div>`;
}

// ------------------------------------------------------------------ nav + status
const NAV = [["dashboard", "⌂", "Dashboard"], ["scan", "◎", "Opportunity Scan"], ["watchlist", "◉", "Watchlist"], ["portfolio", "▣", "My Portfolio"],
  ["demo", "◈", "Demo Account"], ["alerts", "🔔", "Alerts"], ["market", "▤", "Market Overview"], ["journal", "✎", "Trade Journal"], ["settings", "⚙", "Settings"]];
function renderNav(cur, unread = 0) {
  $("#nav").innerHTML = NAV.map(([k, i, l]) => `<a href="#/${k}" class="${cur === k ? "on" : ""}">${i} ${l}${k === "alerts" && unread ? ` <span class="n">${unread}</span>` : ""}</a>`).join("");
}
async function renderStatus() {
  try {
    const s = await api("/api/status");
    const m = s.market.status, mc = { OPEN: "g", "PRE-MARKET": "y", "AFTER-HOURS": "y", CLOSED: "r" }[m];
    const dl = s.data.label === "SYNTHETIC" ? `<span class="chip p">SYNTHETIC DEMO DATA</span>` : `<span class="chip y">DELAYED ~15 MIN</span>`;
    const w = s.worker.current && s.worker.current !== "idle" ? `<span class="chip b">⟳ ${esc(s.worker.current)}</span>` : "";
    $("#status").innerHTML = `<span class="chip ${mc}">US MARKET: ${m}</span>${dl}${w}
      <span class="chip hide-m">Last update: ${ago(s.data.last_update)}</span>
      <span class="chip hide-m">${new Date().toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</span>
      <button class="iconbtn" title="Refresh" onclick="route(true)">⟳</button>`;
  } catch (e) { $("#status").innerHTML = `<span class="chip r">SERVER UNREACHABLE</span>`; }
}

// ------------------------------------------------------------------ shared blocks
function oppTable(rows, opts = {}) {
  if (!rows.length) return `<div class="empty">${opts.empty || "No opportunities yet - press SCAN MARKET."}</div>`;
  const head = `<tr><th>#</th><th>Ticker</th><th>Price</th><th class="hide-m">Daily</th><th>AI Score</th><th>Action</th><th>Best Option</th>
    <th>Premium</th><th>Buy Trigger</th><th>Prob. Profit</th><th>2X Chance</th><th class="hide-m">EV</th><th>Risk</th><th class="hide-m">Key Catalyst</th></tr>`;
  const body = rows.map((r, i) => {
    const [d1, d2, d3, ...rest] = (r.contract || "").split(" ");
    return `<tr class="click ${S.sel === r.result_id ? "sel" : ""}" onclick="${opts.full ? `go('report/${r.result_id}')` : `selectOpp(${r.result_id})`}">
    <td>${i + 1}</td><td><div class="tk">${esc(r.ticker)}</div><div class="c2 mut">${r.direction === "BEARISH" ? "Bearish put" : "Bullish call"}</div></td>
    <td>${money(r.price)}</td><td class="hide-m ${cls(r.change_pct)}">${pct(r.change_pct, 2, true)}</td><td>${ring(r.score)}</td><td>${badge(r.state)}</td>
    <td class="c2">${esc([d1, d2, d3].join(" "))}<br><b>${esc(rest.join(" "))}</b></td><td>${money(r.premium)}</td>
    <td class="warn"><b>${r.state === "AVOID" ? "—" : "≤ " + money(r.trigger_upper)}</b></td><td>${pct(r.pop)}</td><td>${pct(r.p2x)}</td>
    <td class="hide-m ${cls(r.ev)}">${pct(r.ev, 0, true)}</td><td><span class="risk ${(r.risk || "").replace(" ", "")}">${esc(r.risk)}</span></td>
    <td class="hide-m c2">${esc(r.catalyst)}</td></tr>`;
  }).join("");
  const cards = rows.map(r => `<div class="card" onclick="go('report/${r.result_id}')"><div class="top"><span class="tk">${esc(r.ticker)}</span>${ring(r.score, 38)}${badge(r.state)}</div>
    <div class="c2">${esc(r.contract)} · premium ${money(r.premium)} · trigger ≤ ${money(r.trigger_upper)}</div>
    <div class="c2 mut">Profit ${pct(r.pop)} · 2X ${pct(r.p2x)} · EV ${pct(r.ev, 0, true)} · Risk ${esc(r.risk)}</div></div>`).join("");
  return `<div class="tblwrap scroll ${opts.tall ? "tall" : ""}"><table class="tbl">${head}${body}</table></div><div class="cards">${cards}</div>`;
}
function filterOpps(rows) {
  const f = S.filter, q = S.search.trim().toUpperCase();
  return rows.filter(r => !r.error && (!q || r.ticker.includes(q)) && (
    f === "All" || (f === "Bullish Calls" && r.direction === "BULLISH") || (f === "Bearish Puts" && r.direction === "BEARISH") ||
    (f === "Earnings" && r.earnings_date) || (f === "High IV" && r.iv_rv > 1.2) || (f === "LEAPS" && r.mode === "LEAPS")));
}
function scanBar(job) {
  const running = job && ["QUEUED", "RUNNING"].includes(job.status);
  const modes = [["FAST", "🔥 FAST 1-8w"], ["GROWTH", "⚡ GROWTH 2-6m"], ["LEAPS", "🧠 LEAPS 6-18m"]];
  return `<div class="chips"><select id="mode" onchange="S.mode=this.value">${modes.map(([k, l]) => `<option value="${k}" ${S.mode === k ? "selected" : ""}>${l}</option>`).join("")}</select>
    <button class="btn" id="scanbtn" ${running ? "disabled" : ""} onclick="startScan()">⌖ ${running ? "SCANNING…" : "SCAN MARKET"}</button></div>
    ${running ? `<div style="margin-top:8px"><div class="progress"><i style="width:${job.total ? 100 * job.progress / job.total : 3}%"></i></div>
    <div class="mut c2">${esc(job.message)} (${job.progress}/${job.total})</div></div>` : ""}`;
}
async function startScan() {
  try { const r = await api("/api/scan", { method: "POST", body: { mode: S.mode } }); S.job = r.job_id; toast(r.note || "Scan started in the background"); pollJob(); route(); }
  catch (e) { toast(e.message); }
}
async function pollJob() {
  if (!S.job) return;
  const j = await api(`/api/jobs/${S.job}`).catch(() => null);
  if (!j) return;
  if (["QUEUED", "RUNNING"].includes(j.status)) { const el = $("#scanzone"); if (el) el.innerHTML = scanBar(j); setTimeout(pollJob, 2000); }
  else { S.job = null; toast(j.message, 6000); route(); }
}
function actionList(items) {
  if (!items.length) return `<div class="empty">Nothing needs action. Add positions in My Portfolio to get HOLD / SELL signals.</div>`;
  return `<div class="aq">${items.map(a => `<div class="aqi"><div><div class="t">${esc(a.ticker)}</div><div class="m">${esc(a.contract || "")}</div></div>
    <div style="text-align:right">${badge(a.action)}<div class="m">${a.mark ? money(a.mark) : ""} <span class="${cls(a.pnl_pct)}">${pct(a.pnl_pct, 0, true)}</span> · ${ago(a.ts)}</div></div>
    <div class="w">${esc(a.reason || "")} <a href="#/${a.kind === "position" ? "position" : "report"}/${a.id}">details →</a></div></div>`).join("")}</div>`;
}
function planBlock(p) {
  const pl = p.plan, t = p.trigger, cost = p.contract.ask;
  const row = (col, ic, name, val, extra) => `<div class="row"><span class="ic" style="background:${col}">${ic}</span><b style="color:${col}">${name}</b><span>${val} <span class="mut c2">${extra || ""}</span></span></div>`;
  return `<div class="plan">
  ${row("var(--green)", "↗", "Buy Trigger", `<b>${money(t.lower)} – ${money(t.upper)}</b>`, t.breakout || "")}
  ${row("var(--yellow)", "+", "First Profit", `<b>${money(pl.first_price)}</b> <span class="pos">(${pct(pl.first_pct, 0, true)})</span>`, `stock ≈ ${money(pl.first_stock)}`)}
  ${row("#8fdc3c", "◎", "Main Target", `<b>${money(pl.main_price)}</b> <span class="pos">(${pct(pl.main_pct, 0, true)})</span>`, `stock ≈ ${money(pl.target_stock)}`)}
  ${row("var(--red)", "✕", "Cut / Exit", `<b>${money(pl.cut_price)}</b> <span class="neg">(${pct(pl.cut_pct, 0, true)})</span>`, esc(pl.thesis))}
  ${row("var(--blue)", "⏱", "Time Exit", `<b>${esc(pl.time_exit)}</b>`, "theta accelerates after this")}
  <div class="mut c2" style="margin-top:4px">Percentages vs current ask ${money(cost)}. Levels come from volatility, technical structure and the simulation.</div></div>`;
}
function simBlock(p) {
  const cols = ["#ff3b47", "#ff9a1f", "#36a3ff", "#8fdc3c", "#22d66b"];
  const b = p.sim.buckets.map((x, i) => `<div class="row"><span class="sw" style="background:${cols[i]}"></span><span>${esc(x.label)} <span class="mut c2">(${esc(x.note)})</span></span>
    <b>${money(x.value, 0)}</b><span class="${cls(x.value - 1000)}">${pct(x.value / 1000 - 1, 0, true)}</span></div>`).join("");
  const c = p.sim.chances;
  return `<div class="sim">${b}</div>
  <div class="mut c2" style="margin:6px 0">${esc(p.sim.label)} · ${p.sim.n_paths.toLocaleString()} paths · EV ${smoney(p.sim.ev)} per $1,000</div>
  <div class="rings">${probRing(c.profit, "var(--green)", "Chance of profit")}${probRing(c.plus100, "var(--blue)", "Chance of +100%")}${probRing(c.lose_most, "var(--red)", "Lose most/all")}</div>
  <div class="kv c2" style="margin-top:8px"><span>Chance of +50%</span><span>${pct(c.plus50)}</span><span>Chance of +200%</span><span>${pct(c.plus200)}</span>
  <span>Chance of losing >50%</span><span>${pct(c.lose50)}</span></div>`;
}
function verdictBox(p) {
  const col = COLOR[p.state] || "blue";
  return `<div class="verdict" style="border-color:var(--${col === "black" ? "dim" : col})"><div class="mut c2">FINAL VERDICT</div>
    <b style="color:var(--${col === "black" ? "mut" : col})">${verdictText(p.state)}</b><div class="c2">${esc(p.headline)}</div></div>`;
}
function contractStats(p) {
  const c = p.contract, f = p.fair;
  return `<div class="stats"><span>Option price (ask)</span><span>${money(c.ask)}</span><span>Model fair value</span><span>${money(f.model)} <span class="${f.diff > 0 ? "neg" : "pos"}">(${pct(f.diff, 0, true)})</span></span>
  <span>Fair value verdict</span><span>${esc(f.label)}</span><span>Implied volatility</span><span>${pct(c.iv, 1)}</span><span>Delta / Gamma</span><span>${c.delta} / ${c.gamma}</span>
  <span>Theta (daily)</span><span>${money(c.theta)}</span><span>Open interest</span><span>${(c.oi || 0).toLocaleString()}</span><span>Volume</span><span>${(c.volume || 0).toLocaleString()}</span>
  <span>Bid / Ask</span><span>${money(c.bid)} / ${money(c.ask)}</span><span>Break-even</span><span>${money(c.breakeven)}</span></div>`;
}
async function getReport(id, kind = "report") {
  const key = kind + id;
  if (!S.reports[key]) S.reports[key] = await api(kind === "report" ? `/api/report/${id}` : `/api/positions/${id}/report`);
  return S.reports[key];
}

// ------------------------------------------------------------------ DASHBOARD
async function viewDashboard() {
  const [d, latest] = await Promise.all([api("/api/dashboard"), api("/api/jobs/latest").catch(() => null)]); S.dash = d;
  if (latest && ["QUEUED", "RUNNING"].includes(latest.status)) S.job = latest.id;
  renderNav("dashboard", d.alerts_unread);
  const pf = d.portfolio, dm = d.demo;
  const demoBanner = d.data.mode === "demo" ? `<div class="banner">DEMO DATA MODE - every price below is synthetic and for testing only. Set DATA_MODE=live for real (delayed) market data.</div>` : "";
  const tiles = d.indices.map(x => `<div class="tile"><div class="l">${esc(x.label)} ${fr(x.meta.freshness)}</div><div class="v">${ok(x.value) ? x.value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}</div>
    <div class="c ${cls(x.change)}">${pct(x.change, 2, true)}</div>${spark(x.spark, 110, 30)}</div>`).join("");
  const opps = filterOpps(d.opportunities);
  if (!S.sel && opps.length) S.sel = opps[0].result_id;
  V().innerHTML = `${demoBanner}<div class="grid dash">
  <section class="panel a-pf"><h2>Portfolio (Real) <span class="sp"></span><a class="c2" href="#/portfolio">open →</a></h2>
    <div class="acct"><div><div class="big">${money(pf.value, 0)}</div><div class="${cls(pf.total_pl)}" style="font-size:20px;font-weight:700">${smoney(pf.total_pl)} (${pct(pf.total_pl_pct, 1, true)})</div></div>
    <div></div><div class="kv"><span>Day P/L</span><span class="${cls(pf.day_pl)}">${smoney(pf.day_pl)}</span><span>Unrealised</span><span class="${cls(pf.unrealised)}">${smoney(pf.unrealised)}</span>
    <span>Realised</span><span class="${cls(pf.realised)}">${smoney(pf.realised)}</span><span>Cash</span><span>${money(pf.cash, 0)}</span><span>At risk</span><span>${money(pf.at_risk, 0)}</span></div></div></section>
  <section class="panel a-demo"><h2>Demo Account <span class="sp"></span>${dm.auto ? `<span class="chip g">AUTO ON</span>` : ""}<a class="c2" href="#/demo">open →</a></h2>
    <div class="acct"><div><div class="big">${money(dm.equity, 0)}</div><div class="${cls(dm.total_return)}" style="font-size:20px;font-weight:700">${smoney(dm.equity - dm.start)} (${pct(dm.total_return, 1, true)})</div></div>
    <div>${spark(dm.curve, 120, 50)}</div><div class="kv"><span>Day P/L</span><span class="${cls(dm.day_pl)}">${smoney(dm.day_pl)}</span><span>Win rate</span><span>${pct(dm.win_rate)}</span>
    <span>Total trades</span><span>${dm.trades}</span><span>Max drawdown</span><span class="neg">${pct(dm.max_drawdown, 1)}</span></div></div></section>
  <section class="panel a-mkt"><h2>Market Overview <span class="sp"></span>${d.indices.length ? fr(d.indices[0].meta.freshness) : ""}</h2><div class="tiles">${tiles}</div>
    <div class="c2 dim" style="margin-top:4px">${d.indices.length ? esc(d.indices[0].meta.source) + " · " + ago(d.indices[0].meta.timestamp) : ""}</div></section>
  <section class="panel a-opp"><h2>AI Top Opportunities <span class="sp"></span><span class="mut c2">${d.scan ? `${esc(d.scan.mode)} scan · ${ago(d.scan.finished)}` : ""}</span></h2>
    <div class="chips" style="margin-bottom:8px">${["All", "Bullish Calls", "Bearish Puts", "Earnings", "High IV", "LEAPS"].map(f => `<button class="${S.filter === f ? "on" : ""}" onclick="S.filter='${f}';route()">${f}</button>`).join("")}
    <span class="search">⌕<input placeholder="Search ticker…" value="${esc(S.search)}" oninput="S.search=this.value;rerenderOpps()"></span><span id="scanzone">${scanBar(latest)}</span></div>
    ${d.no_trade ? `<div class="notrade">NO HIGH-QUALITY TRADE FOUND — CASH IS A POSITION</div>` : ""}
    <div id="opps">${oppTable(opps)}</div></section>
  <section class="panel alert a-act"><h2>⚠ Action Required (${d.actions.length}) <span class="sp"></span><a class="c2" href="#/alerts">view all</a></h2>${actionList(d.actions)}</section>
  <section class="panel a-pos"><h2>My Portfolio (Real) <span class="sp"></span><a class="btn sm" href="#/portfolio">+ Add Position</a></h2><div id="dpos">Loading…</div></section>
  <section class="panel a-ana" id="ana"><div class="empty">Select an opportunity</div></section>
  <section class="panel a-side" id="side"></section></div>`;
  if (S.job) pollJob();
  api("/api/portfolio").then(p => { const el = $("#dpos"); if (el) el.innerHTML = posTable(p.positions, true); });
  if (S.sel) selectOpp(S.sel, true);
}
function rerenderOpps() { if (S.dash) $("#opps").innerHTML = oppTable(filterOpps(S.dash.opportunities)); }
async function selectOpp(id, keep) {
  S.sel = id;
  if (!keep) rerenderOpps();
  let p; try { p = await getReport(id); } catch (e) { return; }
  const a = $("#ana"), sd = $("#side"); if (!a) return;
  a.innerHTML = `<h2>${esc(p.ticker)} Option Analysis <span class="sp"></span><span class="ranges">${["1M", "3M", "6M", "1Y"].map(r => `<button class="${S.range === r ? "on" : ""}" onclick="S.range='${r}';selectOpp(${id},true)">${r}</button>`).join("")}</span></h2>
    <div class="ana"><div><div style="font-size:20px"><b>${esc(p.ticker)}</b> ${money(p.price)} <span class="${cls(p.change_pct)}">${pct(p.change_pct, 2, true)}</span> ${fr(p.chain_status)}</div>
    ${candles(p.chart, S.range)}<div style="font-size:18px;font-weight:700;margin:6px 0">${esc(p.contract.name)} <span class="mut">(${esc(p.contract.strategy)})</span></div>${contractStats(p)}</div>
    <div><div class="mut" style="text-align:center;font-weight:700">AI SCORE</div>${gauge(p.score)}${verdictBox(p)}
    <p class="c2">${p.why.map(esc).join(" ")}</p><a class="btn" style="display:block;text-align:center" href="#/report/${id}">View Full Analysis →</a></div></div>`;
  sd.innerHTML = `<h2>$1,000 Investment Simulation</h2>${simBlock(p)}<h2 style="margin-top:12px">Trade Plan</h2>${planBlock(p)}`;
}
function posTable(rows, compact) {
  if (!rows.length) return `<div class="empty">No positions yet. Add the options you own to get HOLD / TAKE PROFIT / SELL signals.</div>`;
  const tr = rows.map(r => { const e = r.eval || {};
    return `<tr class="click" onclick="go('position/${r.id}')"><td class="tk">${esc(r.ticker)}</td><td class="c2">${esc(r.contract)}</td><td>${r.qty}</td><td>${money(r.entry_premium)}</td>
    <td>${money(e.mark)}</td><td class="${cls(e.pnl_pct)}"><b>${pct(e.pnl_pct, 1, true)}</b></td>
    ${compact ? "" : `<td class="${cls(e.pnl)}">${smoney(e.pnl)}</td><td class="${cls(e.day_change)}">${smoney(e.day_change)}</td><td>${e.dte ?? "—"}</td>
      <td>${pct(e.hold?.pop)}</td><td class="${cls(e.hold?.ev)}">${pct(e.hold?.ev, 0, true)}</td><td class="neg">${smoney(e.theta_day)}</td>
      <td>${badge(e.would_buy || "PENDING")}${e.would_buy === "ONLY AT LOWER PRICE" ? `<div class="c2">≤ ${money(e.would_buy_price)}</div>` : ""}</td>`}
    <td>${badge(e.action || "PENDING")}</td></tr>`; }).join("");
  const head = `<tr><th>Ticker</th><th>Contract</th><th>Qty</th><th>Avg</th><th>Current</th><th>P/L %</th>${compact ? "" :
    "<th>P/L $</th><th>Day</th><th>DTE</th><th>Prob. from here</th><th>EV from here</th><th>Theta/day</th><th>Would AI buy today?</th>"}<th>AI Signal</th></tr>`;
  const cards = rows.map(r => { const e = r.eval || {};
    return `<div class="card" onclick="go('position/${r.id}')"><div class="top"><span class="tk">${esc(r.ticker)}</span>${badge(e.action || "PENDING", true)}</div>
    <div class="c2">${esc(r.contract)} × ${r.qty} · avg ${money(r.entry_premium)} → ${money(e.mark)} <b class="${cls(e.pnl_pct)}">${pct(e.pnl_pct, 1, true)}</b></div>
    <div class="c2 mut">${esc(e.reason || "Waiting for first evaluation…")}</div>
    <div class="c2">Would AI buy today? <b>${esc(e.would_buy || "—")}</b>${e.would_buy === "ONLY AT LOWER PRICE" ? ` (≤ ${money(e.would_buy_price)})` : ""}</div></div>`; }).join("");
  return `<div class="tblwrap scroll"><table class="tbl">${head}${tr}</table></div><div class="cards">${cards}</div>`;
}

// ------------------------------------------------------------------ OPPORTUNITY SCAN
async function viewScan() {
  renderNav("scan");
  const o = await api("/api/opportunities");
  if (o.job && ["QUEUED", "RUNNING"].includes(o.job.status)) S.job = o.job.id;
  const latest = await api("/api/jobs/latest").catch(() => null);
  if (latest && ["QUEUED", "RUNNING"].includes(latest.status)) S.job = latest.id;
  const rows = filterOpps(o.rows), errs = o.rows.filter(r => r.error);
  V().innerHTML = `<div class="grid"><section class="panel"><h2>AI Market Hunter <span class="sp"></span><span class="mut c2">${o.job ? `Last: ${esc(o.job.mode)} · ${esc(o.job.universe.split(",").length)} tickers · ${ago(o.job.finished)}` : "No scan yet"}</span></h2>
    <p class="mut c2">Scans the universe in Settings for bullish calls and bearish puts, ranked by probability-adjusted expected value. Runs in the background; ${"10,000"}+ simulations per ticker. It is allowed to find nothing.</p>
    <div id="scanzone">${scanBar(latest)}</div>
    <div class="chips" style="margin:10px 0">${["All", "Bullish Calls", "Bearish Puts", "Earnings", "High IV", "LEAPS"].map(f => `<button class="${S.filter === f ? "on" : ""}" onclick="S.filter='${f}';route()">${f}</button>`).join("")}
    <span class="search">⌕<input placeholder="Search ticker…" value="${esc(S.search)}" oninput="S.search=this.value;route()"></span></div>
    ${o.no_trade ? `<div class="notrade">NO HIGH-QUALITY TRADE FOUND — CASH IS A POSITION</div>` : ""}
    ${oppTable(rows, { full: true, tall: true })}
    ${errs.length ? `<details style="margin-top:10px"><summary class="mut">${errs.length} skipped</summary><ul class="tight c2">${errs.map(e => `<li>${esc(e.ticker)} ${esc(e.direction || "")}: ${esc(e.error)}</li>`).join("")}</ul></details>` : ""}
  </section></div>`;
  if (S.job) pollJob();
}

// ------------------------------------------------------------------ WATCHLIST
async function viewWatch() {
  renderNav("watchlist");
  const rows = await api("/api/watchlist");
  const q = S.wsearch.toUpperCase(), f = S.wfilter;
  let list = rows.filter(r => (!q || r.ticker.includes(q)) && (f === "All" || (f === "Favourites" ? r.favourite : r.state === f)));
  const k = S.wsort.k, dir = S.wsort.d;
  list.sort((a, b) => (b.favourite - a.favourite) || (((a[k] ?? -1e9) > (b[k] ?? -1e9) ? 1 : (a[k] ?? -1e9) < (b[k] ?? -1e9) ? -1 : 0) * dir));
  const th = (key, label) => `<th class="s" onclick="S.wsort={k:'${key}',d:S.wsort.k==='${key}'?-S.wsort.d:-1};route()">${label}${S.wsort.k === key ? (S.wsort.d > 0 ? " ▲" : " ▼") : ""}</th>`;
  const tr = list.map(r => `<tr><td><span style="cursor:pointer;color:${r.favourite ? "var(--yellow)" : "var(--dim)"}" onclick="fav(${r.id})">★</span></td>
    <td class="tk">${esc(r.ticker)}<div class="c2 mut">${esc(r.mode || "")}</div></td><td>${money(r.price)} ${fr(r.quote_fresh)}</td><td class="${cls(r.change_pct)}">${pct(r.change_pct, 2, true)}</td>
    <td>${ring(r.score)}</td><td>${badge(r.state)}${r.pending_state ? `<div class="c2 mut">confirming ${esc(r.pending_state)}…</div>` : ""}</td>
    <td class="c2">${esc(r.contract || "—")}</td><td>${esc(r.expiry || "—")}</td><td>${r.strike ? money(r.strike) : "—"}</td><td>${money(r.premium)}</td>
    <td class="warn">${r.trigger_upper ? "≤ " + money(r.trigger_upper) : "—"}</td><td>${pct(r.pop)}</td><td>${pct(r.p2x)}</td><td class="${cls(r.ev)}">${pct(r.ev, 0, true)}</td>
    <td><span class="risk ${(r.risk || "").replace(" ", "")}">${esc(r.risk || "—")}</span></td><td class="c2">${esc(r.catalyst || "")}</td>
    <td>${r.result_id ? `<a class="btn sm ghost" href="#/report/${r.result_id}">Report</a>` : ""} <button class="btn sm ghost" onclick="unwatch(${r.id})">✕</button></td></tr>`).join("");
  const cards = list.map(r => `<div class="card"><div class="top"><span class="tk" onclick="fav(${r.id})">${r.favourite ? "★ " : ""}${esc(r.ticker)}</span>${ring(r.score, 36)}${badge(r.state)}</div>
    <div class="c2">${money(r.price)} <span class="${cls(r.change_pct)}">${pct(r.change_pct, 2, true)}</span> · ${esc(r.contract || "")} · trigger ≤ ${money(r.trigger_upper)}</div>
    <div class="c2 mut">Profit ${pct(r.pop)} · 2X ${pct(r.p2x)} · EV ${pct(r.ev, 0, true)}</div>
    <div>${r.result_id ? `<a class="btn sm ghost" href="#/report/${r.result_id}">Report</a>` : ""} <button class="btn sm ghost" onclick="unwatch(${r.id})">Remove</button></div></div>`).join("");
  V().innerHTML = `<div class="grid"><section class="panel"><h2>Live Opportunity Watchlist <span class="sp"></span><button class="btn sm ghost" onclick="api('/api/watchlist/refresh',{method:'POST'}).then(r=>toast(r.note))">⟳ Re-run engine</button></h2>
    <div class="chips" style="margin-bottom:10px"><input id="wt" placeholder="Ticker e.g. NVDA" style="width:130px" onkeydown="if(event.key==='Enter')addWatch()">
    <select id="wm">${["FAST", "GROWTH", "LEAPS"].map(m => `<option ${m === "GROWTH" ? "selected" : ""}>${m}</option>`).join("")}</select><button class="btn" onclick="addWatch()">+ Add Ticker</button>
    <span class="search">⌕<input placeholder="Search…" value="${esc(S.wsearch)}" oninput="S.wsearch=this.value;route()"></span>
    ${["All", "Favourites", "BUY NOW", "BUY IF TRIGGERED", "WAIT", "AVOID"].map(x => `<button class="${S.wfilter === x ? "on" : ""}" onclick="S.wfilter='${x}';route()">${x}</button>`).join("")}</div>
    <p class="mut c2">Prices refresh automatically. A recommendation only changes after the full decision engine re-runs and confirms it twice (or the score moves decisively) — small price wiggles never flip a signal.</p>
    ${list.length ? `<div class="tblwrap scroll tall"><table class="tbl"><tr><th></th>${th("ticker", "Ticker")}${th("price", "Price")}${th("change_pct", "Daily %")}${th("score", "AI Score")}${th("state", "Action")}
    <th>Best Option</th>${th("expiry", "Expiry")}<th>Strike</th>${th("premium", "Premium")}<th>Buy Trigger</th>${th("pop", "Prob. Profit")}${th("p2x", "2X")}${th("ev", "EV")}<th>Risk</th><th>Catalyst</th><th></th></tr>${tr}</table></div>
    <div class="cards">${cards}</div>` : `<div class="empty">Watchlist empty. Add a ticker above.</div>`}</section></div>`;
}
async function addWatch() {
  const t = $("#wt").value.trim(); if (!t) return;
  try { await api("/api/watchlist", { method: "POST", body: { ticker: t, mode: $("#wm").value } }); toast(`${t.toUpperCase()} added - analysing in the background`); route(); }
  catch (e) { toast(e.message); }
}
const fav = id => api(`/api/watchlist/${id}/favourite`, { method: "POST" }).then(route);
const unwatch = id => confirm("Remove from watchlist?") && api(`/api/watchlist/${id}`, { method: "DELETE" }).then(route);

// ------------------------------------------------------------------ PORTFOLIO
async function viewPortfolio() {
  renderNav("portfolio");
  const p = await api("/api/portfolio"), s = p.summary;
  V().innerHTML = `<div class="grid cols4">
    ${[["Current value", money(s.value, 0)], ["Day P/L", `<span class="${cls(s.day_pl)}">${smoney(s.day_pl)}</span>`], ["Total P/L", `<span class="${cls(s.total_pl)}">${smoney(s.total_pl)} (${pct(s.total_pl_pct, 1, true)})</span>`],
      ["Capital at risk", money(s.at_risk, 0)]].map(([l, v]) => `<section class="panel"><div class="mut">${l}</div><div class="big" style="font-size:30px">${v}</div></section>`).join("")}</div>
  <div class="grid"><section class="panel"><h2>Owned Options <span class="sp"></span><button class="btn sm ghost" onclick="api('/api/positions/refresh',{method:'POST'}).then(()=>toast('Re-evaluating in the background'))">⟳ Re-evaluate</button></h2>
    ${s.unpriced ? `<div class="banner warn">${s.unpriced} position(s) have no current option quote (market closed?). Nothing is estimated for them.</div>` : ""}
    ${posTable(p.positions, false)}
    <p class="mut c2">Signals are judged from today's sell price forward — what you paid does not change the decision (no sunk-cost bias). Cash for "Current value" is set in Settings.</p></section>
  <section class="panel"><h2>+ Add Position <span class="mut c2">(manual entry — this app never places real trades)</span></h2>
    <div class="form"><label>Ticker<input id="p_t" placeholder="NVDA"></label><label>Type<select id="p_k"><option value="call">CALL</option><option value="put">PUT</option></select></label>
    <label>Strike<input id="p_s" type="number" step="0.5"></label><label>Expiration<input id="p_e" type="date"></label><label>Quantity<input id="p_q" type="number" value="1" min="1"></label>
    <label>Purchase premium (per share)<input id="p_p" type="number" step="0.01"></label><label>Purchase date<input id="p_d" type="date"></label>
    <button class="btn" onclick="addPos()">Add</button></div></section>
  ${p.closed.length ? `<section class="panel"><h2>Closed Positions</h2><div class="scroll"><table class="tbl"><tr><th>Ticker</th><th>Contract</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Realised</th><th>Reason</th></tr>
    ${p.closed.map(c => `<tr><td class="tk">${esc(c.ticker)}</td><td>${esc(c.expiry)} $${c.strike} ${esc(c.kind.toUpperCase())}</td><td>${c.qty}</td><td>${money(c.entry_premium)}</td><td>${money(c.exit_premium)}</td>
    <td class="${cls(c.realised)}">${smoney(c.realised)}</td><td class="c2">${esc(c.exit_reason)}</td></tr>`).join("")}</table></div></section>` : ""}</div>`;
}
async function addPos() {
  const b = { ticker: $("#p_t").value, kind: $("#p_k").value, strike: $("#p_s").value, expiry: $("#p_e").value, qty: $("#p_q").value, premium: $("#p_p").value, entry_date: $("#p_d").value };
  try { await api("/api/positions", { method: "POST", body: b }); toast("Added - first AI evaluation runs in the background (~30s)"); route(); } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ DEMO ACCOUNT
async function viewDemo() {
  renderNav("demo");
  const d = await api("/api/demo"), m = d.metrics;
  const card = (l, v, c = "") => `<section class="panel"><div class="mut">${l}</div><div class="${c}" style="font-size:26px;font-weight:700">${v}</div></section>`;
  V().innerHTML = `<div class="banner">Virtual money only. Fills use the real (delayed) ask when buying and bid when selling. History is never rewritten.</div>
  <div class="grid cols4">${card("Current equity", money(m.equity, 0))}${card("Cash", money(m.cash, 0))}${card("Total return", pct(m.total_return, 1, true), cls(m.total_return))}${card("Max drawdown", pct(m.max_drawdown, 1), "neg")}
  ${card("Realised P/L", smoney(m.realised), cls(m.realised))}${card("Unrealised P/L", smoney(m.unrealised), cls(m.unrealised))}${card("Win rate", `${pct(m.win_rate)} <span class="mut c2">(${m.closed} closed)</span>`)}
  ${card("Profit factor", ok(m.profit_factor) ? m.profit_factor.toFixed(2) : "—")}${card("Average winner", smoney(m.avg_winner), "pos")}${card("Average loser", smoney(m.avg_loser), "neg")}${card("Trades", m.trades)}
  <section class="panel"><div class="mut">AI AUTO DEMO TRADING</div><label class="chips" style="font-size:18px;font-weight:700"><input type="checkbox" ${m.auto ? "checked" : ""}
    onchange="api('/api/settings',{method:'POST',body:{auto_demo:this.checked}}).then(()=>{toast('Auto demo trading '+(this.checked?'ON':'OFF'));route()})"> ${m.auto ? "ON" : "OFF"}</label>
    <div class="c2 mut">Buys only BUY NOW signals that pass every rule; risk budget & limits in Settings.</div></section></div>
  <div class="grid"><section class="panel"><h2>Equity curve</h2>${spark(m.curve, 600, 90)}</section>
  <section class="panel"><h2>Open demo positions</h2>${d.positions.length ? `<div class="scroll"><table class="tbl"><tr><th>Ticker</th><th>Contract</th><th>Qty</th><th>Entry</th><th>Mark</th><th>P/L</th><th>AI signal</th><th>Auto</th><th></th></tr>
    ${d.positions.map(p => `<tr><td class="tk">${esc(p.ticker)}</td><td class="c2">${esc(p.contract)}</td><td>${p.qty}</td><td>${money(p.entry_premium)}</td><td>${money(p.eval.mark)}</td>
    <td class="${cls(p.eval.pnl_pct)}">${pct(p.eval.pnl_pct, 1, true)}</td><td>${badge(p.eval.action || "PENDING")}</td><td>${p.auto ? "🤖" : ""}</td>
    <td><button class="btn sm red" onclick="paperSell(${p.id},${p.qty})">Sell</button> <button class="btn sm orange" onclick="paperSell(${p.id},null,true)">Partial</button> <a class="btn sm ghost" href="#/position/${p.id}">Report</a></td></tr>`).join("")}</table></div>` : `<div class="empty">No open demo positions. Paper-buy from any report, or below.</div>`}</section>
  <section class="panel"><h2>Paper Buy (manual contract)</h2><div class="form"><label>Ticker<input id="d_t"></label><label>Type<select id="d_k"><option value="call">CALL</option><option value="put">PUT</option></select></label>
    <label>Strike<input id="d_s" type="number" step="0.5"></label><label>Expiration<input id="d_e" type="date"></label><label>Contracts<input id="d_q" type="number" value="1" min="1"></label>
    <button class="btn green" onclick="paperBuyManual()">Paper Buy at ask</button></div></section>
  <section class="panel"><h2>Trade history</h2>${tradeTable(d.trades)}</section>
  <section class="panel"><h2>Automated decision log</h2>${d.auto_log.length ? `<div class="scroll"><table class="tbl"><tr><th>Time</th><th>Ticker</th><th>Decision</th><th>Reason</th></tr>
    ${d.auto_log.map(a => `<tr><td class="c2">${esc(a.ts)}</td><td class="tk">${esc(a.ticker)}</td><td>${badge(a.decision === "BUY" ? "BUY NOW" : a.decision === "SELL" ? "SELL" : "WAIT")}</td><td class="c2">${esc(a.reason)}</td></tr>`).join("")}</table></div>` : `<div class="empty">No automated decisions yet.</div>`}</section></div>`;
}
const tradeTable = t => t.length ? `<div class="scroll"><table class="tbl"><tr><th>Time</th><th>Side</th><th>Ticker</th><th>Contract</th><th>Qty</th><th>Premium</th><th>Entry score</th><th>Entry prob.</th><th>Entry EV</th><th>P/L</th><th>Held</th><th>Reason</th></tr>
  ${t.map(x => `<tr><td class="c2">${esc(x.ts)}</td><td><span class="bd ${x.side === "BUY" ? "green" : "red"}">${x.side}</span></td><td class="tk">${esc(x.ticker)}</td><td class="c2">${esc(x.contract)}</td><td>${x.qty}</td><td>${money(x.premium)}</td>
  <td>${ok(x.score) ? Math.round(x.score) : "—"}</td><td>${pct(x.pop)}</td><td>${pct(x.ev, 0, true)}</td><td class="${cls(x.pnl)}">${smoney(x.pnl)}</td><td>${ok(x.holding_days) ? x.holding_days + "d" : "—"}</td>
  <td class="c2">${esc(x.reason)}${x.auto ? " 🤖" : ""}</td></tr>`).join("")}</table></div>` : `<div class="empty">No trades yet.</div>`;
async function paperSell(id, qty, partial) {
  if (partial) { qty = prompt("How many contracts to sell?", "1"); if (!qty) return; }
  else if (!confirm("Paper-sell the whole position at the current bid?")) return;
  try { const r = await api("/api/demo/sell", { method: "POST", body: { position_id: id, qty } }); toast(`Sold. P/L ${smoney(r.pnl)}`); route(true); } catch (e) { toast(e.message); }
}
async function paperBuyManual() {
  const b = { ticker: $("#d_t").value, kind: $("#d_k").value, strike: $("#d_s").value, expiry: $("#d_e").value, qty: $("#d_q").value };
  try { await api("/api/demo/buy", { method: "POST", body: b }); toast("Paper position opened"); route(true); } catch (e) { toast(e.message); }
}
async function paperBuyRec(rid) {
  const qty = prompt("Contracts to paper-buy at the current ask?", "1"); if (!qty) return;
  try { await api("/api/demo/buy", { method: "POST", body: { result_id: rid, qty } }); toast("Paper position opened in the Demo Account"); } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ ALERTS
async function viewAlerts() {
  const a = await api("/api/alerts");
  renderNav("alerts", a.alerts.filter(x => !x.is_read).length);
  const col = u => u >= 85 ? "red" : u >= 65 ? "orange" : u >= 50 ? "green" : "blue";
  V().innerHTML = `<div class="grid"><section class="panel alert"><h2>Action Required</h2><div id="aq">Loading…</div></section>
  <section class="panel"><h2>Alert History <span class="sp"></span><button class="btn sm ghost" onclick="api('/api/alerts/read',{method:'POST'}).then(route)">Mark all read</button></h2>
    ${a.alerts.length ? `<div class="scroll tall"><table class="tbl"><tr><th>Time</th><th>Type</th><th>Ticker</th><th>Alert</th><th>Details</th></tr>${a.alerts.map(x => `<tr style="${x.is_read ? "opacity:.65" : ""}">
    <td class="c2">${ago(x.ts)}</td><td><span class="bd ${col(x.urgency)}">${esc(a.types[x.type] || x.type)}</span></td><td class="tk">${esc(x.ticker)}</td><td><b>${esc(x.title)}</b></td><td class="c2">${esc(x.message)}</td></tr>`).join("")}</table></div>`
    : `<div class="empty">No alerts yet.</div>`}</section>
  <section class="panel"><h2>Alert settings</h2><div class="form">${Object.entries(a.types).map(([k, l]) => `<label class="chk"><input type="checkbox" data-k="${k}" ${a.enabled[k] ? "checked" : ""}
    onchange="api('/api/settings',{method:'POST',body:{alerts:{[this.dataset.k]:this.checked}}}).then(()=>toast('Saved'))"> ${esc(l)}</label>`).join("")}</div></section></div>`;
  api("/api/actions").then(x => { const el = $("#aq"); if (el) el.innerHTML = actionList(x); });
}

// ------------------------------------------------------------------ MARKET
async function viewMarket() {
  renderNav("market");
  const m = await api("/api/market");
  V().innerHTML = `<div class="grid"><section class="panel"><h2>US Market: ${esc(m.market.status)} <span class="sp"></span><span class="mut c2">${esc(m.market.ny_time)}</span></h2>
    <div class="tiles">${m.indices.map(x => `<div class="tile"><div class="l">${esc(x.label)} ${fr(x.meta.freshness)}</div><div class="v" style="font-size:26px">${ok(x.value) ? x.value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}</div>
    <div class="c ${cls(x.change)}">${pct(x.change, 2, true)}</div>${spark(x.spark, 160, 50)}<div class="c2 dim">${esc(x.meta.source)} · ${esc(x.meta.note || "")}</div></div>`).join("")}</div></section>
  <section class="panel"><h2>Macro Engine (Soros) <span class="sp"></span><span class="bd ${m.macro.label === "RISK-ON" ? "green" : m.macro.label === "RISK-OFF" ? "red" : "yellow"}">${esc(m.macro.label)} · ${Math.round(m.macro.score)}</span></h2>
    <ul class="tight">${m.macro.notes.map(n => `<li>${esc(n)}</li>`).join("") || "<li>No notable macro signals</li>"}</ul>
    <p class="c2 mut">Risk-free rate ${pct(m.macro.r, 2)} (${esc(m.macro.r_source)}) · data as of ${esc(m.macro.as_of)} ${m.source ? fr(m.source.freshness) : ""}
    ${m.macro.flags.length ? "<br>⚠ " + m.macro.flags.map(esc).join("; ") : ""}</p>
    <p class="c2 mut">The macro score tilts the simulation's bull/bear regime weights and feeds every Entry Score.</p></section></div>`;
}

// ------------------------------------------------------------------ JOURNAL
async function viewJournal() {
  renderNav("journal");
  const j = await api("/api/journal"), s = j.stats;
  const sumRow = (k, x) => `<tr><td><b>${esc(k)}</b></td><td>${x.n}</td><td>${pct(x.win_rate)}</td><td class="${cls(x.avg)}">${pct(x.avg, 1, true)}</td><td>${pct(x.median, 1, true)}</td>
    <td>${ok(x.profit_factor) ? x.profit_factor.toFixed(2) : "—"}</td><td class="neg">${pct(x.max_drawdown, 1)}</td></tr>`;
  const grp = (title, g) => `<section class="panel"><h2>${title}</h2>${Object.keys(g).length ? `<div class="scroll"><table class="tbl"><tr><th>Group</th><th>Closed</th><th>Win rate</th><th>Avg</th><th>Median</th><th>Profit factor</th><th>Max DD</th></tr>
    ${Object.entries(g).map(([k, x]) => sumRow(k, x)).join("")}</table></div>` : `<div class="empty">No closed outcomes yet.</div>`}</section>`;
  const a = s.ai_buys;
  V().innerHTML = `<div class="banner warn">The journal stores EVERY AI recommendation with its probability, EV and simulated distribution, then records what actually happened at the planned time-exit.
    Results are never edited. Until outcomes accumulate, treat all model probabilities as unproven.</div>
  <div class="grid cols4">${[["Recommendations logged", s.total], ["Outcomes known", s.closed], ["AI BUY win rate", a.n ? pct(a.win_rate) : "—"], ["AI BUY avg return", a.n ? pct(a.avg, 1, true) : "—"],
    ["Median return", a.n ? pct(a.median, 1, true) : "—"], ["Profit factor", a.n && ok(a.profit_factor) ? a.profit_factor.toFixed(2) : "—"], ["Max drawdown (5%/trade)", a.n ? pct(a.max_drawdown, 1) : "—"], ["Still open", s.open]]
    .map(([l, v]) => `<section class="panel"><div class="mut">${l}</div><div style="font-size:26px;font-weight:700">${v}</div></section>`).join("")}</div>
  <div class="grid cols2">
  <section class="panel"><h2>Calibration — are the probabilities honest?</h2>${s.calibration.length ? `<table class="tbl"><tr><th>Predicted profit chance</th><th>n</th><th>Predicted</th><th>Actual</th></tr>
    ${s.calibration.map(c => `<tr><td>${c.bucket}</td><td>${c.n}</td><td>${pct(c.predicted)}</td><td class="${Math.abs(c.actual - c.predicted) > 0.1 ? "warn" : "pos"}">${pct(c.actual)}</td></tr>`).join("")}</table>`
    : `<div class="empty">Needs closed outcomes. If "actual" ends up far below "predicted", lower your trust in the model (and raise thresholds in Settings).</div>`}</section>
  ${grp("Results by AI score", s.by_score)}${grp("Results by strategy", s.by_strategy)}${grp("Results by holding period", s.by_holding)}${grp("Results by market regime", s.by_regime)}${grp("Results by recommendation", s.by_state)}</div>
  <div class="grid"><section class="panel"><h2>Recommendation log</h2>${j.entries.length ? `<div class="scroll tall"><table class="tbl"><tr><th>Time</th><th>Ticker</th><th>Rec.</th><th>Score</th><th>Contract</th><th>Ask</th><th>Trigger</th><th>Prob.</th><th>EV</th><th>Time exit</th><th>Regime</th><th>Outcome</th></tr>
    ${j.entries.map(e => `<tr><td class="c2">${esc(e.ts)}</td><td class="tk">${esc(e.ticker)}</td><td>${badge(e.state)}</td><td>${Math.round(e.score || 0)}</td><td class="c2">${esc(e.contract)}</td><td>${money(e.entry_ask)}</td>
    <td>${money(e.trigger_upper)}</td><td>${pct(e.pop)}</td><td>${pct(e.ev, 0, true)}</td><td>${esc(e.time_exit)}</td><td>${esc(e.regime)}</td>
    <td class="${cls(e.outcome_return)}">${e.outcome_status === "CLOSED" ? pct(e.outcome_return, 0, true) : "open"}<div class="c2 mut">${esc(e.outcome_note || "")}</div></td></tr>`).join("")}</table></div>` : `<div class="empty">No recommendations yet. Run a scan.</div>`}</section></div>`;
}

// ------------------------------------------------------------------ SETTINGS
const SET_LABELS = {
  entry_min_score: ["BUY: minimum Entry Score", "BUY trigger rules"], wait_min_score: ["WAIT: minimum score (below = AVOID)"],
  min_pop: ["Minimum probability of profit (0-1)"], min_rr: ["Minimum reward/risk"], min_ev: ["Required EV margin at trigger price (0-1)"],
  max_spread_pct: ["Max bid/ask spread (fraction of mid)"], min_open_interest: ["Minimum open interest"], max_iv_rv: ["Max IV / realised vol"],
  max_atm_iv: ["Max ATM implied vol"], fair_tolerance: ["Max premium above model fair value (0-1)"],
  max_daily_theta_pct: ["Time exit: max daily theta (fraction of premium)", "Exits"], max_loss_rule: ["Max-risk rule: exit if option loses (0-1)"],
  mc_paths: ["Monte Carlo paths per regime", "Simulation"], high_accuracy: ["High-accuracy mode (50,000 paths, slower)"],
  demo_starting_cash: ["Demo starting capital ($)", "Demo / auto paper trading"], auto_demo: ["AI auto demo trading (virtual money only)"],
  auto_risk_pct: ["Auto: max % of demo equity per trade"], auto_max_positions: ["Auto: max open positions"],
  real_cash: ["Real account cash ($, for portfolio value)", "Portfolio"], quote_refresh_sec: ["Quote refresh (seconds)", "Refresh"],
  analysis_refresh_min: ["Full engine re-run (minutes)"], watch_mode: ["Default watchlist mode"], universe: ["Scan universe (comma-separated tickers)", "Universe"],
};
async function viewSettings() {
  renderNav("settings");
  const r = await api("/api/settings"), s = r.settings;
  let html = "", grp = null;
  for (const [k, [label, g]] of Object.entries(SET_LABELS)) {
    if (g && g !== grp) { if (grp) html += "</div></section>"; grp = g; html += `<section class="panel"><h2>${g}</h2><div class="form">`; }
    const v = s[k];
    if (typeof v === "boolean") html += `<label class="chk"><input type="checkbox" data-k="${k}" ${v ? "checked" : ""}> ${label}</label>`;
    else if (k === "watch_mode") html += `<label>${label}<select data-k="${k}">${["FAST", "GROWTH", "LEAPS"].map(m => `<option ${m === v ? "selected" : ""}>${m}</option>`).join("")}</select></label>`;
    else if (k === "universe") html += `<label style="grid-column:1/-1">${label}<textarea data-k="${k}" rows="3">${esc(v)}</textarea></label>`;
    else html += `<label>${label}<input data-k="${k}" type="number" step="any" value="${v}"></label>`;
  }
  html += "</div></section>";
  const pv = r.providers;
  V().innerHTML = `<div class="grid cols2">${html}<section class="panel"><h2>Data providers & storage</h2><div class="kv"><span>Data mode</span><span>${esc(pv.mode)}</span>
    <span>Stocks / options</span><span>${esc(pv.stock)}</span><span>Fundamentals</span><span>${esc(pv.fundamentals)}</span><span>Macro</span><span>${esc(pv.macro)}</span>
    <span>News / 2nd price</span><span>${esc(pv.news)} ${pv.finnhub_key_set ? "✓" : "(no key)"}</span><span>Database</span><span>${esc(r.database)}</span></div>
    <p class="c2 mut">API keys are environment variables on the server and are never sent to this page. Providers are swappable adapters (core/providers.py).</p></section></div>
    <div style="margin-top:12px"><button class="btn green" onclick="saveSettings()">Save settings</button></div>`;
}
async function saveSettings() {
  const b = {};
  document.querySelectorAll("[data-k]").forEach(el => { b[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value; });
  try { await api("/api/settings", { method: "POST", body: b }); toast("Settings saved — new thresholds apply on the next engine run"); } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ REPORT (opportunity or owned position)
async function viewReport(id, kind) {
  renderNav(kind === "position" ? "portfolio" : "scan");
  V().innerHTML = `<div class="empty">Loading report…</div>`;
  let p;
  try { delete S.reports[kind + id]; p = await getReport(id, kind); } catch (e) { V().innerHTML = `<div class="panel"><div class="empty">${esc(e.message)}</div></div>`; return; }
  const pos = p.position, h = pos?.hold;
  const lose = p.contract.max_loss, c = p.sim.chances;
  const posBlock = pos ? `<section class="panel ${["SELL", "EXIT NOW"].includes(pos.action) ? "alert" : ""}"><h2>Your Position — ${esc(p.contract.name)} × ${pos.qty}</h2>
    <div class="hero"><div><div style="margin-bottom:6px">${badge(pos.action, true)}</div><div style="font-size:17px">${esc(pos.reason)}</div>
      ${h?.loss_kind ? `<div class="${h.loss_kind.startsWith("THESIS") ? "neg" : "warn"}" style="font-weight:700;margin-top:4px">${esc(h.loss_kind)}</div>` : ""}</div>
      <div class="kv"><span>Paid</span><span>${money(pos.entry_premium)}</span><span>Now (mid)</span><span>${money(pos.mark)}</span><span>P/L</span><span class="${cls(pos.pnl)}">${smoney(pos.pnl)} (${pct(pos.pnl_pct, 1, true)})</span>
      <span>Days left</span><span>${pos.dte}</span><span>Hold score</span><span>${Math.round(h.hold_score)}</span><span>Exit pressure</span><span class="${h.exit_pressure >= 60 ? "neg" : ""}">${Math.round(h.exit_pressure)}/100</span>
      <span>EV from today</span><span class="${cls(h.ev)}">${pct(h.ev, 0, true)}</span><span>Upside / downside</span><span>${h.rr.toFixed(2)}</span><span>Chance back to cost</span><span>${pct(h.p_recover)}</span></div>
      <div class="verdict" style="min-width:220px"><div class="mut c2">WOULD AI BUY THIS EXACT CONTRACT TODAY?</div><b style="color:var(--${COLOR[pos.would_buy]})">${verdictText(pos.would_buy)}</b>
      ${pos.would_buy === "ONLY AT LOWER PRICE" ? `<div>at ${money(pos.would_buy_price)} or below</div>` : ""}<div class="c2 mut">Independent of what you paid.</div></div></div>
    ${h.exit_reasons.length ? `<div class="c2 mut" style="margin-top:6px">Exit pressure drivers: ${h.exit_reasons.map(esc).join("; ")}</div>` : ""}
    ${pos.account === "demo" ? `<div style="margin-top:8px"><button class="btn red" onclick="paperSell(${pos.id},${pos.qty})">Paper Sell all</button> <button class="btn orange" onclick="paperSell(${pos.id},null,true)">Partial</button></div>` : ""}</section>` : "";
  V().innerHTML = `${p.dq === "LOW" || p.chain_status !== "15-MIN DELAY" && p.chain_status !== "SYNTHETIC" ? `<div class="banner warn">Data: option chain ${esc(p.chain_status)} · quality ${esc(p.dq)}. ${p.chain_status === "CACHED" ? "Quotes are from the last session — confirm prices at the open." : ""}</div>` : ""}
  ${p.flags.some(f => f.message.includes("DEMO")) ? `<div class="banner">DEMO MODE — synthetic data.</div>` : ""}
  <div class="grid">${posBlock}<section class="panel"><div class="hero"><div><div class="tk" style="font-size:30px">${esc(p.ticker)} <span class="mut" style="font-size:18px">${esc(p.name)}</span></div>
    <div style="font-size:20px">${money(p.price)} <span class="${cls(p.change_pct)}">${pct(p.change_pct, 2, true)}</span> ${fr(p.chain_status)} <span class="mut c2">${esc(p.price_time || "")}</span></div>
    <div style="font-size:22px;font-weight:700;margin-top:4px">${esc(p.contract.name)} <span class="mut">· ${esc(p.contract.strategy)} · ${esc(p.contract.moneyness)} · ${p.contract.dte} days</span></div></div>
    <div style="width:170px">${gauge(p.score)}<div class="mut c2" style="text-align:center">Entry score · grade ${esc(p.grade)} · confidence ${p.confidence}</div></div>
    <div style="min-width:230px">${verdictBox(p)}${!pos && p.contract.legs.length === 1 ? `<button class="btn green" style="width:100%;margin-top:8px" onclick="paperBuyRec(${p.result_id})">Paper Buy (demo)</button>` : ""}</div></div></section>
  <section class="panel"><h2>The answer in plain English</h2><div class="qa">
    <div><h3>WHAT?</h3>${esc(p.headline)} — ${esc(p.contract.name)}</div>
    <div><h3>WHY?</h3>${p.why.map(esc).join(" ")}</div>
    <div><h3>AT WHAT PRICE?</h3>Buy between <b>${money(p.trigger.lower)}</b> and <b>${money(p.trigger.upper)}</b> (now ${money(p.contract.ask)}). The ceiling comes from the ${esc(p.trigger.binding)} rule.${p.trigger.breakout ? " Alternative entry: " + esc(p.trigger.breakout.replace(/^or /, "")) + "." : ""}</div>
    <div><h3>HOW MUCH COULD I LOSE?</h3>Up to <b>${money(lose)}</b> per contract (the full premium). Chance of losing most/all: <b>${pct(c.lose_most)}</b> following the plan.</div>
    <div><h3>HOW MUCH COULD I MAKE?</h3>Main target ${money(p.plan.main_price)} (<b class="pos">${pct(p.plan.main_pct, 0, true)}</b>). Chance of +100%: ${pct(c.plus100)}. ${p.contract.max_gain ? `Max gain ${money(p.contract.max_gain)}.` : ""}</div>
    <div><h3>WHAT CHANGES THE AI'S MIND?</h3><ul class="tight">${p.mind_changers.map(m => `<li>${esc(m)}</li>`).join("")}</ul></div></div></section>
  <div class="grid cols3"><section class="panel"><h2>Trade Plan</h2>${planBlock(p)}</section><section class="panel"><h2>If I put $1,000 into this option</h2>${simBlock(p)}</section>
  <section class="panel"><h2>Buy trigger checklist</h2>${p.conditions.map(x => `<div class="cond"><span>${x.passed ? "✅" : "❌"}</span><span>${esc(x.name)}</span><b class="c2">${esc(x.detail)}</b></div>`).join("")}
    <h2 style="margin-top:10px">Option fair value</h2><div class="kv"><span>Market premium (mid)</span><span>${money(p.fair.market)}</span><span>Model fair value</span><span>${money(p.fair.model)}</span>
    <span>Difference</span><span>${pct(p.fair.diff, 0, true)}</span><span>Classification</span><span>${esc(p.fair.label)}</span></div>
    <p class="c2 mut">Model: Black-Scholes + binomial (American) on a realised-volatility forecast. An estimate, not a guarantee.</p></section></div>
  <section class="panel"><h2>Price chart <span class="sp"></span><span class="ranges">${["1M", "3M", "6M", "1Y"].map(r => `<button class="${S.range === r ? "on" : ""}" onclick="S.range='${r}';route()">${r}</button>`).join("")}</span></h2><div style="max-width:900px">${candles(p.chart, S.range)}</div></section>
  <section class="panel"><h2>Engine breakdown</h2><div class="secgrid">${Object.entries(p.sections).map(([k, x]) => `<div class="sec"><h3>${esc(k)}<span style="color:${scoreColor(x.score)}">${ok(x.score) ? Math.round(x.score) : "—"}</span></h3>
    <div class="bar"><i style="width:${x.score || 0}%;background:${scoreColor(x.score)}"></i></div><ul class="tight c2">${x.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></div>`).join("")}</div>
    ${p.penalties.length ? `<p class="c2 warn">Penalties: ${p.penalties.map(([n, v]) => `${esc(n)} −${v}`).join(", ")}</p>` : ""}</section>
  <div class="grid cols2"><section class="panel"><h2>AI Investment Committee</h2><div class="kv">${Object.entries(p.committee.votes).map(([k, v]) => `<span>${esc(k)}</span><span>${badge(v === "BUY" ? "BUY NOW" : v === "WATCH" ? "WAIT" : "AVOID").replace("BUY NOW", "SUPPORT").replace("WAIT", "WATCH").replace(">AVOID<", ">OPPOSE<")}</span>`).join("")}</div>
    <p class="c2">Agents challenge the numbers — the decision is not a majority vote. Bull weight ${p.committee.bull_w.toFixed(1)} vs bear ${p.committee.bear_w.toFixed(1)}: contrarian check <b class="${p.committee.contrarian_pass ? "pos" : "neg"}">${p.committee.contrarian_pass ? "PASSED" : "FAILED"}</b>.</p>
    ${p.committee.vetoes.length ? `<p class="neg c2">Risk manager vetoes: ${p.committee.vetoes.map(esc).join("; ")}</p>` : ""}
    <div class="cols2 grid" style="margin-top:0"><div><b class="pos">For</b><ul class="tight c2">${p.committee.bull.map(x => `<li>${esc(x)}</li>`).join("") || "<li>—</li>"}</ul></div>
    <div><b class="neg">Against (Bear analyst)</b><ul class="tight c2">${p.committee.bear.map(x => `<li>${esc(x)}</li>`).join("") || "<li>—</li>"}</ul></div></div></section>
  <section class="panel"><h2>Contrarian check</h2>${Object.entries(p.committee.contrarian).map(([q, a]) => `<p class="c2"><b>${esc(q)}?</b><br>${esc(a)}</p>`).join("")}</section></div>
  <div class="grid cols2"><section class="panel"><h2>Time path (median outcomes)</h2><table class="tbl"><tr><th>When</th><th>Stock</th><th>Option</th><th>If stock flat</th><th>Delta</th></tr>
    ${p.time_path.map(t => `<tr><td>${esc(t.when)}</td><td>${money(t.stock_median)}</td><td>${money(t.option_median)}</td><td>${money(t.flat_stock_value)}</td><td>${t.delta?.toFixed(2)}</td></tr>`).join("")}</table>
    <p class="c2 mut">"If stock flat" shows pure time decay + IV effects.</p></section>
  <section class="panel"><h2>Stock-move scenarios at time exit ($1,000)</h2><table class="tbl"><tr><th>Stock move</th><th>Stock</th><th>Option</th><th>$1,000 becomes</th></tr>
    ${p.profit_table.map(t => `<tr><td class="${cls(t.move)}">${pct(t.move, 0, true)}</td><td>${money(t.stock)}</td><td>${money(t.option)}</td><td class="${cls(t.ret)}">${money(t.value, 0)} (${pct(t.ret, 0, true)})</td></tr>`).join("")}</table></section></div>
  <div class="grid cols2">${p.alternatives.length ? `<section class="panel"><h2>Why this contract beat nearby alternatives</h2><table class="tbl"><tr><th>Contract</th><th>Type</th><th>Cost</th><th>Prob.</th><th>EV</th><th>Total loss</th></tr>
    ${p.alternatives.map(a => `<tr><td class="c2">${esc(a.name)}</td><td class="c2">${esc(a.moneyness)}</td><td>${money(a.cost, 0)}</td><td>${pct(a.pop)}</td><td class="${cls(a.ev)}">${pct(a.ev, 0, true)}</td><td>${pct(a.tl)}</td></tr>`).join("")}</table>
    <p class="c2 mut">Ranked by probability-adjusted return, not maximum theoretical return.</p></section>` : ""}
  <section class="panel"><h2>Backtest (stock signal)</h2>${p.backtest.n ? `<div class="kv"><span>Similar setups</span><span>${p.backtest.n} — ${esc(p.backtest.confidence)}</span><span>Win rate</span><span>${pct(p.backtest.win_rate)}</span>
    <span>Median / mean</span><span>${pct(p.backtest.median, 1, true)} / ${pct(p.backtest.mean, 1, true)}</span><span>Worst</span><span class="neg">${pct(p.backtest.worst, 1, true)}</span></div>` : `<div class="warn">LOW CONFIDENCE — no comparable setups.</div>`}
    <p class="c2 mut">${esc(p.backtest.note || "")}</p></section></div>
  <section class="panel"><h2>Data quality: ${esc(p.dq)}</h2><table class="tbl"><tr><th>Item</th><th>Source</th><th>Timestamp</th><th>Freshness</th><th>Confidence</th><th>Note</th></tr>
    ${p.sources.map(s => `<tr><td>${esc(s.item)}</td><td>${esc(s.source)}</td><td class="c2">${esc(s.timestamp)}</td><td>${fr(s.freshness)}</td><td>${esc(s.confidence)}</td><td class="c2">${esc(s.note)}</td></tr>`).join("")}</table>
    ${p.flags.length ? `<ul class="tight c2" style="margin-top:6px">${p.flags.map(f => `<li class="${f.severity === "SEVERE" ? "neg" : "mut"}">[${esc(f.severity)}] ${esc(f.message)}</li>`).join("")}</ul>` : ""}</section></div>`;
}

// ------------------------------------------------------------------ router + polling
const go = h => { location.hash = "#/" + h; };
const ROUTES = { dashboard: viewDashboard, scan: viewScan, watchlist: viewWatch, portfolio: viewPortfolio, demo: viewDemo, alerts: viewAlerts,
  market: viewMarket, journal: viewJournal, settings: viewSettings };
async function route(force) {
  const [k, id] = (location.hash.replace(/^#\//, "") || "dashboard").split("/");
  if (force) S.reports = {};
  try {
    if (k === "report" || k === "position") await viewReport(+id, k);
    else await (ROUTES[k] || viewDashboard)();
  } catch (e) { V().innerHTML = `<div class="panel"><div class="empty">Could not load: ${esc(e.message)}</div></div>`; }
  renderStatus();
}
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
setInterval(() => {
  const k = (location.hash.replace(/^#\//, "") || "dashboard").split("/")[0];
  if (!typing() && ["dashboard", "watchlist", "portfolio", "demo", "alerts"].includes(k)) { S.reports = {}; route(); }
  else renderStatus();
}, 45000);
route();
