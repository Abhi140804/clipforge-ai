/* ClipForge frontend core: DOM helper, API client, router, shell, auth. No framework, no build step, no innerHTML with data. */
"use strict";

/* Native append()/prepend() stringify arrays and write "null"; make them accept arrays and skip empty children. */
for (const m of ["append", "prepend"]) {
  const native = Element.prototype[m];
  Element.prototype[m] = function (...kids) { return native.apply(this, kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false)); };
}

const state = { user: null, meta: null, timers: [], signupOpen: false, firstRun: false };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (["value", "checked", "disabled", "selected", "indeterminate"].includes(k)) el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  const add = (c) => {
    if (Array.isArray(c)) c.forEach(add);
    else if (c === null || c === undefined || c === false) return;
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return el;
}
const $ = (s, r = document) => r.querySelector(s);
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

const ICONS = {
  home: "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  plus: "M12 5v14M5 12h14", folder: "M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  film: "M4 4h16v16H4zM8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4", sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
  text: "M5 6V4h14v2M12 4v16M9 20h6", send: "M22 2 11 13M22 2l-7 20-4-9-9-4z", gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  cpu: "M6 6h12v12H6zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4", play: "M8 5v14l11-7z",
};
function icon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "1.8"); s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", ICONS[name] || ""); s.append(p);
  return s;
}

class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function api(method, path, body, opts = {}) {
  const init = { method, headers: { "X-CF-CSRF": "1" }, credentials: "same-origin" };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, init); } catch (e) { throw new ApiError("Cannot reach the ClipForge server. Is it still running?", 0); }
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-JSON */ }
  if (r.status === 401 && !opts.quiet) { state.user = null; boot(); throw new ApiError("Please sign in.", 401); }
  if (!r.ok) throw new ApiError((data && data.error) || r.statusText, r.status);
  return data;
}
const GET = (p) => api("GET", p), POST = (p, b) => api("POST", p, b === undefined ? {} : b), PATCH = (p, b) => api("PATCH", p, b), PUT = (p, b) => api("PUT", p, b), DEL = (p) => api("DELETE", p);

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
async function guard(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { if (e.status !== 401) toast(e.message, true); return undefined; }
}
function modal(title, body, actions) {
  const bg = h("div", { class: "modal-bg", onclick: (e) => { if (e.target === bg) bg.remove(); } });
  const close = () => bg.remove();
  bg.append(h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title }, h("h2", { style: "margin-bottom:14px" }, title), body,
    h("div", { class: "row", style: "margin-top:18px;justify-content:flex-end" }, actions(close))));
  document.body.append(bg);
  const first = bg.querySelector("input,select,textarea,button.primary"); if (first) first.focus();
  return close;
}
function confirmBox(title, text, yes, onYes) {
  modal(title, h("p", { class: "muted" }, text), (close) => [h("button", { onclick: close }, "Keep it"),
    h("button", { class: "primary", onclick: () => { close(); onYes(); } }, yes)]);
}

const fmtTC = (s) => { s = Math.max(0, Math.round(s || 0)); const h_ = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return (h_ ? h_ + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); };
const fmtDate = (t) => t ? new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
function ago(t) {
  const d = Date.now() / 1000 - t;
  if (d < 60) return "just now"; if (d < 3600) return Math.floor(d / 60) + " min ago";
  if (d < 86400) return Math.floor(d / 3600) + " h ago"; return Math.floor(d / 86400) + " d ago";
}
const fmtBytes = (b) => b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.round(b / 1e3) + " KB";
const STATUS = {
  queued: ["Queued", "info"], running: ["Running", "run"], completed: ["Completed", "ok"], failed: ["Failed", "bad"], cancelled: ["Cancelled", ""],
  needs_upload: ["Needs file", "warn"], pending: ["Pending", "info"], rendering: ["Rendering", "run"], ready: ["Ready", "ok"],
  pending_approval: ["Awaiting approval", "warn"], uploading: ["Uploading", "run"], published: ["Published", "ok"], scheduled: ["Scheduled on YouTube", "info"],
  rate_limited: ["Waiting for quota", "warn"],
};
const pill = (s) => { const [t, c] = STATUS[s] || [s, ""]; return h("span", { class: "pill " + c }, t); };

