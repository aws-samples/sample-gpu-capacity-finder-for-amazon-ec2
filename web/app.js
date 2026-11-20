"use strict";

const $ = (id) => document.getElementById(id);
let cfg = null;      // deployment values from /config.json
let limits = null;   // regions, families, and feature switches from /api/config
// The saved instance types ("instance list"): kept in this browser only, never sent to the server.
const LIST_KEY = "myInstanceTypes";
const LIST_MAX = 20;         // types a user may keep on the list
const STORED_MAX = 200;      // most entries read back from storage
const TYPE_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+(?:-[a-z0-9]+)*$/;
// Keep the valid, distinct names (at most STORED_MAX); anything else, or a value that is not an array, gives an empty list.
const cleanList = (v) => (Array.isArray(v)
  ? [...new Set(v.filter((x) => typeof x === "string" && TYPE_NAME_RE.test(x)))].slice(0, STORED_MAX) : []);
function loadList() {
  try { return cleanList(JSON.parse(localStorage.getItem(LIST_KEY))); } catch { return []; }
}
let watchlist = loadList();
// Capacity Block durations EC2 accepts: 1 to 14 days, then 7-day steps up to 182.
const BLOCK_DAYS = [...Array(14).keys()].map((i) => i + 1).concat([...Array(24).keys()].map((i) => 21 + 7 * i));

// --- tiny DOM helpers: text only, never innerHTML ---------------------------
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, text = null) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== null) el.textContent = text;
  return el;
}

// --- strings: one JSON file per locale in i18n/, English as the fallback ----
// Locales with a file in i18n/. A locale missing here stays hidden in the language list.
const LOCALES = ["en-US", "zh-TW", "zh-CN"];
const DEFAULT_LOCALE = "en-US";
// Used only when the string files cannot load, so the page can still say what went wrong.
const BUILT_IN = { "boot.error": "Couldn't load the page. {error}", "boot.retry": "Try again" };
let locale = DEFAULT_LOCALE;
let dict = {};
let fallback = {};
let pluralRules = new Intl.PluralRules(DEFAULT_LOCALE);
let formatters = new Map();

function matchLocale(tag) {
  if (!tag) return null;
  const want = String(tag).toLowerCase();
  const exact = LOCALES.find((l) => l.toLowerCase() === want);
  if (exact) return exact;
  const lang = want.split("-")[0];
  if (lang === "zh") {
    // Traditional script for Taiwan, Hong Kong, Macao, or an explicit Hant tag; Simplified for every other Chinese tag.
    const chosen = !/-hans(-|$)/.test(want) && /-(tw|hk|mo|hant)(-|$)/.test(want) ? "zh-TW" : "zh-CN";
    return LOCALES.includes(chosen) ? chosen : null;
  }
  return LOCALES.find((l) => l.toLowerCase().split("-")[0] === lang) || null;
}

function storedLocale() {
  try { return localStorage.getItem("lang"); } catch { return null; }
}

// ?lang= first, then the choice saved in this browser, then the browser languages.
function pickLocale() {
  const fromQuery = new URLSearchParams(location.search).get("lang");
  const nav = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language];
  for (const tag of [fromQuery, storedLocale(), ...nav]) {
    const found = matchLocale(tag);
    if (found) return found;
  }
  return DEFAULT_LOCALE;
}

async function loadStrings(code) {
  // "no-cache" makes the browser ask for a newer file on every load (a cheap 304 when nothing changed). Without it a browser can keep an
  // old language file after a release and show a new key as raw text next to the new script.
  const resp = await fetch(`i18n/${code}.json`, { cache: "no-cache" });
  if (!resp.ok) throw new Error(`i18n/${code}.json (${resp.status})`);
  return resp.json();
}

async function setLocale(code) {
  if (!Object.keys(fallback).length) fallback = await loadStrings(DEFAULT_LOCALE);
  // A file that does not load leaves the page in English rather than half translated.
  try { dict = code === DEFAULT_LOCALE ? fallback : await loadStrings(code); } catch { dict = fallback; code = DEFAULT_LOCALE; }
  locale = code;
  pluralRules = new Intl.PluralRules(code);
  formatters = new Map();
  document.documentElement.lang = code;
  $("lang").value = code;
  applyStatic();
}

// t("key", { name: value }): {name} placeholders; a numeric `count` picks key.one / key.other.
function t(key, params = {}) {
  const look = (k) => (k in dict ? dict[k] : k in fallback ? fallback[k] : undefined);
  let text;
  if (typeof params.count === "number") text = look(`${key}.${pluralRules.select(params.count)}`) ?? look(`${key}.other`);
  text = text ?? look(key) ?? BUILT_IN[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (m, name) => (name in params ? paramText(params[name]) : m));
}
// A message with several points keeps one point per line; fillPoints shows them as a bullet list.
function fillPoints(el, text) {
  const parts = String(text ?? "").split("\n").filter(Boolean);
  if (parts.length < 2) el.textContent = parts[0] ?? "";
  else el.replaceChildren(h("ul", { class: "points" }, ...parts.flatMap((x, i) => (i ? ["\n", h("li", {}, x)] : [h("li", {}, x)]))));
}
const noteEl = (text) => { const el = h("p", { class: "muted" }); fillPoints(el, text); return el; };
const paramText = (v) => (typeof v === "number" ? fmtNumber(v) : String(v));

// Static text in index.html: data-i18n="key" sets the text, data-i18n-attr="attr:key;attr:key" sets attributes.
// A busy button keeps its spinner; busyButton puts the text back in the active locale when the work ends.
function applyStatic(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]:not([aria-busy=true])")) fillPoints(el, t(el.getAttribute("data-i18n")));
  for (const el of root.querySelectorAll("[data-i18n-attr]")) {
    for (const pair of el.getAttribute("data-i18n-attr").split(";")) {
      const [attr, key] = pair.split(":").map((s) => s.trim());
      if (attr && key) el.setAttribute(attr, t(key));
    }
  }
}

// --- formatting in the active locale ----------------------------------------
function cached(kind, opts, make) {
  const k = kind + JSON.stringify(opts);
  if (!formatters.has(k)) formatters.set(k, make());
  return formatters.get(k);
}
const nf = (opts) => cached("n", opts, () => new Intl.NumberFormat(locale, opts));
const dtf = (opts) => cached("d", opts, () => new Intl.DateTimeFormat(locale, opts));
const rtf = () => cached("r", {}, () => new Intl.RelativeTimeFormat(locale, { numeric: "auto" }));
const fmtNumber = (v) => nf({ maximumFractionDigits: 1 }).format(v);
const fmtPercent = (p) => nf({ style: "percent", maximumFractionDigits: 1 }).format(p / 100);
function fmtMoney(amount, currency, digits = 2) {
  const n = Number(amount);
  if (!Number.isFinite(n)) return String(amount);
  try {
    return nf({ style: "currency", currency: currency || "USD", minimumFractionDigits: 2, maximumFractionDigits: digits }).format(n);
  } catch { return `${nf({ maximumFractionDigits: digits }).format(n)} ${currency || ""}`.trim(); }
}
// Date and time without seconds, with the browser's time zone abbreviation.
const DATE_TIME = { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" };
const fmtDate = (v) => dtf(DATE_TIME).format(new Date(v));
// The same text in two parts that each stay on one line, so a narrow cell breaks between the date and the time only.
function dateTimeNode(v) {
  const parts = dtf(DATE_TIME).formatToParts(new Date(v));
  const i = parts.findIndex((p) => ["hour", "minute", "dayPeriod", "timeZoneName"].includes(p.type));
  if (i <= 0) return fmtDate(v);
  const head = parts.slice(0, i).map((p) => p.value).join("");
  const day = head.replace(/\s+$/, "");
  return h("span", { class: "when" }, h("span", {}, day), head.length > day.length ? " " : "", h("span", {}, parts.slice(i).map((p) => p.value).join("")));
}
function fmtRelative(v) {
  const secs = (new Date(v).getTime() - Date.now()) / 1000;
  const abs = Math.abs(secs);
  if (abs < 60) return secs < 0 ? t("time.justNow") : rtf().format(1, "minute");
  const [value, unit] = abs < 3600 ? [secs / 60, "minute"] : abs < 86400 ? [secs / 3600, "hour"] : [secs / 86400, "day"];
  return rtf().format(Math.round(value), unit);
}
// --- one freshness model: "Updated 3 minutes ago" from the answer's own fetchedAt ---
const FRESH_EVERY_MS = 60e3;   // one timer re-renders every relative label
const FEW_MINUTES = 5;         // stamps this close together read as one time
const stampMs = (s) => (typeof s === "string" && s ? new Date(s).getTime() : NaN);
const validStamps = (stamps) => (stamps || []).filter((s) => Number.isFinite(stampMs(s)));
const ageMinutes = (s, now = Date.now()) => Math.max(0, (now - stampMs(s)) / 60e3);
const nowIso = () => new Date().toISOString();
// The minutes after which data on screen counts as old, from /api/config.
const MINUTES = { scores: () => limits?.spotScoreCacheMinutes ?? 15, quotas: () => limits?.quotaCacheMinutes ?? 60,
  spotPrice: () => limits?.priceCache?.spotMinutes ?? 30, odPrice: () => (limits?.priceCache?.onDemandHours ?? 24) * 60,
  offerings: () => (limits?.offeringCacheHours ?? 6) * 60 };
const olderThan = (stamps, minutes) => validStamps(stamps).some((s) => ageMinutes(s) > minutes);
const unitOf = (m) => (m < 60 ? "minute" : m < 1440 ? "hour" : "day");
const unitMinutes = { minute: 1, hour: 60, day: 1440 };
const RANGE_KEY = { minute: "fresh.rangeMinutes", hour: "fresh.rangeHours", day: "fresh.rangeDays" };

// { text, title } for the stamps of one block: "Updated just now", "Updated 3 minutes ago", or
// "Updated 3 to 25 minutes ago" when the stamps differ by more than a few minutes. The title has the absolute time.
function freshInfo(stamps) {
  const have = validStamps(stamps).sort((a, b) => stampMs(a) - stampMs(b));
  if (!have.length) return { text: "", title: "" };
  const now = Date.now();
  const oldest = ageMinutes(have[0], now), newest = ageMinutes(have[have.length - 1], now);
  if (oldest < 1) return { text: t("fresh.justNow"), title: fmtDate(have[0]) };
  const unit = unitOf(oldest);
  const high = Math.floor(oldest / unitMinutes[unit]);
  const low = Math.max(1, Math.floor(newest / unitMinutes[unit]));
  if (oldest - newest > FEW_MINUTES && unitOf(newest) === unit && low < high) {
    return { text: t(RANGE_KEY[unit], { low, high }), title: fmtList([fmtDate(have[0]), fmtDate(have[have.length - 1])]) };
  }
  return { text: t("fresh.updated", { when: rtf().format(-high, unit) }), title: fmtDate(have[0]) };
}

// The Refresh answer of a cached route: refreshed false with retryAfterSeconds when the server refused to ask AWS again
// this soon. refreshed false without it (the lock was taken, but nothing could be asked) is no wait.
// Returns the seconds to wait (the longest one), or 0 when nothing was blocked.
function blockedSeconds(answers) {
  let secs = 0;
  for (const r of answers) {
    const n = r && r.refreshed === false && typeof r.retryAfterSeconds === "number" ? r.retryAfterSeconds : 0;
    if (Number.isFinite(n) && n > 0) secs = Math.max(secs, Math.ceil(n));
  }
  return secs;
}
// The stamp to show for an answer: the server's own fetchedAt, or "now" right after a refresh that reached AWS.
const stampOf = (r, forced) => (forced && r.refreshed === true ? nowIso() : r.fetchedAt);
// The note keeps its place whether or not it has text (CSS reserves the room), so showing it moves nothing.
function setRefreshNote(id, secs) {
  const el = $(id);
  el.textContent = secs ? t("fresh.blocked", { seconds: secs }) : "";
  el.classList.toggle("is-empty", !secs);
}
// A Refresh button stays enabled while its tab loads (keyboard focus stays on it); the click does nothing then.
function setRefreshBusy(id, busy) {
  $(id).setAttribute("aria-busy", String(Boolean(busy)));
}

const scoreClass = (s) => (s >= 7 ? "s-hi" : s >= 4 ? "s-mid" : "s-lo");
const scoreWord = (s) => (s >= 7 ? t("score.likely") : s >= 4 ? t("score.possible") : t("score.unlikely"));
const familyOf = (type) => (type ? type.split(".", 1)[0] : "");
const sizeOf = (type) => { const s = String(type || ""); const i = s.indexOf("."); return i < 0 ? "" : s.slice(i + 1); };

// --- natural order for instance types ----------------------------------------
// Family: letter runs and digit runs compared in turn (letters alphabetically, digits as numbers), so
// g6e < g7 < p4d < p5 < p5e < p5en < p6-b200 < p6-b300. Size: nano ... xlarge, then {N}xlarge by N, then metal
// variants by number; unknown sizes last, alphabetically.
const SIZE_WORDS = ["nano", "micro", "small", "medium", "large", "xlarge"];
const strCmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
function sizeKey(size) {
  const i = SIZE_WORDS.indexOf(size);
  if (i >= 0) return [0, i, ""];
  let m = /^(\d+)xlarge$/.exec(size);
  if (m) return [1, Number(m[1]), ""];
  m = /^metal(?:-(\d+)xl)?$/.exec(size);
  if (m) return [2, m[1] ? Number(m[1]) : 0, ""];
  return [3, 0, size];
}
function compareFamilies(a, b) {
  const x = String(a).toLowerCase().match(/[a-z]+|\d+/g) || [], y = String(b).toLowerCase().match(/[a-z]+|\d+/g) || [];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const dx = /^\d/.test(x[i]), dy = /^\d/.test(y[i]);
    const d = dx && dy ? Number(x[i]) - Number(y[i]) : dx !== dy ? (dx ? -1 : 1) : strCmp(x[i], y[i]);
    if (d) return d;
  }
  return x.length - y.length || strCmp(a, b);
}
function compareTypes(a, b) {
  const ka = sizeKey(sizeOf(a)), kb = sizeKey(sizeOf(b));
  return compareFamilies(familyOf(a), familyOf(b)) || ka[0] - kb[0] || ka[1] - kb[1] || strCmp(ka[2], kb[2]) || strCmp(a, b);
}
const sortTypes = (types) => [...types].sort(compareTypes);
// Family -> its types, both in natural order.
function groupByFamily(types) {
  const groups = new Map();
  for (const x of sortTypes(types)) {
    const f = familyOf(x);
    if (!groups.has(f)) groups.set(f, []);
    groups.get(f).push(x);
  }
  return groups;
}
// Region codes, instance types, and other names in a sentence: ", " in English, the locale's own separator otherwise.
const fmtList = (items) => items.join(t("common.listSep"));
// Sentences side by side: a space after English punctuation, nothing after a full-width mark such as "。".
const joinSentences = (parts) => parts.filter(Boolean).reduce((out, p) => (!out ? p : /[\u3000-\u303f\uff00-\uffef]$/.test(out) ? out + p : `${out} ${p}`), "");

// Text that follows the active locale: the message is worked out each time it is read, so a view that
// shows an error again after a language change shows it in the new language.
class TextError extends Error {
  constructor(key, params = {}) { super(); this.key = key; this.params = params; }
  get message() { return t(this.key, this.params); }
}

// --- sign-in: Cognito hosted UI, authorization code flow with PKCE ---------
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const randomString = () => b64url(crypto.getRandomValues(new Uint8Array(48)));

// Tokens live in sessionStorage only: tab-scoped, gone when the tab closes, never in localStorage.
const TOKEN_KEYS = ["idToken", "refreshToken", "idTokenExp"];
const RENEW_BEFORE_MS = 2 * 60e3;
const REVOKE_WAIT_MS = 3000;

// The exp claim of a JWT in milliseconds, or null. Read only to schedule renewal; the API verifies the signature.
function jwtExpiry(token) {
  try {
    const part = String(token).split(".")[1] || "";
    if (!/^[A-Za-z0-9_-]+$/.test(part)) return null;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const exp = JSON.parse(atob(b64)).exp;
    return Number.isFinite(exp) && exp > 0 ? exp * 1000 : null;
  } catch { return null; }
}

// Store a token endpoint answer. The refresh grant returns no new refresh token, so the old one is kept.
function saveTokens(data, refreshToken = null) {
  if (!data || typeof data.id_token !== "string" || !data.id_token) return false;
  sessionStorage.setItem("idToken", data.id_token);
  const keep = typeof data.refresh_token === "string" && data.refresh_token ? data.refresh_token : refreshToken;
  if (keep) sessionStorage.setItem("refreshToken", keep); else sessionStorage.removeItem("refreshToken");
  const exp = jwtExpiry(data.id_token) ?? (Number(data.expires_in) > 0 ? Date.now() + Number(data.expires_in) * 1000 : null);
  if (exp) sessionStorage.setItem("idTokenExp", String(exp)); else sessionStorage.removeItem("idTokenExp");
  return true;
}

function clearTokens() {
  for (const k of TOKEN_KEYS) sessionStorage.removeItem(k);
}

function tokenNearExpiry() {
  const exp = Number(sessionStorage.getItem("idTokenExp"));
  return exp > 0 && exp - Date.now() < RENEW_BEFORE_MS;
}

// Get a new ID token with the refresh token. Parallel callers share one request. Resolves true when renewed,
// false when Cognito refuses (expired or revoked): the tokens are cleared and the re-login path runs once
// for all callers. A network failure rejects and keeps the tokens.
let renewing = null;
function renewSession() {
  if (!renewing) {
    renewing = (async () => {
      const refreshToken = sessionStorage.getItem("refreshToken");
      if (!refreshToken) return false;
      let resp;
      try {
        resp = await fetch(`${cfg.cognitoDomain}/oauth2/token`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ grant_type: "refresh_token", client_id: cfg.clientId, refresh_token: refreshToken }),
        });
      } catch { throw new ApiError(0, { code: "network" }); }
      const data = resp.ok ? await resp.json().catch(() => ({})) : {};
      if (saveTokens(data, refreshToken)) return true;
      sessionEnded();
      return false;
    })().finally(() => { renewing = null; });
  }
  return renewing;
}

async function signIn() {
  const verifier = randomString();
  const state = randomString();
  // Keep the selected tab across the round trip through the hosted page.
  sessionStorage.setItem("pkce", JSON.stringify({ verifier, state, hash: location.hash }));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const q = new URLSearchParams({
    response_type: "code", client_id: cfg.clientId, redirect_uri: cfg.siteUrl,
    scope: "openid email", state, code_challenge: challenge, code_challenge_method: "S256",
  });
  location.assign(`${cfg.cognitoDomain}/oauth2/authorize?${q}`);
}

// Take the single-use code and state out of the address bar, so a reload or "Try again" cannot send them twice.
function dropSignInParams() {
  const url = new URL(location.href);
  for (const k of ["code", "state", "error", "error_description"]) url.searchParams.delete(k);
  history.replaceState({}, "", url.pathname + url.search + url.hash);
}

async function finishSignIn(code, state) {
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem("pkce") || "{}") || {}; } catch { saved = {}; }
  sessionStorage.removeItem("pkce");
  dropSignInParams();
  if (!saved.verifier || saved.state !== state) throw new TextError("err.signInState");
  let resp;
  try {
    resp = await fetch(`${cfg.cognitoDomain}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code", client_id: cfg.clientId, code,
        redirect_uri: cfg.siteUrl, code_verifier: saved.verifier,
      }),
    });
  } catch { throw new ApiError(0, { code: "network" }); }
  const data = resp.ok ? await resp.json().catch(() => ({})) : {};
  if (!saveTokens(data)) throw new TextError("err.signInToken");
  history.replaceState({}, "", cfg.siteUrl + (/^#[a-z-]+$/.test(saved.hash || "") ? saved.hash : ""));
}

async function signOut() {
  const refreshToken = sessionStorage.getItem("refreshToken");
  clearTokens();
  sessionStorage.removeItem("reauth");
  // Best effort: revoke the refresh token so it cannot renew the session; never block sign-out on it.
  if (refreshToken) {
    const revoke = fetch(`${cfg.cognitoDomain}/oauth2/revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ token: refreshToken, client_id: cfg.clientId }),
    }).catch(() => null);
    await Promise.race([revoke, new Promise((r) => setTimeout(r, REVOKE_WAIT_MS))]);
  }
  location.assign(`${cfg.cognitoDomain}/logout?${new URLSearchParams({ client_id: cfg.clientId, logout_uri: cfg.siteUrl })}`);
}

// --- API: one shared progress bar while any request is in flight -----------
let busy = 0;
function setBusy(delta) {
  busy = Math.max(0, busy + delta);
  $("busy-bar").hidden = busy === 0;
}

// Error codes from the API, mapped to page text. An unknown code shows the API's English message.
const labelRegion = (p) => (p && typeof p.region === "string" ? { ...p, region: regionLabel(p.region) } : p);
const ERROR_TEXT = {
  "region-not-enabled": (p) => t("err.regionNotEnabled", labelRegion(p)),
  "type-not-enabled": (p) => t("err.typeNotEnabled", p),
  "type-not-offered": (p) => t("err.typeNotOffered", labelRegion(p)),
  "bad-parameter": (p) => t("err.badParameter", p),
  "out-of-range": (p) => t("err.outOfRange", p),
  "bad-days": (p) => t("err.badDays", p),
  "bad-date": (p) => t("err.badDate", p),
  "date-too-far": (p) => t("err.dateTooFar", p),
  "bad-body": (p) => t("err.badBody", p),
  "no-identity": (p) => t("err.noIdentity", p),
  "not-found": (p) => t("err.notFound", p),
  "list-too-long": (p) => t("err.listTooLong", p),
  "probes-disabled": (p) => t("err.probesDisabled", p),
  "spot-probes-disabled": (p) => t("err.spotProbesDisabled", p),
  "probe-daily-limit": (p) => t("err.probeDailyLimit", { ...p, reset: dailyResetText(p.resetAt) }),
  "daily-cap-all": (p) => t("err.dailyCapAll", { ...p, reset: dailyResetText(p.resetAt) }),
  "probe-sg-conflict": (p) => t("err.probeSgConflict", p),
  "probe-sg-open": (p) => t("err.probeSgOpen", p),
  "no-default-vpc": () => t("tl.noVpc").replace(/\n/g, " "),
  "quota-too-low": (p) => t("err.quotaTooLow", { quota: typeof p.quota === "string" && p.quota ? p.quota : t("err.quotaOf", { market: marketText(p.market) }),
    available: t("unit.vcpus", { count: Number(p.available) || 0 }), needed: t("unit.vcpus", { count: Number(p.needed) || 0 }) }),
  "unknown-group": (p) => t("err.unknownGroup", { group: groupLabelOf(p.group) }),
  "group-not-offered": (p) => t("err.groupNotOffered", { group: groupLabelOf(p.group), region: regionLabel(p.region) }),
  "alerts-disabled": (p) => t("err.alertsDisabled", p),
  "email-not-verified": (p) => t("err.emailNotVerified", p),
  "email-missing": (p) => t("err.emailMissing", p),
  "alert-exists": (p) => t("err.alertExists", p),
  "alert-limit": (p) => t("err.alertLimit", p),
  "alert-conflict": (p) => t("err.alertConflict", p),
  "alert-not-found": (p) => t("err.alertNotFound", p),
  "cb-unsupported-type": (p) => t("err.cbUnsupportedType", p),
  "aws-error": (p) => t("err.aws", p),
  "no-answer": () => t("err.noAnswer"),
  "throttled": (p) => t("err.throttled", p),
  "sps-limited": (p) => t("err.spsLimited", p),
  "sps-unsupported": (p) => t("err.spsUnsupported", p),
  "sps-error": (p) => t("err.spsError", p),
  "sps-timeout": (p) => t("err.spsTimeout", p),
  "sps-not-scored": (p) => t("err.spsNotScored", p),
  "sps-zone-removed": () => t("err.spsZoneRemoved"),
  "sps-state-unavailable": () => t("err.spsStateUnavailable"),
  "internal": (p) => t("err.internal", p),
  // Set by the page itself, not by the API.
  "network": (p) => t("err.network", p),
  "rate-limited": (p) => t("err.rateLimited", p),
  "session-expired": (p) => t("err.sessionExpired", p),
};

// The daily test launch count starts again at 00:00 UTC, shown in the browser's time zone. The API sends the time
// as resetAt; without a valid one the page uses the next 00:00 UTC by its own clock.
function dailyResetText(resetAt) {
  if (Number.isFinite(stampMs(resetAt))) return fmtDate(resetAt);
  const next = new Date();
  next.setUTCHours(24, 0, 0, 0);
  return fmtDate(next);
}

function apiErrorText(status, data) {
  const text = ERROR_TEXT[data.code];
  if (text) return text(data.params || {});
  if (status === 429) return t("err.throttled");
  return data.error || t("err.requestFailed", { status });
}

// An error answer from the API. `message` is read in the active locale; an unknown code reads the API's English text.
class ApiError extends Error {
  constructor(status, data = {}) {
    super();
    this.status = status;
    this.data = data;
    this.code = data.code;
    this.params = data.params || {};
  }
  get message() { return apiErrorText(this.status, this.data); }
}

