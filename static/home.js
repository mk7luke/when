(function () {
  var expr = document.getElementById("expr");
  var tz = document.getElementById("tz");
  var custom = document.getElementById("tz-custom");
  var note = document.getElementById("zone-note");
  var form = document.getElementById("form");

  document.addEventListener("keydown", function (ev) {
    var tag = (ev.target && ev.target.tagName) || "";
    if (ev.key === "/" && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") {
      ev.preventDefault();
      if (expr) expr.focus();
    }
  });

  var usePhone = document.getElementById("use-phone");
  var phoneZone = "";
  if (window.Intl && Intl.DateTimeFormat) {
    try { phoneZone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
  }
  if (note && phoneZone) {
    note.textContent = "This phone is " + phoneZone + ". Other zone wins if it is filled. The server will not guess.";
  }
  if (usePhone && phoneZone && form) {
    usePhone.hidden = false;
    usePhone.textContent = "Use " + phoneZone;
    usePhone.addEventListener("click", function () {
      var opt = tz && tz.querySelector('option[value="' + phoneZone + '"]');
      if (opt && tz) {
        tz.value = phoneZone;
        if (custom) custom.value = "";
      } else if (custom) {
        custom.value = phoneZone;
      }
      form.submit();
    });
  }

  if (tz && custom) {
    tz.addEventListener("change", function () {
      custom.value = "";
    });
  }

  var copyExpr = document.getElementById("copy-expr");
  var copyLink = document.getElementById("copy-link");
  function copy(btn, text) {
    if (!btn || !navigator.clipboard) return;
    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(text).then(function () {
        var prev = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = prev; }, 1200);
      }).catch(function () {});
    });
  }
  if (copyExpr && expr) copy(copyExpr, expr.value);
  if (copyLink) copy(copyLink, window.location.href);
})();
