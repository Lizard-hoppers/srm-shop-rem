/* Generic AJAX photo upload with a status line, shared by
   inventory_product_detail.html (product photo) and repair_detail.html
   (device photo) — same shape both places: pick a file, upload without a
   full-page navigation (a plain <form> submit just hangs blank on a real
   phone photo tripping a size limit, with no response ever reaching the
   page to render — see the 18.08 product-photo fix this generalizes).

   Wires up any <form data-photo-upload="/some/endpoint">, expected to sit
   inside a container with:
     .photo-preview      <img>, shown/updated on a successful upload
     .no-photo-text       hidden once a photo exists
     a <label> whose first child text node reads "Добавить/Заменить фото"
     .photo-status        status line ("Загружаю…" / "Готово ✅" / errors)
   and containing a <input type="file"> + submit <button>. */
(function () {
  document.querySelectorAll("form[data-photo-upload]").forEach(function (form) {
    var endpoint = form.dataset.photoUpload;
    var input = form.querySelector('input[type="file"]');
    var btn = form.querySelector('button[type="submit"]');
    var container = form.closest(".panel-box") || form.parentElement;
    var status = container.querySelector(".photo-status");
    var img = container.querySelector(".photo-preview");
    var noPhotoText = container.querySelector(".no-photo-text");
    var label = form.querySelector("label");

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var file = input.files[0];
      if (!file) return;
      btn.disabled = true;
      if (status) {
        status.style.display = "block";
        status.textContent = "Загружаю…";
      }

      var formData = new FormData();
      formData.append("photo", file);
      fetch(endpoint + window.location.search, {
        method: "POST",
        body: formData,
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data && data.ok) {
            if (img) {
              img.src = data.photo_url + "?v=" + Date.now();
              img.style.display = "inline-block";
            }
            if (noPhotoText) noPhotoText.style.display = "none";
            if (label && label.childNodes[0]) label.childNodes[0].textContent = "Заменить фото";
            if (status) status.textContent = "Готово ✅";
            input.value = "";
          } else if (status) {
            status.textContent = "⚠️ " + ((data && data.error) || "Не удалось загрузить фото.");
          }
        })
        .catch(function () {
          if (status) status.textContent = "⚠️ Не удалось загрузить фото — проверьте соединение и попробуйте ещё раз.";
        })
        .finally(function () {
          btn.disabled = false;
        });
    });
  });
})();
