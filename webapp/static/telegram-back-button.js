/* Telegram Mini App's own "Назад" control (23.08) — shows a back arrow
   in Telegram's own header (top-left, next to the app title), a single
   tap navigates back one page. This app is a traditional server-rendered
   multi-page site (every link is a real navigation, no client router),
   so "back" is just the browser's own history — history.back() is
   exactly right here, same page the phone's own back gesture would land
   on if Telegram let it through.

   Hidden on the dashboard ("/") — that's home, nothing to go back to
   from there. Shown on every other page, regardless of login state (the
   anonymous /miniapp boot page just no-ops on tap if there's nothing to
   go back to, harmless). */
(function () {
  if (!window.Telegram || !window.Telegram.WebApp || !window.Telegram.WebApp.BackButton) return;
  var tg = window.Telegram.WebApp;

  if (window.location.pathname === "/") {
    tg.BackButton.hide();
    return;
  }

  tg.BackButton.onClick(function () {
    history.back();
  });
  tg.BackButton.show();
})();
