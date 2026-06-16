// PWA service worker
if ('serviceWorker' in navigator)
  navigator.serviceWorker.register('/sw.js');

// Keep sticky filter controls below the responsive sticky header.
(function () {
  var header = document.querySelector('header');
  if (!header) return;

  function setStickyFilterOffset() {
    document.documentElement.style.setProperty(
      '--sticky-filter-top',
      header.getBoundingClientRect().height + 'px'
    );
  }

  setStickyFilterOffset();
  window.addEventListener('resize', setStickyFilterOffset);

  if ('ResizeObserver' in window) {
    new ResizeObserver(setStickyFilterOffset).observe(header);
  }
})();

// Copy-link buttons
document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-copy]');
  if (!b) return;
  (navigator.clipboard ? navigator.clipboard.writeText(b.dataset.copy)
                       : Promise.reject())
    .then(function(){ b.textContent = '✓ Copied'; })
    .catch(function(){ prompt('Copy this link:', b.dataset.copy); });
});
