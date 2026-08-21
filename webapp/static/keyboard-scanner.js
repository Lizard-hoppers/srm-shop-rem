/* Detects a hardware Bluetooth/USB "keyboard-wedge" barcode scanner's
   input globally — no field needs to be focused/clicked into first
   (19.08, originally built for Продажи, reused on Склад). A scanner
   types its whole code as real keystrokes in a few milliseconds and
   finishes with Enter or Tab; a human never sustains that pace across a
   multi-character burst, so timing alone tells the two apart without
   the cashier doing anything to "arm" scanning first.

   Any page that includes this script (before its own script) gets
   window.onBarcodeScan(code) called with each detected scan — set that
   function up before this fires, or just define it further down; both
   work since scans only happen on a later user action. */
(function () {
  var MAX_GAP_MS = 30;
  var MIN_LEN = 4;
  var buffer = "";
  var lastKeyAt = 0;

  document.addEventListener("keydown", function (e) {
    var now = Date.now();

    if (e.key === "Enter" || e.key === "Tab") {
      var code = buffer.trim();
      var wasFast = now - lastKeyAt <= MAX_GAP_MS;
      buffer = "";
      lastKeyAt = now;
      if (wasFast && code.length >= MIN_LEN && typeof window.onBarcodeScan === "function") {
        e.preventDefault();
        window.onBarcodeScan(code);
      }
      return;
    }

    if (now - lastKeyAt > MAX_GAP_MS) buffer = ""; // too slow — a human, not a scanner
    lastKeyAt = now;
    if (e.key.length === 1) buffer += e.key;
  });
})();
