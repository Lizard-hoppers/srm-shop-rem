/* Row-management JS for the "Принять устройство в ремонт" intake form
   (repairs_list.html) — one client can drop off several devices in the
   same visit, so "+ Добавить ещё устройство" grows the form with no
   cap, same dynamic-row shape as Приход/Продажи (purchase-rows.js,
   sale-rows.js). Each row gets its own brand->model datalist refresh
   (device_type/brand share one static datalist across rows since their
   options don't depend on which row you're in; the model datalist is
   per-row because it's filtered by that row's own brand) and its own
   📷 scan button (POST /repairs/scan-device — the existing single-device
   vision-OCR endpoint, unchanged; only the row-wiring here is new).
   Driven by:
     window.REPAIR_DEVICE_CATALOG   [{device_type, brand, model}, ...]
*/
(function () {
  var catalog = window.REPAIR_DEVICE_CATALOG || [];

  var rowsContainer = document.getElementById("deviceRows");
  var rowCountInput = document.getElementById("deviceCount");
  var rowTemplate = document.getElementById("deviceRowTemplate");
  var nextIndex = rowsContainer.children.length;

  function refreshModels(row) {
    var brandInput = row.querySelector(".device-brand-input");
    var modelList = row.querySelector(".device-model-input").list;
    var brand = brandInput.value.trim().toLowerCase();
    var seen = {};
    modelList.innerHTML = "";
    catalog.forEach(function (entry) {
      if (brand && entry.brand.toLowerCase() !== brand) return;
      if (seen[entry.model]) return;
      seen[entry.model] = true;
      var opt = document.createElement("option");
      opt.value = entry.model;
      modelList.appendChild(opt);
    });
  }

  function wireScanButton(row) {
    var btn = row.querySelector(".device-scan-btn");
    var status = row.querySelector(".device-scan-status");

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
      status.style.display = "block";
      status.textContent = "📷 Распознаю…";

      var formData = new FormData();
      formData.append("photo", file);
      fetch("/repairs/scan-device" + window.location.search, {
        method: "POST",
        body: formData,
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data && data.ok) {
            var filledAny = false;
            if (data.device_type) { row.querySelector(".device-type-input").value = data.device_type; filledAny = true; }
            if (data.brand) { row.querySelector(".device-brand-input").value = data.brand; filledAny = true; }
            if (data.model) { row.querySelector(".device-model-input").value = data.model; filledAny = true; }
            if (data.serial_number) { row.querySelector(".device-serial-input").value = data.serial_number; filledAny = true; }
            refreshModels(row);
            status.textContent = filledAny ? "Готово ✅" : "Не распознал ничего чёткого — впишите вручную.";
          } else {
            status.textContent = "⚠️ " + ((data && data.error) || "Не удалось распознать.");
          }
        })
        .catch(function () {
          status.textContent = "⚠️ Не удалось отправить фото.";
        })
        .finally(function () {
          btn.disabled = false;
          input.value = "";
        });
    });
  }

  function wireRow(row) {
    var brandInput = row.querySelector(".device-brand-input");
    brandInput.addEventListener("input", function () { refreshModels(row); });
    refreshModels(row);
    wireScanButton(row);
  }

  Array.prototype.forEach.call(rowsContainer.children, wireRow);

  document.getElementById("addDeviceRowBtn").addEventListener("click", function () {
    var html = rowTemplate.innerHTML.split("__IDX__").join(String(nextIndex));
    var wrapper = document.createElement("div");
    wrapper.innerHTML = html.trim();
    var rowEl = wrapper.firstElementChild;
    rowsContainer.appendChild(rowEl);
    wireRow(rowEl);
    nextIndex++;
    rowCountInput.value = nextIndex;
  });
})();
