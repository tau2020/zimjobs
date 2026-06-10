// PWA service worker
if ('serviceWorker' in navigator)
  navigator.serviceWorker.register('/sw.js');

// Copy-link buttons
document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-copy]');
  if (!b) return;
  (navigator.clipboard ? navigator.clipboard.writeText(b.dataset.copy)
                       : Promise.reject())
    .then(function(){ b.textContent = '✓ Copied'; })
    .catch(function(){ prompt('Copy this link:', b.dataset.copy); });
});