function page(title, sub, actions) {
  return h("div", { class: "head" }, h("div", {}, h("h1", {}, title), sub ? h("p", {}, sub) : null), h("div", { class: "row" }, actions));
}
function every(ms, fn) { const id = setInterval(fn, ms); state.timers.push(id); return id; }

/* ---------- router ---------- */
const routes = [];
const route = (re, fn, nav) => routes.push({ re, fn, nav });
async function renderRoute() {
  state.timers.forEach(clearInterval); state.timers = [];
  const hash = location.hash.replace(/^#/, "") || "/";
  const path = hash.split("?")[0];
  const query = Object.fromEntries(new URLSearchParams(hash.split("?")[1] || ""));
  const main = $("#main"); if (!main) return;
  for (const r of routes) {
    const m = path.match(r.re);
    if (m) {
      document.querySelectorAll(".rail a.nav").forEach((a) => a.classList.toggle("on", a.dataset.nav === r.nav));
      clear(main);
      try { await r.fn(main, m.slice(1), query); } catch (e) { if (e.status !== 401) main.append(h("div", { class: "notice bad" }, e.message)); }
      window.scrollTo(0, 0);
      return;
    }
  }
  clear(main).append(h("div", { class: "empty" }, h("h3", {}, "Page not found"), h("a", { href: "#/" }, "Back to the dashboard")));
}
window.addEventListener("hashchange", renderRoute);

/* ---------- shell + auth ---------- */
const NAV = [["/", "Dashboard", "home"], ["/new", "New project", "plus"], ["/projects", "Projects", "folder"], ["/clips", "Clips", "film"],
  ["/studio", "Content studio", "text"], ["/publishing", "Publishing", "send"], ["/settings", "Settings", "gear"]];

function shell() {
  const root = clear($("#root"));
  root.append(h("div", { class: "app" },
    h("nav", { class: "rail", "aria-label": "Main" }, h("div", { class: "brand" }, h("i"), "ClipForge"),
      NAV.map(([p, label, ic]) => h("a", { class: "nav", href: "#" + p, "data-nav": p }, icon(ic), label)),
      h("div", { class: "spacer" }), h("div", { class: "who" }, state.user.email),
      h("a", { class: "nav", href: "#", onclick: async (e) => { e.preventDefault(); await POST("/api/auth/logout"); state.user = null; boot(); } }, "Sign out")),
    h("main", { id: "main" })));
}

function authView() {
  const root = clear($("#root"));
  const signup = state.firstRun;
  const email = h("input", { type: "email", autocomplete: "email", required: true, placeholder: "you@example.com" });
  const pw = h("input", { type: "password", autocomplete: signup ? "new-password" : "current-password", required: true, minlength: signup ? 10 : 1 });
  const err = h("div", { class: "notice bad", style: "display:none;margin-bottom:12px" });
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault(); err.style.display = "none";
    try { const r = await POST(signup ? "/api/auth/signup" : "/api/auth/login", { email: email.value, password: pw.value }); state.user = r.user; boot(); }
    catch (ex) { err.textContent = ex.message; err.style.display = "block"; }
  } }, err, h("div", { class: "stack" },
    h("label", { class: "f" }, h("span", {}, "Email"), email),
    h("label", { class: "f" }, h("span", {}, "Password"), pw, signup ? h("span", { class: "hint" }, "At least 10 characters.") : null),
    h("button", { class: "primary", type: "submit", style: "justify-content:center" }, signup ? "Create account" : "Sign in")));
  root.append(h("div", { class: "auth" }, h("div", { class: "panel" }, h("div", { class: "brand", style: "padding:0 0 18px" }, h("i"), "ClipForge"),
    h("h1", {}, signup ? "Create the owner account" : "Sign in"),
    h("p", { class: "muted", style: "margin-top:6px" }, signup ? "This is the first run. The first account owns this server; sign-ups close afterwards." : "Welcome back."), form)));
}

async function boot() {
  try {
    const me = await api("GET", "/api/auth/me", undefined, { quiet: true });
    state.user = me.user; state.firstRun = me.first_run; state.signupOpen = me.signup_open;
  } catch (e) { clear($("#root")).append(h("div", { class: "auth" }, h("div", { class: "notice bad" }, e.message))); return; }
  if (!state.user) return authView();
  state.meta = await GET("/api/meta");
  shell();
  renderRoute();
}
