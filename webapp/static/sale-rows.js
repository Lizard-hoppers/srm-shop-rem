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
})();
