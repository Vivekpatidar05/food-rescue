// donor.js — donor dashboard: broadcast batches, track status, self-deliver.

// Same-origin in production (served behind nginx); localhost API in dev.
const API_BASE =
  location.protocol === "file:" ||
  location.hostname === "localhost" ||
  location.hostname === "127.0.0.1"
    ? "http://localhost:5000"
    : "";
const POLL_MS = 30000;

const STATUS_LABELS = {
  pending: "Pending",
  ngo_pickup: "NGO Pickup",
  volunteer_needed: "Volunteer Needed",
  in_transit: "In Transit",
  donor_delivering: "You're Delivering",
  completed: "Completed",
  expired: "Expired",
  cancelled: "Cancelled",
};

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
if (!getToken() || !currentUser || currentUser.role !== "donor") {
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

function timeLeft(isoDate) {
  const ms = new Date(isoDate) - Date.now();
  if (ms <= 0) return "expiring…";
  const minutes = Math.floor(ms / 60000);
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m left`;
}

// ---------------------------------------------------------------------------
// Nav / profile
// ---------------------------------------------------------------------------

document.getElementById("user-name").textContent = currentUser.name;
document.getElementById("btn-logout").addEventListener("click", logout);

async function refreshProfile() {
  try {
    const { user } = await api("/auth/me");
    localStorage.setItem("fr_user", JSON.stringify(user));
    document.getElementById("trust-badge").textContent = `🛡 ${user.trust_score}`;
  } catch {
    /* non-fatal — badge keeps its last value */
  }
}

// ---------------------------------------------------------------------------
// Broadcast form
// ---------------------------------------------------------------------------

const broadcastSection = document.getElementById("broadcast-section");

document.getElementById("btn-toggle-form").addEventListener("click", () => {
  broadcastSection.classList.toggle("hidden");
  if (!broadcastSection.classList.contains("hidden")) {
    broadcastSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

function fillLocation() {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (position) => {
      document.getElementById("lat").value = position.coords.latitude.toFixed(6);
      document.getElementById("lng").value = position.coords.longitude.toFixed(6);
    },
    () => showToast("Could not read your location — allow location access."),
    { enableHighAccuracy: true, timeout: 10000 }
  );
}

document.getElementById("btn-locate").addEventListener("click", fillLocation);

document.getElementById("broadcast-form").addEventListener("submit", async (event) => {
  event.preventDefault();

  const latitude = document.getElementById("lat").value;
  const longitude = document.getElementById("lng").value;
  if (!latitude || !longitude) {
    showToast('Click "Use my location" first so NGOs can find you.');
    return;
  }

  // Vegetarian is locked-in; extra tags are additive.
  const dietaryTags = ["vegetarian"];
  document
    .querySelectorAll('input[name="dietary"]:checked')
    .forEach((checkbox) => dietaryTags.push(checkbox.value));

  try {
    await api("/donor/broadcast", {
      method: "POST",
      body: JSON.stringify({
        food_description: document.getElementById("food-description").value.trim(),
        quantity_kg: document.getElementById("quantity-kg").value,
        dietary_tags: dietaryTags,
        address: document.getElementById("pickup-address").value.trim(),
        prepared_hours_ago: document.getElementById("prepared-ago").value || null,
        cold_chain: document.getElementById("cold-chain").checked || null,
        latitude,
        longitude,
      }),
    });
    event.target.reset();
    broadcastSection.classList.add("hidden");
    showToast("Broadcast sent! Matching nearby NGOs now… 🌍", "success");
    loadBatches();
  } catch (error) {
    showToast(error.message, "error");
  }
});

// ---------------------------------------------------------------------------
// Batch list + self-delivery modal
// ---------------------------------------------------------------------------

const modal = document.getElementById("self-deliver-modal");
let modalBatchId = null;
const dismissedPrompts = new Set(
  JSON.parse(sessionStorage.getItem("fr_dismissed_prompts") || "[]")
);

function rememberDismissal(batchId) {
  dismissedPrompts.add(batchId);
  sessionStorage.setItem("fr_dismissed_prompts", JSON.stringify([...dismissedPrompts]));
}

function openSelfDeliverModal(batch) {
  modalBatchId = batch._id;
  document.getElementById("modal-batch-info").textContent =
    `"${batch.food_description}" (${batch.quantity_kg} kg) — ${timeLeft(batch.expires_at)}`;
  modal.classList.remove("hidden");
}

function closeModal() {
  modal.classList.add("hidden");
  modalBatchId = null;
}

document.getElementById("btn-self-deliver").addEventListener("click", async () => {
  if (!modalBatchId) return;
  const batchId = modalBatchId;
  closeModal();
  try {
    const data = await api(`/donor/self-deliver/${batchId}`, { method: "POST" });
    const route = data.batch.route_info;
    const routeNote = route
      ? ` Route: ${route.distance_km} km, ~${route.duration_min} min.`
      : "";
    showToast(`+50 trust points! You're the hero of this rescue. 🦸${routeNote}`);
    refreshProfile();
    loadBatches();
  } catch (error) {
    showToast(error.message);
  }
});

