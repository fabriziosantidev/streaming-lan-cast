// Streaming LAN Cast: media sniffer.
// Watches the network for HLS/DASH/direct-video sources so the popup can cast streams from
// sites streamlink can't resolve from the page URL. Captures the request headers the browser
// used (Referer/Origin/User-Agent/Cookie) so the helper can replay them.
// Gated behind the OPTIONAL webRequest + <all_urls> permission (enabled from the popup).

// Headers the sniffer captures to replay. Keep in sync with the helper's _REPLAY_HEADERS
// (helper/streaming-lan-cast-helper.py). That set is the security boundary; this must be a subset.
const WANT = ["referer", "origin", "user-agent", "cookie"];

const perTab = new Map();    // tabId -> Map(url -> {url, type, headers, ts})
const hdrStash = new Map();  // requestId -> {Referer, Origin, User-Agent, Cookie}
let installed = false;

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
      for (const s of arr) m.set(s.url, s);
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
function record(tabId, src) {
  if (tabId < 0) return;
  let m = perTab.get(tabId);
  if (!m) { m = new Map(); perTab.set(tabId, m); }
  m.set(src.url, src);
  while (m.size > 12) m.delete(m.keys().next().value);   // bound memory
  persistTab(tabId);                                     // survive event-page suspension
}

function onBeforeRequest(d) {
  if (d.type === "main_frame") { perTab.delete(d.tabId); persistTab(d.tabId); }   // navigation -> reset this tab
}
function onBeforeSendHeaders(d) {
  const h = pickHeaders(d.requestHeaders);
  if (Object.keys(h).length) {
    hdrStash.set(d.requestId, h);
    while (hdrStash.size > 64) hdrStash.delete(hdrStash.keys().next().value);   // bound: drop stale cookie-bearing entries
  }
}
function onHeadersReceived(d) {
  const type = classify(d.url, ctOf(d.responseHeaders));
  if (type) record(d.tabId, { url: d.url, type, headers: hdrStash.get(d.requestId) || {}, ts: Date.now() });
}
function cleanup(d) { hdrStash.delete(d.requestId); }

function installListeners() {
  if (installed || !browser.webRequest) return;
  const f = { urls: ["<all_urls>"] };
  browser.webRequest.onBeforeRequest.addListener(onBeforeRequest, f);
  browser.webRequest.onBeforeSendHeaders.addListener(onBeforeSendHeaders, f, ["requestHeaders"]);
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
// events whose listeners were added during top-level evaluation -- adding them later from a Promise
// means a navigation after a suspend wouldn't wake the sniffer and sources would be silently missed.
rehydrate();
if (browser.webRequest) installListeners();
browser.permissions.onAdded.addListener(() => { if (browser.webRequest) installListeners(); });
browser.permissions.onRemoved.addListener(() => { if (!browser.webRequest) removeListeners(); });
browser.tabs.onRemoved.addListener((tabId) => { perTab.delete(tabId); persistTab(tabId); });

browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg) return;
  // fail CLOSED: serve ONLY our own privileged extension pages (popup/options). Reject a missing
  // sender, a foreign extension id, and any content-script / tab sender -- this protects the
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
      return { sources: arr };
    })();
  }
});
