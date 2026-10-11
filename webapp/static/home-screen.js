/* «На главный экран» (10.10) — the card on «Ещё» that puts the CRM's icon
   on the phone's home screen, so it opens like any other app.

   Telegram does the adding (WebApp.addToHomeScreen, Bot API 8.0+); the
   card only asks for it. It stays hidden where that can't work — outside
   Telegram, on an old client, on a platform that says «unsupported» —
   and once the icon is already there, so nobody is offered a button that
   does nothing. */
(function () {
  var card = document.getElementById("homeScreenCard");
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!card || !tg || !tg.initData || !tg.addToHomeScreen || !(tg.isVersionAtLeast && tg.isVersionAtLeast("8.0"))) return;
  var sub = document.getElementById("homeScreenSub");

  function show(status) {
    card.style.display = (status === "unsupported" || status === "added") ? "none" : "";
  }
  try { tg.onEvent("homeScreenAdded", function () { if (sub) sub.textContent = card.getAttribute("data-done"); }); } catch (e) {}
  // No answer from an older shell: offer the card anyway — a tap then simply does what it can.
  try { tg.checkHomeScreenStatus(show); } catch (e) { show("unknown"); }

  card.addEventListener("click", function (event) {
    event.preventDefault();
    try { tg.addToHomeScreen(); } catch (e) {}
  });
})();