document.getElementById("btn-let-expire").addEventListener("click", () => {
  if (modalBatchId) rememberDismissal(modalBatchId);
  closeModal();
});

// Freshness badge from the food-safety window (4-hour rule).
function freshnessBadge(batch) {
  if (!batch.safe_until || ["completed", "expired", "cancelled"].includes(batch.status)) return "";
  const minutes = Math.floor((new Date(batch.safe_until) - Date.now()) / 60000);
  if (minutes <= 0) return '<span class="fr-fresh critical">☣ past safe window</span>';
  if (minutes <= 45) return `<span class="fr-fresh critical">🔥 safe ${minutes}m more</span>`;
  if (minutes <= 120) return `<span class="fr-fresh expiring">⏱ safe ${Math.floor(minutes / 60)}h ${minutes % 60}m</span>`;
  return '<span class="fr-fresh fresh">🌿 fresh &amp; safe</span>';
}

const CHATTABLE = ["ngo_pickup", "volunteer_needed", "in_transit", "donor_delivering"];

function renderBatches(batches) {
  const list = document.getElementById("batches-list");
  if (!batches.length) {
    list.innerHTML = '<p class="empty-state">No broadcasts yet — share your first surplus batch above.</p>';
    return;
  }

  list.innerHTML = batches
    .map((batch) => {
      const status = escapeHtml(batch.status);
      const label = STATUS_LABELS[batch.status] || batch.status;
      const tags = (batch.dietary_tags || []).map(escapeHtml).join(", ");
      const active = !["completed", "expired", "cancelled"].includes(batch.status);
      const route = batch.route_info
        ? `<span class="batch-route">🚗 ${escapeHtml(batch.route_info.distance_km)} km · ~${escapeHtml(batch.route_info.duration_min)} min</span>`
        : "";
      // Handoff OTP: the collector must read this code off the donor in
      // person before they can mark the rescue complete.
      const pickupCode = active && batch.pickup_code
        ? `<span class="batch-route pickup-code">🔑 Pickup code: <b>${escapeHtml(batch.pickup_code)}</b>
             <button class="fr-btn-mini brand" data-handoff="${escapeHtml(batch._id)}">🪪 Handoff card</button></span>`
        : "";
      const coldChip = batch.cold_chain
        ? '<span class="fr-fresh expiring" title="Only cold-storage NGOs are matched — 2h window">❄ cold chain</span>'
        : "";
      const trackSlot = batch.status === "in_transit"
        ? `<div class="batch-track" data-track="${escapeHtml(batch._id)}"></div>`
        : "";
      const actions = [];
      if (batch.status === "donor_delivering") {
        actions.push(`<button class="btn-small" data-complete="${escapeHtml(batch._id)}">✓ Mark Delivered</button>`);
      }
      if (CHATTABLE.includes(batch.status)) {
        actions.push(`<button class="fr-btn-mini brand" data-chat="${escapeHtml(batch._id)}" data-title="${escapeHtml(batch.food_description)}">💬 Chat</button>`);
      }
      if (batch.status === "completed" && batch.has_proof) {
        actions.push(`<button class="btn-small" data-proof="${escapeHtml(batch._id)}">📸 View Delivery Proof</button>`);
      }
      if (batch.status === "completed" && !frUI.rate.hasRated(batch._id)) {
        actions.push(`<button class="fr-btn-mini cta" data-rate="${escapeHtml(batch._id)}">⭐ Rate rescue</button>`);
      }
      if (["completed", "expired", "cancelled"].includes(batch.status)) {
        actions.push(`<button class="fr-btn-mini" data-again="${escapeHtml(batch._id)}">🔁 Donate again</button>`);
      }
      if (batch.status === "pending") {
        actions.push(`<button class="fr-btn-mini danger" data-cancel="${escapeHtml(batch._id)}">✕ Cancel broadcast</button>`);
      }
      const completeBtn = actions.length
        ? `<div class="batch-actions">${actions.join("")}</div>`
        : "";
      return `
        <article class="batch-card">
          <span class="batch-title">${escapeHtml(batch.food_description)}</span>
          <span class="badge badge-${status}">${escapeHtml(label)}</span>
          <span class="batch-meta">
            ${escapeHtml(batch.quantity_kg)} kg · ${tags} · ${escapeHtml(batch.pickup_address)}
            ${active ? ` · ⏳ ${escapeHtml(timeLeft(batch.expires_at))}` : ""}
            ${freshnessBadge(batch)}
            ${coldChip}
          </span>
          ${route}
          ${pickupCode}
          ${trackSlot}
          ${completeBtn}
        </article>`;
    })
    .join("");

  list.querySelectorAll("[data-track]").forEach((slot) => {
    frUI.trackBar(slot, slot.dataset.track);
  });

  list.querySelectorAll("[data-handoff]").forEach((button) => {
    button.addEventListener("click", () => {
      const batch = batches.find((b) => b._id === button.dataset.handoff);
      if (batch) frUI.handoffCard(batch);
    });
  });

  list.querySelectorAll("[data-chat]").forEach((button) => {
    button.addEventListener("click", () =>
      frUI.chat.open(button.dataset.chat, button.dataset.title)
    );
  });

  list.querySelectorAll("[data-rate]").forEach((button) => {
    button.addEventListener("click", () => {
      const batch = batches.find((b) => b._id === button.dataset.rate);
      if (batch) frUI.rate.open(batch, { onDone: loadBatches });
    });
  });

  list.querySelectorAll("[data-again]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api(`/donor/rebroadcast/${button.dataset.again}`, { method: "POST" });
        showToast("Broadcast is live again — matching NGOs now! 🌍", "success");
        loadBatches();
      } catch (error) {
        showToast(error.message, "error");
      }
    });
  });

  list.querySelectorAll("[data-cancel]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm("Cancel this broadcast? NGOs will stop seeing it immediately.")) return;
      try {
        await api(`/donor/cancel/${button.dataset.cancel}`, { method: "POST" });
        showToast("Broadcast cancelled.", "info");
        loadBatches();
      } catch (error) {
        showToast(error.message, "error");
      }
    });
  });

  list.querySelectorAll("[data-complete]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api(`/donor/complete/${button.dataset.complete}`, { method: "POST" });
        showToast("Delivery confirmed — rescue complete! 🎉", "party");
        frUI.confetti();
        loadBatches();
      } catch (error) {
        showToast(error.message, "error");
      }
    });
  });

  list.querySelectorAll("[data-proof]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const proof = await api(`/media/proof/${button.dataset.proof}`);
        document.getElementById("photo-modal-img").src = proof.data_uri;
        document.getElementById("photo-modal-meta").textContent =
          `Uploaded ${new Date(proof.uploaded_at).toLocaleString()}`;
        document.getElementById("photo-modal").classList.remove("hidden");
      } catch (error) {
        showToast(error.message, "error");
      }
    });
  });
}

