// v2.5 deeper MAIN-world detection helpers.
//
// inject.js already patches fetch / XMLHttpRequest / JSON.parse. This file
// covers three more places where manifest text or URLs hide today:
//
//   1. Web Workers — players (hls.js, dash.js, shaka) commonly process
//      manifests inside a Worker via postMessage. We wrap the Worker
//      constructor so we can sniff messages going IN to the worker (where
//      manifest text usually appears as a string parameter) and OUT of it
//      (where parsed segment URLs come back).
//
//   2. atob — sites sometimes base64-pack manifest text. If the result of
//      a decode starts with #EXTM3U or <MPD, we've caught one.
//
//   3. URL.createObjectURL — when a player feeds a Blob or MediaSource into
//      <video src=...>, the blob's first bytes can reveal a manifest. We
//      sniff at construction time, not at video.src assignment time.
//
// SCOPE: this is detection only — finding manifests + segment URLs.
// We deliberately do NOT hook Uint8Array / Int8Array / DataView / String.
// fromCharCode / Array.prototype.join (the typed-array AES-key extraction
// patterns). Those cross from "find the playlist" into "extract the AES
// key the site is trying to hide" — past where RFC 8216 EXT-X-KEY URI=...
// public delivery covers, and past the line drawn in the v2.5 plan.

