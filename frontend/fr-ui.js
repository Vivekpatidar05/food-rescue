/* fr-ui.js — FoodRescue shared UI layer, loaded on every panel AFTER theme.js
 * and BEFORE the panel's own script.
 *
 * Provides (as window.frUI):
 *   toast(msg, kind)          stacked toasts ("info" | "success" | "error" | "party")
 *   confetti()                celebration burst on rescue completion
 *   chat.open(batchId, title) per-batch coordination chat dock (polls 5s)
 *   rate.open(batch, opts)    post-rescue 5-star rating modal
 *   trackBar(el, batchId)     live courier progress bar (polls 10s)
 *   registerCommands(list)    add panel-specific Ctrl+K commands
 *   openPalette()             command palette (Ctrl+K / Cmd+K)
 *   enableDesktopAlerts()     browser Notification permission + polling
 *   escapeHtml(str)
 *
 * Also self-runs: surge-mode banner poll, PWA service-worker registration,
 * install-prompt capture, desktop-notification polling, Ctrl+K binding.
 */
(function () {
  "use strict";

  var API = "http://localhost:5000";
  var SURGE_POLL_MS = 90000;
  var ALERT_POLL_MS = 45000;

  function token() { return localStorage.getItem("fr_token"); }
  function storedUser() {
    try { return JSON.parse(localStorage.getItem("fr_user")); } catch (e) { return null; }
  }

  function api(path, options) {
    options = options || {};
    var headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    if (token()) headers["Authorization"] = "Bearer " + token();
    return fetch(API + path, Object.assign({}, options, { headers: headers })).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var err = new Error(data.error || "Request failed (" + resp.status + ")");
          err.status = resp.status;
          throw err;
        }
        return data;
      });
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }

  function el(tag, className, html) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (html != null) node.innerHTML = html;
    return node;
  }

  /* ------------------------------------------------------------------ *
   * Toasts                                                             *
   * ------------------------------------------------------------------ */
  var toastStack = null;
  function toast(message, kind, ms) {
    if (!toastStack) {
      toastStack = el("div", "fr-toast-stack");
      document.body.appendChild(toastStack);
    }
    var icons = { info: "✨", success: "✅", error: "⚠️", party: "🎉" };
    var node = el("div", "fr-toast " + (kind || "info"),
      "<span>" + (icons[kind] || icons.info) + "</span><span>" + escapeHtml(message) + "</span>");
    toastStack.appendChild(node);
    while (toastStack.children.length > 4) toastStack.removeChild(toastStack.firstChild);
    setTimeout(function () {
      node.classList.add("leaving");
      setTimeout(function () { node.remove(); }, 300);
    }, ms || 4200);
  }

  /* ------------------------------------------------------------------ *
   * Confetti — tiny dependency-free burst                              *
   * ------------------------------------------------------------------ */
  function confetti() {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var canvas = el("canvas", "fr-confetti");
    canvas.width = innerWidth;
    canvas.height = innerHeight;
    document.body.appendChild(canvas);
    var ctx = canvas.getContext("2d");
    var colors = ["#7c3aed", "#ec4899", "#f97316", "#0d9488", "#0ea5e9", "#f59e0b", "#c026d3"];
    var bits = [];
    for (var i = 0; i < 130; i++) {
      bits.push({
        x: innerWidth / 2 + (Math.random() - 0.5) * 120,
        y: innerHeight * 0.35,
        vx: (Math.random() - 0.5) * 14,
        vy: -Math.random() * 13 - 4,
        size: Math.random() * 7 + 4,
        color: colors[i % colors.length],
        rot: Math.random() * Math.PI,
        vr: (Math.random() - 0.5) * 0.3,
      });
    }
    var frames = 0;
    (function tick() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      bits.forEach(function (b) {
        b.x += b.vx; b.y += b.vy; b.vy += 0.42; b.rot += b.vr;
        ctx.save();
        ctx.translate(b.x, b.y);
        ctx.rotate(b.rot);
        ctx.fillStyle = b.color;
        ctx.fillRect(-b.size / 2, -b.size / 2, b.size, b.size * 0.6);
        ctx.restore();
      });
      if (++frames < 120) requestAnimationFrame(tick);
      else canvas.remove();
    })();
  }

  /* ------------------------------------------------------------------ *
   * Chat dock                                                          *
   * ------------------------------------------------------------------ */
  var chatState = { dock: null, batchId: null, lastId: null, timer: null };

  function closeChat() {
    if (chatState.timer) clearInterval(chatState.timer);
    if (chatState.dock) chatState.dock.remove();
    chatState = { dock: null, batchId: null, lastId: null, timer: null };
  }

  function renderChatMessages(list, batch) {
    var me = storedUser() || {};
    var body = chatState.dock.querySelector(".fr-chat-body");
    var empty = body.querySelector(".fr-chat-empty");
    if (list.length && empty) empty.remove();
    list.forEach(function (m) {
      chatState.lastId = m._id;
      var mine = m.sender_id === me._id;
      var node = el("div", "fr-chat-msg" + (mine ? " mine" : ""),
        (mine ? "" : '<span class="fr-chat-who">' + escapeHtml(m.sender_name) +
          " · " + escapeHtml(m.sender_role) + "</span>") + escapeHtml(m.text));
      body.appendChild(node);
    });
    if (list.length) body.scrollTop = body.scrollHeight;
  }

  function pollChat() {
    if (!chatState.batchId) return;
    var path = "/social/chat/" + chatState.batchId +
      (chatState.lastId ? "?after=" + chatState.lastId : "");
    api(path).then(function (data) {
      renderChatMessages(data.messages || []);
    }).catch(function () { /* poll again next tick */ });
  }

  function openChat(batchId, title) {
    if (chatState.batchId === batchId) return;
    closeChat();
    var dock = el("div", "fr-chat-dock");
    dock.innerHTML =
      '<div class="fr-chat-head">💬 <span class="fr-chat-title">' + escapeHtml(title || "Rescue chat") + "</span>" +
      '<button class="fr-chat-close" type="button" aria-label="Close chat">✕</button></div>' +
      '<div class="fr-chat-body"><p class="fr-chat-empty">Say hello — the donor, NGO and volunteer all see this thread.</p></div>' +
      '<form class="fr-chat-form"><input type="text" maxlength="500" placeholder="Type a message…" />' +
      '<button class="fr-chat-send" type="submit">Send</button></form>';
    document.body.appendChild(dock);
    chatState.dock = dock;
    chatState.batchId = batchId;

    dock.querySelector(".fr-chat-close").addEventListener("click", closeChat);
    dock.querySelector(".fr-chat-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var input = dock.querySelector("input");
      var text = input.value.trim();
      if (!text) return;
      input.value = "";
      api("/social/chat/" + batchId, { method: "POST", body: JSON.stringify({ text: text }) })
        .then(function (data) { renderChatMessages([data.message]); })
        .catch(function (err) { toast(err.message, "error"); });
    });

    pollChat();
    chatState.timer = setInterval(pollChat, 5000);
  }

  /* ------------------------------------------------------------------ *
   * Rating modal                                                       *
   * ------------------------------------------------------------------ */
  function openRating(batch, opts) {
    opts = opts || {};
    var overlay = el("div", "fr-modal-overlay");
    var stars = 0;
    overlay.innerHTML =
      '<div class="fr-modal" role="dialog" aria-modal="true"><h3>⭐ Rate this rescue</h3>' +
      '<p class="fr-modal-sub">' + escapeHtml(opts.subtitle || ('"' + (batch.food_description || "") + '"')) + "</p>" +
      '<div class="fr-stars">' +
      [1, 2, 3, 4, 5].map(function (n) {
        return '<button type="button" class="fr-star" data-star="' + n + '" aria-label="' + n + ' stars">⭐</button>';
      }).join("") +
      "</div>" +
      '<textarea maxlength="300" placeholder="A short note (optional)…"></textarea>' +
      '<div class="fr-modal-actions"><button class="fr-btn-primary" type="button">Submit rating</button>' +
      '<button class="fr-btn-quiet" type="button">Maybe later</button></div></div>';
    document.body.appendChild(overlay);

    function paint() {
      overlay.querySelectorAll(".fr-star").forEach(function (btn) {
        btn.classList.toggle("on", Number(btn.dataset.star) <= stars);
      });
    }
    overlay.querySelectorAll(".fr-star").forEach(function (btn) {
      btn.addEventListener("click", function () { stars = Number(btn.dataset.star); paint(); });
    });
    overlay.querySelector(".fr-btn-quiet").addEventListener("click", function () { overlay.remove(); });
    overlay.addEventListener("click", function (event) { if (event.target === overlay) overlay.remove(); });
    overlay.querySelector(".fr-btn-primary").addEventListener("click", function () {
      if (!stars) { toast("Pick a star rating first", "error"); return; }
      var body = { stars: stars, comment: overlay.querySelector("textarea").value.trim() };
      if (opts.rateeId) body.ratee_id = opts.rateeId;
      api("/social/rate/" + batch._id, { method: "POST", body: JSON.stringify(body) })
        .then(function () {
          overlay.remove();
          rememberRated(batch._id);
          toast("Thanks — your rating keeps the community honest!", "party");
          if (typeof opts.onDone === "function") opts.onDone();
        })
        .catch(function (err) {
          overlay.remove();
          if (err.status === 409) rememberRated(batch._id);
          toast(err.message, err.status === 409 ? "info" : "error");
        });
    });
  }

  function ratedSet() {
    try { return new Set(JSON.parse(localStorage.getItem("fr_rated") || "[]")); }
    catch (e) { return new Set(); }
  }
  function rememberRated(batchId) {
    var set = ratedSet();
    set.add(batchId);
    localStorage.setItem("fr_rated", JSON.stringify(Array.from(set).slice(-100)));
  }
  function hasRated(batchId) { return ratedSet().has(batchId); }

  /* ------------------------------------------------------------------ *
   * Live tracking bar                                                  *
   * ------------------------------------------------------------------ */
  var trackTimers = {};
  function trackBar(container, batchId) {
    if (!container || container.dataset.frTrack === batchId) return;
    container.dataset.frTrack = batchId;
    container.innerHTML =
      '<div class="fr-progress"><div class="fr-progress-fill" style="width:4%"></div></div>' +
      '<div class="fr-progress-label"><span class="fr-track-state">📡 Waiting for courier signal…</span>' +
      '<span class="fr-track-eta"></span></div>';

    function poll() {
      if (!document.body.contains(container)) {
        clearInterval(trackTimers[batchId]);
        delete trackTimers[batchId];
        return;
      }
      api("/social/track/" + batchId).then(function (data) {
        var t = data.tracking || {};
        if (!t.position) return;
        var pct = t.progress_pct == null ? 5 : Math.max(5, t.progress_pct);
        container.querySelector(".fr-progress-fill").style.width = pct + "%";
        var state = container.querySelector(".fr-track-state");
        var eta = container.querySelector(".fr-track-eta");
        if (t.progress_pct != null) {
          state.textContent = "🛵 Courier " + t.progress_pct + "% of the way";
          eta.textContent = (t.remaining_km != null ? t.remaining_km + " km" : "") +
            (t.eta_min != null ? " · ~" + t.eta_min + " min out" : "");
        } else {
          state.textContent = "🛵 Courier is moving";
          eta.textContent = "";
        }
      }).catch(function () { /* retry next poll */ });
    }
    poll();
    if (trackTimers[batchId]) clearInterval(trackTimers[batchId]);
    trackTimers[batchId] = setInterval(poll, 10000);
  }

  /* ------------------------------------------------------------------ *
   * Surge banner                                                       *
   * ------------------------------------------------------------------ */
  var surgeBanner = null;
  function pollSurge() {
    fetch(API + "/stats/surge").then(function (r) { return r.json(); }).then(function (surge) {
      if (surge.active && !surgeBanner) {
        surgeBanner = el("div", "fr-surge-banner");
        surgeBanner.textContent = "SURGE MODE — " + surge.reason +
          " · matching radius widened " + surge.multiplier + "×";
        document.body.prepend(surgeBanner);
      } else if (!surge.active && surgeBanner) {
        surgeBanner.remove();
        surgeBanner = null;
      }
    }).catch(function () { /* offline — try later */ });
  }

  /* ------------------------------------------------------------------ *
   * Desktop notifications                                              *
   * ------------------------------------------------------------------ */
  function pollDesktopAlerts() {
    if (!token() || !("Notification" in window) || Notification.permission !== "granted") return;
    api("/auth/notifications").then(function (data) {
      var seen = new Set(JSON.parse(localStorage.getItem("fr_alerted") || "[]"));
      var fresh = (data.notifications || []).filter(function (n) { return !n.read && !seen.has(n._id); });
      fresh.slice(0, 3).forEach(function (n) {
        new Notification("FoodRescue", { body: n.message, tag: n._id });
        seen.add(n._id);
      });
      localStorage.setItem("fr_alerted", JSON.stringify(Array.from(seen).slice(-200)));
    }).catch(function () { /* ignore */ });
  }

  function enableDesktopAlerts() {
    if (!("Notification" in window)) { toast("This browser has no notification support", "error"); return; }
    Notification.requestPermission().then(function (perm) {
      if (perm === "granted") {
        toast("Desktop alerts on — you'll hear about nearby rescues instantly", "success");
        pollDesktopAlerts();
      } else {
        toast("Notifications stay off (browser permission denied)", "info");
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * PWA — service worker + install prompt                              *
   * ------------------------------------------------------------------ */
  var installPrompt = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    installPrompt = event;
  });
  function installApp() {
    if (!installPrompt) { toast("Already installed (or your browser hides the prompt)", "info"); return; }
    installPrompt.prompt();
    installPrompt.userChoice.then(function (choice) {
      if (choice.outcome === "accepted") toast("FoodRescue installed — find it on your home screen!", "party");
      installPrompt = null;
    });
  }
  if ("serviceWorker" in navigator && location.protocol !== "file:") {
    navigator.serviceWorker.register("sw.js").catch(function () { /* dev server without sw — fine */ });
  }

  /* ------------------------------------------------------------------ *
   * Command palette (Ctrl+K)                                           *
   * ------------------------------------------------------------------ */
  var extraCommands = [];
  function registerCommands(list) { extraCommands = extraCommands.concat(list || []); }

  function baseCommands() {
    var user = storedUser();
    var commands = [];
    if (user) {
      commands.push({ icon: "🏠", label: "Go to my dashboard", run: function () { location.href = user.role + ".html"; } });
      // The command center is admin-only — never tease regular users with a
      // link that dead-ends on the admin sign-in screen.
      if (user.role === "admin") {
        commands.push({ icon: "📊", label: "Open admin command center", run: function () { location.href = "admin.html"; } });
      }
      commands.push({
        icon: "🎁", label: "Copy my referral code",
        run: function () {
          api("/auth/referral").then(function (data) {
            (navigator.clipboard ? navigator.clipboard.writeText(data.referral_code) : Promise.reject())
              .then(function () { toast("Referral code " + data.referral_code + " copied!", "success"); })
              .catch(function () { toast("Your referral code: " + data.referral_code, "info", 8000); });
          }).catch(function (err) { toast(err.message, "error"); });
        },
      });
      commands.push({
        icon: "📄", label: "Download my impact report (CSV)",
        run: function () {
          fetch(API + "/stats/my-impact.csv", { headers: { Authorization: "Bearer " + token() } })
            .then(function (r) { return r.blob(); })
            .then(function (blob) {
              var a = document.createElement("a");
              a.href = URL.createObjectURL(blob);
              a.download = "foodrescue-impact.csv";
              a.click();
              URL.revokeObjectURL(a.href);
            });
        },
      });
    }
    commands.push({ icon: "🌓", label: "Toggle light / dark theme", run: function () {
      document.querySelector(".fr-theme-toggle") && document.querySelector(".fr-theme-toggle").click();
    } });
    commands.push({ icon: "🔔", label: "Enable desktop alerts", run: enableDesktopAlerts });
    commands.push({ icon: "📱", label: "Install FoodRescue app", run: installApp });
    commands.push({ icon: "🔄", label: "Refresh data", run: function () { window.dispatchEvent(new CustomEvent("fr:refresh")); toast("Refreshing…", "info", 1500); } });
    if (user) {
      commands.push({ icon: "🚪", label: "Log out", run: function () {
        localStorage.removeItem("fr_token");
        localStorage.removeItem("fr_user");
        location.href = "auth.html";
      } });
    }
    return commands;
  }

  var paletteOverlay = null;
  function closePalette() {
    if (paletteOverlay) { paletteOverlay.remove(); paletteOverlay = null; }
  }

  function openPalette() {
    if (paletteOverlay) { closePalette(); return; }
    var commands = extraCommands.concat(baseCommands());
    paletteOverlay = el("div", "fr-palette-overlay");
    paletteOverlay.innerHTML =
      '<div class="fr-palette"><input type="text" placeholder="Type a command…" aria-label="Command search" />' +
      '<div class="fr-palette-list"></div>' +
      '<div class="fr-palette-hint"><span><span class="fr-kbd">↑↓</span> navigate</span>' +
      '<span><span class="fr-kbd">Enter</span> run</span><span><span class="fr-kbd">Esc</span> close</span></div></div>';
    document.body.appendChild(paletteOverlay);
    var input = paletteOverlay.querySelector("input");
    var list = paletteOverlay.querySelector(".fr-palette-list");
    var filtered = commands;
    var active = 0;

    function render() {
      if (!filtered.length) {
        list.innerHTML = '<p class="fr-palette-empty">No matching command</p>';
        return;
      }
      list.innerHTML = filtered.map(function (c, i) {
        return '<button type="button" class="fr-palette-item' + (i === active ? " active" : "") +
          '" data-i="' + i + '"><span class="fr-pi-icon">' + c.icon + "</span>" + escapeHtml(c.label) + "</button>";
      }).join("");
      list.querySelectorAll(".fr-palette-item").forEach(function (btn) {
        btn.addEventListener("click", function () { run(Number(btn.dataset.i)); });
      });
      var current = list.children[active];
      if (current && current.scrollIntoView) current.scrollIntoView({ block: "nearest" });
    }
    function run(index) {
      var command = filtered[index];
      closePalette();
      if (command) command.run();
    }
    input.addEventListener("input", function () {
      var needle = input.value.trim().toLowerCase();
      filtered = commands.filter(function (c) { return c.label.toLowerCase().includes(needle); });
      active = 0;
      render();
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") { active = Math.min(active + 1, filtered.length - 1); render(); event.preventDefault(); }
      else if (event.key === "ArrowUp") { active = Math.max(active - 1, 0); render(); event.preventDefault(); }
      else if (event.key === "Enter") { run(active); }
      else if (event.key === "Escape") { closePalette(); }
    });
    paletteOverlay.addEventListener("click", function (event) {
      if (event.target === paletteOverlay) closePalette();
    });
    render();
    input.focus();
  }

  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openPalette();
    }
  });

  /* ------------------------------------------------------------------ *
   * Page identity                                                      *
   * ------------------------------------------------------------------ */
  var PAGE = (location.pathname.split("/").pop() || "index.html").toLowerCase();
  var ROLE_PAGES = { "donor.html": "donor", "ngo.html": "ngo", "volunteer.html": "volunteer" };
  var PAGE_ROLE = ROLE_PAGES[PAGE] || null;

  /* ------------------------------------------------------------------ *
   * Accent themes — Festival is the default; Ocean / Forest / Sunrise  *
   * remap the brand tokens for a fresh look. Persisted like dark mode. *
   * ------------------------------------------------------------------ */
  var ACCENTS = ["festival", "ocean", "forest", "sunrise"];
  function applyAccent(name, announce) {
    if (ACCENTS.indexOf(name) === -1) name = "festival";
    if (name === "festival") document.documentElement.removeAttribute("data-accent");
    else document.documentElement.setAttribute("data-accent", name);
    try { localStorage.setItem("fr_accent", name); } catch (e) { /* private mode */ }
    if (announce) toast("Accent theme: " + name.charAt(0).toUpperCase() + name.slice(1), "success");
  }
  try { applyAccent(localStorage.getItem("fr_accent") || "festival", false); } catch (e) { /* ignore */ }

  /* ------------------------------------------------------------------ *
   * Image helper — downscale any picked image file to a JPEG data URI  *
   * ------------------------------------------------------------------ */
  function compressImage(file, done) {
    var reader = new FileReader();
    reader.onload = function () {
      var img = new Image();
      img.onload = function () {
        var maxSide = 1280;
        var scale = Math.min(1, maxSide / Math.max(img.width, img.height));
        var canvas = document.createElement("canvas");
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        done(canvas.toDataURL("image/jpeg", 0.85));
      };
      img.onerror = function () { done(null); };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  }

  /* ------------------------------------------------------------------ *
   * Verification gate — donors/NGOs/volunteers must be admin-approved  *
   * before the dashboard unlocks. Pending → status screen; rejected →  *
   * reason + resubmission form.                                        *
   * ------------------------------------------------------------------ */
  var DETAIL_LABELS = {
    phone: "Phone", org_type: "Donor type", business_name: "Business",
    id_number: "ID number", registration_number: "Registration no.",
    darpan_id: "Darpan ID", driving_licence: "Driving licence", vehicle_type: "Vehicle",
  };

  var verifyOverlay = null;
  var verifyPollTimer = null;

  function detailChips(details) {
    return Object.keys(details || {}).filter(function (k) { return details[k]; }).map(function (k) {
      return '<span class="fr-verify-chip"><b>' + escapeHtml(DETAIL_LABELS[k] || k) + ":</b> " +
        escapeHtml(String(details[k]).replace(/_/g, " ")) + "</span>";
    }).join("");
  }

  function resubmitFormHtml(role, details) {
    details = details || {};
    var fields =
      '<label>Contact phone<input name="phone" type="tel" value="' + escapeHtml(details.phone || "") + '" /></label>';
    if (role === "donor") {
      fields +=
        '<label>Donor type<select name="org_type">' +
        ["restaurant", "caterer", "event_hall", "hostel_canteen", "household", "other"].map(function (t) {
          return '<option value="' + t + '"' + (details.org_type === t ? " selected" : "") + ">" +
            t.replace(/_/g, " ") + "</option>";
        }).join("") + "</select></label>" +
        '<label>Business name<input name="business_name" value="' + escapeHtml(details.business_name || "") + '" /></label>' +
        '<label>FSSAI / licence / govt ID<input name="id_number" value="' + escapeHtml(details.id_number || "") + '" /></label>';
    } else if (role === "ngo") {
      fields +=
        '<label>NGO registration number<input name="registration_number" value="' + escapeHtml(details.registration_number || "") + '" /></label>' +
        '<label>NGO Darpan ID (optional)<input name="darpan_id" value="' + escapeHtml(details.darpan_id || "") + '" /></label>';
    } else {
      fields +=
        '<label>Driving licence number<input name="driving_licence" value="' + escapeHtml(details.driving_licence || "") + '" /></label>' +
        '<label>Vehicle<select name="vehicle_type">' +
        ["bike", "scooter", "bicycle", "car", "van", "other"].map(function (t) {
          return '<option value="' + t + '"' + (details.vehicle_type === t ? " selected" : "") + ">" + t + "</option>";
        }).join("") + "</select></label>";
    }
    fields += '<label>New ID document photo (optional)<input name="id_document" type="file" accept="image/*" /></label>';
    return '<form class="fr-verify-form">' + fields +
      '<button class="fr-btn-primary" type="submit">Resubmit for review</button></form>';
  }

  function renderVerifyOverlay(user) {
    var verification = user.verification || {};
    var status = user.verification_status || "approved";
    if (!verifyOverlay) {
      verifyOverlay = el("div", "fr-verify-overlay");
      document.body.appendChild(verifyOverlay);
      document.body.style.overflow = "hidden";
    }
    var pending = status === "pending";
    verifyOverlay.innerHTML =
      '<div class="fr-verify-card">' +
      '<div class="fr-verify-icon">' + (pending ? "🕵️" : "❌") + "</div>" +
      "<h2>" + (pending ? "Verification in progress" : "Verification declined") + "</h2>" +
      (pending
        ? '<p class="fr-verify-sub">Thanks for joining, <b>' + escapeHtml((user.name || "").split(" ")[0]) +
          "</b>! Our team is reviewing your details — most accounts are approved within a few hours. " +
          "You'll get a notification the moment you're cleared for takeoff. 🚀</p>" +
          '<div class="fr-verify-chips">' + detailChips(verification.details) + "</div>" +
          '<div class="fr-verify-actions">' +
          '<button class="fr-btn-primary" data-act="check" type="button">↻ Check my status</button>' +
          '<button class="fr-btn-quiet" data-act="logout" type="button">Log out</button></div>'
        : '<p class="fr-verify-sub">Reason: <b>' + escapeHtml(verification.rejection_reason || "details could not be confirmed") +
          "</b><br/>Update your details below and our team will take another look.</p>" +
          resubmitFormHtml(user.role, verification.details) +
          '<div class="fr-verify-actions"><button class="fr-btn-quiet" data-act="logout" type="button">Log out</button></div>') +
      "</div>";

    var checkBtn = verifyOverlay.querySelector('[data-act="check"]');
    if (checkBtn) checkBtn.addEventListener("click", function () { refreshVerification(true); });
    verifyOverlay.querySelector('[data-act="logout"]').addEventListener("click", function () {
      localStorage.removeItem("fr_token");
      localStorage.removeItem("fr_user");
      location.href = "auth.html";
    });

    var form = verifyOverlay.querySelector(".fr-verify-form");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var payload = {};
        form.querySelectorAll("input[name],select[name]").forEach(function (input) {
          if (input.type !== "file") payload[input.name] = input.value.trim();
        });
        var fileInput = form.querySelector('input[type="file"]');
        var file = fileInput.files && fileInput.files[0];
        function send() {
          api("/auth/verification", { method: "POST", body: JSON.stringify({ verification: payload }) })
            .then(function (data) {
              toast("Details resubmitted — back in the review queue!", "success");
              var user = storedUser() || {};
              user.verification_status = "pending";
              user.verification = data.verification;
              localStorage.setItem("fr_user", JSON.stringify(user));
              renderVerifyOverlay(user);
            })
            .catch(function (err) { toast(err.message, "error"); });
        }
        if (file) compressImage(file, function (uri) { if (uri) payload.id_document = uri; send(); });
        else send();
      });
    }
  }

  function clearVerifyOverlay() {
    if (verifyPollTimer) { clearInterval(verifyPollTimer); verifyPollTimer = null; }
    if (verifyOverlay) { verifyOverlay.remove(); verifyOverlay = null; document.body.style.overflow = ""; }
  }

  function refreshVerification(fromClick) {
    return api("/auth/me").then(function (data) {
      var user = data.user;
      localStorage.setItem("fr_user", JSON.stringify(user));
      var status = user.verification_status || "approved";
      if (status === "approved") {
        if (verifyOverlay) {
          clearVerifyOverlay();
          toast("You're verified — welcome to the rescue network! 🎉", "party");
          confetti();
          setTimeout(function () { location.reload(); }, 1200);
        }
        return true;
      }
      renderVerifyOverlay(user);
      if (fromClick) toast("Still " + status + " — hang tight, our team is on it.", "info");
      return false;
    }).catch(function () { return true; /* offline/401 — panel's own guard handles it */ });
  }

  function verificationGate() {
    if (!PAGE_ROLE || !token()) return Promise.resolve(true);
    var user = storedUser() || {};
    var promise = refreshVerification(false);
    // While pending, quietly re-check so approval unlocks without a refresh.
    promise.then(function (ok) {
      if (!ok && !verifyPollTimer) verifyPollTimer = setInterval(function () { refreshVerification(false); }, 30000);
    });
    return promise;
  }

  /* ------------------------------------------------------------------ *
   * Live activity ticker — recent completed rescues across the city    *
   * ------------------------------------------------------------------ */
  var tickerBar = null;
  function timeAgo(iso) {
    if (!iso) return "";
    var mins = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (mins < 60) return mins + "m ago";
    var hours = Math.round(mins / 60);
    if (hours < 48) return hours + "h ago";
    return Math.round(hours / 24) + "d ago";
  }

  function mountTicker() {
    var nav = document.querySelector(".nav");
    if (!nav) return;
    fetch(API + "/stats/activity").then(function (r) { return r.json(); }).then(function (data) {
      var items = data.activity || [];
      if (!items.length) { if (tickerBar) { tickerBar.remove(); tickerBar = null; } return; }
      var chunks = items.map(function (a) {
        var meals = Math.round(a.kg * (data.meals_per_kg || 2.5));
        return "<span class='fr-tick-item'>🥗 <b>" + escapeHtml(a.donor_first_name) + "</b> rescued <b>" +
          a.kg + " kg</b>" + (a.zone ? " in " + escapeHtml(a.zone) : "") +
          " · ~" + meals + " meals · " + timeAgo(a.completed_at) + "</span>";
      }).join("<span class='fr-tick-dot'>•</span>");
      if (!tickerBar) {
        tickerBar = el("div", "fr-ticker");
        tickerBar.innerHTML = '<span class="fr-tick-live">🔴 LIVE</span><div class="fr-tick-window"><div class="fr-tick-track"></div></div>';
        nav.insertAdjacentElement("afterend", tickerBar);
      }
      // Duplicate content for a seamless marquee loop.
      tickerBar.querySelector(".fr-tick-track").innerHTML = chunks + "<span class='fr-tick-dot'>•</span>" + chunks;
    }).catch(function () { /* offline — retry on next cycle */ });
  }

  /* ------------------------------------------------------------------ *
   * Onboarding tour — spotlight walkthrough, once per role             *
   * ------------------------------------------------------------------ */
  var PAGE_TOURS = {
    "donor.html": [
      { sel: "#impact-strip", title: "Community impact", text: "Every kilogram you donate shows up here — kg rescued, meals served, CO₂ avoided." },
      { sel: "#broadcast-form", title: "Broadcast surplus food", text: "Type it like you'd say it — “15 kg of cooked paneer left over”. Our AI parses the rest and alerts the nearest NGOs instantly." },
      { sel: "#batches-list", title: "Track every rescue", text: "Watch batches move from pending → accepted → delivered, with live courier GPS and per-rescue chat." },
      { sel: "#btn-bell", title: "Notifications", text: "NGO acceptances, volunteer claims and completed rescues land here in real time." },
    ],
    "ngo.html": [
      { sel: "#available-list", title: "Nearby surplus food", text: "Fresh batches sorted by distance — accept with one tap before the food-safety window closes." },
      { sel: "#zone-chips", title: "Watch zones", text: "Follow any city zone and get pinged about every compatible batch there, however far away." },
      { sel: "#trust-log", title: "Your trust history", text: "Reliability is currency here — completed pickups raise it, no-shows cost you." },
      { sel: "#btn-bell", title: "Notifications", text: "New nearby batches and volunteer updates arrive here instantly." },
    ],
    "volunteer.html": [
      { sel: "#routes-list", title: "Delivery routes", text: "Claim a route, pick up from the donor, drop at the NGO — proof photo seals the rescue (+10 trust)." },
      { sel: "#avail-grid", title: "Your weekly shifts", text: "Tell us when you ride. On-shift volunteers get first dibs in the matching engine." },
      { sel: "#badge-grid", title: "Badges & levels", text: "Deliveries earn XP, streaks and badges — climb from Rookie Rescuer to Guardian of the City." },
      { sel: "#btn-bell", title: "Notifications", text: "New route alerts land here — enable desktop alerts to never miss one." },
    ],
  };

  var tourState = null;
  function endTour() {
    if (!tourState) return;
    tourState.spot.remove();
    tourState.card.remove();
    tourState = null;
    document.removeEventListener("keydown", tourKeys);
  }
  function tourKeys(event) {
    if (event.key === "Escape") endTour();
    if (event.key === "Enter" || event.key === "ArrowRight") tourNext();
  }

  function tourShow(index) {
    var steps = tourState.steps;
    if (index >= steps.length) { endTour(); toast("You're all set — happy rescuing! 🥗", "party"); return; }
    tourState.index = index;
    var target = document.querySelector(steps[index].sel);
    if (!target || target.offsetParent === null) { tourShow(index + 1); return; }
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    setTimeout(function () {
      if (!tourState) return;
      var rect = target.getBoundingClientRect();
      var pad = 8;
      var spot = tourState.spot;
      spot.style.top = rect.top - pad + window.scrollY + "px";
      spot.style.left = rect.left - pad + window.scrollX + "px";
      spot.style.width = rect.width + pad * 2 + "px";
      spot.style.height = rect.height + pad * 2 + "px";
      var card = tourState.card;
      card.innerHTML =
        '<p class="fr-tour-step">' + (index + 1) + " / " + steps.length + "</p>" +
        "<h3>" + escapeHtml(steps[index].title) + "</h3>" +
        "<p>" + escapeHtml(steps[index].text) + "</p>" +
        '<div class="fr-tour-actions">' +
        '<button class="fr-btn-quiet" data-act="skip" type="button">Skip tour</button>' +
        '<button class="fr-btn-primary" data-act="next" type="button">' +
        (index + 1 === steps.length ? "Finish ✨" : "Next →") + "</button></div>";
      card.querySelector('[data-act="skip"]').addEventListener("click", endTour);
      card.querySelector('[data-act="next"]').addEventListener("click", tourNext);
      var below = rect.bottom + 190 < window.innerHeight;
      card.style.top = (below ? rect.bottom + 14 : Math.max(12, rect.top - card.offsetHeight - 160)) + window.scrollY + "px";
      card.style.left = Math.max(12, Math.min(rect.left, window.innerWidth - 340)) + window.scrollX + "px";
    }, 350);
  }
  function tourNext() { if (tourState) tourShow(tourState.index + 1); }

  function startTour(steps) {
    if (tourState || !steps || !steps.length) return;
    tourState = {
      steps: steps,
      index: 0,
      spot: el("div", "fr-tour-spot"),
      card: el("div", "fr-tour-card"),
    };
    document.body.appendChild(tourState.spot);
    document.body.appendChild(tourState.card);
    document.addEventListener("keydown", tourKeys);
    tourShow(0);
  }

  function autoTour() {
    var steps = PAGE_TOURS[PAGE];
    if (!steps) return;
    var key = "fr_tour_" + PAGE;
    try {
      if (localStorage.getItem(key)) return;
      localStorage.setItem(key, "1");
    } catch (e) { return; }
    setTimeout(function () { startTour(steps); }, 900);
  }

  /* ------------------------------------------------------------------ *
   * Achievements card — level, XP bar and badge wall (any role)        *
   * ------------------------------------------------------------------ */
  function mountAchievements(host) {
    if (!host || !token()) return;
    api("/gamification/achievements").then(function (data) {
      var level = data.level || {};
      var pct = level.xp_to_next
        ? Math.min(99, Math.round((level.xp / (level.xp + level.xp_to_next)) * 100))
        : 100;
      host.innerHTML =
        '<div class="fr-ach-head"><div>' +
        '<p class="fr-ach-level">Lv ' + (level.number || 1) + " · " + escapeHtml(level.name || "Rookie Rescuer") + "</p>" +
        '<p class="fr-ach-xp">' + level.xp + " XP" +
        (level.next_level ? " · " + level.xp_to_next + " XP to " + escapeHtml(level.next_level) : " · max level!") + "</p></div>" +
        '<span class="fr-ach-count">🏅 ' + data.badges_earned + "/" + (data.badges || []).length + "</span></div>" +
        '<div class="fr-ach-bar"><div class="fr-ach-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="fr-ach-grid">' +
        (data.badges || []).map(function (b) {
          return '<span class="fr-ach-badge' + (b.earned ? " earned" : "") + '" title="' +
            escapeHtml(b.label) + " — " + Math.round(b.progress * 100) + '%">' +
            '<span class="fr-ach-emoji">' + b.emoji + "</span>" +
            '<span class="fr-ach-label">' + escapeHtml(b.label) + "</span></span>";
        }).join("") + "</div>";
    }).catch(function () { host.innerHTML = ""; });
  }

  /* ------------------------------------------------------------------ *
   * Handoff card — the donor's 6-digit OTP as a giant, scannable card. *
   * QR rendering uses a CDN encoder; offline the digits still show.    *
   * ------------------------------------------------------------------ */
  var qrLibState = "idle"; // idle | loading | ready | failed
  function ensureQrLib(done) {
    if (window.qrcode) { qrLibState = "ready"; done(true); return; }
    if (qrLibState === "failed") { done(false); return; }
    var script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js";
    qrLibState = "loading";
    script.onload = function () { qrLibState = "ready"; done(true); };
    script.onerror = function () { qrLibState = "failed"; done(false); };
    document.head.appendChild(script);
  }

  function handoffCard(batch) {
    var code = batch.pickup_code || "";
    if (!code) { toast("This batch has no handoff code", "error"); return; }
    var overlay = el("div", "fr-modal-overlay");
    overlay.innerHTML =
      '<div class="fr-modal fr-handoff" role="dialog" aria-modal="true">' +
      "<h3>🔑 Handoff card</h3>" +
      '<p class="fr-modal-sub">' + escapeHtml(batch.food_description || "") + "</p>" +
      '<div class="fr-handoff-code">' +
      code.split("").map(function (d) { return "<span>" + d + "</span>"; }).join("") +
      "</div>" +
      '<div class="fr-handoff-qr"><p class="fr-handoff-qr-note">Loading QR…</p></div>' +
      '<p class="fr-handoff-note">Show this only to the person collecting the food — ' +
      "they need the code to complete the rescue. Never share it in chat.</p>" +
      '<div class="fr-modal-actions">' +
      '<button class="fr-btn-primary" data-act="print" type="button">🖨 Print</button>' +
      '<button class="fr-btn-quiet" data-act="close" type="button">Close</button></div></div>';
    document.body.appendChild(overlay);
    overlay.addEventListener("click", function (event) {
      if (event.target === overlay) overlay.remove();
    });
    overlay.querySelector('[data-act="close"]').addEventListener("click", function () { overlay.remove(); });

    var qrHost = overlay.querySelector(".fr-handoff-qr");
    ensureQrLib(function (ok) {
      if (!document.body.contains(overlay)) return;
      if (!ok) { qrHost.innerHTML = '<p class="fr-handoff-qr-note">Offline — use the digits above.</p>'; return; }
      try {
        var qr = window.qrcode(0, "M");
        qr.addData("FOODRESCUE:" + code);
        qr.make();
        qrHost.innerHTML = qr.createSvgTag({ scalable: true, margin: 2 });
      } catch (e) {
        qrHost.innerHTML = '<p class="fr-handoff-qr-note">Use the digits above.</p>';
      }
    });

    overlay.querySelector('[data-act="print"]').addEventListener("click", function () {
      var win = window.open("", "_blank", "width=460,height=640");
      if (!win) { toast("Pop-up blocked — allow pop-ups to print", "error"); return; }
      win.document.write(
        "<html><head><title>FoodRescue handoff card</title><style>" +
        "body{font-family:Arial,sans-serif;text-align:center;padding:2rem;color:#241b35}" +
        "h1{color:#7c3aed;font-size:1.3rem}p{color:#555}" +
        ".code{font-size:3.2rem;font-weight:800;letter-spacing:0.35em;margin:1.2rem 0;color:#c026d3}" +
        "svg{width:220px;height:220px}</style></head><body>" +
        "<h1>🥗 FoodRescue — Handoff card</h1>" +
        "<p>" + escapeHtml(batch.food_description || "") + "</p>" +
        '<div class="code">' + escapeHtml(code) + "</div>" +
        (qrHost.querySelector("svg") ? qrHost.innerHTML : "") +
        "<p>The collector must present/enter this code to complete the rescue.</p>" +
        "</body></html>"
      );
      win.document.close();
      setTimeout(function () { win.print(); }, 300);
    });
  }

  /* ------------------------------------------------------------------ *
   * Impact certificate — a printable CSR certificate of contribution   *
   * ------------------------------------------------------------------ */
  function openCertificate() {
    if (!token()) { toast("Sign in first", "error"); return; }
    api("/stats/my-impact").then(function (data) {
      var user = storedUser() || {};
      var totals = data.totals || {};
      var kg = Number(totals.kg || 0);
      var win = window.open("", "_blank", "width=900,height=700");
      if (!win) { toast("Pop-up blocked — allow pop-ups for the certificate", "error"); return; }
      var today = new Date().toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
      win.document.write(
        "<html><head><title>FoodRescue Impact Certificate</title><style>" +
        "body{font-family:Georgia,serif;margin:0;padding:3rem;background:#faf7ff;color:#241b35}" +
        ".cert{border:6px double #7c3aed;border-radius:18px;padding:3rem;text-align:center;background:#fff;max-width:760px;margin:auto}" +
        ".logo{font-size:2rem}.brand{color:#7c3aed;font-size:1.6rem;font-weight:bold;letter-spacing:0.04em}" +
        "h1{font-size:2.1rem;margin:1rem 0 0.4rem;color:#c026d3}" +
        ".name{font-size:1.7rem;font-weight:bold;margin:0.8rem 0;border-bottom:2px solid #e9e2f4;display:inline-block;padding:0 2rem 0.4rem}" +
        ".stats{display:flex;justify-content:center;gap:2.4rem;margin:1.6rem 0;flex-wrap:wrap}" +
        ".stat b{display:block;font-size:1.7rem;color:#7c3aed}.stat span{font-size:0.85rem;color:#776d91}" +
        ".foot{margin-top:1.8rem;font-size:0.85rem;color:#776d91}" +
        "@media print{body{background:#fff;padding:0}}" +
        "</style></head><body><div class='cert'>" +
        "<div class='logo'>🥗</div><div class='brand'>FoodRescue</div>" +
        "<h1>Certificate of Impact</h1>" +
        "<p>proudly presented to</p>" +
        "<div class='name'>" + escapeHtml(user.name || "A Food Rescuer") + "</div>" +
        "<p>for verified contributions to the fight against food waste and hunger</p>" +
        "<div class='stats'>" +
        "<div class='stat'><b>" + (totals.rescues || 0) + "</b><span>rescues completed</span></div>" +
        "<div class='stat'><b>" + kg.toFixed(1) + " kg</b><span>food rescued</span></div>" +
        "<div class='stat'><b>" + Math.round(kg * 2.5) + "</b><span>meals served (est.)</span></div>" +
        "<div class='stat'><b>" + (kg * 2.5).toFixed(1) + " kg</b><span>CO₂e avoided (est.)</span></div>" +
        "</div>" +
        "<p class='foot'>Issued " + today + " · verified by the FoodRescue platform · every figure reflects completed, proof-backed rescues</p>" +
        "</div></body></html>"
      );
      win.document.close();
      setTimeout(function () { win.print(); }, 400);
    }).catch(function (err) { toast(err.message, "error"); });
  }

  /* ------------------------------------------------------------------ *
   * SMS alerts preference                                              *
   * ------------------------------------------------------------------ */
  function toggleSmsAlerts() {
    api("/auth/preferences").then(function (data) {
      var current = !!(data.preferences || {}).sms_alerts;
      return api("/auth/preferences", {
        method: "POST",
        body: JSON.stringify({ sms_alerts: !current }),
      }).then(function (result) {
        var on = !!(result.preferences || {}).sms_alerts;
        toast(on
          ? "📱 SMS alerts ON — we'll text you about nearby rescues"
          : "SMS alerts off", on ? "success" : "info");
      });
    }).catch(function (err) { toast(err.message, "error"); });
  }

  /* ------------------------------------------------------------------ *
   * Account settings — profile, password, delete (all signed-in roles) *
   * ------------------------------------------------------------------ */
  function openAccountSettings() {
    if (!token()) { toast("Sign in first", "error"); return; }
    var cached = storedUser() || {};
    if (cached.role === "admin") {
      toast("The admin account is managed via .env on the server", "info");
      return;
    }
    api("/auth/me").then(function (data) {
      renderAccountSettings(data.user || cached);
    }).catch(function () { renderAccountSettings(cached); });
  }

  function renderAccountSettings(user) {
    var overlay = el("div", "fr-modal-overlay");
    var picked = null; // freshly located coordinates, if the user updates them
    var coords = ((user.location || {}).coordinates || []);
    var coordsLabel = coords.length === 2
      ? coords[1].toFixed(4) + ", " + coords[0].toFixed(4)
      : "not set";
    overlay.innerHTML =
      '<div class="fr-modal fr-settings" role="dialog" aria-modal="true">' +
      "<h3>⚙️ Account settings</h3>" +
      '<p class="fr-modal-sub">' + escapeHtml(user.email || "") + " · " + escapeHtml(user.role || "") + "</p>" +
      '<form class="fr-verify-form" data-form="profile">' +
      '<label>Name<input name="name" value="' + escapeHtml(user.name || "") + '" required /></label>' +
      '<label>Address<input name="address" value="' + escapeHtml(user.address || "") + '" required /></label>' +
      '<div class="fr-settings-geo"><span>📍 <span data-coords>' + escapeHtml(coordsLabel) + "</span></span>" +
      '<button type="button" class="fr-btn-quiet" data-act="locate">Use my current location</button></div>' +
      (user.role === "ngo"
        ? '<label class="fr-settings-check"><input type="checkbox" name="accepts_non_veg"' +
          (user.accepts_non_veg ? " checked" : "") + " /> We can accept non-vegetarian food</label>"
        : "") +
      '<button class="fr-btn-primary" type="submit">Save profile</button></form>' +
      '<form class="fr-verify-form" data-form="password">' +
      '<label>Current password<input name="current" type="password" autocomplete="current-password" required /></label>' +
      '<label>New password (min 8 chars, letters + numbers)<input name="next" type="password" autocomplete="new-password" required /></label>' +
      '<button class="fr-btn-primary" type="submit">Change password</button></form>' +
      '<details class="fr-settings-danger"><summary>Danger zone — delete my account</summary>' +
      '<p class="fr-modal-sub">Permanently removes your login, KYC details, schedules and notifications. ' +
      "Completed rescues stay in the city's impact history. This cannot be undone.</p>" +
      '<form class="fr-verify-form" data-form="delete">' +
      '<label>Confirm with your password<input name="password" type="password" autocomplete="current-password" required /></label>' +
      '<button class="fr-btn-danger" type="submit">Delete my account forever</button></form></details>' +
      '<div class="fr-modal-actions"><button class="fr-btn-quiet" data-act="close" type="button">Close</button></div></div>';
    document.body.appendChild(overlay);

    overlay.addEventListener("click", function (event) { if (event.target === overlay) overlay.remove(); });
    overlay.querySelector('[data-act="close"]').addEventListener("click", function () { overlay.remove(); });

    overlay.querySelector('[data-act="locate"]').addEventListener("click", function () {
      if (!navigator.geolocation) { toast("This browser has no location support", "error"); return; }
      navigator.geolocation.getCurrentPosition(function (position) {
        picked = { latitude: position.coords.latitude, longitude: position.coords.longitude };
        overlay.querySelector("[data-coords]").textContent =
          picked.latitude.toFixed(4) + ", " + picked.longitude.toFixed(4) + " (new)";
        toast("Location captured — hit Save profile to keep it", "info");
      }, function () {
        toast("Could not read your location — allow location access", "error");
      }, { enableHighAccuracy: true, timeout: 10000 });
    });

    overlay.querySelector('[data-form="profile"]').addEventListener("submit", function (event) {
      event.preventDefault();
      var form = event.target;
      var body = {
        name: form.querySelector('[name="name"]').value.trim(),
        address: form.querySelector('[name="address"]').value.trim(),
      };
      if (picked) { body.latitude = picked.latitude; body.longitude = picked.longitude; }
      var nonVeg = form.querySelector('[name="accepts_non_veg"]');
      if (nonVeg) body.accepts_non_veg = nonVeg.checked;
      api("/auth/profile", { method: "POST", body: JSON.stringify(body) }).then(function (data) {
        localStorage.setItem("fr_user", JSON.stringify(data.user));
        var nameEl = document.getElementById("user-name");
        if (nameEl) nameEl.textContent = data.user.name;
        toast("Profile updated ✨", "success");
      }).catch(function (err) { toast(err.message, "error"); });
    });

    overlay.querySelector('[data-form="password"]').addEventListener("submit", function (event) {
      event.preventDefault();
      var form = event.target;
      api("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: form.querySelector('[name="current"]').value,
          new_password: form.querySelector('[name="next"]').value,
        }),
      }).then(function (data) {
        // Other sessions are revoked; this one continues on the fresh token.
        if (data.token) localStorage.setItem("fr_token", data.token);
        form.reset();
        toast("Password changed 🔒 Other devices were signed out.", "success");
      }).catch(function (err) { toast(err.message, "error"); });
    });

    overlay.querySelector('[data-form="delete"]').addEventListener("submit", function (event) {
      event.preventDefault();
      if (!confirm("Delete your FoodRescue account forever? This cannot be undone.")) return;
      api("/auth/delete-account", {
        method: "POST",
        body: JSON.stringify({ password: event.target.querySelector('[name="password"]').value }),
      }).then(function () {
        toast("Account deleted. Thank you for every rescue. 👋", "info");
        localStorage.removeItem("fr_token");
        localStorage.removeItem("fr_user");
        setTimeout(function () { location.href = "auth.html"; }, 1200);
      }).catch(function (err) { toast(err.message, "error"); });
    });
  }

  function mountAccountEntry() {
    var nameEl = document.getElementById("user-name");
    if (!nameEl || !token()) return;
    nameEl.classList.add("fr-account-link");
    nameEl.title = "Account settings";
    nameEl.addEventListener("click", openAccountSettings);
  }

  /* ------------------------------------------------------------------ *
   * Boot                                                               *
   * ------------------------------------------------------------------ */
  function mountHint() {
    var hint = el("button", "fr-cmd-hint",
      '⌘ <span>Commands</span> <span class="fr-kbd">Ctrl K</span>');
    hint.type = "button";
    hint.addEventListener("click", openPalette);
    document.body.appendChild(hint);
  }

  function boot() {
    mountHint();
    mountAccountEntry();
    pollSurge();
    setInterval(pollSurge, SURGE_POLL_MS);
    if (token()) {
      pollDesktopAlerts();
      setInterval(pollDesktopAlerts, ALERT_POLL_MS);
    }
    if (PAGE_ROLE) {
      mountTicker();
      setInterval(mountTicker, 120000);
      verificationGate().then(function (approved) {
        if (approved) autoTour();
      });
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  // Extra command-palette entries: accent themes + tour replay.
  registerCommands(
    ACCENTS.map(function (name) {
      return {
        icon: { festival: "🎪", ocean: "🌊", forest: "🌿", sunrise: "🌅" }[name],
        label: "Accent theme: " + name.charAt(0).toUpperCase() + name.slice(1),
        run: function () { applyAccent(name, true); },
      };
    }).concat(PAGE_TOURS[PAGE] ? [{
      icon: "🧭", label: "Replay the welcome tour",
      run: function () { startTour(PAGE_TOURS[PAGE]); },
    }] : []).concat(token() ? [
      { icon: "⚙️", label: "Account settings", run: openAccountSettings },
      { icon: "📱", label: "Toggle SMS alerts (Brevo)", run: toggleSmsAlerts },
      { icon: "🎓", label: "Print my impact certificate", run: openCertificate },
    ] : [])
  );

  window.frUI = {
    api: api,
    toast: toast,
    confetti: confetti,
    chat: { open: openChat, close: closeChat },
    rate: { open: openRating, hasRated: hasRated, remember: rememberRated },
    trackBar: trackBar,
    registerCommands: registerCommands,
    openPalette: openPalette,
    enableDesktopAlerts: enableDesktopAlerts,
    installApp: installApp,
    escapeHtml: escapeHtml,
    compressImage: compressImage,
    mountAchievements: mountAchievements,
    startTour: startTour,
    applyAccent: applyAccent,
    handoffCard: handoffCard,
    certificate: openCertificate,
    toggleSmsAlerts: toggleSmsAlerts,
    accountSettings: openAccountSettings,
  };
})();
