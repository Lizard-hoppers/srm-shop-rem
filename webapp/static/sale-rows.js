/* Row-management JS for the "Новая продажа" checkout form
   (sales_list.html) — dynamic rows (no fixed cap, unlike the old
   hardcoded 3-row form), product search by typing OR by scanning a
   barcode/label (same OpenAI-vision endpoint the SKU scanner on the
   product forms uses), same product-picker/default-value pattern as
   purchase-rows.js on Приход. Driven by globals the page sets before
   including this script:
     window.SALE_PRODUCTS                  [{id, label}, ...]
     window.SALE_DEFAULT_PRICE_BY_PRODUCT   {product_id: price, ...}

   Unlike Приход, a row that doesn't resolve to a real product is NOT
   silently turned into "will create a new product" — a sale can only
   move stock that already exists, so an unresolved row is flagged and
   the server rejects the submission with a friendly error rather than
   guessing. */
(function () {
  var productPicker = window.SALE_PRODUCTS || [];
  var defaultPriceByProduct = window.SALE_DEFAULT_PRICE_BY_PRODUCT || {};

  var labelToId = {};
  var idToLabel = {};
  var skuToId = {};
  productPicker.forEach(function (p) {
    labelToId[p.label] = p.id;
    idToLabel[p.id] = p.label;
    var skuMatch = /\(([^)]+)\)\s*$/.exec(p.label);
    if (skuMatch) skuToId[skuMatch[1]] = p.id;
  });

  var datalist = document.getElementById("saleProductList");
  productPicker.forEach(function (p) {
    var opt = document.createElement("option");
    opt.value = p.label;
    datalist.appendChild(opt);
  });

  var rowsContainer = document.getElementById("saleRows");
  var rowCountInput = document.getElementById("saleRowCount");
  var rowTemplate = document.getElementById("saleRowTemplate");
  var nextIndex = rowsContainer.children.length;

  function resolveRow(searchInput) {
    var row = searchInput.closest(".sale-row");
    var hiddenId = row.querySelector(".product-id-input");
    var priceInput = row.querySelector('[name^="price_"]');
    var hint = row.querySelector(".sale-row-hint");
    var label = searchInput.value.trim();
    var matchedId = labelToId[label];

    if (matchedId) {
      hiddenId.value = matchedId;
      hint.style.display = "none";
      var defaultPrice = defaultPriceByProduct[matchedId];
      if (defaultPrice != null && !priceInput.value) {
        priceInput.value = defaultPrice;
      }
    } else {
      hiddenId.value = "";
      hint.style.display = label ? "block" : "none";
      hint.textContent = "⚠️ нет в каталоге — выберите товар из подсказок";
    }
  }

  rowsContainer.addEventListener("input", function (e) {
    if (e.target.classList.contains("product-search")) {
      resolveRow(e.target);
    }
  });

  function addRow() {
    var html = rowTemplate.innerHTML.split("__IDX__").join(String(nextIndex));
    var wrapper = document.createElement("div");
    wrapper.innerHTML = html.trim();
    var rowEl = wrapper.firstElementChild;
    rowsContainer.appendChild(rowEl);
    wireScanButton(rowEl);
    nextIndex++;
    rowCountInput.value = nextIndex;
    return rowEl;
  }

  document.getElementById("addSaleRowBtn").addEventListener("click", function () { addRow(); });

  function applyScanResult(rowEl, data) {
    var search = rowEl.querySelector(".product-search");
    var hint = rowEl.querySelector(".sale-row-hint");
    var matchedId = (data.sku && skuToId[data.sku]) || null;

    if (!matchedId && data.name) {
      var needle = data.name.trim().toLowerCase();
      var candidates = productPicker.filter(function (p) {
        return p.label.toLowerCase().indexOf(needle) !== -1;
      });
      if (candidates.length === 1) matchedId = candidates[0].id;
    }

    if (matchedId) {
      search.value = idToLabel[matchedId];
      resolveRow(search);
    } else if (data.name) {
      search.value = data.name;
      resolveRow(search); // will show the "not in catalog" hint
    } else {
      hint.style.display = "block";
      hint.textContent = "⚠️ Не удалось распознать — впишите вручную.";
    }
  }

  function wireScanButton(rowEl) {
    var btn = rowEl.querySelector(".sale-scan-btn");
    var hint = rowEl.querySelector(".sale-row-hint");

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
      hint.style.display = "block";
      hint.textContent = "📷 Распознаю…";

      var formData = new FormData();
      formData.append("photo", file);
      fetch("/inventory/products/scan-label" + window.location.search, {
        method: "POST",
        body: formData,
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data && data.ok) {
            applyScanResult(rowEl, data);
          } else {
            hint.style.display = "block";
            hint.textContent = "⚠️ " + ((data && data.error) || "Не удалось распознать.");
          }
        })
        .catch(function () {
          hint.style.display = "block";
          hint.textContent = "⚠️ Не удалось отправить фото.";
        })
        .finally(function () {
          btn.disabled = false;
          input.value = "";
        });
    });
  }

  Array.prototype.forEach.call(rowsContainer.children, wireScanButton);

  /* Bluetooth/USB "keyboard-wedge" barcode scanner (19.08) — no click
     into any field needed first. Detection itself (telling a scanner's
     burst-typed input apart from a human by keystroke timing) lives in
     the shared keyboard-scanner.js, included before this file; it calls
     window.onBarcodeScan(code) for anything it recognizes as a scan. */
  var toastEl = null;
  var toastTimer = null;
  function showScanToast(text, isWarning) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.style.cssText =
        "position:fixed; left:50%; bottom:90px; transform:translateX(-50%); z-index:50;" +
        "padding:10px 18px; border-radius:10px; font-size:13.5px; font-weight:600;" +
        "box-shadow:var(--shadow-lg); transition:opacity 0.2s ease; color:#fff;";
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.style.background = isWarning ? "#b45309" : "#16a34a";
    toastEl.style.opacity = "1";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.style.opacity = "0"; }, 1600);
  }

  function findRowByProductId(id) {
    var rows = rowsContainer.querySelectorAll(".sale-row");
    for (var i = 0; i < rows.length; i++) {
      var hidden = rows[i].querySelector(".product-id-input");
      if (hidden && hidden.value === String(id)) return rows[i];
    }
    return null;
  }

  function findFirstEmptyRow() {
    var rows = rowsContainer.querySelectorAll(".sale-row");
    for (var i = 0; i < rows.length; i++) {
      var search = rows[i].querySelector(".product-search");
      if (search && !search.value.trim()) return rows[i];
    }
    return null;
  }

  function flashRow(rowEl) {
    rowEl.style.transition = "background 0.4s ease";
    rowEl.style.background = "color-mix(in srgb, var(--accent) 22%, transparent)";
    setTimeout(function () { rowEl.style.background = ""; }, 600);
  }

  function handleBarcodeScan(code) {
    var productId = skuToId[code];
    if (!productId) {
      showScanToast("⚠️ Штрих-код не найден: " + code, true);
      return;
    }

    var existingRow = findRowByProductId(productId);
    if (existingRow) {
      var qtyInput = existingRow.querySelector('[name^="qty_"]');
      qtyInput.value = (parseInt(qtyInput.value, 10) || 0) + 1;
      flashRow(existingRow);
      showScanToast("✅ " + idToLabel[productId] + " ×" + qtyInput.value);
      return;
    }

    var rowEl = findFirstEmptyRow() || addRow();
    var search = rowEl.querySelector(".product-search");
    search.value = idToLabel[productId];
    resolveRow(search);
    var newQtyInput = rowEl.querySelector('[name^="qty_"]');
    if (!newQtyInput.value) newQtyInput.value = 1;
    flashRow(rowEl);
    showScanToast("✅ " + idToLabel[productId] + " ×1");
  }

  window.onBarcodeScan = handleBarcodeScan;
})();
