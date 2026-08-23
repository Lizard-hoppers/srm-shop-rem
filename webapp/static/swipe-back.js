/* Edge-swipe-to-go-back (23.08) — a finger swipe starting near the left
   edge of the screen and moving right navigates back one page
   (history.back()), the familiar iOS/Android "swipe back" gesture —
   Павел asked for this specifically, holding Telegram's own back
   control felt slow to reach for every step back.

   Deliberately narrow to avoid false triggers: only fires for a touch
   that (a) STARTS within EDGE_ZONE px of the left edge — anywhere else
   on the page is a normal tap/scroll/drag and must never trigger this;
   (b) ends up moving right by at least SWIPE_THRESHOLD px; (c) is
   clearly horizontal, not a vertical scroll or a diagonal drag. Passive
   listeners, no preventDefault anywhere — never interferes with normal
   scrolling (including .table-scroll's own horizontal scroll, which
   starts well past the edge zone) or tapping elsewhere on the page. */
(function () {
  var EDGE_ZONE = 24;
  var SWIPE_THRESHOLD = 70;
  var startX = null;
  var startY = null;
  var tracking = false;

  document.addEventListener(
    "touchstart",
    function (e) {
      var t = e.touches[0];
      if (t.clientX <= EDGE_ZONE) {
        startX = t.clientX;
        startY = t.clientY;
        tracking = true;
      } else {
        tracking = false;
      }
    },
    { passive: true }
  );

  document.addEventListener(
    "touchend",
    function (e) {
      if (!tracking || startX === null) return;
      tracking = false;
      var t = e.changedTouches[0];
      var dx = t.clientX - startX;
      var dy = t.clientY - startY;
      if (dx > SWIPE_THRESHOLD && Math.abs(dx) > Math.abs(dy) * 2) {
        history.back();
      }
    },
    { passive: true }
  );
})();
