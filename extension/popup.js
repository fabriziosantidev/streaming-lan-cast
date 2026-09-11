// Streaming LAN Cast popup: loopback fetch to the local helper (127.0.0.1:9988).
// CONTROL / TOKEN_HEADER / DETECT_PERM come from constants.js (loaded first in popup.html).
// Pages the helper resolves itself. Everything else goes through the sniffer, which is the general
// path, so this list routes rather than restricts. Matched on the host: a page that carries another
// address in its query string is not that host and must not be routed as if it were.
const SUPPORTED = ["kick.com", "twitch.tv", "youtube.com", "youtu.be"];
function isSupported(u) {
  let h;
  try { h = new URL(u).hostname.toLowerCase(); } catch { return false; }
  return SUPPORTED.some(s => h === s || h.endsWith("." + s));
}
const $ = (id) => document.getElementById(id);

let selectedId = null;
let pickerActive = false;
let scanTimer = null;
let statusTimer = null;
let activeUrl = "";
let themeMode = "auto";
let suppressUntil = 0;   // ignore casting:false during a quality re-cast (brief proxy gap)
let pagePos = 0;         // where the page's own player sits, offered as a starting point
let pageDur = 0;         // its runtime, as the page's own player reports it
let pageLive = false;    // the page says it is showing a broadcast that is still running
let pageBehind = 0;      // how far behind its edge that broadcast is being watched
let castBehind = 0;      // the distance the next cast should open at, for a replayable live source
let castSeekable = false;  // the running cast carries its own timeline, so it can be asked for a moment
let castReplay = false;    // ...and that timeline is one we built, so moving means anchoring it again
let castBehindNow = 0;     // how far behind its edge the page is, for a replay that follows it
let posTick = 0;         // throttles re-reading that position while the popup stays open
let castFrom = -1;       // a point inside the recording to open the cast at, -1 = the live edge
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
// For a string that reaches a screen: a missing catalogue entry falls back to readable text, never the key name.
function tOr(key, fallback) { return browser.i18n.getMessage(key) || fallback; }

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
  // A caller that is holding up the first view can cap the wait: a connection to a port nothing
  // listens on is refused immediately on some systems and left hanging on others, and the popup has
  // nothing to show until this answers.
  let timer;
  if (opts && opts.timeoutMs) {
    const ac = new AbortController();
    init.signal = ac.signal;
    timer = setTimeout(() => ac.abort(), opts.timeoutMs);
  }
  let r;
  try { r = await fetch(CONTROL + path, init); }
  finally { clearTimeout(timer); }
  if (r.status === 401 || r.status === 403) {
    const e = new Error("unauthorized"); e.unauthorized = true; throw e;
  }
  return r.json();
}
function whatOf(s) { return (s.title || "").trim() || (s.url || ""); }
// The recording of what this tab is playing, if it has one. The sniffer resolves it in the background
// while the page runs; when it has not got there yet, ask the helper directly rather than offering
// nothing. A page only fetches its recording once the viewer rewinds, so a lookup that comes up empty
// is worth repeating while the picker is open.
let recUrl = "";            // the recording once known; kept for the life of the popup
let recTried = 0;           // when the last fruitless lookup ran, so retries stay cheap but keep coming
async function tabRecording(tabId) {
  if (recUrl) return recUrl;
  if (Date.now() - recTried < 4000) return "";
  recTried = Date.now();
  const det = await browser.runtime.sendMessage({ cmd: "getDetected", tabId }).catch(() => null);
  if (det && det.rec) return (recUrl = det.rec);
  const srcs = ((det && det.sources) || []).filter(s => s.type === "hls");
  if (!srcs.length) return "";
  const hs = {};
  for (const k of ["Referer", "Origin", "User-Agent"]) { const v = pickHeader(srcs[0].headers, k); if (v) hs[k] = v; }
  const body = "urls=" + encodeURIComponent(JSON.stringify(srcs.map(s => s.url).slice(0, 6)))
             + "&h=" + encodeURIComponent(JSON.stringify(hs));
  try {
    const r = await call("/recording", { method: "POST", body });
    return (recUrl = (r && r.rec) || "");
  } catch { return ""; }
}
// A point in a recording, spelled out: 45s, 12m 34s, 1h 13m 13s. Empty units are dropped from the
// front, so a short position stays short.
function hms(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), x = sec % 60;
  return h ? h + "h " + m + "m " + x + "s" : m ? m + "m " + x + "s" : x + "s";
}
function view(name) {
  $("booting").hidden = true;            // a real view is up; the first-paint stand-in is done
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
  try { ping = await call("/ping", { timeoutMs: 2500 }); }
  catch (e) { stopAll(); setLive(false); return view(e && e.unauthorized ? "needToken" : "noHelper"); }
  checkHelperVersion(ping);
  let status = {};
  try { status = await call("/status"); } catch {}
  if (status.casting) showCasting(status.name || status.device || "", whatOf(status), status.url, status.quality);
  else await showPicker();
  startStatusPoll();
}