// The session ended: one silent re-login through the hosted page (the tab hash is kept);
// a second time in a row stops the loop and shows the signed-out view.
const sessionExpiredError = () => new ApiError(401, { code: "session-expired" });
// Single flight: parallel calls that all meet a 401 start the redirect (or show the signed-out view) once. The first
// call clears the tokens; a later call that finds none is part of the same ending and only reports the error.
function sessionEnded() {
  if (sessionStorage.getItem("idToken")) {
    clearTokens();
    if (!sessionStorage.getItem("reauth")) { sessionStorage.setItem("reauth", "1"); signIn(); }
    else { sessionStorage.removeItem("reauth"); showSignedOut(); }
  }
  return sessionExpiredError();
}

// A GET that meets HTTP 429 ("throttled"), 503 or a network failure is sent again up to RETRY_MAX times, waiting about 0.6, 1.2 and
// 2.4 seconds plus up to 30% jitter (a Retry-After header wins, at most RETRY_CAP_MS). The loading state stays on.
// POST, PUT and DELETE are never repeated, and a 429 with the code "sps-limited", "probe-daily-limit" or "daily-cap-all"
// is a daily limit, so waiting does not help.
const NO_RETRY_429 = new Set(["sps-limited", "probe-daily-limit", "daily-cap-all"]);
const RETRY_MAX = 3;
const RETRY_BASE_MS = 600;
const RETRY_CAP_MS = 8000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function retryDelay(attempt, resp) {
  const header = resp?.headers?.get ? resp.headers.get("Retry-After") : null;
  const seconds = header === null || header === undefined || header === "" ? NaN : Number(header);
  const ms = Number.isFinite(seconds) && seconds >= 0 ? seconds * 1000 : RETRY_BASE_MS * 2 ** attempt * (1 + Math.random() * 0.3);
  return Math.min(ms, RETRY_CAP_MS);
}

async function api(path, params = {}, body = null, method = body ? "POST" : "GET") {
  setBusy(1);
  try {
    const q = new URLSearchParams(params);
    const send = async () => {
      try {
        return await fetch(`${cfg.apiUrl}${path}${q.size ? "?" + q : ""}`, {
          method,
          headers: { Authorization: `Bearer ${sessionStorage.getItem("idToken")}`, ...(body ? { "Content-Type": "application/json" } : {}) },
          body: body ? JSON.stringify(body) : undefined,
        });
      } catch { throw new ApiError(0, { code: "network" }); }
    };
    // One exchange: the response and its parsed body (a 401 has none), with the retries for a GET.
    const exchange = async () => {
      for (let n = 0; ; n++) {
        let resp = null, failure = null;
        try { resp = await send(); } catch (e) { failure = e; }
        const data = resp && resp.status !== 401 ? await resp.json().catch(() => ({})) : {};
        const transient = Boolean(failure) || resp.status === 503 || (resp.status === 429 && !NO_RETRY_429.has(data.code));
        if (!transient || method !== "GET") {
          if (failure) throw failure;
          return { resp, data };
        }
        if (n >= RETRY_MAX) throw failure || new ApiError(resp.status, { code: "rate-limited" });
        await sleep(retryDelay(n, resp));
      }
    };
    // No token: the session ended and the re-login is under way; do not send an unauthenticated call.
    if (!sessionStorage.getItem("idToken")) throw sessionExpiredError();
    // Renew shortly before the ID token expires, so a call does not go out with a stale token.
    if (sessionStorage.getItem("refreshToken") && tokenNearExpiry() && !(await renewSession())) throw sessionExpiredError();
    let { resp, data } = await exchange();
    // A 401 can still come from clock skew or a token revoked elsewhere: renew once and retry once.
    if (resp.status === 401 && sessionStorage.getItem("refreshToken")) {
      if (!(await renewSession())) throw sessionExpiredError();
      ({ resp, data } = await exchange());
    }
    if (resp.status === 401) throw sessionEnded();
    sessionStorage.removeItem("reauth");
    if (!resp.ok) throw new ApiError(resp.status, data);
    return data;
  } finally { setBusy(-1); }
}

// --- shared UI patterns: loading, errors, busy buttons, dialogs -------------
const SKELETON_ROWS = 4;   // a table with no data yet shows about as many rows as it usually ends with (CSS gives them a real height)
function skeletonRows(cols, rows = SKELETON_ROWS) {
  return Array.from({ length: rows }, () => h("tr", { class: "skel-row", "aria-hidden": "true" },
    ...Array.from({ length: cols }, () => h("td", {}, h("span", { class: "skel" })))));
}
const skeleton = () => h("span", { class: "skel skel-inline", "aria-hidden": "true" });
const hasRows = (tbody) => [...tbody.rows].some((r) => !r.classList.contains("skel-row"));

// While a table reloads: grey rows when it is empty, faded rows when it shows data.
function tableLoading(table, cols, rows) {
  const body = table.tBodies[0];
  if (hasRows(body)) setReloading(table, true);
  else { body.replaceChildren(...skeletonRows(cols, rows)); table.setAttribute("aria-busy", "true"); }
}
function setReloading(el, on) {
  el.classList.toggle("reloading", on);
  if (on) el.setAttribute("aria-busy", "true"); else el.removeAttribute("aria-busy");
}
function emptyRow(cols, text, cls = "muted") {
  return h("tr", { class: "empty-row" }, h("td", { colspan: cols, class: cls }, text));
}

function errorAlert(text, retry) {
  return h("div", { class: "alert alert-error", role: "alert" }, h("span", {}, text),
    retry ? h("button", { type: "button", class: "ghost small", onclick: retry }, t("action.retry")) : null);
}
const okNotice = (...content) => h("div", { class: "alert alert-ok", role: "status" }, ...content);
const warnNotice = (...content) => h("div", { class: "alert alert-warn", role: "status" }, ...content);

// Politely announce a result that appears inside a table cell.
function announce(text) { $("sr-live").textContent = text; }

// The clicked button shows a spinner and progressive text until the work ends.
async function busyButton(btn, textKey, work) {
  const label = [...btn.childNodes];
  btn.disabled = true;
  btn.classList.add("is-busy");
  btn.setAttribute("aria-busy", "true");
  btn.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), t(textKey));
  try { return await work(); } finally {
    btn.disabled = false;
    btn.classList.remove("is-busy");
    btn.removeAttribute("aria-busy");
    btn.replaceChildren(...label);
    if (btn.hasAttribute("data-i18n")) btn.textContent = t(btn.getAttribute("data-i18n"));
  }
}

// Result lines that stay on screen after an action (a saved alert, a failed cancel). Each slot keeps the
// function that builds it, so a language change can build it again from the same result.
const slots = new Map();
function setSlot(id, build) {
  if (build) slots.set(id, build); else slots.delete(id);
  $(id).replaceChildren(...(build ? [build()].flat().filter(Boolean) : []));
}
function renderSlots() {
  for (const [id, build] of slots) $(id).replaceChildren(...[build()].flat().filter(Boolean));
}

function openDialog(d) {
  if (typeof d.showModal === "function") d.showModal(); else d.setAttribute("open", "");
}
function closeDialog(d) {
  if (typeof d.close === "function" && d.open) d.close(); else d.removeAttribute("open");
}

