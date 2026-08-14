"use strict";

// Everything here is a plain fetch()-polling loop against the JSON API in
// dashboard/routes.py — no build step, no framework, no third-party JS.
// Nothing here reads faster than the underlying data actually changes (the
// bot's cron cadence); polling is a UX choice for a "feels live" feed, not
// a freshness guarantee.

const state = {
  profile: "base",
  ordersCursor: null,
  feedMode: "today",   // "today" | "all"
  allOrders: [],        // full window from the API; feed re-renders from this
  seenOrderKeys: new Set(),
  lastChartPayload: null,
  lastTrendsPayload: null,
  lastExposurePayload: null,
  chartView: "equity",  // "equity" | "return" | "pnl" | "drawdown"
  chartDays: 90,
  timers: [],
};

function fmtMoney(value) {
  if (value === null || value === undefined) return "—";
  return "$" + Number(value).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}

function fmtPct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return Number(value).toFixed(digits) + "%";
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch (e) {
    return iso;
  }
}

// Escape DB-sourced text before innerHTML interpolation. Values are
// engine-generated today, but the habit keeps new panels safe by default.
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function clearChildren(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ---- polling ---------------------------------------------------------

// A frozen dashboard must look frozen: track consecutive failures of the
// summary poll (the page's heartbeat) and flip a visible disconnected
// state after a few, instead of silently rendering ever-staler data as
// if it were live. Fetch errors used to be swallowed with no indicator.
const connection = { failures: 0, lastSuccess: null };
const DISCONNECT_AFTER_FAILURES = 3;

function renderConnection() {
  const el = document.getElementById("data-freshness");
  if (!el) return;
  const disconnected = connection.failures >= DISCONNECT_AFTER_FAILURES;
  document.body.classList.toggle("disconnected", disconnected);
  if (disconnected) {
    el.textContent = connection.lastSuccess
      ? `DISCONNECTED · last data ${connection.lastSuccess.toLocaleTimeString()}`
      : "DISCONNECTED";
    el.className = "freshness danger";
  } else if (connection.lastSuccess) {
    el.textContent = `updated ${connection.lastSuccess.toLocaleTimeString()}`;
    el.className = "freshness";
  }
}

function poll(url, intervalMs, onData, opts) {
  const isHeartbeat = opts && opts.heartbeat;
  let stopped = false;
  async function fetchOnce() {
    try {
      const res = await fetch(url());
      if (res.ok) {
        onData(await res.json());
        if (isHeartbeat) {
          connection.failures = 0;
          connection.lastSuccess = new Date();
          renderConnection();
        }
        return;
      }
      throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      // Transient hiccups retry next tick; the heartbeat poller counts
      // them so a dead backend becomes visible instead of a frozen page.
      if (isHeartbeat) {
        connection.failures += 1;
        renderConnection();
      }
    }
  }
  async function tick() {
    if (stopped) return;
    // Only the recurring interval pauses while the tab is hidden (saves a
    // phone's battery/data when the screen is off) — the very first fetch
    // always happens immediately, whether that's initial page load or a
    // tab switch, so the widget never sits on a stale "loading…" waiting
    // for visibility to flip.
    if (document.visibilityState === "visible") {
      await fetchOnce();
    }
    if (!stopped) setTimeout(tick, intervalMs);
  }
  fetchOnce();
  setTimeout(tick, intervalMs);
  return () => { stopped = true; };
}

function stopAllTimers() {
  state.timers.forEach((stop) => stop());
  state.timers = [];
}

// ---- header: status + health + problem strip ---------------------------

function renderStatus(data) {
  const dot = document.getElementById("status-dot");
  const text = document.getElementById("status-text");
  const counts = document.getElementById("status-counts");
  const health = data.health;
  const attention = (data.attention && data.attention.signals) || [];
  const hasDanger = attention.some((s) => s.severity === "danger");

  let cls = "ok";
  let label = `${data.mode} · equity ${fmtMoney(data.equity)}`;
  if (data.gross_leverage && Number(data.gross_leverage) !== 1) {
    label += ` · ${Number(data.gross_leverage).toFixed(1)}x`;
  }
  if (data.halted || data.mode === "halt") {
    cls = "danger";
    label = "HALTED · " + label;
  } else if (!health) {
    cls = "";
    label += " · waiting for healthcheck";
  } else if (health.healthy === false) {
    cls = "danger";
    const n = (health.problems || []).length;
    label = `${n} health problem${n === 1 ? "" : "s"} · ` + label;
  } else if (hasDanger) {
    // A stale health file or stuck order must not hide behind a green
    // HEALTHY verdict that may itself be the problem.
    cls = "danger";
    label += ` · ${attention.length} attention signal${attention.length === 1 ? "" : "s"}`;
  } else if (attention.length) {
    cls = "warn";
    label += ` · HEALTHY ${fmtTime(health.ts)}`;
  } else {
    label += ` · HEALTHY ${fmtTime(health.ts)}`;
  }
  dot.className = "dot " + cls;
  text.textContent = label + (data.last_run_ts ? ` · last run ${fmtTime(data.last_run_ts)}` : "");
  if (health) {
    // health.equity is a string on disk; positions/open_orders are counts.
    counts.textContent = `· ${health.positions ?? "—"} pos · ${health.open_orders ?? "—"} open ord`;
  } else {
    counts.textContent = "";
  }

  renderProblemStrip(health, state.reconProblems || [], attention);
}

function renderProblemStrip(health, reconEvents, attention) {
  const strip = document.getElementById("problem-strip");
  const list = document.getElementById("problem-list");
  const problems = [];  // {text, severity}
  if (health && health.healthy === false) {
    (health.problems || []).forEach((p) => problems.push({ text: String(p), severity: "danger" }));
    if (!(health.problems || []).length) {
      problems.push({ text: `unhealthy, no detail (as of ${fmtTime(health.ts)})`, severity: "danger" });
    }
  }
  (attention || []).forEach((s) => problems.push({ text: s.message, severity: s.severity }));
  reconEvents.forEach((e) => {
    const critical = String(e.severity).toUpperCase() === "CRITICAL";
    problems.push({
      text: `options reconciliation${critical ? "" : ` (${e.severity})`}: ${e.detail}`,
      severity: critical ? "danger" : "warn",
    });
  });
  clearChildren(list);
  if (!problems.length) {
    strip.hidden = true;
    return;
  }
  problems.sort((a, b) => (a.severity === "danger" ? 0 : 1) - (b.severity === "danger" ? 0 : 1));
  problems.forEach((p) => {
    const li = document.createElement("li");
    li.className = p.severity === "warn" ? "warn" : "";
    li.textContent = p.text;
    list.appendChild(li);
  });
  strip.hidden = false;
}

// ---- small cards ---------------------------------------------------------

function budgetRow(label, usedPct, limitPct) {
  const row = document.createElement("div");
  row.className = "budget-row";
  const pct = usedPct === null || usedPct === undefined ? 0 : usedPct;
  const fillClass = pct >= 100 ? "danger" : pct >= 70 ? "warn" : "";
  row.innerHTML = `
    <div class="label"><span>${esc(label)}</span><span>${fmtPct(usedPct)} of ${fmtPct(limitPct)} budget</span></div>
    <div class="budget-track"><div class="budget-fill ${fillClass}" style="width:${Math.min(pct, 100)}%"></div></div>
  `;
  return row;
}

function renderBudget(budget) {
  const el = document.getElementById("budget-bars");
  clearChildren(el);
  if (!budget) {
    el.innerHTML = '<span class="muted">no equity data yet</span>';
    return;
  }
  el.appendChild(budgetRow("Daily loss limit", budget.daily_used_pct, budget.daily_loss_limit_pct));
  el.appendChild(budgetRow("Monthly kill switch", budget.monthly_used_pct, budget.monthly_kill_switch_pct));
  el.appendChild(budgetRow("Peak drawdown halt", budget.peak_used_pct, budget.peak_drawdown_halt_pct));
}

function renderOverlay(data) {
  const el = document.getElementById("overlay-card");
  const progressEl = document.getElementById("overlay-progress");
  const overlay = data.volatility_overlay;
  const rec = overlay.latest_recommendation;
  const rows = [["configured mode", overlay.configured_mode]];
  if (rec) {
    rows.push(
      ["target vol", rec.target_vol !== null ? fmtPct(rec.target_vol * 100) : "—"],
      ["realized vol", rec.realized_vol !== null ? fmtPct(rec.realized_vol * 100) : "—"],
      ["recommended", rec.recommended_leverage !== null ? rec.recommended_leverage.toFixed(2) + "x" : "—"],
      ["ready", rec.ready ? "yes" : "no"],
    );
    if (rec.reason) rows.push(["reason", rec.reason]);
  } else {
    rows.push(["recommendation", "no data yet"]);
  }
  el.innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

  clearChildren(progressEl);
  const minObs = overlay.min_observations;
  if (rec && minObs) {
    const pct = Math.min(100, (rec.observations / minObs) * 100);
    progressEl.innerHTML = `
      <div class="label"><span>observations</span><span>${esc(rec.observations)} / ${esc(minObs)}</span></div>
      <div class="budget-track"><div class="budget-fill" style="width:${pct}%"></div></div>
    `;
  }
}

function renderCooldown(list) {
  const el = document.getElementById("cooldown-list");
  clearChildren(el);
  if (!list.length) {
    el.innerHTML = '<li class="muted">none</li>';
    return;
  }
  const shown = list.slice(0, 5);
  shown.forEach((r) => {
    const li = document.createElement("li");
    li.textContent = `${r.symbol} — ${r.days_remaining}d remaining (exited ${r.exit_date})`;
    el.appendChild(li);
  });
  if (list.length > shown.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = `+${list.length - shown.length} more`;
    el.appendChild(li);
  }
}

// ---- experiments & options ----------------------------------------------

function renderExperiments(experiments) {
  const el = document.getElementById("experiments-status");
  clearChildren(el);
  if (state.profile === "base") {
    el.innerHTML = '<span class="muted">base is the control — no experiments</span>';
    return;
  }
  const standdowns = (experiments && experiments.standdowns) || [];
  const pnl = (experiments && experiments.realized_pnl) || {};
  const misses = (experiments && experiments.buying_power_misses) || {};
  const names = new Set([...standdowns, ...Object.keys(pnl), ...Object.keys(misses)]);
  if (!names.size) {
    el.innerHTML = '<span class="muted">no experiment P&amp;L recorded yet</span>';
    return;
  }
  [...names].sort().forEach((name) => {
    const badge = document.createElement("span");
    const stood = standdowns.includes(name);
    const missStreak = Number(misses[name] || 0);
    badge.className = "badge " + (stood ? "danger" : missStreak > 0 ? "warn" : "ok");
    const value = pnl[name];
    badge.textContent = `${name}: ${stood ? "STOOD DOWN" : "active"}` +
      (value !== undefined ? ` · realized ${fmtMoney(value)}` : "") +
      (missStreak > 0 ? ` · ${missStreak}d short of buying power` : "");
    el.appendChild(badge);
  });
}

function renderOptions(data) {
  const tbody = document.querySelector("#options-table tbody");
  clearChildren(tbody);
  // Keep every reconciliation event — the strip styles CRITICAL as danger
  // and everything else as warn; previously non-CRITICAL events were
  // silently discarded here and displayed nowhere.
  state.reconProblems = data.reconciliation_events || [];
  const structures = data.structures || [];
  if (!structures.length) {
    const msg = state.profile === "base"
      ? "base has no options capability"
      : "no options structures yet";
    tbody.innerHTML = `<tr><td colspan="9" class="muted">${msg}</td></tr>`;
    return;
  }
  structures.forEach((s) => {
    const tr = document.createElement("tr");
    const legs = (s.legs || [])
      .map((l) => `${l.position_intent === "sell_to_open" ? "-" : "+"}${l.symbol}`)
      .join(" ");
    tr.innerHTML = `
      <td>${fmtTime(s.opened_ts)}</td>
      <td>${esc(s.strategy)}</td>
      <td>${esc(legs) || "—"}</td>
      <td>${esc(s.expiration_date)}</td>
      <td>${esc(s.contracts)}</td>
      <td>${fmtMoney(s.credit)}</td>
      <td>${fmtMoney(s.maximum_loss)}</td>
      <td>${esc(s.status)}</td>
      <td>${s.realized_pnl !== null && s.realized_pnl !== undefined ? fmtMoney(s.realized_pnl) : "—"}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ---- positions -----------------------------------------------------------

function renderPositions(positions) {
  const tbody = document.querySelector("#positions-table tbody");
  clearChildren(tbody);
  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="muted">no open positions</td></tr>';
    return;
  }
  positions.forEach((p) => {
    const tr = document.createElement("tr");
    const pnl = p.cost_basis_available
      ? fmtMoney(p.unrealized_pl)
      : (p.stop_exempt_sleeve ? "n/a (stop-exempt)" : "n/a");
    let toStop = "—";
    let toStopClass = "";
    if (p.stop_price !== null && p.price > 0) {
      const dist = (p.price - p.stop_price) / p.price;
      // Shorts have stops above price (negative distance as computed) —
      // magnitude is what matters for "how close am I".
      const distPct = Math.abs(dist) * 100;
      toStop = fmtPct(distPct);
      if (distPct < 2) toStopClass = "near-stop";
    }
    const stopCell = p.stop_price !== null
      ? fmtMoney(p.stop_price)
      : (p.stop_exempt_sleeve ? "exempt" : '<span class="badge warn">unprotected</span>');
    tr.innerHTML = `
      <td>${esc(p.symbol)}</td>
      <td>${esc(p.sleeve) || "—"}</td>
      <td>${esc(p.qty)}</td>
      <td>${fmtMoney(p.price)}</td>
      <td>${fmtMoney(p.market_value)}</td>
      <td>${esc(p.entry_date) || "—"}</td>
      <td>${stopCell}</td>
      <td class="${toStopClass}">${toStop}</td>
      <td>${pnl}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ---- execution quality ---------------------------------------------------

function executionRow(label, row) {
  const tr = document.createElement("tr");
  const slip = row.adverse_slippage_bps;
  // Status histogram, e.g. "filled 12 · new 2" — a nonzero 'new' count is
  // exactly where a stuck order shows up, so it gets the danger tint.
  const statuses = Object.entries(row.statuses || {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([status, n]) => {
      const cls = status === "new" && n > 0 ? ' class="near-stop"' : "";
      return `<span${cls}>${esc(status)} ${esc(n)}</span>`;
    })
    .join(" · ") || "—";
  tr.innerHTML = `
    <td>${esc(label)}</td>
    <td>${esc(row.orders)}</td>
    <td>${statuses}</td>
    <td>${row.fill_pct !== null && row.fill_pct !== undefined ? fmtPct(row.fill_pct) : "—"}</td>
    <td>${row.approval_pct !== null && row.approval_pct !== undefined ? fmtPct(row.approval_pct) : "—"}</td>
    <td>${fmtMoney(row.requested_notional)}</td>
    <td>${fmtMoney(row.approved_notional)}</td>
    <td>${slip !== null && slip !== undefined ? Number(slip).toFixed(2) + " bp" : "pending"}</td>
  `;
  return tr;
}

function renderExecution(execution) {
  const tbody = document.querySelector("#execution-table tbody");
  clearChildren(tbody);
  if (!execution || !execution.overall || !execution.overall.orders) {
    tbody.innerHTML = '<tr><td colspan="8" class="muted">no fills yet</td></tr>';
    return;
  }
  tbody.appendChild(executionRow("(overall)", execution.overall));
  Object.entries(execution.by_sleeve || {}).forEach(([sleeve, row]) => {
    tbody.appendChild(executionRow(sleeve, row));
  });
}

// ---- exposure ------------------------------------------------------------

function renderExposure(payload) {
  state.lastExposurePayload = payload;
  const exposure = payload && payload.latest_exposure;
  renderExposureHistory((payload && payload.history) || []);
  const tbody = document.querySelector("#exposure-table tbody");
  const drift = document.getElementById("drift-list");
  clearChildren(tbody);
  clearChildren(drift);
  if (!exposure) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted">no exposure snapshot yet</td></tr>';
    return;
  }
  const bySleeve = exposure.by_sleeve || {};
  const targets = exposure.target_by_sleeve || {};
  const sleeves = [...new Set([...Object.keys(bySleeve), ...Object.keys(targets)])];
  if (!sleeves.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted">no sleeve breakdown yet</td></tr>';
    return;
  }
  const triple = (row) => row
    ? `${fmtPct(row.long * 100)} / ${fmtPct(row.short * 100)} / ${fmtPct(row.gross * 100)}`
    : "—";
  sleeves.forEach((sleeve) => {
    const actual = bySleeve[sleeve];
    const target = targets[sleeve];
    let gapText = "—";
    if (actual && target) {
      const gap = (actual.gross - target.gross) * 100;
      gapText = (gap >= 0 ? "+" : "") + gap.toFixed(1) + "pp";
    }
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(sleeve)}</td>
      <td>${triple(target)}</td>
      <td>${triple(actual)}</td>
      <td>${esc(gapText)}</td>
      <td>${actual ? fmtMoney(actual.unrealized_pl) : "—"}</td>
    `;
    tbody.appendChild(tr);
  });
  const gaps = exposure.largest_symbol_gaps || [];
  if (gaps.length) {
    const parts = gaps.slice(0, 6).map((g) => {
      const symbol = g.symbol ?? g[0];
      // engine/attribution.py emits "weight_gap"; "gap"/[1] kept as
      // fallbacks for old journal rows.
      const value = g.weight_gap ?? g.gap ?? g[1];
      return `${symbol} ${(Number(value) * 100).toFixed(1)}pp`;
    });
    drift.textContent = "biggest symbol drift: " + parts.join(" · ");
  }
}

function renderExposureHistory(history) {
  const canvas = document.getElementById("exposure-history-chart");
  drawChart(canvas, {
    labels: history.map((h) => h.date),
    series: [
      { values: history.map((h) => h.target_gross), color: cssVar("--cash"), width: 1 },
      { values: history.map((h) => h.actual_gross), color: cssVar("--accent"), width: 2 },
    ],
    yFormat: (v) => v.toFixed(2) + "x",
    emptyText: "no exposure history yet",
  });
}

// ---- execution trends ----------------------------------------------------

function renderTrends(data) {
  state.lastTrendsPayload = data;
  const days = data.days || [];
  drawChart(document.getElementById("trends-chart"), {
    labels: days.map((d) => d.date),
    series: [{
      values: days.map((d) => d.adverse_slippage_bps), type: "bar",
      color: cssVar("--warn"), negativeColor: cssVar("--ok"),
    }],
    yFormat: (v) => v.toFixed(1) + "bp",
    emptyText: "no execution history yet",
  });

  const tbody = document.querySelector("#trends-table tbody");
  clearChildren(tbody);
  if (!days.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">no orders or rejections in range</td></tr>';
    return;
  }
  [...days].reverse().slice(0, 15).forEach((d) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(d.date)}</td>
      <td>${esc(d.orders)}</td>
      <td>${d.fill_pct !== null && d.fill_pct !== undefined ? fmtPct(d.fill_pct) : "—"}</td>
      <td>${d.adverse_slippage_bps !== null && d.adverse_slippage_bps !== undefined
        ? d.adverse_slippage_bps.toFixed(1) + " bp" : "—"}</td>
      <td>${d.avg_latency_s !== null && d.avg_latency_s !== undefined
        ? d.avg_latency_s.toFixed(0) + "s" : "—"}</td>
      <td>${esc(d.rejections)}</td>
      <td>${fmtMoney(d.blocked_notional)}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ---- realized P&L (round trips) ------------------------------------------

function renderRoundTrips(data) {
  const summary = document.getElementById("roundtrips-summary");
  const bySleeve = data.by_sleeve || {};
  const parts = Object.entries(bySleeve).map(([sleeve, agg]) =>
    `${sleeve}: ${fmtMoney(agg.realized_pnl)} over ${agg.trips} trips (${agg.wins}W/${agg.losses}L)`);
  summary.textContent = parts.length
    ? parts.join(" · ") + ` · ${esc(data.coverage_note || "")}` +
      (data.unmatched ? ` · ${data.unmatched} exit(s) unmatched (predate fill recording)` : "")
    : "";

  const tbody = document.querySelector("#roundtrips-table tbody");
  clearChildren(tbody);
  const trips = data.trips || [];
  if (!trips.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">no closed round trips yet (${esc(data.coverage_note || "")})</td></tr>`;
    return;
  }
  trips.slice(0, 40).forEach((t) => {
    const tr = document.createElement("tr");
    const pnlClass = t.realized_pnl >= 0 ? "side-buy" : "side-sell";
    tr.innerHTML = `
      <td>${esc(t.symbol)}</td>
      <td>${esc(t.sleeve) || "—"}</td>
      <td>${fmtTime(t.entry_ts)}</td>
      <td>${fmtTime(t.exit_ts)}</td>
      <td>${esc(t.qty)}</td>
      <td>${fmtMoney(t.entry_price)}</td>
      <td>${fmtMoney(t.exit_price)}</td>
      <td class="${pnlClass}">${fmtMoney(t.realized_pnl)}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ---- trade feed ----------------------------------------------------------

function orderKey(o) {
  return `${o.ts}|${o.symbol}|${o.side}`;
}

function slippageBps(o) {
  if (!o.filled_avg_price || !o.reference_price) return null;
  const raw = (o.filled_avg_price - o.reference_price) / o.reference_price;
  const direction = (o.side === "buy" || o.side === "cover") ? 1 : -1;
  return raw * direction * 10000;
}

function fillLatency(o) {
  if (!o.filled_at || !o.ts) return null;
  const ms = new Date(o.filled_at) - new Date(o.ts);
  if (!isFinite(ms) || ms < 0) return null;
  return ms / 1000;
}

function isToday(iso) {
  if (!iso) return false;
  const d = new Date(iso);
  const now = new Date();
  return d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
}

function orderRow(o, isNew) {
  const tr = document.createElement("tr");
  tr.className = "feed-row" + (isNew ? " row-new" : "");
  const slip = slippageBps(o);
  const latency = fillLatency(o);
  tr.innerHTML = `
    <td>${fmtTime(o.ts)}</td>
    <td>${esc(o.symbol)}</td>
    <td class="side-${esc(o.side)}">${esc(o.side)}</td>
    <td>${esc(o.sleeve) || "—"}</td>
    <td>${esc(o.qty)}</td>
    <td>${fmtMoney(o.notional)}</td>
    <td>${o.filled_avg_price ? fmtMoney(o.filled_avg_price) : "—"}</td>
    <td>${slip !== null ? slip.toFixed(1) + " bp" : "—"}</td>
    <td>${latency !== null ? latency.toFixed(0) + "s" : "—"}</td>
    <td>${esc(o.status) || "—"}</td>
    <td>${esc(o.reason) || "—"}</td>
  `;
  // Click-to-expand detail row for the columns the API returns but the
  // table doesn't show — kept out of the header row to hold 11 columns.
  tr.addEventListener("click", () => {
    const existing = tr.nextElementSibling;
    if (existing && existing.classList.contains("feed-detail")) {
      existing.remove();
      return;
    }
    const detail = document.createElement("tr");
    detail.className = "feed-detail";
    detail.innerHTML = `<td colspan="11" class="muted">
      limit ${fmtMoney(o.limit_price)} · filled qty ${esc(o.filled_qty ?? "—")} ·
      requested ${fmtMoney(o.requested_notional)} · ref px ${fmtMoney(o.reference_price)} ·
      alpaca id ${esc(o.alpaca_id) || "—"}
    </td>`;
    tr.after(detail);
  });
  return tr;
}

function renderFeed(newKeys) {
  const tbody = document.querySelector("#orders-table tbody");
  clearChildren(tbody);
  const visible = state.feedMode === "today"
    ? state.allOrders.filter((o) => isToday(o.ts))
    : state.allOrders;
  if (!visible.length) {
    const msg = state.feedMode === "today" ? "no trades yet today" : "no orders in range";
    tbody.innerHTML = `<tr><td colspan="11" class="muted">${msg}</td></tr>`;
    return;
  }
  // newest first, capped so the DOM doesn't grow without bound
  [...visible].reverse().slice(0, 300).forEach((o) => {
    tbody.appendChild(orderRow(o, newKeys ? newKeys.has(orderKey(o)) : false));
  });
}

function ingestOrders(data) {
  const incoming = data.orders || [];
  const newKeys = new Set();
  const firstLoad = state.ordersCursor === null;
  incoming.forEach((o) => {
    const key = orderKey(o);
    if (!state.seenOrderKeys.has(key)) {
      state.seenOrderKeys.add(key);
      state.allOrders.push(o);
      if (!firstLoad) newKeys.add(key);
    }
  });
  state.allOrders.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
  if (state.allOrders.length > 500) state.allOrders = state.allOrders.slice(-500);
  if (data.latest_ts) state.ordersCursor = data.latest_ts;
  renderFeed(newKeys);
}

// ---- rejections ----------------------------------------------------------

function renderRejections(data) {
  const stats = document.getElementById("rejections-stats");
  stats.textContent =
    `${data.count ?? 0} rejections · ${fmtMoney(data.requested_notional)} blocked · ` +
    `${data.whole_share_rounding ?? 0} whole-share rounding · ${data.hard_to_borrow ?? 0} hard-to-borrow`;

  const tbody = document.querySelector("#rejections-table tbody");
  clearChildren(tbody);
  const reasons = data.top_reasons || [];
  if (!reasons.length) {
    tbody.innerHTML = '<tr><td colspan="2" class="muted">no rejections in range</td></tr>';
  } else {
    reasons.forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(r.reason)}</td><td>${esc(r.count)}</td>`;
      tbody.appendChild(tr);
    });
  }

  const sleeveBody = document.querySelector("#rejections-sleeve-table tbody");
  clearChildren(sleeveBody);
  const bySleeve = data.by_sleeve_side || [];
  if (!bySleeve.length) {
    sleeveBody.innerHTML = '<tr><td colspan="4" class="muted">—</td></tr>';
  } else {
    bySleeve.forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${esc(r.sleeve)}</td><td>${esc(r.side) || "—"}</td>
        <td>${esc(r.count)}</td><td>${fmtMoney(r.blocked_notional)}</td>`;
      sleeveBody.appendChild(tr);
    });
  }
}

// ---- charts (hand-rolled canvas, no dependency) -------------------------

function chartGeometry(canvas) {
  const cssWidth = canvas.clientWidth || 900;
  const fallback = canvas.classList.contains("mini-chart") ? 90 : 170;
  const cssHeight = canvas.clientHeight || fallback;
  return { cssWidth, cssHeight, pad: { top: 12, right: 12, bottom: 24, left: 64 } };
}

// Generic single-axis chart: line and bar series share one y-scale.
// config: {labels, series: [{values, color, width, type: "line"|"bar"}],
//          refs: [{value, color}], yFormat, emptyText}
function drawChart(canvas, config) {
  const ctx = canvas.getContext("2d");
  const { cssWidth, cssHeight, pad } = chartGeometry(canvas);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const labels = config.labels || [];
  const plotW = cssWidth - pad.left - pad.right;
  const plotH = cssHeight - pad.top - pad.bottom;
  const colorMuted = cssVar("--fg-muted");
  const colorGrid = cssVar("--border");

  if (labels.length < 2) {
    ctx.fillStyle = colorMuted;
    ctx.font = "13px sans-serif";
    ctx.fillText(config.emptyText || "not enough history yet", pad.left, pad.top + 20);
    return null;
  }

  const values = [];
  config.series.forEach((s) => s.values.forEach((v) => {
    if (v !== null && v !== undefined) values.push(v);
  }));
  (config.refs || []).forEach((r) => {
    if (r.value !== null && r.value !== undefined) values.push(r.value);
  });
  const hasBars = config.series.some((s) => s.type === "bar");
  if (hasBars) values.push(0);  // bars need a zero baseline in view
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08;
  max += span * 0.08;

  const x = (i) => pad.left + (i / (labels.length - 1)) * plotW;
  const y = (v) => pad.top + plotH - ((v - min) / (max - min)) * plotH;
  const yFormat = config.yFormat || ((v) => "$" + Math.round(v).toLocaleString());

  ctx.strokeStyle = colorGrid;
  ctx.fillStyle = colorMuted;
  ctx.font = "11px sans-serif";
  ctx.lineWidth = 1;
  const ySteps = 4;
  for (let i = 0; i <= ySteps; i++) {
    const v = min + (max - min) * (i / ySteps);
    const yy = y(v);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(cssWidth - pad.right, yy);
    ctx.stroke();
    ctx.fillText(yFormat(v), 4, yy + 4);
  }

  (config.refs || []).forEach((r) => {
    if (r.value === null || r.value === undefined) return;
    ctx.save();
    ctx.strokeStyle = r.color;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, y(r.value));
    ctx.lineTo(cssWidth - pad.right, y(r.value));
    ctx.stroke();
    ctx.restore();
  });

  config.series.forEach((s) => {
    if (s.type === "bar") {
      const barW = Math.max(2, (plotW / labels.length) * 0.6);
      const y0 = y(0);
      s.values.forEach((v, i) => {
        if (v === null || v === undefined) return;
        ctx.fillStyle = s.negativeColor && v < 0 ? s.negativeColor : s.color;
        const py = y(v);
        ctx.fillRect(x(i) - barW / 2, Math.min(py, y0), barW, Math.abs(py - y0) || 1);
      });
      return;
    }
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width || 1;
    ctx.beginPath();
    let started = false;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) return;
      if (!started) { ctx.moveTo(x(i), y(v)); started = true; } else ctx.lineTo(x(i), y(v));
    });
    ctx.stroke();
  });

  ctx.fillStyle = colorMuted;
  ctx.fillText(labels[0], pad.left, cssHeight - 6);
  const lastLabel = labels[labels.length - 1];
  ctx.fillText(lastLabel, cssWidth - pad.right - ctx.measureText(lastLabel).width, cssHeight - 6);
  return { x, y, pad, cssWidth, cssHeight, plotW };
}

// The equity chart's four views over the same payload. Legend text and
// y-format switch with the view; the tooltip always shows everything.
const CHART_VIEWS = {
  equity: {
    legend: '<span><i class="swatch equity"></i>equity</span><span><i class="swatch cash"></i>cash</span>'
      + '<span><i class="swatch halt"></i>peak-drawdown halt</span><span><i class="swatch kill"></i>monthly kill switch</span>',
    config: (points, refs) => ({
      series: [
        { values: points.map((p) => p.cash), color: cssVar("--cash"), width: 1 },
        { values: points.map((p) => p.equity), color: cssVar("--accent"), width: 2 },
      ],
      refs: [
        { value: refs.peak_drawdown_halt, color: cssVar("--danger") },
        { value: refs.monthly_kill_switch, color: cssVar("--warn") },
      ],
    }),
  },
  return: {
    legend: '<span><i class="swatch equity"></i>cumulative return over window</span>',
    config: (points) => ({
      series: [{ values: points.map((p) => p.return_pct), color: cssVar("--accent"), width: 2 }],
      refs: [{ value: 0, color: cssVar("--border") }],
      yFormat: (v) => v.toFixed(1) + "%",
    }),
  },
  pnl: {
    legend: '<span><i class="swatch equity"></i>daily P&amp;L</span>',
    config: (points) => ({
      series: [{
        values: points.map((p) => p.pnl), type: "bar",
        color: cssVar("--ok"), negativeColor: cssVar("--danger"),
      }],
      yFormat: (v) => "$" + Math.round(v).toLocaleString(),
    }),
  },
  drawdown: {
    legend: '<span><i class="swatch halt"></i>drawdown from all-time peak</span>',
    config: (points) => ({
      series: [{ values: points.map((p) => p.drawdown_pct), color: cssVar("--danger"), width: 2 }],
      refs: [{ value: 0, color: cssVar("--border") }],
      yFormat: (v) => v.toFixed(1) + "%",
    }),
  },
};

function drawEquityChart(payload) {
  state.lastChartPayload = payload;
  const canvas = document.getElementById("equity-chart");
  const points = payload.points || [];
  const view = CHART_VIEWS[state.chartView] || CHART_VIEWS.equity;
  const config = view.config(points, payload.reference_lines || {});
  config.labels = points.map((p) => p.date);
  drawChart(canvas, config);
  document.getElementById("equity-legend").innerHTML = view.legend;

  // Day-over-day equity delta in the panel title.
  const deltaEl = document.getElementById("equity-delta");
  const last = points[points.length - 1];
  if (last && last.pnl !== null && last.pnl !== undefined) {
    const up = last.pnl >= 0;
    deltaEl.textContent = `${up ? "+" : ""}${fmtMoney(last.pnl)} today`;
    deltaEl.className = "equity-delta " + (up ? "pos" : "neg");
  } else {
    deltaEl.textContent = "";
  }
}

// Crosshair + tooltip: nearest point by x, redrawn over the base chart.
function setupChartHover() {
  const canvas = document.getElementById("equity-chart");
  const tooltip = document.getElementById("chart-tooltip");

  function nearestIndex(offsetX, points, pad, plotW) {
    const t = (offsetX - pad.left) / plotW;
    return Math.max(0, Math.min(points.length - 1, Math.round(t * (points.length - 1))));
  }

  canvas.addEventListener("mousemove", (ev) => {
    const payload = state.lastChartPayload;
    const points = payload && payload.points;
    if (!points || points.length < 2) return;
    const { cssWidth, cssHeight, pad } = chartGeometry(canvas);
    const plotW = cssWidth - pad.left - pad.right;
    const i = nearestIndex(ev.offsetX, points, pad, plotW);
    const p = points[i];

    drawEquityChart(payload);   // clean base frame
    const ctx = canvas.getContext("2d");
    const px = pad.left + (i / (points.length - 1)) * plotW;
    ctx.save();
    ctx.strokeStyle = cssVar("--fg-muted");
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(px, pad.top);
    ctx.lineTo(px, cssHeight - pad.bottom);
    ctx.stroke();
    ctx.restore();

    // Every derived metric regardless of active view — the tooltip is
    // where the views converge.
    tooltip.innerHTML =
      `<span class="tt-date">${esc(p.date)}</span><br>` +
      `equity ${fmtMoney(p.equity)}` +
      (p.cash !== null && p.cash !== undefined ? `<br>cash ${fmtMoney(p.cash)}` : "") +
      (p.pnl !== null && p.pnl !== undefined ? `<br>day P&amp;L ${fmtMoney(p.pnl)}` : "") +
      (p.return_pct !== null && p.return_pct !== undefined ? `<br>window ${fmtPct(p.return_pct)}` : "") +
      (p.drawdown_pct ? `<br>drawdown ${fmtPct(p.drawdown_pct)}` : "");
    tooltip.hidden = false;
    const wrap = canvas.parentElement.getBoundingClientRect();
    const ttWidth = tooltip.offsetWidth || 120;
    const left = Math.min(ev.clientX - wrap.left + 14, wrap.width - ttWidth - 4);
    tooltip.style.left = Math.max(0, left) + "px";
    tooltip.style.top = "8px";
  });

  canvas.addEventListener("mouseleave", () => {
    tooltip.hidden = true;
    if (state.lastChartPayload) drawEquityChart(state.lastChartPayload);
  });
}

// ---- wiring --------------------------------------------------------

function apiUrl(path) {
  return `/api/${state.profile}${path}`;
}

function startPollers() {
  stopAllTimers();
  state.ordersCursor = null;
  state.allOrders = [];
  state.seenOrderKeys = new Set();
  state.reconProblems = [];

  state.timers.push(poll(() => apiUrl("/summary"), 10000, (data) => {
    renderStatus(data);
    renderBudget(data.risk_budget);
    renderOverlay(data);
    renderCooldown(data.reentry_cooldown || []);
    renderExperiments(data.experiments);
    renderExecution(data.execution);
  }, { heartbeat: true }));

  state.timers.push(poll(() => apiUrl(`/orders?limit=200`), 6000, ingestOrders));

  state.timers.push(poll(() => apiUrl("/positions"), 25000, (data) => {
    renderPositions(data.positions || []);
  }));

  state.timers.push(poll(() => apiUrl(`/equity-curve?days=${state.chartDays}`), 60000, drawEquityChart));
  state.timers.push(poll(() => apiUrl("/exposure"), 60000, renderExposure));
  state.timers.push(poll(() => apiUrl("/rejections?days=7"), 60000, renderRejections));
  state.timers.push(poll(() => apiUrl("/trends?days=30"), 60000, renderTrends));
  state.timers.push(poll(() => apiUrl("/round-trips?limit=100"), 60000, renderRoundTrips));

  if (state.profile !== "base") {
    state.timers.push(poll(() => apiUrl("/options"), 30000, renderOptions));
  } else {
    renderOptions({ structures: [], reconciliation_events: [] });
  }
}

function selectProfile(profile) {
  state.profile = profile;
  document.querySelectorAll(".tab").forEach((btn) => {
    const active = btn.dataset.profile === profile;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  });
  startPollers();
}

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => selectProfile(btn.dataset.profile));
});

// Each toggle group manages its own active state — scoped by data
// attribute, NOT all .toggle-btn globally (the chart controls reuse the
// same class).
document.querySelectorAll(".toggle-btn[data-feed]").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.feedMode = btn.dataset.feed;
    document.querySelectorAll(".toggle-btn[data-feed]").forEach((b) => {
      b.classList.toggle("active", b === btn);
    });
    renderFeed();
  });
});

document.querySelectorAll(".toggle-btn[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.chartView = btn.dataset.view;
    document.querySelectorAll(".toggle-btn[data-view]").forEach((b) => {
      b.classList.toggle("active", b === btn);
    });
    if (state.lastChartPayload) drawEquityChart(state.lastChartPayload);
  });
});

document.querySelectorAll(".toggle-btn[data-days]").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.chartDays = parseInt(btn.dataset.days, 10) || 90;
    document.querySelectorAll(".toggle-btn[data-days]").forEach((b) => {
      b.classList.toggle("active", b === btn);
    });
    // Refetch at the new window immediately rather than waiting out the
    // 60s poll interval.
    fetch(apiUrl(`/equity-curve?days=${state.chartDays}`))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => { if (data) drawEquityChart(data); })
      .catch(() => {});
  });
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (state.lastChartPayload) drawEquityChart(state.lastChartPayload);
    if (state.lastTrendsPayload) renderTrends(state.lastTrendsPayload);
    if (state.lastExposurePayload) renderExposure(state.lastExposurePayload);
  }, 150);
});

setupChartHover();
selectProfile("base");