// Prompt when the local helper is behind the latest published version. The helper reports `latest`
// (and `min_ext`, the minimum extension that helper needs) from the release manifest, so a helper-only
// release can nudge without shipping a new extension. Only prompt when this extension can actually drive
// that helper: if the latest helper needs a newer extension than this one, stay quiet until the store
// updates the extension. A helper too old to report `latest` falls back to comparing against this
// extension's own version.
function checkHelperVersion(ping) {
  const mm = (v) => { const p = String(v || "").split("."); return (Number(p[0]) || 0) * 1e6 + (Number(p[1]) || 0) * 1e3 + (Number(p[2]) || 0); };
  const helper = ping && ping.version;
  let behind;
  if (ping && ping.latest) {
    const helperOld = !helper || mm(helper) < mm(ping.latest);
    const extOk = !ping.min_ext || mm(browser.runtime.getManifest().version) >= mm(ping.min_ext);
    behind = helperOld && extOk;
  } else {
    behind = !helper || mm(helper) < mm(browser.runtime.getManifest().version);
  }
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
    // Two ways a cast can be moved through. One has a recording of the broadcast to switch onto and
    // an edge to come back to. The other carries its own timeline and is simply asked for another
    // moment of itself, which is every finished video and every live stream being replayed: there is
    // no edge to return to there, so that control stays out of it.
    const dv = !!(s.casting && s.dvr);
    const sk = !!(s.casting && s.seekable && !s.dvr);
    const replay = sk && s.seekable === "replay";
    $("rewindRow").hidden = !(dv || sk);
    // A cast that opened inside a recording because the broadcast is over has no edge to return to.
    $("backLive").hidden = sk || !!s.nolive;
    // A replay counts from its own anchor, so a moment of it cannot be named by the page's clock.
    // What can be asked for is another anchor: the replay is rebuilt to start where the page now is.
    $("rewindHere").hidden = false;
    castReplay = replay;
    if ((dv || sk) && posTick-- <= 0) {   // the page keeps playing, so refresh the point on offer
      posTick = 5;
      const tb = await activeTab();
      const pm = tb.id != null ? await readPageMedia(tb.id) : { t: 0 };
      pagePos = pm.t || 0;
      // On a page showing a running broadcast the point worth naming is how far back it is being
      // watched, which its own player answers; its position counts from somewhere else entirely.
      const shown = pm.live ? (pm.atEdge ? 0 : (pm.behind || 0)) : pagePos;
      castBehindNow = pm.live && !pm.atEdge ? (pm.behind || 0) : 0;
      const enough = pm.live ? shown > 60 : shown > 30;
      $("rewindHere").querySelector(".lbl").textContent =
        enough ? hms(shown) : tOr("rewindHere", "Position");
      $("rewindHere").disabled = !enough;
    }
    castSeekable = sk;
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
    if (isSupported(url)) {
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
      // A supplied-address cast has no rendition list to offer; deriving one from whatever tab is
      // open would list renditions of a different source, and picking one re-casts that source.
      if (cq && cq.url === url && cq.manual) return;
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
    if (url && !isSupported(url)) {
      const tb = await activeTab();
      if (tb && tb.url === url) { qCtx.quality.value = val; await castCurrentTab(); }   // re-cast at the new quality
    } else {
      await call("/quality?value=" + encodeURIComponent(val));
    }
  } catch { notify(t("errNoHelper"), "err"); }
  setTimeout(() => setQSpin(false), RECAST_REENABLE_MS);       // proxy is up by then
}