// Resolves true when the user chooses the action, false on Cancel or Escape. Focus goes back to the opener.
function confirmDialog({ title, facts = [], body, ok, cancel }) {
  const d = $("confirm-dialog");
  const opener = document.activeElement;
  $("confirm-title").textContent = title;
  $("confirm-facts").replaceChildren(...facts.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  $("confirm-facts").hidden = !facts.length;
  $("confirm-body").textContent = body || "";
  $("confirm-ok").textContent = ok;
  $("confirm-cancel").textContent = cancel || t("action.cancel");
  return new Promise((resolve) => {
    const done = (value) => {
      $("confirm-ok").removeEventListener("click", onOk);
      $("confirm-cancel").removeEventListener("click", onCancel);
      d.removeEventListener("cancel", onEscape);
      closeDialog(d);
      if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus();
      resolve(value);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onEscape = (ev) => { ev.preventDefault(); done(false); };
    $("confirm-ok").addEventListener("click", onOk);
    $("confirm-cancel").addEventListener("click", onCancel);
    d.addEventListener("cancel", onEscape);
    openDialog(d);
    $("confirm-cancel").focus();
  });
}

// --- last used Region and instance type: defaults only, kept in this browser
const LAST_KEY = "lastChoice";
function lastUsed() {
  try {
    const v = JSON.parse(localStorage.getItem(LAST_KEY) || "{}");
    return { region: typeof v.region === "string" ? v.region : null, type: typeof v.type === "string" ? v.type : null };
  } catch { return { region: null, type: null }; }
}
function remember(region, type) {
  const cur = lastUsed();
  const next = { region: region || cur.region, type: type || cur.type };
  try { localStorage.setItem(LAST_KEY, JSON.stringify(next)); } catch { /* storage full or blocked */ }
}
function defaultRegion() {
  const r = lastUsed().region;
  return limits.regions.includes(r) ? r : limits.regions[0];
}
// AWS Region names ("Asia Pacific (Tokyo)") stay in English in every language, so these labels need no i18n keys.
// regionLabel(code) is "Asia Pacific (Tokyo) · ap-northeast-1"; short gives the name alone. Both give the plain code when the API sent no name.
const regionName = (code) => {
  const n = limits && limits.regionNames ? limits.regionNames[code] : "";
  return typeof n === "string" ? n : "";
};
function regionLabel(code, short = false) {
  const n = regionName(code);
  if (!n) return code;
  return short ? n : `${n} · ${code}`;
}
const regionLabels = (codes) => codes.map((c) => regionLabel(c));
function fillRegionSelect(sel, value) {
  sel.replaceChildren(...limits.regions.map((r) => h("option", { value: r }, regionLabel(r))));
  sel.value = limits.regions.includes(value) ? value : limits.regions[0] || "";
}

// --- instance types: what EC2 offers per Region, narrowed by the saved list ---
// One shared list per Region and route (the answers come from the same API for every tab). A list that is older than
// the offering cache time is read again, without refresh=true, the next time a tab asks for it.
function typeLists(extra = {}) {
  const cache = new Map();   // Region -> { promise, at }
  const old = (e) => Boolean(e.at) && olderThan([e.at], MINUTES.offerings());
  return {
    get(region) {
      let e = cache.get(region);
      if (e && old(e)) { cache.delete(region); e = null; }
      if (!e) {
        e = { at: null, promise: null };
        const mine = e;
        e.promise = api("/api/instance-types", { region, ...extra }).then((r) => { mine.at = r.fetchedAt || nowIso(); mine.types = r.instanceTypes; return r.instanceTypes; },
          (err) => { if (cache.get(region) === mine) cache.delete(region); throw err; });
        cache.set(region, e);
      }
      return e.promise;
    },
    stale: () => [...cache.values()].some(old),
    // Every type of the lists that have arrived, for text that must not wait for a request.
    known: () => [...cache.values()].flatMap((e) => e.types || []),
    // The list of one Region once it has arrived, else null.
    of: (region) => cache.get(region)?.types || null,
    clear: () => cache.clear(),
  };
}
const typeCache = typeLists();
const regionTypes = (region) => typeCache.get(region);
const shownTypes = (offered) => (watchlist.length ? offered.filter((x) => watchlist.includes(x)) : offered);

// Fill a type select, keeping its value, else the last used type, else the first. Options are grouped by family.
function fillTypeSelect(sel, types) {
  const keep = sel.value;
  const groups = groupByFamily(types);
  sel.replaceChildren(...[...groups].map(([fam, list]) => h("optgroup", { label: fam }, list.map((x) => h("option", { value: x }, x)))));
  const first = [...groups.values()][0]?.[0] || "";
  const last = lastUsed().type;
  sel.value = types.includes(keep) ? keep : types.includes(last) ? last : first;
}

function typeNoteText(offered, shown) {
  if (!offered.length) return t("list.noneOfferedRegion");
  if (!shown.length) return t("list.noneOnListRegion");
  return watchlist.length ? t("list.showingList") : t("list.showingAll");
}

// --- Edit instance list: a dialog, the tab stays where it is ----------------------
let listDone = null;
const listBoxes = () => [...$("list-options").querySelectorAll("input[data-type]")];
// The editor shows what the page shows: no saved list means every offered type is ticked.
function updateListEditor() {
  const boxes = listBoxes();
  const on = boxes.filter((b) => b.checked).length;
  $("list-count").textContent = t("ms.selected", { count: on, total: boxes.length });
  for (const group of $("list-options").querySelectorAll(".opt-group")) {
    const mine = [...group.querySelectorAll("input[data-type]")];
    const sel = mine.filter((b) => b.checked).length;
    const head = group.querySelector("input.fam-box");
    head.checked = sel === mine.length;
    head.indeterminate = sel > 0 && sel < mine.length;
    group.querySelector(".fam-count").textContent = `${sel}/${mine.length}`;
  }
  // The limit only matters for a custom list, not for "every type".
  $("list-limit").textContent = on > LIST_MAX && on < boxes.length ? t("list.tooMany", { count: LIST_MAX }) : "";
  $("list-error").replaceChildren();
}
function openListEditor(offered, onSaved) {
  const names = [...new Set([...offered, ...watchlist])];
  const chosen = watchlist.length ? watchlist : names;
  fillPoints($("list-help"), t("list.help"));
  $("list-options").replaceChildren(...[...groupByFamily(names)].map(([fam, list]) => {
    const head = h("input", { type: "checkbox", class: "fam-box" });
    const boxes = list.map((x) => {
      const box = h("input", { type: "checkbox", value: x, "data-type": x });
      box.checked = chosen.includes(x);
      box.addEventListener("change", updateListEditor);
      return h("label", {}, box, offered.includes(x) ? x : t("list.notOfferedHere", { type: x }));
    });
    head.addEventListener("change", () => {
      for (const b of boxes) b.querySelector("input").checked = head.checked;
      updateListEditor();
    });
    return h("div", { class: "opt-group", role: "group", "aria-label": fam },
      h("label", { class: "opt-head" }, head, h("span", {}, fam), h("span", { class: "muted fam-count" })),
      h("div", { class: "options" }, boxes));
  }));
  listDone = onSaved;
  updateListEditor();
  openDialog($("list-dialog"));
  $("list-options").querySelector("input")?.focus();
}
function pickAllTypes(on) {
  for (const b of listBoxes()) b.checked = on;
  updateListEditor();
}

async function saveList() {
  const types = listBoxes().filter((b) => b.checked).map((b) => b.value);
  const everything = types.length === listBoxes().length;
  $("list-error").replaceChildren();
  if (!types.length) { $("list-error").replaceChildren(errorAlert(t("list.needOne"))); return; }
  if (!everything && types.length > LIST_MAX) { $("list-error").replaceChildren(errorAlert(t("list.tooMany", { count: LIST_MAX }))); return; }
  await busyButton($("list-save"), "action.saving", async () => {
    try {
      // Every type ticked is the "all" case: no list is stored, so types EC2 adds later show up on their own.
      const clean = everything ? [] : cleanList(types);
      if (clean.length) localStorage.setItem(LIST_KEY, JSON.stringify(clean)); else localStorage.removeItem(LIST_KEY);   // throws when the browser blocks storage
      watchlist = clean;
      closeDialog($("list-dialog"));
      if (listDone) listDone();
    } catch (e) { $("list-error").replaceChildren(errorAlert(t("list.saveFailed", { error: e.message }), saveList)); }
  });
}

// --- region error text shared by the Region-wide views ----------------------
// A failed Region in a Region-wide answer: { region, error (English), errorCode, errorParams }.
const isNotEnabled = (row) => Boolean(row && (row.errorCode === "not-enabled" || (!row.errorCode && row.error === "not enabled")));
const REGION_ERROR_TEXT = {
  "not-enabled": () => t("common.notEnabled"),
  "aws-error": (p) => t("err.aws", p),
  "no-answer": () => t("err.noAnswer"),
};
const regionErrorText = (row) => (REGION_ERROR_TEXT[row.errorCode] ? REGION_ERROR_TEXT[row.errorCode](row.errorParams || {}) : row.error);

// --- quotas: the quota that limits an instance family ----------------------
// The quota family the API reports on each row ("p", "g", "trn", "inf", or "cb:<family>") that limits an instance type.
// VT instances count against the G quotas. Other letters have no quota on this page.
const QUOTA_LETTERS = new Map([["p", "p"], ["g", "g"], ["vt", "g"], ["trn", "trn"], ["inf", "inf"]]);
function quotaFamilyOf(type, category) {
  const fam = familyOf(type);
  if (!fam) return null;
  const letters = (/^[a-z]+/.exec(fam) || [""])[0];
  if (category === "capacity-block") return QUOTA_LETTERS.has(letters) ? `cb:${fam}` : null;
  return QUOTA_LETTERS.get(letters) || null;
}
const familyQuotas = (rows, category, type) => {
  const want = quotaFamilyOf(type, category);
  return want ? rows.filter((q) => q.family === want) : [];
};

// The amount with the unit that Service Quotas reports. EC2 reports "None" for the vCPU quotas.
function quotaAmount(value, q) {
  const unit = q.unit;
  if (!unit || unit === "None") return q.category === "capacity-block" ? fmtNumber(value) : t("unit.vcpus", { count: value });
  if (unit === "Count") return q.category === "capacity-block" ? t("unit.blocks", { count: value }) : fmtNumber(value);
  return `${fmtNumber(value)} ${unit}`;
}
const usageLevel = (p) => (p === null || p === undefined ? "" : p >= 100 ? "q-bad" : p >= 80 ? "q-warn" : "");
const usageWord = (p) => (p >= 100 ? t("q.overLimit") : p >= 80 ? t("q.nearLimit") : "");

// --- quotas: what a quota counts, and which families are in its group -------
// Each On-Demand and Spot quota counts a whole group of EC2 families. Line 1 says the usage is AWS's count for the whole group in
// this account; line 2 lists the group's families that the enabled Regions offer (acceleratorFamilies of the offering answer),
// or these examples while that list is not known yet.
const QUOTA_COUNTS = {
  p: { group: "P", examples: ["p3", "p4d", "p5"] },
  g: { group: "G and VT", examples: ["g4dn", "g5", "g6", "g6e"] },
  trn: { group: "Trn", examples: ["trn1", "trn2"] },
  inf: { group: "Inf", examples: ["inf1", "inf2"] },
};
// The offering answer the tip reads when the Compare tab has not loaded it: one silent request, cached by the server.
const tipOffer = { matrix: null, state: "" };
function ensureTipOffers(refill) {
  if (cmp.matrix || tipOffer.state === "loading" || tipOffer.state === "done") return;
  tipOffer.state = "loading";
  api("/api/offering-matrix").then((m) => { tipOffer.matrix = m; tipOffer.state = "done"; }, () => { tipOffer.state = ""; })
    .then(() => { if (refill && tipOffer.state === "done") refill(); });
}
// Real family names: the families of the type lists and the offering answer. The tracked families themselves
// (trackedFamilyRule, for example g6 or newer) have no names of their own here; they appear as soon as the offering answer
// lists what each Region sells.
function pageFamilies() {
  const matrixTypes = [];
  for (const row of (cmp.matrix || tipOffer.matrix)?.regions || []) matrixTypes.push(...offeredIn(row));
  return new Set([...typeCache.known(), ...cbTypeCache.known(), ...cmpTypes(), ...matrixTypes].map(familyOf).filter(Boolean));
}
// Grouped by the same mapping that picks the highlighted quota, so the list and the highlight never disagree.
const familiesCounting = (q) => [...pageFamilies()].filter((f) => quotaFamilyOf(f, q.category) === q.family).sort(compareFamilies);
// Every family of the quota's group that the enabled Regions offer, from the offering answer; [] while it is not known.
function groupFamilies(q) {
  const all = (cmp.matrix || tipOffer.matrix)?.acceleratorFamilies;
  if (!Array.isArray(all)) return [];
  return all.filter((f) => typeof f === "string" && quotaFamilyOf(f, q.category) === q.family).sort(compareFamilies);
}
const CB_FAMILY_RE = /^cb:([a-z0-9-]+)$/;
function hasQuotaInfo(q) {
  if (typeof q.family !== "string") return false;
  return q.category === "capacity-block" ? CB_FAMILY_RE.test(q.family) : Object.hasOwn(QUOTA_COUNTS, q.family);
}
// Two lines. On-Demand and Spot: the usage is AWS's count for the whole quota group, then the group's families that the enabled
// Regions offer (the examples while that list is not known). Capacity Blocks: the one family the quota names.
function quotaInfoLines(q) {
  if (q.category === "capacity-block") {
    const fams = familiesCounting(q);
    return [t("q.countsCb"), fams.length ? fmtList(fams) : CB_FAMILY_RE.exec(q.family)[1]];
  }
  const g = QUOTA_COUNTS[q.family];
  const what = t(q.category === "spot" ? "q.countsSpot" : "q.countsOd", { group: g.group });
  const fams = groupFamilies(q);
  return [what, fams.length ? t("q.groupFamilies", { families: fmtList(fams) }) : t("q.groupExamples", { examples: fmtList(g.examples) })];
}
// The quota name is the control: hovering or focusing it, or a tap, shows the two-line tip box. A quota with no known family gets
// the plain name. Returns the name element and the tip box (or null) to put side by side in the name cell.
function quotaName(q, id) {
  if (!hasQuotaInfo(q)) return [q.name, null];
  const pop = h("span", { id: `${id}-pop`, class: "popover", role: "tooltip", hidden: true });
  const name = h("span", { id, class: "q-tip", tabindex: "0", role: "button", "aria-expanded": "false", "aria-describedby": pop.id }, q.name);
  const group = { pop, btns: [name], fill: () => { ensureTipOffers(() => { if (infoOpen?.group === group) group.fill(); }); pop.replaceChildren(...quotaInfoLines(q).map((x) => h("span", { class: "pop-line" }, x))); } };
  wireInfo(name, group, true);
  pop.addEventListener("mouseenter", () => hoverIn(group, name));
  pop.addEventListener("mouseleave", () => hoverOut(group));
  return [name, pop];
}

// ======================================================================
// 1. Spot score: one score per Region and score group (an instance family with one GPU count per instance)
// ======================================================================
const REGION_COORDS = {
  "us-east-1": [38.9, -77.4], "us-east-2": [40.0, -83.0], "us-west-1": [37.4, -121.9], "us-west-2": [45.8, -119.7],
  "ca-central-1": [45.5, -73.6], "ca-west-1": [51.0, -114.1], "mx-central-1": [20.6, -100.4], "sa-east-1": [-23.5, -46.6],
  "eu-west-1": [53.3, -6.3], "eu-west-2": [51.5, -0.1], "eu-west-3": [48.9, 2.4], "eu-central-1": [50.1, 8.7],
  "eu-central-2": [47.4, 8.5], "eu-north-1": [59.3, 18.1], "eu-south-1": [45.5, 9.2], "eu-south-2": [41.6, -0.9],
  "il-central-1": [32.1, 34.8], "me-south-1": [26.1, 50.6], "me-central-1": [25.2, 55.3], "af-south-1": [-33.9, 18.4],
  "ap-south-1": [19.1, 72.9], "ap-south-2": [17.4, 78.5], "ap-east-1": [22.3, 114.2], "ap-east-2": [25.0, 121.5],
  "ap-southeast-1": [1.35, 103.8], "ap-southeast-2": [-33.9, 151.2], "ap-southeast-3": [-6.2, 106.8],
  "ap-southeast-4": [-37.8, 145.0], "ap-southeast-5": [3.1, 101.7], "ap-southeast-6": [-36.8, 174.8], "ap-southeast-7": [13.8, 100.5],
  "ap-northeast-1": [35.7, 139.7], "ap-northeast-2": [37.6, 127.0], "ap-northeast-3": [34.7, 135.5],
};
const MAP_W = 720, MAP_H = 360;
const project = (lat, lon) => [((lon + 180) / 360) * MAP_W, ((90 - lat) / 180) * MAP_H];
const GROUP_BATCH = 30;                        // score groups per /api/spot-scores call with group=
const HIDDEN_FAMILIES_KEY = "cmpHiddenFamilies"; // families unticked in the filter, kept in this browser

// The score groups from /api/spot-scores. groups: key -> the group of the newest answer; order: the keys in the API's
// order; at: key -> when AWS answered (the group's own fetchedAt); state: key -> loading | ok | err; errors: key -> the
// group's first error; req: key -> the newest request for it, so an older answer cannot replace a newer one.
// full: the id of a load of every group in flight (0 when none); meta: the schedule fields of the newest answer.
// `blocked` is the seconds a Refresh must wait when the server refused to repeat a request this soon.
const newScores = () => ({ groups: new Map(), order: [], at: {}, state: {}, errors: {}, req: {}, full: 0, meta: {}, loaded: false, err: null, batches: 0, done: 0, blocked: 0 });
// selected: the Region in the detail panel; chip: the open item there ("g|<group key>" or "t|<type>"). Both stay in this tab only.
// idleOpen: the list of Regions that sell none of the shown groups is expanded.
const cmp = { matrix: null, err: null, loading: false, view: "table", selected: null, chip: null, sg: newScores(), land: undefined, idleOpen: false };
let cmpSeq = 0, scoreSeq = 0;
let cmpFamMs = null;   // the Families dropdown
let cmpHeadSig = null; // what the heatmap headings show now (they are built again only when it changes)

function readStored(key, fallback) {
  try { const v = JSON.parse(localStorage.getItem(key)); return v ?? fallback; } catch { return fallback; }
}
function writeStored(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage full or blocked */ }
}
function hiddenFamilies() {
  const v = readStored(HIDDEN_FAMILIES_KEY, []);
  return new Set(Array.isArray(v) ? v.filter((x) => typeof x === "string") : []);
}

const offeredIn = (row) => new Set(Object.values(row?.families || {}).flat());
// Every instance type that any Region offers, in natural order (the offering answer, for the Region detail and the quota tips).
function cmpTypes() {
  const all = new Set();
  for (const row of cmp.matrix?.regions || []) for (const x of offeredIn(row)) all.add(x);
  return sortTypes(all);
}

// --- score groups: the newest answer, else the deployment's list in /api/config ---
const isObj = (v) => Boolean(v) && typeof v === "object" && !Array.isArray(v);
const validGroup = (g) => isObj(g) && typeof g.key === "string" && /^[a-z0-9-]+\/\d+$/.test(g.key) && Array.isArray(g.sizes);
function cleanGroup(g) {
  return { ...g, label: typeof g.label === "string" && g.label ? g.label : g.key, family: typeof g.family === "string" && g.family ? g.family : g.key.split("/")[0],
    gpus: Number(g.gpus) || Number(g.key.split("/")[1]) || 0, sizes: sortTypes(g.sizes.filter((x) => typeof x === "string")),
    scores: isObj(g.scores) ? g.scores : {}, regionStates: isObj(g.regionStates) ? g.regionStates : {}, prices: isObj(g.prices) ? g.prices : {},
    history: Array.isArray(g.history) ? g.history : [] };
}
const groupOf = (key) => cmp.sg.groups.get(key) || null;
const configGroups = () => (Array.isArray(limits?.spotScoreGroups) ? limits.spotScoreGroups.filter(validGroup).map(cleanGroup) : []);
function knownGroups() {
  return cmp.sg.order.length ? cmp.sg.order.map(groupOf).filter(Boolean) : configGroups();
}
// The score group of an instance type, or null when its family gets no Spot score (or its size has no group yet).
const groupOfType = (type) => knownGroups().find((g) => g.sizes.includes(type)) || null;
const gpuText = (n) => t("grp.gpus", { count: n });
// The group for people, in the page's language: "g6e · 4 GPUs". The server's English label (else the key) only when the
// GPU count is not known.
const groupName = (g) => (g.gpus > 0 ? `${g.family} · ${gpuText(g.gpus)}` : g.label);
// The same from a group key ("g6e/4"); `fallback` (else the key) when the key does not parse.
function groupLabelOf(key, fallback) {
  const g = knownGroups().find((x) => x.key === key);
  if (g) return groupName(g);
  const m = /^([a-z0-9-]+)\/(\d+)$/.exec(String(key));
  return m && Number(m[2]) > 0 ? `${m[1]} · ${gpuText(Number(m[2]))}` : fallback || String(key);
}

const scoreFamilies = () => [...new Set(cmp.sg.order.map((k) => groupOf(k)?.family).filter(Boolean))];
// The columns on screen: every score group of the families that are ticked. The saved list does not apply here.
function shownKeys() {
  const hidden = hiddenFamilies();
  return cmp.sg.order.filter((k) => !hidden.has(groupOf(k)?.family));
}
const band = (s) => (s >= 7 ? "hi" : s >= 4 ? "mid" : "lo");
const scoreAt = (key, region) => { const s = groupOf(key)?.scores?.[region]; return typeof s === "number" ? s : null; };
const regionState = (key, region) => { const s = groupOf(key)?.regionStates?.[region]; return typeof s === "string" ? s : null; };
const keyLoading = (key) => Boolean(cmp.sg.full) || cmp.sg.state[key] === "loading";
const scoresLoading = () => Boolean(cmp.sg.full) || Object.values(cmp.sg.state).includes("loading");
// sps-limited has its own notice and sps-unsupported only leaves the cells neutral; every other error gets Retry.
const hardError = (e) => Boolean(e) && e.code !== "sps-limited" && e.code !== "sps-unsupported";
const scoreErrorText = (e) => (e.error ? e.error.message : ERROR_TEXT[e.code] ? ERROR_TEXT[e.code](e.params || {}) : e.message || "");
// The local time of a schedule stamp from the API ("00:30 UTC" shows as the browser's own time), or "" without one.
const fmtTime = (v) => (Number.isFinite(stampMs(v)) ? dtf({ hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(new Date(v)) : "");

function takeMeta(r) {
  const m = {};
  for (const k of ["state", "nextDailyUpdateAt", "lastDailyUpdateAt", "nextWarmAt", "warmMinutes", "spotScoreLimitedUntil"]) if (k in r) m[k] = r[k];
  cmp.sg.meta = { ...cmp.sg.meta, ...m };
}
// `prev` and `prevAt`: what the tab knew before this answer (a load of every group clears the maps first).
function takeGroup(g, refresh, prev = cmp.sg.groups, prevAt = cmp.sg.at) {
  const sg = cmp.sg;
  const e = Array.isArray(g.errors) && g.errors.length ? g.errors[0] : null;
  const old = prev.get(g.key);
  // A refresh that reached AWS reads "just now"; a blocked one keeps the older stamp.
  let at = refresh && g.refreshed === true ? nowIso() : g.fetchedAt;
  // An answer with an error and no score keeps the scores, stamp and readings the tab had for the group (in the
  // Regions the group still lists), next to the error.
  if (e && !Object.keys(g.scores).length && old && Object.keys(old.scores).length) {
    const states = Object.keys(g.regionStates).length ? g.regionStates : old.regionStates;
    const scores = Object.fromEntries(Object.entries(old.scores).filter(([r]) => r in states));
    g = { ...g, scores, regionStates: states, history: old.history };
    at = prevAt[g.key] || at;
  }
  sg.groups.set(g.key, g);
  if (at) sg.at[g.key] = at; else delete sg.at[g.key];
  if (e) sg.errors[g.key] = e; else delete sg.errors[g.key];
  sg.state[g.key] = e ? "err" : "ok";
}

// Every score group in one call: the answer replaces what the tab knew. A failure keeps the groups on screen next to its error.
async function loadScores() {
  const sg = cmp.sg;
  const id = ++scoreSeq;
  sg.full = id;
  sg.err = null;
  sg.batches = 0; sg.done = 0;
  renderCompare();
  try {
    const r = await api("/api/spot-scores");
    if (sg.full !== id) return null;
    const groups = (Array.isArray(r.groups) ? r.groups : []).filter(validGroup).map(cleanGroup);
    const prev = sg.groups, prevAt = sg.at;
    sg.groups = new Map(); sg.order = []; sg.state = {}; sg.errors = {}; sg.at = {}; sg.req = {};
    for (const g of groups) { sg.order.push(g.key); takeGroup(g, false, prev, prevAt); }
    takeMeta(r);
    sg.loaded = true;
    return !groups.some((g) => hardError(sg.errors[g.key]));
  } catch (e) {
    if (sg.full !== id) return null;
    sg.err = e;
    return false;
  } finally {
    if (sg.full === id) sg.full = 0;
    renderCompare();
  }
}

// The groups `keys` again, GROUP_BATCH per call, one call after another. `refresh` asks the server to repeat their
// configurations now (it never creates or changes one). A failed call marks its own groups only; the next call is still sent.
function askGroups(keys, refresh = false) {
  const sg = cmp.sg;
  const batches = [];
  for (let i = 0; i < keys.length; i += GROUP_BATCH) batches.push(keys.slice(i, i + GROUP_BATCH));
  if (!batches.length) return Promise.resolve(true);
  if (!scoresLoading()) { sg.batches = 0; sg.done = 0; }
  sg.batches += batches.length;
  let blocked = 0;   // the wait the server asked for; the note on screen keeps its old text until every call answered
  let missing = false;   // an answer left out a group it was asked for (the groups changed): read every group again
  const jobs = batches.map((list) => {
    const id = ++scoreSeq;
    for (const k of list) { sg.state[k] = "loading"; sg.req[k] = id; }
    const params = { group: list.join(",") };
    if (refresh) params.refresh = "true";
    const current = () => list.filter((k) => sg.req[k] === id);
    const fail = (e) => { for (const k of current()) if (sg.state[k] === "loading") { sg.errors[k] = { code: e.code, error: e }; sg.state[k] = "err"; } };
    return () => api("/api/spot-scores", params).then((r) => {
      const got = new Map((Array.isArray(r.groups) ? r.groups : []).filter(validGroup).map((g) => [g.key, cleanGroup(g)]));
      const mine = current();
      for (const k of mine) {
        if (got.has(k)) takeGroup(got.get(k), refresh);
        else { missing = true; sg.state[k] = sg.errors[k] ? "err" : "ok"; }   // the answer left the group out
      }
      if (mine.length) takeMeta(r);
      if (refresh) blocked = Math.max(blocked, blockedSeconds(mine.map((k) => got.get(k))));
      return !mine.some((k) => hardError(sg.errors[k]));
    }, (e) => { fail(e); return false; })
      // A fault while reading the answer: its cells show the error, and the next call is still sent.
      .catch((e) => { fail(e); return false; })
      .finally(() => { sg.done++; renderCompare(); });
  });
  const calls = [];
  let chain = Promise.resolve();
  for (const job of jobs) { chain = chain.then(job); calls.push(chain); }
  renderCompare();
  return Promise.all(calls).then((oks) => {
    if (refresh) { sg.blocked = blocked; renderCompare(); }
    // Once, after every call answered: the full list says which groups there are now (a full load already running does too).
    if (!missing || sg.full) return oks.every(Boolean);
    return loadScores().then((ok) => (ok === null ? null : ok && oks.every(Boolean)));
  });
}

// Retry: the groups whose last answer failed, or every group when the whole answer failed.
function retryScores() {
  const sg = cmp.sg;
  if (sg.err || !sg.loaded) return loadTab("compare", true, false);
  return askGroups(sg.order.filter((k) => sg.state[k] === "err" && hardError(sg.errors[k])));
}

// The offerings (for the Region detail) and the Spot scores load side by side; the table shows as soon as the scores
// arrive. `refresh` repeats the shown groups' configurations; before any group is known it reads them like a first open.
async function loadCompare(refresh = false) {
  const mine = ++cmpSeq;
  cmp.loading = true;
  cmp.err = null;
  renderCompare();
  const offers = api("/api/offering-matrix").then((m) => { if (mine === cmpSeq) cmp.matrix = m; }, (e) => { if (mine === cmpSeq) cmp.err = e; })
    .finally(() => { if (mine === cmpSeq) { cmp.loading = false; renderCompare(); } });
  const keys = shownKeys();
  const scores = refresh && cmp.sg.loaded && keys.length ? askGroups(keys, true) : loadScores();
  const [, ok] = await Promise.all([offers, scores]);
  if (mine !== cmpSeq || ok === null) return null;
  renderCompare();
  return ok && !cmp.err;
}

const scoresStale = () => olderThan(shownKeys().map((k) => cmp.sg.at[k]), MINUTES.scores());
const matrixStale = () => Boolean(cmp.matrix) && olderThan(cmp.matrix.regions.filter((r) => !r.error).map((r) => r.fetchedAt), MINUTES.offerings());
// While the groups are being built, the tab reads them again on the next minute tick.
const scoresBuilding = () => cmp.sg.meta.state === "building";
// Built for the first time: the server is building the groups and has none to show yet.
const firstBuild = () => scoresBuilding() && cmp.sg.loaded && !cmp.sg.order.length;
const compareStale = () => scoresStale() || matrixStale() || scoresBuilding();

// Focus stays on the control the user chose when a render replaces it: elements carry a data-fk key.
function focusFk(root, key) {
  const el = key ? [...root.querySelectorAll("[data-fk]")].find((x) => x.getAttribute("data-fk") === key) : null;
  if (el && typeof el.focus === "function") el.focus();
  return Boolean(el);
}
function keepFocus(root, render) {
  const a = document.activeElement;
  const key = a && root.contains(a) ? a.getAttribute("data-fk") : null;
  render();
  if (key && !a.isConnected) focusFk(root, key);
}

function openRegion(region, type) {
  remember(region, type);
  $("cap-region").value = region;
  cap.pendingType = type;
  delete loadedKey.capacity;
  return selectTab("capacity");
}
// The On-Demand and Spot tab with the test launch set to this Region and target ("group:<key>" or "type:<name>").
async function openTest(region, target, type) {
  cap.pendingTarget = target;
  await openRegion(region, type);
  const box = $("tl-section");
  if (typeof box.scrollIntoView === "function") box.scrollIntoView({ block: "nearest" });
  $("tl-target").focus();
}

function selectRegion(region) {
  const prev = cmp.selected;
  cmp.selected = region && region !== prev ? region : null;
  cmp.chip = null;
  renderCompare();
  if (cmp.selected) {
    const box = $("cmp-detail");
    if (typeof box.scrollIntoView === "function") box.scrollIntoView({ block: "nearest" });
  } else if (prev) focusFk($("panel-compare"), cmp.view === "table" ? `rh|${prev}` : `mk|${prev}`);
}

function toggleChip(id) {
  const prev = cmp.chip;
  cmp.chip = id && id !== prev ? id : null;
  renderCompare();
  focusFk($("cmp-detail"), cmp.chip ? "act-first" : prev ? `chip|${prev}` : null);
}

// Groups exist but every family is unticked: the heatmap, map and detail give way to one message.
const noFamilySelected = () => cmp.sg.order.length > 0 && shownKeys().length === 0;
function renderCompare() {
  if (!limits) return;
  keepFocus($("panel-compare"), () => {
    const isTable = cmp.view === "table";
    const empty = noFamilySelected();
    $("cmp-view-table").setAttribute("aria-pressed", String(isTable));
    $("cmp-view-map").setAttribute("aria-pressed", String(!isTable));
    $("cmp-heat-box").hidden = !isTable || empty;
    $("cmp-score-row").hidden = !isTable || empty;
    $("cmp-map-wrap").hidden = isTable || empty;
    $("cmp-empty").hidden = !empty;
    renderCmpIntro();
    renderCmpFilters();
    renderScoreStatus();
    renderCmpAlerts();
    // The chosen Region stays in cmp.selected, so it is back when a family is ticked again.
    if (empty) $("cmp-detail").hidden = true;
    else {
      if (isTable) renderHeatTable(); else renderCmpMap();
      renderRegionDetail();
    }
    $("cmp-refresh").disabled = empty;
    setRefreshBusy("cmp-refresh", cmp.loading || scoresLoading());
    setRefreshNote("cmp-refresh-note", cmp.sg.blocked);
    renderFresh();
  });
}

// What a score means, how often it updates, and where the scored families come from.
// One bullet each; the families bullet only when the deployment names its families.
function renderCmpIntro() {
  const every = Number(cmp.sg.meta.warmMinutes) || Number(limits.warmMinutes) || 15;
  const fams = Array.isArray(limits.spotScoreFamilies) ? limits.spotScoreFamilies.filter((f) => typeof f === "string").sort(compareFamilies) : [];
  $("cmp-intro").replaceChildren(...[t("cmp.introScore"), t("cmp.introPromise"), t("cmp.introEvery", { minutes: every }), t("cmp.introColumns")].map((x) => h("li", {}, x)),
    ...(fams.length ? [h("li", { id: "cmp-families-note" }, t("cmp.familiesNote", { families: fmtList(fams) }))] : []));
}

// The Families dropdown is built again only when the families change, so an open panel keeps its focus.
// Families left unticked are kept in this browser, so a new family shows by default.
const famSummary = (sel, total) => (sel.length === total ? t("cmp.famAll", { count: total })
  : sel.length <= 3 ? fmtList(sel) : t("cmp.famMore", { names: fmtList(sel.slice(0, 2)), more: sel.length - 2 }));
function setUpFamilies() {
  cmpFamMs = multiSelect("cmp-families", { none: () => t("ms.noFamilies"), choose: () => t("cmp.famNone"), empty: () => t("ms.emptyFamilies"), summary: famSummary },
    onFamiliesChange);
}
function onFamiliesChange() {
  const chosen = new Set(cmpFamMs.selected());
  writeStored(HIDDEN_FAMILIES_KEY, cmpFamMs.options().filter((f) => !chosen.has(f)).sort());
  renderCompare();
}
function renderCmpFilters() {
  const fams = scoreFamilies();
  const hidden = hiddenFamilies();
  const chosen = fams.filter((f) => !hidden.has(f));
  if (cmpFamMs.options().join(",") !== fams.join(",")) cmpFamMs.setOptions(fams.map((f) => ({ value: f, label: () => f })), chosen);
  else if (cmpFamMs.selected().join(",") !== chosen.join(",")) cmpFamMs.set(chosen);
  else cmpFamMs.refresh();
  $("cmp-filters").hidden = !fams.length;
}

// [cell class, sample, text key], in reading order: the scores, then the cells without one. Loading stays last.
const LEGEND = [["hi", "9", "cmp.legendHi"], ["mid", "5", "cmp.legendMid"], ["lo", "2", "cmp.legendLo"], ["pend", "–", "cmp.legendPending"],
  ["unk", "•", "cmp.legendOffered"], ["none", "", "cmp.legendBlank"], ["err", "•", "cmp.legendErr"], ["loading", "", "cmp.legendLoading"]];

function renderScoreStatus() {
  const sg = cmp.sg;
  const loading = scoresLoading();
  // The "Loading" entry is always there, so the legend does not change size; it is only visible while scores load.
  $("cmp-heat-legend").replaceChildren(...LEGEND.map(([c, sample, key]) => h("span", { class: `lg-item${c === "loading" && !loading ? " lg-off" : ""}`, "aria-hidden": c === "loading" && !loading ? "true" : null },
    h("span", { class: `sw hc-${c}`, "aria-hidden": "true" }, sample), t(key))));
  $("cmp-score-progress").hidden = !loading;
  $("cmp-score-bar").max = Math.max(1, sg.batches);
  // A load of every group, or no answer yet: an indeterminate bar; then the share of calls answered.
  if (sg.done && !sg.full) $("cmp-score-bar").value = Math.min(sg.done, sg.batches); else $("cmp-score-bar").removeAttribute("value");
  $("cmp-score-progress-text").textContent = t("cmp.scoresLoading");
}

function renderCmpAlerts() {
  const sg = cmp.sg;
  const errs = shownKeys().filter((k) => sg.state[k] === "err" && sg.errors[k]).map((k) => sg.errors[k]);
  const failed = errs.filter(hardError);
  const until = sg.meta.spotScoreLimitedUntil;
  $("cmp-alerts").replaceChildren(...[
    sg.err && errorAlert(t("cmp.scoresFailed", { error: sg.err.message }), () => loadTab("compare", true, false)),
    !sg.err && failed.length && errorAlert(t("cmp.scoresFailed", { error: scoreErrorText(failed[0]) }), retryScores),
    // The first build (no group yet): a notice with a spinner; the tab reads again every minute until the groups are there.
    scoresBuilding() && (firstBuild() ? warnNotice(h("span", { class: "setup-note" }, h("span", { class: "spinner", "aria-hidden": "true" }), t("cmp.buildingFirst")))
      : warnNotice(t("cmp.building"))),
    Number.isFinite(stampMs(until)) && warnNotice(t("cmp.limitedUntil", { time: fmtDate(until) })),
    !Number.isFinite(stampMs(until)) && errs.some((e) => e.code === "sps-limited") && warnNotice(t("cmp.spsLimited")),
    cmp.err && errorAlert(t("cmp.matrixFailed", { error: cmp.err.message }), () => loadTab("compare", true, false)),
  ].filter(Boolean));
}

// The scores of one Region in a list of readings [{ at, scores: { region: score } }], oldest first, the last `n` of them.
const READINGS_TIP = 12;
const readingsOf = (list, key, n) => (Array.isArray(list) ? list : []).map((r) => r && r.scores && r.scores[key]).filter((v) => Number.isFinite(Number(v)) && v !== null).slice(-n);
// A score older than two update rounds is dimmed and says to refresh (the group's own stamp).
const oldScoreMinutes = () => 2 * (Number(cmp.sg.meta.warmMinutes) || Number(limits?.warmMinutes) || 15);

// USD per hour from the API's price strings, or null.
const usdOf = (v) => { const n = v === null || v === undefined || v === "" ? NaN : Number(v); return Number.isFinite(n) && n > 0 ? n : null; };
const usdText = (n) => fmtMoney(n, "USD", 4);
// "from $4.90/h": the cheapest size in its cheapest zone of the Region, or "" when no Spot price is cached there.
function fromPrice(g, region) {
  const n = usdOf(g?.prices?.[region]?.fromUsdPerHour);
  return n === null ? "" : t("price.fromHour", { price: usdText(n) });
}
// The group's sizes, each with its own lowest Spot price in the Region when it is known.
function sizesText(g, region) {
  const p = g.prices?.[region]?.sizes || {};
  return fmtList(g.sizes.map((x) => { const n = usdOf(p[x]?.usdPerHour); return n === null ? x : t("cmp.sizePrice", { type: x, price: usdText(n) }); }));
}
// The sentence for a Region and group: its score or why it has none, then the sizes with their prices and the last readings.
function cellText(key, region) {
  const g = groupOf(key);
  const st = regionState(key, region);
  const p = { region: regionLabel(region), group: groupName(g) };
  const s = scoreAt(key, region);
  let head;
  if (!st) return t("cmp.cellNotSold", p);
  if (s !== null) head = t("cmp.cellScore", { ...p, score: s });
  else if (keyLoading(key)) head = t("cmp.cellLoading", p);
  else if (st === "after-daily-update") head = t("cmp.cellPending", { ...p, time: fmtTime(cmp.sg.meta.nextDailyUpdateAt) || t("cmp.nextDaily") });
  else if (st === "planned") head = t("cmp.cellPlanned", p);
  else if (st === "limited") head = t("cmp.cellLimited", p);
  else if (cmp.sg.state[key] === "err" && hardError(cmp.sg.errors[key])) head = t("cmp.cellErr", p);
  else if (st === "waiting") head = t("cmp.cellWaiting", { ...p, time: fmtTime(cmp.sg.meta.nextWarmAt) || t("cmp.nextWarm") });
  else head = joinSentences([t("cmp.cellNoScore", p), limits.probeEnabled ? t("cmp.cellTestHere") : ""]);
  const from = fromPrice(g, region);
  const past = s !== null ? readingsOf(g.history, region, READINGS_TIP) : [];
  return joinSentences([head, t("cmp.cellSizes", { sizes: sizesText(g, region) }), from && t("cmp.cellFrom", { from }),
    past.length ? t("cmp.cellHist", { readings: past.join(t("cmp.readingSep")) }) : ""]);
}
const LATER_STATES = new Set(["after-daily-update", "planned", "limited", "waiting"]);
// The cell class: blank when the Region does not sell the group, the score in its band colour, a dash while it waits, or a dot.
function cellKind(key, region) {
  const st = regionState(key, region);
  if (!st) return "none";
  const s = scoreAt(key, region);
  if (s !== null) return band(s);
  if (keyLoading(key)) return "loading";
  if (LATER_STATES.has(st)) return "pend";
  if (cmp.sg.state[key] === "err" && hardError(cmp.sg.errors[key])) return "err";
  return "unk";
}
const CELL_MARK = { pend: "–", err: "•", unk: "•" };
// A Region state whose score comes later (a dash). "no-score" is not one: AWS answered and left the Region out.
function heatCell(region, key, first) {
  const fam = first ? " fam-start" : "";
  const kind = cellKind(key, region);
  let label = cellText(key, region);
  if (kind === "hi" || kind === "mid" || kind === "lo") {
    const s = scoreAt(key, region);
    // A column that asks again keeps its old scores, a little dimmed, until the new answer comes.
    const loading = keyLoading(key);
    const old = !loading && olderThan([cmp.sg.at[key]], oldScoreMinutes());
    if (old) label = joinSentences([label, `${t("cap.scoreOldTitle")}.`]);
    return h("td", { class: `hc hc-${kind}${loading ? " hc-stale" : ""}${old ? " score-old" : ""}${fam}`, title: label, "aria-label": label }, fmtNumber(s));
  }
  return h("td", { class: `hc hc-${kind}${fam}`, title: label, "aria-label": label }, CELL_MARK[kind] || "");
}

// Shown groups by family, in the API's order: [[family, [keys]]].
function keysByFamily(keys) {
  const out = [];
  for (const k of keys) {
    const f = groupOf(k).family;
    if (!out.length || out[out.length - 1][0] !== f) out.push([f, []]);
    out[out.length - 1][1].push(k);
  }
  return out;
}

// The column heading popover: the group and its sizes.
// The popover: the group name, then one size per line.
function groupTipLines(g) {
  return [t("cmp.groupTip", { group: groupName(g) }), ...g.sizes];
}
// A GPU count heading: the count is the control (hover, focus, or a tap shows the popover; Escape closes it), as on the
// quota names. Screen readers read "g6e · 4 GPUs". `end`: a heading in the right half opens its popover to the left.
function groupHeading(g, first, end) {
  const id = `cmp-gh-${g.key.replace(/[^a-z0-9-]/g, "-")}`;
  const pop = h("span", { id: `${id}-pop`, class: `popover gh-pop${end ? " gh-end" : ""}`, role: "tooltip", hidden: true });
  const name = h("span", { id, class: "q-tip gh-tip", tabindex: "0", role: "button", "aria-expanded": "false", "aria-describedby": pop.id, "data-fk": `gh|${g.key}` },
    h("span", { class: "sr-only" }, `${g.family} · `), gpuText(g.gpus));
  const group = { pop, btns: [name], fill: () => { const [title, ...sizes] = groupTipLines(g); pop.replaceChildren(h("span", { class: "pop-line pop-title" }, title), ...sizes.map((x) => h("span", { class: "pop-line pop-size" }, x))); } };
  wireInfo(name, group, true);
  pop.addEventListener("mouseenter", () => hoverIn(group, name));
  pop.addEventListener("mouseleave", () => hoverOut(group));
  return h("th", { scope: "col", class: `size${first ? " fam-start" : ""}`, "data-group": g.key }, name, pop);
}

function renderHeatTable() {
  const table = $("cmp-table");
  const head = table.tHead, body = table.tBodies[0];
  const corner = (rows) => h("th", { scope: "col", class: "corner", rowspan: rows > 1 ? rows : null }, t("col.region"));
  if (!cmp.sg.loaded) {
    if (infoOpen && head.contains(infoOpen.opener)) closeInfo();
    cmpHeadSig = null;
    head.replaceChildren(h("tr", { class: "fam-row" }, corner(1)));
    if (scoresLoading()) { body.replaceChildren(...skeletonRows(1, Math.min(limits.regions.length, SKELETON_ROWS * 2) || SKELETON_ROWS)); table.setAttribute("aria-busy", "true"); }
    else { body.replaceChildren(emptyRow(1, t("cmp.noScores"))); setReloading(table, false); }
    renderIdleRegions([]);
    return;
  }
  const keys = shownKeys();
  const fams = keysByFamily(keys);
  const firsts = new Set(fams.map(([, list]) => list[0]));
  // The headings are built again only when what they show changes, so an open popover on one stays open through a render.
  const sig = JSON.stringify([locale, keys.map((k) => [k, groupTipLines(groupOf(k))])]);
  if (sig !== cmpHeadSig) {
    if (infoOpen && head.contains(infoOpen.opener)) closeInfo();
    cmpHeadSig = sig;
    head.replaceChildren(
      h("tr", { class: "fam-row" }, corner(keys.length ? 2 : 1),
        ...fams.map(([f, list]) => h("th", { scope: "colgroup", colspan: list.length > 1 ? list.length : null, class: "fam fam-start" }, f))),
      ...(keys.length ? [h("tr", { class: "size-row" }, ...keys.map((k, i) => groupHeading(groupOf(k), firsts.has(k), i >= keys.length / 2)))] : []));
  }
  const { shown, idle } = splitRegions(keys);
  body.replaceChildren(...shown.map((row) => {
    const region = row.region;
    const sel = cmp.selected === region;
    const th = h("th", { scope: "row", class: "rh" }, regionButton(region));
    if (row.error) {
      return h("tr", { class: sel ? "sel" : null }, th, h("td", { colspan: keys.length > 1 ? keys.length : null, class: "row-err" },
        isNotEnabled(row) ? t("common.notEnabled") : regionErrorText(row)));
    }
    return h("tr", { class: sel ? "sel" : null }, th, ...keys.map((k) => heatCell(region, k, firsts.has(k))));
  }));
  renderIdleRegions(idle);
  if (!limits.regions.length) body.replaceChildren(emptyRow(keys.length + 1, t("cmp.noRegions")));
  // While the first build runs, the notice above says so; "no groups" is only for a server that has built them and found none.
  else if (!keys.length) body.replaceChildren(emptyRow(1, t(firstBuild() ? "cmp.noScores" : "cmp.noGroups")));
  setReloading(table, false);
  if (scoresLoading()) table.setAttribute("aria-busy", "true");   // scores are still coming
  updateHeatMore();
}
// The Region code in a row header (and in the list of Regions that sell none of the shown groups): it opens the Region below.
// With a name it shows the name and, in a smaller muted second line, the code; the full label is in the title.
function regionButton(region) {
  const named = regionName(region);
  return h("button", { type: "button", class: "link", "aria-pressed": String(cmp.selected === region), "data-fk": `rh|${region}`,
    title: t("cmp.selectRegion", { region: regionLabel(region) }), onclick: () => selectRegion(region) },
    named ? [h("span", { class: "rn" }, named), " ", h("span", { class: "rc" }, region)] : region);
}
// Rows on screen, in the order of /api/config: a Region with a state in a shown group, and a Region whose offerings failed.
// A Region that sells none of the shown groups goes to one collapsed line under the table instead of a row of its own.
function splitRegions(keys) {
  const rows = new Map((cmp.matrix?.regions || []).map((r) => [r.region, r]));
  const shown = [], idle = [];
  for (const region of limits.regions) {
    const has = keys.some((k) => regionState(k, region));
    const row = rows.get(region);
    if (has) shown.push({ region });
    else if (row?.error) shown.push(row);
    else if (keys.length) idle.push(region);
    else shown.push({ region });
  }
  return { shown, idle };
}
// "N Regions sell none of the shown families": a disclosure button that lists them, each opening its detail like a row.
function renderIdleRegions(idle) {
  const box = $("cmp-idle");
  box.hidden = !idle.length;
  if (!idle.length) { box.replaceChildren(); return; }
  const open = cmp.idleOpen;
  const toggle = h("button", { type: "button", class: "link idle-toggle", id: "cmp-idle-toggle", "aria-expanded": String(open), "aria-controls": "cmp-idle-list",
    "data-fk": "idle-toggle", onclick: () => { cmp.idleOpen = !cmp.idleOpen; renderCompare(); } }, t("cmp.idleSummary", { count: idle.length }));
  const list = h("div", { id: "cmp-idle-list", class: "idle-list", hidden: !open }, idle.map(regionButton));
  box.replaceChildren(toggle, list);
}

// A fade on the right edge tells that more columns are off screen; it goes away at the end of the scroll.
function updateHeatMore() {
  const sc = $("cmp-table-wrap");
  $("cmp-heat-box").classList.toggle("more", sc.scrollWidth > sc.clientWidth + 1 && sc.scrollLeft + sc.clientWidth < sc.scrollWidth - 1);
}

// The Region's score groups with their score and price, then the other GPU types it sells; an item opens its actions.
function renderRegionDetail() {
  const box = $("cmp-detail");
  const region = cmp.selected;
  if (!region || !limits.regions.includes(region)) { box.hidden = true; box.replaceChildren(); return; }
  box.hidden = false;
  const row = cmp.matrix?.regions.find((r) => r.region === region) || null;
  const head = h("div", { class: "detail-head" }, h("h3", { id: "cmp-detail-title" }, t("cmp.detailTitle", { region: regionLabel(region) })),
    h("button", { type: "button", class: "ghost small", "data-fk": "detail-close", onclick: () => selectRegion(null) }, t("action.close")));
  const keys = shownKeys().filter((k) => regionState(k, region));
  const scored = new Set(knownGroups().flatMap((g) => g.sizes));
  const others = row && !row.error ? sortTypes([...offeredIn(row)].filter((x) => !scored.has(x))) : [];
  const ids = [...keys.map((k) => `g|${k}`), ...others.map((x) => `t|${x}`)];
  const content = [];
  if (keys.length) content.push(h("div", { class: "fam-group" }, h("h4", {}, t("cmp.detailGroups")), h("div", { class: "chips" }, keys.map((k) => groupChip(region, k)))));
  if (others.length) content.push(h("div", { class: "fam-group" }, h("h4", {}, t("cmp.detailOthers")), h("div", { class: "chips" }, others.map((x) => typeChip(x)))));
  if (!cmp.matrix && !keys.length) content.push(h("p", { class: "muted" }, cmp.loading ? t("common.loading") : ""));
  else if (row?.error && !keys.length) content.push(h("p", { class: "muted" }, isNotEnabled(row) ? t("common.notEnabled") : regionErrorText(row)));
  else if (!content.length) content.push(h("p", { class: "muted" }, t("list.noneOfferedRegion")));
  else content.splice(0, content.length, h("div", { class: "fam-groups" }, content));
  if (ids.includes(cmp.chip)) content.push(chipActions(region, cmp.chip));
  box.replaceChildren(head, ...content.filter(Boolean));
}

function groupChip(region, key) {
  const g = groupOf(key);
  const id = `g|${key}`;
  const open = cmp.chip === id;
  const kind = cellKind(key, region);
  const s = scoreAt(key, region);
  const from = fromPrice(g, region);
  return h("button", { type: "button", class: `tchip${open ? " open" : ""}`, "aria-expanded": String(open), "data-fk": `chip|${id}`, title: cellText(key, region),
    "aria-label": cellText(key, region), onclick: () => toggleChip(id) },
  h("span", { class: "tchip-size" }, groupName(g)),
  s !== null ? h("b", { class: `tchip-score hc-${band(s)}${keyLoading(key) ? " hc-stale" : ""}` }, fmtNumber(s))
    : kind === "loading" ? h("b", { class: "tchip-score hc-loading", "aria-hidden": "true" }, "  ")
      : kind === "pend" ? h("b", { class: "tchip-score hc-pend", "aria-hidden": "true" }, "–") : null,
  from ? h("small", { class: "tchip-price", "aria-hidden": "true" }, from) : null);
}

function typeChip(type) {
  const id = `t|${type}`;
  const open = cmp.chip === id;
  return h("button", { type: "button", class: `tchip${open ? " open" : ""}`, "aria-expanded": String(open), "data-fk": `chip|${id}`, title: type,
    onclick: () => toggleChip(id) }, h("span", { class: "tchip-size" }, type));
}

// Capacity Block types: the list EC2 gave for the Region when it has arrived, else the configured families.
const cbOffers = (types, type) => (types ? types.includes(type) : (limits.capacityBlockFamilies || []).includes(familyOf(type)));

// Test launch, On-Demand and Spot, Capacity Blocks for a group (its first size the Region sells) or a type.
function chipActions(region, id) {
  const [kind, name] = [id.slice(0, 1), id.slice(2)];
  const g = kind === "g" ? groupOf(name) : null;
  const row = cmp.matrix?.regions.find((r) => r.region === region);
  const sold = row && !row.error ? offeredIn(row) : null;
  const sizes = g ? g.sizes.filter((x) => !sold || sold.has(x)) : [name];
  const type = sizes[0] || (g ? g.sizes[0] : name);
  const block = sizes.find((x) => cbOffers(cbTypeCache.of(region), x));
  const title = t("cmp.actionsTitle", { what: g ? groupName(g) : name, region: regionLabel(region) });
  const buttons = [
    limits.probeEnabled ? h("button", { type: "button", onclick: () => openTest(region, g ? `group:${g.key}` : `type:${name}`, type) }, t("cmp.actTest")) : null,
    h("button", { type: "button", class: limits.probeEnabled ? "ghost" : null, onclick: () => openRegion(region, type) }, t("cmp.actCapacity")),
    block ? h("button", { type: "button", class: "ghost", onclick: () => openBlocks(region, block) }, t("cmp.actBlocks")) : null,
  ].filter(Boolean);
  buttons[0].setAttribute("data-fk", "act-first");
  return h("div", { class: "chip-menu", role: "group", "aria-label": title },
    h("span", { class: "chip-menu-title" }, title), ...buttons,
    h("button", { type: "button", class: "link", onclick: () => toggleChip(null) }, t("action.cancel")));
}

// Marker state: the highest Spot score among the shown groups the Region has, in the heatmap's bands.
// "off" = the offerings failed and no group lists the Region; "none" = in none of the shown groups; "hi/mid/lo" = the best score
// (with the group that has it, the first in the API's order on a tie); "loading" = no score yet and some are coming; "err" = every
// group's scores failed; "pend" = every group's score comes later; "unk" = AWS returned no score for the Region. `stale` = that best score is being asked again.
function markerState(region, keys) {
  const mine = keys.filter((k) => regionState(k, region));
  if (!mine.length) {
    const row = cmp.matrix?.regions.find((r) => r.region === region);
    return { state: row?.error ? "off" : "none" };
  }
  let best = null, key = null;
  for (const k of mine) { const s = scoreAt(k, region); if (s !== null && (best === null || s > best)) { best = s; key = k; } }
  if (best !== null) return { state: band(best), score: best, group: groupName(groupOf(key)), stale: keyLoading(key) };
  if (mine.some(keyLoading)) return { state: "loading" };
  const failed = mine.every((k) => cmp.sg.state[k] === "err" && hardError(cmp.sg.errors[k]));
  if (failed) return { state: "err" };
  // Every group's score comes later: "pend"; else AWS's answer left the Region out for a group: "unk".
  return { state: mine.every((k) => LATER_STATES.has(regionState(k, region))) ? "pend" : "unk" };
}

// Place each Region label next to its dot, trying other spots when it would overlap a placed label or dot.
function placeLabels(points) {
  const W = 6.2, H = 11, placed = [];
  const hit = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  const dots = points.map((p) => ({ x: p.cx - 7, y: p.cy - 7, w: 14, h: 14 }));
  return points.map((p) => {
    const w = p.label.length * W;
    const tries = [[10, -5], [-10 - w, -5], [-w / 2, -20], [-w / 2, 9], [10, -17], [10, 7], [-10 - w, -17], [-10 - w, 7],
      [10, 19], [10, -29], [-10 - w, 19], [-10 - w, -29]];
    let best = null;
    for (const [dx, dy] of tries) {
      const box = { x: Math.min(Math.max(p.cx + dx, 2), MAP_W - w - 2), y: Math.min(Math.max(p.cy + dy, 2), MAP_H - H - 2), w, h: H };
      if (!placed.some((b) => hit(box, b)) && !dots.some((b) => hit(box, b))) { best = box; break; }
    }
    best = best || { x: Math.min(p.cx + 10, MAP_W - w - 2), y: p.cy - 5, w, h: H };
    placed.push(best);
    return best;
  });
}

// The land outline from world.json, fetched once when the map is first shown. Without it the map keeps the grid only.
function loadLand() {
  if (cmp.land !== undefined) return;
  cmp.land = null;
  fetch("world.json").then((r) => (r.ok ? r.json() : null)).then((j) => {
    if (j && typeof j.d === "string" && j.d) { cmp.land = j.d; if (cmp.view === "map") renderCompare(); }
  }).catch(() => { /* grid only */ });
}

// Called with the Region label; the label it builds names the Region in full.
const MAP_STATE_TEXT = {
  hi: (region, m) => t("map.stateScore", { region, score: m.score, group: m.group }),
  mid: (region, m) => t("map.stateScore", { region, score: m.score, group: m.group }),
  lo: (region, m) => t("map.stateScore", { region, score: m.score, group: m.group }),
  unk: (region) => joinSentences([t("map.stateNoScore", { region }), limits.probeEnabled ? t("cmp.cellTestHere") : ""]),
  pend: (region) => t("map.statePending", { region }),
  loading: (region) => t("map.stateLoading", { region }),
  err: (region) => t("map.stateScoreErr", { region }),
  none: (region) => t("map.stateNone", { region }),
  off: (region) => t("map.stateOff", { region }),
};
// Legend: [dot class, text key]; the Loading entry only shows while scores for the shown groups load.
const MAP_LEGEND = [["hi", "cmp.legendHi"], ["mid", "cmp.legendMid"], ["lo", "cmp.legendLo"], ["pend", "cmp.legendPending"], ["unk", "map.legendNoScore"],
  ["none", "cmp.legendBlank"], ["err", "cmp.legendErr"], ["off", "map.legendOff"], ["loading", "cmp.legendLoading"]];

// Map zoom and pan: k is the scale (1 to MAP_ZOOM_MAX); x, y the top-left corner of the view in map units. The land and the
// grid scale; markers and labels are placed in view units, so they keep their size and spread apart as the map zooms in.
const MAP_ZOOM_MAX = 8, MAP_ZOOM_STEP = 1.5;
const mapView = { k: 1, x: 0, y: 0 };
const toView = (x, y) => [(x - mapView.x) * mapView.k, (y - mapView.y) * mapView.k];
const clampZoom = (k) => Math.min(Math.max(k, 1), MAP_ZOOM_MAX);
// `k` is already within 1 to MAP_ZOOM_MAX; the corner is kept inside the map.
function setMapView(k, x, y) {
  mapView.k = k;
  mapView.x = Math.min(Math.max(x, 0), MAP_W - MAP_W / mapView.k);
  mapView.y = Math.min(Math.max(y, 0), MAP_H - MAP_H / mapView.k);
}
// Draw the map again; focus stays on a marker the user chose.
const redrawMap = () => keepFocus($("cmp-map-wrap"), renderCmpMap);
// Zoom by `f`, keeping the point (cx, cy) of the view (in view units; the centre by default) in place.
function zoomMap(f, cx = MAP_W / 2, cy = MAP_H / 2) {
  const wx = mapView.x + cx / mapView.k, wy = mapView.y + cy / mapView.k;
  const k = clampZoom(mapView.k * f);
  setMapView(k, wx - cx / k, wy - cy / k);
  redrawMap();
}
function resetMap() { setMapView(1, 0, 0); redrawMap(); }
// The buttons, Ctrl or Cmd with the wheel (a plain wheel scrolls the page), a drag to pan, and a pinch on touch screens.
function setUpMapZoom() {
  const map = $("cmp-map");
  $("cmp-zoom-in").addEventListener("click", () => zoomMap(MAP_ZOOM_STEP));
  $("cmp-zoom-out").addEventListener("click", () => zoomMap(1 / MAP_ZOOM_STEP));
  $("cmp-zoom-reset").addEventListener("click", resetMap);
  // A pointer position in view units, or null when the map has no size on screen.
  const at = (ev) => { const r = map.getBoundingClientRect(); return r.width > 0 && r.height > 0 ? { x: (ev.clientX - r.left) * MAP_W / r.width, y: (ev.clientY - r.top) * MAP_H / r.height } : null; };
  map.addEventListener("wheel", (ev) => {
    if (!ev.ctrlKey && !ev.metaKey) return;
    ev.preventDefault();
    const p = at(ev);
    zoomMap(ev.deltaY < 0 ? 1.25 : 0.8, p?.x, p?.y);
  }, { passive: false });
  const down = new Map();   // pointer id -> its last position
  let moved = 0, dragged = false;
  map.addEventListener("pointerdown", (ev) => {
    const p = at(ev);
    if (!p || ev.button > 0) return;
    down.set(ev.pointerId, p);
    if (down.size === 1) { moved = 0; dragged = false; }
  });
  map.addEventListener("pointermove", (ev) => {
    const prev = down.get(ev.pointerId), p = at(ev);
    if (!prev || !p) return;
    down.set(ev.pointerId, p);
    if (down.size === 2) {
      const other = [...down].find(([id]) => id !== ev.pointerId)[1];
      const d0 = Math.hypot(prev.x - other.x, prev.y - other.y), d1 = Math.hypot(p.x - other.x, p.y - other.y);
      dragged = true;
      if (d0 > 0) zoomMap(d1 / d0, (p.x + other.x) / 2, (p.y + other.y) / 2);
      return;
    }
    if (mapView.k === 1) return;
    moved += Math.hypot(p.x - prev.x, p.y - prev.y);
    if (moved < 4) return;
    if (!dragged) { dragged = true; window.getSelection?.()?.removeAllRanges(); try { map.setPointerCapture(ev.pointerId); } catch { /* the pointer is gone */ } }
    setMapView(mapView.k, mapView.x - (p.x - prev.x) / mapView.k, mapView.y - (p.y - prev.y) / mapView.k);
    redrawMap();
  });
  for (const type of ["pointerup", "pointercancel"]) map.addEventListener(type, (ev) => down.delete(ev.pointerId));
  // The click that ends a drag does not open a Region.
  map.addEventListener("click", (ev) => { if (dragged) { dragged = false; ev.preventDefault(); ev.stopPropagation(); } }, true);
}

function renderCmpMap() {
  loadLand();
  const map = $("cmp-map");
  const keys = shownKeys();
  // The hatch a loading dot takes instead of its shimmer when the visitor asks for reduced motion.
  const hatch = svg("pattern", { id: "mk-hatch", width: 4, height: 4, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  hatch.append(svg("rect", { width: 4, height: 4, fill: "#E1E6EC" }), svg("rect", { width: 2, height: 4, fill: "#B3BDC9" }));
  const defs = svg("defs");
  defs.append(hatch);
  const { k } = mapView;
  // The land and the grid lines in map units, scaled to the view (their strokes keep their width).
  const world = svg("g", { class: "map-world", transform: `translate(${(-mapView.x * k).toFixed(2)} ${(-mapView.y * k).toFixed(2)}) scale(${k})` });
  if (cmp.land) world.append(svg("path", { class: "map-land", d: cmp.land }));
  const labels = [];
  for (let lon = -150; lon <= 150; lon += 30) {
    const [x] = project(0, lon);
    world.append(svg("line", { class: "grat", x1: x, y1: 0, x2: x, y2: MAP_H }));
    const [vx] = toView(x, 0);
    if (vx >= 0 && vx <= MAP_W - 20) labels.push(svg("text", { class: "grat-label", x: (vx + 2).toFixed(1), y: MAP_H - 4 },
      lon < 0 ? t("map.west", { deg: -lon }) : lon > 0 ? t("map.east", { deg: lon }) : "0"));
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    const [, y] = project(lat, 0);
    world.append(svg("line", { class: lat === 0 ? "grat eq" : "grat", x1: 0, y1: y, x2: MAP_W, y2: y }));
    const [, vy] = toView(0, y);
    if (vy >= 12 && vy <= MAP_H) labels.push(svg("text", { class: "grat-label", x: 3, y: (vy - 3).toFixed(1) },
      lat < 0 ? t("map.south", { deg: -lat }) : lat > 0 ? t("map.north", { deg: lat }) : "0"));
  }
  map.replaceChildren(defs, world, ...labels, svg("rect", { class: "frame", x: 0.5, y: 0.5, width: MAP_W - 1, height: MAP_H - 1 }));
  map.classList.toggle("zoomed", k > 1);
  $("cmp-zoom-in").disabled = k >= MAP_ZOOM_MAX;
  $("cmp-zoom-out").disabled = k <= 1;
  $("cmp-zoom-reset").disabled = k <= 1;
  fillPoints($("cmp-map-desc"), t("map.desc"));
  const loading = scoresLoading();
  $("cmp-legend").replaceChildren(...MAP_LEGEND.filter(([c]) => c !== "loading" || loading).map(([c, key]) => h("span", { class: "lg-item" }, h("span", { class: `dot dot-${c}`, "aria-hidden": "true" }), t(key))));
  if (!cmp.sg.loaded) { $("cmp-unplaced").textContent = loading ? t("common.loading") : ""; return; }
  const unplaced = [];
  const points = [];
  for (const region of limits.regions) {
    const at = REGION_COORDS[region];
    if (!at) { unplaced.push(region); continue; }
    const [cx, cy] = toView(...project(at[0], at[1]));
    // Zoomed in, a Region outside the view has no marker (zoom out or pan to reach it).
    if (cx < -6 || cy < -6 || cx > MAP_W + 6 || cy > MAP_H + 6) continue;
    points.push({ cx, cy, label: region });
  }
  const boxes = placeLabels(points);
  points.forEach((p, i) => {
    const region = p.label;
    const m = markerState(region, keys);
    const state = m.state;
    const label = MAP_STATE_TEXT[state](regionLabel(region), m);
    const box = boxes[i];
    // A label moved away from its dot gets a leader line to the nearest point of the label.
    const ex = Math.min(Math.max(p.cx, box.x), box.x + box.w), ey = Math.min(Math.max(p.cy, box.y), box.y + box.h);
    if (Math.hypot(ex - p.cx, ey - p.cy) > 12) map.append(svg("line", { class: "leader", x1: p.cx.toFixed(1), y1: p.cy.toFixed(1), x2: ex.toFixed(1), y2: ey.toFixed(1) }));
    // A link per Region, so the marker works with the keyboard; choosing it opens the Region below the map.
    const sel = cmp.selected === region;
    const link = svg("a", { href: "#spot-score", class: "mk-link", "aria-label": label, "data-region": region, "data-fk": `mk|${region}` });
    if (sel) link.setAttribute("aria-current", "true");
    if (sel) link.append(svg("circle", { class: "mk-ring", cx: p.cx.toFixed(1), cy: p.cy.toFixed(1), r: 10 }));
    link.append(svg("circle", { class: `mk mk-${state}${m.stale ? " mk-stale" : ""}`, cx: p.cx.toFixed(1), cy: p.cy.toFixed(1), r: 6 }), svg("title", {}, label));
    link.addEventListener("click", (ev) => { ev.preventDefault(); selectRegion(region); });
    map.append(link, svg("text", { class: `mk-label${sel ? " mk-label-sel" : ""}`, x: box.x.toFixed(1), y: (box.y + 9).toFixed(1), "aria-hidden": "true" }, region));
  });
  $("cmp-unplaced").textContent = unplaced.length ? t("map.unplaced", { regions: fmtList(regionLabels(unplaced)) }) : "";
}
// ======================================================================
// 2. On-Demand and Spot: the Spot score of the type's group, list prices, test launches, Reserve with CLI
// ======================================================================
// results: Region -> the last test launch started there ({ target, market, r } or { target, market, error }).
// target: the test launch choice ("group:<key>" or "type:<name>"); targetFor: the Region and type it was chosen for.
// noVpc: Regions where a test launch answered no-default-vpc; today: the newest daily counts from the API.
const cap = { offered: [], typesRegion: null, typesErr: null, data: null, loading: false, pendingType: null, pendingTarget: null,
  targetFor: null, results: new Map(), testing: false, noVpc: new Set(), today: null };
let capSeq = 0;
const PROBE_RESULT = {
  "launched": () => t("result.launched"), "no-capacity": () => t("result.noCapacity"), "quota": () => t("result.quota"),
  "unsupported": () => t("result.unsupported"), "error": () => t("result.error"), "in-progress": () => t("result.inProgress"),
};
// The same results in lower case, for use inside a sentence ("Latest: no capacity, 3 hours ago").
const PROBE_RESULT_INLINE = {
  "launched": () => t("resultInline.launched"), "no-capacity": () => t("resultInline.noCapacity"), "quota": () => t("resultInline.quota"),
  "unsupported": () => t("resultInline.unsupported"), "error": () => t("resultInline.error"),
};
const resultText = (r) => (PROBE_RESULT[r] ? PROBE_RESULT[r]() : r);
const resultInline = (r) => (PROBE_RESULT_INLINE[r] ? PROBE_RESULT_INLINE[r]() : r);
const resultKind = (r) => (r === "launched" ? "ok" : r === "no-capacity" ? "bad" : "warn");
const marketText = (m) => (m === "spot" ? t("probe.modeSpot") : t("probe.modeOd"));

// On-Demand list price per Region and type, read once for this tab.
// An entry older than the price cache time is read again, without refresh=true.
const odPrices = new Map();   // "region|type" -> { promise, at }
function getOdPrice(region, type, again = false) {
  const key = `${region}|${type}`;
  let e = odPrices.get(key);
  if (e && (again || (e.at && olderThan([e.at], MINUTES.odPrice())))) { odPrices.delete(key); e = null; }
  if (!e) {
    const mine = { at: null, promise: null };
    mine.promise = api("/api/on-demand-price", { region, instanceType: type }).then((r) => { mine.at = r.fetchedAt || nowIso(); return r; },
      (err) => { if (odPrices.get(key) === mine) odPrices.delete(key); throw err; });
    odPrices.set(key, mine);
    e = mine;
  }
  return e.promise;
}
// The price as a number, or null when the answer has none.
const odUsd = (r) => { const n = r && r.usdPerHour !== null && r.usdPerHour !== undefined ? Number(r.usdPerHour) : NaN; return Number.isFinite(n) && n > 0 ? n : null; };
const percentOf = (part, whole) => Math.round((Number(part) / whole) * 100);

function fillCapTypes() {
  const shown = shownTypes(cap.offered);
  // A type chosen on the Spot score tab stays selectable even when it is not on the saved list.
  if (cap.pendingType && cap.offered.includes(cap.pendingType) && !shown.includes(cap.pendingType)) shown.push(cap.pendingType);
  if (cap.pendingType && shown.includes(cap.pendingType)) $("cap-type").replaceChildren(h("option", { value: cap.pendingType }, cap.pendingType));
  fillTypeSelect($("cap-type"), shown);
  cap.pendingType = null;
  $("cap-type").disabled = !shown.length;
  fillPoints($("cap-type-note"), cap.typesErr ? "" : typeNoteText(cap.offered, shown));
}

// The group answer of /api/spot-scores?instanceType=: { state (grouped, pending, off), group or null, the schedule, and the
// group's first error (a Refresh that failed, or that could not repeat the request) or null }.
function scoreView(r) {
  const g = (Array.isArray(r.groups) ? r.groups : []).filter(validGroup).find((x) => x.key === r.group);
  const e = g && Array.isArray(g.errors) && g.errors.length && isObj(g.errors[0]) ? g.errors[0] : null;
  return { state: r.scoreState, group: g ? cleanGroup(g) : null, nextDailyUpdateAt: r.nextDailyUpdateAt || null, error: e };
}

async function loadCapacity(refresh = false) {
  const mine = ++capSeq;
  const region = $("cap-region").value;
  $("cap-title").textContent = t("cap.title", { region: regionLabel(region) });
  if (cap.typesRegion !== region || cap.pendingType || cap.typesErr || typeCache.stale()) {
    try { cap.offered = await regionTypes(region); cap.typesErr = null; } catch (e) { cap.offered = []; cap.typesErr = e; }
    if (mine !== capSeq) return null;
    cap.typesRegion = region;
    fillCapTypes();
  }
  // An earlier call that this one overtook left the flag set; the early returns below end this load without the data calls.
  cap.loading = false;
  const type = $("cap-type").value;
  fillTestTargets();
  if (cap.typesErr) {
    cap.data = null;
    renderCapacity();
    $("cap-alerts").replaceChildren(errorAlert(t("cap.typesFailed", { error: cap.typesErr.message }), () => loadTab("capacity", true, false)));
    return false;
  }
  if (!type) { cap.data = null; renderCapacity(); return true; }
  remember(region, type);
  cap.loading = true;
  const prev = cap.data;
  if (!cap.data || cap.data.region !== region || cap.data.type !== type) cap.data = null;
  renderCapacity();
  // Only the Refresh button (refresh=true) asks the server to repeat its Spot score request; opening the tab or changing a choice does not.
  const fresh = refresh ? { refresh: "true" } : {};
  const parts = await Promise.allSettled([
    api("/api/spot-scores", { instanceType: type, ...fresh }),
    api("/api/spot-prices", { region, instanceType: type }),
    api("/api/probes", { region }),
    api("/api/instance-type-zones", { region, instanceType: type }),
    getOdPrice(region, type, refresh),
  ]);
  if (mine !== capSeq) return null;
  const [scores, prices, probes, zones, od] = parts;
  const err = (p) => (p.status === "rejected" ? p.reason : null);
  // A part that fails keeps what the page showed for the same Region and type, next to its error, so the chips and rows stay.
  const old = prev && prev.region === region && prev.type === type ? prev : null;
  const keep = (p, value, field, empty) => (p.value ? value : err(p) && old ? old[field] : empty);
  const score = keep(scores, scores.value && scoreView(scores.value), "score", null);
  if (probes.value?.today) cap.today = probes.value.today;
  cap.data = {
    region, type, score,
    prices: keep(prices, prices.value?.prices || {}, "prices", {}),
    currency: prices.value?.currency || (old && err(prices) ? old.currency : "USD"),
    probes: keep(probes, probes.value, "probes", null),
    zones: zones.value?.zones || (err(zones) && old ? old.zones : null),
    od: od.value || (err(od) && old ? old.od : null),
    // When AWS was asked: the group's Spot score and the Spot prices, and the list price on its own.
    stamps: [scores.value && score?.group ? stampOf(score.group, refresh) : null, prices.value ? prices.value.fetchedAt : null].filter(Boolean),
    odStamp: od.value ? od.value.fetchedAt || null : null,
    blocked: refresh && score?.group ? blockedSeconds([score.group]) : 0,
    errors: { scores: err(scores), prices: err(prices), probes: err(probes), zones: err(zones), od: err(od) },
  };
  cap.loading = false;
  renderCapacity();
  return parts.every((p) => p.status === "fulfilled");
}

const capStale = () => Boolean(cap.data) && (olderThan(cap.data.stamps, Math.min(MINUTES.scores(), MINUTES.spotPrice()))
  || olderThan([cap.data.odStamp], MINUTES.odPrice()) || typeCache.stale());

// The Spot score of the type's group in every Region that has one, best first; a line for the Regions that join later.
function renderCapScores() {
  const box = $("cap-scores");
  const d = cap.data;
  const s = d?.score;
  const g = s?.group;
  $("cap-score-legend").textContent = g ? t("score.legend") : "";
  fillPoints($("cap-scores-intro"), g ? t("cap.scoresIntro", { group: groupName(g), type: d.type }) : "");
  if (!d) {
    $("cap-scores-note").textContent = "";
    box.replaceChildren(...(cap.loading ? [skeleton(), skeleton(), skeleton()] : []));
    // A load overtaken by a Region with no type list leaves no data: clear the faded state of the chips it replaced.
    box.classList.remove("reloading");
    if (cap.loading) box.setAttribute("aria-busy", "true"); else box.removeAttribute("aria-busy");
    return;
  }
  const time = fmtTime(s?.nextDailyUpdateAt) || t("cmp.nextDaily");
  let note = "";
  if (s?.state === "off") note = t("cap.scoreOff", { family: familyOf(d.type) });
  else if (s?.state === "pending" || (s && !g)) note = t("cap.scorePending", { type: d.type, time });
  else if (g) {
    const later = Object.entries(g.regionStates).filter(([, st]) => st === "after-daily-update").map(([r]) => r).sort();
    if (later.length) note = t("cap.pendingRegions", { regions: fmtList(regionLabels(later)), time });
  }
  fillPoints($("cap-scores-note"), note);
  const scores = g ? Object.entries(g.scores).filter(([r, v]) => typeof v === "number" && limits.regions.includes(r)).sort((a, b) => b[1] - a[1]) : [];
  box.replaceChildren(...scores.map(([region, v]) => {
    const from = fromPrice(g, region);
    return h("span", { class: `chip${region === d.region ? " chip-sel" : ""}`, title: joinSentences([scoreWord(v), from]) },
      regionLabel(region), h("b", { class: `score ${scoreClass(v)}` }, v));
  }));
  if (g && !scores.length && !d.errors.scores) box.append(h("span", { class: "muted" }, t("cap.noScores")));
  // A type with no score group (its family is not monitored, or its score is still pending) has no chips. Say so here,
  // so the section is not empty; the note below says why. A failed score request keeps its own error message instead.
  if (!g && !d.errors.scores) box.append(h("span", { class: "muted" }, t("cap.scoreNone", { type: d.type })));
  setReloading(box, cap.loading);
}

// "On-Demand list price: $x/hour" for the chosen Region and type, or "Not available" when AWS lists none.
function renderOdPrice() {
  const d = cap.data;
  const line = $("cap-od-text");
  if (!d || (cap.loading && !d.od)) { line.textContent = cap.loading ? t("common.loading") : ""; return; }
  if (d.errors.od) { line.textContent = t("cap.failOdPrice", { error: d.errors.od.message }); return; }
  const usd = odUsd(d.od);
  line.textContent = usd === null ? t("price.odMissing") : t("price.odLine", { price: fmtMoney(usd, d.od.currency, 4) });
}

// The Spot price of a zone, with its share of the On-Demand list price when that price is known.
function spotPriceCell(az) {
  const d = cap.data;
  const price = d.prices[az];
  if (!price) return h("span", { class: "muted" }, t("cap.noPrice"));
  const usd = odUsd(d.od);
  const money = fmtMoney(price, d.currency, 4);
  if (usd === null || !Number.isFinite(Number(price))) return money;
  return [money, " ", h("small", { title: t("price.pctTitle") }, t("price.pctOd", { percent: fmtPercent(percentOf(price, usd)) }))];
}

function renderCapacity() {
  if (!limits) return;
  const region = $("cap-region").value;
  $("cap-title").textContent = t("cap.title", { region: regionLabel(region) });
  if (cap.typesRegion) fillPoints($("cap-type-note"), cap.typesErr ? "" : typeNoteText(cap.offered, shownTypes(cap.offered)));
  renderCapScores();
  renderInfo();
  renderOdPrice();
  renderTestPanel();
  setRefreshBusy("cap-refresh", cap.loading);
  setRefreshNote("cap-refresh-note", cap.data ? cap.data.blocked : 0);
  renderFresh();
  const table = $("cap-table");
  const body = table.tBodies[0];
  const cols = 3;
  if (!cap.data) {
    $("cap-alerts").replaceChildren();
    if (cap.loading) tableLoading(table, cols);
    else { body.replaceChildren(emptyRow(cols, $("cap-type").value ? t("common.noData") : t("cap.noType"))); setReloading(table, false); }
    return;
  }
  const d = cap.data;
  const retry = () => loadTab("capacity", true, false);
  const failed = [["scores", "cap.failScores"], ["prices", "cap.failPrices"], ["probes", "cap.failProbes"], ["zones", "cap.failZones"]]
    .filter(([k]) => d.errors[k]);
  // The group's own error (the request answered): sps-limited gets its notice, sps-unsupported nothing, any other a Retry.
  const ge = !d.errors.scores ? d.score?.error : null;
  $("cap-alerts").replaceChildren(...[ge?.code === "sps-limited" && warnNotice(t("err.spsLimited")),
    hardError(ge) && errorAlert(t("cap.failScores", { error: scoreErrorText(ge) }), retry)].filter(Boolean),
    ...failed.map(([k, key]) => errorAlert(t(key, { error: d.errors[k].message }), retry)));
  if (cap.loading) { setReloading(table, true); return; }
  // List only the zones that offer the type; if that lookup failed, fall back to every zone with a price.
  const azs = [...new Set([...Object.keys(d.prices), ...(d.zones || [])])].filter((az) => !d.zones || d.zones.includes(az)).sort();
  body.replaceChildren(...azs.map((az) => h("tr", {},
    h("th", { scope: "row", "data-label": t("col.zone") }, az),
    h("td", { "data-label": t("cap.colPrice") }, spotPriceCell(az)),
    h("td", { class: "c-action", "data-label": t("col.action") }, reserveButton(d.region, az, d.type)))));
  if (!azs.length) body.append(emptyRow(cols, t("cap.noZones")));
  setReloading(table, false);
}

// --- test launch: one instance of a score group (EC2 picks the size and the zone) or of one size ---
const splitTarget = (v) => { const i = String(v).indexOf(":"); return i < 0 ? ["", ""] : [v.slice(0, i), v.slice(i + 1)]; };
// "g6e · 4 GPUs (g6e.12xlarge, g6e.24xlarge)" for a group, the type name for one size.
function targetText(target, region) {
  const [kind, name] = splitTarget(target);
  if (kind !== "group") return name;
  const g = knownGroups().find((x) => x.key === name);
  if (!g) return groupLabelOf(name);
  const sold = region === cap.typesRegion ? g.sizes.filter((x) => cap.offered.includes(x)) : g.sizes;
  return t("tl.groupSizes", { group: groupName(g), sizes: fmtList(sold.length ? sold : g.sizes) });
}
// The choices for the Region: per score group it sells, the group (EC2 picks a size) then each size; then the other GPU types.
function testTargets() {
  const offered = cap.offered;
  const groups = knownGroups().map((g) => [g, g.sizes.filter((x) => offered.includes(x))]).filter(([, s]) => s.length);
  const grouped = new Set(groups.flatMap(([, s]) => s));
  return { groups, others: sortTypes(offered.filter((x) => !grouped.has(x))) };
}
// The default choice: the group of the chosen type, else the type itself.
function defaultTarget(type) {
  const g = groupOfType(type);
  return g && g.sizes.some((x) => cap.offered.includes(x)) ? `group:${g.key}` : type ? `type:${type}` : "";
}
function fillTestTargets() {
  const sel = $("tl-target");
  const { groups, others } = testTargets();
  const keep = sel.value;
  sel.replaceChildren(...groups.map(([g, sizes]) => h("optgroup", { label: groupName(g) },
    h("option", { value: `group:${g.key}` }, t("tl.anySize", { group: groupName(g) })),
    sizes.map((x) => h("option", { value: `type:${x}` }, t("tl.oneSize", { type: x }))))),
  ...(others.length ? [h("optgroup", { label: t("tl.otherTypes") }, others.map((x) => h("option", { value: `type:${x}` }, x)))] : []));
  const values = [...sel.options].map((o) => o.value);
  const type = $("cap-type").value;
  const now = `${$("cap-region").value}|${type}`;
  // A new Region or type picks the default again; a choice from the Spot score tab wins once.
  const want = cap.pendingTarget && values.includes(cap.pendingTarget) ? cap.pendingTarget
    : cap.targetFor === now && values.includes(keep) ? keep : defaultTarget(type);
  sel.value = values.includes(want) ? want : values[0] || "";
  if (cap.pendingTarget && cap.typesRegion === $("cap-region").value) cap.pendingTarget = null;
  cap.targetFor = now;
  sel.disabled = !values.length;
}

// "Test launches today: 37 of 100 (all users). You: 4 of 50. The counts reset at ...". A count is null when its limit is off.
const countOf = (v) => (Number.isFinite(Number(v)) && v !== null && v !== "" ? Number(v) : null);
function todayParts(today) {
  if (!today || typeof today !== "object") return { text: "", allFull: false, userFull: false, reset: "" };
  const all = countOf(today.all), allMax = countOf(today.allLimit), user = countOf(today.user), userMax = countOf(today.userLimit);
  const reset = dailyResetText(today.resetAt);
  const text = joinSentences([all !== null && allMax ? t("tl.todayAll", { used: all, max: allMax }) : "",
    user !== null && userMax ? t("tl.todayYou", { used: user, max: userMax }) : "", (allMax || userMax) ? t("tl.resets", { time: reset }) : ""]);
  return { text, allFull: all !== null && Boolean(allMax) && all >= allMax, userFull: user !== null && Boolean(userMax) && user >= userMax, reset };
}

function renderTestPanel() {
  const on = Boolean(limits.probeEnabled);
  $("tl-off").hidden = on;
  for (const id of ["tl-form", "tl-today", "tl-observed-wrap", "cap-probe-note"]) $(id).hidden = !on;
  if (!on) { $("tl-note").hidden = true; $("tl-result").replaceChildren(); return; }
  const region = $("cap-region").value;
  const spotOpt = $("tl-market").querySelector("option[value=spot]");
  spotOpt.hidden = spotOpt.disabled = limits.spotProbeEnabled === false;
  if (limits.spotProbeEnabled === false) $("tl-market").value = "on-demand";
  const p = cap.data && cap.data.region === region ? cap.data.probes : null;
  const noVpc = (p && p.defaultVpc === false) || cap.noVpc.has(region);
  const day = todayParts(cap.today);
  $("tl-today").textContent = day.text;
  const note = noVpc ? t("tl.noVpc") : day.allFull ? t("tl.capAll", { reset: day.reset }) : day.userFull ? t("tl.capYou", { reset: day.reset }) : "";
  fillPoints($("tl-note"), note);
  $("tl-note").hidden = !note;
  const run = $("tl-run");
  if (!run.hasAttribute("aria-busy")) run.disabled = cap.testing || !$("tl-target").value || noVpc || day.allFull || day.userFull;
  if (note) run.setAttribute("aria-describedby", "tl-note"); else run.removeAttribute("aria-describedby");
  fillPoints($("cap-probe-note"), t("cap.probeNote", { count: limits.cooldownMinutes }));
  const last = cap.results.get(region);
  $("tl-result").replaceChildren(...(last ? [testResult(last, region)] : []));
  renderObserved(p);
}

// The result of a test launch: what happened (the zone and size EC2 picked), then what was asked and the sizes the quota left out.
function testLines(e) {
  if (e.error) return { kind: e.error.code === "quota-too-low" || e.error.code === "no-default-vpc" ? "warn" : "bad", main: e.error.message, extra: [] };
  const r = e.r;
  // An unknown outcome (EC2 did not answer clearly) has its own text and no EC2 code: an instance may have started.
  const unknown = r.result === "error" && r.errorCode === "unknown-outcome";
  const kind = r.result === "in-progress" ? "warn" : resultKind(r.result);
  const got = r.obtained && typeof r.obtained === "object" ? r.obtained : null;
  const MAIN = {
    "launched": () => (got?.az && got?.instanceType ? t("tl.launched", { type: got.instanceType, zone: got.az }) : t("result.launched")),
    "no-capacity": () => t("tl.noCapacity"), "quota": () => t("tl.quota"), "unsupported": () => t("tl.unsupported"),
    "error": () => (r.errorMessage ? t("tl.errorReason", { message: String(r.errorMessage), code: String(r.errorCode || "") })
      : r.errorCode ? t("tl.errorCode", { code: String(r.errorCode) }) : t("tl.error")), "in-progress": () => t("probe.inProgress"),
  };
  let main = unknown ? t("tl.unknownOutcome") : MAIN[r.result] ? MAIN[r.result]() : resultText(r.result);
  if (r.cached) {
    const minutes = r.ts ? Math.max(0, Math.round((Date.now() - new Date(r.ts).getTime()) / 60000)) : 0;
    main = joinSentences([main, t("tl.cached", { minutes, cooldown: limits.cooldownMinutes })]);
  }
  const sizes = Array.isArray(r.sizes) ? r.sizes.filter((x) => typeof x === "string") : [];
  const skipped = Array.isArray(r.skipped) ? r.skipped.filter((x) => x && typeof x.instanceType === "string") : [];
  const extra = [
    sizes.length > 1 ? t("tl.tried", { sizes: fmtList(sortTypes(sizes)) }) : "",
    skipped.length ? t("tl.skipped", { sizes: fmtList(skipped.map((x) => (Number.isFinite(Number(x.vcpus)) ? t("tl.sizeVcpus", { type: x.instanceType, vcpus: t("unit.vcpus", { count: Number(x.vcpus) }) }) : x.instanceType))) }) : "",
  ].filter(Boolean);
  return { kind, main, extra, code: r.result === "error" && !unknown ? r.errorCode : null };
}
function testResult(e, region) {
  const l = testLines(e);
  const when = e.r?.ts && Number.isFinite(stampMs(e.r.ts)) ? fmtRelative(e.r.ts) : "";
  const what = t("tl.resultFor", { what: targetText(e.target, region), market: marketText(e.market) });
  return h("div", { class: "tl-result" },
    h("p", { class: `feedback fb-${l.kind}`, title: l.code ? t("tl.ec2Code", { code: l.code }) : null }, h("span", { class: "fb-icon", "aria-hidden": "true" }), l.main),
    h("small", { class: "muted" }, when ? `${what} · ${when}` : what),
    ...l.extra.map((x) => h("small", { class: "muted" }, x)));
}
const testSummary = (e) => testLines(e).main;

// Every user's test launches in this Region in the last 24 hours, per score group (or type) and market, with where they landed.
function renderObserved(p) {
  const table = $("tl-observed");
  const body = table.tBodies[0];
  if (!p) {
    if (cap.loading) tableLoading(table, 5, 2);
    else { body.replaceChildren(emptyRow(5, cap.data?.errors.probes ? "" : t("common.noData"))); setReloading(table, false); }
    return;
  }
  const rows = (Array.isArray(p.observed) ? p.observed : []).filter((o) => o && (o.label || o.instanceType || o.group));
  body.replaceChildren(...rows.map((o) => {
    const total = Number(o.total) || 0;
    const landed = (Array.isArray(o.landed) ? o.landed : []).filter((l) => l && l.az && l.instanceType);
    return h("tr", {},
      h("th", { scope: "row", "data-label": t("tl.colWhat") }, o.group ? groupLabelOf(o.group, o.label) : o.label || o.instanceType),
      h("td", { "data-label": t("probe.dlgMarket") }, marketText(o.market)),
      h("td", { "data-label": t("tl.colResults"), title: t("probe.breakdown", { launched: o.launched || 0, noCapacity: o.noCapacity || 0, quota: o.quota || 0, other: o.other || 0 }) },
        t("probe.observed", { launched: o.launched || 0, count: total })),
      h("td", { "data-label": t("tl.colLanded") }, landed.length ? fmtList(landed.map((l) => t("tl.landedItem", { zone: l.az, type: l.instanceType, count: Number(l.count) || 1 })))
        : h("span", { class: "muted" }, t("tl.landedNone"))),
      h("td", { "data-label": t("tl.colLast") }, o.last?.result ? h("span", { class: `res res-${o.last.result}` }, t("probe.last", { result: resultInline(o.last.result), when: fmtRelative(o.last.ts) })) : ""));
  }));
  if (!rows.length) body.append(emptyRow(5, t("tl.noneObserved")));
  setReloading(table, cap.loading);
}

async function runTest() {
  const region = $("cap-region").value, target = $("tl-target").value, market = $("tl-market").value;
  if (!target || cap.testing) return;
  const [kind, name] = splitTarget(target);
  const spot = market === "spot";
  const ok = await confirmDialog({
    title: spot ? t("probe.dlgTitleSpot") : t("probe.dlgTitleOd"),
    facts: [[t("col.region"), regionLabel(region)], [t("tl.what"), targetText(target, region)], [t("probe.dlgMarket"), marketText(market)]],
    body: spot ? t("probe.dlgBodySpot") : t("probe.dlgBodyOd"),
    ok: t("probe.dlgOk"),
  });
  if (!ok) return;
  cap.testing = true;
  let entry;
  await busyButton($("tl-run"), "probe.testing", async () => {
    try {
      const r = await api("/api/probe", {}, { region, market, ...(kind === "group" ? { group: name } : { instanceType: name }) });
      entry = { target, market, r };
      if (r.today) cap.today = r.today;
    } catch (e) {
      entry = { target, market, error: e };
      if (e.code === "no-default-vpc") cap.noVpc.add(region);
    }
  });
  cap.testing = false;
  cap.results.set(region, entry);
  announce(testSummary(entry));
  renderCapacity();
  await loadTab("capacity", true, false);
  if ($("tl-run").isConnected && !$("app").hidden) $("tl-run").focus();
}

// --- Reserve with CLI: a command to copy. The page never creates a reservation. ---
const SAFE_NAME = /^[a-z0-9][a-z0-9.-]*$/;
// One hour from now in UTC, to the second: 2030-01-01T10:00:00Z.
const reserveEnd = () => new Date(Date.now() + 3600e3).toISOString().replace(/\.\d{3}Z$/, "Z");
function reserveCommand(region, az, type) {
  return `aws ec2 create-capacity-reservation --region ${region} --availability-zone ${az} --instance-type ${type} --instance-platform Linux/UNIX`
    + ` --instance-count 1 --instance-match-criteria targeted --end-date-type limited --end-date ${reserveEnd()}`;
}
// A small secondary button; nothing is shown for a name that is not a plain Region, zone, or type name.
function reserveButton(region, az, type) {
  if (![region, az, type].every((x) => SAFE_NAME.test(String(x)))) return null;
  const btn = h("button", { type: "button", class: "link small", "data-key": `reserve|${az}` }, t("cap.reserveCli"));
  btn.addEventListener("click", () => openReserve(region, az, type, btn));
  return btn;
}
let reserveCmd = "";
let reserveOpener = null;
function openReserve(region, az, type, opener) {
  reserveCmd = reserveCommand(region, az, type);
  reserveOpener = opener;
  $("cli-command").textContent = reserveCmd;
  $("cli-copy").textContent = t("cb.copy");
  $("cli-facts").replaceChildren(...[[t("col.instanceType"), type], [t("col.zone"), az]].flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  openDialog($("cli-dialog"));
  $("cli-copy").focus();
}
// Close button or Escape: focus goes back to the button that opened the dialog.
function closeReserve() {
  closeDialog($("cli-dialog"));
  if (reserveOpener && reserveOpener.isConnected) reserveOpener.focus();
  reserveOpener = null;
}
async function copyReserve() {
  const btn = $("cli-copy");
  try {
    await navigator.clipboard.writeText(reserveCmd);
    btn.textContent = t("cb.copied");
    announce(t("cb.copied"));
  } catch {
    // The command is always in view; select it so Ctrl+C or Cmd+C works.
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents($("cli-command"));
    sel.removeAllRanges();
    sel.addRange(range);
    btn.textContent = t("cli.copyManual");
  }
}

// ======================================================================
// 3. Capacity Blocks: multi-Region search, arrival alerts, max capacity
// ======================================================================
const CB_MAX_PAIRS = 40;     // Region and type pairs in one search
const CB_WORKERS = 6;        // concurrent /api/capacity-blocks calls (each takes seconds on the EC2 side)
// A pair that met the rate limit ("rate-limited" from api() after its own retries, or the API's
// "throttled") is tried once more after the others, following a short wait with jitter.
const CB_RETRY_CODES = new Set(["rate-limited", "throttled"]);
const CB_RETRY_DELAY_MS = 1000;
const cbRetryDelay = () => CB_RETRY_DELAY_MS * (1 + Math.random() * 0.5);
const CB_RECENT_KEY = "cbRecent";
const CB_RECENT_MAX = 5;
const cbTypeCache = typeLists({ capacityBlocks: "true" }); // the Capacity Block types EC2 offers per Region
let cbOffered = new Map();     // instance type -> Set of selected Regions that offer it
let cbTypesLoading = false;
let cbTypesFailed = [];
let cbRegionsSeq = 0;
let cbSeq = 0;                 // newest search; older answers are dropped
let cbRows = [];
let cbSearch = null;           // the last search: { pairs, skipped, done, final, failures, notes, form }
// Notes from the API when EC2 narrowed the search window. An unknown code shows the API's English text.
const CB_NOTE_TEXT = {
  "cb-window-end": (p) => t("cb.noteWindowEnd", { date: fmtDate(p.date) }),
  "cb-window-start": (p) => t("cb.noteWindowStart", { date: fmtDate(p.date) }),
};
const cbNoteText = (n) => (CB_NOTE_TEXT[n.code] && n.params?.date ? CB_NOTE_TEXT[n.code](n.params) : n.message);
// One note per kind across the searched pairs, with the earliest limit date.
function addCbNotes(r) {
  for (const n of r.notes || (r.note ? [{ message: r.note }] : [])) {
    const key = n.code || n.message;
    const have = cbSearch.notes.get(key);
    if (!have || (n.params?.date && have.params?.date && n.params.date < have.params.date)) cbSearch.notes.set(key, n);
  }
}
let cbRegionsMs = null, cbTypesMs = null;
const maxCount = () => limits.capacityBlockMaxCount || 64;

// A checkbox list behind a disclosure button, with Select all / Clear and a count summary. `text.summary(selected, total)`
// replaces the default summary of the collapsed button.
function multiSelect(id, text, onChange) {
  const root = $(id);
  const button = h("button", { type: "button", class: "ghost ms-button", id: `${id}-button`, "aria-expanded": "false",
    "aria-controls": `${id}-panel`, "aria-labelledby": `${id}-label ${id}-button` });
  const list = h("div", { class: "ms-list", role: "group", "aria-labelledby": `${id}-label` });
  const all = h("button", { type: "button", class: "link", id: `${id}-all` });
  const none = h("button", { type: "button", class: "link", id: `${id}-clear` });
  const empty = h("p", { class: "muted ms-empty" });
  const panel = h("div", { class: "ms-panel", id: `${id}-panel`, hidden: "" }, h("div", { class: "ms-tools" }, all, none), list, empty);
  root.replaceChildren(button, panel);
  let items = [];
  const boxes = () => [...list.querySelectorAll("input[type=checkbox]")];
  const self = {
    selected: () => boxes().filter((b) => b.checked).map((b) => b.value),
    options: () => items.map((i) => i.value),
    set(chosen) { for (const b of boxes()) b.checked = chosen.includes(b.value); self.refresh(); },
    setOptions(next, chosen = self.selected()) {
      items = next;
      const option = (i) => {
        const box = h("input", { type: "checkbox", value: i.value });
        box.checked = chosen.includes(i.value);
        box.addEventListener("change", () => { self.refresh(); onChange(); });
        return h("label", { class: "ms-option" }, box, h("span", { "data-value": i.value }, i.label()));
      };
      // Items with a group (the instance family) sit under a sub-heading per group, in the order given.
      const groups = [];
      for (const i of items) {
        if (!groups.length || groups[groups.length - 1].name !== (i.group || null)) groups.push({ name: i.group || null, items: [] });
        groups[groups.length - 1].items.push(i);
      }
      list.replaceChildren(...groups.flatMap((g) => (g.name === null ? g.items.map(option)
        : [h("div", { class: "ms-group", role: "group", "aria-label": g.name }, h("div", { class: "ms-group-head", "aria-hidden": "true" }, g.name), g.items.map(option))])));
      self.refresh();
    },
    // Rewrites every label in the active locale.
    refresh() {
      all.textContent = t("ms.selectAll");
      none.textContent = t("ms.clear");
      for (const span of list.querySelectorAll("span[data-value]")) span.textContent = items.find((i) => i.value === span.dataset.value).label();
      empty.textContent = items.length ? "" : text.empty();
      const sel = self.selected();
      button.textContent = !items.length ? text.none() : !sel.length ? text.choose()
        : text.summary ? text.summary(sel, items.length)
          : sel.length <= 2 ? fmtList(sel) : t("ms.selected", { count: sel.length, total: items.length });
      button.classList.toggle("ms-none", !sel.length);
    },
    close() { panel.hidden = true; button.setAttribute("aria-expanded", "false"); },
  };
  const pick = (on) => { for (const b of boxes()) b.checked = on; self.refresh(); onChange(); };
  all.addEventListener("click", () => pick(true));
  none.addEventListener("click", () => pick(false));
  button.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    button.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) boxes()[0]?.focus();
  });
  root.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !panel.hidden) { ev.preventDefault(); ev.stopPropagation(); self.close(); button.focus(); }
  });
  document.addEventListener("click", (ev) => { if (!panel.hidden && !root.contains(ev.target)) self.close(); });
  self.refresh();
  return self;
}

