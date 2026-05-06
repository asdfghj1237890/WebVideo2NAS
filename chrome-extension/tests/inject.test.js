// Tests for inject.js v2.3.18 deepsearch port — JSON.parse hook that
// scans parsed objects for m3u8/mpd URLs embedded in API responses.
//
// inject.js runs as a self-invoking IIFE in the page context. We load it
// into a vm sandbox with stub `window`, `XMLHttpRequest`, `fetch`, and
// capture postMessage calls.

import { describe, expect, it, beforeEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const INJECT_PATH = path.resolve(__dirname, '..', 'inject.js');
const INJECT_SRC = fs.readFileSync(INJECT_PATH, 'utf-8');

function createSandbox(options) {
  options = options || {};
  const messages = [];
  const ctx = {
    console,
    URL,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    JSON: { parse: JSON.parse, stringify: JSON.stringify },
    TextDecoder,
    Object,
    Array,
  };
  ctx.window = {
    __messages: messages,
    postMessage: (data) => messages.push(data),
    addEventListener: () => {},
    fetch: () => Promise.resolve({}),
  };
  // Simulate hostile-page preconditions where the page has already locked
  // the stats slot before inject.js runs (Codex adversarial review #5).
  if (options.preStatsValue !== undefined) {
    ctx.window.__wv2nas_scan_stats = options.preStatsValue;
  }
  if (options.lockStatsSlot) {
    Object.defineProperty(ctx.window, '__wv2nas_scan_stats', {
      value: options.preStatsValue !== undefined ? options.preStatsValue : null,
      writable: false,
      configurable: false,
    });
  }
  // inject.js patches XMLHttpRequest.prototype methods, so we need a real
  // class with the methods present.
  ctx.XMLHttpRequest = class XHR {
    open() {}
    send() {}
    addEventListener() {}
  };
  vm.createContext(ctx);
  vm.runInContext(INJECT_SRC, ctx);
  return ctx;
}

function getManifestMessages(ctx) {
  return ctx.window.__messages.filter(
    (m) => m && m.type === 'WV2NAS_MANIFEST_DETECTED'
  );
}

describe('inject.js deepsearch JSON.parse hook (v2.3.18)', () => {
  it('detects m3u8 URL embedded inside parsed JSON object', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('{"video":{"hls":"https://cdn.example.com/v/master.m3u8"}}');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/v/master.m3u8');
    expect(msgs[0].format).toBe('m3u8');
  });

  it('detects mpd URL embedded inside parsed JSON', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('{"streams":[{"dash":"https://cdn.example.com/v/manifest.mpd"}]}');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].format).toBe('mpd');
  });

  it('detects URLs with query strings', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"u":"https://cdn.example.com/v/index.m3u8?token=abc&exp=999"}'
    );

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe(
      'https://cdn.example.com/v/index.m3u8?token=abc&exp=999'
    );
  });

  it('walks deeply nested objects', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"a":{"b":{"c":{"d":{"deep":"https://x.test/y.m3u8"}}}}}'
    );

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://x.test/y.m3u8');
  });

  it('walks arrays', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"qualities":[{"url":"https://x.test/720p.m3u8"},{"url":"https://x.test/1080p.m3u8"}]}'
    );

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(2);
    expect(msgs.map((m) => m.url).sort()).toEqual([
      'https://x.test/1080p.m3u8',
      'https://x.test/720p.m3u8',
    ]);
  });

  it('finds multiple URLs in the same string field', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"playlist":"primary: https://x.test/a.m3u8 ; backup: https://y.test/b.mpd"}'
    );

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(2);
  });

  it('does not fire on JSON without media URLs', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"user":{"name":"alice","age":30,"website":"https://example.com/about.html"}}'
    );

    expect(getManifestMessages(ctx)).toHaveLength(0);
  });

  it('does not match m3u8 substring without proper URL prefix', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('{"comment":"the m3u8 file format is..."}');

    expect(getManifestMessages(ctx)).toHaveLength(0);
  });

  it('does not break the page when scan throws', () => {
    const ctx = createSandbox();
    // Even if the parsed value is an exotic shape, parsing must succeed
    // and return the result. Inject.js wraps scan in try/catch so we just
    // verify that JSON.parse still works normally.
    const result = ctx.JSON.parse('{"a":1,"b":[1,2,3]}');
    expect(result).toEqual({ a: 1, b: [1, 2, 3] });
  });

  it('returns the parsed value untouched (passive observer)', () => {
    const ctx = createSandbox();
    const result = ctx.JSON.parse(
      '{"hls":"https://x.test/v.m3u8","other":42}'
    );
    expect(result).toEqual({ hls: 'https://x.test/v.m3u8', other: 42 });
  });

  it('handles primitive JSON values without crashing', () => {
    const ctx = createSandbox();
    expect(ctx.JSON.parse('null')).toBe(null);
    expect(ctx.JSON.parse('42')).toBe(42);
    expect(ctx.JSON.parse('"just a string"')).toBe('just a string');
    expect(ctx.JSON.parse('true')).toBe(true);
    expect(getManifestMessages(ctx)).toHaveLength(0);
  });

  it('detects URL embedded as primitive string', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('"https://x.test/inline.m3u8"');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://x.test/inline.m3u8');
  });

  it('bounds recursion depth (does not stack overflow on circular-shaped data)', () => {
    const ctx = createSandbox();
    // Build a deeply nested string via JSON. JSON itself can't be circular,
    // but we can build something pathologically deep (15 levels).
    let payload = '"end"';
    for (let i = 0; i < 15; i++) {
      payload = `{"d":${payload}}`;
    }
    // Should not throw, even though scan gives up at depth 10
    expect(() => ctx.JSON.parse(payload)).not.toThrow();
  });

  // --- v2.4.1: prefilter + scan budgets (Codex adversarial review #1) ---
  // The hook patches the page's native JSON.parse, so it runs on every
  // JSON parse the page does — including large benign API payloads with
  // no media URLs at all. Without a cheap prefilter and hard scan budgets
  // it can dominate page execution and cause user-visible jank/hangs.

  it('skips structural scan entirely when raw JSON text contains no media markers', () => {
    const ctx = createSandbox();
    // Build a 5k-entry benign object that would be expensive to walk.
    const big = { items: [] };
    for (let i = 0; i < 5000; i++) {
      big.items.push({
        id: i,
        name: 'item-' + i,
        desc: 'lorem ipsum dolor sit amet '.repeat(8),
        meta: { a: i, b: i * 2, c: 'foo-' + i },
      });
    }
    const text = JSON.stringify(big);
    ctx.JSON.parse(text);
    // No media URL → no detection (correctness).
    expect(getManifestMessages(ctx)).toHaveLength(0);
    // Prefilter must have fired — observable via stats counter.
    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats).toBeDefined();
    expect(stats.prefilter_skips).toBeGreaterThan(0);
    expect(stats.nodes_walked).toBe(0);
  });

  it('does NOT skip scan when raw JSON text contains a media marker', () => {
    const ctx = createSandbox();
    const payload = { items: [] };
    for (let i = 0; i < 50; i++) {
      payload.items.push({ id: i, name: 'item-' + i });
    }
    payload.video = { hls: 'https://cdn.x.test/master.m3u8' };
    ctx.JSON.parse(JSON.stringify(payload));

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.x.test/master.m3u8');
    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats.nodes_walked).toBeGreaterThan(0);
  });

  it('aborts scan after node budget is exhausted (does not walk pathological payloads)', () => {
    const ctx = createSandbox();
    // Decoy marker so prefilter passes; then a huge benign array that would
    // walk far past the node budget if no cap existed.
    const payload = { _decoy: 'looks-like.m3u8-but-not-a-url', items: [] };
    for (let i = 0; i < 50000; i++) {
      payload.items.push({ id: i, label: 'noise-' + i });
    }
    ctx.JSON.parse(JSON.stringify(payload));

    // Decoy is not a URL match (no http(s) prefix) → no detection.
    expect(getManifestMessages(ctx)).toHaveLength(0);
    const stats = ctx.window.__wv2nas_scan_stats;
    // Budget abort must have fired and nodes_walked must be bounded.
    expect(stats.budget_aborts).toBeGreaterThan(0);
    // Scan must have stopped well before walking all 50k items.
    expect(stats.nodes_walked).toBeLessThan(20000);
  });

  it('windows individual strings — pathologically large single string completes without hang', () => {
    const ctx = createSandbox();
    // Marker passes prefilter; giant benign string is single-pass scanned
    // (windowed) and yields zero matches. Validates we don't hang and we
    // don't false-positive on benign payloads.
    const huge = 'x'.repeat(2 * 1024 * 1024); // 2MB string
    const payload = { _marker: '.m3u8', blob: huge };
    ctx.JSON.parse(JSON.stringify(payload));

    expect(getManifestMessages(ctx)).toHaveLength(0);
    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats.calls).toBeGreaterThan(0);
  });

  // --- v2.4.1 round 3 (Codex adversarial review #3) -------------------
  // Two regressions vs. the original deepsearch behavior:
  //   1. Per-string hard-skip at 4096 bytes dropped real player-config
  //      blobs (10KB+ HTML/JSON strings with embedded m3u8 URLs).
  //   2. Oversized raw-text fallback used the structural-path regex,
  //      which excludes backslashes, so JSON-escaped URLs like
  //      "https:\/\/cdn\/v.m3u8" were silently missed.

  it('detects manifest URL embedded inside a long string field (>4KB)', () => {
    const ctx = createSandbox();
    // Build a 10KB player-config-like string with an embedded m3u8 URL.
    // This is the realistic shape Codex flagged: not a structural object
    // wrapping the URL, but a long string blob containing it.
    const filler = 'lorem ipsum dolor sit amet '.repeat(400); // ~10KB
    const blob = filler + ' source: "https://cdn.example.com/v/master.m3u8" ' + filler;
    const payload = { config: blob };
    ctx.JSON.parse(JSON.stringify(payload));

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/v/master.m3u8');
  });

  it('caps matches per long string to bound notify amplification', () => {
    const ctx = createSandbox();
    // Long string with many embedded URLs — must cap, not flood postMessage.
    let blob = 'pad '.repeat(200); // ~800 bytes prefix
    for (let i = 0; i < 200; i++) {
      blob += ' https://x.test/v' + i + '.m3u8 ';
    }
    const payload = { config: blob };
    ctx.JSON.parse(JSON.stringify(payload));

    const msgs = getManifestMessages(ctx);
    expect(msgs.length).toBeGreaterThan(0);
    expect(msgs.length).toBeLessThanOrEqual(16);
  });

  it('oversized raw fallback detects JSON-escaped manifest URLs', () => {
    const ctx = createSandbox();
    // >4MB raw with an escaped slash form. Many JSON encoders emit \/ for
    // forward slashes; the structural-path regex rejects backslashes, so
    // the raw fallback needs an escape-aware variant.
    const filler = '"' + 'p'.repeat(5 * 1024 * 1024) + '"';
    const escaped = '"https:\\/\\/cdn.example.com\\/v\\/master.m3u8"';
    const payload = '{"_pad": ' + filler + ', "u": ' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    // notify must emit the unescaped URL — not the literal \/-form.
    expect(msgs[0].url).toBe('https://cdn.example.com/v/master.m3u8');
    expect(msgs[0].format).toBe('m3u8');
  });

  // --- v2.4.1 round 4 (Codex adversarial review #4) -------------------
  // Under-threshold (≤4MB) JSON payloads with the manifest URL appearing
  // AFTER a large prefix array hit the node budget and used to silently
  // drop the URL. Solution: when ctx.aborted fires, fall back to the same
  // bounded raw-text scan we already use for oversized payloads.

  it('detects manifest URL appearing after node-budget abort via raw fallback', () => {
    const ctx = createSandbox();
    // Build 6000 small items so the structural walk burns through its
    // 5000-node budget before reaching `video.hls`. Total JSON is well
    // under SCAN_MAX_RAW_BYTES so the under-threshold abort path applies.
    const big = { items: [] };
    for (let i = 0; i < 6000; i++) {
      big.items.push({ id: i });
    }
    big.video = { hls: 'https://cdn.example.com/late/master.m3u8' };
    ctx.JSON.parse(JSON.stringify(big));

    const msgs = getManifestMessages(ctx);
    // URL must be detected even though the structural walk aborted before
    // it. Allow ≥1 (the late URL) — duplicates from early structural walk
    // are acceptable since downstream already handles them from fetch/XHR.
    expect(msgs.length).toBeGreaterThanOrEqual(1);
    expect(msgs.some((m) => m.url === 'https://cdn.example.com/late/master.m3u8')).toBe(true);
    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats.budget_aborts).toBeGreaterThan(0);
    expect(stats.raw_scans).toBeGreaterThan(0);
  });

  // --- v2.4.1 round 5 (Codex adversarial review #5) -------------------
  // inject.js runs in MAIN world, so window.__wv2nas_scan_stats was
  // page-writable. A hostile/buggy page that pre-locks that slot to null
  // / a primitive / a frozen object would make every stats mutation throw
  // — caught silently by the outer try/catch — and the entire JSON.parse
  // hook would stop running. Stats must live in closure-local state so
  // detection cannot be disabled from the page side.

  it('JSON manifest detection survives page predefining __wv2nas_scan_stats = null', () => {
    const ctx = createSandbox({ preStatsValue: null });
    ctx.JSON.parse('{"video":{"hls":"https://cdn.example.com/v/master.m3u8"}}');
    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/v/master.m3u8');
  });

  it('JSON manifest detection survives page predefining __wv2nas_scan_stats = primitive', () => {
    const ctx = createSandbox({ preStatsValue: 42 });
    ctx.JSON.parse('{"video":{"hls":"https://cdn.example.com/v/master.m3u8"}}');
    expect(getManifestMessages(ctx)).toHaveLength(1);
  });

  it('JSON manifest detection survives page locking __wv2nas_scan_stats with non-configurable frozen object', () => {
    const ctx = createSandbox({
      preStatsValue: Object.freeze({}),
      lockStatsSlot: true,
    });
    ctx.JSON.parse('{"video":{"hls":"https://cdn.example.com/v/master.m3u8"}}');
    expect(getManifestMessages(ctx)).toHaveLength(1);
  });

  it('JSON manifest detection survives page locking __wv2nas_scan_stats to null (non-configurable)', () => {
    const ctx = createSandbox({
      preStatsValue: null,
      lockStatsSlot: true,
    });
    ctx.JSON.parse('{"video":{"hls":"https://cdn.example.com/v/master.m3u8"}}');
    expect(getManifestMessages(ctx)).toHaveLength(1);
  });

  // --- v2.4.1 round 6 (Codex adversarial review #6) -------------------
  // Per-string match cap (8) + structural node budget (5000) leaves a
  // theoretical ceiling of ~40k notify() calls per JSON.parse. Each notify
  // is a synchronous postMessage. A payload with thousands of URL-bearing
  // string fields can still amplify into page jank or extension-spam even
  // though no single field exceeds the cap. Add a per-parse global emit
  // budget AND a per-parse URL dedup set, and stop scanning once the
  // global budget is exhausted.

  it('caps total notifications per JSON.parse across many string fields', () => {
    const ctx = createSandbox();
    const big = { items: [] };
    for (let i = 0; i < 5000; i++) {
      big.items.push({ url: 'https://x.test/v' + i + '.m3u8' });
    }
    ctx.JSON.parse(JSON.stringify(big));

    const msgs = getManifestMessages(ctx);
    expect(msgs.length).toBeGreaterThan(0);
    // TOTAL_EMIT_BUDGET = 256 (raised from 32 in round 7 to avoid
    // dropping the playable manifest in realistic player bootstraps).
    expect(msgs.length).toBeLessThanOrEqual(256);
    // Truncation telemetry must fire for an over-budget payload.
    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats.truncated).toBeGreaterThan(0);
  });

  it('dedupes the same manifest URL within a single JSON.parse', () => {
    const ctx = createSandbox();
    const big = { items: [] };
    for (let i = 0; i < 1000; i++) {
      // SAME URL repeated across 1000 fields — must collapse to 1 notify
      big.items.push({ url: 'https://x.test/duplicate.m3u8' });
    }
    ctx.JSON.parse(JSON.stringify(big));

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://x.test/duplicate.m3u8');
  });

  // --- v2.4.1 round 7 (Codex adversarial review #7) -------------------
  // Cap of 32 was order-dependent: a player bootstrap with 30+ unique
  // candidates (CDN alternates × qualities × audio/sub tracks) could
  // burn the budget before reaching the playable manifest. Raise the
  // cap to a value that comfortably covers real-world payloads, AND
  // surface truncation via stats so downstream knows when scanning
  // hit the wall.

  it('emits all unique URLs in a realistic 100-variant payload (no truncation)', () => {
    const ctx = createSandbox();
    // 100 unique URLs is plausible for a multi-CDN, multi-quality
    // bootstrap. ALL must be emitted, no truncation telemetry.
    const big = { variants: [] };
    for (let i = 0; i < 100; i++) {
      big.variants.push({ url: 'https://cdn.x.test/v' + i + '.m3u8' });
    }
    ctx.JSON.parse(JSON.stringify(big));

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(100);
    expect(ctx.window.__wv2nas_scan_stats.truncated).toBe(0);
  });

  // --- v2.4.1 round 8 (Codex adversarial review #8) -------------------
  // The oversized (>4MB) raw fallback had its own RAW_REGEX_MAX_MATCHES
  // cap of 16 — left over from round 2 and never aligned with the round-7
  // bump of TOTAL_EMIT_BUDGET to 256. A real bootstrap with 50+ candidate
  // URLs ahead of the playable master would silently drop the master.

  it('oversized raw fallback emits 50+ unique URLs including a late master URL', () => {
    const ctx = createSandbox();
    // >4MB raw with 50 candidate URLs followed by the actual playable
    // master. Pre-fix the fallback would stop at 16 and silently miss
    // the master URL the user actually needs.
    let urls = '';
    for (let i = 0; i < 50; i++) {
      urls += ',"c' + i + '":"https://cdn.x.test/c' + i + '.m3u8"';
    }
    urls += ',"master":"https://cdn.x.test/playable/master.m3u8"';
    const filler = '"' + 'p'.repeat(5 * 1024 * 1024) + '"';
    const payload = '{"_pad":' + filler + urls + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs.length).toBeGreaterThanOrEqual(50);
    expect(msgs.some((m) => m.url === 'https://cdn.x.test/playable/master.m3u8')).toBe(true);
  });

  // --- v2.4.1 round 9 (Codex adversarial review #9) -------------------
  // Two miss paths in the round 8 design:
  //   (a) String-scan truncates at 1MB without falling back to raw scan,
  //       so a 1-4MB embedded string with the URL past byte 1MB is
  //       silently dropped even though the raw text is under 4MB.
  //   (b) Raw-fallback iteration cap counts dedup hits, so a payload
  //       with 256+ duplicate URLs before a late unique URL stops at
  //       the iter cap and never reaches the late URL.

  it('detects manifest URL past byte 1MB in a multi-MB string field via raw fallback', () => {
    const ctx = createSandbox();
    // 3MB single string field with the URL placed past byte 1MB.
    // String scan windows at 1MB and would miss it. Total raw is under
    // 4MB so the structural-scan path runs (not the oversized path).
    const prefix = 'x'.repeat(2 * 1024 * 1024);  // 2MB of filler before URL
    const blob = prefix + ' source: "https://cdn.example.com/late/in-blob/master.m3u8" ' + 'y'.repeat(512 * 1024);
    const payload = { config: blob };
    ctx.JSON.parse(JSON.stringify(payload));

    const msgs = getManifestMessages(ctx);
    expect(msgs.some((m) => m.url === 'https://cdn.example.com/late/in-blob/master.m3u8')).toBe(true);
  });

  it('oversized raw fallback finds late unique URL despite many duplicate matches before it', () => {
    const ctx = createSandbox();
    // >4MB raw with 300 dedup-hits of the SAME URL followed by a unique
    // master. With the round 8 cap (RAW_REGEX_MAX_MATCHES = 256) the loop
    // stops after iterating 256 times — emitting only 1 URL — and never
    // reaches the master. After fix the iter cap counts CPU work, not
    // emissions, so dedup-heavy prefixes don't starve later unique URLs.
    let urls = '';
    for (let i = 0; i < 300; i++) {
      urls += ',"u' + i + '":"https://cdn.x.test/SAME.m3u8"';
    }
    urls += ',"master":"https://cdn.x.test/playable/master.m3u8"';
    const filler = '"' + 'p'.repeat(5 * 1024 * 1024) + '"';
    const payload = '{"_pad":' + filler + urls + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs.some((m) => m.url === 'https://cdn.x.test/playable/master.m3u8')).toBe(true);
    expect(msgs.some((m) => m.url === 'https://cdn.x.test/SAME.m3u8')).toBe(true);
  });

  it('still emits the late-arriving URL when count is below the cap', () => {
    const ctx = createSandbox();
    // 50 candidate URLs followed by the actual playable master URL late
    // in the payload — must surface it (33rd+ position used to drop).
    const big = { candidates: [] };
    for (let i = 0; i < 50; i++) {
      big.candidates.push({ url: 'https://cdn.x.test/c' + i + '.m3u8' });
    }
    big.master = { url: 'https://cdn.x.test/playable/master.m3u8' };
    ctx.JSON.parse(JSON.stringify(big));

    const msgs = getManifestMessages(ctx);
    expect(msgs.some((m) => m.url === 'https://cdn.x.test/playable/master.m3u8')).toBe(true);
    expect(ctx.window.__wv2nas_scan_stats.truncated).toBe(0);
  });

  it('does NOT dedupe across separate JSON.parse calls (per-parse only)', () => {
    const ctx = createSandbox();
    // Each parse has its own ctx → identical URLs in separate calls each
    // get their own notify. Cross-parse dedup is downstream's job; this
    // hook deliberately forwards every parse-fresh observation.
    ctx.JSON.parse('{"u":"https://x.test/same.m3u8"}');
    ctx.JSON.parse('{"u":"https://x.test/same.m3u8"}');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(2);
  });

  it('does NOT run raw fallback when structural walk completes within budget', () => {
    const ctx = createSandbox();
    // Small payload — completes well under SCAN_MAX_NODES → no abort,
    // no raw-fallback overhead.
    ctx.JSON.parse('{"video":{"hls":"https://cdn.example.com/short/master.m3u8"}}');
    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats.budget_aborts).toBe(0);
    expect(stats.raw_scans).toBe(0);
  });

  // --- v2.4.1 round 10 (Codex adversarial review #10) -----------------
  // Raw fallback only unescaped \/ and stopped at any other backslash.
  // Signed-URL CDNs commonly emit JSON-encoded forms like
  //   "https:\/\/cdn\/master.m3u8?Policy=abc&Signature=def"
  // where & is the JSON unicode escape for `&`. Pre-fix the URL
  // would be truncated at "?Policy=abc" — silently breaking download
  // because the auth signature is missing. Decoder must handle \uXXXX
  // alongside \/ in the raw fallback path.

  it('oversized raw fallback decodes \\u0026-escaped query separator in signed URLs', () => {
    const ctx = createSandbox();
    // String literal: '\\u0026' is the 6-char JSON escape sequence
    // (not the actual `&` char). After JSON.parse the parsed value
    // would have `&`, but the raw text contains the escape and that's
    // what the raw fallback scans on >4MB payloads.
    const filler = '"' + 'p'.repeat(5 * 1024 * 1024) + '"';
    const escaped = '"https:\\/\\/cdn.x.test\\/master.m3u8?Policy=abc\\u0026Signature=def"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.x.test/master.m3u8?Policy=abc&Signature=def');
  });

  it('oversized raw fallback decodes \\u002f-escaped slashes (Unicode form)', () => {
    const ctx = createSandbox();
    const filler = '"' + 'q'.repeat(5 * 1024 * 1024) + '"';
    // / is the Unicode escape for `/` — alternative encoding to \/
    const escaped = '"https:\\u002f\\u002fcdn.x.test\\u002fmaster.m3u8"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.x.test/master.m3u8');
  });

  it('oversized raw fallback decodes mixed \\/, \\u0026, and plain forms in one URL', () => {
    const ctx = createSandbox();
    const filler = '"' + 'r'.repeat(5 * 1024 * 1024) + '"';
    const escaped = '"https:\\/\\/cdn.x.test\\/path\\/master.m3u8?key1=v1\\u0026key2=v2\\u003dliteral"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    // = decodes to `=` — full reassembled URL must match
    expect(msgs[0].url).toBe('https://cdn.x.test/path/master.m3u8?key1=v1&key2=v2=literal');
  });

  // --- v2.4.1 round 11 (Codex adversarial review #11) -----------------
  // Prefilter only checked literal `.m3u8`/`.mpd` substrings. Some JSON
  // serializers escape the dot as ., so payloads under 4MB with
  // `.m3u8` in the raw text would parse cleanly to `.m3u8` but fail
  // the prefilter and skip the structural scan entirely. Allow either
  // literal `.` or `.` (case-insensitive) before m3u8/mpd.

  it('prefilter passes JSON with \\u002e-escaped extension marker (under-4MB path)', () => {
    const ctx = createSandbox();
    // Raw payload contains .m3u8 — no literal .m3u8 substring.
    // After JSON.parse the parsed string is `https://...master.m3u8`,
    // which the structural scan finds via MEDIA_URL_RE — but only if
    // the prefilter lets us through.
    const payload = '{"u":"https:\\/\\/cdn.example.com\\/master\\u002em3u8"}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/master.m3u8');
  });

  it('prefilter passes JSON with uppercase \\u002E (uppercase hex digit)', () => {
    const ctx = createSandbox();
    const payload = '{"u":"https:\\/\\/cdn.example.com\\/master\\u002EM3U8"}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/master.M3U8');
  });

  it('prefilter passes JSON with \\u002empd for DASH manifests', () => {
    const ctx = createSandbox();
    const payload = '{"u":"https:\\/\\/cdn.example.com\\/manifest\\u002empd"}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].format).toBe('mpd');
  });

  // --- v2.4.1 round 12 (Codex adversarial review #12) -----------------
  // Round 11 made the prefilter accept .m3u8 but the raw fallback
  // regex still required a literal `.` before the extension. Oversized
  // (>4MB) payloads with .m3u8 passed the prefilter, skipped the
  // structural walk (raw > 4MB), and then failed to match in
  // scanRawTextForMediaUrls — silently dropping the URL.

  it('oversized raw fallback matches .-escaped m3u8 extension', () => {
    const ctx = createSandbox();
    const filler = '"' + 'p'.repeat(5 * 1024 * 1024) + '"';
    // Raw text contains literal `.m3u8` — extension dot is escaped,
    // slashes use \/.
    const escaped = '"https:\\/\\/cdn.example.com\\/master\\u002em3u8"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/master.m3u8');
    expect(msgs[0].format).toBe('m3u8');
  });

  it('oversized raw fallback matches .-escaped mpd extension (uppercase hex)', () => {
    const ctx = createSandbox();
    const filler = '"' + 'q'.repeat(5 * 1024 * 1024) + '"';
    const escaped = '"https:\\/\\/cdn.example.com\\/manifest\\u002Empd"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/manifest.mpd');
    expect(msgs[0].format).toBe('mpd');
  });

  // --- v2.4.1 round 13 (Codex adversarial review #13) -----------------
  // Codex flagged that .-escaped `?` query delimiter is dropped by the
  // raw regex even though the URL body and extension-dot already accept
  // Unicode escapes. Took the chance to systematically audit every
  // literal char in RAW_MEDIA_URL_RE — also covering `:` (protocol
  // separator) and the query delimiter.

  it('oversized raw fallback emits full signed URL with .-escaped query delimiter', () => {
    const ctx = createSandbox();
    const filler = '"' + 'p'.repeat(5 * 1024 * 1024) + '"';
    // ? is escaped via ?. Body chars & = also escaped per
    // round 10. Pre-fix the regex would match only through `master.m3u8`
    // and drop the entire signed query, breaking auth on the downloader.
    const escaped = '"https:\\/\\/cdn.x.test\\/master.m3u8\\u003fPolicy=abc\\u0026Signature=def"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.x.test/master.m3u8?Policy=abc&Signature=def');
    expect(msgs[0].format).toBe('m3u8');
  });

  it('oversized raw fallback handles uppercase \\u003F query delimiter', () => {
    const ctx = createSandbox();
    const filler = '"' + 'q'.repeat(5 * 1024 * 1024) + '"';
    const escaped = '"https:\\/\\/cdn.x.test\\/master.m3u8\\u003FExpires=999"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.x.test/master.m3u8?Expires=999');
  });

  it('oversized raw fallback handles \\u003a-escaped protocol separator', () => {
    const ctx = createSandbox();
    const filler = '"' + 'r'.repeat(5 * 1024 * 1024) + '"';
    // : is escape for `:` — uncommon but realistic for paranoid encoders
    const escaped = '"https\\u003a\\/\\/cdn.x.test\\/master.m3u8"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.x.test/master.m3u8');
  });

  it('oversized raw fallback handles every-literal-char-as-Unicode-escape URL', () => {
    const ctx = createSandbox();
    const filler = '"' + 's'.repeat(5 * 1024 * 1024) + '"';
    // Aggressively-escaping serializer: : / . ? & all become \uXXXX.
    // Round 13 makes every literal in the regex escape-aware.
    const escaped =
      '"https\\u003a\\u002f\\u002fcdn.x.test\\u002fpath\\u002fmaster\\u002em3u8\\u003fk1=v1\\u0026k2=v2"';
    const payload = '{"_pad":' + filler + ',"u":' + escaped + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.x.test/path/master.m3u8?k1=v1&k2=v2');
  });

  it('oversized raw fallback also handles unescaped URLs (regression sanity)', () => {
    const ctx = createSandbox();
    const filler = '"' + 'q'.repeat(5 * 1024 * 1024) + '"';
    const payload = '{"_pad":' + filler + ',"u":"https://cdn.example.com/v/master.m3u8"}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/v/master.m3u8');
  });

  it('prefilter is case-insensitive (matches scanner regex /gi semantics)', () => {
    // Codex adversarial review (round 2): the MEDIA_URL_RE is case-insensitive,
    // so URLs ending in .M3U8 / .MPD were previously detectable. The raw-text
    // prefilter must preserve that semantics — otherwise a CDN that serves
    // uppercase extensions silently stops being detected.
    const ctx = createSandbox();
    ctx.JSON.parse('{"video":{"hls":"https://cdn.example.com/v/MASTER.M3U8"}}');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/v/MASTER.M3U8');
    expect(msgs[0].format).toBe('m3u8');
  });

  it('prefilter accepts uppercase MPD too', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('{"streams":[{"dash":"https://cdn.example.com/v/MANIFEST.MPD"}]}');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].format).toBe('mpd');
  });

  it('prefilter accepts mixed-case extensions', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('{"u":"https://cdn.example.com/v/index.M3u8?token=abc"}');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/v/index.M3u8?token=abc');
  });

  it('skips structural scan when raw JSON text exceeds max-bytes threshold', () => {
    const ctx = createSandbox();
    // A pathologically large raw string — prefilter on raw bytes must skip
    // the *structural* walk regardless of content. (The bounded raw-regex
    // fallback below covers the case where a real URL is embedded.)
    const huge = '"' + 'a'.repeat(8 * 1024 * 1024) + '.foo"'; // ~8MB raw, no media marker after path
    ctx.JSON.parse(huge);

    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats.prefilter_skips).toBeGreaterThan(0);
    expect(stats.nodes_walked).toBe(0);
  });

  // --- v2.4.1 round 2 (Codex adversarial review #2): bounded raw-regex
  // fallback for oversized JSON. The previous patch unconditionally skipped
  // any JSON.parse where raw text exceeded 4MB — which silently regressed
  // detection for large bootstrap/API payloads that happen to embed an
  // HLS/DASH URL. Compromise: above the structural-scan threshold we run
  // ONE pass of MEDIA_URL_RE on the raw string itself (no recursive walk,
  // no node-by-node allocations). This emits obvious URL matches while
  // preserving the page-jank protection that motivated the threshold.

  it('detects manifest URL embedded in oversized JSON via raw-regex fallback', () => {
    const ctx = createSandbox();
    // Build a >4MB JSON string with a real m3u8 URL embedded mid-payload.
    // Structural scan would be blocked by SCAN_MAX_RAW_BYTES; raw-regex
    // fallback must still surface the URL.
    const filler = '"' + 'x'.repeat(5 * 1024 * 1024) + '"'; // >4MB filler string
    const payload = '{"noise": ' + filler + ', "video": {"hls": "https://cdn.example.com/big/master.m3u8"}}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/big/master.m3u8');
    expect(msgs[0].format).toBe('m3u8');
    // Raw fallback was used (not structural).
    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats.raw_scans).toBeGreaterThan(0);
    expect(stats.nodes_walked).toBe(0);
  });

  it('raw-regex fallback is case-insensitive (matches scanner /gi semantics)', () => {
    const ctx = createSandbox();
    const filler = '"' + 'y'.repeat(5 * 1024 * 1024) + '"';
    const payload = '{"x": ' + filler + ', "u": "https://cdn.example.com/big/MASTER.M3U8"}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/big/MASTER.M3U8');
  });

  it('raw-regex fallback caps total notifications to bound runaway payloads', () => {
    const ctx = createSandbox();
    // Construct >4MB JSON with WAY more URLs than the cap — bounded fallback
    // must stop at the cap rather than emit thousands of postMessage calls.
    let urls = '';
    for (let i = 0; i < 500; i++) {
      urls += ',"u' + i + '":"https://x.test/m' + i + '.m3u8"';
    }
    const filler = '"' + 'z'.repeat(5 * 1024 * 1024) + '"';
    const payload = '{"_pad":' + filler + urls + '}';
    ctx.JSON.parse(payload);

    const msgs = getManifestMessages(ctx);
    // Must be capped at TOTAL_EMIT_BUDGET (256) — NOT all 500.
    expect(msgs.length).toBeGreaterThan(0);
    expect(msgs.length).toBeLessThanOrEqual(256);
  });

  it('raw-regex fallback is skipped when raw text has no media marker', () => {
    const ctx = createSandbox();
    // >4MB benign payload — neither structural nor raw scan should run.
    const filler = '"' + 'q'.repeat(5 * 1024 * 1024) + '"';
    const payload = '{"_pad":' + filler + ',"safe":"https://example.com/page"}';
    ctx.JSON.parse(payload);

    expect(getManifestMessages(ctx)).toHaveLength(0);
    const stats = ctx.window.__wv2nas_scan_stats;
    expect(stats.prefilter_skips).toBeGreaterThan(0);
    expect(stats.raw_scans).toBe(0);
    expect(stats.nodes_walked).toBe(0);
  });
});
