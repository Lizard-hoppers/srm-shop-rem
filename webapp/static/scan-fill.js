/* Generic scan-to-fill button: take/pick a photo, POST it to a vision-OCR
   endpoint, map the JSON response onto form fields. Shared by the SKU
   scanner (inventory_products.html, inventory_product_detail.html) and
   the device-info scanner on repair intake (repairs_list.html) — same
   shape both places, just different endpoint + fields.

   Wires up any button with class "scan-fill-btn" carrying:
     data-scan-endpoint   the POST endpoint (photo in, JSON out)
     data-fields          JSON map of {"response_key": "target_element_id"}
     data-status-target   optional element id for a status line
   A filled field fires an "input" event too, so any datalist-refresh
   logic listening on that field (e.g. brand -> model suggestions) still
   runs. No price/cost field is ever wired here — see core/vision_ocr.py
   for why. */
(function () {
  document.querySelectorAll(".scan-fill-btn").forEach(function (btn) {
    var endpoint = btn.dataset.scanEndpoint;
    var fields = JSON.parse(btn.dataset.fields || "{}");
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
      fetch(endpoint + window.location.search, {
        method: "POST",
        body: formData,
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!statusEl) return;
          if (data && data.ok) {
            var filledAny = false;
            Object.keys(fields).forEach(function (key) {
              var el = document.getElementById(fields[key]);
              if (el && data[key]) {
                el.value = data[key];
                el.dispatchEvent(new Event("input", { bubbles: true }));
                filledAny = true;
              }
            });
            statusEl.textContent = filledAny
              ? "Готово ✅"
              : "Не распознал ничего чёткого — впишите вручную.";
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