const cbTypes = (region) => cbTypeCache.get(region);

// Rebuild the type list from the Capacity Block types that the chosen Regions offer.
async function onCbRegionsChange() {
  const regions = cbRegionsMs.selected();
  const mine = ++cbRegionsSeq;
  cbTypesLoading = true;
  updateCbForm();
  const results = await Promise.allSettled(regions.map(cbTypes));
  if (mine !== cbRegionsSeq) return null;
  const offered = new Map();
  const failed = [];
  results.forEach((r, i) => {
    if (r.status === "rejected") { failed.push(regions[i]); return; }
    for (const x of r.value) {
      if (!offered.has(x)) offered.set(x, new Set());
      offered.get(x).add(regions[i]);
    }
  });
  cbOffered = offered;
  const total = regions.length;
  cbTypesMs.setOptions(sortTypes(offered.keys()).map((x) => ({
    value: x, group: familyOf(x), label: () => (offered.get(x).size === total ? x : t("cb.typePartial", { type: x, count: offered.get(x).size, total })) })));
  cbTypesLoading = false;
  cbTypesFailed = failed;
  renderCbAlerts();
  updateCbForm();
  return !failed.length;
}

function renderCbAlerts() {
  $("cb-alerts").replaceChildren(...(cbTypesFailed.length
    ? [errorAlert(t("cb.typesFailed", { regions: fmtList(regionLabels(cbTypesFailed)) }), () => loadTab("blocks", true))] : []));
}

