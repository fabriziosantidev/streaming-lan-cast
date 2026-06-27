// Streaming LAN Cast options: store the per-install token and verify it against the helper.
// CONTROL / TOKEN_HEADER come from constants.js (loaded first in options.html).
const $ = (id) => document.getElementById(id);

async function load() {
  $("token").value = (await browser.storage.local.get("token")).token || "";
}

async function save() {
  const tok = $("token").value.trim();
  let ok = false, rejected = false;
  try {
    const r = await fetch(CONTROL + "/ping", { cache: "no-store", headers: { [TOKEN_HEADER]: tok } });
    ok = r.ok;
    rejected = (r.status === 401 || r.status === 403);
  } catch { ok = false; }
  // Don't clobber a working stored token with one the helper explicitly REJECTED. A network
  // error (helper offline) still saves, so first-time setup works before the helper is running.
  // SECURITY: keep this in storage.local (per-device, origin-isolated). NEVER storage.sync,
  // which would upload the per-install secret to Mozilla's servers and off-device.
  if (!rejected) await browser.storage.local.set({ token: tok });
  const s = $("saved");
  s.textContent = ok ? (browser.i18n.getMessage("saved") || "Saved ✓")
                : rejected ? (browser.i18n.getMessage("savedRejected") || "Token rejected, not saved")
                : (browser.i18n.getMessage("savedNoConn") || "Saved (helper not reachable)");
  s.style.color = ok ? "#16a34a" : "#dc2626";
  s.classList.add("on");
  setTimeout(() => s.classList.remove("on"), 2600);
}

// fill the token field from the clipboard (the token is long and opaque, so a one-click paste helps)
async function pasteToken() {
  try {
    const t = (await navigator.clipboard.readText()).trim();
    if (t) $("token").value = t;
  } catch (e) {
    // clipboard read blocked (older Firefox / missing permission) -> the user can still Ctrl+V
  }
  $("token").focus();
}

// match the OS color scheme for native form controls
document.documentElement.style.colorScheme =
  matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";

localize();
load();
$("save").addEventListener("click", save);
$("paste").addEventListener("click", pasteToken);
$("token").addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
