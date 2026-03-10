// Runs in the MAIN world (page context) to intercept fetch/XHR responses
// and detect HLS/DASH manifests by inspecting response content.
// This catches manifests served from arbitrary URLs without standard
// extensions or Content-Type headers (e.g. sites that disguise segments as .jpg).

(function() {
  'use strict';

  // Avoid double-injection
  if (window.__wv2nas_injected) return;
  window.__wv2nas_injected = true;

  function detectFormat(text) {
    if (!text || typeof text !== 'string') return null;
    var trimmed = text.trimStart();
    if (trimmed.startsWith('#EXTM3U')) return 'm3u8';
    if (trimmed.startsWith('<MPD') || (trimmed.startsWith('<?xml') && trimmed.includes('<MPD'))) return 'mpd';
    return null;
  }

  // Buffer detected manifests so content.js can read them even if it loads late
  if (!window.__wv2nas_manifests) window.__wv2nas_manifests = [];

  function notify(url, format) {
    var data = { type: 'WV2NAS_MANIFEST_DETECTED', url: url, format: format };
    window.__wv2nas_manifests.push(data);
    try {
      window.postMessage(data, '*');
    } catch (_) {}
  }

  // Skip URLs that definitely can't be manifests.
  // We intentionally do NOT filter by Content-Type because some sites
  // disguise manifests as image/jpeg or other non-standard types.
  function shouldInspect(url) {
    if (!url || typeof url !== 'string') return false;
    if (url.startsWith('data:') || url.startsWith('blob:') || url.startsWith('chrome')) return false;
    return true;
  }

  // When content.js signals it's ready, replay any buffered manifests
  window.addEventListener('message', function(event) {
    if (event.data && event.data.type === 'WV2NAS_CONTENT_READY') {
      var buf = window.__wv2nas_manifests || [];
      for (var i = 0; i < buf.length; i++) {
        try { window.postMessage(buf[i], '*'); } catch (_) {}
      }
    }
  });

  // --- Patch fetch ---
  var origFetch = window.fetch;
  window.fetch = function() {
    var args = arguments;
    var url = null;
    try {
      var input = args[0];
      url = (typeof input === 'string') ? input
        : (input && typeof input === 'object' && input.url) ? input.url
        : null;
    } catch (_) {}

    var promise = origFetch.apply(this, args);

    if (url) {
      promise.then(function(response) {
        try {
          if (!shouldInspect(url)) return;

          var cl = parseInt(response.headers.get('content-length') || '0', 10);
          if (cl > 5 * 1024 * 1024) return;

          var cloned = response.clone();
          var reader = cloned.body.getReader();
          reader.read().then(function(result) {
            reader.cancel();
            if (!result.value) return;
            var snippet = new TextDecoder().decode(result.value.slice(0, 500));
            var fmt = detectFormat(snippet);
            if (fmt) {
              var finalUrl = url;
              try { finalUrl = response.url || url; } catch (_) {}
              notify(finalUrl, fmt);
            }
          }).catch(function() {});
        } catch (_) {}
      }).catch(function() {});
    }

    return promise;
  };

  // --- Patch XMLHttpRequest ---
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    this._wv2nas_url = url;
    return origOpen.apply(this, arguments);
  };

  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function() {
    var xhr = this;
    xhr.addEventListener('load', function() {
      try {
        var url = xhr._wv2nas_url;
        if (!url) return;
        if (!shouldInspect(url)) return;
        var text = null;
        if (xhr.responseType === '' || xhr.responseType === 'text') {
          text = (xhr.responseText || '').substring(0, 500);
        } else if (xhr.responseType === 'arraybuffer' && xhr.response) {
          try { text = new TextDecoder().decode(new Uint8Array(xhr.response).slice(0, 500)); } catch (_) {}
        }
        if (text) {
          var fmt = detectFormat(text);
          if (fmt) {
            var finalUrl = xhr.responseURL || url;
            notify(finalUrl, fmt);
          }
        }
      } catch (_) {}
    });
    return origSend.apply(this, arguments);
  };
})();