document.getElementById("btn-close-photo").addEventListener("click", () => {
  document.getElementById("photo-modal").classList.add("hidden");
});

async function loadBatches() {
  try {
    const { batches } = await api("/donor/my-batches");
    renderBatches(batches);

    // Auto-open the self-delivery prompt when any batch needs a volunteer.
    if (!modalBatchId) {
      const candidate = batches.find(
        (batch) => batch.status === "volunteer_needed" && !dismissedPrompts.has(batch._id)
      );
      if (candidate) openSelfDeliverModal(candidate);
    }
  } catch (error) {
    showToast(error.message);
  }
}

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
// Community impact stats
// ---------------------------------------------------------------------------

async function loadImpact() {
  try {
    const stats = await api("/stats/impact");
    document.getElementById("stat-kg").textContent = stats.total_kg_rescued;
    document.getElementById("stat-meals").textContent = stats.meals_served_estimate;
    document.getElementById("stat-rescues").textContent = stats.rescues_completed;
    document.getElementById("stat-co2").textContent = stats.co2e_kg_saved_estimate;
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Batch templates — save the form once, donate in one click forever
// ---------------------------------------------------------------------------

async function loadTemplates() {
  try {
    const { templates } = await api("/donor/templates");
    const strip = document.getElementById("template-strip");
    const chips = document.getElementById("template-chips");
    strip.classList.toggle("hidden", !templates.length);
    chips.innerHTML = templates
      .map(
        (t) => `
          <span class="fr-chip" data-tpl="${escapeHtml(t._id)}" title="${escapeHtml(t.food_description)}">
            ⚡ ${escapeHtml(t.name)} (${escapeHtml(t.quantity_kg)} kg)
          </span>
          <button class="fr-btn-mini danger" data-tpl-del="${escapeHtml(t._id)}" title="Delete template">✕</button>`
      )
      .join("");

    chips.querySelectorAll("[data-tpl]").forEach((chip) => {
      chip.addEventListener("click", async () => {
        try {
          await api(`/donor/templates/${chip.dataset.tpl}/donate`, { method: "POST" });
          showToast("Template broadcast is live — matching NGOs now! ⚡", "success");
          loadBatches();
        } catch (error) {
          showToast(error.message, "error");
        }
      });
    });
    chips.querySelectorAll("[data-tpl-del]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await api(`/donor/templates/${button.dataset.tplDel}`, { method: "DELETE" });
          loadTemplates();
        } catch (error) {
          showToast(error.message, "error");
        }
      });
    });
  } catch {
    /* non-fatal */
  }
}