// Offer to start a cast inside the recording, when this tab has one.
// A live stream can be cast from behind its edge only where the helper can ask its source for
// earlier segments, which today means YouTube; every other site keeps the plain cast it has always
// had. Offering the choice more widely would promise a rewind that arrives at the live edge anyway.
function replayableLive(url) {
  let h;
  try { h = new URL(url).hostname; } catch { return false; }
  return /(^|\.)youtube\.com$/.test(h) || /(^|\.)youtu\.be$/.test(h);
}

async function refreshStartRow() {
  const row = $("startRow");
  const tb = await activeTab();
  if (tb.id == null || !isSupported(tb.url || "")) { row.hidden = true; $("castRow").hidden = false; return; }
  const shown = readPageMedia(tb.id).then((pm) => {
    pagePos = pm.t || 0;
    pageDur = pm.d || 0;
    pageLive = !!pm.live;
    // On a running broadcast the point on offer is how far back it is being watched, which the page's
    // player answers directly. Sitting at the edge is nothing to offer at all.
    pageBehind = pageLive && !pm.atEdge ? (pm.behind || 0) : 0;
    const off = pageLive ? pageBehind : pagePos;
    const enough = pageLive ? pageBehind > 60 : pagePos > 30;
    $("startAtHere").querySelector(".lbl").textContent =
      enough ? hms(off) : tOr("rewindHere", "Position");
    $("startAtHere").disabled = !enough;
  }).catch(() => {});
  const rec = await tabRecording(tb.id);
  await shown;
  // With a recording in hand these replace the plain cast: on a page whose broadcast has ended there
  // is no live to cast, and where there is one the casting view still offers a way back to it. A page
  // with no separate recording still gets them once its own player is known to sit inside a finite
  // video, which is what a source that casts as a seekable VOD looks like from here.
  // A page still showing a running broadcast is left out: its cast opens on the live edge, which has
  // no earlier point to start from, so the buttons would promise something they cannot deliver.
  const atPos = !rec && (pageLive
    ? replayableLive(tb.url || "") && pageBehind > 60
    : Number.isFinite(pageDur) && pageDur > 0 && pagePos > 30);
  // A running broadcast has no beginning to offer: what it keeps reaches back only so far, and how
  // far is the source's answer, not the page's.
  $("startAtZero").hidden = !!pageLive;
  row.hidden = !(rec || atPos);
  $("castRow").hidden = !!(rec || atPos);
}