(function () {
  'use strict';

  if (window.__wv2nas_deepsearch_injected) return;
  window.__wv2nas_deepsearch_injected = true;

  // Reuse inject.js's detect signature — keep both files agreeing on what
  // counts as a manifest so we don't double-fire or miss.
  function detectFormat(text) {
    if (!text || typeof text !== 'string') return null;
    var trimmed = text.trimStart();
    if (trimmed.startsWith('#EXTM3U')) return 'm3u8';
    if (trimmed.startsWith('<MPD') || (trimmed.startsWith('<?xml') && trimmed.indexOf('<MPD') !== -1)) return 'mpd';
    return null;
  }

  function detectMediaUrl(text) {
    if (!text || typeof text !== 'string') return null;
    var trimmed = text.trim();
    if (!/^https?:\/\//i.test(trimmed)) return null;
    var lower = trimmed.toLowerCase();
    if (/\.m3u8(?:[?#]|$)/.test(lower) || /[?&](?:format|type|kind)=(?:hls|m3u8)(?:[&#]|$)/.test(lower)) {
      return { url: trimmed, format: 'm3u8', isManifest: true };
    }
    if (/\.mpd(?:[?#]|$)/.test(lower) || /[?&](?:format|type|kind)=(?:dash|mpd)(?:[&#]|$)/.test(lower)) {
      return { url: trimmed, format: 'mpd', isManifest: true };
    }
    if (/\.(?:ts|m4s)(?:[?#]|$)/.test(lower)) {
      return { url: trimmed, format: lower.indexOf('.m4s') !== -1 ? 'm4s' : 'ts', isManifest: false };
    }
    return null;
  }

  // Same buffer + replay protocol as inject.js so content.js sees both
  // files' detections through one channel. Deep hits need their own buffer:
  // this script runs at document_start in MAIN world, while content.js only
  // attaches its listener at document_idle.
  if (!window.__wv2nas_manifests) window.__wv2nas_manifests = [];
  if (!window.__wv2nas_deep_hits) window.__wv2nas_deep_hits = [];

  // Codex review (P2): a downloadable manifest URL must be a real
  // HTTP(S) URL the NAS can fetch. The page's own URL and `blob:`
  // object URLs are NOT downloadable:
  //   - Page URL: NAS would fetch the HTML page and try to parse it
  //     as a manifest. Fails opaquely; user thinks they queued a
  //     download but nothing happens.
  //   - blob: URL: scoped to the originating page only. Server-side
  //     can't resolve it. NAS rejects the HttpUrl validation.
  // Reject both at the source so they never enter the detection
  // pipeline.
  function _isDownloadableManifestUrl(url) {
    if (!url || typeof url !== 'string') return false;
    var lower = url.toLowerCase();
    if (lower.startsWith('http://') || lower.startsWith('https://')) {
      // The page URL itself doesn't qualify just because it's
      // https — the manifest source has to be different from the
      // user-visible page. We can't tell that perfectly here, but
      // callers of notifyManifest pass the real URL only when they
      // intercepted a network fetch / known media URL; the
      // worker/atob/blob paths in this file pass `window.location`
      // or `blob:` and use notifyDeepHit instead.
      return true;
    }
    return false;
  }

  function notifyManifest(url, format, source) {
    if (!_isDownloadableManifestUrl(url)) {
      // Caller had only an in-memory manifest blob / page URL. Emit
      // a deep-hit so SW can register "page contains stream content"
      // without offering a download that will fail.
      notifyDeepHit({
        kind: 'manifest-text-no-url',
        format: format,
        source: source || 'deepsearch',
      });
      return;
    }
    var data = {
      type: 'WV2NAS_MANIFEST_DETECTED',
      url: url,
      format: format,
      source: source || 'deepsearch',
    };
    window.__wv2nas_manifests.push(data);
    try { window.postMessage(data, '*'); } catch (_) {}
  }

  function notifyDeepHit(payload) {
    var data = {
      type: 'WV2NAS_DEEP_DETECTED',
      ...payload,
    };
    window.__wv2nas_deep_hits.push(data);
    try {
      window.postMessage(data, '*');
    } catch (_) {}
  }

  window.addEventListener('message', function (event) {
    if (event.source !== window) return;
    if (!event.data || event.data.type !== 'WV2NAS_CONTENT_READY') return;
    var hits = window.__wv2nas_deep_hits || [];
    for (var i = 0; i < hits.length; i++) {
      try { window.postMessage(hits[i], '*'); } catch (_) {}
    }
  });

  function sniffWorkerString(value, source, state) {
    var fmt = detectFormat(value);
    if (fmt) {
      notifyManifest(null, fmt, source);
      return 'deep';
    }

    var mediaUrl = detectMediaUrl(value);
    if (!mediaUrl) return null;
    if (mediaUrl.isManifest) {
      notifyManifest(mediaUrl.url, mediaUrl.format, source);
      return 'manifest-url';
    } else {
      if (!state.segmentHitSeen) {
        notifyDeepHit({
          kind: 'segment-url',
          url: mediaUrl.url,
          format: mediaUrl.format,
          source: source,
        });
        state.segmentHitSeen = true;
      }
    }
    return 'deep';
  }

  var WORKER_SCAN_MAX_DEPTH = 8;
  var WORKER_SCAN_MAX_NODES = 500;

  function sniffWorkerValue(value, sourceBase, depth, state) {
    if (state.nodes >= WORKER_SCAN_MAX_NODES) return false;
    state.nodes += 1;

    if (typeof value === 'string') {
      var hit = sniffWorkerString(
        value,
        sourceBase + (depth === 0 ? ':string' : ':obj'),
        state
      );
      if (hit) state.found = true;
      return hit === 'manifest-url';
    }
    if (!value || typeof value !== 'object') return false;
    if (depth >= WORKER_SCAN_MAX_DEPTH) return false;

    try {
      if (typeof ArrayBuffer === 'function') {
        if (value instanceof ArrayBuffer) return false;
        if (ArrayBuffer.isView && ArrayBuffer.isView(value)) return false;
      }
      if (typeof Blob === 'function' && value instanceof Blob) return false;
    } catch (_) {}

    if (state.seen) {
      try {
        if (state.seen.has(value)) return false;
        state.seen.add(value);
      } catch (_) {}
    }

    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i++) {
        if (sniffWorkerValue(value[i], sourceBase, depth + 1, state)) return true;
      }
      return false;
    }

    for (var k in value) {
      if (!Object.prototype.hasOwnProperty.call(value, k)) continue;
      var child;
      try {
        child = value[k];
      } catch (_) {
        continue;
      }
      if (sniffWorkerValue(child, sourceBase, depth + 1, state)) return true;
    }
    return false;
  }

  function sniffWorkerPayload(payload, sourceBase) {
    var state = {
      nodes: 0,
      found: false,
      segmentHitSeen: false,
      seen: typeof WeakSet === 'function' ? new WeakSet() : null,
    };
    sniffWorkerValue(payload, sourceBase, 0, state);
    return state.found;
  }

  // -----------------------------------------------------------------------
  // 1. Worker hook
  // -----------------------------------------------------------------------
  //
  // We wrap the Worker constructor so we can intercept .postMessage calls
  // (sniff what's going INTO the worker — typically the raw manifest text
  // when hls.js/dash.js demuxes off-thread) and also instrument the worker's
  // onmessage to catch parsed manifests coming back.

  try {
    var OriginalWorker = window.Worker;
    if (typeof OriginalWorker === 'function') {
      // ESM Worker constructor accepts URL | string. Don't mess with the
      // script URL itself — just shim the prototype's postMessage.
      var WrappedWorker = function (scriptURL, options) {
        var w = new OriginalWorker(scriptURL, options);
        var origPostMessage = w.postMessage.bind(w);
        try {
          if (typeof w.addEventListener === 'function') {
            w.addEventListener('message', function (event) {
              try {
                sniffWorkerPayload(event && event.data, 'worker:message');
              } catch (_) {}
            }, true);
          }
        } catch (_) {}
        w.postMessage = function () {
          try {
            var first = arguments[0];
            // Codex review (P2): we have manifest TEXT in memory but
            // no downloadable URL — passing `window.location.href`
            // here used to make the side-panel offer the page URL
            // as a manifest, which NAS then tries to parse as
            // m3u8/mpd and fails. Pass null so notifyManifest
            // routes to the deep-hit channel (telemetry only, no
            // user-clickable broken entry).
            //
            // Common shapes: a string, or an object with `data`/`payload`.
            sniffWorkerPayload(first, 'worker:postMessage');
          } catch (_) {}
          return origPostMessage.apply(this, arguments);
        };
        return w;
      };
      WrappedWorker.prototype = OriginalWorker.prototype;
      // Preserve static members the player may consult.
      try {
        Object.setPrototypeOf(WrappedWorker, OriginalWorker);
      } catch (_) {}
      window.Worker = WrappedWorker;
    }
  } catch (_) {
    // If anything in the wrapping fails, fall back silently — the page's
    // workers continue to function on the original Worker.
  }

  // -----------------------------------------------------------------------
  // 2. atob hook
  // -----------------------------------------------------------------------
  //
  // Light-touch: only fire when the decoded result starts with a manifest
  // signature. We do NOT scan every atob call's body — that's noisy and
  // crosses into the typed-array key reconstruction territory we explicitly
  // declined.

  try {
    var origAtob = window.atob;
    if (typeof origAtob === 'function') {
      window.atob = function (encoded) {
        var result = origAtob.apply(this, arguments);
        try {
          if (typeof result === 'string' && result.length < 5 * 1024 * 1024) {
            var fmt = detectFormat(result);
            if (fmt) {
              // Codex review (P2): atob produced manifest TEXT in
              // memory — we have no downloadable URL. Pass null so
              // notifyManifest routes to the deep-hit channel.
              notifyManifest(null, fmt, 'atob');
            }
          }
        } catch (_) {}
        return result;
      };
    }
  } catch (_) {}

  // -----------------------------------------------------------------------
  // 3. URL.createObjectURL hook
  // -----------------------------------------------------------------------
  //
  // Players that hand <video> a blob:URL pointing at a Blob/MediaSource may
  // be staging a manifest there. We can't synchronously read a Blob's bytes
  // (FileReader is async), so we trigger a peek that posts a deep-hit if
  // the blob begins with a manifest signature. The url itself is also
  // reported so background.js can correlate even if peek finishes after
  // the player has already fed the video element.

  try {
    var origCreateObjectURL = URL.createObjectURL;
    if (typeof origCreateObjectURL === 'function') {
      URL.createObjectURL = function (obj) {
        var url = origCreateObjectURL.apply(this, arguments);
        try {
          // Only inspect Blob/File — not MediaSource (whose source buffers
          // we'd have to instrument separately, and which often hold raw
          // segment bytes rather than manifest text).
          if (obj && typeof obj === 'object' && typeof obj.size === 'number'
              && typeof obj.slice === 'function' && obj.size > 0 && obj.size < 5 * 1024 * 1024) {
            var head = obj.slice(0, 256);
            if (head && typeof head.text === 'function') {
              head.text().then(function (text) {
                var fmt = detectFormat(text);
                if (fmt) {
                  // Codex review (P2): the `blob:...` URL is
                  // page-scoped — server-side cannot fetch it, and
                  // sendToNAS would just bounce off HttpUrl
                  // validation. Pass null so notifyManifest routes
                  // to the deep-hit channel; the user gets an
                  // accurate "stream content present" signal but
                  // no broken download offer.
                  notifyManifest(null, fmt, 'createObjectURL');
                }
              }).catch(function () {});
            }
          }
          notifyDeepHit({ kind: 'createObjectURL', url: url, mime: obj && obj.type ? obj.type : null });
        } catch (_) {}
        return url;
      };
    }
  } catch (_) {}
})();
