// Streaming LAN Cast popup: loopback fetch to the local helper (127.0.0.1:9988).
// CONTROL / TOKEN_HEADER / DETECT_PERM come from constants.js (loaded first in popup.html).
const SUPPORTED = ["kick.com", "twitch.tv", "youtube.com", "youtu.be"];
const $ = (id) => document.getElementById(id);

let selectedId = null;
let pickerActive = false;
let scanTimer = null;
let statusTimer = null;
let activeUrl = "";
let themeMode = "auto";
let suppressUntil = 0;   // ignore casting:false during a quality re-cast (brief proxy gap)
let activeQuality = "best";   // quality currently being cast (to skip no-op changes)
let castQualityUrl = "";      // url the casting-view dropdown was populated for
let authToken = "";           // per-install secret shared with the helper (set in options)
const deviceMap = new Map();
const elMap = new Map();
const RECAST_SUPPRESS_MS = 11000;   // ignore casting:false this long after a re-cast (helper relaunch grace)
const RECAST_REENABLE_MS = 4000;    // re-enable the dropdown after this (proxy is up by then)

const ICON = {
  auto: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>',
  light: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  dark: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
};

// ---- i18n ----
function t(key, subs) { return browser.i18n.getMessage(key, subs) || key; }

// ---- inline notice (replaces native alert(): it renders clipped in this 300px popup) ----
let noticeTimer = 0;
function notify(msg, kind) {       // kind: "" = info, "err" = error
  const el = $("notice");
  el.textContent = msg;
  el.classList.toggle("err", kind === "err");
  el.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { el.hidden = true; }, 7000);
}
// map a /cast failure to a clear message: a known reason -> localized string,
// otherwise the generic "couldn't cast" plus the helper's detail.
const FAIL_KEY = { drm: "errDrm", unplayable: "errStreamUnavailable" };
function castFailMsg(r) {
  const k = r && FAIL_KEY[r.reason];
  if (k) { const m = t(k); if (m && m !== k) return m; }
  return t("errCantCast") + (r && r.error ? "\n" + r.error : "");
}

// ---- theme (auto by default; sun/moon control cycles auto -> light -> dark) ----
function resolveTheme() {
  return themeMode !== "auto" ? themeMode
    : (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
}
function applyTheme() {
  const eff = resolveTheme();
  document.documentElement.setAttribute("data-theme", eff);
  document.documentElement.style.colorScheme = eff;
  const b = $("themeBtn");
  const icon = new DOMParser().parseFromString(ICON[themeMode], "image/svg+xml");
  b.replaceChildren(document.importNode(icon.documentElement, true));
  b.title = t(themeMode === "auto" ? "themeAuto" : themeMode === "light" ? "themeLight" : "themeDark");
}
async function initTheme() {
  themeMode = (await browser.storage.local.get("theme")).theme || "auto";
  applyTheme();
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (themeMode === "auto") applyTheme(); });
  $("themeBtn").addEventListener("click", () => {
    themeMode = themeMode === "auto" ? "light" : themeMode === "light" ? "dark" : "auto";
    browser.storage.local.set({ theme: themeMode });
    applyTheme();
  });
}

async function call(path, opts) {
  const init = { cache: "no-store", headers: authToken ? { [TOKEN_HEADER]: authToken } : {} };
  if (opts && opts.method) init.method = opts.method;
  if (opts && opts.body != null) {
    init.body = opts.body;
    init.headers["Content-Type"] = "application/x-www-form-urlencoded";
  }
  const r = await fetch(CONTROL + path, init);
  if (r.status === 401 || r.status === 403) {
    const e = new Error("unauthorized"); e.unauthorized = true; throw e;
  }
  return r.json();
}
function whatOf(s) { return (s.title || "").trim() || (s.url || ""); }
function view(name) {
  $("noHelper").hidden = name !== "noHelper";
  $("needToken").hidden = name !== "needToken";
  $("pickerView").hidden = name !== "picker";
  $("castingView").hidden = name !== "casting";
}
function setLive(on) { $("title").classList.toggle("live", on); }
function setSpin(on) { $("spin").classList.toggle("on", on); }
function setQSpin(on) { $("castQSpin").classList.toggle("on", on); }
function castEnabled() { $("castBtn").disabled = !selectedId; }
async function activeTab() {
  const [tb] = await browser.tabs.query({ active: true, currentWindow: true });
  return tb || {};
}

