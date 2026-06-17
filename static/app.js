// PWA service worker
if ('serviceWorker' in navigator)
  navigator.serviceWorker.register('/sw.js');

// Centralized growth analytics wrapper. It is intentionally no-op safe when
// GA4/GTM has not been installed yet.
(function () {
  function snakeKey(key) {
    return key.replace(/^track/, '')
      .replace(/^[A-Z]/, function (m) { return m.toLowerCase(); })
      .replace(/[A-Z]/g, function (m) { return '_' + m.toLowerCase(); });
  }

  function metadataFrom(node) {
    var data = {};
    if (!node || !node.dataset) return data;
    Object.keys(node.dataset).forEach(function (key) {
      if (key === 'trackEvent' || key === 'trackView') return;
      if (key.indexOf('track') !== 0) return;
      data[snakeKey(key)] = node.dataset[key];
    });
    return data;
  }

  function withDefaults(metadata) {
    var body = document.body || {};
    return Object.assign({
      page_path: window.location.pathname,
      user_logged_in: body.dataset && body.dataset.userLoggedIn === 'true',
      timestamp: new Date().toISOString()
    }, metadata || {});
  }

  function emit(eventName, metadata) {
    var payload = withDefaults(metadata);
    try {
      if (typeof window.gtag === 'function') {
        window.gtag('event', eventName, payload);
      }
      if (Array.isArray(window.dataLayer)) {
        window.dataLayer.push(Object.assign({ event: eventName }, payload));
      }
      window.dispatchEvent(new CustomEvent('zimjobs:analytics', {
        detail: { event: eventName, metadata: payload }
      }));
    } catch (_) {}
  }

  function track(eventNames, metadata) {
    String(eventNames || '').split(',').map(function (name) {
      return name.trim();
    }).filter(Boolean).forEach(function (name) {
      emit(name, metadata);
    });
  }

  window.ZimJobsAnalytics = { track: track };

  document.querySelectorAll('[data-track-view]').forEach(function (node) {
    track(node.dataset.trackView, metadataFrom(node));
  });

  document.addEventListener('click', function (e) {
    var node = e.target.closest('[data-track-event]');
    if (!node || node.tagName === 'FORM') return;
    track(node.dataset.trackEvent, metadataFrom(node));
  });

  document.addEventListener('submit', function (e) {
    var node = e.target.closest('form[data-track-event]');
    if (!node) return;
    track(node.dataset.trackEvent, metadataFrom(node));
  });

  try {
    var status = new URLSearchParams(window.location.search).get('email_alert');
    if (status) {
      track('email_alert_signup', { status: status, source: 'redirect' });
    }
  } catch (_) {}
})();

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

// Affiliate offer impression tracking. Clicks are tracked by the server-side
// redirect route so outbound links still work if JavaScript is unavailable.
(function () {
  var offers = document.querySelectorAll('[data-affiliate-impression]');
  if (!offers.length) return;

  var seen = new Set();
  function track(node) {
    var key = node.dataset.offerId + ':' + node.dataset.placementId;
    if (seen.has(key)) return;
    seen.add(key);

    var payload = JSON.stringify({
      event_type: 'impression',
      offer_id: node.dataset.offerId,
      placement_id: node.dataset.placementId,
      job_category: node.dataset.jobCategory || '',
      page_path: window.location.pathname
    });

    if (navigator.sendBeacon) {
      navigator.sendBeacon('/affiliate/event',
        new Blob([payload], { type: 'application/json' }));
      return;
    }
    fetch('/affiliate/event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload,
      keepalive: true
    }).catch(function () {});
  }

  if (!('IntersectionObserver' in window)) {
    offers.forEach(track);
    return;
  }

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      track(entry.target);
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.35 });

  offers.forEach(function (node) { observer.observe(node); });
})();
