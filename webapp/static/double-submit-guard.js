/* App-wide guard against double-tap form submits — a slow/laggy request
   (weak connection, a big photo attached, server under load) leaves the
   button tappable for a moment after the first tap, and a second tap
   fires a second identical POST (duplicate repair/sale/client/etc. —
   see 18.08, a repair got submitted twice this way). Disables the
   submit button the instant a form's submit event fires, before the
   request even goes out, so a second tap has nothing to hit.

   Deliberately skips any form whose OWN submit handler already called
   preventDefault() (photo-upload.js, purchase-rows.js's paste/scan
   flows, etc. — those already own their button's disabled state via
   their own request lifecycle; e.defaultPrevented is true by the time
   this document-level listener runs, since it fires after the form's
   own listener in the bubble phase). Only a plain, real form submission
   — one that's about to navigate away or reload the page — gets the
   disable-on-submit treatment here.

   The disabled state is never explicitly re-enabled by this script: a
   real submit either navigates to a new page or the server re-renders
   this one from scratch, both of which naturally reset it. The one
   exception is the browser back/forward cache restoring the exact DOM
   a user disabled right before navigating away (e.g. they went back
   after submitting) — pageshow's persisted flag catches that case. */
(function () {
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    var form = e.target;
    var btn = form.querySelector('button[type="submit"]');
    if (!btn || btn.disabled) return;
    btn.dataset.originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = btn.textContent.trim() + "…";
  });

  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('button[type="submit"][disabled]').forEach(function (btn) {
      btn.disabled = false;
      if (btn.dataset.originalText) btn.textContent = btn.dataset.originalText;
    });
  });
})();
