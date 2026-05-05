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

  // --- Patch JSON.parse (v2.3.18 deepsearch port from CocoCut) ---
  // The fetch/XHR scans above only look at the first 500 bytes of a
  // response and only fire when the body STARTS with #EXTM3U or <MPD.
  // Many sites wrap the manifest URL inside a JSON API response, e.g.
  //   {"video": {"hls": "https://cdn.example.com/.../master.m3u8"}}
  // — those URLs never appear at byte 0 of the response, so they're
  // missed. CocoCut's deepsearch hooks JSON.parse and walks every
  // parsed object looking for m3u8/mpd URLs in any string field. We
  // port the same idea here. Worker / AES-key hooks from CocoCut are
  // intentionally NOT ported (Worker injection is fragile across
  // origins; AES keys are usually fetched via #EXT-X-KEY URI which
  // we already capture).
  var MEDIA_URL_RE = /https?:\/\/[^\s"'<>\\]+\.(m3u8|mpd)(\?[^\s"'<>\\]*)?/gi;
  var SCAN_MAX_DEPTH = 10;

  function scanForMediaUrls(value, depth) {
    if (depth === undefined) depth = 0;
    if (depth > SCAN_MAX_DEPTH) return;
    if (typeof value === 'string') {
      var m;
      MEDIA_URL_RE.lastIndex = 0;  // global regex — reset state per call
      while ((m = MEDIA_URL_RE.exec(value)) !== null) {
        notify(m[0], m[1].toLowerCase());
      }
      return;
    }
    if (typeof value !== 'object' || value === null) return;
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i++) scanForMediaUrls(value[i], depth + 1);
      return;
    }
    for (var key in value) {
      if (Object.prototype.hasOwnProperty.call(value, key)) {
        scanForMediaUrls(value[key], depth + 1);
      }
    }
  }

  var origJsonParse = JSON.parse;
  JSON.parse = function() {
    var result = origJsonParse.apply(this, arguments);
    try {
      scanForMediaUrls(result);
    } catch (_) {
      // Never break the page on scan errors — this is a passive observer.
    }
    return result;
  };
})();
