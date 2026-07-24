// conway-badge.js — drop-in Base address-safety badge. FREE (/overview), no wallet, no key.
// Usage:
//   <span class="conway-badge" data-address="0xABC…"></span>
//   <script src="https://YOUR-CDN/conway-badge.js"></script>
// Renders: "👤 wallet · 🆕 never used" etc. Onchain facts only — not an audit.
(function () {
  var BASE = "https://conway-address-intel-production.up.railway.app";
  function render(el) {
    var addr = el.getAttribute("data-address");
    if (!addr) return;
    el.textContent = "checking…";
    fetch(BASE + "/overview/" + addr)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) { el.textContent = ""; return; }
        var bits = [d.type === "contract" ? "📄 contract" : "👤 wallet"];
        if (!d.holds_funds) bits.push("∅ no funds");
        if (d.activity === "none") bits.push("🆕 never used");
        else if (d.activity === "high") bits.push("⚡ very active");
        el.textContent = bits.join(" · ");
        el.title = "Address intel · onchain facts, not an audit · conway";
      })
      .catch(function () { el.textContent = ""; });
  }
  function init() { Array.prototype.forEach.call(document.querySelectorAll(".conway-badge"), render); }
  if (document.readyState !== "loading") init(); else document.addEventListener("DOMContentLoaded", init);
  window.ConwayBadge = { render: render, init: init };
})();
