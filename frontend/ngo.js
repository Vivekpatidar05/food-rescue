// ngo.js — NGO console: claim batches atomically, request volunteers, track trust.

const API_BASE = "http://localhost:5000";
const POLL_MS = 20000;

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
if (!getToken() || !currentUser || currentUser.role !== "ngo") {
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

// Freshness badge from the food-safety window (4-hour rule).
function freshnessBadge(batch) {
  if (!batch.safe_until) return "";
  const minutes = Math.floor((new Date(batch.safe_until) - Date.now()) / 60000);
  if (minutes <= 0) return '<span class="fr-fresh critical">☣ past safe window</span>';
  if (minutes <= 45) return `<span class="fr-fresh critical">🔥 safe ${minutes}m more</span>`;
  if (minutes <= 120) return `<span class="fr-fresh expiring">⏱ safe ${Math.floor(minutes / 60)}h ${minutes % 60}m</span>`;
  return '<span class="fr-fresh fresh">🌿 fresh &amp; safe</span>';
}

function timeAgo(isoDate) {
  const minutes = Math.floor((Date.now() - new Date(isoDate)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// ---------------------------------------------------------------------------
// Nav / profile
// ---------------------------------------------------------------------------

document.getElementById("org-name").textContent = currentUser.name;
document.getElementById("btn-logout").addEventListener("click", logout);

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
// Live countdown (single 1-second ticker for every card on screen)
// ---------------------------------------------------------------------------

function renderCountdown(element) {
  const expires = new Date(element.dataset.expires);
  const ms = expires - Date.now();
  if (ms <= 0) {
    element.textContent = "⏳ expired";
    element.classList.remove("safe");
    return;
  }
  const totalSeconds = Math.floor(ms / 1000);
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  element.textContent = `⏳ ${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  element.classList.toggle("safe", ms > 45 * 60000);
}

setInterval(() => {
  document.querySelectorAll("[data-expires]").forEach(renderCountdown);
}, 1000);

// ---------------------------------------------------------------------------
// Card rendering
// ---------------------------------------------------------------------------

function dietaryPills(tags) {
  return (tags || [])
    .map((tag) => {
      const cls = tag === "vegetarian" ? "pill pill-veg" : "pill";
      const icon = tag === "vegetarian" ? "🌱 " : "";
      return `<span class="${cls}">${icon}${escapeHtml(tag)}</span>`;
    })
    .join("");
}

function availableCard(batch) {
  const distanceKm = batch.distance_meters != null
    ? (batch.distance_meters / 1000).toFixed(1)
    : "?";
  const donorName = batch.donor?.name || "Anonymous donor";
  const donorTrust = batch.donor?.trust_score ?? "—";
  return `
    <article class="batch-card">
      <div class="batch-head">
        <span class="batch-title">${escapeHtml(batch.food_description)}</span>
        <span class="countdown" data-expires="${escapeHtml(batch.expires_at)}"></span>
      </div>
      <div class="pill-row">
        <span class="pill pill-distance">📍 ${escapeHtml(distanceKm)} km away</span>
        <span class="pill">${escapeHtml(batch.quantity_kg)} kg</span>
        ${dietaryPills(batch.dietary_tags)}
        <span class="pill pill-trust">🛡 Donor trust ${escapeHtml(donorTrust)}</span>
        ${freshnessBadge(batch)}
        ${batch.cold_chain ? '<span class="fr-fresh expiring" title="Requires cold storage — 2h window">❄ cold chain</span>' : ""}
      </div>
      <p class="batch-meta">From ${escapeHtml(donorName)} · ${escapeHtml(batch.pickup_address)}</p>
      <div class="accept-row">
        <button class="btn-accept btn-pickup" data-accept="${escapeHtml(batch._id)}">
          ✓ We'll Pick It Up
        </button>
        <button class="btn-accept btn-volunteer" data-request="${escapeHtml(batch._id)}">
          🚗 Request Volunteer Delivery
        </button>
      </div>
    </article>`;
}

function activeCard(batch) {
  const statusNotes = {
    ngo_pickup: "Awaiting your pickup — confirm within 2 hours of accepting.",
    volunteer_needed: "Waiting for a volunteer to claim the route…",
    in_transit: "A volunteer is on the way with your food. 🚗",
    donor_delivering: "The donor is delivering this batch to you directly. 🎉",
  };
  const route = batch.route_info
    ? `<p class="batch-meta">🚗 Route: ${escapeHtml(batch.route_info.distance_km)} km · ~${escapeHtml(batch.route_info.duration_min)} min</p>`
    : "";
  const trackSlot = batch.status === "in_transit"
    ? `<div class="active-track" data-track="${escapeHtml(batch._id)}"></div>`
    : "";
  const actions = [];
  if (batch.status === "ngo_pickup") {
    actions.push(
      `<button class="btn-accept btn-pickup" data-confirm="${escapeHtml(batch._id)}">✓ Confirm Pickup Complete</button>`,
      `<button class="btn-accept btn-volunteer" data-request="${escapeHtml(batch._id)}">🚗 Request Volunteer Instead</button>`
    );
  }
  actions.push(
    `<button class="btn-accept btn-chat" data-chat="${escapeHtml(batch._id)}" data-title="${escapeHtml(batch.food_description)}">💬 Chat with donor${batch.status === "in_transit" ? " & courier" : ""}</button>`
  );
  if (["ngo_pickup", "volunteer_needed"].includes(batch.status)) {
    actions.push(
      `<button class="btn-accept btn-danger" data-cancel="${escapeHtml(batch._id)}">✗ Cancel (−15 trust)</button>`
    );
  }
  return `
    <article class="batch-card status-${escapeHtml(batch.status)}">
      <div class="batch-head">
        <span class="batch-title">${escapeHtml(batch.food_description)}</span>
        <span class="countdown" data-expires="${escapeHtml(batch.expires_at)}"></span>
      </div>
      <div class="pill-row">
        <span class="pill">${escapeHtml(batch.quantity_kg)} kg</span>
        ${dietaryPills(batch.dietary_tags)}
        ${freshnessBadge(batch)}
        ${batch.cold_chain ? '<span class="fr-fresh expiring" title="Requires cold storage — 2h window">❄ cold chain</span>' : ""}
      </div>
      <p class="batch-meta">
        ${escapeHtml(statusNotes[batch.status] || batch.status)}<br />
        Pickup: ${escapeHtml(batch.pickup_address)}
        ${batch.donor ? ` · Donor: ${escapeHtml(batch.donor.name)}` : ""}
      </p>
      ${route}
      ${trackSlot}
      ${actions.length ? `<div class="accept-row">${actions.join("")}</div>` : ""}
    </article>`;
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function acceptBatch(batchId, thenRequestVolunteer) {
  try {
    await api(`/ngo/accept/${batchId}`, { method: "POST" });
    if (thenRequestVolunteer) {
      await api(`/ngo/request-volunteer/${batchId}`, { method: "POST" });
      showToast("Batch secured — searching for a volunteer driver. 🚗");
    } else {
      showToast("Batch is yours! Confirm pickup within 2 hours.");
    }
  } catch (error) {
    if (error.status === 409) {
      showToast(error.message.includes("active")
        ? error.message
        : "Another NGO just claimed this batch. 😔");
    } else {
      showToast(error.message);
    }
  }
  await refreshAll();
}

function bindCardActions(root) {
  root.querySelectorAll("[data-accept]").forEach((button) =>
    button.addEventListener("click", () => acceptBatch(button.dataset.accept, false))
  );
  root.querySelectorAll("[data-request]").forEach((button) =>
    button.addEventListener("click", () => acceptOrRequest(button.dataset.request))
  );
  root.querySelectorAll("[data-chat]").forEach((button) =>
    button.addEventListener("click", () =>
      frUI.chat.open(button.dataset.chat, button.dataset.title)
    )
  );
  root.querySelectorAll("[data-track]").forEach((slot) =>
    frUI.trackBar(slot, slot.dataset.track)
  );
  root.querySelectorAll("[data-confirm]").forEach((button) =>
    button.addEventListener("click", async () => {
      // Handoff OTP: the donor shows this code at the pickup point.
      const code = window.prompt("Enter the donor's 6-digit pickup code (shown on their dashboard):");
      if (code === null) return; // cancelled
      const batchId = button.dataset.confirm;
      const title = button.closest(".batch-card")?.querySelector(".batch-title")?.textContent || "";
      try {
        await api(`/ngo/confirm-pickup/${batchId}`, {
          method: "POST",
          body: JSON.stringify({ pickup_code: code.trim() }),
        });
        showToast("Pickup confirmed — rescue complete! 🎉", "party");
        frUI.confetti();
        frUI.chat.close();
        frUI.rate.open(
          { _id: batchId, food_description: title },
          { subtitle: "How was working with this donor?" }
        );
      } catch (error) {
        showToast(error.message, "error");
      }
      await refreshAll();
    })
  );
  root.querySelectorAll("[data-cancel]").forEach((button) =>
    button.addEventListener("click", async () => {
      try {
        const data = await api(`/ngo/cancel/${button.dataset.cancel}`, { method: "POST" });
        showToast(`Batch released. Trust ${data.trust_penalty}.`);
      } catch (error) {
        showToast(error.message);
      }
      await refreshAll();
    })
  );
}

async function acceptOrRequest(batchId) {
  // If we already own the batch, jump straight to requesting a volunteer.
  try {
    await api(`/ngo/request-volunteer/${batchId}`, { method: "POST" });
    showToast("Searching for a volunteer driver. 🚗");
    await refreshAll();
    return;
  } catch {
    /* not ours yet — fall through to accept-then-request */
  }
  await acceptBatch(batchId, true);
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadActive() {
  const section = document.getElementById("active-section");
  const container = document.getElementById("active-card");
  try {
    const { batch } = await api("/ngo/active-batch");
    if (batch) {
      container.innerHTML = activeCard(batch);
      bindCardActions(container);
      section.classList.remove("hidden");
    } else {
      section.classList.add("hidden");
    }
  } catch {
    section.classList.add("hidden");
  }
}

async function loadAvailable() {
  const list = document.getElementById("available-list");
  try {
    const { batches } = await api("/ngo/available-batches");
    if (!batches.length) {
      list.innerHTML = '<p class="empty-state">No pending batches nearby right now — check back soon.</p>';
      return;
    }
    list.innerHTML = batches.map(availableCard).join("");
    bindCardActions(list);
    list.querySelectorAll("[data-expires]").forEach(renderCountdown);
  } catch (error) {
    list.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

async function loadTrustHistory() {
  const log = document.getElementById("trust-log");
  try {
    const { history } = await api("/ngo/trust-history");
    if (!history.length) {
      log.innerHTML = '<li class="empty-state">No trust events yet.</li>';
      return;
    }
    log.innerHTML = history
      .map((entry) => {
        const sign = entry.delta > 0 ? "+" : "";
        const cls = entry.delta >= 0 ? "positive" : "negative";
        return `
          <li>
            <span><span class="trust-delta ${cls}">${sign}${escapeHtml(entry.delta)}</span>
            ${escapeHtml(entry.reason)}</span>
            <span class="trust-when">${escapeHtml(timeAgo(entry.created_at))}</span>
          </li>`;
      })
      .join("");
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Notifications bell
// ---------------------------------------------------------------------------

const bellBtn = document.getElementById("btn-bell");
const bellPanel = document.getElementById("bell-panel");
const bellCount = document.getElementById("bell-count");
let bellItems = [];

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
// Watch zones — subscribe to city zones for out-of-radius alerts
// ---------------------------------------------------------------------------

let watchState = { zones: [], watching: new Set() };

function renderZoneChips() {
  const box = document.getElementById("zone-chips");
  box.innerHTML = watchState.zones
    .map((zone) => {
      const on = watchState.watching.has(zone);
      return `<button type="button" class="fr-chip ${on ? "on" : ""}" data-zone="${escapeHtml(zone)}">
        ${on ? "👁" : "◯"} ${escapeHtml(zone)}</button>`;
    })
    .join("");
  box.querySelectorAll("[data-zone]").forEach((chip) =>
    chip.addEventListener("click", async () => {
      const zone = chip.dataset.zone;
      if (watchState.watching.has(zone)) watchState.watching.delete(zone);
      else watchState.watching.add(zone);
      renderZoneChips();
      try {
        await api("/ngo/watch-zones", {
          method: "POST",
          body: JSON.stringify({ zones: [...watchState.watching] }),
        });
        showToast(
          watchState.watching.has(zone)
            ? `Watching ${zone} — you'll hear about every batch there 👁`
            : `Stopped watching ${zone}`,
          "success"
        );
      } catch (error) {
        showToast(error.message, "error");
      }
    })
  );
}

