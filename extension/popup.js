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
// quality menu state per trigger: current value ("best" or "itag:NNN") + the /qualities format matrix
const qCtx = { quality: { value: "best", matrix: [], qualities: [] }, castQuality: { value: "best", matrix: [], qualities: [], url: "" } };
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
function setPickQSpin(on) { const e = $("qSpin"); if (e) e.classList.toggle("on", on); }   // picker-view quality trigger
function castEnabled() { $("castBtn").disabled = !selectedId; }
async function activeTab() {
  const [tb] = await browser.tabs.query({ active: true, currentWindow: true });
  return tb || {};
}

async function init() {
  let ping;
  try { ping = await call("/ping"); }
  catch (e) { stopAll(); setLive(false); return view(e && e.unauthorized ? "needToken" : "noHelper"); }
  checkHelperVersion(ping);
  let status = {};
  try { status = await call("/status"); } catch {}
  if (status.casting) showCasting(status.name || status.device || "", whatOf(status), status.url, status.quality);
  else await showPicker();
  startStatusPoll();
}

// warn if the local helper is behind the extension: the two ship together (every release bumps both), so
// an older helper misses fixes and can reject newer requests. Compares the full major.minor.patch, so a
// patch-level helper update prompts too; a helper too old to report a version at all counts as behind.
function checkHelperVersion(ping) {
  const mm = (v) => { const p = String(v || "").split("."); return (Number(p[0]) || 0) * 1e6 + (Number(p[1]) || 0) * 1e3 + (Number(p[2]) || 0); };
  const helper = ping && ping.version;
  const behind = !helper || mm(helper) < mm(browser.runtime.getManifest().version);
  $("helperOld").hidden = !behind;
}
function stopAll() { stopScan(); stopStatusPoll(); }

