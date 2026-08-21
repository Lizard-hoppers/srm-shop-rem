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
   and containing a <input type="file"> + submit <button>.

   Shrinks the photo client-side before upload (19.08) — a phone camera
   photo straight off the camera is several MB at 3000+px, which on a
   mobile connection is most of what made this feel slow; the server
   caps stored photos at 1600px anyway (core.photos.compress_photo), so
   sending anything bigger than that never bought any real quality. */
(function () {
  function shrinkPhoto(file) {
    return new Promise(function (resolve) {
      var img = new Image();
      var url = URL.createObjectURL(file);
      img.onload = function () {
        URL.revokeObjectURL(url);
        var scale = Math.min(1, 1600 / Math.max(img.width, img.height));
        if (scale === 1) { resolve(file); return; }
        var canvas = document.createElement("canvas");
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(function (blob) { resolve(blob || file); }, "image/jpeg", 0.85);
      };
      img.onerror = function () { resolve(file); };
      img.src = url;
    });
  }

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

      shrinkPhoto(file)
        .then(function (photoBlob) {
          var formData = new FormData();
          formData.append("photo", photoBlob, "photo.jpg");
          return fetch(endpoint + window.location.search, { method: "POST", body: formData });
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
