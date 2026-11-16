// Kleine progressieve verbeteringen; alles werkt ook zonder JavaScript.
document.addEventListener("submit", function (e) {
  var vraag = e.target.getAttribute && e.target.getAttribute("data-bevestig");
  if (vraag && !window.confirm(vraag)) e.preventDefault();
});
document.addEventListener("click", function (e) {
  var id = e.target.getAttribute && e.target.getAttribute("data-kopieer");
  if (!id) return;
  var el = document.getElementById(id);
  if (el && navigator.clipboard) {
    navigator.clipboard.writeText(el.textContent.trim()).then(function () {
      e.target.textContent = "Gekopieerd";
    });
  }
});
