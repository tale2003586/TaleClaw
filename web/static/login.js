const state = {
  mode: "login",
  registrationEnabled: false,
  busy: false,
};

const els = {
  form: document.querySelector("#authForm"),
  tabs: [...document.querySelectorAll("[data-auth-mode]")],
  username: document.querySelector("#username"),
  password: document.querySelector("#password"),
  confirmField: document.querySelector("#confirmField"),
  confirmPassword: document.querySelector("#confirmPassword"),
  submit: document.querySelector("#authSubmit"),
  error: document.querySelector("#authError"),
};

function nextPath() {
  const value = new URLSearchParams(window.location.search).get("next") || "/";
  return value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

function setBusy(value) {
  state.busy = value;
  els.submit.disabled = value;
  els.username.disabled = value;
  els.password.disabled = value;
  els.confirmPassword.disabled = value;
  els.submit.textContent = value
    ? "请稍候"
    : state.mode === "register" ? "创建账号" : "登录";
}

function setMode(mode) {
  if (mode === "register" && !state.registrationEnabled) return;
  state.mode = mode;
  els.confirmField.hidden = mode !== "register";
  els.confirmPassword.required = mode === "register";
  els.password.autocomplete = mode === "register" ? "new-password" : "current-password";
  els.error.textContent = "";
  for (const tab of els.tabs) {
    const active = tab.dataset.authMode === mode;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
  setBusy(false);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function submitAuth() {
  const username = els.username.value.trim();
  const password = els.password.value;
  if (state.mode === "register" && password !== els.confirmPassword.value) {
    els.error.textContent = "两次输入的密码不一致。";
    return;
  }

  setBusy(true);
  els.error.textContent = "";
  try {
    await fetchJson(`/api/auth/${state.mode}`, {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    window.location.assign(nextPath());
  } catch (error) {
    els.error.textContent = error.message;
    setBusy(false);
  }
}

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!state.busy) submitAuth();
});

for (const tab of els.tabs) {
  tab.addEventListener("click", () => setMode(tab.dataset.authMode));
}

async function init() {
  const config = await fetchJson("/api/auth/config");
  state.registrationEnabled = Boolean(config.registration_enabled);
  const registerTab = els.tabs.find((tab) => tab.dataset.authMode === "register");
  registerTab.hidden = !state.registrationEnabled;

  const me = await fetch("/api/auth/me");
  if (me.ok) {
    window.location.assign(nextPath());
    return;
  }
  setMode("login");
  els.username.focus();
}

init().catch((error) => {
  els.error.textContent = error.message;
});
