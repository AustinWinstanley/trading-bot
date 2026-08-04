"use strict";

// Everything here is a plain fetch()-polling loop against the JSON API in
// dashboard/routes.py — no build step, no framework, no third-party JS.
// Nothing here reads faster than the underlying data actually changes (the
// bot's cron cadence); polling is a UX choice for a "feels live" feed, not
// a freshness guarantee.

const state = {
  profile: "base",
  ordersCursor: null,
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

function clearChildren(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

// ---- polling ---------------------------------------------------------

function poll(url, intervalMs, onData) {
  let stopped = false;
  async function fetchOnce() {
    try {
      const res = await fetch(url());
      if (res.ok) onData(await res.json());
    } catch (e) {
      // Transient network hiccup — next tick retries. No point spamming
      // the console for something the user can't act on.
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

// ---- renderers ---------------------------------------------------------

function renderStatus(data) {
  const dot = document.getElementById("status-dot");
  const text = document.getElementById("status-text");
  let cls = "ok";
  let label = `${data.mode} · equity ${fmtMoney(data.equity)}`;
  if (data.halted) {
    cls = "danger";
    label = "HALTED · " + label;
  } else if (data.health && data.health.healthy === false) {
    cls = "warn";
    label = "health check failing · " + label;
  } else if (data.mode === "halt") {
    cls = "danger";
  }
  dot.className = "dot " + cls;
  text.textContent = label + (data.last_run_ts ? ` · last run ${fmtTime(data.last_run_ts)}` : "");
}

function budgetRow(label, usedPct, limitPct) {
  const row = document.createElement("div");
  row.className = "budget-row";
  const pct = usedPct === null || usedPct === undefined ? 0 : usedPct;
  const fillClass = pct >= 100 ? "danger" : pct >= 70 ? "warn" : "";
  row.innerHTML = `
    <div class="label"><span>${label}</span><span>${fmtPct(usedPct)} of ${fmtPct(limitPct)} budget</span></div>
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
  const rec = data.volatility_overlay.latest_recommendation;
  const rows = [["configured mode", data.volatility_overlay.configured_mode]];
  if (rec) {
    rows.push(
      ["observations", rec.observations],
      ["target vol", rec.target_vol !== null ? fmtPct(rec.target_vol * 100) : "—"],
      ["realized vol", rec.realized_vol !== null ? fmtPct(rec.realized_vol * 100) : "—"],
      ["recommended leverage", rec.recommended_leverage !== null ? rec.recommended_leverage.toFixed(2) + "x" : "—"],
      ["ready", rec.ready ? "yes" : "no"],
    );
  } else {
    rows.push(["recommendation", "no data yet"]);
  }
  el.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
}

function renderCooldown(list) {
  const el = document.getElementById("cooldown-list");
  clearChildren(el);
  if (!list.length) {
    el.innerHTML = '<li class="muted">none</li>';
    return;
  }
  list.forEach((r) => {
    const li = document.createElement("li");
    li.textContent = `${r.symbol} — ${r.days_remaining}d remaining (exited ${r.exit_date})`;
    el.appendChild(li);
  });
}

function renderHealth(health) {
  const el = document.getElementById("health-list");
  clearChildren(el);
  if (!health) {
    el.innerHTML = '<li class="muted">no data yet — waiting for the next healthcheck cron run</li>';
    return;
  }
  if (health.healthy) {
    el.innerHTML = `<li>HEALTHY as of ${fmtTime(health.ts)}</li>`;
    return;
  }
  (health.problems || []).forEach((p) => {
    const li = document.createElement("li");
    li.textContent = p;
    li.style.color = "var(--danger)";
    el.appendChild(li);
  });
  if (!health.problems || !health.problems.length) {
    el.innerHTML = `<li class="muted">unhealthy, no detail (as of ${fmtTime(health.ts)})</li>`;
  }
}

function renderPositions(positions) {
  const tbody = document.querySelector("#positions-table tbody");
  clearChildren(tbody);
  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">no open positions</td></tr>';
    return;
  }
  positions.forEach((p) => {
    const tr = document.createElement("tr");
    const pnl = p.cost_basis_available
      ? fmtMoney(p.unrealized_pl)
      : (p.stop_exempt_sleeve ? "cost basis unavailable (stop-exempt sleeve)" : "cost basis unavailable");
    tr.innerHTML = `
      <td>${p.symbol}</td>
      <td>${p.sleeve || "—"}</td>
      <td>${p.qty}</td>
      <td>${fmtMoney(p.price)}</td>
      <td>${fmtMoney(p.market_value)}</td>
      <td>${p.stop_price !== null ? fmtMoney(p.stop_price) : "—"}</td>
      <td>${pnl}</td>
    `;
    tbody.appendChild(tr);
  });
}

function renderExposure(exposure) {
  const tbody = document.querySelector("#exposure-table tbody");
  clearChildren(tbody);
  if (!exposure) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">no exposure snapshot yet</td></tr>';
    return;
  }
  const bySleeve = exposure.by_sleeve || {};
  const sleeves = Object.keys(bySleeve);
  if (!sleeves.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">no sleeve breakdown yet</td></tr>';
    return;
  }
  sleeves.forEach((sleeve) => {
    const actual = bySleeve[sleeve];
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${sleeve}</td>
      <td>—</td>
      <td>${fmtPct(actual.long * 100)} / ${fmtPct(actual.short * 100)} / ${fmtPct(actual.gross * 100)}</td>
      <td>${fmtMoney(actual.unrealized_pl)}</td>
    `;
    tbody.appendChild(tr);
  });
}

function orderRow(o) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td>${fmtTime(o.ts)}</td>
    <td>${o.symbol}</td>
    <td class="side-${o.side}">${o.side}</td>
    <td>${o.sleeve || "—"}</td>
    <td>${o.qty}</td>
    <td>${fmtMoney(o.notional)}</td>
    <td>${o.status || "—"}</td>
    <td>${o.reason || "—"}</td>
  `;
  return tr;
}

function renderOrdersInitial(orders) {
  const tbody = document.querySelector("#orders-table tbody");
  clearChildren(tbody);
  if (!orders.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="muted">no orders in range</td></tr>';
    return;
  }
  // newest first for the feed
  [...orders].reverse().forEach((o) => tbody.appendChild(orderRow(o)));
}

function appendOrders(orders) {
  if (!orders.length) return;
  const tbody = document.querySelector("#orders-table tbody");
  const placeholder = tbody.querySelector("td.muted");
  if (placeholder) clearChildren(tbody);
  [...orders].reverse().forEach((o) => tbody.insertBefore(orderRow(o), tbody.firstChild));
  // cap the visible feed so the DOM doesn't grow without bound over a long session
  while (tbody.children.length > 300) tbody.removeChild(tbody.lastChild);
}

function renderRejections(data) {
  const tbody = document.querySelector("#rejections-table tbody");
  clearChildren(tbody);
  const reasons = data.top_reasons || [];
  if (!reasons.length) {
    tbody.innerHTML = '<tr><td colspan="2" class="muted">no rejections in range</td></tr>';
    return;
  }
  reasons.forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.reason}</td><td>${r.count}</td>`;
    tbody.appendChild(tr);
  });
}

