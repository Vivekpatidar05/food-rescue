// auth.js — login / register / forgot-password logic shared by all roles.

const API_BASE = "http://localhost:5000";

const ROLE_HOME = {
  donor: "donor.html",
  ngo: "ngo.html",
  volunteer: "volunteer.html",
  admin: "admin.html",
};

const loginTab = document.getElementById("tab-login");
const registerTab = document.getElementById("tab-register");
const loginForm = document.getElementById("login-form");
const registerForm = document.getElementById("register-form");
const forgotForm = document.getElementById("forgot-form");
const errorBox = document.getElementById("auth-error");
const ngoExtra = document.getElementById("ngo-extra");

// Already signed in? Go straight to the role dashboard.
(function redirectIfAuthed() {
  const token = localStorage.getItem("fr_token");
  const rawUser = localStorage.getItem("fr_user");
  if (!token || !rawUser) return;
  try {
    const user = JSON.parse(rawUser);
    if (user && ROLE_HOME[user.role]) window.location.href = ROLE_HOME[user.role];
  } catch {
    localStorage.removeItem("fr_user");
  }
})();

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
}

function clearError() {
  errorBox.classList.add("hidden");
}

function switchTab(showLogin) {
  loginTab.classList.toggle("active", showLogin);
  registerTab.classList.toggle("active", !showLogin);
  loginForm.classList.toggle("hidden", !showLogin);
  registerForm.classList.toggle("hidden", showLogin);
  forgotForm.classList.add("hidden");
  clearError();
}

loginTab.addEventListener("click", () => switchTab(true));
registerTab.addEventListener("click", () => switchTab(false));

// Role selection toggles the NGO extras and the per-role verification block.
const verifyBlocks = {
  donor: document.getElementById("verify-donor"),
  ngo: document.getElementById("verify-ngo"),
  volunteer: document.getElementById("verify-volunteer"),
};

function syncRoleFields() {
  const role = registerForm.querySelector('input[name="role"]:checked').value;
  ngoExtra.classList.toggle("hidden", role !== "ngo");
  for (const [name, block] of Object.entries(verifyBlocks)) {
    block.classList.toggle("hidden", name !== role);
  }
}

registerForm.querySelectorAll('input[name="role"]').forEach((radio) => {
  radio.addEventListener("change", syncRoleFields);
});
syncRoleFields();

// Household donors don't need a licence — soften the ID label live.
document.getElementById("reg-org-type").addEventListener("change", (event) => {
  const label = document.getElementById("reg-donor-id-label");
  label.textContent =
    event.target.value === "household"
      ? "Govt ID number (optional for households)"
      : "FSSAI / shop licence / govt ID number";
});

document.getElementById("btn-locate").addEventListener("click", () => {
  if (!navigator.geolocation) {
    showError("Geolocation is not supported by this browser.");
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (position) => {
      document.getElementById("reg-lat").value = position.coords.latitude.toFixed(6);
      document.getElementById("reg-lng").value = position.coords.longitude.toFixed(6);
      clearError();
    },
    () => showError("Could not read your location — please allow location access."),
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

// ---------------------------------------------------------------------------
// ID document upload — downscale client-side to a compact JPEG data URI
// ---------------------------------------------------------------------------

let idDocumentDataUri = null;

document.getElementById("reg-id-doc").addEventListener("change", (event) => {
  const file = event.target.files && event.target.files[0];
  const note = document.getElementById("reg-id-doc-note");
  idDocumentDataUri = null;
  note.classList.add("hidden");
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload = () => {
      const maxSide = 1280;
      const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      idDocumentDataUri = canvas.toDataURL("image/jpeg", 0.85);
      note.textContent = `✅ ${file.name} attached (${Math.round(idDocumentDataUri.length / 1364)} KB)`;
      note.classList.remove("hidden");
    };
    img.onerror = () => showError("That file does not look like an image.");
    img.src = reader.result;
  };
  reader.readAsDataURL(file);
});

async function postJson(path, body) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function finishAuth(data) {
  localStorage.setItem("fr_token", data.token);
  localStorage.setItem("fr_user", JSON.stringify(data.user));
  window.location.href = ROLE_HOME[data.user.role] || "auth.html";
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  try {
    const data = await postJson("/auth/login", {
      email: document.getElementById("login-email").value,
      password: document.getElementById("login-password").value,
    });
    finishAuth(data);
  } catch (error) {
    showError(error.message);
  }
});

// ---------------------------------------------------------------------------
// Forgot password — request a code, then set the new password
// ---------------------------------------------------------------------------

document.getElementById("link-forgot").addEventListener("click", (event) => {
  event.preventDefault();
  loginForm.classList.add("hidden");
  registerForm.classList.add("hidden");
  forgotForm.classList.remove("hidden");
  document.getElementById("forgot-step-1").classList.remove("hidden");
  document.getElementById("forgot-step-2").classList.add("hidden");
  clearError();
});

document.getElementById("link-back-login").addEventListener("click", (event) => {
  event.preventDefault();
  switchTab(true);
});

async function requestResetCode() {
  const email = document.getElementById("forgot-email").value.trim();
  if (!email) throw new Error("Please enter your account email first.");
  const data = await postJson("/auth/forgot-password", { email });
  const note = document.getElementById("forgot-sent-note");
  if (data.dev_code) {
    // Local development without an email provider — surface the code.
    note.textContent = `⚙️ Dev mode (no email service configured): your code is ${data.dev_code}`;
  } else {
    note.textContent = "📬 A reset code is on its way to your inbox. It expires in 15 minutes.";
  }
  document.getElementById("forgot-step-1").classList.add("hidden");
  document.getElementById("forgot-step-2").classList.remove("hidden");
}

forgotForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const onStepOne = !document.getElementById("forgot-step-1").classList.contains("hidden");
  try {
    if (onStepOne) {
      await requestResetCode();
      return;
    }
    const data = await postJson("/auth/reset-password", {
      email: document.getElementById("forgot-email").value.trim(),
      code: document.getElementById("forgot-code").value.trim(),
      new_password: document.getElementById("forgot-new-password").value,
    });
    if (window.frUI) frUI.toast(data.message || "Password updated!", "success");
    switchTab(true);
    document.getElementById("login-email").value = document.getElementById("forgot-email").value.trim();
  } catch (error) {
    showError(error.message);
  }
});