async function loadWatchZones() {
  try {
    const data = await api("/ngo/watch-zones");
    watchState = { zones: data.zones, watching: new Set(data.watching) };
    renderZoneChips();
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Cold-storage capability — cold-chain (dairy) batches only match NGOs
// that declared refrigeration.
// ---------------------------------------------------------------------------

function mountColdStorageToggle() {
  const host = document.getElementById("zone-chips");
  if (!host || document.getElementById("cold-storage-line")) return;
  const me = getStoredUser() || {};
  const line = document.createElement("label");
  line.id = "cold-storage-line";
  line.className = "cold-storage-line";
  line.innerHTML = `<input type="checkbox" id="cold-storage-toggle" ${me.has_cold_storage ? "checked" : ""} />
    ❄ We have cold storage <small>(unlocks dairy &amp; refrigeration-dependent batches)</small>`;
  host.insertAdjacentElement("afterend", line);
  line.querySelector("input").addEventListener("change", async (event) => {
    try {
      const data = await api("/ngo/cold-storage", {
        method: "POST",
        body: JSON.stringify({ enabled: event.target.checked }),
      });
      const stored = getStoredUser() || {};
      stored.has_cold_storage = data.has_cold_storage;
      localStorage.setItem("fr_user", JSON.stringify(stored));
      showToast(
        data.has_cold_storage
          ? "❄ Cold storage ON — dairy batches will now reach you"
          : "Cold storage off — cold-chain batches hidden",
        "success"
      );
      loadAvailable();
    } catch (error) {
      showToast(error.message, "error");
      event.target.checked = !event.target.checked;
    }
  });
}

async function refreshAll() {
  await Promise.all([
    loadActive(),
    loadAvailable(),
    loadTrustHistory(),
    refreshProfile(),
    loadNotifications(),
  ]);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

window.addEventListener("fr:refresh", () => {
  refreshAll();
  loadWatchZones();
});

refreshAll();
loadWatchZones();
mountColdStorageToggle();
frUI.mountAchievements(document.getElementById("achievements-card"));
window.addEventListener("fr:refresh", () => {
  frUI.mountAchievements(document.getElementById("achievements-card"));
});
setInterval(refreshAll, POLL_MS);