function startStatusPoll() {
  stopStatusPoll();
  statusTimer = setInterval(async () => {
    let s; try { s = await call("/status"); } catch { return; }
    const inCasting = !$("castingView").hidden;
    // The casting stream's source failed (a signed url expired / a 4xx): stop it and tell the user to
    // reload the page. suppressUntil bounds it to one notice per cast; gated on inCasting so a lingering
    // flag never disturbs the picker.
    if (s.perror && inCasting && Date.now() >= suppressUntil) {
      suppressUntil = Date.now() + RECAST_SUPPRESS_MS;
      try { await call("/stop"); } catch {}
      try { await browser.storage.session.remove("castQuals"); } catch {}
      notify(t("errStreamExpired"), "err");
      return showPicker();
    }
    if (s.casting && !inCasting) showCasting(s.name || s.device || "", whatOf(s), s.url, s.quality);
    else if (!s.casting && inCasting) { if (Date.now() < suppressUntil) return; showPicker(); }
    else if (s.casting && inCasting) {
      $("castingName").textContent = s.name || s.device || "";
      $("castingTitle").textContent = whatOf(s);
      // recover the quality dropdown if state arrived after the view was shown (e.g. helper restart)
      if (s.url && s.url !== qCtx.castQuality.url) populateCastQuality(s.url, s.quality);
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

// ---- quality menu: a custom trigger + drill-down (resolution -> codec/range). A native <select>
// can't show the codec/dynamic-range variants, and a floating cascade would clip against the 300px
// popup edge, so each resolution drills down in place. Picks a value the helper casts by itag. ----
function qmk(tag, cls, txt) { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
function qLabel(id) {
  const c = qCtx[id];
  if (!c.value || c.value === "best") return t("qualityBest");
  const m = /^itag:(\w+)$/.exec(c.value);
  if (m) for (const r of c.matrix) { const s = r.fps.length === 1; for (const fp of r.fps) for (const o of fp.opts)
    if (o.itag === m[1]) return r.res + "p" + (s ? fp.fps : "") + " · " + o.codec + (o.range === "HDR" ? " HDR" : ""); }
  return c.value;
}
function qSyncTrig(id) { const b = $(id); if (!b) return; b.dataset.q = qCtx[id].value; b.querySelector(".qtl").textContent = qLabel(id); }
// is the current pick still offered by the freshly-loaded source? (else it should fall back to best)
function qValid(id) {
  const c = qCtx[id], v = c.value;
  if (!v || v === "best") return true;
  const m = /^itag:(\w+)$/.exec(v);
  if (m) { for (const r of c.matrix) for (const fp of r.fps) for (const o of fp.opts) if (o.itag === m[1]) return true; return false; }
  return (c.qualities || []).includes(v);
}
function qMenuRoot(id, menu, onPick) {
  menu.replaceChildren();
  const c = qCtx[id];
  const auto = qmk("div", "qm-item" + (c.value === "best" ? " sel" : "")); auto.appendChild(qmk("span", "qm-l", t("qualityBest")));
  auto.addEventListener("click", () => onPick("best")); menu.appendChild(auto);
  const flat = c.qualities || [];
  if (c.matrix.length || flat.length) menu.appendChild(qmk("div", "qm-sep"));
  if (c.matrix.length) {                        // YouTube: resolution -> codec/range drill-down
    for (const r of c.matrix) {
      const s = r.fps.length === 1, it = qmk("div", "qm-item");
      it.appendChild(qmk("span", "qm-l", r.res + "p" + (s ? r.fps[0].fps : "")));
      const badge = r.res >= 2160 ? "4K" : (r.res >= 1080 ? "HD" : ""); if (badge) it.appendChild(qmk("span", "qm-badge", badge));
      it.appendChild(qmk("span", "qm-chev", "›"));
      it.addEventListener("click", () => qMenuRes(id, menu, r, onPick));
      menu.appendChild(it);
    }
  } else {                                       // other sources (streamlink): a flat list of qualities
    for (const q of flat) {
      const it = qmk("div", "qm-item" + (c.value === q ? " sel" : ""));
      it.appendChild(qmk("span", "qm-l", q));
      it.addEventListener("click", () => onPick(q));
      menu.appendChild(it);
    }
  }
}
function qMenuRes(id, menu, r, onPick) {
  menu.replaceChildren();
  const s = r.fps.length === 1;
  const back = qmk("div", "qm-back"); back.appendChild(qmk("span", "qm-chev", "‹")); back.appendChild(qmk("span", null, r.res + "p" + (s ? r.fps[0].fps : "")));
  back.addEventListener("click", () => qMenuRoot(id, menu, onPick)); menu.appendChild(back);
  for (const fp of r.fps) for (const o of fp.opts) {
    const val = "itag:" + o.itag, it = qmk("div", "qm-item" + (qCtx[id].value === val ? " sel" : ""));
    it.appendChild(qmk("span", "qm-l", (s ? "" : fp.fps + "p · ") + o.codec + (o.range === "HDR" ? " · HDR" : "")));
    if (o.tbr) it.appendChild(qmk("span", "qm-hint", o.tbr + " Mbps"));
    it.addEventListener("click", () => onPick(val)); menu.appendChild(it);
  }
}
function qToggleMenu(id, onPick) {
  const menu = $(id + "Menu"), wasOpen = !menu.hidden;
  $("qualityMenu").hidden = true; $("castQualityMenu").hidden = true;
  if (wasOpen) return;
  qCtx[id]._pick = (val) => {
    menu.hidden = true;
    if (val === qCtx[id].value) return;         // no change -> don't re-cast / cut the TV
    qCtx[id].value = val; qSyncTrig(id); onPick(val);
  };
  qMenuRoot(id, menu, qCtx[id]._pick);
  menu.hidden = false;
}
// re-render an already-open menu once the format matrix loads (opened before /qualities came back)
function qRefresh(id) {
  const menu = $(id + "Menu");
  if (menu && !menu.hidden && qCtx[id]._pick) qMenuRoot(id, menu, qCtx[id]._pick);
}

// fill the casting-view quality dropdown from the renditions of the stream being cast
async function populateCastQuality(url, current) {
  qCtx.castQuality.value = current || "best";
  qCtx.castQuality.url = url || "";
  qCtx.castQuality.matrix = [];
  qCtx.castQuality.qualities = [];      // clear stale: a previous cast's list (even from another tab) must not linger
  $("castQualityMenu").hidden = true;   // and close a menu that could still be showing that old list
  qSyncTrig("castQuality");
  if (!url) return;
  setQSpin(true);
  try {
    if (SUPPORTED.some(s => url.includes(s))) {
      const res = await call("/qualities?url=" + encodeURIComponent(url));
      if ($("castingView").hidden || qCtx.castQuality.url !== url) return;   // view changed / a newer populate won
      qCtx.castQuality.matrix = res.matrix || [];
      qCtx.castQuality.qualities = res.qualities || [];
    } else {
      // A sniffed site's renditions are fixed for the whole cast, so use the list captured at cast time.
      // It's stored extension-wide (storage.session), so ANY window's cast view shows it without a
      // re-fetch and without depending on which tab is active.
      let cq = null;
      try { cq = (await browser.storage.session.get("castQuals")).castQuals; } catch {}
      if (cq && cq.url === url && (cq.qualities || []).length) {
        if ($("castingView").hidden || qCtx.castQuality.url !== url) return;
        qCtx.castQuality.qualities = cq.qualities;
      } else {
        // no stored list (e.g. the helper recovered a cast across a restart) -> read the cast page's own
        // renditions, but only when this window is actually on it (else we'd list a different tab's).
        const tb = await activeTab();
        if (!tb || tb.url !== url) return;
        const [det, lr] = await Promise.all([
          browser.runtime.sendMessage({ cmd: "getDetected", tabId: tb.id }).catch(() => null),
          readPageLadder(tb.id),
        ]);
        const srcs = ((det && det.sources) || []).filter(s => s.type === "hls");
        if (!srcs.length) return;
        const hs = {};
        for (const k of ["Referer", "Origin", "User-Agent"]) { const v = pickHeader(srcs[0].headers, k); if (v) hs[k] = v; }
        const body = "url=" + encodeURIComponent(srcs[0].url)
          + "&h=" + encodeURIComponent(JSON.stringify(hs))
          + "&urls=" + encodeURIComponent(JSON.stringify(srcs.map(s => s.url)))
          + "&ladder=" + encodeURIComponent(JSON.stringify(lr.ladder || {}));
        const res = await call("/qualities", { method: "POST", body });
        if ($("castingView").hidden || qCtx.castQuality.url !== url) return;
        qCtx.castQuality.qualities = res.qualities || [];
      }
    }
  } catch { return; }
  finally { setQSpin(false); }
  qSyncTrig("castQuality");
  qRefresh("castQuality");
}

// change quality while casting. For a streamlink site the helper re-resolves the picked format in place
// (/quality). For a sniffed site the stream is a fixed per-quality url, so switching means re-casting the
// picked quality's own url. Only the tab actually on the cast page can rebuild that, so guard on it.
async function changeCastQuality(val) {
  const url = qCtx.castQuality.url || "";
  setQSpin(true);
  suppressUntil = Date.now() + RECAST_SUPPRESS_MS;             // cover the helper's ~10s relaunch grace
  try {
    if (url && !SUPPORTED.some(s => url.includes(s))) {
      const tb = await activeTab();
      if (tb && tb.url === url) { qCtx.quality.value = val; await castCurrentTab(); }   // re-cast at the new quality
    } else {
      await call("/quality?value=" + encodeURIComponent(val));
    }
  } catch { notify(t("errNoHelper"), "err"); }
  setTimeout(() => setQSpin(false), RECAST_REENABLE_MS);       // proxy is up by then
}

async function showPicker() {
  setLive(false);
  view("picker");
  qCtx.castQuality.url = ""; qCtx.castQuality.value = "best";   // reset casting-view menu tracking
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

function pickHeader(headers, name) {   // sniffed headers keep original casing; match case-insensitively
  if (!headers) return "";
  const ln = name.toLowerCase();
  for (const k in headers) if (k.toLowerCase() === ln) return headers[k];
  return "";
}
// Read a quality ladder the page lists in its own player config (a set of {quality, url} HLS renditions
// exposed before any one is fetched, so the sniffer only ever sees the playing quality). Runs in the
// loaded page's MAIN world via activeTab (the isolated world can't see inline <script> globals), reads
// the rendition list directly, and falls back to scanning the HTML. No re-fetch (which hosts bot-block);
// the page's own auth already applies. Returns {ladder:{height:url}, diag:{...}}.
async function readPageLadder(tabId) {
  const diag = { scripting: !!(browser.scripting && browser.scripting.executeScript) };
  if (!diag.scripting) return { ladder: {}, diag };
  try {
    const res = await browser.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async () => {
        // Read-only scan of the page HTML for the player's rendition list ({quality, url} objects). No
        // touching of window globals (some sites boobytrap them and break their own player when read).
        // Targeted: find each HLS url, then read the quality from its immediate enclosing object. This
        // avoids scanning the whole (multi-MB) document for every JSON object.
        const out = {}, d = { htmlLen: 0, hits: 0 };
        const push = (q, u) => {
          const h = parseInt(q, 10);
          if (h >= 100 && h <= 4320 && /\.m3u8/.test(u) && out[h] === undefined) out[h] = String(u);
        };
        try {
          const html = document.documentElement.outerHTML; d.htmlLen = html.length;
          const urlRe = /"(?:videoUrl|url|src|file|manifest)"\s*:\s*"([^"]*\.m3u8[^"]*)"/g;
          let m;
          while ((m = urlRe.exec(html))) {
            d.hits++;
            let a = m.index; const lo = Math.max(0, m.index - 2000);
            while (a > lo && html[a] !== "{") a--;
            let b = m.index; const hi = Math.min(html.length, m.index + 2000);
            while (b < hi && html[b] !== "}") b++;
            const o = html.slice(a, b + 1);
            const q = o.match(/"(?:quality|label|res|height)"\s*:\s*"?(\d{3,4})p?"?/);
            if (q) { try { push(q[1], JSON.parse('"' + m[1] + '"')); } catch { push(q[1], m[1].replace(/\\\//g, "/")); } }
          }
          // Some players don't inline the m3u8 URLs; mediaDefinitions[].videoUrl points at a remote list
          // endpoint (.../media/hls/?s=...) that returns the per-quality URLs only when fetched. Fetch it
          // right here in the page: it's same-origin, so the browser attaches the session cookie itself and
          // the helper never handles it. Only when nothing was inlined.
          if (!Object.keys(out).length) {
            const rm = html.match(/"videoUrl"\s*:\s*"([^"]*?\\?\/media\\?\/hls\\?\/[^"]*?)"/);
            if (rm) {
              let ru; try { ru = JSON.parse('"' + rm[1] + '"'); } catch { ru = rm[1].replace(/\\\//g, "/"); }
              try {
                const resp = await fetch(ru, { credentials: "include" });
                const arr = await resp.json();
                for (const it of (Array.isArray(arr) ? arr : [])) {
                  const h = parseInt(it && it.quality, 10);
                  if (h >= 100 && h <= 4320 && it && typeof it.videoUrl === "string" && out[h] === undefined) out[h] = it.videoUrl;
                }
                d.resolved = Object.keys(out).length;
              } catch (e) { d.rerr = String(e).slice(0, 50); }
            }
          }
        } catch (e) { d.err = String(e).slice(0, 60); }
        return { out, d };
      },
    });
    const r = (res && res[0] && res[0].result) || {};
    return { ladder: r.out || {}, diag: Object.assign(diag, r.d || {}) };
  } catch (e) { diag.execErr = String(e).slice(0, 80); return { ladder: {}, diag }; }
}

async function loadQualities() {
  qCtx.quality.matrix = []; qCtx.quality.qualities = [];   // keep the picked value across stop/re-cast
  setPickQSpin(true);                                      // show it's loading so the menu isn't opened empty
  try {
    let body = "url=" + encodeURIComponent(activeUrl || "");
    if (!activeUrl || !SUPPORTED.some(s => activeUrl.includes(s))) {
      // not a streamlink-resolvable page: read the sniffed HLS sources; the helper finds the master among
      // them and lists its variants, plus reads any full ladder the watch page inlines in its HTML.
      const tb = await activeTab();
      // The sniffed sources and the page-ladder read are independent; run them together so the page scan
      // overlaps the background wakeup instead of stacking in series.
      const [det, lr] = await Promise.all([
        browser.runtime.sendMessage({ cmd: "getDetected", tabId: tb.id }).catch(() => null),
        readPageLadder(tb.id),                    // the full ladder read straight from the loaded page
      ]);
      // instant: the background precomputes this tab's qualities as the page plays, so show that cached
      // list right away (like the already-warm device list). The refresh below still runs so a rendition
      // that appeared after the precompute lands too.
      const pc = det && det.qualities;
      if (pc && (pc.qualities || []).length) {
        qCtx.quality.qualities = pc.qualities;
        qCtx.quality.matrix = pc.matrix || [];
        if (!qValid("quality")) qCtx.quality.value = "best";
        qSyncTrig("quality"); qRefresh("quality"); setPickQSpin(false);
      }
      const srcs = ((det && det.sources) || []).filter(s => s.type === "hls");
      if (!srcs.length) {
        if (!(qCtx.quality.qualities || []).length) { qCtx.quality.value = "best"; qSyncTrig("quality"); }
        return;
      }
      const hs = {};   // Referer/Origin/UA that the host needs to serve the master (never the Cookie, in a URL)
      for (const k of ["Referer", "Origin", "User-Agent"]) { const v = pickHeader(srcs[0].headers, k); if (v) hs[k] = v; }
      body = "url=" + encodeURIComponent(srcs[0].url)
           + "&h=" + encodeURIComponent(JSON.stringify(hs))
           + "&urls=" + encodeURIComponent(JSON.stringify(srcs.map(s => s.url)))
           + "&ladder=" + encodeURIComponent(JSON.stringify(lr.ladder || {}))
           + "&ldiag=" + encodeURIComponent(JSON.stringify(lr.diag || {}));
    }
    let res;
    try { res = await call("/qualities", { method: "POST", body }); }
    catch { return; }
    qCtx.quality.matrix = res.matrix || [];
    qCtx.quality.qualities = res.qualities || [];
    if (!qValid("quality")) qCtx.quality.value = "best";     // the pick isn't offered by this source
    qSyncTrig("quality");
    qRefresh("quality");
  } finally {
    setPickQSpin(false);
  }
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
// Every HLS source the sniffer captured for the tab (newest-first). The player fetches the master once
// then rides a variant, so the master is usually in here alongside the variant; the helper picks it out
// to list qualities and to cast adaptive / paired audio+video.
async function detectedHlsSources(tabId) {
  try {
    const det = await browser.runtime.sendMessage({ cmd: "getDetected", tabId });
    return ((det && det.sources) || []).filter(s => s.type === "hls");
  } catch { return []; }
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
  const quality = qCtx.quality.value || "best";
  let media = "", headers = "", medias = "", ladder = "";
  if (!SUPPORTED.some(s => url.includes(s))) {
    // unknown site -> use the media the background sniffer captured. Prefer the HLS sources (the helper
    // casts the master among them); fall back to any single sniffed source (e.g. a direct file).
    const det = await browser.runtime.sendMessage({ cmd: "getDetected", tabId: tb.id }).catch(() => null);
    const hls = ((det && det.sources) || []).filter(s => s.type === "hls");
    const src = hls[0] || (det && det.sources && det.sources[0]) || null;
    if (!src) {
      castEnabled();
      notify((await hasDetectPermission()) ? t("noSourceFound") : t("enableDetectHint"));
      return;
    }
    media = src.url;
    headers = JSON.stringify(src.headers || {});
    if (hls.length) medias = JSON.stringify(hls.map(s => s.url));
    // per-quality URLs read from the page (inline, or resolved in-page from a remote list endpoint) so a
    // picked quality can be cast by its own url. For a remote list this re-fetches -> fresh, unexpired urls.
    ladder = JSON.stringify((await readPageLadder(tb.id)).ladder || {});
  }
  try {
    // POST: the captured request headers (incl. Cookie) go in the body, never the URL/query.
    const body =
      `url=${encodeURIComponent(url)}&device=${encodeURIComponent(dev.host)}` +
      `&name=${encodeURIComponent(dev.name)}&title=${encodeURIComponent(what)}` +
      `&quality=${encodeURIComponent(quality)}&kind=${encodeURIComponent(dev.kind || "dlna")}` +
      (media ? `&media=${encodeURIComponent(media)}&headers=${encodeURIComponent(headers)}` : ``) +
      (medias ? `&medias=${encodeURIComponent(medias)}` : ``) +
      (ladder ? `&ladder=${encodeURIComponent(ladder)}` : ``);
    const r = await call("/cast", { method: "POST", body });
    if (r.ok) {
      // remember this cast's quality list extension-wide so the cast view shows it in any window/tab
      // without re-deriving it (the renditions don't change for the life of the cast).
      try { await browser.storage.session.set({ castQuals: { url: r.url || url, qualities: qCtx.quality.qualities || [] } }); } catch {}
      showCasting(r.name || dev.name, whatOf(r) || what, r.url || url, r.quality || quality);
    }
    else { castEnabled(); notify(castFailMsg(r), "err"); }
  } catch (e) {
    castEnabled(); notify(e && e.unauthorized ? t("needTokenHint") : t("errNoHelper"), "err");
  }
}

async function stopCast() {
  try { await call("/stop"); } catch {}
  try { await browser.storage.session.remove("castQuals"); } catch {}   // the cached quality list is stale now
  await showPicker();
}

$("castBtn").addEventListener("click", castCurrentTab);
$("quality").addEventListener("click", () => qToggleMenu("quality", () => {}));
$("castQuality").addEventListener("click", () => qToggleMenu("castQuality", changeCastQuality));
document.addEventListener("click", (e) => {   // click outside a trigger/menu closes any open menu
  if (e.target.closest(".qtrig") || e.target.closest(".qmenu")) return;
  $("qualityMenu").hidden = true; $("castQualityMenu").hidden = true;
}, true);
$("enableDetect").addEventListener("click", enableDetection);
$("stopBtn").addEventListener("click", stopCast);
$("retryHelper").addEventListener("click", init);
$("getHelper").addEventListener("click", () => browser.tabs.create({ url: SITE_URL }));
$("openOptions").addEventListener("click", () => browser.runtime.openOptionsPage());
$("notice").addEventListener("click", () => { $("notice").hidden = true; clearTimeout(noticeTimer); });
$("helperOld").addEventListener("click", (e) => { e.preventDefault(); browser.tabs.create({ url: SITE_URL + "#update" }); });

// boot
localize();
initTheme();
(async () => {
  const st = await browser.storage.local.get(["token"]);
  authToken = st.token || "";
  init();
})();