function cbForm() {
  return {
    regions: cbRegionsMs.selected(), types: cbTypesMs.selected(), count: $("cb-count").value.trim(),
    days: $("cb-days").value, start: $("cb-start").value,
  };
}

// Alerts: only the pairs where the Region offers the type.
function cbPairs(form) {
  const pairs = [];
  for (const type of form.types) for (const region of form.regions) if (cbOffered.get(type)?.has(region)) pairs.push([type, region]);
  return pairs;
}

// A search sends every chosen pair except those that the Region's loaded Capacity Block list leaves out.
// A Region whose list could not be loaded is sent, and the API answers for it.
function cbSearchPairs(form) {
  const send = [];
  const skipped = [];
  for (const type of form.types) for (const region of form.regions) {
    const list = cbTypeCache.of(region);
    (list && !list.includes(type) ? skipped : send).push([type, region]);
  }
  return { send, skipped };
}

const validCount = (c) => /^\d{1,2}$/.test(c) && Number(c) >= 1 && Number(c) <= maxCount();
// The earliest start is a UTC day, up to 14 days ahead.
const START_DAYS_AHEAD = 14;
const utcDay = (ms) => new Date(ms).toISOString().slice(0, 10);
const startMax = () => utcDay(Date.now() + START_DAYS_AHEAD * 86400e3);
const validDate = (d) => d === "" || /^\d{4}-\d{2}-\d{2}$/.test(d);
const dateTooFar = (d) => d !== "" && d > startMax();

