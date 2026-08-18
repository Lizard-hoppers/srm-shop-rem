/* Scan-to-fill button next to the SKU field on the product create form
   (inventory_products.html) and the product card edit form
   (inventory_product_detail.html) — photo of a barcode/label ->
   POST /inventory/products/scan-label -> auto-fills name + SKU. Wires up
   any button with class "scan-label-btn" carrying data-name-target /
   data-sku-target (element ids to fill) and optionally data-status-target
   (element id for a status line). No price is ever returned by the
   endpoint — see core/vision_ocr.py for why. */
(function () {
  document.querySelectorAll(".scan-label-btn").forEach(function (btn) {
    var nameField = document.getElementById(btn.dataset.nameTarget || "");
    var skuField = document.getElementById(btn.dataset.skuTarget || "");
    var statusEl = btn.dataset.statusTarget ? document.getElementById(btn.dataset.statusTarget) : null;

    var input = document.createElement("input");
    input.type = "file";
    input.accept = "image/jpeg,image/png,image/webp";
    input.capture = "environment";
    input.style.display = "none";
    btn.insertAdjacentElement("afterend", input);

    btn.addEventListener("click", function () { input.click(); });

    input.addEventListener("change", function () {
      var file = input.files[0];
      if (!file) return;
      btn.disabled = true;
      if (statusEl) {
        statusEl.style.display = "block";
        statusEl.textContent = "📷 Распознаю…";
      }

      var formData = new FormData();
      formData.append("photo", file);
      fetch("/inventory/products/scan-label" + window.location.search, {
        method: "POST",
        body: formData,
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!statusEl) return;
          if (data && data.ok) {
            if (data.name && nameField) nameField.value = data.name;
            if (data.sku && skuField) skuField.value = data.sku;
            statusEl.textContent = (data.name || data.sku)
              ? "Готово ✅"
              : "Не распознал ни название, ни код — впишите вручную.";
          } else {
            statusEl.textContent = "⚠️ " + ((data && data.error) || "Не удалось распознать.");
          }
        })
        .catch(function () {
          if (statusEl) statusEl.textContent = "⚠️ Не удалось отправить фото.";
        })
        .finally(function () {
          btn.disabled = false;
          input.value = "";
        });
    });
  });
})();
