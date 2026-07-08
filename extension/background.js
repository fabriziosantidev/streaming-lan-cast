// Streaming LAN Cast: media sniffer.
// Watches the network for HLS/DASH/direct-video sources so the popup can cast streams from
// sites streamlink can't resolve from the page URL. Captures the request headers the browser
// used (Referer/Origin/User-Agent/Cookie) so the helper can replay them.
// Gated behind the OPTIONAL webRequest + <all_urls> permission (enabled from the popup).

// In Chrome the background is a service worker (no <script> tags), so the polyfill is loaded here
// for browser.* and promise-returning onMessage. In Firefox it is an event page and the polyfill is
// already loaded via the manifest's background.scripts, so importScripts is absent and this is skipped.
if (typeof importScripts === "function") importScripts("browser-polyfill.js");

// Headers the sniffer captures to replay. Keep in sync with the helper's _REPLAY_HEADERS
// (helper/streaming-lan-cast-helper.py). That set is the security boundary; this must be a subset.
const WANT = ["referer", "origin", "user-agent", "cookie"];

const perTab = new Map();    // tabId -> Map(url -> {url, type, headers, ts})
const hdrStash = new Map();  // requestId -> {Referer, Origin, User-Agent, Cookie}
const dirTab = new Map();    // stream directory -> owning tabId, learned from tab-attributed requests
const orphanSrc = [];        // sources seen with no tabId (Worker context), held until their tab is known
let installed = false;
let activeTabId = -1;        // the focused tab; quality precompute is limited to it (avoid scanning background tabs)

// The popup computes the quality list on open (read the page's inlined ladder + ask the helper). To make
// it appear instantly (like the already-warm device list) precompute it here in the background as the
// user watches, and hand the popup the cached answer. Keyed by tab; dropped on navigation / tab close.
const qualCache = new Map(); // tabId -> {qualities, matrix, ts}
const qualTimer = new Map(); // tabId -> debounce timer id
const qualLast = new Map();  // tabId -> last precompute time (rate-limit a busy live stream)
// Loopback control server + auth header (mirrors constants.js, which the popup loads; keep in sync).
const HELPER_CTRL = "http://127.0.0.1:9988";
const HELPER_TOKEN_HEADER = "X-LanCast-Token";

// The MV3 background is a non-persistent event page: it's suspended when idle, dropping perTab.
// Mirror detected sources into storage.session so the popup can still read them after a suspend
// (and a cast doesn't silently replay without the captured Cookie). storage.session is in-memory,
// extension-scoped, survives event-page suspension, and is cleared on browser restart.
async function persistTab(tabId) {
  try {
    const cur = (await browser.storage.session.get("sniffed")).sniffed || {};
    const m = perTab.get(tabId);
    if (m && m.size) cur[tabId] = [...m.values()];
    else delete cur[tabId];
    await browser.storage.session.set({ sniffed: cur });
  } catch {}
}
async function rehydrate() {
  try {
    const sniffed = (await browser.storage.session.get("sniffed")).sniffed || {};
    for (const [tabId, arr] of Object.entries(sniffed)) {
      const m = new Map();
      for (const s of arr) m.set(s.url.split("?")[0], s);   // same query-less key as record()
      perTab.set(Number(tabId), m);
    }
  } catch {}
}