function updateCbForm() {
  if (!cbRegionsMs) return false;
  const form = cbForm();
  const pairs = cbPairs(form);
  const searchPairs = cbSearchPairs(form).send;
  let problem = "";
  if (cbTypesLoading) problem = t("cb.formLoading");
  else if (!form.regions.length) problem = t("cb.formNoRegion");
  else if (!cbOffered.size) problem = t("cb.formNoFamilies");
  else if (!form.types.length) problem = t("cb.formNoType");
  else if (!validCount(form.count)) problem = t("cb.formCount", { max: maxCount() });
  else if (!validDate(form.start)) problem = t("cb.formDate");
  else if (dateTooFar(form.start)) problem = t("cb.formDateFar", { days: START_DAYS_AHEAD });
  else if (searchPairs.length > CB_MAX_PAIRS) problem = t("cb.formTooMany", { count: searchPairs.length, max: CB_MAX_PAIRS });
  const searching = cbSearch && !cbSearch.final;
  $("cb-search").disabled = Boolean(problem) || Boolean(searching);
  $("cb-search-again").disabled = !cbSearch;
  setRefreshBusy("cb-search-again", searching);
  $("cb-start").max = startMax();
  // The alert uses the form's count and days; it does not need the search pair cap.
  $("cb-alert").disabled = cbTypesLoading || !pairs.length;
  $("cb-form-status").textContent = problem;
  return !problem;
}

const daysText = (n) => t("unit.days", { count: n });
const fmtDuration = (hours) => (hours % 24 ? t("unit.hours", { count: hours }) : daysText(hours / 24));

function cbReason(e, form) {
  switch (e.code) {
    case "cb-unsupported-type": return t("cb.reasonUnsupportedType");
    case "cb-unsupported": return t("cb.reasonUnsupported");
    case "bad-parameter": return t("cb.reasonBadParameter");
    case "cb-invalid-duration": return t("cb.reasonDuration", { days: daysText(Number(form.days)) });
    case "cb-invalid-count": return form.count ? t("cb.reasonCount", { count: Number(form.count) }) : t("cb.reasonCountAny");
    case "cb-invalid-start": return t("cb.reasonStart");
    case "cb-invalid-end": return t("cb.reasonEnd");
    case "cb-invalid-request": return t("cb.reasonInvalid", { error: e.message });
    case "rate-limited": case "throttled": return t("cb.reasonThrottled");
    default: return e.status === 429 ? t("cb.reasonThrottled") : e.message;
  }
}

function renderCbFailures() {
  const groups = new Map();
  for (const f of cbSearch?.failures || []) {
    const reason = cbReason(f.error, cbSearch.form);
    const key = `${f.type}|${reason}`;
    if (!groups.has(key)) groups.set(key, { type: f.type, reason, regions: [] });
    groups.get(key).regions.push(f.region);
  }
  // Pairs left out before sending: one line per type, with no request made.
  const skipped = new Map();
  for (const [type, region] of cbSearch?.skipped || []) {
    if (!skipped.has(type)) skipped.set(type, []);
    skipped.get(type).push(region);
  }
  $("cb-failures").replaceChildren(
    ...sortTypes(skipped.keys()).map((type) => h("li", {}, t("cb.notOfferedLine", { type, regions: fmtList(regionLabels(skipped.get(type).sort())) }))),
    ...[...groups.values()].sort((a, b) => compareTypes(a.type, b.type))
      .map((g) => h("li", {}, t("cb.failureLine", { type: g.type, reason: g.reason, regions: fmtList(regionLabels(g.regions.sort())) }))));
}

// The Capacity Reservations page of the row's Region in the EC2 console, where Create Capacity Block starts a purchase.
function buyCell(o) {
  if (!REGION_RE.test(o.region || "")) return h("span", { class: "muted" }, t("cb.buyConsole"));
  return h("a", { href: `https://${o.region}.console.aws.amazon.com/ec2/home?region=${o.region}#CapacityReservations:`,
    target: "_blank", rel: "noopener noreferrer", class: "ext" }, t("cb.buyConsole"), h("span", { class: "sr-only" }, t("common.newTab")));
}

// The upfront fee spread over the hours and the instances; null when the offering has no fee.
function perHourValue(o) {
  const fee = Number(o.UpfrontFee);
  const count = Number(o.InstanceCount ?? o.count);
  if (!o.UpfrontFee || !Number.isFinite(fee) || !o.CapacityBlockDurationHours || !count) return null;
  return fee / o.CapacityBlockDurationHours / count;
}
const perHour = (o) => { const v = perHourValue(o); return v === null ? h("span", { class: "muted" }, t("cb.feeMissing")) : fmtMoney(v, o.CurrencyCode, 4); };

function renderCbRows() {
  const body = $("cb-table").tBodies[0];
  const final = cbSearch?.final;
  cbRows.sort((a, b) => new Date(a.StartDate) - new Date(b.StartDate) || a.region.localeCompare(b.region));
  body.replaceChildren(...cbRows.map((o) => h("tr", {},
    h("td", { "data-label": t("col.region") }, regionLabel(o.region)), h("td", { "data-label": t("col.zone") }, o.AvailabilityZone),
    h("td", { "data-label": t("col.instanceType") }, o.type), h("td", { "data-label": t("cb.colInstances") }, fmtNumber(Number(o.InstanceCount ?? o.count))),
    h("td", { "data-label": t("cb.colStart") }, dateTimeNode(o.StartDate)), h("td", { "data-label": t("cb.colEnd") }, dateTimeNode(o.EndDate)),
    h("td", { "data-label": t("cb.colDuration") }, fmtDuration(o.CapacityBlockDurationHours)),
    h("td", { "data-label": t("cb.colFee") }, o.UpfrontFee ? fmtMoney(o.UpfrontFee, o.CurrencyCode) : h("span", { class: "muted" }, t("cb.feeMissing"))),
    h("td", { "data-label": t("cb.colPerHour") }, perHour(o)),
    h("td", { "data-label": t("cb.colBuy") }, buyCell(o)))));
  $("cb-buy-note").hidden = !cbRows.length;
  fillPoints($("cb-buy-note"), t("cb.buyNote"));
  $("cb-price-note").hidden = !cbRows.length;
  $("cb-price-note").textContent = t("cb.priceNote");
  if (cbRows.length) return;
  if (!final) { body.replaceChildren(...skeletonRows(10)); return; }
  const alertBtn = limits.notificationsEnabled
    ? h("button", { type: "button", class: "link", id: "cb-empty-alert", onclick: createCbAlerts }, t("cb.emptyAlert")) : null;
  body.replaceChildren(h("tr", { class: "empty-row" }, h("td", { colspan: 10, class: "empty" }, h("b", {}, t("cb.emptyTitle")), " ", t("cb.emptyHint"), " ", alertBtn)));
}

function renderCbProgress() {
  if (!cbSearch) return;
  const { pairs, done, final } = cbSearch;
  $("cb-progress").max = pairs.length;
  $("cb-progress").value = done;
  $("cb-progress-text").textContent = final && cbRows.length
    ? t("cb.progressFound", { done, total: pairs.length, count: cbRows.length }) : t("cb.progress", { done, total: pairs.length });
  $("cb-note").textContent = joinSentences([...cbSearch.notes.values()].map(cbNoteText));
}

// `rerun` runs the last search once more with the choices it was made with, whatever the form shows now.
async function runCbSearch(rerun = false) {
  rerun = rerun === true;
  if (rerun && !cbSearch) return;
  if (!rerun && !updateCbForm()) return;
  const form = rerun ? cbSearch.form : cbForm();
  if (!form.types.length || !form.regions.length) return;
  if (!rerun) saveRecent(form);
  const mine = ++cbSeq;
  // A Region whose Capacity Block list is not loaded yet is loaded first, once, so that pairs it leaves out are not sent.
  // A list that fails to load leaves its pairs in the search, and the API answers for them.
  const unloaded = [...new Set(form.regions)].filter((r) => !cbTypeCache.of(r));
  if (unloaded.length) {
    await Promise.allSettled(unloaded.map(cbTypes));
    if (mine !== cbSeq) return;
  }
  const { send: pairs, skipped } = cbSearchPairs(form);
  if (!pairs.length && !skipped.length) return;
  cbRows = [];
  cbSearch = { pairs, skipped, done: 0, final: false, failures: [], notes: new Map(), form };
  $("cb-progress-wrap").hidden = false;
  $("cb-table-wrap").hidden = false;
  updateCbForm();
  renderCbProgress();
  renderCbFailures();
  renderCbRows();
  const btn = $("cb-search");
  const queue = pairs.slice();
  const again = [];   // pairs that met the rate limit: tried once more after the rest
  const worker = async () => {
    while (queue.length && mine === cbSeq) {
      const [type, region, second] = queue.shift();
      const params = { region, instanceType: type, days: form.days, count: form.count };
      if (form.start) params.startDate = form.start;
      let settled = true;
      try {
        const r = await api("/api/capacity-blocks", params);
        if (mine !== cbSeq) return;
        addCbNotes(r);
        cbRows.push(...r.offerings.map((o) => ({ ...o, region, type, count: Number(form.count) })));
      } catch (e) {
        if (mine !== cbSeq) return;
        if (CB_RETRY_CODES.has(e.code) && !second) { again.push([type, region, true]); settled = false; }
        else cbSearch.failures.push({ type, region, error: e });
      }
      if (settled) cbSearch.done++;
      renderCbProgress();
      renderCbRows();
      renderCbFailures();
    }
  };
  const pool = () => Promise.all(Array.from({ length: Math.min(CB_WORKERS, queue.length) }, worker));
  await busyButton(btn, "cb.searching", async () => {
    await pool();
    if (mine !== cbSeq || !again.length) return;
    // One retry round only: each pair in it is marked as a second try, so it is never queued again.
    await sleep(cbRetryDelay());
    if (mine !== cbSeq) return;
    queue.push(...again.splice(0));
    await pool();
  });
  if (mine !== cbSeq) return;
  cbSearch.final = true;
  renderCbRows();
  renderCbProgress();
  updateCbForm();
}

// --- recent searches: the last five, kept in this browser only ---
function readRecent() {
  try {
    const list = JSON.parse(localStorage.getItem(CB_RECENT_KEY) || "[]");
    return Array.isArray(list) ? list.filter((e) => e && Array.isArray(e.types) && Array.isArray(e.regions)).slice(0, CB_RECENT_MAX) : [];
  } catch { return []; }
}

function writeRecent(list) {
  try { localStorage.setItem(CB_RECENT_KEY, JSON.stringify(list.slice(0, CB_RECENT_MAX))); } catch { /* storage full or blocked */ }
  renderRecent();
}

const recentKey = (e) => JSON.stringify([[...e.types].sort(), [...e.regions].sort(), String(e.count), String(e.days)]);

function saveRecent(form) {
  const entry = { types: form.types, regions: form.regions, count: Number(form.count), days: Number(form.days) };
  writeRecent([entry, ...readRecent().filter((e) => recentKey(e) !== recentKey(entry))]);
}

function recentLabel(e) {
  const sorted = sortTypes(e.types);
  const types = sorted.length === 1 ? e.types[0] : t("cb.recentTypes", { type: sorted[0], more: sorted.length - 1 });
  const regions = e.regions.length === 1 ? regionLabel(e.regions[0]) : t("cb.recentRegions", { count: e.regions.length });
  return t("cb.recentChip", { types, regions, count: Number(e.count), days: daysText(Number(e.days)) });
}

function renderRecent() {
  const list = readRecent();
  $("cb-recent-wrap").hidden = !list.length;
  $("cb-recent").replaceChildren(...list.map((e, i) => {
    const label = recentLabel(e);
    const run = h("button", { type: "button", class: "chip-run", title: t("cb.recentRun", { types: fmtList(sortTypes(e.types)), regions: fmtList(regionLabels(e.regions)) }) }, label);
    run.addEventListener("click", () => applyRecent(e));
    const remove = h("button", { type: "button", class: "chip-remove", "aria-label": t("cb.recentRemove", { label }) }, "×");
    remove.addEventListener("click", () => writeRecent(readRecent().filter((_, j) => j !== i)));
    return h("span", { class: "chip recent-chip", role: "listitem" }, run, remove);
  }));
}

async function applyRecent(e) {
  $("cb-count").value = String(e.count);
  if (BLOCK_DAYS.includes(Number(e.days))) $("cb-days").value = String(e.days);
  $("cb-start").value = "";
  cbRegionsMs.set(e.regions.filter((r) => limits.regions.includes(r)));
  saveCbRegions();
  await onCbRegionsChange();
  cbTypesMs.set(e.types);
  if (updateCbForm()) await runCbSearch();
}

// --- arrival alerts from the search form ---
async function createCbAlerts(ev) {
  const trigger = ev?.currentTarget instanceof HTMLButtonElement ? ev.currentTarget : $("cb-alert");
  const form = cbForm();
  const pairs = cbPairs(form);
  if (!pairs.length) return;
  const days = Number(form.days);
  const instances = Number(form.count);
  const inst = t("unit.instances", { count: instances });
  const ok = await confirmDialog({
    title: t("cbAlert.dlgTitle"),
    facts: [[t("cbAlert.dlgPairs"), fmtNumber(pairs.length)], [t("field.instanceCount"), fmtNumber(instances)], [t("cb.colDuration"), daysText(days)]],
    body: t("cbAlert.dlgBody", { count: pairs.length, instances: inst }),
    ok: t("cbAlert.dlgOk"),
  });
  if (!ok) return;
  setSlot("cb-alert-status", null);
  await busyButton(trigger, "cbAlert.creating", async () => {
    let current;
    try { current = await api("/api/subscriptions"); } catch (e) {
      setSlot("cb-alert-status", () => errorAlert(t("cbAlert.failed", { error: e.message }), createCbAlerts));
      return;
    }
    const max = current.max ?? limits.maxSubscriptionsPerUser;
    const have = new Set(current.subscriptions.filter((s) => s.signal === "capacity-block").map((s) => `${s.region}|${s.instanceType}|${s.days}|${s.count ?? 1}`));
    let room = max - current.subscriptions.length;
    let created = 0, existing = 0, overLimit = 0, email = null;
    const errors = [];
    for (const [type, region] of pairs) {
      if (have.has(`${region}|${type}|${days}|${instances}`)) { existing++; continue; }
      if (room <= 0) { overLimit++; continue; }
      try {
        const r = await api("/api/subscriptions", {}, { region, instanceType: type, signal: "capacity-block", days, count: instances });
        created++; room--;
        if (r.confirmationSent) email = r.email;
      } catch (e) {
        if (e.code === "alert-exists") existing++;
        else if (e.code === "alert-limit") { overLimit++; room = 0; }
        else errors.push({ type, region, error: e });
      }
    }
    delete loadedKey.alerts;
    setSlot("cb-alert-status", () => {
      const parts = [t("cbAlert.created", { count: created, instances: inst, days: daysText(days) })];
      if (existing) parts.push(t("cbAlert.existing", { count: existing }));
      if (overLimit) parts.push(t("cbAlert.overLimit", { count: overLimit, max }));
      if (errors.length) {
        parts.push(t("cbAlert.errors", { errors: errors.map((x) => t("cbAlert.pairFailed", { type: x.type, region: regionLabel(x.region), error: x.error.message }))
          .join(t("common.clauseSep")) }));
      }
      const link = h("a", { href: "#alerts", id: "cb-alert-link" }, t("cbAlert.open"));
      return [okNotice(joinSentences(parts), " ", link), email ? warnNotice(t("alerts.confirmEmail", { email })) : null];
    });
  });
  updateCbForm();
}