async function init() {
  try { await call("/ping"); }
  catch (e) { stopAll(); setLive(false); return view(e && e.unauthorized ? "needToken" : "noHelper"); }
  let status = {};
  try { status = await call("/status"); } catch {}
  if (status.casting) showCasting(status.name || status.device || "", whatOf(status), status.url, status.quality);
  else await showPicker();
  startStatusPoll();
}
function stopAll() { stopScan(); stopStatusPoll(); }

function startStatusPoll() {
  stopStatusPoll();
  statusTimer = setInterval(async () => {
    let s; try { s = await call("/status"); } catch { return; }
    const inCasting = !$("castingView").hidden;
    if (s.casting && !inCasting) showCasting(s.name || s.device || "", whatOf(s), s.url, s.quality);
    else if (!s.casting && inCasting) { if (Date.now() < suppressUntil) return; showPicker(); }
    else if (s.casting && inCasting) {
      $("castingName").textContent = s.name || s.device || "";
      $("castingTitle").textContent = whatOf(s);
      // recover the quality dropdown if state arrived after the view was shown (e.g. helper restart)
      if (s.url && s.url !== castQualityUrl) populateCastQuality(s.url, s.quality);
    }
  }, 1500);
}
function stopStatusPoll() { if (statusTimer) { clearInterval(statusTimer); statusTimer = null; } }

function showCasting(name, what, url, quality) {
  stopScan();
  setLive(true);
  $("castingName").textContent = name;
  $("castingTitle").textContent = what || "";
  populateCastQuality(url || "", quality || "best");
  view("casting");
}

// guarantee an <option> exists for val so <select>.value=val never silently falls back to "best"
function ensureOption(sel, val) {
  if (val && val !== "best" && ![...sel.options].some(o => o.value === val)) {
    const o = document.createElement("option");
    o.value = val; o.textContent = val;
    sel.appendChild(o);
  }
}
function buildQualityOptions(sel, qualities, current) {
  sel.replaceChildren();
  const best = document.createElement("option");
  best.value = "best"; best.textContent = t("qualityBest");
  sel.appendChild(best);
  for (const qual of (qualities || [])) {
    const o = document.createElement("option");
    o.value = qual; o.textContent = qual;
    sel.appendChild(o);
  }
  ensureOption(sel, current);          // keep the active quality representable even if not in the list
  sel.value = current || "best";
}

// fill the casting-view quality dropdown from the renditions of the stream being cast
async function populateCastQuality(url, current) {
  current = current || "best";
  activeQuality = current;
  castQualityUrl = url || "";
  const sel = $("castQuality");
  buildQualityOptions(sel, [], current);          // immediate: best + the active quality
  if (!url || !SUPPORTED.some(s => url.includes(s))) return;
  setQSpin(true);
  let res;
  try { res = await call("/qualities?url=" + encodeURIComponent(url)); }
  catch { return; }
  finally { setQSpin(false); }
  if ($("castingView").hidden || castQualityUrl !== url) return;   // view changed / a newer populate won
  buildQualityOptions(sel, res.qualities, activeQuality);          // rebuild with real renditions
}

// change quality while casting -> helper re-casts the same stream at the new quality
async function changeCastQuality() {
  const sel = $("castQuality");
  const val = sel.value || "best";
  if (val === activeQuality) return;              // no change -> don't cut the TV
  activeQuality = val;
  sel.disabled = true; setQSpin(true);
  suppressUntil = Date.now() + RECAST_SUPPRESS_MS;             // cover the helper's ~10s relaunch grace
  try { await call("/quality?value=" + encodeURIComponent(val)); }
  catch { notify(t("errNoHelper"), "err"); }
  setTimeout(() => { sel.disabled = false; setQSpin(false); }, RECAST_REENABLE_MS);  // proxy is up by then
}

