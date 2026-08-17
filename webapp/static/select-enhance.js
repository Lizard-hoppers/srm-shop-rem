/* Replaces every native <select> with a small custom dropdown we fully
 * style ourselves. Native <select> option-list popups are rendered by the
 * OS/engine in many WebViews (notably desktop Telegram) — page CSS cannot
 * reach that popup at all there, so it always renders with a bright native
 * background no matter what we set on <option>. The original <select>
 * stays in the DOM (just visually hidden) so forms submit exactly as
 * before; nothing server-side needs to change.
 */
(function () {
  function enhance(select) {
    if (select.dataset.enhanced) return;
    select.dataset.enhanced = "1";

    var wrap = document.createElement("div");
    wrap.className = "csel";
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.classList.add("csel-native");

    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "csel-trigger";
    wrap.appendChild(trigger);

    var panel = document.createElement("div");
    panel.className = "csel-panel";
    wrap.appendChild(panel);

    function updateLabel() {
      var opt = select.options[select.selectedIndex];
      trigger.textContent = opt ? opt.textContent : "";
    }

    function buildPanel() {
      panel.innerHTML = "";
      Array.prototype.forEach.call(select.options, function (opt, i) {
        var item = document.createElement("div");
        item.className = "csel-item" + (i === select.selectedIndex ? " active" : "");
        item.textContent = opt.textContent;
        item.addEventListener("click", function () {
          select.selectedIndex = i;
          select.dispatchEvent(new Event("change", { bubbles: true }));
          updateLabel();
          closePanel();
        });
        panel.appendChild(item);
      });
    }

    function openPanel() {
      document.querySelectorAll(".csel-panel.open").forEach(function (p) {
        if (p !== panel) p.classList.remove("open");
      });
      buildPanel();
      panel.classList.add("open");
      document.addEventListener("click", onOutsideClick, true);
    }

    function closePanel() {
      panel.classList.remove("open");
      document.removeEventListener("click", onOutsideClick, true);
    }

    function onOutsideClick(e) {
      if (!wrap.contains(e.target)) closePanel();
    }

    trigger.addEventListener("click", function (e) {
      e.stopPropagation();
      if (panel.classList.contains("open")) closePanel();
      else openPanel();
    });

    updateLabel();
  }

  function enhanceAll() {
    document.querySelectorAll("select").forEach(enhance);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhanceAll);
  } else {
    enhanceAll();
  }
})();