// --- max capacity: the largest count with an offering, and its earliest start ---
// The search runs as a few short requests: the first without lo and hi, then each with the lo and hi of
// the previous answer, until an answer has done: true (3 requests at most for counts 0 to 64; CBM_MAX_REQUESTS
// is a safety stop). The status line under the button shows how far the search has narrowed.
const CBM_MAX_REQUESTS = 4;
let cbmSeq = 0;      // newest instance type load
let cbmRun = 0;      // newest search; a newer search or leaving the tab drops older answers
let cbmRunning = false;
let cbmIdle = null;  // the status line when no search runs (the no-types note), or null
// The status line keeps its height when empty (a no-break space), so text appearing moves nothing.
const setMaxStatus = (build) => setSlot("cbm-status", () => (build && build()) || "\u00a0");

async function loadMaxTypes() {
  const region = $("cbm-region").value;
  const sel = $("cbm-type");
  const mine = ++cbmSeq;
  sel.disabled = true;
  $("cbm-find").disabled = true;
  let types = [], ok = true;
  try { types = await cbTypes(region); } catch (e) {
    ok = false;
    if (mine === cbmSeq && !cbmRunning) setSlot("cbm-result", () => errorAlert(t("cbm.typesFailed", { region: regionLabel(region), error: e.message }), loadMaxTypes));
  }
  if (mine !== cbmSeq) return null;
  fillTypeSelect(sel, types);
  sel.disabled = !types.length;
  $("cbm-find").disabled = !sel.value;
  cbmIdle = ok && !types.length ? () => t("cbm.noTypes", { region: regionLabel(region) }) : null;
  if (!cbmRunning) {
    setMaxStatus(cbmIdle);
    if (ok && $("cbm-result").querySelector(".alert-error")) setSlot("cbm-result", null);
  }
  return ok;
}

// The Find button stays enabled while a search runs (keyboard focus stays on it), marked aria-busy; a click then does nothing.
function setMaxBusy(on) {
  const btn = $("cbm-find");
  cbmRunning = on;
  btn.classList.toggle("is-busy", on);
  if (on) {
    btn.setAttribute("aria-busy", "true");
    btn.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), t("cb.searching"));
  } else {
    btn.removeAttribute("aria-busy");
    btn.textContent = t(btn.getAttribute("data-i18n"));
    setMaxStatus(cbmIdle);
  }
}

// Leaving the Max capacity tab (or the Capacity Blocks tab) stops a running search: its later answers are dropped.
function leaveMax() {
  if (!cbmRunning) return;
  cbmRun++;
  setMaxBusy(false);
  setSlot("cbm-result", () => h("p", { class: "muted" }, t("cbm.stopped")));
}

// A bracket from the API: 0 <= lo < hi <= max + 1.
const validBracket = (r) => Number.isInteger(r.lo) && Number.isInteger(r.hi) && r.lo >= 0 && r.lo < r.hi && r.hi <= maxCount() + 1;

async function findMax() {
  if (cbmRunning) return;
  const region = $("cbm-region").value, instanceType = $("cbm-type").value, days = $("cbm-days").value;
  if (!instanceType) return;
  remember(region, instanceType);
  const mine = ++cbmRun;
  const span = () => daysText(Number(days));
  const fail = (reason) => setSlot("cbm-result", () => h("div", { class: "result result-none" }, h("p", {}, t("cbm.failed", { type: instanceType, reason: reason() }))));
  setSlot("cbm-result", () => h("p", { class: "muted" }, t("cbm.checking", { max: maxCount(), type: instanceType, region: regionLabel(region) })));
  setMaxStatus(null);
  setMaxBusy(true);
  let r = null, bracket = null, retried = false;
  try {
    for (let n = 0; n < CBM_MAX_REQUESTS; ) {
      try {
        r = await api("/api/capacity-blocks/max", { region, instanceType, days, ...(bracket ? { lo: bracket.lo, hi: bracket.hi } : {}) });
      } catch (e) {
        if (mine !== cbmRun) return;
        // Throttled after api()'s own retries: one more try of the same step after a short wait, then the error.
        if (CB_RETRY_CODES.has(e.code) && !retried) {
          retried = true;
          await sleep(cbRetryDelay());
          if (mine !== cbmRun) return;
          continue;
        }
        fail(() => cbReason(e, { days, count: "" }));
        return;
      }
      if (mine !== cbmRun) return;
      n++;
      if (r.done) break;
      if (!validBracket(r)) { r = null; break; }
      bracket = { lo: r.lo, hi: r.hi };
      // The answer is from max(lo, 1) to hi - 1 instances.
      const low = Math.max(bracket.lo, 1), high = bracket.hi - 1;
      setMaxStatus(() => t("cbm.progress", { low, high }));
    }
  } finally {
    if (mine === cbmRun) setMaxBusy(false);
  }
  if (mine !== cbmRun) return;
  if (!r || !r.done) { fail(() => t("cbm.reasonIncomplete")); return; }
  if (!r.maxCount) {
    setSlot("cbm-result", () => h("div", { class: "result result-none" },
      h("p", { class: "result-head" }, t("cbm.noneTitle")),
      h("p", {}, t("cbm.noneBody", { days: span(), type: instanceType, region: regionLabel(region), max: maxCount() })),
      h("p", { class: "muted" }, t("cbm.noneHint"))));
    return;
  }
  const e = r.earliest || {};
  setSlot("cbm-result", () => {
    const dash = h("span", { class: "muted" }, t("common.noData"));
    return h("div", { class: "result" },
      h("p", { class: "result-head" }, t("cbm.upTo", { count: r.maxCount })),
      h("p", {}, t("cbm.what", { type: instanceType, region: regionLabel(region), days: span() })),
      h("dl", { class: "facts" },
        h("dt", {}, t("cbm.earliest")), h("dd", {}, e.start ? dateTimeNode(e.start) : dash),
        h("dt", {}, t("cb.colEnd")), h("dd", {}, e.end ? dateTimeNode(e.end) : dash.cloneNode(true)),
        h("dt", {}, t("col.zone")), h("dd", {}, e.zone || dash.cloneNode(true)),
        h("dt", {}, t("cb.colFee")), h("dd", {}, e.upfrontFee ? fmtMoney(e.upfrontFee, e.currencyCode) : dash.cloneNode(true))),
      h("p", { class: "muted" }, t("cbm.pointInTime")));
  });
}

// --- sub-tabs inside the Capacity Blocks tab; not tied to the address bar ---
const CB_SUBTABS = ["search", "max"];
function selectCbSubtab(id, focus = false) {
  if (id !== "max") leaveMax();
  for (const x of CB_SUBTABS) {
    const on = x === id;
    $(`cb-tab-${x}`).setAttribute("aria-selected", String(on));
    $(`cb-tab-${x}`).tabIndex = on ? 0 : -1;
    $(`cb-panel-${x}`).hidden = !on;
  }
  if (focus) $(`cb-tab-${id}`).focus();
}

function fillDays() {
  for (const id of ["cb-days", "alert-days", "cbm-days"]) {
    const keep = $(id).value;
    $(id).replaceChildren(...BLOCK_DAYS.map((d) => h("option", { value: d }, fmtNumber(d))));
    if (keep) $(id).value = keep;
  }
}

function setUpBlocks() {
  cbRegionsMs = multiSelect("cb-regions", { none: () => t("ms.noRegions"), choose: () => t("ms.chooseRegions"), empty: () => t("ms.emptyRegions"),
      // One Region shows its full label; more show a count, because the names are long.
      summary: (sel, total) => (sel.length === 1 ? regionLabel(sel[0]) : t("ms.selected", { count: sel.length, total })) },
    () => { saveCbRegions(); return onCbRegionsChange(); });
  cbTypesMs = multiSelect("cb-types", { none: () => t("ms.noTypes"), choose: () => t("ms.chooseTypes"), empty: () => t("ms.emptyTypes") }, () => updateCbForm());
  cbRegionsMs.setOptions(limits.regions.map((r) => ({ value: r, label: () => regionLabel(r) })), []);
  for (const id of ["cb-count", "cb-start"]) $(id).addEventListener("input", () => updateCbForm());
  $("cb-count").max = String(maxCount());
  $("cb-start").max = startMax();
  $("cb-days").addEventListener("change", () => updateCbForm());
  $("cb-search").addEventListener("click", runCbSearch);
  $("cb-alert").hidden = !limits.notificationsEnabled;
  $("cb-alert").addEventListener("click", createCbAlerts);
  $("cb-search-again").addEventListener("click", () => { if (cbSearch && !cbSearch.final) return; runCbSearch(true); });
  fillRegionSelect($("cbm-region"), defaultRegion());
  $("cbm-region").addEventListener("change", () => { remember($("cbm-region").value); loadMaxTypes(); });
  $("cbm-find").addEventListener("click", findMax);
  for (const x of CB_SUBTABS) $(`cb-tab-${x}`).addEventListener("click", () => selectCbSubtab(x));
  $("cb-subtabs").addEventListener("keydown", (ev) => {
    const i = CB_SUBTABS.indexOf(CB_SUBTABS.find((x) => $(`cb-tab-${x}`).getAttribute("aria-selected") === "true"));
    const next = { ArrowRight: CB_SUBTABS[(i + 1) % CB_SUBTABS.length], ArrowLeft: CB_SUBTABS[(i - 1 + CB_SUBTABS.length) % CB_SUBTABS.length],
      Home: CB_SUBTABS[0], End: CB_SUBTABS[CB_SUBTABS.length - 1] }[ev.key];
    if (!next) return;
    ev.preventDefault();
    selectCbSubtab(next, true);
  });
  renderRecent();
}

// The Regions ticked when the tab first opens: the Region chosen on Compare Regions; else the set used last time in this
// browser; else the last used Region of any tab; on a first visit none, so a search never covers Regions nobody chose.
const CB_REGIONS_KEY = "cbRegions";
function cbStartRegions(fromCompare) {
  const last = lastUsed().region;
  if (fromCompare && limits.regions.includes(last)) return [last];
  const saved = readStored(CB_REGIONS_KEY, []);
  const kept = Array.isArray(saved) ? limits.regions.filter((r) => saved.includes(r)) : [];
  if (kept.length) return kept;
  return limits.regions.includes(last) ? [last] : [];
}
const saveCbRegions = () => writeStored(CB_REGIONS_KEY, cbRegionsMs.selected());

// The first open starts from the last used Region and type. Refresh asks EC2 again for the type lists;
// a search runs only when you choose Find.
let cbStarted = false;
let cbPending = false;  // Compare Regions chose a Region and type: start from them, like a first open
async function loadBlocks(refresh = false) {
  const first = !cbStarted || cbPending;
  const pending = cbPending;
  if (refresh || !cbStarted) cbTypeCache.clear();
  if (cbPending) selectCbSubtab("search");
  cbPending = false;
  if (first) {
    const last = lastUsed();
    cbRegionsMs.set(cbStartRegions(pending));
    $("cbm-region").value = defaultRegion();
    if (last.type) $("cbm-type").replaceChildren(h("option", { value: last.type }, last.type));
  }
  const [regionsOk, maxOk] = await Promise.all([onCbRegionsChange(), loadMaxTypes()]);
  if (regionsOk === null || maxOk === null) return null;
  const itype = lastUsed().type;
  if (first && cbTypesMs.options().includes(itype)) { cbTypesMs.set([itype]); updateCbForm(); }
  // A failed load does not count as loaded, so the next open tries again.
  if (regionsOk && maxOk) cbStarted = true;
  return regionsOk && maxOk;
}

// From Compare Regions: the Capacity Blocks tab with this Region and type chosen, saved as the last used choice.
function openBlocks(region, type) {
  remember(region, type);
  cbPending = true;
  delete loadedKey.blocks;
  return selectTab("blocks");
}

function renderBlocks() {
  if (!cbRegionsMs) return;
  cbRegionsMs.refresh();
  cbTypesMs.refresh();
  renderRecent();
  renderCbAlerts();
  updateCbForm();
  if (cbSearch) { renderCbRows(); renderCbProgress(); renderCbFailures(); }
}

// ======================================================================
// 4. Quotas: limit, usage from CloudWatch, one Region or all
// ======================================================================
const QUOTA_GROUPS = [
  ["on-demand", "q.groupOd", "q.groupOdIntro"],
  ["spot", "q.groupSpot", "q.groupSpotIntro"],
  ["capacity-block", "q.groupCb", "q.groupCbIntro"],
];
const LIMIT_ONLY = "capacity-block";
const QUOTA_CODE_RE = /^L-[0-9A-F]{1,16}$/i;
const REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d$/;
const quotas = { single: null, err: null, loading: false, stamps: [], blocked: 0 };
let quotaSeq = 0;

function quotaLink(region, code) {
  if (!QUOTA_CODE_RE.test(code || "") || !REGION_RE.test(region || "")) return "";
  return h("a", { href: `https://${region}.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/${code}`,
    target: "_blank", rel: "noopener noreferrer", class: "ext" }, t("q.viewLink"), h("span", { class: "sr-only" }, t("common.newTab")));
}

function limitCell(q) {
  return h("td", { class: "num", "data-label": t("q.colLimit") },
    q.value === null || q.value === undefined ? t("common.noData") : quotaAmount(q.value, q), q.applied ? null : h("small", { class: "tag" }, t("q.awsDefault")));
}

function usageCell(q, usageError) {
  if (q.used === null || q.used === undefined) {
    return h("td", { class: "muted", "data-label": t("q.colUsed"), title: usageError ? t("q.usageMissing") : t("q.noMetric") }, "-");
  }
  const pct = q.percent ?? 0;
  const level = usageLevel(q.percent);
  const bar = h("progress", { class: `qbar ${level}`.trim(), max: 100, value: Math.min(100, pct), "aria-label": t("q.barLabel", { name: q.name, percent: fmtPercent(pct) }) });
  const word = usageWord(pct);
  return h("td", { class: "usage", "data-label": t("q.colUsed") }, h("span", { class: "used" }, quotaAmount(q.used, q)), bar,
    h("span", { class: `pct ${level}`.trim() }, fmtPercent(pct)), word ? h("span", { class: `level ${level}` }, word) : null);
}

function renderQuotaTable(r) {
  const box = $("q-table");
  const type = lastUsed().type;
  const groups = [];
  for (const [cat, labelKey, introKey] of QUOTA_GROUPS) {
    let rows = r.quotas.filter((q) => q.category === cat);
    if (!rows.length) continue;
    const match = familyQuotas(rows, cat, type);
    if (match.length) rows = [...match, ...rows.filter((q) => !match.includes(q))];
    const limitOnly = cat === LIMIT_ONLY;
    const head = h("tr", {}, h("th", { class: "c-name" }, t("q.colQuota")), h("th", { class: "c-limit" }, t("q.colLimit")),
      limitOnly ? null : h("th", { class: "c-used" }, t("q.colUsed")), h("th", { class: "c-link" }, h("span", { class: "sr-only" }, t("q.colLink"))));
    const body = rows.map((q, i) => { const [name, tip] = quotaName(q, `q-row-info-${cat}-${i}`); return h("tr", { class: match.includes(q) ? "hl" : null },
      h("td", { "data-label": t("q.colQuota") }, h("div", { class: "q-name" }, h("span", {}, name,
        limitOnly ? h("span", { class: "tag" }, t("q.limitOnly")) : null,
        q.adjustable === false ? h("small", {}, t("q.notAdjustable")) : null), tip)),
      limitCell(q),
      limitOnly ? null : usageCell(q, r.usageError),
      h("td", { class: "c-link" }, quotaLink(r.region, q.code))); });
    groups.push(h("div", { class: `quota-group qg-${cat}` }, h("h3", {}, t(labelKey)), noteEl(t(introKey)),
      h("table", { class: `quota cards-sm${limitOnly ? " limit-only" : ""}` }, h("thead", {}, head), h("tbody", {}, body))));
  }
  // A popover in the old rows goes with them.
  if (infoOpen && box.contains(infoOpen.opener)) closeInfo();
  clearTimeout(hoverTimer);
  box.replaceChildren(...groups);
  if (!r.quotas.length) box.append(h("p", { class: "muted" }, t("q.none")));
}

function renderQuotaNotices() {
  const notes = [];
  if (quotas.single) {
    if (quotas.single.usageError) notes.push(t("q.usageMissingOne", { region: regionLabel(quotas.single.region) }));
    const over = quotas.single.quotas.filter((q) => (q.percent ?? 0) >= 100);
    if (over.length) notes.push(t("q.overNote", { count: over.length }));
  }
  $("q-notice").replaceChildren(...notes.map((n) => h("span", { class: "notice-line" }, n)));
  $("q-notice").hidden = !notes.length;
}

async function loadQuotas(refresh = false) {
  const mine = ++quotaSeq;
  const region = $("q-region").value;
  quotas.loading = true;
  quotas.err = null;
  renderQuotas();
  try {
    const fresh = refresh ? { refresh: "true" } : {};
    // The answer carries its fetchedAt and, after Refresh, whether AWS was asked.
    const answer = await api("/api/quotas", { region, ...fresh });
    if (mine !== quotaSeq) return null;
    quotas.single = answer;
    quotas.stamps = [stampOf(answer, refresh)].filter(Boolean);
    quotas.blocked = refresh ? blockedSeconds([answer]) : 0;
  } catch (e) { if (mine === quotaSeq) quotas.err = e; }
  if (mine !== quotaSeq) return null;
  quotas.loading = false;
  renderQuotas();
  return !quotas.err;
}

function renderQuotas() {
  if (!limits) return;
  fillPoints($("q-lag-note"), t("q.lagNote", { minutes: MINUTES.quotas() }));
  const data = quotaData();
  $("q-alerts").replaceChildren(...(quotas.err ? [errorAlert(t("q.loadFailed", { error: quotas.err.message }), () => loadTab("quotas", true, false))] : []));
  const box = $("q-table");
  if (quotas.loading && !box.querySelector("table:not(.skel-table)")) {
    box.replaceChildren(h("table", { class: "skel-table", "aria-hidden": "true" }, h("tbody", {}, ...skeletonRows(4, 9))));
    box.setAttribute("aria-busy", "true");
  } else if (quotas.loading) setReloading(box, true);
  else if (data) { renderQuotaTable(data); setReloading(box, false); }
  else { box.replaceChildren(); setReloading(box, false); }
  setRefreshNote("q-refresh-note", quotas.err ? 0 : quotas.blocked);
  setRefreshBusy("q-refresh", quotas.loading);
  renderFresh();
  renderQuotaNotices();
}

// ======================================================================
// 5. Alerts: email alerts for Capacity Block offerings and Spot scores
// ======================================================================
const alertsState = { data: null, err: null, loading: false, offered: [], cbTypes: null, typesErr: null, scoresAsked: false };
const signalText = (s) => (s.signal === "capacity-block"
  ? t("alerts.signalCbText", { instances: t("unit.instances", { count: Number(s.count) || 1 }), days: daysText(s.days) })
  : t("alerts.signalSpotText", { threshold: s.threshold }));
// A Spot score alert names its score group (the type it is stored with in the title); a Capacity Block alert its type.
const alertTarget = (s) => (s.signal === "spot-score" ? (groupOfType(s.instanceType) ? groupName(groupOfType(s.instanceType)) : s.instanceType) : s.instanceType);
// The state EC2 checks report. "rejected" carries a reason code; an unknown reason shows the raw code.
const ALERT_STATE = { "available": () => t("alerts.stateAvailable"), "not-available": () => t("alerts.stateUnavailable"), "waiting": () => t("alerts.statePending") };
const ALERT_REASON = {
  "cb-invalid-duration": () => t("alerts.reasonDuration"), "cb-unsupported": () => t("alerts.reasonUnsupported"), "cb-invalid-count": () => t("alerts.reasonCount"),
  "cb-unsupported-type": () => t("alerts.reasonUnsupportedType"), "cb-invalid-request": () => t("alerts.reasonInvalid"),
};
const alertStateText = (s) => (s.state === "rejected" ? t("alerts.stateRejected", { reason: ALERT_REASON[s.reason] ? ALERT_REASON[s.reason]() : String(s.reason ?? "") })
  : ALERT_STATE[s.state] ? ALERT_STATE[s.state]() : String(s.state ?? ""));
let alertsSeq = 0, alertTypesSeq = 0;

// The list follows the signal: Capacity Block alerts offer the Capacity Block types of the Region (the configured families
// when that list could not be read); Spot score alerts offer every score group the Region sells (the watchlist does not
// apply), since every size of a group shares one score. A group's alert is stored with one size per group and Region: the
// first size of the whole group (natural order) that the Region sells, so the same alert is always the same item.
const spotSignal = () => $("alert-signal").value === "spot-score";
function alertGroups() {
  const sold = new Set(alertsState.offered);
  return knownGroups().map((g) => [g, g.sizes.find((x) => sold.has(x))]).filter(([, size]) => size);
}
// A short hint when the chosen group has no Spot score in the Region yet (the Spot score tab knows its Region state).
function alertScoreHint() {
  if (!spotSignal()) return "";
  const key = $("alert-type").selectedOptions[0]?.getAttribute("data-group");
  const region = $("alert-region").value;
  if (!key || !groupOf(key) || keyLoading(key)) return "";
  return regionState(key, region) && scoreAt(key, region) === null ? t("alerts.noScoreYet") : "";
}
// The hint needs the scores: when the Spot score tab has not read them, read them once (from the cache, no new configuration).
function loadAlertScores() {
  if (!spotSignal() || cmp.sg.loaded || cmp.sg.full || alertsState.scoresAsked) return;
  alertsState.scoresAsked = true;
  loadScores().then(() => { if (spotSignal()) fillAlertTypes(); });
}
function setAlertTypeNote(count) {
  $("alert-type-note").textContent = !alertsState.typesErr && alertsState.offered.length && !count ? t("alerts.noTypes") : alertScoreHint();
}
function fillAlertTypes() {
  const sel = $("alert-type");
  $("alert-type-name").textContent = spotSignal() ? t("alerts.scoreGroup") : t("field.instanceType");
  let count;
  if (spotSignal()) {
    const groups = alertGroups();
    const keep = sel.value;
    const last = groupOfType(lastUsed().type);
    sel.replaceChildren(...groups.map(([g, size]) => h("option", { value: size, "data-group": g.key }, groupName(g))));
    const values = groups.map(([, size]) => size);
    const lastValue = last ? groups.find(([g]) => g.key === last.key)?.[1] : null;
    sel.value = values.includes(keep) ? keep : lastValue || values[0] || "";
    count = groups.length;
  } else {
    const types = shownTypes(alertsState.offered).filter((x) => cbOffers(alertsState.cbTypes, x));
    fillTypeSelect(sel, types);
    count = types.length;
  }
  $("alert-type").disabled = !count;
  $("alert-add").disabled = !$("alert-type").value;
  setAlertTypeNote(count);
}

async function loadAlertTypes() {
  const region = $("alert-region").value;
  const mine = ++alertTypesSeq;
  // Nothing from the previous Region stays selectable while this Region's lists load.
  alertsState.offered = []; alertsState.cbTypes = null; alertsState.typesErr = null;
  fillAlertTypes();
  const blocks = cbTypes(region).catch(() => null);
  let offered = [], typesErr = null;
  try { offered = await regionTypes(region); } catch (e) { typesErr = e; }
  const cb = await blocks;
  // A slower answer for an earlier Region never overwrites the lists of the Region chosen since.
  if (mine !== alertTypesSeq) return null;
  alertsState.offered = offered; alertsState.typesErr = typesErr; alertsState.cbTypes = cb;
  fillAlertTypes();
  if (alertsState.typesErr) setSlot("alert-status", () => errorAlert(t("alerts.typesFailed", { error: alertsState.typesErr.message }), loadAlertTypes));
  return !alertsState.typesErr;
}

