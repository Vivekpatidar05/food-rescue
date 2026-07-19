// admin.js — Ops Analytics dashboard: impact tiles, 30-day trend chart,
// per-zone forecast chart, fraud-audit table. Charts are hand-rolled SVG.

// Same-origin in production (served behind nginx); localhost API in dev.
const API_BASE =
  location.protocol === "file:" ||
  location.hostname === "localhost" ||
  location.hostname === "127.0.0.1"
    ? "http://localhost:5000"
    : "";

const ROLE_HOME = { donor: "donor.html", ngo: "ngo.html", volunteer: "volunteer.html" };
const SVG_NS = "http://www.w3.org/2000/svg";

// ---------------------------------------------------------------------------
// Auth guard & helpers
// ---------------------------------------------------------------------------

function getToken() {
  return localStorage.getItem("fr_token");
}

function getStoredUser() {
  try {
    return JSON.parse(localStorage.getItem("fr_user"));
  } catch {
    return null;
  }
}

function logout() {
  localStorage.removeItem("fr_token");
  localStorage.removeItem("fr_user");
  window.location.reload(); // back to the admin sign-in screen
}

let currentUser = getStoredUser(); // may be null — the login screen handles it

function showLoginScreen() {
  document.getElementById("admin-login").classList.remove("hidden");
  document.getElementById("admin-main").classList.add("hidden");
  document.getElementById("btn-logout").classList.add("hidden");
  document.getElementById("btn-back").classList.add("hidden");
  document.getElementById("user-name").textContent = "";
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  headers["Authorization"] = `Bearer ${getToken()}`;
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (response.status === 401) showLoginScreen();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

let toastTimer = null;
function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 4000);
}

document.getElementById("btn-logout").addEventListener("click", logout);

function el(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function niceMax(value) {
  if (value <= 0) return 10;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const mult of [1, 2, 2.5, 5, 10]) {
    if (value <= mult * magnitude) return mult * magnitude;
  }
  return 10 * magnitude;
}

