// Shared constants for Streaming LAN Cast. Loaded before popup.js / options.js (via a
// <script> tag in popup.html / options.html), so each value is defined in exactly one place.
const CONTROL = "http://127.0.0.1:9988";                                       // loopback control server
const TOKEN_HEADER = "X-LanCast-Token";                                        // auth header (must match the helper)
const DETECT_PERM = { permissions: ["webRequest"], origins: ["<all_urls>"] };  // optional source-detection grant
const SITE_URL = "https://fabriziosantidev.github.io/streaming-lan-cast/";      // download / install help page

// ---- i18n: fill textContent from [data-i18n] and the title attribute from [data-i18n-title] ----
function localize() {
  for (const el of document.querySelectorAll("[data-i18n]")) {
    const m = browser.i18n.getMessage(el.dataset.i18n);
    if (m) el.textContent = m;
  }
  for (const el of document.querySelectorAll("[data-i18n-title]")) {
    const m = browser.i18n.getMessage(el.dataset.i18nTitle);
    if (m) el.title = m;
  }
}