async function loadAlerts() {
  if (!limits.notificationsEnabled) return true;
  const mine = ++alertsSeq;
  alertsState.loading = true;
  renderAlerts();
  let data = null, err = null;
  try { data = await api("/api/subscriptions"); } catch (e) { err = e; }
  if (mine !== alertsSeq) return null;
  if (err) alertsState.err = err; else { alertsState.data = data; alertsState.err = null; }
  alertsState.loading = false;
  renderAlerts();
  return !alertsState.err;
}

function renderAlerts() {
  if (!limits?.notificationsEnabled) return;
  const table = $("alerts-table");
  const every = limits.notifyIntervalMinutes ?? alertsState.data?.intervalMinutes ?? 15;
  const r = alertsState.data;
  fillPoints($("alerts-note"), [t("alerts.interval", { count: every }),
    r ? t("alerts.count", { count: r.subscriptions.length, max: r.max }) : "",
    r?.email ? t("alerts.email", { email: r.email }) : "", t("alerts.confirmOnce")].filter(Boolean).join("\n"));
  $("alerts-alerts").replaceChildren(...(alertsState.err ? [errorAlert(t("alerts.loadFailed", { error: alertsState.err.message }), () => loadTab("alerts", true))] : []));
  setRefreshBusy("alerts-refresh", alertsState.loading);
  $("alert-count").max = String(maxCount());
  renderFresh();
  if (alertsState.loading) { tableLoading(table, 6); return; }
  if (!r) { table.tBodies[0].replaceChildren(); setReloading(table, false); return; }
  table.tBodies[0].replaceChildren(...r.subscriptions.map((s) => {
    const btn = h("button", { type: "button", class: "ghost small" }, t("alerts.delete"));
    btn.addEventListener("click", () => deleteAlert(btn, s));
    const available = s.state === "available";
    const rejected = s.state === "rejected";
    return h("tr", {}, h("td", { "data-label": t("col.region") }, regionLabel(s.region)),
      h("td", { "data-label": t("col.instanceType"), title: alertTarget(s) !== s.instanceType ? s.instanceType : null }, alertTarget(s)),
      h("td", { "data-label": t("alerts.colSignal") }, signalText(s)),
      h("td", { "data-label": t("alerts.colState"), class: available ? "s-hi" : rejected ? "s-lo" : null },
        available ? h("span", { class: "fb-icon fb-ok-icon", "aria-hidden": "true" }) : null, alertStateText(s)),
      h("td", { "data-label": t("alerts.colChecked") }, s.lastCheckedAt ? h("span", { title: fmtDate(s.lastCheckedAt) }, fmtRelative(s.lastCheckedAt)) : h("span", { class: "muted" }, t("alerts.notChecked"))),
      h("td", { class: "c-action" }, btn));
  }));
  if (!r.subscriptions.length) table.tBodies[0].append(emptyRow(6, t("alerts.none")));
  setReloading(table, false);
}

function onSignalChange() {
  const block = $("alert-signal").value === "capacity-block";
  $("alert-days-label").hidden = !block;
  $("alert-count-label").hidden = !block;
  $("alert-threshold-label").hidden = block;
  fillAlertTypes();
  loadAlertScores();
}

const validAlertCount = (c) => /^\d{1,2}$/.test(c) && Number(c) >= 1 && Number(c) <= maxCount();

async function addAlert() {
  const region = $("alert-region").value, type = $("alert-type").value;
  if (!type) { setSlot("alert-status", () => errorAlert(t("alerts.pickType"))); return; }
  const signal = $("alert-signal").value;
  const body = { region, instanceType: type, signal };
  if (signal === "capacity-block") {
    const count = $("alert-count").value.trim();
    if (!validAlertCount(count)) { setSlot("alert-status", () => errorAlert(t("cb.formCount", { max: maxCount() }))); return; }
    body.days = Number($("alert-days").value);
    body.count = Number(count);
  } else body.threshold = Number($("alert-threshold").value);
  remember(region, type);
  setSlot("alert-status", null);
  await busyButton($("alert-add"), "alerts.adding", async () => {
    try {
      const r = await api("/api/subscriptions", {}, body);
      setSlot("alert-status", () => (r.confirmationSent ? warnNotice(t("alerts.confirmEmail", { email: r.email }))
        : okNotice(t("alerts.added", { signal: signalText(r.subscription), type: alertTarget({ signal, instanceType: type }), region: regionLabel(region) }))));
      await loadAlerts();
    } catch (e) { setSlot("alert-status", () => errorAlert(t("alerts.addFailed", { error: e.message }))); }
  });
}

async function deleteAlert(btn, s) {
  const ok = await confirmDialog({
    title: t("alerts.deleteTitle"),
    facts: [[t("col.region"), regionLabel(s.region)], [t("col.instanceType"), alertTarget(s)], [t("alerts.colSignal"), signalText(s)]],
    body: "",
    ok: t("alerts.deleteOk"),
  });
  if (!ok) return;
  await busyButton(btn, "alerts.deleting", async () => {
    try {
      await api("/api/subscriptions", { id: s.id }, null, "DELETE");
      setSlot("alert-status", () => okNotice(t("alerts.deleted")));
    } catch (e) { setSlot("alert-status", () => errorAlert(t("alerts.deleteFailed", { error: e.message }))); }
  });
  await loadAlerts();
}

async function loadAlertsTab() {
  const results = await Promise.all([
    loadAlerts(),
    !$("alert-type").options.length || typeCache.stale() ? loadAlertTypes() : true,
  ]);
  if (results.includes(null)) return null;
  return results.every(Boolean);
}

function renderAlertsTab() { renderAlerts(); fillAlertTypes(); }

// ======================================================================
// 6. Users: a guide to managing users in the Amazon Cognito console. No API call, no user data.
// ======================================================================
const COGNITO_USERS_URL = "https://{region}.console.aws.amazon.com/cognito/v2/idp/user-pools/{userPoolId}/users?region={region}";
const USER_POOL_ID_RE = /^[a-z]{2}(-[a-z]+)+-\d_[A-Za-z0-9]+$/;
// The Users page of this deployment's user pool, or "" when config.json does not name a valid pool and Region.
function cognitoUsersUrl(c) {
  const region = c && c.region, pool = c && c.userPoolId;
  if (!REGION_RE.test(region || "") || !USER_POOL_ID_RE.test(pool || "")) return "";
  return COGNITO_USERS_URL.replace(/\{region\}/g, region).replace("{userPoolId}", encodeURIComponent(pool));
}
function setUpUsers() {
  const url = cognitoUsersUrl(cfg);
  if (url) $("users-console").setAttribute("href", url);
  else $("users-console").removeAttribute("href");
  $("users-console-wrap").hidden = !url;
}

// ======================================================================
// Tabs: each loads on first open, and again when its own choices change or its data gets old
// ======================================================================
const quotaData = () => (quotas.single && quotas.single.region === $("q-region").value ? quotas.single : null);
const quotasStale = () => Boolean(quotaData()) && olderThan(quotas.stamps, MINUTES.quotas());
const blocksStale = () => cbTypeCache.stale();
const alertsStale = () => typeCache.stale();
const TABS = [
  { id: "compare", key: () => "all", load: loadCompare, render: renderCompare, stale: compareStale },
  { id: "capacity", key: () => `${$("cap-region").value}|${$("cap-type").value}`, load: loadCapacity, render: renderCapacity, stale: capStale },
  { id: "blocks", key: () => "all", load: loadBlocks, render: renderBlocks, stale: blocksStale },
  { id: "quotas", key: () => $("q-region").value, load: loadQuotas, render: renderQuotas, stale: quotasStale },
  { id: "alerts", key: () => "all", load: loadAlertsTab, render: renderAlertsTab, stale: alertsStale, enabled: () => limits.notificationsEnabled },
  // The guide is static text; opening it loads nothing.
  { id: "users", key: () => "all", load: async () => true, render: () => {} },
];
// Earlier tab names, so old bookmarks still open the right tab.
const TAB_ALIASES = { offerings: "compare", reservations: "alerts", holds: "alerts", "spot-score": "compare" };
// The Spot score tab keeps the element id "compare"; its URL hash is #spot-score (old #compare links still open it).
const tabHash = (id) => (id === "compare" ? "spot-score" : id);
const loadedKey = {};   // tab id -> key of the data on screen
const loadingKey = {};  // tab id -> key of the load in flight
let activeTab = "compare";
const tabOn = (x) => !x.enabled || x.enabled();
const shownTabs = () => TABS.filter(tabOn);

// `force` reloads even when the data on screen matches; `refresh` also asks the server to skip its caches.
async function loadTab(id, force = false, refresh = force) {
  const tab = TABS.find((x) => x.id === id);
  if (!tab || !tabOn(tab)) return;
  const key = tab.key();
  if (!force && (loadedKey[id] === key || loadingKey[id] === key)) return;
  loadingKey[id] = key;
  let ok = false;
  try { ok = await tab.load(refresh); } catch (e) { ok = false; }
  if (loadingKey[id] === key) delete loadingKey[id];
  // A failed load does not count as loaded, so opening the tab again retries.
  if (ok === true) loadedKey[id] = tab.key();
  else if (ok === false) delete loadedKey[id];
}

// Opening a tab, or coming back to the page: data that is already on screen but older than its cache time is read
// again, without refresh=true (the server answers from its cache or asks AWS by its own rules).
function openTab(id) {
  const tab = TABS.find((x) => x.id === id);
  if (tab && tabOn(tab) && loadedKey[id] === tab.key() && !loadingKey[id] && tab.stale && tab.stale()) return loadTab(id, true, false);
  return loadTab(id);
}

function selectTab(id, focus = false) {
  id = TAB_ALIASES[id] || id;
  if (!shownTabs().some((x) => x.id === id)) id = "compare";
  activeTab = id;
  if (id !== "blocks") leaveMax();
  for (const x of TABS) {
    const on = x.id === id;
    const btn = $(`tab-${x.id}`);
    btn.setAttribute("aria-selected", String(on));
    btn.tabIndex = on ? 0 : -1;
    $(`panel-${x.id}`).hidden = !on;
  }
  closeInfo();
  revealTab($(`tab-${id}`));
  if (focus) $(`tab-${id}`).focus();
  if (location.hash !== `#${tabHash(id)}`) history.replaceState(null, "", `${location.pathname}${location.search}#${tabHash(id)}`);
  renderFresh();
  return openTab(id);
}

// A narrow screen scrolls the tab bar sideways: bring the selected tab into view (the page itself does not move).
function revealTab(btn) {
  const bar = $("tablist");
  if (bar.scrollWidth <= bar.clientWidth) return;
  const b = bar.getBoundingClientRect(), r = btn.getBoundingClientRect();
  if (r.left < b.left) bar.scrollLeft -= b.left - r.left;
  else if (r.right > b.right) bar.scrollLeft += r.right - b.right;
}

function onTabKey(ev) {
  const ids = shownTabs().map((x) => x.id);
  const i = ids.indexOf(activeTab);
  const next = { ArrowRight: ids[(i + 1) % ids.length], ArrowLeft: ids[(i - 1 + ids.length) % ids.length], Home: ids[0], End: ids[ids.length - 1] }[ev.key];
  if (!next) return;
  ev.preventDefault();
  selectTab(next, true);
}

// --- freshness labels: one per data block, one timer for all of them ---------
// Each label shows "Updating..." while its block loads, else the text worked out from the stamps of its answers.
const FRESH = [
  { id: "cmp-fresh", busy: () => scoresLoading(), stamps: () => shownKeys().map((k) => cmp.sg.at[k]) },
  { id: "cap-fresh", busy: () => cap.loading, stamps: () => cap.data?.stamps },
  { id: "cap-od-fresh", busy: () => cap.loading, stamps: () => [cap.data?.odStamp] },
  { id: "q-fresh", busy: () => quotas.loading, stamps: () => (quotaData() ? quotas.stamps : []) },
];
function renderFresh() {
  if (!limits) return;
  for (const b of FRESH) {
    const el = $(b.id);
    const info = b.busy() ? { text: t("fresh.updating"), title: "" } : freshInfo(b.stamps());
    el.textContent = info.text;
    if (info.title) el.setAttribute("title", info.title); else el.removeAttribute("title");
  }
}

let freshTimer = null;
function scheduleFresh() {
  clearTimeout(freshTimer);
  freshTimer = null;
  if (!document.hidden && appSetUp && !$("app").hidden) freshTimer = setTimeout(tickFresh, FRESH_EVERY_MS);
}
function tickFresh() {
  freshTimer = null;
  renderFresh();
  // Score groups being built: read them again once a minute while the Spot score tab is open.
  if (activeTab === "compare" && scoresBuilding() && !scoresLoading() && !$("app").hidden) loadTab("compare", true, false);
  scheduleFresh();
}
// Hidden page: no timer. Visible again: labels are worked out again and old data on the open tab loads again.
function onVisibilityChange() {
  if (document.hidden) { clearTimeout(freshTimer); freshTimer = null; return; }
  if (!appSetUp || $("app").hidden) return;
  renderFresh();
  scheduleFresh();
  openTab(activeTab);
}

// --- the info button next to a tab's first label: a small popover, closed by Escape or a click elsewhere ---
const INFO_TABS = ["cmp", "cap", "cb", "q", "alerts", "spot"];
// One popover is open at a time: { group, opener, pinned }. A group is the popover with every button that opens it.
// Hover or focus shows a preview; a click pins it (it stays until a second click, Escape, or a click elsewhere).
let infoOpen = null;
let hoverTimer = null;
let refocusing = false;
const HOVER_OPEN_MS = 150, HOVER_CLOSE_MS = 250;
// Each tab's button shows only what is true for that tab, with the times from /api/config.
const INFO_KEY = { cmp: "fresh.infoSpot", cap: "fresh.infoCapacity", cb: "fresh.infoBlocks", q: "fresh.infoQuotas", alerts: "fresh.infoAlerts" };
function infoText(p) {
  if (p === "spot") return t("price.spotInfo");
  const hours = (m) => Math.round(m / 60);
  const values = {
    cmp: { scoreMinutes: Number(limits?.warmMinutes) || 15 },
    cap: { odHours: hours(MINUTES.odPrice()), spotMinutes: MINUTES.spotPrice(), offeringHours: hours(MINUTES.offerings()), scoreMinutes: MINUTES.scores() },
    q: { quotaMinutes: MINUTES.quotas() },
    alerts: { minutes: limits?.notifyIntervalMinutes ?? alertsState.data?.intervalMinutes ?? 15 },
  }[p] || {};
  return t(INFO_KEY[p] || INFO_KEY.cmp, values);
}
function renderInfo() {
  for (const p of INFO_TABS) fillPoints($(`${p}-info-pop`), infoText(p));
}
function closeInfo(refocus = false) {
  if (!infoOpen) return;
  const { group, opener, pinned } = infoOpen;
  infoOpen = null;
  clearTimeout(hoverTimer);
  group.pop.hidden = true;
  for (const b of group.btns) b.setAttribute("aria-expanded", "false");
  if (refocus && pinned && opener.isConnected) {
    refocusing = true;  // moving the focus back is not a request for a preview
    opener.focus();
    refocusing = false;
  }
}
function openInfo(group, opener, pinned) {
  if (infoOpen?.group === group) {
    if (pinned) infoOpen.pinned = true;
    return;
  }
  closeInfo();
  group.fill();
  group.pop.hidden = false;
  for (const b of group.btns) b.setAttribute("aria-expanded", "true");
  infoOpen = { group, opener, pinned };
}
// A click on a preview pins it; a click on a pinned popover closes it.
function toggleInfo(group, btn) {
  if (infoOpen?.group === group && infoOpen.pinned) closeInfo();
  else openInfo(group, btn, true);
}
function hoverIn(group, btn) {
  clearTimeout(hoverTimer);
  if (infoOpen?.group === group || infoOpen?.pinned) return;
  hoverTimer = setTimeout(() => { if (btn.isConnected && !infoOpen?.pinned) openInfo(group, btn, false); }, HOVER_OPEN_MS);
}
function hoverOut(group) {
  clearTimeout(hoverTimer);
  if (infoOpen?.group !== group || infoOpen.pinned) return;
  hoverTimer = setTimeout(() => { if (infoOpen?.group === group && !infoOpen.pinned) closeInfo(); }, HOVER_CLOSE_MS);
}
// `preview`: hover and focus show the popover without pinning it (the tab buttons only toggle on click).
function wireInfo(btn, group, preview) {
  btn.addEventListener("click", () => toggleInfo(group, btn));
  if (!preview) return;
  btn.addEventListener("mouseenter", () => hoverIn(group, btn));
  btn.addEventListener("mouseleave", () => hoverOut(group));
  btn.addEventListener("focus", () => { if (!refocusing) openInfo(group, btn, false); });
  btn.addEventListener("blur", () => { if (infoOpen?.group === group && !infoOpen.pinned) closeInfo(); });
}
function setUpInfo() {
  for (const p of INFO_TABS) {
    const btn = $(`${p}-info`), pop = $(`${p}-info-pop`);
    wireInfo(btn, { pop, btns: [btn], fill: () => fillPoints(pop, infoText(p)) }, false);
  }
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && infoOpen) { ev.preventDefault(); closeInfo(true); } });
  document.addEventListener("click", (ev) => {
    if (infoOpen && !infoOpen.group.btns.some((b) => b.contains(ev.target)) && !infoOpen.group.pop.contains(ev.target)) closeInfo();
  });
}

// --- boot ------------------------------------------------------------------
// A failed sign-in leaves the page signed out with the reason; the address no longer carries the one-time code.
let signInError = null;
function renderSignInError() {
  $("signin-error").replaceChildren(...(signInError ? [errorAlert(signInError.message)] : []));
}
function showSignedOut(err = null) {
  signInError = err;
  $("app").hidden = true;
  $("boot-error-wrap").hidden = true;
  $("signed-out").hidden = false;
  clearTimeout(freshTimer);
  freshTimer = null;
  $("auth").replaceChildren(h("button", { type: "button", onclick: signIn }, t("auth.signIn")));
  renderSignInError();
}

let bootError = null;
function renderBootError() {
  if (!bootError) return;
  $("boot-error-text").textContent = t("boot.error", { error: bootError.message || String(bootError) });
  $("boot-retry").textContent = t("boot.retry");
}
function showBootError(err) {
  bootError = err;
  $("app").hidden = true;
  renderBootError();
  $("boot-error-wrap").hidden = false;
}

// Text outside the tabs: the header, the tab names, and the day lists.
function renderChrome() {
  applyStatic();
  if (!$("boot-error-wrap").hidden) renderBootError();
  renderSignInError();
  if (!limits) return;
  $("auth").querySelector("button")?.replaceChildren(t("auth.signOut"));
  fillDays();
  renderInfo();
}

// After a language change: every view re-renders from the data it already has.
function renderAll() {
  renderChrome();
  if (!limits) return;
  for (const tab of TABS) if (tabOn(tab)) tab.render();
  renderSlots();
  renderFresh();
}

function setUpLanguage() {
  for (const opt of $("lang").options) opt.hidden = !LOCALES.includes(opt.value);
  $("lang").value = locale;
  $("lang").addEventListener("change", async () => {
    const code = $("lang").value;
    if (!LOCALES.includes(code)) return;
    try { localStorage.setItem("lang", code); } catch { /* storage blocked */ }
    const url = new URL(location.href);
    if (url.searchParams.has("lang")) { url.searchParams.delete("lang"); history.replaceState(null, "", url.pathname + url.search + url.hash); }
    try { await setLocale(code); } catch (e) { return; }
    if (!$("signed-out").hidden) $("auth").querySelector("button")?.replaceChildren(t("auth.signIn"));
    renderAll();
    renderSignInError();
  });
}

let appSetUp = false;
function setUpApp() {
  $("auth").replaceChildren(h("button", { type: "button", class: "ghost", onclick: signOut }, t("auth.signOut")));
  fillDays();
  setUpInfo();
  // Spot score: one Refresh repeats the shown groups' Spot score requests; the offerings follow their own cache time.
  setUpFamilies();
  $("cmp-view-table").addEventListener("click", () => { cmp.view = "table"; renderCompare(); });
  $("cmp-view-map").addEventListener("click", () => { cmp.view = "map"; renderCompare(); });
  $("cmp-refresh").addEventListener("click", () => { if (cmp.loading || scoresLoading()) return; loadTab("compare", true, true); });
  $("cmp-table-wrap").addEventListener("scroll", updateHeatMore);
  window.addEventListener("resize", updateHeatMore);
  setUpMapZoom();
  $("cmp-empty-all").addEventListener("click", () => {
    cmpFamMs.set(cmpFamMs.options());
    onFamiliesChange();
    $("cmp-families-button").focus();
  });
  $("cmp-detail").addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    ev.preventDefault();
    if (cmp.chip) toggleChip(null); else selectRegion(null);
  });
  // On-Demand and Spot
  fillRegionSelect($("cap-region"), defaultRegion());
  $("cap-region").addEventListener("change", () => { remember($("cap-region").value); loadTab("capacity"); });
  $("cap-type").addEventListener("change", () => { remember(null, $("cap-type").value); loadTab("capacity"); });
  $("cap-refresh").addEventListener("click", () => { if (cap.loading) return; loadTab("capacity", true); });
  $("cap-edit-list").addEventListener("click", () => openListEditor(cap.offered, () => { fillCapTypes(); loadTab("capacity"); }));
  $("tl-target").addEventListener("change", () => renderTestPanel());
  $("tl-market").addEventListener("change", () => renderTestPanel());
  $("tl-run").addEventListener("click", runTest);
  $("cli-copy").addEventListener("click", copyReserve);
  $("cli-close").addEventListener("click", closeReserve);
  $("cli-dialog").addEventListener("cancel", (ev) => { ev.preventDefault(); closeReserve(); });
  // Capacity Blocks
  setUpBlocks();
  // Quotas
  fillRegionSelect($("q-region"), defaultRegion());
  $("q-region").addEventListener("change", () => { remember($("q-region").value); loadTab("quotas"); });
  $("q-refresh").addEventListener("click", () => { if (quotas.loading) return; loadTab("quotas", true); });
  // Alerts
  $("alerts-refresh").addEventListener("click", () => { if (alertsState.loading) return; loadAlerts(); });
  fillRegionSelect($("alert-region"), defaultRegion());
  $("alert-region").addEventListener("change", () => { remember($("alert-region").value); loadAlertTypes(); });
  $("alert-signal").addEventListener("change", onSignalChange);
  $("alert-type").addEventListener("change", () => setAlertTypeNote($("alert-type").options.length));
  $("alert-add").addEventListener("click", addAlert);
  onSignalChange();
  // Users
  setUpUsers();
  // Edit instance list dialog
  $("list-save").addEventListener("click", saveList);
  $("list-cancel").addEventListener("click", () => closeDialog($("list-dialog")));
  $("list-all").addEventListener("click", () => pickAllTypes(true));
  $("list-none").addEventListener("click", () => pickAllTypes(false));
  // Tabs
  for (const x of TABS) {
    $(`tab-${x.id}`).hidden = !tabOn(x);
    $(`tab-${x.id}`).addEventListener("click", () => selectTab(x.id));
  }
  $("tablist").addEventListener("keydown", onTabKey);
  window.addEventListener("hashchange", () => selectTab(location.hash.slice(1)));
  document.addEventListener("visibilitychange", onVisibilityChange);
}

// `regions` is every Region the routes cover (the enabled Regions minus excludeRegions), in the API's order; test launches
// run in the same Regions. An answer without a Region list falls back to probeRegions.
const uniqueNames = (v) => (Array.isArray(v) ? [...new Set(v.filter((r) => typeof r === "string" && r))] : null);
function normalizeConfig(c) {
  const probe = uniqueNames(c.probeRegions);
  const all = uniqueNames(c.regions);
  const regions = all && all.length ? all : probe || [];
  // regionNames: { code: long name } from the API; keep only text entries for listed Regions (a name can be missing).
  const names = {};
  if (c.regionNames && typeof c.regionNames === "object") for (const r of regions) { const n = c.regionNames[r]; if (typeof n === "string" && n.trim()) names[r] = n.trim(); }
  return { ...c, regions, probeRegions: probe, regionNames: names };
}

async function startApp() {
  $("boot-error-wrap").hidden = true;
  try { limits = normalizeConfig(await api("/api/config")); } catch (e) {
    if (e.status !== 401) showBootError(e);
    return;
  }
  if (!appSetUp) { setUpApp(); appSetUp = true; }
  signInError = null;
  renderSignInError();
  $("signed-out").hidden = true;
  $("app").hidden = false;
  renderChrome();
  scheduleFresh();
  await selectTab(location.hash.slice(1));
}

(async function main() {
  try { localStorage.removeItem("cmpOnlyMine"); } catch { /* storage blocked */ }   // flag of a removed option
  $("boot-retry").addEventListener("click", () => (cfg && sessionStorage.getItem("idToken") ? startApp() : location.reload()));
  try { cfg = await (await fetch("config.json", { cache: "no-cache" })).json(); } catch (e) { showBootError(e); return; }
  locale = pickLocale();
  try { await setLocale(locale); } catch { /* the built-in text covers the boot error */ }
  setUpLanguage();
  const p = new URLSearchParams(location.search);
  if (p.get("code")) {
    try { await finishSignIn(p.get("code"), p.get("state")); } catch (e) { clearTokens(); showSignedOut(e); return; }
  }
  if (sessionStorage.getItem("idToken")) await startApp();
  else showSignedOut();
})();