document.getElementById("btn-save-template").addEventListener("click", async () => {
  const dietaryTags = ["vegetarian"];
  document.querySelectorAll('input[name="dietary"]:checked').forEach((c) => dietaryTags.push(c.value));
  try {
    await api("/donor/templates", {
      method: "POST",
      body: JSON.stringify({
        food_description: document.getElementById("food-description").value.trim(),
        quantity_kg: document.getElementById("quantity-kg").value,
        address: document.getElementById("pickup-address").value.trim(),
        dietary_tags: dietaryTags,
        latitude: document.getElementById("lat").value,
        longitude: document.getElementById("lng").value,
      }),
    });
    showToast("Template saved — it'll appear as a quick-fill chip 💾", "success");
    loadTemplates();
  } catch (error) {
    showToast(error.message, "error");
  }
});

// ---------------------------------------------------------------------------
// Recurring donation schedules
// ---------------------------------------------------------------------------

const recurringForm = document.getElementById("recurring-form");
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

document.getElementById("btn-toggle-recurring").addEventListener("click", () => {
  recurringForm.classList.toggle("hidden");
});

document.querySelectorAll("#rec-days .fr-chip").forEach((chip) => {
  chip.addEventListener("click", () => chip.classList.toggle("on"));
});

recurringForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const days = [...document.querySelectorAll("#rec-days .fr-chip.on")].map((c) => Number(c.dataset.day));
  if (!days.length) {
    showToast("Pick at least one weekday for the schedule", "error");
    return;
  }
  try {
    await api("/donor/recurring", {
      method: "POST",
      body: JSON.stringify({
        food_description: document.getElementById("rec-desc").value.trim(),
        quantity_kg: document.getElementById("rec-kg").value,
        address: document.getElementById("rec-address").value.trim(),
        time: document.getElementById("rec-time").value,
        days,
        tz_offset_min: -new Date().getTimezoneOffset(),
        latitude: document.getElementById("lat").value,
        longitude: document.getElementById("lng").value,
      }),
    });
    recurringForm.reset();
    recurringForm.classList.add("hidden");
    document.querySelectorAll("#rec-days .fr-chip.on").forEach((c) => c.classList.remove("on"));
    showToast("Schedule created — your surplus now rescues itself 🔁", "success");
    loadRecurring();
  } catch (error) {
    showToast(error.message, "error");
  }
});

