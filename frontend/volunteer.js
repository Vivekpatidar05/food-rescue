// volunteer.js — volunteer app: claim routes, deliver, earn trust.

// Same-origin in production (served behind nginx); localhost API in dev.
const API_BASE =
  location.protocol === "file:" ||
  location.hostname === "localhost" ||
  location.hostname === "127.0.0.1"
    ? "http://localhost:5000"
    : "";
const POLL_MS = 15000;
const FALLBACK_SPEED_KMH = 25; // matches backend fallback estimate

// ---------------------------------------------------------------------------
// Auth guard & shared helpers
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
  window.location.href = "auth.html";
}

const currentUser = getStoredUser();
if (!getToken() || !currentUser || currentUser.role !== "volunteer") {
  logout();
  // logout() only schedules the redirect — halt so the rest of the script
  // never dereferences a null currentUser.
  throw new Error("redirecting to sign-in");
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  headers["Authorization"] = `Bearer ${getToken()}`;
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (response.status === 401) logout();
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

function showToast(message, kind) {
  frUI.toast(message, kind || "info");
}

// ---------------------------------------------------------------------------
// Nav / profile / availability toggle
// ---------------------------------------------------------------------------

document.getElementById("vol-name").textContent = currentUser.name;
document.getElementById("btn-logout").addEventListener("click", logout);

const toggle = document.getElementById("availability-toggle");
const statusDot = document.getElementById("status-dot");
const availabilityLabel = document.getElementById("availability-label");

toggle.checked = localStorage.getItem("fr_available") !== "false";

function applyAvailability() {
  const available = toggle.checked;
  localStorage.setItem("fr_available", String(available));
  statusDot.classList.toggle("off", !available);
  availabilityLabel.textContent = available ? "Available" : "Offline";
  if (available) refreshAll();
  else renderOffline();
}

toggle.addEventListener("change", applyAvailability);

async function refreshProfile() {
  try {
    const { user } = await api("/auth/me");
    localStorage.setItem("fr_user", JSON.stringify(user));
    document.getElementById("trust-badge").textContent = `🛡 ${user.trust_score}`;
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function routeStats(batch) {
  const pills = [];
  const distanceKm = batch.route_info
    ? batch.route_info.distance_km
    : batch.distance_meters != null
      ? (batch.distance_meters / 1000).toFixed(1)
      : null;
  const etaMin = batch.route_info
    ? batch.route_info.duration_min
    : distanceKm != null
      ? Math.max(1, Math.round((distanceKm / FALLBACK_SPEED_KMH) * 60))
      : null;
  if (distanceKm != null) pills.push(`<span class="pill">📍 ${escapeHtml(distanceKm)} km</span>`);
  if (etaMin != null) pills.push(`<span class="pill pill-eta">🕐 ~${escapeHtml(etaMin)} min drive</span>`);
  pills.push(`<span class="pill">${escapeHtml(batch.quantity_kg)} kg</span>`);
  if ((batch.dietary_tags || []).includes("vegetarian")) {
    pills.push('<span class="pill pill-veg">🌱 vegetarian</span>');
  }
  return pills.join("");
}

function routeLine(batch) {
  const dropoff = batch.receiver?.address || "NGO location (shared on claim)";
  return `
    <div class="route-line">
      <div class="route-stop">
        <span class="stop-label">Pickup</span>
        <span class="stop-address">${escapeHtml(batch.pickup_address)}</span>
      </div>
      <span class="route-arrow">➜</span>
      <div class="route-stop">
        <span class="stop-label">Drop-off${batch.receiver?.name ? ` · ${escapeHtml(batch.receiver.name)}` : ""}</span>
        <span class="stop-address">${escapeHtml(dropoff)}</span>
      </div>
    </div>`;
}

function availableRouteCard(batch) {
  return `
    <article class="route-card">
      <div class="route-head">
        <span class="route-title">${escapeHtml(batch.food_description)}</span>
        ${batch.cold_chain ? '<span class="fr-fresh expiring" title="Insulated bag needed">❄ cold</span>' : ""}
        <span class="reward-pill">+10 trust</span>
      </div>
      ${routeLine(batch)}
      <div class="route-stats">${routeStats(batch)}</div>
      <button class="btn-claim" data-claim="${escapeHtml(batch._id)}">⚡ Claim This Route</button>
    </article>`;
}

function activeDeliveryCard(batch) {
  return `
    <article class="route-card">
      <div class="route-head">
        <span class="route-title">${escapeHtml(batch.food_description)}</span>
        <span class="reward-pill">+10 on delivery</span>
      </div>
      ${routeLine(batch)}
      <div class="route-stats">${routeStats(batch)}</div>
      <p class="gps-note">📡 <span id="gps-status">Sharing your live position with the donor &amp; NGO…</span></p>
      <button class="btn-claim btn-complete" data-complete="${escapeHtml(batch._id)}">📸 Mark Delivered (photo proof)</button>
      <button class="btn-chat-vol" data-chat="${escapeHtml(batch._id)}" data-title="${escapeHtml(batch.food_description)}">💬 Chat with donor &amp; NGO</button>
      <button class="btn-cancel" data-cancel="${escapeHtml(batch._id)}">Cancel delivery (−15 trust)</button>
    </article>`;
}

function renderMap(batch) {
  const mapEl = document.getElementById("route-map");
  const route = batch.route_info
    ? `${batch.route_info.distance_km} km · ~${batch.route_info.duration_min} min` +
      (batch.route_info.status === "fallback" ? " (straight-line estimate)" : "")
    : "route pending…";
  mapEl.innerHTML = `🗺️ <strong>&nbsp;${escapeHtml(batch.pickup_address)}&nbsp;</strong> ➜
    <strong>&nbsp;${escapeHtml(batch.receiver?.address || "NGO")}&nbsp;</strong><br />
    ${escapeHtml(route)} — live map requires the Google Maps JS SDK`;
}

function renderOffline() {
  document.getElementById("routes-list").innerHTML =
    '<p class="empty-state">You\'re offline. Flip the toggle to see nearby routes. 💤</p>';
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

function bindActions(root) {
  root.querySelectorAll("[data-claim]").forEach((button) =>
    button.addEventListener("click", async () => {
      try {
        const { batch } = await api(`/volunteer/claim/${button.dataset.claim}`, {
          method: "POST",
        });
        const route = batch.route_info
          ? ` ${batch.route_info.distance_km} km to pickup.`
          : "";
        showToast(`Route claimed — you're on!${route} 🚗`);
      } catch (error) {
        showToast(error.status === 409 ? error.message : `Claim failed: ${error.message}`);
      }
      await refreshAll();
    })
  );
  root.querySelectorAll("[data-complete]").forEach((button) =>
    button.addEventListener("click", () => openProofModal(button.dataset.complete))
  );
  root.querySelectorAll("[data-chat]").forEach((button) =>
    button.addEventListener("click", () =>
      frUI.chat.open(button.dataset.chat, button.dataset.title)
    )
  );
  root.querySelectorAll("[data-cancel]").forEach((button) =>
    button.addEventListener("click", async () => {
      try {
        const data = await api(`/volunteer/cancel/${button.dataset.cancel}`, { method: "POST" });
        showToast(`Delivery cancelled. Trust ${data.trust_penalty}.`);
      } catch (error) {
        showToast(error.message);
      }
      await refreshAll();
    })
  );
}

// ---------------------------------------------------------------------------
// Proof-of-delivery photo capture
// ---------------------------------------------------------------------------

const proofModal = document.getElementById("proof-modal");
const proofInput = document.getElementById("proof-input");
const proofPreview = document.getElementById("proof-preview");
const pickupCodeInput = document.getElementById("pickup-code-input");
const confirmDeliveryBtn = document.getElementById("btn-confirm-delivery");

let proofBatchId = null;
let proofDataUri = null;

function openProofModal(batchId) {
  proofBatchId = batchId;
  proofDataUri = null;
  proofInput.value = "";
  pickupCodeInput.value = "";
  proofPreview.classList.add("hidden");
  confirmDeliveryBtn.disabled = true;
  proofModal.classList.remove("hidden");
}

function closeProofModal() {
  proofModal.classList.add("hidden");
  proofBatchId = null;
  proofDataUri = null;
}

// Downscale to ≤1280px JPEG so uploads stay small and fast on mobile data.
function compressImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Could not read the photo"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error("That file doesn't look like an image"));
      img.onload = () => {
        const scale = Math.min(1, 1280 / Math.max(img.width, img.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL("image/jpeg", 0.72));
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

document.getElementById("btn-take-photo").addEventListener("click", () => proofInput.click());

proofInput.addEventListener("change", async () => {
  const file = proofInput.files && proofInput.files[0];
  if (!file) return;
  try {
    proofDataUri = await compressImage(file);
    proofPreview.src = proofDataUri;
    proofPreview.classList.remove("hidden");
    confirmDeliveryBtn.disabled = false;
  } catch (error) {
    showToast(error.message);
  }
});

confirmDeliveryBtn.addEventListener("click", async () => {
  if (!proofBatchId || !proofDataUri) return;
  const pickupCode = pickupCodeInput.value.trim();
  if (!pickupCode) {
    showToast("Enter the donor's 6-digit pickup code — they see it on their dashboard.");
    return;
  }
  confirmDeliveryBtn.disabled = true;
  const completedBatchId = proofBatchId;
  const completedTitle =
    document.querySelector("#active-card .route-title")?.textContent || "";
  try {
    await api(`/volunteer/complete/${completedBatchId}`, {
      method: "POST",
      body: JSON.stringify({ proof_photo: proofDataUri, pickup_code: pickupCode }),
    });
    closeProofModal();
    showToast("Delivered & proof saved! +10 trust points. 💚", "party");
    frUI.confetti();
    frUI.chat.close();
    stopGpsPings();
    frUI.rate.open(
      { _id: completedBatchId, food_description: completedTitle },
      { subtitle: "How was this pickup & drop-off?" }
    );
  } catch (error) {
    confirmDeliveryBtn.disabled = false;
    showToast(error.message, "error");
  }
  await refreshAll();
});

document.getElementById("btn-close-proof").addEventListener("click", closeProofModal);

// ---------------------------------------------------------------------------
// Notifications bell
// ---------------------------------------------------------------------------

const bellBtn = document.getElementById("btn-bell");
const bellPanel = document.getElementById("bell-panel");
const bellCount = document.getElementById("bell-count");
let bellItems = [];

function timeAgo(isoDate) {
  const minutes = Math.floor((Date.now() - new Date(isoDate)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function renderBellPanel() {
  if (!bellItems.length) {
    bellPanel.innerHTML = '<p class="bell-item">No notifications yet.</p>';
    return;
  }
  bellPanel.innerHTML = bellItems
    .map(
      (n) => `
        <div class="bell-item ${n.read ? "" : "unread"}">
          ${escapeHtml(n.message)}
          <span class="bell-when">${escapeHtml(timeAgo(n.created_at))}</span>
        </div>`
    )
    .join("");
}

async function loadNotifications() {
  try {
    const { notifications } = await api("/auth/notifications");
    bellItems = notifications;
    const unread = notifications.filter((n) => !n.read).length;
    bellCount.textContent = unread;
    bellCount.classList.toggle("hidden", unread === 0);
    if (!bellPanel.classList.contains("hidden")) renderBellPanel();
  } catch {
    /* non-fatal */
  }
}

bellBtn.addEventListener("click", async () => {
  const opening = bellPanel.classList.contains("hidden");
  bellPanel.classList.toggle("hidden");
  if (opening) {
    renderBellPanel();
    if (bellItems.some((n) => !n.read)) {
      try {
        await api("/auth/notifications/read", { method: "POST" });
        loadNotifications();
      } catch {
        /* non-fatal */
      }
    }
  }
});

// ---------------------------------------------------------------------------
// Leaderboard
// ---------------------------------------------------------------------------

async function loadLeaderboard() {
  const list = document.getElementById("leaderboard");
  try {
    const { top_volunteers } = await api("/stats/leaderboard");
    if (!top_volunteers.length) {
      list.innerHTML = '<li class="empty-state">No rescuers ranked yet — be the first!</li>';
      return;
    }
    list.innerHTML = top_volunteers
      .map((v) => {
        const stars = v.rating
          ? ` · <span class="fr-stars-inline" title="${escapeHtml(v.rating)}/5 community rating">${"★".repeat(Math.round(v.rating))}</span>`
          : "";
        return `
          <li>
            <span class="leader-name">${escapeHtml(v.name)}</span>
            <span class="leader-stat">🛡 ${escapeHtml(v.trust_score)} · ${escapeHtml(v.deliveries)} deliveries${stars}</span>
          </li>`;
      })
      .join("");
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Achievements
// ---------------------------------------------------------------------------

async function loadAchievements() {
  try {
    const data = await api("/gamification/achievements");
    const level = data.level;
    const span = level.xp + level.xp_to_next;
    document.getElementById("level-line").textContent =
      `Level ${level.number} — ${level.name} · ${level.xp} XP` +
      (level.next_level ? ` (${level.xp_to_next} XP to ${level.next_level})` : " (max level!)");
    document.getElementById("xp-fill").style.width =
      level.next_level && span > 0 ? `${Math.round((level.xp / span) * 100)}%` : "100%";
    const streak = data.stats.streak_days;
    document.getElementById("streak-chip").textContent =
      streak > 0 ? `🔥 ${streak}-day streak` : "";
    document.getElementById("badge-grid").innerHTML = data.badges
      .map((badge) => {
        const pct = Math.round(badge.progress * 100);
        return `
          <div class="badge-chip ${badge.earned ? "" : "locked"}" title="${escapeHtml(badge.label)}">
            <span class="badge-emoji">${badge.emoji}</span>
            <span>${escapeHtml(badge.label)}
              <span class="badge-progress">${badge.earned ? "Earned ✓" : `${pct}%`}</span>
            </span>
          </div>`;
      })
      .join("");
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadActive() {
  const section = document.getElementById("active-section");
  const container = document.getElementById("active-card");
  try {
    const { batch, batches } = await api("/volunteer/active-batch");
    const actives = batches && batches.length ? batches : batch ? [batch] : [];
    if (actives.length) {
      container.innerHTML =
        actives.map(activeDeliveryCard).join("") + '<div id="addon-slot"></div>';
      bindActions(container);
      renderMap(actives[0]);
      section.classList.remove("hidden");
      startGpsPings(actives[0]._id);
      loadAddons(actives.length);
    } else {
      section.classList.add("hidden");
      stopGpsPings();
    }
  } catch {
    section.classList.add("hidden");
  }
}

// Multi-stop trips: one extra rescue on the same route (2 batches max).
async function loadAddons(activeCount) {
  const slot = document.getElementById("addon-slot");
  if (!slot) return;
  if (activeCount >= 2) {
    slot.innerHTML = '<p class="gps-note">🧺 Trip is full — two rescues on board!</p>';
    return;
  }
  try {
    const { addons } = await api("/volunteer/route-addons");
    if (!addons || !addons.length) {
      slot.innerHTML = "";
      return;
    }
    slot.innerHTML =
      `<div class="addon-box">
        <p class="addon-title">🧭 On your way — grab a second rescue for barely any detour:</p>` +
      addons
        .map(
          (a) => `
          <div class="addon-row">
            <span>${escapeHtml(a.food_description)} · ${escapeHtml(a.quantity_kg)} kg
              · +${escapeHtml(a.pickup_detour_km)} km detour
              ${a.same_receiver ? " · 🎯 same NGO" : ""}${a.cold_chain ? " · ❄" : ""}</span>
            <button class="fr-btn-mini cool" data-claim="${escapeHtml(a._id)}">➕ Add to trip</button>
          </div>`
        )
        .join("") +
      `</div>`;
    bindActions(slot);
  } catch {
    slot.innerHTML = "";
  }
}

// ---------------------------------------------------------------------------
// Live GPS pings — while a delivery is active, share position every 20s so
// the donor and NGO watch the courier approach in real time.
// ---------------------------------------------------------------------------

const GPS_PING_MS = 20000;
let gpsTimer = null;
let gpsBatchId = null;

function sendGpsPing() {
  if (!gpsBatchId || !navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    async (position) => {
      try {
        await api(`/social/track/${gpsBatchId}`, {
          method: "POST",
          body: JSON.stringify({
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
          }),
        });
        const status = document.getElementById("gps-status");
        if (status) status.textContent = `Live position shared ${new Date().toLocaleTimeString()} 🟢`;
      } catch {
        /* completed/cancelled mid-ping — the next loadActive stops us */
      }
    },
    () => {
      const status = document.getElementById("gps-status");
      if (status) status.textContent = "Location blocked — enable it so the NGO can track you.";
    },
    { enableHighAccuracy: true, timeout: 12000 }
  );
}

function startGpsPings(batchId) {
  if (gpsBatchId === batchId) return;
  stopGpsPings();
  gpsBatchId = batchId;
  sendGpsPing();
  gpsTimer = setInterval(sendGpsPing, GPS_PING_MS);
}

function stopGpsPings() {
  if (gpsTimer) clearInterval(gpsTimer);
  gpsTimer = null;
  gpsBatchId = null;
}

// ---------------------------------------------------------------------------
// Weekly availability shifts
// ---------------------------------------------------------------------------

const SHIFT_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const SHIFT_PERIODS = [
  ["morning", "🌅 Morning"],
  ["afternoon", "☀️ Afternoon"],
  ["evening", "🌇 Evening"],
];
let shiftSlots = new Set();

function renderShiftGrid() {
  const grid = document.getElementById("avail-grid");
  let html = '<span></span>' + SHIFT_DAYS.map((d) => `<span class="head">${d}</span>`).join("");
  SHIFT_PERIODS.forEach(([period, label]) => {
    html += `<span class="rowlab">${label}</span>`;
    for (let day = 0; day < 7; day++) {
      const key = `${day}-${period}`;
      html += `<button type="button" class="fr-avail-cell ${shiftSlots.has(key) ? "on" : ""}"
        data-slot="${key}" aria-label="${SHIFT_DAYS[day]} ${period}"></button>`;
    }
  });
  grid.innerHTML = html;
  grid.querySelectorAll("[data-slot]").forEach((cell) =>
    cell.addEventListener("click", () => {
      const key = cell.dataset.slot;
      if (shiftSlots.has(key)) shiftSlots.delete(key);
      else shiftSlots.add(key);
      cell.classList.toggle("on");
    })
  );
}

async function loadShifts() {
  try {
    const data = await api("/volunteer/availability");
    shiftSlots = new Set(data.slots || []);
  } catch {
    shiftSlots = new Set();
  }
  renderShiftGrid();
}

document.getElementById("btn-save-shifts").addEventListener("click", async () => {
  try {
    await api("/volunteer/availability", {
      method: "POST",
      body: JSON.stringify({
        slots: [...shiftSlots],
        tz_offset_min: -new Date().getTimezoneOffset(),
      }),
    });
    showToast(`Shifts saved — you're prioritised in ${shiftSlots.size} weekly slot${shiftSlots.size === 1 ? "" : "s"} 📅`, "success");
  } catch (error) {
    showToast(error.message, "error");
  }
});

async function loadRoutes() {
  if (!toggle.checked) {
    renderOffline();
    return;
  }
  const list = document.getElementById("routes-list");
  try {
    const { batches } = await api("/volunteer/available-batches");
    if (!batches.length) {
      list.innerHTML = '<p class="empty-state">No deliveries needed nearby right now — stay ready!</p>';
      return;
    }
    list.innerHTML = batches.map(availableRouteCard).join("");
    bindActions(list);
  } catch (error) {
    list.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

async function refreshAll() {
  await Promise.all([
    loadActive(),
    loadRoutes(),
    refreshProfile(),
    loadNotifications(),
    loadLeaderboard(),
    loadAchievements(),
  ]);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

window.addEventListener("fr:refresh", () => {
  refreshAll();
  loadShifts();
});

applyAvailability();
loadShifts();
setInterval(() => {
  if (toggle.checked) refreshAll();
}, POLL_MS);
