/* Telegram's own in-app WebView eats plain <a href="tel:..."> clicks —
   nothing happens on tap, on both Android and iOS (documented Telegram
   bugs: bugs.telegram.org/c/42436 and /c/43416, still open as of 19.08).
   window.open() on the same tel: URL is the known workaround — Telegram
   treats it as an external-navigation attempt and hands it off to the
   OS's phone dialer instead of trying to load it as a page itself.
   Delegated on document so every tel: link works everywhere, including
   ones added by JS after page load, with no per-template changes. */
(function () {
  document.addEventListener("click", function (e) {
    var a = e.target.closest('a[href^="tel:"]');
    if (!a) return;
    e.preventDefault();
    window.open(a.href, "_blank");
  });
})();
