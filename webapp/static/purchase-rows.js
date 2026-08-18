/* Shared row-management JS for the goods-receipt intake form
   (purchases_list.html) and the photo-draft review page
   (purchase_draft.html) — same product picker / dynamic rows / paste
   logic on both, driven by a few globals the page sets before including
   this script:
     window.PURCHASE_PRODUCTS       [{id, label}, ...]
     window.PURCHASE_DEFAULT_CELLS  {product_id: cell_id, ...}
     window.PURCHASE_INITIAL_ROWS   optional [{product_id, name_guess, qty, unit_cost}, ...]
       — if set, replaces the server-rendered empty rows with these
       (photo-draft review); if unset, the server-rendered rows stay as-is
       (normal intake form). */
(function () {
  var productPicker = window.PURCHASE_PRODUCTS || [];
  var defaultCellByProduct = window.PURCHASE_DEFAULT_CELLS || {};
  var initialRows = window.PURCHASE_INITIAL_ROWS || null;

  var labelToId = {};
  var idToLabel = {};
  productPicker.forEach(function (p) { labelToId[p.label] = p.id; idToLabel[p.id] = p.label; });

  var datalist = document.getElementById("productList");
  productPicker.forEach(function (p) {
    var opt = document.createElement("option");
    opt.value = p.label;
    datalist.appendChild(opt);
  });

  var rowsContainer = document.getElementById("purchaseRows");
  var rowCountInput = document.getElementById("rowCount");
  var rowTemplate = document.getElementById("rowTemplate");
  var nextIndex = rowsContainer.children.length;

  function resolveRow(searchInput) {
    var row = searchInput.closest(".purchase-row");
    var hiddenId = row.querySelector(".product-id-input");
    var cellSelect = row.querySelector(".cell-select");
    var hint = row.querySelector(".new-product-hint");
    var label = searchInput.value.trim();
    var matchedId = labelToId[label];

    if (matchedId) {
      hiddenId.value = matchedId;
      hint.style.display = "none";
      var defaultCell = defaultCellByProduct[matchedId];
      if (defaultCell && !cellSelect.value) {
        cellSelect.value = String(defaultCell);
      }
    } else {
      hiddenId.value = "";
      hint.style.display = label ? "block" : "none";
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
    nextIndex++;
    rowCountInput.value = nextIndex;
    return rowEl;
  }

  var addRowBtn = document.getElementById("addRowBtn");
  if (addRowBtn) addRowBtn.addEventListener("click", function () { addRow(); });

  function fillRow(rowEl, data) {
    var search = rowEl.querySelector(".product-search");
    search.value = (data.product_id && idToLabel[data.product_id]) || data.name_guess || "";
    if (data.qty != null) rowEl.querySelector('[name^="qty_"]').value = data.qty;
    if (data.unit_cost != null) rowEl.querySelector('[name^="unit_cost_"]').value = data.unit_cost;
    resolveRow(search);
  }

  var parseBtn = document.getElementById("parseInvoiceBtn");
  if (parseBtn) {
    parseBtn.addEventListener("click", function () {
      var text = document.getElementById("invoiceText").value.trim();
      if (!text) return;
      fetch(window.location.pathname + "/parse" + window.location.search, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var parsedRows = (data && data.rows) || [];
          rowsContainer.innerHTML = "";
          nextIndex = 0;
          parsedRows.forEach(function (row) { fillRow(addRow(), row); });
          if (!parsedRows.length) addRow();
        });
    });
  }

  if (initialRows) {
    rowsContainer.innerHTML = "";
    nextIndex = 0;
    initialRows.forEach(function (row) { fillRow(addRow(), row); });
  }
})();