document.getElementById("btn-resend-code").addEventListener("click", async () => {
  clearError();
  try {
    await requestResetCode();
    if (window.frUI) frUI.toast("A fresh code was sent.", "info");
  } catch (error) {
    showError(error.message);
  }
});

// ---------------------------------------------------------------------------
// Registration — profile + role-specific verification details
// ---------------------------------------------------------------------------

function buildVerificationPayload(role) {
  const verification = { phone: document.getElementById("reg-phone").value.trim() };
  if (role === "donor") {
    verification.org_type = document.getElementById("reg-org-type").value;
    verification.business_name = document.getElementById("reg-business-name").value.trim();
    verification.id_number = document.getElementById("reg-donor-id").value.trim();
  } else if (role === "ngo") {
    verification.registration_number = document.getElementById("reg-ngo-reg").value.trim();
    verification.darpan_id = document.getElementById("reg-ngo-darpan").value.trim();
  } else {
    verification.driving_licence = document.getElementById("reg-licence").value.trim();
    verification.vehicle_type = document.getElementById("reg-vehicle").value;
  }
  if (idDocumentDataUri) verification.id_document = idDocumentDataUri;
  return verification;
}

// Pre-submit validation — catch junk before it wastes a round-trip (the
// server independently enforces every rule, this is just faster feedback).
function validateRegistration(role) {
  const fail = (id, message) => {
    const field = document.getElementById(id);
    if (field) field.focus();
    return message;
  };
  const name = document.getElementById("reg-name").value.trim();
  if (name.length < 3) return fail("reg-name", "Please enter your real full name / organization name.");
  const email = document.getElementById("reg-email").value.trim();
  if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(email)) return fail("reg-email", "That email address doesn't look valid.");
  const password = document.getElementById("reg-password").value;
  if (password.length < 8) return fail("reg-password", "Password needs at least 8 characters.");
  if (!(/[A-Za-z]/.test(password) && /[0-9]/.test(password)))
    return fail("reg-password", "Password needs at least one letter and one number.");
  const address = document.getElementById("reg-address").value.trim();
  if (address.length < 4) return fail("reg-address", "Please enter your full address — collectors navigate to it.");

  const phone = document.getElementById("reg-phone").value.trim();
  if (!/^\+?[0-9][0-9 \-]{5,17}[0-9]$/.test(phone))
    return fail("reg-phone", "Please enter a valid contact phone number.");
  if (new Set(phone.replace(/\D/g, "")).size < 4)
    return fail("reg-phone", "That phone number doesn't look real — enter your actual number.");

  const hasDigits = (v, n) => (v.match(/[0-9]/g) || []).length >= n;
  if (role === "donor") {
    const orgType = document.getElementById("reg-org-type").value;
    const id = document.getElementById("reg-donor-id").value.trim();
    if (orgType !== "household" && (id.length < 5 || !hasDigits(id, 2)))
      return fail("reg-donor-id", "Businesses must provide their FSSAI / shop-licence / govt ID number (it contains digits).");
  } else if (role === "ngo") {
    const reg = document.getElementById("reg-ngo-reg").value.trim();
    if (reg.length < 5 || !hasDigits(reg, 2))
      return fail("reg-ngo-reg", "Please enter your NGO's real registration number (it contains digits).");
  } else {
    const licence = document.getElementById("reg-licence").value.trim().toUpperCase();
    if (licence.length < 5 || !hasDigits(licence, 2))
      return fail("reg-licence", "Please enter your real driving licence number.");
    if (!/^[A-Z]{2}[ -]?[0-9]{1,2}/.test(licence))
      return fail("reg-licence", "A driving licence starts with the state code + RTO number, e.g. MP09 20210012345.");
  }
  return null;
}

registerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();

  const latitude = document.getElementById("reg-lat").value;
  const longitude = document.getElementById("reg-lng").value;
  if (!latitude || !longitude) {
    showError('Please click "Use my location" so we can match you geographically.');
    return;
  }

  const role = registerForm.querySelector('input[name="role"]:checked').value;
  const problem = validateRegistration(role);
  if (problem) {
    showError(problem);
    return;
  }
  const payload = {
    name: document.getElementById("reg-name").value.trim(),
    email: document.getElementById("reg-email").value.trim(),
    password: document.getElementById("reg-password").value,
    role,
    address: document.getElementById("reg-address").value.trim(),
    latitude,
    longitude,
    verification: buildVerificationPayload(role),
  };
  if (role === "ngo") {
    payload.partner_type = document.getElementById("reg-partner-type").value;
    payload.accepts_non_veg = document.getElementById("reg-accepts-nonveg").checked;
    payload.has_cold_storage = document.getElementById("reg-cold-storage").checked;
  }
  const referral = document.getElementById("reg-referral").value.trim().toUpperCase();
  if (referral) payload.referral_code = referral;

  try {
    const data = await postJson("/auth/register", payload);
    finishAuth(data);
  } catch (error) {
    showError(error.message);
  }
});