async function showPicker() {
  setLive(false);
  view("picker");
  castQualityUrl = ""; activeQuality = "best";   // reset casting-view dropdown tracking
  deviceMap.clear(); elMap.clear(); $("devices").replaceChildren();
  selectedId = (await browser.storage.local.get("lastDevice")).lastDevice || null;
  const tb = await activeTab();
  activeUrl = tb.url || "";
  try { mergeDevices((await call("/devices")).devices || []); }
  catch { stopAll(); setLive(false); return view("noHelper"); }
  startScan();
  loadQualities();
  refreshDetectUI();
  updateSourceStatus();
}

function startScan() { if (pickerActive) return; pickerActive = true; setSpin(true); scanLoop(); }
function stopScan() { pickerActive = false; if (scanTimer) { clearTimeout(scanTimer); scanTimer = null; } setSpin(false); }
async function scanLoop() {
  if (!pickerActive) return;
  let res;
  try { res = await call("/devices?fresh=1"); }
  catch { stopAll(); setLive(false); return view("noHelper"); }
  if (!pickerActive) return;
  mergeDevices(res.devices || []);
  updateSourceStatus();              // refresh the detected-source line while the picker is open
  scanTimer = setTimeout(scanLoop, 1200);
}

function mergeDevices(found) {
  const ids = new Set(found.map(d => d.id));
  for (const [id, rec] of [...deviceMap]) {
    if (ids.has(id)) rec.misses = 0;
    else { rec.misses++; if (rec.misses >= 2) { removeDeviceEl(id); deviceMap.delete(id); if (selectedId === id) selectedId = null; } }
  }
  for (const d of found) {
    if (deviceMap.has(d.id)) { deviceMap.get(d.id).device = d; updateDeviceEl(d); }
    else { deviceMap.set(d.id, { device: d, misses: 0 }); addDeviceEl(d); }
  }
  if (!selectedId && deviceMap.size) selectedId = [...deviceMap.keys()][0];
  refreshSelectionUI(); updatePlaceholder(); castEnabled();
}

function addDeviceEl(d) {
  const lab = document.createElement("label");
  lab.className = "dev"; lab.dataset.id = d.id;
  const radio = document.createElement("input"); radio.type = "radio"; radio.name = "dev";
  const name = document.createElement("span"); name.className = "name"; name.textContent = d.name;
  const model = document.createElement("span"); model.className = "model"; model.textContent = d.model || "";
  lab.append(radio, name, model);
  lab.addEventListener("click", () => selectDevice(d.id));
  $("devices").appendChild(lab);
  elMap.set(d.id, lab);
}
function updateDeviceEl(d) {
  const el = elMap.get(d.id); if (!el) return;
  el.querySelector(".name").textContent = d.name;
  el.querySelector(".model").textContent = d.model || "";
}
function removeDeviceEl(id) { const el = elMap.get(id); if (el) el.remove(); elMap.delete(id); }

function selectDevice(id) {
  selectedId = id;
  browser.storage.local.set({ lastDevice: id });
  refreshSelectionUI(); castEnabled();
}
function refreshSelectionUI() {
  for (const [id, el] of elMap) {
    const sel = id === selectedId;
    el.classList.toggle("sel", sel);
    const r = el.querySelector("input"); if (r) r.checked = sel;
  }
}
function updatePlaceholder() {
  let ph = $("devices").querySelector(".ph");
  if (elMap.size === 0 && !ph) {
    ph = document.createElement("div"); ph.className = "ph muted center";
    ph.textContent = t("searching"); $("devices").appendChild(ph);
  } else if (elMap.size > 0 && ph) ph.remove();
}

async function loadQualities() {
  const sel = $("quality");
  buildQualityOptions(sel, [], "best");                // immediate: just "best"
  if (!activeUrl || !SUPPORTED.some(s => activeUrl.includes(s))) return;
  let res;
  try { res = await call("/qualities?url=" + encodeURIComponent(activeUrl)); }
  catch { return; }
  buildQualityOptions(sel, res.qualities, "best");     // rebuild with real renditions
}