async function loadRecurring() {
  try {
    const { schedules } = await api("/donor/recurring");
    const list = document.getElementById("recurring-list");
    if (!schedules.length) {
      list.innerHTML = '<p class="empty-state">No schedules yet.</p>';
      return;
    }
    list.innerHTML = schedules
      .map((s) => {
        const days = (s.days || []).map((d) => DAY_NAMES[d]).join(" · ");
        const next = s.next_run_at ? new Date(s.next_run_at).toLocaleString() : "—";
        return `
          <article class="batch-card recurring-card ${s.active ? "" : "paused"}">
            <span class="batch-title">${s.active ? "🔁" : "⏸"} ${escapeHtml(s.food_description)}</span>
            <span class="batch-meta">${escapeHtml(s.quantity_kg)} kg · ${escapeHtml(days)} at ${escapeHtml(s.time)}
              · ran ${escapeHtml(s.runs)}× ${s.active ? `· next: ${escapeHtml(next)}` : "· paused"}</span>
            <div class="batch-actions">
              <button class="fr-btn-mini cta" data-run="${escapeHtml(s._id)}">🚀 Run now</button>
              <button class="fr-btn-mini" data-pause="${escapeHtml(s._id)}">${s.active ? "⏸ Pause" : "▶ Resume"}</button>
              <button class="fr-btn-mini danger" data-del="${escapeHtml(s._id)}">🗑 Delete</button>
            </div>
          </article>`;
      })
      .join("");

    list.querySelectorAll("[data-run]").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api(`/donor/recurring/${b.dataset.run}/run-now`, { method: "POST" });
          showToast("Broadcast fired from your schedule 🚀", "success");
          loadBatches();
          loadRecurring();
        } catch (error) {
          showToast(error.message, "error");
        }
      })
    );
    list.querySelectorAll("[data-pause]").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api(`/donor/recurring/${b.dataset.pause}/toggle`, { method: "POST" });
          loadRecurring();
        } catch (error) {
          showToast(error.message, "error");
        }
      })
    );
    list.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api(`/donor/recurring/${b.dataset.del}`, { method: "DELETE" });
          loadRecurring();
        } catch (error) {
          showToast(error.message, "error");
        }
      })
    );
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Personal impact report + referral card
// ---------------------------------------------------------------------------