function shortDate(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

// ---------------------------------------------------------------------------
// Impact tiles
// ---------------------------------------------------------------------------

async function loadTiles() {
  try {
    const stats = await api("/stats/impact");
    document.getElementById("tile-kg").textContent = stats.total_kg_rescued.toLocaleString();
    document.getElementById("tile-meals").textContent = stats.meals_served_estimate.toLocaleString();
    document.getElementById("tile-rescues").textContent = stats.rescues_completed.toLocaleString();
    document.getElementById("tile-active").textContent = stats.active_batches.toLocaleString();
  } catch (error) {
    showToast(error.message);
  }
}

// ---------------------------------------------------------------------------
// 30-day trend — two-series line chart (broadcast kg vs rescued kg)
// ---------------------------------------------------------------------------

async function loadTrend() {
  const host = document.getElementById("trend-chart");
  let data;
  try {
    data = await api("/stats/trends?days=30");
  } catch (error) {
    host.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    return;
  }
  const daily = data.daily;
  if (!daily.length) {
    host.innerHTML = '<p class="empty-state">No activity recorded yet.</p>';
    return;
  }

  const W = 720, H = 250, L = 46, R = 96, T = 14, B = 30;
  const plotW = W - L - R, plotH = H - T - B;
  const maxY = niceMax(Math.max(...daily.map((d) => Math.max(d.broadcast_kg, d.rescued_kg)), 1));
  const x = (i) => L + (daily.length === 1 ? plotW / 2 : (i / (daily.length - 1)) * plotW);
  const y = (v) => T + plotH - (v / maxY) * plotH;

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  svg.setAttribute("aria-label", "Daily broadcast and rescued kilograms, last 30 days");

  for (let g = 0; g <= 4; g++) {
    const gy = T + (plotH * g) / 4;
    svg.appendChild(el("line", { x1: L, x2: L + plotW, y1: gy, y2: gy, class: g === 4 ? "baseline" : "gridline" }));
    const label = el("text", { x: L - 8, y: gy + 4, "text-anchor": "end", class: "axis-label" });
    label.textContent = Math.round(maxY * (1 - g / 4)).toLocaleString();
    svg.appendChild(label);
  }
  const tickEvery = Math.max(1, Math.round(daily.length / 5));
  daily.forEach((d, i) => {
    if (i % tickEvery !== 0 && i !== daily.length - 1) return;
    const label = el("text", { x: x(i), y: T + plotH + 18, "text-anchor": "middle", class: "axis-label" });
    label.textContent = shortDate(d.date);
    svg.appendChild(label);
  });

  const series = [
    { key: "broadcast_kg", name: "Broadcast", cssVar: "--series-broadcast" },
    { key: "rescued_kg", name: "Rescued", cssVar: "--series-rescued" },
  ];
  const styles = getComputedStyle(document.documentElement);
  series.forEach((s) => {
    s.color = styles.getPropertyValue(s.cssVar).trim();
    const path = daily.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d[s.key]).toFixed(1)}`).join("");
    svg.appendChild(el("path", { d: path, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round" }));
    // Direct label at line end — colored dot carries identity, text stays ink.
    const last = daily[daily.length - 1];
    svg.appendChild(el("circle", { cx: x(daily.length - 1), cy: y(last[s.key]), r: 3.5, fill: s.color }));
    const label = el("text", { x: x(daily.length - 1) + 8, y: y(last[s.key]) + 4, class: "end-label" });
    label.textContent = `${s.name} ${last[s.key]}`;
    svg.appendChild(label);
  });

  // Hover layer: crosshair + shared tooltip.
  const crosshair = el("line", { y1: T, y2: T + plotH, class: "baseline", opacity: 0 });
  svg.appendChild(crosshair);
  const hoverDots = series.map((s) => {
    const dot = el("circle", { r: 4.5, fill: s.color, stroke: "var(--surface)", "stroke-width": 2, opacity: 0 });
    svg.appendChild(dot);
    return dot;
  });
  const overlay = el("rect", { x: L, y: T, width: plotW, height: plotH, fill: "transparent" });
  svg.appendChild(overlay);
  const tooltip = document.getElementById("trend-tooltip");

  overlay.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(daily.length - 1, Math.round(((px - L) / plotW) * (daily.length - 1))));
    const d = daily[i];
    crosshair.setAttribute("x1", x(i));
    crosshair.setAttribute("x2", x(i));
    crosshair.setAttribute("opacity", 1);
    series.forEach((s, k) => {
      hoverDots[k].setAttribute("cx", x(i));
      hoverDots[k].setAttribute("cy", y(d[s.key]));
      hoverDots[k].setAttribute("opacity", 1);
    });
    tooltip.innerHTML = `
      <span class="tt-title">${escapeHtml(shortDate(d.date))}</span>
      <span class="tt-row"><span class="dot dot-broadcast"></span>Broadcast <b>&nbsp;${escapeHtml(d.broadcast_kg)} kg</b> · ${escapeHtml(d.broadcasts)} batches</span>
      <span class="tt-row"><span class="dot dot-rescued"></span>Rescued <b>&nbsp;${escapeHtml(d.rescued_kg)} kg</b> · ${escapeHtml(d.rescues)} rescues</span>`;
    tooltip.classList.remove("hidden");
    const wrap = host.parentElement.getBoundingClientRect();
    const ttX = ((x(i) / W) * rect.width) + 14;
    tooltip.style.left = `${Math.min(ttX, wrap.width - 180)}px`;
    tooltip.style.top = "10px";
  });
  overlay.addEventListener("mouseleave", () => {
    tooltip.classList.add("hidden");
    crosshair.setAttribute("opacity", 0);
    hoverDots.forEach((dot) => dot.setAttribute("opacity", 0));
  });

  host.replaceChildren(svg);

  document.getElementById("trend-table").innerHTML =
    "<thead><tr><th>Date</th><th class='num'>Broadcast kg</th><th class='num'>Batches</th><th class='num'>Rescued kg</th><th class='num'>Rescues</th></tr></thead><tbody>" +
    daily.slice().reverse().map((d) =>
      `<tr><td>${escapeHtml(d.date)}</td><td class="num">${escapeHtml(d.broadcast_kg)}</td><td class="num">${escapeHtml(d.broadcasts)}</td><td class="num">${escapeHtml(d.rescued_kg)}</td><td class="num">${escapeHtml(d.rescues)}</td></tr>`
    ).join("") + "</tbody>";
}

// ---------------------------------------------------------------------------
// 7-day forecast — sorted horizontal bars (single-hue magnitude)
// ---------------------------------------------------------------------------

async function loadForecast() {
  const host = document.getElementById("forecast-chart");
  let data;
  try {
    data = await api("/api/predict-surplus");
  } catch (error) {
    host.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    return;
  }
  const zones = data.zones || [];
  if (!zones.length) {
    host.innerHTML = '<p class="empty-state">No forecast available — seed history first.</p>';
    return;
  }
  document.getElementById("forecast-sub").textContent =
    `Next ${data.horizon_days} days · fleet of ${data.fleet_size} volunteers · trained on ${data.history_days} days of history`;

  const rowH = 30, gap = 8, L = 150, R = 70, T = 6;
  const W = 720, plotW = W - L - R;
  const H = T + zones.length * (rowH + gap);
  const maxKg = niceMax(Math.max(...zones.map((z) => z.total_predicted_kg), 1));
  const styles = getComputedStyle(document.documentElement);
  const barColor = styles.getPropertyValue("--seq-bar").trim();

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  svg.setAttribute("aria-label", "Predicted surplus kg per zone over the next 7 days");
  const tooltip = document.getElementById("forecast-tooltip");

  zones.forEach((zone, i) => {
    const yTop = T + i * (rowH + gap);
    const barW = Math.max(2, (zone.total_predicted_kg / maxKg) * plotW);
    const name = el("text", { x: L - 10, y: yTop + rowH / 2 + 4, "text-anchor": "end", class: "zone-label" });
    name.textContent = zone.zone;
    svg.appendChild(name);
    // Bar: squared at the baseline, 4px rounded data-end.
    const r = Math.min(4, barW / 2);
    const bar = el("path", {
      d: `M${L},${yTop} h${barW - r} a${r},${r} 0 0 1 ${r},${r} v${rowH - 2 * r} a${r},${r} 0 0 1 -${r},${r} h-${barW - r} Z`,
      fill: barColor,
    });
    svg.appendChild(bar);
    const value = el("text", { x: L + barW + 8, y: yTop + rowH / 2 + 4, class: "bar-label" });
    value.textContent = `${zone.total_predicted_kg.toLocaleString()} kg`;
    svg.appendChild(value);

    const hit = el("rect", { x: 0, y: yTop - gap / 2, width: W, height: rowH + gap, fill: "transparent" });
    hit.addEventListener("mousemove", (event) => {
      tooltip.innerHTML = `
        <span class="tt-title">${escapeHtml(zone.zone)}</span>
        Predicted: <b>${escapeHtml(zone.total_predicted_kg)} kg</b> over 7 days<br />
        Peak day: <b>${escapeHtml(shortDate(zone.peak_day))}</b><br />
        Pre-position: <b>${escapeHtml(zone.suggested_volunteers)} volunteer${zone.suggested_volunteers === 1 ? "" : "s"}</b><br />
        Model: ${escapeHtml(zone.model)}`;
      tooltip.classList.remove("hidden");
      const wrap = host.parentElement.getBoundingClientRect();
      tooltip.style.left = `${Math.min(event.clientX - wrap.left + 16, wrap.width - 200)}px`;
      tooltip.style.top = `${event.clientY - wrap.top + 14}px`;
    });
    hit.addEventListener("mouseleave", () => tooltip.classList.add("hidden"));
    svg.appendChild(hit);
  });

  svg.appendChild(el("line", { x1: L, x2: L, y1: T, y2: H, class: "baseline" }));
  host.replaceChildren(svg);

  document.getElementById("forecast-table").innerHTML =
    "<thead><tr><th>Zone</th><th class='num'>Predicted kg (7d)</th><th>Peak day</th><th class='num'>Suggested volunteers</th><th>Model</th></tr></thead><tbody>" +
    zones.map((z) =>
      `<tr><td>${escapeHtml(z.zone)}</td><td class="num">${escapeHtml(z.total_predicted_kg)}</td><td>${escapeHtml(z.peak_day)}</td><td class="num">${escapeHtml(z.suggested_volunteers)}</td><td>${escapeHtml(z.model)}</td></tr>`
    ).join("") + "</tbody>";
}

// ---------------------------------------------------------------------------
// Fraud audit table
// ---------------------------------------------------------------------------

// Per-feature deviation bars: the model's "why", ranked by standard deviations
// above the peer mean. Only above-peer (positive-z) features are shown; the bar
// saturates at 6 sigma so blatant outliers still read on the same scale.
const Z_CAP = 6;
function explainBars(explain) {
  const top = (explain || []).filter((c) => c.z > 0.5).slice(0, 3);
  if (!top.length) return "";
  return `<div class="zbars">${top
    .map((c) => {
      const pct = Math.max(6, Math.min(100, (c.z / Z_CAP) * 100));
      return `<div class="zbar-row" title="${escapeHtml(c.label)}: ${escapeHtml(
        c.value
      )} vs peer mean ${escapeHtml(c.peer_mean)}">
        <span class="zbar-label">${escapeHtml(c.label)}</span>
        <span class="zbar-track"><span class="zbar-fill" style="width:${pct}%"></span></span>
        <span class="zbar-val">+${c.z.toFixed(1)}σ</span>
      </div>`;
    })
    .join("")}</div>`;
}

function renderAudit(report) {
  const table = document.getElementById("audit-table");
  document.getElementById("audit-sub").textContent =
    `${report.accounts_analyzed} accounts · ${report.events_analyzed} completed rescues analyzed · model: ${report.model || "—"}`;
  const flagged = report.flagged || [];
  if (!flagged.length) {
    table.innerHTML = "";
    table.insertAdjacentHTML("afterend", "");
    table.innerHTML = "<tbody><tr><td class='empty-state'>No anomalous accounts flagged. ✅</td></tr></tbody>";
    return;
  }
  table.innerHTML =
    "<thead><tr><th>Account</th><th>Role</th><th class='num'>Trust</th><th class='num'>Anomaly</th><th>Why flagged — and how far above peers</th><th>Action</th></tr></thead><tbody>" +
    flagged.map((f) => {
      const critical = f.recommended_action === "suspend pending review";
      const chip = critical
        ? '<span class="chip chip-critical">⛔ Suspend pending review</span>'
        : '<span class="chip chip-serious">🔍 Manual review</span>';
      return `<tr>
        <td>${escapeHtml(f.name)}${f.is_synthetic ? ' <span class="chip">🧪 synthetic</span>' : ""}</td>
        <td>${escapeHtml(f.role)}</td>
        <td class="num">${escapeHtml(f.trust_score)}</td>
        <td class="num">${escapeHtml(f.anomaly_score)}</td>
        <td><ul class="reason-list">${f.reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>${explainBars(f.explain)}</td>
        <td>${chip}</td>
      </tr>`;
    }).join("") + "</tbody>";
}

async function loadAudit() {
  const table = document.getElementById("audit-table");
  try {
    renderAudit(await api("/api/run-audit"));
  } catch (error) {
    table.innerHTML = `<tbody><tr><td class="empty-state">${escapeHtml(
      error.status === 403 ? "Audit access is restricted to admin accounts." : error.message
    )}</td></tr></tbody>`;
  }
}

document.getElementById("btn-rerun-audit").addEventListener("click", async () => {
  showToast("Re-running trust audit…");
  await loadAudit();
  showToast("Audit refreshed.");
});

// ---------------------------------------------------------------------------
// Citywide forecast with an 80% prediction interval (shaded band + line)
// ---------------------------------------------------------------------------

async function loadForecastBand() {
  const host = document.getElementById("band-chart");
  let data;
  try {
    data = await api("/api/predict-surplus");
  } catch (error) {
    host.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    return;
  }
  const daily = data.citywide_daily || [];
  if (!daily.length || daily[0].upper_kg === undefined) {
    host.innerHTML = '<p class="empty-state">No forecast available — seed history first.</p>';
    return;
  }
  document.getElementById("band-sub").textContent =
    `Point forecast with an ${data.interval_level || 80}% prediction interval · fleet of ${data.fleet_size} · trained on ${data.history_days} days`;

  const W = 720, H = 250, L = 48, R = 60, T = 14, B = 30;
  const plotW = W - L - R, plotH = H - T - B;
  const maxY = niceMax(Math.max(...daily.map((d) => d.upper_kg), 1));
  const x = (i) => L + (daily.length === 1 ? plotW / 2 : (i / (daily.length - 1)) * plotW);
  const y = (v) => T + plotH - (v / maxY) * plotH;

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  svg.setAttribute("aria-label", "Citywide 7-day surplus forecast with 80% prediction interval");

  for (let g = 0; g <= 4; g++) {
    const gy = T + (plotH * g) / 4;
    svg.appendChild(el("line", { x1: L, x2: L + plotW, y1: gy, y2: gy, class: g === 4 ? "baseline" : "gridline" }));
    const label = el("text", { x: L - 8, y: gy + 4, "text-anchor": "end", class: "axis-label" });
    label.textContent = Math.round(maxY * (1 - g / 4)).toLocaleString();
    svg.appendChild(label);
  }
  daily.forEach((d, i) => {
    const label = el("text", { x: x(i), y: T + plotH + 18, "text-anchor": "middle", class: "axis-label" });
    label.textContent = shortDate(d.date);
    svg.appendChild(label);
  });

  // Band: uppers left→right, then lowers right→left, closed.
  const upper = daily.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.upper_kg).toFixed(1)}`).join("");
  const lower = daily.slice().reverse().map((d, k) => `L${x(daily.length - 1 - k).toFixed(1)},${y(d.lower_kg).toFixed(1)}`).join("");
  svg.appendChild(el("path", { d: `${upper}${lower}Z`, class: "band-area" }));

  const line = daily.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.predicted_kg).toFixed(1)}`).join("");
  svg.appendChild(el("path", { d: line, class: "forecast-line" }));
  const styles = getComputedStyle(document.documentElement);
  const lineColor = styles.getPropertyValue("--series-actual").trim();
  daily.forEach((d, i) => svg.appendChild(el("circle", { cx: x(i), cy: y(d.predicted_kg), r: 3, fill: lineColor })));

  const crosshair = el("line", { y1: T, y2: T + plotH, class: "baseline", opacity: 0 });
  svg.appendChild(crosshair);
  const overlay = el("rect", { x: L, y: T, width: plotW, height: plotH, fill: "transparent" });
  svg.appendChild(overlay);
  const tooltip = document.getElementById("band-tooltip");
  overlay.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(daily.length - 1, Math.round(((px - L) / plotW) * (daily.length - 1))));
    const d = daily[i];
    crosshair.setAttribute("x1", x(i));
    crosshair.setAttribute("x2", x(i));
    crosshair.setAttribute("opacity", 1);
    tooltip.innerHTML = `<span class="tt-title">${escapeHtml(shortDate(d.date))}</span>
      Forecast <b>${escapeHtml(d.predicted_kg)} kg</b><br />
      80% range <b>${escapeHtml(d.lower_kg)}–${escapeHtml(d.upper_kg)} kg</b>`;
    tooltip.classList.remove("hidden");
    const wrap = host.parentElement.getBoundingClientRect();
    tooltip.style.left = `${Math.min((x(i) / W) * rect.width + 14, wrap.width - 170)}px`;
    tooltip.style.top = "10px";
  });
  overlay.addEventListener("mouseleave", () => {
    tooltip.classList.add("hidden");
    crosshair.setAttribute("opacity", 0);
  });

  host.replaceChildren(svg);
}

// ---------------------------------------------------------------------------
// Forecast accuracy — scorecard + predicted-vs-actual overlay (backtest)
// ---------------------------------------------------------------------------

async function loadAccuracy() {
  const cards = document.getElementById("accuracy-cards");
  const host = document.getElementById("overlay-chart");
  let data;
  try {
    data = await api("/api/forecast-accuracy");
  } catch (error) {
    cards.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    return;
  }
  if (data.error) {
    cards.innerHTML = `<p class="empty-state">${escapeHtml(data.error)}</p>`;
    return;
  }
  document.getElementById("accuracy-sub").textContent =
    `${data.folds_evaluated}-fold walk-forward · ${data.points_evaluated} held-out days · scored on ${data.evaluation_scale} vs a ${data.baseline.name} baseline`;

  const skillPct = Math.round((data.skill_score || 0) * 100);
  const skillText = skillPct > 0 ? `+${skillPct}%` : skillPct === 0 ? "on par" : `${skillPct}%`;
  const tiles = [
    { value: `${data.model.wape}%`, label: "Daily error (WAPE)", help: `vs ${data.baseline.wape}% for the naive baseline` },
    { value: `${data.model.mae}`, label: "Mean abs error (kg/day)", help: `RMSE ${data.model.rmse} kg` },
    { value: skillText, label: "Skill vs naive baseline", help: "share of baseline error removed", good: skillPct > 0 },
    { value: data.zone_rank_accuracy.toFixed(2), label: "Zone-ranking ρ", help: "how well it orders zones for the fleet split", good: data.zone_rank_accuracy >= 0.5 },
  ];
  cards.innerHTML = tiles
    .map((t) => `<div class="score-tile">
      <div class="score-value${t.good ? " good" : ""}">${escapeHtml(t.value)}</div>
      <div class="score-label">${escapeHtml(t.label)}</div>
      <div class="score-help">${escapeHtml(t.help)}</div>
    </div>`)
    .join("");

  const overlay = data.overlay || [];
  if (!overlay.length) {
    host.innerHTML = '<p class="empty-state">Not enough held-out history to chart.</p>';
    return;
  }
  const W = 720, H = 240, L = 48, R = 20, T = 14, B = 30;
  const plotW = W - L - R, plotH = H - T - B;
  const maxY = niceMax(Math.max(...overlay.map((d) => Math.max(d.actual_kg, d.predicted_kg)), 1));
  const x = (i) => L + (overlay.length === 1 ? plotW / 2 : (i / (overlay.length - 1)) * plotW);
  const y = (v) => T + plotH - (v / maxY) * plotH;

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  svg.setAttribute("aria-label", "Predicted versus actual citywide surplus on held-out days");
  for (let g = 0; g <= 4; g++) {
    const gy = T + (plotH * g) / 4;
    svg.appendChild(el("line", { x1: L, x2: L + plotW, y1: gy, y2: gy, class: g === 4 ? "baseline" : "gridline" }));
    const label = el("text", { x: L - 8, y: gy + 4, "text-anchor": "end", class: "axis-label" });
    label.textContent = Math.round(maxY * (1 - g / 4)).toLocaleString();
    svg.appendChild(label);
  }
  overlay.forEach((d, i) => {
    const label = el("text", { x: x(i), y: T + plotH + 18, "text-anchor": "middle", class: "axis-label" });
    label.textContent = shortDate(d.date);
    svg.appendChild(label);
  });
  const actualPath = overlay.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.actual_kg).toFixed(1)}`).join("");
  const predPath = overlay.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.predicted_kg).toFixed(1)}`).join("");
  svg.appendChild(el("path", { d: actualPath, class: "actual-line" }));
  svg.appendChild(el("path", { d: predPath, class: "predicted-line" }));

  const crosshair = el("line", { y1: T, y2: T + plotH, class: "baseline", opacity: 0 });
  svg.appendChild(crosshair);
  const hit = el("rect", { x: L, y: T, width: plotW, height: plotH, fill: "transparent" });
  svg.appendChild(hit);
  const tooltip = document.getElementById("overlay-tooltip");
  hit.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(overlay.length - 1, Math.round(((px - L) / plotW) * (overlay.length - 1))));
    const d = overlay[i];
    crosshair.setAttribute("x1", x(i));
    crosshair.setAttribute("x2", x(i));
    crosshair.setAttribute("opacity", 1);
    tooltip.innerHTML = `<span class="tt-title">${escapeHtml(shortDate(d.date))}</span>
      <span class="tt-row"><span class="dot dot-actual"></span>Actual <b>&nbsp;${escapeHtml(d.actual_kg)} kg</b></span>
      <span class="tt-row"><span class="dot dot-predicted"></span>Forecast <b>&nbsp;${escapeHtml(d.predicted_kg)} kg</b></span>`;
    tooltip.classList.remove("hidden");
    const wrap = host.parentElement.getBoundingClientRect();
    tooltip.style.left = `${Math.min((x(i) / W) * rect.width + 14, wrap.width - 170)}px`;
    tooltip.style.top = "10px";
  });
  hit.addEventListener("mouseleave", () => {
    tooltip.classList.add("hidden");
    crosshair.setAttribute("opacity", 0);
  });
  host.replaceChildren(svg);
}

// ---------------------------------------------------------------------------
// Demand heatmap — weekday × hour, single sequential hue
// ---------------------------------------------------------------------------

async function loadHeatmap() {
  const host = document.getElementById("heatmap");
  let data;
  try {
    data = await api("/api/demand-patterns");
  } catch (error) {
    host.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    return;
  }
  const grid = data.grid || [];
  if (!grid.length || !data.max_kg) {
    host.innerHTML = '<p class="empty-state">No demand history yet — seed data first.</p>';
    return;
  }
  if (data.peak) {
    document.getElementById("heatmap-sub").textContent =
      `Peak: ${data.peak.weekday} ${String(data.peak.hour).padStart(2, "0")}:00 (${data.peak.kg} kg) · ${data.total_kg.toLocaleString()} kg across all history`;
  }

  const tooltip = document.getElementById("heatmap-tooltip");
  const wrap = document.createElement("div");
  wrap.className = "heatmap-grid";
  wrap.appendChild(Object.assign(document.createElement("div"), { className: "heat-corner" }));
  for (let h = 0; h < 24; h++) {
    const head = document.createElement("div");
    head.className = "heat-colhead";
    head.textContent = h % 3 === 0 ? String(h) : "";
    wrap.appendChild(head);
  }
  data.weekdays.forEach((day, d) => {
    const rowhead = document.createElement("div");
    rowhead.className = "heat-rowhead";
    rowhead.textContent = day;
    wrap.appendChild(rowhead);
    for (let h = 0; h < 24; h++) {
      const kg = grid[d][h];
      const count = (data.counts && data.counts[d] && data.counts[d][h]) || 0;
      const t = Math.sqrt(kg / data.max_kg); // sqrt: perceptual spread for skewed volume
      const cell = document.createElement("div");
      cell.className = "heat-cell";
      cell.style.background = `color-mix(in oklab, var(--heat-hue) ${(t * 100).toFixed(0)}%, var(--heat-base))`;
      cell.addEventListener("mousemove", (event) => {
        tooltip.innerHTML = `<span class="tt-title">${escapeHtml(day)} ${String(h).padStart(2, "0")}:00</span>
          <b>${escapeHtml(kg)} kg</b> · ${escapeHtml(count)} batches`;
        tooltip.classList.remove("hidden");
        const box = host.parentElement.getBoundingClientRect();
        tooltip.style.left = `${Math.min(event.clientX - box.left + 14, box.width - 160)}px`;
        tooltip.style.top = `${event.clientY - box.top + 14}px`;
      });
      cell.addEventListener("mouseleave", () => tooltip.classList.add("hidden"));
      wrap.appendChild(cell);
    }
  });

  const scale = document.createElement("div");
  scale.className = "heat-scale";
  scale.innerHTML = `<span>Low</span><span class="heat-ramp"></span><span>High (${data.max_kg} kg)</span>`;
  host.replaceChildren(wrap, scale);
}

// ---------------------------------------------------------------------------
// Surge mode controls
// ---------------------------------------------------------------------------

async function loadSurge() {
  const state = document.getElementById("surge-state");
  try {
    const { surge } = await api("/admin/surge");
    if (surge.active) {
      state.textContent = `🔴 ACTIVE ${surge.multiplier}× — ${surge.reason}`;
      state.className = "surge-state on";
      document.getElementById("surge-reason").value = surge.reason || "";
    } else {
      state.textContent = "⚪ inactive";
      state.className = "surge-state";
    }
  } catch (error) {
    state.textContent = error.status === 403 ? "admin only" : "unavailable";
  }
}

document.getElementById("btn-surge-on").addEventListener("click", async () => {
  const reason = document.getElementById("surge-reason").value.trim();
  if (!reason) {
    frUI.toast("A public reason is required — it appears on every dashboard", "error");
    return;
  }
  try {
    await api("/admin/surge", {
      method: "POST",
      body: JSON.stringify({
        active: true,
        reason,
        multiplier: Number(document.getElementById("surge-mult").value),
      }),
    });
    frUI.toast("Surge mode ACTIVE — every radius widened, partners alerted 🚨", "party");
    loadSurge();
  } catch (error) {
    frUI.toast(error.message, "error");
  }
});

document.getElementById("btn-surge-off").addEventListener("click", async () => {
  try {
    await api("/admin/surge", { method: "POST", body: JSON.stringify({ active: false }) });
    frUI.toast("Surge mode deactivated — back to normal matching", "success");
    loadSurge();
  } catch (error) {
    frUI.toast(error.message, "error");
  }
});

// ---------------------------------------------------------------------------
// User management
// ---------------------------------------------------------------------------

function userRow(user) {
  const rating = user.rating ? `★ ${user.rating}` : "—";
  const status = user.suspended
    ? `<span class="user-flag suspended" title="${escapeHtml(user.suspended_reason || "")}">suspended</span>`
    : '<span class="user-flag ok">active</span>';
  return `
    <tr class="${user.suspended ? "row-suspended" : ""}">
      <td><b>${escapeHtml(user.name)}</b><br /><span class="user-email">${escapeHtml(user.email)}</span></td>
      <td>${escapeHtml(user.role)}${user.partner_type === "animal_shelter" ? " 🐾" : ""}</td>
      <td class="num">🛡 ${escapeHtml(user.trust_score)}</td>
      <td class="num">${escapeHtml(rating)}</td>
      <td>${status}</td>
      <td class="user-actions">
        <button class="btn-small" data-trust="${escapeHtml(user._id)}">± trust</button>
        <button class="btn-small ${user.suspended ? "" : "btn-danger-sm"}" data-suspend="${escapeHtml(user._id)}"
          data-suspended="${user.suspended ? "1" : ""}">
          ${user.suspended ? "✓ Reinstate" : "⛔ Suspend"}
        </button>
      </td>
    </tr>`;
}

async function loadUsers() {
  const table = document.getElementById("users-table");
  const q = document.getElementById("user-search").value.trim();
  const role = document.getElementById("user-role").value;
  try {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (role) params.set("role", role);
    const { users } = await api(`/admin/users?${params}`);
    document.getElementById("users-sub").textContent =
      `${users.length} account${users.length === 1 ? "" : "s"} shown — suspensions and trust edits are audited`;
    table.innerHTML =
      "<thead><tr><th>Account</th><th>Role</th><th>Trust</th><th>Rating</th><th>Status</th><th></th></tr></thead>" +
      `<tbody>${users.map(userRow).join("")}</tbody>`;

    table.querySelectorAll("[data-suspend]").forEach((button) =>
      button.addEventListener("click", async () => {
        const suspending = !button.dataset.suspended;
        const reason = suspending
          ? window.prompt("Reason for suspension (shown in the audit trail):") || ""
          : "";
        if (suspending && reason === null) return;
        try {
          await api(`/admin/users/${button.dataset.suspend}/suspend`, {
            method: "POST",
            body: JSON.stringify({ suspended: suspending, reason }),
          });
          frUI.toast(suspending ? "Account suspended ⛔" : "Account reinstated ✓", "success");
          loadUsers();
        } catch (error) {
          frUI.toast(error.message, "error");
        }
      })
    );
    table.querySelectorAll("[data-trust]").forEach((button) =>
      button.addEventListener("click", async () => {
        const delta = Number(window.prompt("Trust delta (e.g. 10 or -20, within ±100):"));
        if (!delta) return;
        const reason = window.prompt("Reason (required — goes to the audit log):");
        if (!reason) return;
        try {
          const data = await api(`/admin/users/${button.dataset.trust}/trust`, {
            method: "POST",
            body: JSON.stringify({ delta, reason }),
          });
          frUI.toast(`Trust updated → ${data.trust_score}`, "success");
          loadUsers();
        } catch (error) {
          frUI.toast(error.message, "error");
        }
      })
    );
  } catch (error) {
    table.innerHTML = `<tbody><tr><td>${escapeHtml(
      error.status === 403 ? "Admin access required (set ADMIN_EMAILS in .env)" : error.message
    )}</td></tr></tbody>`;
  }
}

let userSearchTimer = null;
document.getElementById("user-search").addEventListener("input", () => {
  clearTimeout(userSearchTimer);
  userSearchTimer = setTimeout(loadUsers, 350);
});
document.getElementById("user-role").addEventListener("change", loadUsers);

// ---------------------------------------------------------------------------
// Ratings feed
// ---------------------------------------------------------------------------

async function loadRatingsFeed() {
  const feed = document.getElementById("ratings-feed");
  try {
    const { recent_ratings } = await api("/admin/overview");
    if (!recent_ratings.length) {
      feed.innerHTML = '<p class="panel-sub">No ratings posted yet.</p>';
      return;
    }
    feed.innerHTML = recent_ratings
      .map(
        (r) => `
          <div class="rating-row">
            <span class="fr-stars-inline">${"★".repeat(r.stars)}${"☆".repeat(5 - r.stars)}</span>
            <span><b>${escapeHtml(r.rater_name)}</b> → <b>${escapeHtml(r.ratee_name)}</b>
              ${r.comment ? ` · “${escapeHtml(r.comment)}”` : ""}</span>
          </div>`
      )
      .join("");
  } catch (error) {
    feed.innerHTML = `<p class="panel-sub">${escapeHtml(
      error.status === 403 ? "Admin access required" : error.message
    )}</p>`;
  }
}

// ---------------------------------------------------------------------------
// Live city map — hand-rolled SVG plot of the zone grid with live counts
// ---------------------------------------------------------------------------

async function loadZoneMap() {
  const host = document.getElementById("zone-map");
  let zones;
  try {
    ({ zones } = await api("/stats/zones-live"));
  } catch (error) {
    host.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    return;
  }
  if (!zones || !zones.length) {
    host.innerHTML = '<p class="empty-state">No zones configured yet.</p>';
    return;
  }

  const W = 720, H = 320, PAD = 56;
  const lngs = zones.map((z) => z.lng);
  const lats = zones.map((z) => z.lat);
  const minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);
  const x = (lng) => PAD + ((lng - minLng) / (maxLng - minLng || 1)) * (W - PAD * 2);
  // Latitude grows northwards — invert for screen coordinates.
  const y = (lat) => H - PAD - ((lat - minLat) / (maxLat - minLat || 1)) * (H - PAD * 2);

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  svg.setAttribute("aria-label", "Live batches per city zone");
  const tooltip = document.getElementById("zone-map-tooltip");

  zones.forEach((zone) => {
    const cx = x(zone.lng), cy = y(zone.lat);
    const live = zone.pending + zone.moving;
    const r = 10 + Math.min(26, Math.sqrt(live) * 7);
    const g = el("g", { class: "zone-node" });
    g.appendChild(el("circle", { cx, cy, r, class: live ? "zone-live" : "zone-quiet" }));
    if (zone.pending) {
      g.appendChild(el("circle", { cx, cy, r: r + 5, class: "zone-pulse" }));
    }
    const count = el("text", { x: cx, y: cy + 4, "text-anchor": "middle", class: "zone-count" });
    count.textContent = live || "";
    g.appendChild(count);
    const label = el("text", { x: cx, y: cy + r + 14, "text-anchor": "middle", class: "zone-label" });
    label.textContent = zone.name;
    g.appendChild(label);

    g.addEventListener("mousemove", (event) => {
      const rect = host.getBoundingClientRect();
      tooltip.style.left = `${event.clientX - rect.left + 14}px`;
      tooltip.style.top = `${event.clientY - rect.top - 10}px`;
      tooltip.innerHTML = `<b>${escapeHtml(zone.name)}</b><br />
        🍲 ${zone.pending} pending · 🚗 ${zone.moving} in motion<br />
        ✅ ${zone.completed_today} completed today`;
      tooltip.classList.remove("hidden");
    });
    g.addEventListener("mouseleave", () => tooltip.classList.add("hidden"));
    svg.appendChild(g);
  });

  host.innerHTML = "";
  host.appendChild(svg);
}

// ---------------------------------------------------------------------------
// Verification queue — approve / reject new accounts (KYC review)
// ---------------------------------------------------------------------------

const DETAIL_LABELS = {
  phone: "Phone", org_type: "Type", business_name: "Business",
  id_number: "ID no.", registration_number: "Reg. no.", darpan_id: "Darpan",
  driving_licence: "Licence", vehicle_type: "Vehicle",
};
const ROLE_EMOJI = { donor: "🍲", ngo: "🏛️", volunteer: "🚗" };

function verifyCard(request) {
  const v = request.verification || {};
  const details = v.details || {};
  const chips = Object.keys(details)
    .filter((key) => details[key])
    .map(
      (key) =>
        `<span class="verify-chip"><b>${escapeHtml(DETAIL_LABELS[key] || key)}:</b> ${escapeHtml(
          String(details[key]).replace(/_/g, " ")
        )}</span>`
    )
    .join("");
  const submitted = v.submitted_at ? new Date(v.submitted_at).toLocaleString() : "—";
  return `
    <div class="verify-card" data-id="${escapeHtml(request._id)}">
      <div class="verify-card-head">
        <div>
          <b>${ROLE_EMOJI[request.role] || "👤"} ${escapeHtml(request.name)}</b>
          <span class="verify-role">${escapeHtml(request.role)}</span><br />
          <span class="user-email">${escapeHtml(request.email)} · ${escapeHtml(request.address || "")}</span>
        </div>
        <span class="verify-when">submitted ${escapeHtml(submitted)}</span>
      </div>
      <div class="verify-chips">${chips || '<span class="user-email">no details supplied</span>'}</div>
      ${v.rejection_reason ? `<p class="verify-reject-note">Last rejection: ${escapeHtml(v.rejection_reason)}</p>` : ""}
      <div class="verify-actions">
        ${v.has_document ? `<button class="btn-small" data-doc="${escapeHtml(request._id)}">🪪 View ID document</button>` : '<span class="user-email">no ID document</span>'}
        <span class="verify-spacer"></span>
        <button class="btn-small btn-approve" data-approve="${escapeHtml(request._id)}">✅ Approve</button>
        <button class="btn-small btn-danger-sm" data-reject="${escapeHtml(request._id)}">✕ Reject</button>
      </div>
    </div>`;
}

async function loadVerifications() {
  const host = document.getElementById("verify-list");
  const status = document.getElementById("verify-status").value;
  try {
    const { requests } = await api(`/admin/verifications?status=${status}`);
    if (status === "pending") {
      document.getElementById("tile-pending-verify").textContent = requests.length;
      setVerifyBadge(requests.length);
    }
    if (!requests.length) {
      host.innerHTML = `<p class="panel-sub">${
        status === "pending" ? "🎉 Queue is clear — no accounts waiting." : `No ${status} accounts.`
      }</p>`;
      return;
    }
    host.innerHTML = requests.map(verifyCard).join("");

    host.querySelectorAll("[data-doc]").forEach((button) =>
      button.addEventListener("click", async () => {
        try {
          const doc = await api(`/admin/verifications/${button.dataset.doc}/document`);
          const overlay = document.createElement("div");
          overlay.className = "doc-overlay";
          overlay.innerHTML = `<div class="doc-frame"><p>${escapeHtml(doc.name || "")} — ID document</p>
            <img src="${doc.data_uri}" alt="ID document" /><button class="btn-small" type="button">Close</button></div>`;
          overlay.addEventListener("click", (event) => {
            if (event.target === overlay || event.target.tagName === "BUTTON") overlay.remove();
          });
          document.body.appendChild(overlay);
        } catch (error) {
          frUI.toast(error.message, "error");
        }
      })
    );
    host.querySelectorAll("[data-approve]").forEach((button) =>
      button.addEventListener("click", async () => {
        try {
          await api(`/admin/verifications/${button.dataset.approve}/approve`, { method: "POST" });
          frUI.toast("Account approved — they're live! ✅", "success");
          loadVerifications();
          loadUsers();
        } catch (error) {
          frUI.toast(error.message, "error");
        }
      })
    );
    host.querySelectorAll("[data-reject]").forEach((button) =>
      button.addEventListener("click", async () => {
        const reason = window.prompt("Rejection reason (the applicant sees this):");
        if (!reason) return;
        try {
          await api(`/admin/verifications/${button.dataset.reject}/reject`, {
            method: "POST",
            body: JSON.stringify({ reason }),
          });
          frUI.toast("Verification rejected — applicant notified", "info");
          loadVerifications();
        } catch (error) {
          frUI.toast(error.message, "error");
        }
      })
    );
  } catch (error) {
    host.innerHTML = `<p class="panel-sub">${escapeHtml(error.message)}</p>`;
  }
}

document.getElementById("verify-status").addEventListener("change", loadVerifications);

// ---------------------------------------------------------------------------
// Tabbed navigation — the command center's sections, properly organized
// ---------------------------------------------------------------------------

const ADMIN_TABS = [
  ["overview", "🏠", "Overview"],
  ["verify", "🛡️", "Verifications"],
  ["users", "👥", "Users"],
  ["analytics", "📈", "Analytics & Forecasts"],
  ["fraud", "🕵️", "Fraud Audit"],
];

function switchTab(tabId) {
  document.querySelectorAll("#admin-main [data-tab]").forEach((section) => {
    section.classList.toggle("hidden", section.dataset.tab !== tabId);
  });
  document.querySelectorAll("#admin-tabs .admin-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tabBtn === tabId);
  });
  try {
    localStorage.setItem("fr_admin_tab", tabId);
  } catch {
    /* private mode */
  }
}

function mountTabs() {
  const bar = document.getElementById("admin-tabs");
  bar.innerHTML = ADMIN_TABS.map(
    ([id, icon, label]) => `
      <button type="button" class="admin-tab" data-tab-btn="${id}">
        <span>${icon}</span> ${label}
        ${id === "verify" ? '<span id="tab-verify-badge" class="tab-badge hidden">0</span>' : ""}
      </button>`
  ).join("");
  bar.querySelectorAll("[data-tab-btn]").forEach((button) =>
    button.addEventListener("click", () => switchTab(button.dataset.tabBtn))
  );
  let saved = "overview";
  try {
    saved = localStorage.getItem("fr_admin_tab") || "overview";
  } catch {
    /* private mode */
  }
  if (!ADMIN_TABS.some(([id]) => id === saved)) saved = "overview";
  switchTab(saved);
}

function setVerifyBadge(count) {
  const badge = document.getElementById("tab-verify-badge");
  if (!badge) return;
  badge.textContent = count;
  badge.classList.toggle("hidden", !count);
}

// ---------------------------------------------------------------------------
// Boot — dedicated admin sign-in gates the whole console
// ---------------------------------------------------------------------------

function bootDashboard() {
  document.getElementById("admin-login").classList.add("hidden");
  document.getElementById("admin-main").classList.remove("hidden");
  document.getElementById("btn-logout").classList.remove("hidden");
  document.getElementById("user-name").textContent = currentUser ? currentUser.name : "";
  const backLink = document.getElementById("btn-back");
  if (currentUser && ROLE_HOME[currentUser.role]) {
    backLink.href = ROLE_HOME[currentUser.role];
    backLink.classList.remove("hidden");
  }
  mountTabs();
  loadTiles();
  loadZoneMap();
  loadTrend();
  loadForecastBand();
  loadForecast();
  loadAccuracy();
  loadHeatmap();
  loadAudit();
  loadSurge();
  loadUsers();
  loadRatingsFeed();
  loadVerifications();
  setInterval(loadZoneMap, 60000); // the map is "live" — refresh every minute
}

window.addEventListener("fr:refresh", () => {
  loadTiles();
  loadSurge();
  loadUsers();
  loadRatingsFeed();
  loadVerifications();
});

document.getElementById("admin-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorBox = document.getElementById("admin-login-error");
  errorBox.classList.add("hidden");
  try {
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: document.getElementById("admin-email").value.trim(),
        password: document.getElementById("admin-password").value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Sign-in failed (${response.status})`);

    // Only genuine administrators may enter the command center.
    const probe = await fetch(`${API_BASE}/admin/overview`, {
      headers: { Authorization: `Bearer ${data.token}` },
    });
    if (!probe.ok) throw new Error("That account is not an administrator.");

    localStorage.setItem("fr_token", data.token);
    localStorage.setItem("fr_user", JSON.stringify(data.user));
    currentUser = data.user;
    frUI.toast(`Welcome back, ${data.user.name} 🛡️`, "success");
    bootDashboard();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
  }
});

(async function initAdmin() {
  if (getToken()) {
    try {
      await api("/admin/overview"); // works for admins (and legacy allowlisted accounts)
      bootDashboard();
      return;
    } catch {
      /* not an admin — fall through to the sign-in screen */
    }
  }
  showLoginScreen();
})();