// ---- generic-site discovery (optional webRequest + <all_urls>, granted from the popup) ----
// DETECT_PERM is defined in constants.js (loaded first in popup.html).
async function hasDetectPermission() {
  try { return await browser.permissions.contains(DETECT_PERM); } catch { return false; }
}
async function refreshDetectUI() {
  $("enableDetect").hidden = await hasDetectPermission();
}
async function enableDetection() {
  let granted = false;
  try { granted = await browser.permissions.request(DETECT_PERM); } catch {}
  await refreshDetectUI();
  if (granted) notify(t("detectEnabledReload"));
}
async function detectedSource(tabId) {
  try {
    const det = await browser.runtime.sendMessage({ cmd: "getDetected", tabId });
    return (det && det.sources && det.sources[0]) || null;
  } catch { return null; }
}
// live "source detected" indicator for unknown sites, so the user sees the sniffer working
async function updateSourceStatus() {
  const st = $("status");
  const tb = await activeTab();
  const url = tb.url || "";
  if (!url || SUPPORTED.some(s => url.includes(s))) { st.textContent = ""; return; }
  if (!(await hasDetectPermission())) { st.textContent = ""; return; }   // enableDetect button covers this
  const src = await detectedSource(tb.id);
  st.textContent = src ? `${t("sourceDetected")} (${src.type.toUpperCase()})` : t("sourceNone");
}

async function castCurrentTab() {
  if (!selectedId) return;
  const dev = deviceMap.get(selectedId) && deviceMap.get(selectedId).device;
  if (!dev) return;
  $("castBtn").disabled = true;
  const tb = await activeTab();
  const url = tb.url || "";
  const what = (tb.title || "").trim() || url;
  const quality = $("quality").value || "best";
  let media = "", headers = "";
  if (!SUPPORTED.some(s => url.includes(s))) {
    // unknown site -> use the media source the background sniffer captured (if any)
    const src = await detectedSource(tb.id);
    if (!src) {
      castEnabled();
      notify((await hasDetectPermission()) ? t("noSourceFound") : t("enableDetectHint"));
      return;
    }
    media = src.url;
    headers = JSON.stringify(src.headers || {});
  }
  try {
    // POST: the captured request headers (incl. Cookie) go in the body, never the URL/query.
    const body =
      `url=${encodeURIComponent(url)}&device=${encodeURIComponent(dev.host)}` +
      `&name=${encodeURIComponent(dev.name)}&title=${encodeURIComponent(what)}` +
      `&quality=${encodeURIComponent(quality)}&kind=${encodeURIComponent(dev.kind || "dlna")}` +
      (media ? `&media=${encodeURIComponent(media)}&headers=${encodeURIComponent(headers)}` : ``);
    const r = await call("/cast", { method: "POST", body });
    if (r.ok) showCasting(r.name || dev.name, whatOf(r) || what, r.url || url, r.quality || quality);
    else { castEnabled(); notify(castFailMsg(r), "err"); }
  } catch (e) {
    castEnabled(); notify(e && e.unauthorized ? t("needTokenHint") : t("errNoHelper"), "err");
  }
}

async function stopCast() { try { await call("/stop"); } catch {} await showPicker(); }

$("castBtn").addEventListener("click", castCurrentTab);
$("castQuality").addEventListener("change", changeCastQuality);
$("enableDetect").addEventListener("click", enableDetection);
$("stopBtn").addEventListener("click", stopCast);
$("retryHelper").addEventListener("click", init);
$("getHelper").addEventListener("click", () => browser.tabs.create({ url: SITE_URL }));
$("openOptions").addEventListener("click", () => browser.runtime.openOptionsPage());
$("notice").addEventListener("click", () => { $("notice").hidden = true; clearTimeout(noticeTimer); });

// boot
localize();
initTheme();
(async () => {
  const st = await browser.storage.local.get(["token"]);
  authToken = st.token || "";
  init();
})();
