/* Checkbox safety net for the product forms (inventory_products.html,
   inventory_product_detail.html) — see below. The scan-to-fill button
   next to the SKU field is now handled by the shared scan-fill.js. */
(function () {
  // Safety net for "Деталь для ремонта" / "Товар на продажу": a <label>
  // that implicitly wraps its checkbox is unreliable on mobile WebKit/
  // Chrome (tap doesn't toggle the box), even though it works fine on
  // desktop. Explicitly toggle on any tap in the label that isn't the
  // checkbox itself — direct taps on the box still use native behavior,
  // so this never double-toggles.
  document.querySelectorAll("label.checkbox-label").forEach(function (label) {
    var checkbox = label.querySelector('input[type="checkbox"]');
    if (!checkbox) return;
    label.addEventListener("click", function (e) {
      if (e.target === checkbox) return;
      e.preventDefault();
      checkbox.checked = !checkbox.checked;
      checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });
})();