async function loadMyImpact() {
  try {
    const report = await api("/stats/my-impact");
    const box = document.getElementById("my-impact");
    const maxKg = Math.max(...report.weekly.map((w) => w.kg), 1);
    const bars = report.weekly
      .map(
        (w, i) => `
          <div class="fr-sparkbar ${i === report.weekly.length - 1 ? "now" : ""}"
               style="height:${Math.max(6, (w.kg / maxKg) * 100)}%"
               data-tip="${escapeHtml(w.week_start)}: ${escapeHtml(w.kg)} kg · ${escapeHtml(w.rescues)} rescues"></div>`
      )
      .join("");
    box.innerHTML = `
      <div class="impact-grid">
        <div class="impact-cell"><b>${escapeHtml(report.totals.rescues)}</b><span>rescues</span></div>
        <div class="impact-cell"><b>${escapeHtml(report.totals.kg)}</b><span>kg donated</span></div>
        <div class="impact-cell"><b>${escapeHtml(report.totals.meals_estimate)}</b><span>meals</span></div>
        <div class="impact-cell"><b>${escapeHtml(report.totals.co2e_kg_saved)}</b><span>kg CO₂e</span></div>
      </div>
      <div class="fr-sparkbars">${bars}</div>
      <p class="section-hint">Last 12 weeks · 🔥 streak: <b>${escapeHtml(report.streak_days)}</b> day${report.streak_days === 1 ? "" : "s"}${report.top_zone ? ` · top zone: <b>${escapeHtml(report.top_zone)}</b>` : ""}</p>`;
  } catch {
    /* non-fatal */
  }
}