async function showPicker() {
  setLive(false);
  view("picker");
  qCtx.castQuality.url = ""; qCtx.castQuality.value = "best";   // reset casting-view menu tracking
  deviceMap.clear(); elMap.clear(); $("devices").replaceChildren();
  selectedId = (await browser.storage.local.get("lastDevice")).lastDevice || null;
  const tb = await activeTab();
  if ((tb.url || "") !== activeUrl) { recUrl = ""; recTried = 0; }   // another page, another recording
  activeUrl = tb.url || "";
  try { mergeDevices((await call("/devices")).devices || []); }
  catch { stopAll(); setLive(false); return view("noHelper"); }
  startScan();
  loadQualities();
  refreshDetectUI();
  updateSourceStatus();
  refreshStartRow();
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
  refreshStartRow();                 // the recording may only surface once the page fetches it
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
    if (!activeUrl || !isSupported(activeUrl)) {
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
      const srcs = ((det && det.sources) || []).filter(s => s.type === "hls" || s.type === "dash");
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
  if (!url || isSupported(url)) { st.textContent = ""; return; }
  if (!(await hasDetectPermission())) { st.textContent = ""; return; }   // enableDetect button covers this
  const src = await detectedSource(tb.id);
  st.textContent = src ? `${t("sourceDetected")} (${src.type.toUpperCase()})` : t("sourceNone");
}

// A stream address supplied in the picker, for a page whose media the sniffer cannot reach. Only
// counts while the field is showing, so a leftover value can't silently override a normal cast.
function manualMedia() {
  const row = $("manualRow"), inp = $("manualUrl");
  const v = row && !row.hidden && inp ? inp.value.trim() : "";
  return /^https?:\/\//i.test(v) ? v : "";
}

// What the page's own <video> elements point at, for a source that never crossed the wire. Only an
// ordinary address is usable: a MediaSource-fed element carries a blob: url, a buffer inside that
// page, which can be neither re-fetched nor opened by a target. The largest element across every
// reachable frame wins (players commonly live in an embed); blobOnly = a video exists, unreachable.
async function readPageMedia(tabId) {
  if (!(browser.scripting && browser.scripting.executeScript)) return { url: "", blobOnly: false };
  const probe = () => {
    // A live edge and a recording look alike from the media element: on a stream that has run for
    // hours with its whole window seekable, the runtime and the seekable end both sit still while
    // playback advances. The page's own description of the broadcast is what separates them.
    const lb = document.querySelector('meta[itemprop="isLiveBroadcast"]');
    const live = !!lb && String(lb.content || "").toLowerCase() !== "false"
                 && !document.querySelector('meta[itemprop="endDate"]');
    // A running broadcast's media element reports a runtime that is a placeholder, so how far behind
    // the edge it is being watched cannot be taken from it: on one stream it read as 188 days, on
    // another as fourteen hours, and neither moved with playback. The page's own player keeps the
    // seekable bounds that do mean something, and says whether it is sitting at the edge at all.
    let behind = 0, atEdge = false, dvrWindow = 0;
    try {
      const mp = document.querySelector('#movie_player');
      const ps = mp && mp.getProgressState ? mp.getProgressState() : null;
      if (ps && isFinite(ps.seekableEnd) && isFinite(ps.current)) {
        behind = Math.max(0, ps.seekableEnd - ps.current);
        dvrWindow = Math.max(0, ps.seekableEnd - (ps.seekableStart || 0));
        atEdge = !!ps.isAtLiveHead;
      }
    } catch (e) {}
    let best = "", bestArea = -1, blobOnly = false, bestT = 0, bestD = 0;
    for (const v of document.querySelectorAll("video")) {
      const s = v.currentSrc || v.src || "";
      const a = (v.videoWidth || v.clientWidth || 0) * (v.videoHeight || v.clientHeight || 0);
      if (/^https?:\/\//i.test(s)) {
        if (a > bestArea) { bestArea = a; best = s; bestT = v.currentTime || 0; bestD = v.duration || 0; }
      } else if (s) {
        blobOnly = true;                       // a MediaSource src still reports a usable position
        if (a > bestArea) { bestArea = a; bestT = v.currentTime || 0; bestD = v.duration || 0; }
      }
    }
    return { url: best, area: bestArea, blobOnly, t: bestT, d: bestD, live, behind, atEdge, dvrWindow };
  };
  // In the page's own world, where a player that answers for its live bounds is reachable: those are
  // methods the page script hangs on its element, and an isolated world sees the element without
  // them. The media element itself reads the same either way, so a world that cannot be had costs
  // only the live bounds.
  let res;
  try {
    res = await browser.scripting.executeScript({ target: { tabId, allFrames: true }, world: "MAIN", func: probe });
  } catch {
    try {
      res = await browser.scripting.executeScript({ target: { tabId }, world: "MAIN", func: probe });
    } catch {
      try { res = await browser.scripting.executeScript({ target: { tabId, allFrames: true }, func: probe }); }
      catch {
        try { res = await browser.scripting.executeScript({ target: { tabId }, func: probe }); }
        catch { return { url: "", blobOnly: false }; }
      }
    }
  }
  let url = "", area = -1, blobOnly = false, t = 0, d = 0, live = false;
  let behind = 0, atEdge = false, dvrWindow = 0;
  for (const f of res || []) {
    const r = f && f.result;
    if (!r) continue;
    if (r.url && r.area > area) { area = r.area; url = r.url; }
    if (r.t > t) { t = r.t; d = r.d || 0; }   // the runtime of the frame the position came from
    blobOnly = blobOnly || !!r.blobOnly;
    live = live || !!r.live;              // the frame carrying the page's own markup is the one that knows
    if (r.behind > behind) { behind = r.behind; dvrWindow = r.dvrWindow || 0; atEdge = !!r.atEdge; }
    else if (r.atEdge) atEdge = true;
  }
  return { url, blobOnly, t, d, live, behind, atEdge, dvrWindow };
}

// Read off the path, so a signed query string does not hide the extension.
function isHlsPlaylist(u) {
  let p;
  try { p = new URL(u).pathname.toLowerCase(); } catch { return false; }
  return p.endsWith(".m3u8") || p.endsWith(".m3u");
}

// The button casts whichever source is in play, so its label follows the field.
function syncCastLabel() {
  $("castBtn").textContent = manualMedia() ? tOr("castUrlButton", "Cast this URL")
                                           : tOr("castButton", "Cast this tab");
}

async function castCurrentTab() {
  if (!selectedId) return;
  const dev = deviceMap.get(selectedId) && deviceMap.get(selectedId).device;
  if (!dev) return;
  $("castBtn").disabled = true;
  const tb = await activeTab();
  const url = tb.url || "";
  const quality = qCtx.quality.value || "best";
  let media = "", headers = "", medias = "", ladder = "", dvrRec = "";
  const supplied = manualMedia();
  // A supplied address is unrelated to the open page, so the tab's title names the wrong thing; sent
  // empty, the helper reads a title off the page url, which is the tab again. So send a fixed label.
  const what = supplied ? tOr("manualTitle", "Stream") : ((tb.title || "").trim() || url);
  if (supplied) {
    media = supplied;
    // The source list is what has the helper read the stream from its content, so an on-demand one
    // keeps its seek bar (a bare media url is treated as live). The list is read as HLS playlists:
    // a direct file or a DASH manifest in it would be served through the wrong proxy.
    if (isHlsPlaylist(supplied)) medias = JSON.stringify([supplied]);
    // Carry over the headers the sniffer captured on this tab. A host that checks Referer or Origin
    // refuses a request arriving without them, so the address alone is not enough to fetch it.
    const det = await browser.runtime.sendMessage({ cmd: "getDetected", tabId: tb.id }).catch(() => null);
    const seen = (det && det.sources || []).find(s => s.headers && Object.keys(s.headers).length);
    headers = JSON.stringify((seen && seen.headers) || {});
  } else if (!isSupported(url)) {
    // unknown site -> use the media the background sniffer captured. Prefer the HLS sources (the helper
    // casts the master among them); fall back to any single sniffed source (e.g. a direct file).
    const det = await browser.runtime.sendMessage({ cmd: "getDetected", tabId: tb.id }).catch(() => null);
    const hls = ((det && det.sources) || []).filter(s => s.type === "hls");
    const src = hls[0] || (det && det.sources && det.sources[0]) || null;
    if (!src) {
      // Nothing crossed the wire; ask the page what its own elements point at.
      const el = await readPageMedia(tb.id);
      if (!el.url) {
        castEnabled();
        notify(el.blobOnly
          ? tOr("errBlobSource", "This page's video has no address that can be cast.")
          : ((await hasDetectPermission()) ? t("noSourceFound") : t("enableDetectHint")));
        return;
      }
      media = el.url;
    } else {
      media = src.url;
      headers = JSON.stringify(src.headers || {});
      if (hls.length) medias = JSON.stringify(hls.map(s => s.url));
      // per-quality URLs read from the page (inline, or resolved in-page from a remote list endpoint) so a
      // picked quality can be cast by its own url. For a remote list this re-fetches -> fresh, unexpired urls.
      ladder = JSON.stringify((await readPageLadder(tb.id)).ladder || {});
    }
  } else {
    // A helper-resolved page may also expose a recording of the ongoing broadcast, which its player
    // fetches over the wire once the viewer rewinds. Every sniffed playlist goes along as a candidate;
    // the helper tells the recording apart from the live window by content, and only then offers a
    // rewind. Watching live stays on the low-latency edge either way.
    dvrRec = await tabRecording(tb.id);
  }
  try {
    // POST: the captured request headers (incl. Cookie) go in the body, never the URL/query.
    const body =
      `url=${encodeURIComponent(url)}&device=${encodeURIComponent(dev.host)}` +
      `&name=${encodeURIComponent(dev.name)}&title=${encodeURIComponent(what)}` +
      `&quality=${encodeURIComponent(quality)}&kind=${encodeURIComponent(dev.kind || "dlna")}` +
      (media ? `&media=${encodeURIComponent(media)}&headers=${encodeURIComponent(headers)}` : ``) +
      (medias ? `&medias=${encodeURIComponent(medias)}` : ``) +
      (ladder ? `&ladder=${encodeURIComponent(ladder)}` : ``) +
      (dvrRec ? `&dvrrec=${encodeURIComponent(dvrRec)}` : ``) +
      (dvrRec && castFrom >= 0 ? `&dvrstart=${Math.floor(castFrom)}` : ``) +
      (!dvrRec && castFrom > 0 ? `&start=${Math.floor(castFrom)}` : ``) +
      (!dvrRec && castBehind > 0 ? `&behind=${Math.floor(castBehind)}` : ``);
    castFrom = -1; castBehind = 0;      // consumed: a later plain cast opens on the live edge
    const r = await call("/cast", { method: "POST", body });
    if (r.ok) {
      // remember this cast's quality list extension-wide so the cast view shows it in any window/tab
      // without re-deriving it (the renditions don't change for the life of the cast).
      try { await browser.storage.session.set({ castQuals: { url: r.url || url, manual: !!supplied,
        qualities: supplied ? [] : (qCtx.quality.qualities || []) } }); } catch {}
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
$("manualToggle").addEventListener("click", () => {
  const row = $("manualRow");
  row.hidden = !row.hidden;
  if (!row.hidden) $("manualUrl").focus();
  syncCastLabel();
});
$("manualUrl").addEventListener("input", syncCastLabel);
$("manualPaste").addEventListener("click", async () => {
  try { $("manualUrl").value = (await navigator.clipboard.readText()).trim(); }
  catch { $("manualUrl").focus(); }   // clipboard read blocked -> the keyboard paste still works
  syncCastLabel();
});
$("startAtZero").addEventListener("click", () => { castFrom = 0; castCurrentTab(); });
$("startAtHere").addEventListener("click", () => {
  if (pageLive) { castBehind = pageBehind; castFrom = -1; } else { castFrom = pagePos; }
  castCurrentTab();
});
// A cast with a recording behind it is switched onto that recording at a moment; one carrying its
// own timeline is asked for a moment of what it is already playing. Same two points, different verb.
function rewindTo(sec) {
  const at = Math.max(0, Math.floor(sec));
  return castSeekable ? `/rewind?seek=1&t=${at}` : `/rewind?t=${at}`;
}
// Following the page on a replay is not a move within what is playing: the replay is anchored again,
// this time where the page is, and starts over from there.
function rewindFollow() {
  return `/rewind?behind=${Math.max(0, Math.floor(castBehindNow))}`;
}
$("rewindStart").addEventListener("click", async () => {
  try { await call(rewindTo(0)); } catch { notify(t("errNoHelper"), "err"); }
});
$("rewindHere").addEventListener("click", async () => {
  try { await call(castReplay ? rewindFollow() : rewindTo(pagePos)); }
  catch { notify(t("errNoHelper"), "err"); }
});
$("backLive").addEventListener("click", async () => {
  try { await call("/rewind?live=1"); } catch { notify(t("errNoHelper"), "err"); }
});
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