function pickHeaders(list) {
  const h = {};
  for (const x of (list || [])) {
    if (WANT.includes(x.name.toLowerCase())) h[x.name] = x.value;
  }
  return h;
}
function ctOf(headers) {
  for (const h of (headers || [])) if (h.name.toLowerCase() === "content-type") return h.value || "";
  return "";
}
// classify ONLY manifests + standalone files; segments (.ts/.m4s, video/mp2t) are deliberately ignored.
function classify(url, ct) {
  const u = (url || "").toLowerCase();
  ct = (ct || "").toLowerCase();
  if (/\.m3u8(\?|#|$)/.test(u) || ct.includes("mpegurl")) return "hls";
  if (/\.mpd(\?|#|$)/.test(u) || ct.includes("dash+xml")) return "dash";
  if (/\.(mp4|webm)(\?|#|$)/.test(u) && ct.startsWith("video/")) return "file";
  return null;
}
// Segment-level playlists whose filenames carry a rolling stream/segment id (live low-latency chunklists).
// A running stream keeps minting new ones, so their path-keys accumulate; without protecting the master
// they would push the one-shot multi-quality master out of the bounded buffer before the popup reads it.
// Both patterns match the FILENAME only (not the whole path), so a master under a numbered/track-named
// directory isn't mistaken for a chunklist; a conventional master filename is never an eviction victim.
const MASTER_NAME_RE = /(^|[_-])(master|index|playlist|manifest)\.m3u8$/i;
const SEGMENT_PLAYLIST_RE = /^chunklist|(^|[_-])(seg|segment|frag|part)[_-]?\d|[_-]\d+[_-](video|audio)[_-]/i;
function baseName(k) { return k.split("/").pop() || k; }
function record(tabId, src) {
  if (tabId < 0) return;
  let m = perTab.get(tabId);
  if (!m) { m = new Map(); perTab.set(tabId, m); }
  // Key by URL without its query string: a low-latency playlist is re-fetched constantly with a changing
  // ?_HLS_msn=... , and keying by the full URL would let those refills evict the one-shot master playlist
  // (which the helper needs to read the stream's qualities and audio).
  const key = src.url.split("?")[0];
  const fresh = !m.has(key);                             // a genuinely new playlist, not a chunklist refresh
  m.set(key, src);
  while (m.size > 40) {                                  // bound memory
    let victim = null;
    for (const k of m.keys()) {                          // 1st choice: oldest rolling chunklist that isn't a master
      const b = baseName(k);
      if (!MASTER_NAME_RE.test(b) && SEGMENT_PLAYLIST_RE.test(b)) { victim = k; break; }
    }
    if (victim == null) for (const k of m.keys()) {      // else oldest non-master, so a master survives
      if (!MASTER_NAME_RE.test(baseName(k))) { victim = k; break; }
    }
    m.delete(victim != null ? victim : m.keys().next().value);   // all masters -> drop oldest
  }
  persistTab(tabId);                                     // survive event-page suspension
  // A newly-seen HLS playlist on the focused tab -> warm the quality list so the popup opens instantly.
  // A running live stream re-fetches its chunklists under the same query-less key (not "fresh"), so it
  // doesn't re-trigger this; only a genuinely new playlist (the master, a new rendition) does.
  if (fresh && src.type === "hls" && tabId === activeTabId) scheduleQualPrecompute(tabId);
}

function pickHeaderCI(headers, name) {   // sniffed headers keep original casing; match case-insensitively
  if (!headers) return "";
  const ln = name.toLowerCase();
  for (const k in headers) if (k.toLowerCase() === ln) return headers[k];
  return "";
}

// Read the page's inlined {quality,url} rendition list from its MAIN world (the isolated world can't see
// inline <script> globals). Mirrors popup.js readPageLadder. Keep the injected scan in sync with it.
async function readPageLadderBg(tabId) {
  if (!(browser.scripting && browser.scripting.executeScript)) return { ladder: {} };
  try {
    const res = await browser.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async () => {
        const out = {};
        const push = (q, u) => {
          const h = parseInt(q, 10);
          if (h >= 100 && h <= 4320 && /\.m3u8/.test(u) && out[h] === undefined) out[h] = String(u);
        };
        try {
          const html = document.documentElement.outerHTML;
          const urlRe = /"(?:videoUrl|url|src|file|manifest)"\s*:\s*"([^"]*\.m3u8[^"]*)"/g;
          let m;
          while ((m = urlRe.exec(html))) {
            let a = m.index; const lo = Math.max(0, m.index - 2000);
            while (a > lo && html[a] !== "{") a--;
            let b = m.index; const hi = Math.min(html.length, m.index + 2000);
            while (b < hi && html[b] !== "}") b++;
            const o = html.slice(a, b + 1);
            const q = o.match(/"(?:quality|label|res|height)"\s*:\s*"?(\d{3,4})p?"?/);
            if (q) { try { push(q[1], JSON.parse('"' + m[1] + '"')); } catch { push(q[1], m[1].replace(/\\\//g, "/")); } }
          }
          // remote list endpoint (.../media/hls/?s=...) -> fetch it in-page (same-origin, the browser sends
          // the cookie itself, so the helper never handles it). Only when nothing was inlined.
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
              } catch (e) {}
            }
          }
        } catch (e) {}
        return { out };
      },
    });
    const r = (res && res[0] && res[0].result) || {};
    return { ladder: r.out || {} };
  } catch (e) { return { ladder: {} }; }
}

function scheduleQualPrecompute(tabId) {
  clearTimeout(qualTimer.get(tabId));
  qualTimer.set(tabId, setTimeout(() => precomputeQualities(tabId), 800));   // coalesce the startup burst
}

// Compute the quality list the popup would, and cache it. Same inputs as popup.loadQualities: the sniffed
// HLS sources' replay headers + the page's inlined ladder, POSTed to the helper (which short-circuits on
// the ladder when present). The Cookie never rides in a URL; it goes in the POST body via the page headers.
async function precomputeQualities(tabId) {
  const last = qualLast.get(tabId) || 0;
  if (Date.now() - last < 5000) { scheduleQualPrecompute(tabId); return; }   // rate-limit; retry later
  qualLast.set(tabId, Date.now());
  try {
    const m = perTab.get(tabId);
    const srcs = m ? [...m.values()].filter(s => s.type === "hls") : [];
    if (!srcs.length) return;
    const hs = {};   // Referer/Origin/UA the host needs to serve the master (never the Cookie, in a URL)
    for (const k of ["Referer", "Origin", "User-Agent"]) { const v = pickHeaderCI(srcs[0].headers, k); if (v) hs[k] = v; }
    const lr = await readPageLadderBg(tabId);
    const body = "url=" + encodeURIComponent(srcs[0].url)
      + "&urls=" + encodeURIComponent(JSON.stringify(srcs.map(s => s.url)))
      + "&h=" + encodeURIComponent(JSON.stringify(hs))
      + "&ladder=" + encodeURIComponent(JSON.stringify(lr.ladder || {}));
    let token = "";
    try { token = (await browser.storage.local.get("token")).token || ""; } catch {}
    const headers = { "Content-Type": "application/x-www-form-urlencoded" };
    if (token) headers[HELPER_TOKEN_HEADER] = token;
    const r = await fetch(HELPER_CTRL + "/qualities", { method: "POST", cache: "no-store", headers, body });
    const j = await r.json().catch(() => null);
    if (j && j.ok) qualCache.set(tabId, { qualities: j.qualities || [], matrix: j.matrix || [], ts: Date.now() });
  } catch {}
}

function clearTabDirs(tabId) {   // drop this tab's stream-directory ownerships so they can't outlive its stream
  for (const [dir, t] of dirTab) if (t === tabId) dirTab.delete(dir);
}
function onBeforeRequest(d) {
  if (d.type === "main_frame") {                         // navigation -> reset this tab
    perTab.delete(d.tabId); clearTabDirs(d.tabId); qualCache.delete(d.tabId);
    clearTimeout(qualTimer.get(d.tabId)); qualTimer.delete(d.tabId);
    persistTab(d.tabId);
  }
}
function onBeforeSendHeaders(d) {
  const h = pickHeaders(d.requestHeaders);
  if (Object.keys(h).length) {
    hdrStash.set(d.requestId, h);
    while (hdrStash.size > 64) hdrStash.delete(hdrStash.keys().next().value);   // bound: drop stale cookie-bearing entries
  }
}
// The stream directory (host + path without the filename) is shared by a master and its own chunklists
// but is unique per stream, so it attributes a no-tab source to the exact tab playing that stream.
function dirOf(u) { try { const x = new URL(u); return x.host + x.pathname.replace(/\/[^/]*$/, ""); } catch { return ""; } }
// A player can fetch its one-shot MASTER playlist from a Web Worker, which webRequest reports with no
// tabId (-1); the continuous chunklists come from the page with a real tabId. Attribute a no-tab source
// to the tab that owns the same stream directory, holding it until a tab-attributed request reveals that
// tab (the master usually arrives first). Matching on the directory (not the host) keeps concurrent
// streams on a shared CDN host from cross-wiring, which would cast a different stream than the one chosen.
function attachOrphans(dir, tabId) {
  for (let i = orphanSrc.length - 1; i >= 0; i--) {
    if (orphanSrc[i].dir === dir) { record(tabId, orphanSrc[i].src); orphanSrc.splice(i, 1); }
  }
}
function onHeadersReceived(d) {
  const type = classify(d.url, ctOf(d.responseHeaders));
  if (!type) return;
  const src = { url: d.url, type, headers: hdrStash.get(d.requestId) || {}, ts: Date.now() };
  const dir = dirOf(d.url);
  if (d.tabId >= 0) {
    if (dir) {
      dirTab.set(dir, d.tabId); attachOrphans(dir, d.tabId);
      while (dirTab.size > 128) dirTab.delete(dirTab.keys().next().value);   // bound (also cleared on nav/close)
    }
    record(d.tabId, src);
  } else if (dir && dirTab.has(dir)) {
    record(dirTab.get(dir), src);
  } else if (dir) {
    orphanSrc.push({ dir, src });
    while (orphanSrc.length > 32) orphanSrc.shift();
  }
}
function cleanup(d) { hdrStash.delete(d.requestId); }

function installListeners() {
  if (installed || !browser.webRequest) return;
  const f = { urls: ["<all_urls>"] };
  // Chrome hides Cookie/Referer/Origin/User-Agent from webRequest unless "extraHeaders" is requested;
  // Firefox exposes them with "requestHeaders" alone and has no such option. Add it only where it
  // exists so the same code captures the replay headers in both browsers.
  const reqSpec = ["requestHeaders"];
  const obsh = browser.webRequest.OnBeforeSendHeadersOptions;
  if (obsh && obsh.EXTRA_HEADERS) reqSpec.push("extraHeaders");
  browser.webRequest.onBeforeRequest.addListener(onBeforeRequest, f);
  browser.webRequest.onBeforeSendHeaders.addListener(onBeforeSendHeaders, f, reqSpec);
  browser.webRequest.onHeadersReceived.addListener(onHeadersReceived, f, ["responseHeaders"]);
  browser.webRequest.onCompleted.addListener(cleanup, f);
  browser.webRequest.onErrorOccurred.addListener(cleanup, f);
  installed = true;
}
function removeListeners() {
  if (!installed) return;
  try {
    browser.webRequest.onBeforeRequest.removeListener(onBeforeRequest);
    browser.webRequest.onBeforeSendHeaders.removeListener(onBeforeSendHeaders);
    browser.webRequest.onHeadersReceived.removeListener(onHeadersReceived);
    browser.webRequest.onCompleted.removeListener(cleanup);
    browser.webRequest.onErrorOccurred.removeListener(cleanup);
  } catch {}   // browser.webRequest may already be gone once the permission is revoked
  installed = false;
}

// Register webRequest listeners SYNCHRONOUSLY at top-level eval (browser.webRequest exists iff the
// optional "webRequest" permission is granted). A non-persistent MV3 event page is only woken for
// events whose listeners were added during top-level evaluation. Adding them later from a Promise
// means a navigation after a suspend wouldn't wake the sniffer and sources would be silently missed.
rehydrate();
if (browser.webRequest) installListeners();
browser.permissions.onAdded.addListener(() => { if (browser.webRequest) installListeners(); });
browser.permissions.onRemoved.addListener(() => { if (!browser.webRequest) removeListeners(); });
browser.tabs.onRemoved.addListener((tabId) => {
  perTab.delete(tabId); clearTabDirs(tabId);
  qualCache.delete(tabId); clearTimeout(qualTimer.get(tabId)); qualTimer.delete(tabId); qualLast.delete(tabId);
  persistTab(tabId);
});
// Track the focused tab so quality precompute only ever scans the page the user is actually on.
browser.tabs.onActivated.addListener((info) => { activeTabId = info.tabId; });
if (browser.windows && browser.windows.onFocusChanged) {
  browser.windows.onFocusChanged.addListener((wid) => {
    if (wid < 0) return;   // all windows blurred
    browser.tabs.query({ active: true, windowId: wid }).then((t) => { if (t[0]) activeTabId = t[0].id; }).catch(() => {});
  });
}
browser.tabs.query({ active: true, currentWindow: true }).then((t) => { if (t[0]) activeTabId = t[0].id; }).catch(() => {});

browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg) return;
  // fail CLOSED: serve ONLY our own privileged extension pages (popup/options). Reject a missing
  // sender, a foreign extension id, and any content-script / tab sender. This protects the
  // captured Cookie/header data from being read by a web page or another extension.
  if (!sender || sender.id !== browser.runtime.id || sender.tab) return;
  if (msg.cmd === "getDetected") {
    return (async () => {
      const m = perTab.get(msg.tabId);
      let arr = m ? [...m.values()] : [];
      if (!arr.length) {
        // the event page may have been suspended since the sniff -> read the persisted copy
        try {
          const sniffed = (await browser.storage.session.get("sniffed")).sniffed || {};
          arr = sniffed[msg.tabId] || [];
        } catch {}
      }
      const rank = (t) => (t === "hls" ? 0 : t === "dash" ? 1 : 2);
      arr = [...arr].sort((a, b) => rank(a.type) - rank(b.type) || b.ts - a.ts);
      return { sources: arr, qualities: qualCache.get(msg.tabId) || null };
    })();
  }
});