document.getElementById("btn-csv").addEventListener("click", async () => {
  try {
    const response = await fetch(`${API_BASE}/stats/my-impact.csv`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    const blob = await response.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "foodrescue-impact.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch {
    showToast("Could not download the CSV", "error");
  }
});

async function loadReferral() {
  try {
    const ref = await api("/auth/referral");
    document.getElementById("referral-card").innerHTML = `
      <p class="section-hint">Share your code — you BOTH earn <b>+${escapeHtml(ref.bonus_per_rescue)} trust</b> when your invitee completes their first rescue.</p>
      <div class="referral-code-row">
        <code class="referral-code">${escapeHtml(ref.referral_code)}</code>
        <button id="btn-copy-ref" class="fr-btn-mini cta" type="button">📋 Copy</button>
      </div>
      <p class="section-hint">👥 <b>${escapeHtml(ref.referred_count)}</b> joined with your code · 🏅 <b>${escapeHtml(ref.bonus_paid_count)}</b> bonus${ref.bonus_paid_count === 1 ? "" : "es"} earned</p>`;
    document.getElementById("btn-copy-ref").addEventListener("click", () => {
      navigator.clipboard
        ? navigator.clipboard.writeText(ref.referral_code).then(() => showToast("Referral code copied! 🎁", "success"))
        : showToast(`Your code: ${ref.referral_code}`, "info");
    });
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

frUI.registerCommands([
  {
    icon: "📢",
    label: "New broadcast (open form)",
    run: () => {
      broadcastSection.classList.remove("hidden");
      broadcastSection.scrollIntoView({ behavior: "smooth" });
    },
  },
]);

// ---------------------------------------------------------------------------
// ESG sustainability dashboard — carbon impact + certified reports
// ---------------------------------------------------------------------------

let esgData = null;

async function loadEsg() {
  const tiles = document.getElementById("esg-tiles");
  try {
    esgData = await api("/esg/dashboard");
  } catch (error) {
    tiles.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    return;
  }
  const t = esgData.totals;
  tiles.innerHTML = `
    <div class="esg-tile"><b>${t.total_co2e_kg.toLocaleString()}</b><span>kg CO₂e avoided</span></div>
    <div class="esg-tile"><b>${t.methane_kg.toLocaleString()}</b><span>kg methane prevented</span></div>
    <div class="esg-tile"><b>${t.kg_food.toLocaleString()}</b><span>kg food rescued</span></div>
    <div class="esg-tile"><b>${t.rescues.toLocaleString()}</b><span>verified rescues</span></div>`;

  const eq = t.equivalents;
  document.getElementById("esg-equivalents").innerHTML = `
    <span class="esg-eq">🚗 ${eq.car_days_off_road.toLocaleString()} car-days off the road</span>
    <span class="esg-eq">🌳 ${eq.tree_years.toLocaleString()} tree-years of absorption</span>
    <span class="esg-eq">🛣️ ${eq.km_not_driven.toLocaleString()} km not driven</span>
    <span class="esg-eq">🔌 ${eq.phone_charges.toLocaleString()} phone charges</span>
    <span class="esg-eq">🍽️ ${eq.meals_served.toLocaleString()} meals served</span>`;

  const months = esgData.monthly || [];
  const maxCo2 = Math.max(...months.map((m) => m.co2e_kg), 1);
  document.getElementById("esg-months").innerHTML = months
    .map(
      (m) => `
      <div class="esg-month" title="${escapeHtml(m.month)}: ${m.kg} kg food · ${m.co2e_kg} kg CO₂e · ${m.rescues} rescues">
        <div class="esg-bar" style="height:${Math.max(4, (m.co2e_kg / maxCo2) * 72)}px"></div>
        <span>${escapeHtml(m.month.slice(5))}</span>
      </div>`
    )
    .join("");
}

document.getElementById("btn-esg-csv").addEventListener("click", async () => {
  try {
    const response = await fetch(`${API_BASE}/esg/report.csv`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!response.ok) throw new Error(`Report failed (${response.status})`);
    const blob = await response.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "foodrescue-esg-report.csv";
    a.click();
    URL.revokeObjectURL(a.href);
    showToast("ESG audit report downloaded — ready for CSR filings 📄", "success");
  } catch (error) {
    showToast(error.message, "error");
  }
});

document.getElementById("btn-esg-print").addEventListener("click", () => {
  if (!esgData) {
    showToast("Impact data is still loading — try again in a second", "info");
    return;
  }
  const t = esgData.totals;
  const m = esgData.methodology;
  const win = window.open("", "_blank", "width=900,height=760");
  if (!win) {
    showToast("Pop-up blocked — allow pop-ups for the report", "error");
    return;
  }
  const rows = (esgData.monthly || [])
    .map(
      (r) =>
        `<tr><td>${r.month}</td><td>${r.rescues}</td><td>${r.kg}</td><td>${r.methane_kg}</td><td>${r.co2e_kg}</td></tr>`
    )
    .join("");
  win.document.write(`<html><head><title>FoodRescue — Certified ESG Report</title><style>
    body{font-family:Georgia,serif;color:#241b35;padding:2.2rem;max-width:820px;margin:auto}
    h1{color:#7c3aed;font-size:1.5rem;margin-bottom:0.2rem}
    .sub{color:#776d91;font-size:0.85rem;margin-bottom:1.4rem}
    .grid{display:flex;gap:1.6rem;flex-wrap:wrap;margin:1.2rem 0}
    .stat b{display:block;font-size:1.5rem;color:#0d9488}.stat span{font-size:0.78rem;color:#776d91}
    table{border-collapse:collapse;width:100%;font-size:0.82rem;margin:1rem 0}
    th,td{border:1px solid #e9e2f4;padding:0.4rem 0.6rem;text-align:right}
    th{background:#f7f2fd;color:#5b5175}td:first-child,th:first-child{text-align:left}
    .meth{font-size:0.75rem;color:#776d91;border-top:2px solid #e9e2f4;padding-top:0.8rem;line-height:1.5}
    @media print{body{padding:0.5rem}}
  </style></head><body>
    <h1>🥗 FoodRescue — Certified ESG / Carbon Offset Report</h1>
    <p class="sub">Organization: <b>${frUI.escapeHtml(esgData.donor.name)}</b> ·
      Generated ${new Date().toLocaleDateString()} ·
      Basis: verified, proof-backed completed rescues only</p>
    <div class="grid">
      <div class="stat"><b>${t.total_co2e_kg.toLocaleString()} kg</b><span>CO₂e emissions avoided</span></div>
      <div class="stat"><b>${t.methane_kg.toLocaleString()} kg</b><span>landfill methane prevented</span></div>
      <div class="stat"><b>${t.kg_food.toLocaleString()} kg</b><span>food diverted from landfill</span></div>
      <div class="stat"><b>${t.equivalents.car_days_off_road.toLocaleString()}</b><span>car-days off the road</span></div>
      <div class="stat"><b>${t.equivalents.tree_years.toLocaleString()}</b><span>tree-years of CO₂ absorption</span></div>
    </div>
    <table><thead><tr><th>Month</th><th>Rescues</th><th>Food (kg)</th><th>CH₄ avoided (kg)</th><th>CO₂e avoided (kg)</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="meth"><b>Methodology.</b> Landfill pathway: ${m.ch4_per_kg_food} kg CH₄ per kg food
      (IPCC first-order-decay model) × GWP-100 of ${m.gwp100_ch4} = ${m.landfill_co2e_per_kg} kg CO₂e/kg.
      Avoided replacement production (vegetarian mix, Poore &amp; Nemecek 2018): ${m.production_co2e_per_kg} kg CO₂e/kg.
      Total factor: <b>${m.total_co2e_per_kg} kg CO₂e per kg rescued</b>.
      Equivalencies per US EPA. Sources: ${frUI.escapeHtml(m.sources)}.</p>
  </body></html>`);
  win.document.close();
  setTimeout(() => win.print(), 400);
});

// ---------------------------------------------------------------------------
// POS integration — per-donor API key management
// ---------------------------------------------------------------------------

async function loadPosKey() {
  const host = document.getElementById("pos-key-info");
  try {
    const { api_key } = await api("/integrations/api-key");
    const revokeBtn = document.getElementById("btn-revoke-key");
    if (!api_key) {
      revokeBtn.classList.add("hidden");
      host.innerHTML =
        '<p class="empty-state">No key yet — generate one and paste it into your POS/billing software.</p>';
      return;
    }
    revokeBtn.classList.remove("hidden");
    host.innerHTML = `
      <div class="pos-key-row">
        <code>${escapeHtml(api_key.masked)}</code>
        <span class="refresh-note">created ${new Date(api_key.created_at).toLocaleDateString()}
          · used ${api_key.uses} time${api_key.uses === 1 ? "" : "s"}</span>
      </div>
      <details class="pos-howto">
        <summary>How to connect a POS system</summary>
        <pre>curl -X POST http://localhost:5000/integrations/pos/broadcast \\
  -H "X-API-Key: &lt;your key&gt;" -H "Content-Type: application/json" \\
  -d '{"food_description": "8 kg dal makhani surplus", "quantity_kg": 8}'</pre>
        <p class="refresh-note">Address &amp; location default to your registered profile — one call at closing time is all it takes.</p>
      </details>`;
  } catch (error) {
    host.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

document.getElementById("btn-gen-key").addEventListener("click", async () => {
  if (!window.confirm("Generate a new key? Any existing key stops working immediately.")) return;
  try {
    const data = await api("/integrations/api-key", { method: "POST" });
    window.prompt(
      "Your POS API key — copy it NOW, it is shown only once:",
      data.api_key
    );
    showToast("API key generated — paste it into your POS system 🔑", "success");
    loadPosKey();
  } catch (error) {
    showToast(error.message, "error");
  }
});

document.getElementById("btn-revoke-key").addEventListener("click", async () => {
  if (!window.confirm("Revoke the POS key? Automated broadcasts will stop.")) return;
  try {
    await api("/integrations/api-key", { method: "DELETE" });
    showToast("Key revoked", "info");
    loadPosKey();
  } catch (error) {
    showToast(error.message, "error");
  }
});

function refreshAll() {
  refreshProfile();
  loadBatches();
  loadNotifications();
  loadImpact();
  loadTemplates();
  loadRecurring();
  loadMyImpact();
  loadReferral();
  loadPosKey();
  loadEsg();
  frUI.mountAchievements(document.getElementById("achievements-card"));
}

window.addEventListener("fr:refresh", refreshAll);

refreshAll();
fillLocation();
setInterval(() => {
  loadBatches();
  loadNotifications();
  loadImpact();
}, POLL_MS);
