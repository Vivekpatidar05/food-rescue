/* theme.js — FoodRescue theme controller.
 *
 * A tiny inline snippet in each page's <head> sets data-theme before first
 * paint (no flash). This script owns the rest: it injects the toggle button,
 * persists the choice to localStorage, and animates the flip. Loaded on every
 * panel; safe to run before or after the panel's own script.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "fr_theme";
  var root = document.documentElement;

  // Restore the accent pack (Festival/Ocean/Forest/Sunrise) before first
  // paint — fr-ui.js owns switching it, we just prevent the flash.
  try {
    var accent = localStorage.getItem("fr_accent");
    if (accent && accent !== "festival") root.setAttribute("data-accent", accent);
  } catch (e) { /* private mode */ }

  function current() {
    return root.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function apply(theme, animate) {
    if (animate) {
      root.classList.add("fr-theming");
      window.setTimeout(function () { root.classList.remove("fr-theming"); }, 260);
    }
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem(STORAGE_KEY, theme); } catch (e) { /* private mode */ }
    document.querySelectorAll(".fr-theme-toggle").forEach(function (btn) {
      btn.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
      btn.setAttribute("title", theme === "dark" ? "Light mode" : "Dark mode");
    });
  }

  function toggle() {
    apply(current() === "dark" ? "light" : "dark", true);
  }

  function makeButton(floating) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "fr-theme-toggle" + (floating ? " fr-floating" : "");
    btn.innerHTML =
      '<span class="fr-icon-light" aria-hidden="true">☾</span>' +
      '<span class="fr-icon-dark" aria-hidden="true">☀</span>';
    btn.addEventListener("click", toggle);
    return btn;
  }

  function mount() {
    if (document.querySelector(".fr-theme-toggle")) return;
    var navRight = document.querySelector(".nav-right");
    if (navRight) {
      navRight.insertBefore(makeButton(false), navRight.firstChild);
    } else {
      document.body.appendChild(makeButton(true));
    }
    apply(current(), false); // sync labels
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  // Follow the OS theme only while the user hasn't made an explicit choice.
  try {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", function (e) {
      if (!localStorage.getItem(STORAGE_KEY)) apply(e.matches ? "dark" : "light", true);
    });
  } catch (e) { /* older browsers: ignore */ }
})();
