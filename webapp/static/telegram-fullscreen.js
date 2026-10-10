/* The CRM as a full-screen app inside Telegram (10.10) — not a sheet
   that covers part of the chat.

   Loaded in <head>, before anything is painted, on every page (this is a
   server-rendered multi-page site: each navigation is a fresh document,
   so each one asks again — Telegram keeps the mode between pages, and
   asking while already fullscreen is skipped).

   - expand(): the full height of the sheet, on every client.
   - requestFullscreen() (Bot API 8.0+): the whole screen, Telegram's own
     header gone. Phones and tablets only — on desktop it would turn the
     Telegram window itself into an OS-fullscreen one, which nobody asked
     for; there the app simply fills the window it is given.
   - disableVerticalSwipes(): a swipe down scrolls the page, it does not
     close the app in the middle of a form.
   - In fullscreen the page runs under the status bar and Telegram's
     floating «закрыть / ⋯» controls. Telegram reports how much room they
     take (safeAreaInset + contentSafeAreaInset); that goes into
     --tg-top / --tg-bottom, which style.css adds to the header and the
     tab bar. Outside Telegram, or on an old client, both stay 0 and the
     layout is exactly what it was. */
(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg || !tg.initData) return;
  var root = document.documentElement;

  function insets() {
    var safe = tg.safeAreaInset || {}, content = tg.contentSafeAreaInset || {};
    root.style.setProperty("--tg-top", ((safe.top || 0) + (content.top || 0)) + "px");
    root.style.setProperty("--tg-bottom", ((safe.bottom || 0) + (content.bottom || 0)) + "px");
    root.classList.toggle("tg-fullscreen", !!tg.isFullscreen);
  }

  try { tg.ready(); tg.expand(); } catch (e) {}
  try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}

  var modern = tg.isVersionAtLeast && tg.isVersionAtLeast("8.0");
  var handheld = tg.platform === "ios" || tg.platform === "android";
  if (modern) {
    ["safeAreaChanged", "contentSafeAreaChanged", "fullscreenChanged", "viewportChanged"].forEach(function (name) {
      try { tg.onEvent(name, insets); } catch (e) {}
    });
    // Refused (an old shell, a platform that can't): the app stays expanded — nothing to do about it.
    try { tg.onEvent("fullscreenFailed", insets); } catch (e) {}
    if (handheld && tg.requestFullscreen && !tg.isFullscreen) {
      try { tg.requestFullscreen(); } catch (e) {}
    }
  }
  insets();
})();
