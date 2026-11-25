// Streaming LAN Cast options: store the per-install token and verify it against the helper.
// CONTROL / TOKEN_HEADER come from constants.js (loaded first in options.html).
const $ = (id) => document.getElementById(id);

async function load() {
  const st = await browser.storage.local.get(["token", "castMeta", "castOthers", "castAwake"]);
  $("token").value = st.token || "";
  $("castMeta").checked = !!st.castMeta;
  $("castOthers").checked = !!st.castOthers;
  $("castAwake").checked = st.castAwake !== false;    // on until turned off here
  loadAutoUpdate();
}

// The update setting lives in the helper, which acts on it without the extension: the box shows
// what the helper reports, and a change is sent to it (and kept here, so the popup can resend it
// to a helper that was not running when it changed). It starts off, and until it is answered the
// token step asks about it.
const AUTO_OFF_MSG = { env: "optAutoUpdOffEnv", source: "optAutoUpdOffSource",
                       readonly: "optAutoUpdOffReadonly", platform: "optAutoUpdOffPlatform" };
async function loadAutoUpdate() {
  const st = await browser.storage.local.get(["token", "autoUpdate"]);
  $("autoUpdate").checked = st.autoUpdate === true;
  $("updAsk").hidden = typeof st.autoUpdate === "boolean";
  try {
    const r = await fetch(CONTROL + "/ping", { cache: "no-store", headers: { [TOKEN_HEADER]: st.token || "" } });
    if (!r.ok) return;
    const ping = await r.json();
    if (typeof st.autoUpdate !== "boolean" && typeof ping.auto_update === "boolean") $("autoUpdate").checked = ping.auto_update;
    const why = ping.auto_update_off && AUTO_OFF_MSG[ping.auto_update_off];
    $("autoUpdOff").textContent = why ? (browser.i18n.getMessage(why) || "") : "";
    $("autoUpdOff").hidden = !why;
  } catch {}
}
async function saveAutoUpdate() {
  const on = $("autoUpdate").checked;
  await browser.storage.local.set({ autoUpdate: on });
  const { token } = await browser.storage.local.get("token");
  try {
    await fetch(CONTROL + "/settings", {
      method: "POST", cache: "no-store", body: "auto_update=" + (on ? "1" : "0"),
      headers: { [TOKEN_HEADER]: token || "", "Content-Type": "application/x-www-form-urlencoded" },
    });
  } catch {}
}
$("autoUpdate").addEventListener("change", saveAutoUpdate);
for (const [id, on] of [["updYes", true], ["updNo", false]]) {
  $(id).addEventListener("click", () => {
    $("autoUpdate").checked = on;
    $("updAsk").hidden = true;
    saveAutoUpdate();
  });
}
// Both are off until turned on here: a cast shows nothing about itself to the other devices on
// the network and takes no commands from them.
for (const id of ["castMeta", "castOthers", "castAwake"]) {
  $(id).addEventListener("change", () => browser.storage.local.set({ [id]: $(id).checked }));
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
  // which would upload the per-install secret to the browser vendor's servers and off-device.
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
    // clipboard read blocked (older browser / missing permission) -> the user can still Ctrl+V
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