// ---- equity chart (hand-rolled canvas, no dependency) -------------------

function drawEquityChart(payload) {
  const canvas = document.getElementById("equity-chart");
  const ctx = canvas.getContext("2d");
  const cssWidth = canvas.clientWidth || 900;
  const cssHeight = 260;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const points = payload.points || [];
  const pad = { top: 12, right: 12, bottom: 24, left: 64 };
  const plotW = cssWidth - pad.left - pad.right;
  const plotH = cssHeight - pad.top - pad.bottom;

  if (points.length < 2) {
    ctx.fillStyle = "#8a92a3";
    ctx.font = "13px sans-serif";
    ctx.fillText("not enough history yet", pad.left, pad.top + 20);
    return;
  }

  const values = points.map((p) => p.equity);
  const refs = payload.reference_lines || {};
  [refs.peak_drawdown_halt, refs.monthly_kill_switch].forEach((v) => {
    if (v !== null && v !== undefined) values.push(v);
  });
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08;
  max += span * 0.08;

  const x = (i) => pad.left + (i / (points.length - 1)) * plotW;
  const y = (v) => pad.top + plotH - ((v - min) / (max - min)) * plotH;

  // gridlines + axis labels
  ctx.strokeStyle = "#232a38";
  ctx.fillStyle = "#8a92a3";
  ctx.font = "11px sans-serif";
  ctx.lineWidth = 1;
  const ySteps = 4;
  for (let i = 0; i <= ySteps; i++) {
    const v = min + (span * 1.16) * (i / ySteps);
    const yy = y(v);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(cssWidth - pad.right, yy);
    ctx.stroke();
    ctx.fillText("$" + Math.round(v).toLocaleString(), 4, yy + 4);
  }

  function referenceLine(value, color) {
    if (value === null || value === undefined) return;
    const yy = y(value);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(cssWidth - pad.right, yy);
    ctx.stroke();
    ctx.restore();
  }
  referenceLine(refs.peak_drawdown_halt, "#ef5d6f");
  referenceLine(refs.monthly_kill_switch, "#e8b84b");

  // equity line
  ctx.strokeStyle = "#5b9dff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const px = x(i);
    const py = y(p.equity);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  });
  ctx.stroke();

  // x-axis: first/last date labels only, to avoid crowding
  ctx.fillStyle = "#8a92a3";
  ctx.fillText(points[0].date, pad.left, cssHeight - 6);
  const lastLabel = points[points.length - 1].date;
  ctx.fillText(lastLabel, cssWidth - pad.right - ctx.measureText(lastLabel).width, cssHeight - 6);
}

// ---- wiring --------------------------------------------------------

function apiUrl(path) {
  return `/api/${state.profile}${path}`;
}

function startPollers() {
  stopAllTimers();
  state.ordersCursor = null;

  state.timers.push(poll(() => apiUrl("/summary"), 7000, (data) => {
    renderStatus(data);
    renderBudget(data.risk_budget);
    renderOverlay(data);
    renderCooldown(data.reentry_cooldown || []);
    renderHealth(data.health);
  }));

  state.timers.push(poll(() => apiUrl(`/orders?limit=200`), 6000, (data) => {
    if (state.ordersCursor === null) {
      renderOrdersInitial(data.orders);
    } else {
      const fresh = data.orders.filter((o) => !state.ordersCursor || o.ts > state.ordersCursor);
      appendOrders(fresh);
    }
    if (data.latest_ts) state.ordersCursor = data.latest_ts;
  }));

  state.timers.push(poll(() => apiUrl("/positions"), 25000, (data) => {
    renderPositions(data.positions || []);
  }));

  state.timers.push(poll(() => apiUrl("/equity-curve?days=90"), 60000, drawEquityChart));
  state.timers.push(poll(() => apiUrl("/exposure"), 60000, (data) => renderExposure(data.latest_exposure)));
  state.timers.push(poll(() => apiUrl("/rejections?days=7"), 60000, renderRejections));
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

window.addEventListener("resize", () => {
  // Re-fetch is overkill for a resize; the next scheduled poll will redraw
  // the chart at the new width anyway. Nothing to do here.
});

selectProfile("base");
