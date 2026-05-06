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
  // Variant for raw-source-text scans (oversized JSON fallback). JSON
  // encoders emit \/ for forward slashes and \uXXXX for many other
  // characters that commonly appear in signed URLs. The structural
  // regex rejects all backslashes, so this variant explicitly accepts
  // both forms at every position where a literal char would otherwise
  // anchor the match. Matches must be decoded before notify() — see
  // decodeJsonEscapesInUrl + scanRawTextForMediaUrls.
  //
  // Audit of literal positions (kept in lock-step with PREFILTER_MARKER_RE):
  //   - protocol separator `:`  → also accept \u003[aA]   (round 13)
  //   - slashes `//`             → also accept \u002[fF]   (round 10)
  //   - URL body chars           → accept any \uXXXX        (round 10)
  //   - extension dot `.`        → also accept \u002[eE]   (round 12)
  //   - query delimiter `?`      → also accept \u003[fF]   (round 13, Codex)
  //   - query body chars         → accept any \uXXXX        (round 10)
  // Extension chars (m3u8/mpd) and protocol letters (https) intentionally
  // stay literal — escaping alphanumerics is not a realistic encoder
  // behavior and tightening here would let the regex match arbitrary
  // hex sequences.
  var RAW_MEDIA_URL_RE = /https?(?::|\\u003[aA])(?:\\?\/|\\u002[fF]){2}(?:[^\s"'<>\\]|\\\/|\\u[0-9a-fA-F]{4})+(?:\.|\\u002[eE])(m3u8|mpd)(?:(?:\?|\\u003[fF])(?:[^\s"'<>\\]|\\\/|\\u[0-9a-fA-F]{4})*)?/gi;
  var SCAN_MAX_DEPTH = 10;
  // v2.4.1 (Codex adversarial review): bound the structural walk so a
  // pathological API payload (huge benign object) cannot dominate the
  // page's main thread. Numbers are conservative — small enough that even
  // worst-case scan completes in single-digit ms, large enough to cover
  // realistic playlist payloads (a few hundred qualities × a few fields).
  var SCAN_MAX_NODES = 5000;
  // Per-string scan budget. Large embedded blobs (player config, HTML
  // snippets, etc.) routinely run into the tens of KB and legitimately
  // contain manifest URLs, so we MUST scan them — but we window the scan
  // to STRING_SCAN_MAX_BYTES (regex.exec is single-pass O(n) so this only
  // matters for pathological multi-MB single strings) and cap matches per
  // string to avoid notify amplification.
  var STRING_SCAN_MAX_BYTES = 1 * 1024 * 1024;
  var STRING_SCAN_MAX_MATCHES = 8;
  var SCAN_MAX_RAW_BYTES = 4 * 1024 * 1024;
  // Above SCAN_MAX_RAW_BYTES we don't do a structural recursive walk
  // (that's the page-jank risk we want to avoid), but we DO run a single
  // bounded regex pass over the raw string up to RAW_REGEX_MAX_BYTES so
  // payloads that legitimately embed a manifest URL aren't silently
  // dropped (Codex adversarial review #2). Beyond RAW_REGEX_MAX_BYTES
  // we give up entirely — at that scale even a single regex pass is
  // user-visible work on the main thread.
  var RAW_REGEX_MAX_BYTES = 16 * 1024 * 1024;
  // Global cap on emitted notifications per JSON.parse call. Per-string
  // (8) × node-budget (5000) was a theoretical 40k notify ceiling — bound
  // it globally too. (Codex adversarial review #6.) Originally 32 — raised
  // to 256 in round 7 because real player bootstraps with multi-CDN and
  // multi-quality variants legitimately have ~50–100 unique URLs and the
  // playable master often comes after the alternates. 256 stays well
  // bounded for postMessage cost while covering realistic payloads;
  // truncation telemetry surfaces the rare cases where it isn't enough.
  var TOTAL_EMIT_BUDGET = 256;
  // CPU bound for the raw-text fallback loop. Counts ALL regex iterations
  // (including dedup hits where emit returns true without notifying), so
  // it should be much larger than TOTAL_EMIT_BUDGET — otherwise a payload
  // with many duplicate URLs would starve later unique URLs (Codex
  // adversarial review #9). Set to 10× the emit budget: gives ample
  // headroom for dedup-heavy payloads while still capping CPU well below
  // user-visible levels (regex.exec on 16MB is single-pass O(n) so even
  // 10000 invocations stay tens of ms at most).
  var RAW_REGEX_MAX_ITERATIONS = TOTAL_EMIT_BUDGET * 10;
  // Case-insensitive marker — must match MEDIA_URL_RE's /gi semantics so a
  // .M3U8 / .Mpd extension on the wire still triggers the structural scan.
  // Single-pass test, no per-call string allocation (raw text can be MBs).
  // Also accepts the JSON Unicode-escaped dot form `.` — some
  // serializers escape punctuation, and missing this prefilter case
  // silently drops the structural scan even though the parsed value would
  // contain a normal `.m3u8`. (Codex adversarial review #11.)
  var PREFILTER_MARKER_RE = /(?:\.|\\u002[eE])(?:m3u8|mpd)/i;

  // Stats counter — closure-local so the MAIN-world page cannot disable
  // the scan by pre-locking, freezing, or replacing the stats slot
  // (Codex adversarial review #5). Exposed for tests and live triage via
  // a read-only snapshot getter installed below; actual mutations always
  // target this private object.
  var _stats = {
    calls: 0,
    prefilter_skips: 0,
    raw_scans: 0,
    nodes_walked: 0,
    budget_aborts: 0,
    // truncated counts emit attempts dropped because the per-parse global
    // cap (TOTAL_EMIT_BUDGET) was already exhausted. Non-zero means at
    // least one URL was found by scanning but never forwarded — surface
    // this so triage doesn't silently miss "we capped early" cases.
    truncated: 0
  };
  // Best-effort exposure for triage. If the page already locked the slot
  // with a non-configurable descriptor, swallow the error — the scan
  // still works against _stats either way; we just lose telemetry.
  try {
    Object.defineProperty(window, '__wv2nas_scan_stats', {
      configurable: false,
      enumerable: false,
      get: function () {
        // Return a fresh shallow snapshot so the page can't mutate our
        // counters by writing through the returned object.
        return {
          calls: _stats.calls,
          prefilter_skips: _stats.prefilter_skips,
          raw_scans: _stats.raw_scans,
          nodes_walked: _stats.nodes_walked,
          budget_aborts: _stats.budget_aborts,
          truncated: _stats.truncated
        };
      }
    });
  } catch (_) {
    // Page locked the slot first — fine, scan continues without telemetry.
  }

  function rawTextHasMediaMarker(raw) {
    return PREFILTER_MARKER_RE.test(raw);
  }

  // Decode the subset of JSON string escapes that legitimately appear in
  // URLs: \/ → /, and \uXXXX → BMP char. Other JSON escapes (\\ \" \b
  // \f \n \r \t) don't appear in valid URLs and are deliberately left
  // alone (if seen, the URL is malformed anyway). Fast path returns the
  // input unchanged when there are no backslashes.
  function decodeJsonEscapesInUrl(s) {
    if (s.indexOf('\\') === -1) return s;
    return s
      .replace(/\\u([0-9a-fA-F]{4})/g, function (_match, hex) {
        return String.fromCharCode(parseInt(hex, 16));
      })
      .replace(/\\\//g, '/');
  }

  // Gated notify(): per-parse dedup + global emit budget. Returns false
  // when the global budget is exhausted so callers stop scanning. Dedup
  // is intentionally per-parse only — across parses the downstream layer
  // already handles duplicate URLs from fetch/XHR observers.
  // When the budget is exhausted, _stats.truncated is incremented so
  // triage can tell "we never forwarded this URL" apart from "we never
  // saw a URL".
  function emit(ctx, url, format) {
    if (ctx.emitted >= TOTAL_EMIT_BUDGET) {
      ctx.aborted = true;
      _stats.truncated++;
      return false;
    }
    if (ctx.seen[url]) return true;
    ctx.seen[url] = true;
    ctx.emitted++;
    notify(url, format);
    return true;
  }

  // Bounded fallback for oversized JSON: regex once over the raw string,
  // capped by total matches. NO recursive walk, NO substring allocation —
  // just a single pass through the source text. Backstop the page-jank
  // protection without silently dropping legitimate detections.
  // Uses RAW_MEDIA_URL_RE which tolerates JSON-escaped slashes (\/) and
  // unescapes them before notify so consumers receive a usable URL.
  function scanRawTextForMediaUrls(raw, ctx) {
    var iterations = 0;
    var m;
    RAW_MEDIA_URL_RE.lastIndex = 0;
    while ((m = RAW_MEDIA_URL_RE.exec(raw)) !== null) {
      var url = decodeJsonEscapesInUrl(m[0]);
      if (!emit(ctx, url, m[1].toLowerCase())) return;
      // Bound CPU per call. emit() handles unique-emission cap; this
      // guard only fires for pathologically dup-heavy payloads so we
      // don't spin the regex through millions of duplicate matches.
      if (++iterations >= RAW_REGEX_MAX_ITERATIONS) break;
    }
  }

  function scanForMediaUrls(value, depth, ctx) {
    if (ctx.aborted) return;
    if (depth > SCAN_MAX_DEPTH) return;
    ctx.nodes++;
    if (ctx.nodes > SCAN_MAX_NODES) {
      ctx.aborted = true;
      return;
    }
    if (typeof value === 'string') {
      // Window very large strings (1MB+) to bound worst-case scan time;
      // most player-config blobs are well under this and realistic embedded
      // URLs appear early in the payload. Cap matches per string so a
      // degenerate match-everywhere blob can't flood postMessage.
      // When we DO truncate, mark ctx so the post-walk path runs the
      // raw-text fallback over the original raw JSON (which contains the
      // full string content) — otherwise URLs past byte 1MB would be
      // silently dropped (Codex adversarial review #9).
      var view;
      if (value.length > STRING_SCAN_MAX_BYTES) {
        view = value.substring(0, STRING_SCAN_MAX_BYTES);
        ctx.string_truncated = true;
      } else {
        view = value;
      }
      var found = 0;
      var m;
      MEDIA_URL_RE.lastIndex = 0;  // global regex — reset state per call
      while ((m = MEDIA_URL_RE.exec(view)) !== null) {
        if (!emit(ctx, m[0], m[1].toLowerCase())) return;
        found++;
        if (found >= STRING_SCAN_MAX_MATCHES) break;
      }
      return;
    }
    if (typeof value !== 'object' || value === null) return;
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i++) {
        if (ctx.aborted) return;
        scanForMediaUrls(value[i], depth + 1, ctx);
      }
      return;
    }
    for (var key in value) {
      if (ctx.aborted) return;
      if (Object.prototype.hasOwnProperty.call(value, key)) {
        scanForMediaUrls(value[key], depth + 1, ctx);
      }
    }
  }

  var origJsonParse = JSON.parse;
  JSON.parse = function() {
    var result = origJsonParse.apply(this, arguments);
    try {
      _stats.calls++;
      // Cheap raw-text prefilter. Most JSON.parse calls a page makes have
      // nothing to do with media; skipping the recursive walk for them is
      // the difference between "passive observer" and "page-wide jank".
      var raw = arguments[0];
      if (typeof raw === 'string' && !rawTextHasMediaMarker(raw)) {
        _stats.prefilter_skips++;
        return result;
      }
      // One ctx per parse — drives node budget, abort, per-parse URL
      // dedup, and global emit budget. seen uses a null-prototype object
      // so user-controlled URL strings can't collide with Object.prototype.
      // string_truncated tracks whether any individual string field was
      // larger than STRING_SCAN_MAX_BYTES and got windowed — if so, the
      // post-walk path runs raw fallback to catch URLs past the window.
      var ctx = {
        nodes: 0,
        aborted: false,
        emitted: 0,
        seen: Object.create(null),
        string_truncated: false
      };
      if (typeof raw === 'string' && raw.length > SCAN_MAX_RAW_BYTES) {
        // Oversized but a marker is present → bounded raw-regex fallback
        // (no structural walk). Beyond the hard ceiling, give up.
        if (raw.length <= RAW_REGEX_MAX_BYTES) {
          scanRawTextForMediaUrls(raw, ctx);
          _stats.raw_scans++;
        } else {
          _stats.prefilter_skips++;
        }
        return result;
      }
      scanForMediaUrls(result, 0, ctx);
      _stats.nodes_walked += ctx.nodes;
      if (ctx.aborted) _stats.budget_aborts++;
      // Run raw fallback when:
      //   - structural walk aborted on node/emit budget (Codex review #4)
      //   - any string field was windowed at STRING_SCAN_MAX_BYTES so
      //     URLs past byte 1MB inside that string need re-scanning over
      //     the full raw JSON text (Codex review #9)
      // emit() short-circuits cleanly if the budget is already exhausted,
      // and seen[] prevents double-notify of URLs the structural walk
      // already found.
      if ((ctx.aborted || ctx.string_truncated) &&
          typeof raw === 'string' &&
          raw.length <= RAW_REGEX_MAX_BYTES) {
        scanRawTextForMediaUrls(raw, ctx);
        _stats.raw_scans++;
      }
    } catch (_) {
      // Never break the page on scan errors — this is a passive observer.
    }
    return result;
  };
})();
