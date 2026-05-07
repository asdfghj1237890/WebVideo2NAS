// v2.5 browser-side segment downloader.
//
// Runs inside offscreen.html (long-lived, not subject to MV3 SW 30s idle).
// For each segment in a job plan:
//   1. fetch with credentials:'include' so user's cookies + session ride along
//   2. if AES-128 key URI present, fetch key + decrypt via SubtleCrypto
//   3. PUT plaintext bytes to NAS /api/jobs/{id}/segments/{seq}
//
// Concurrency capped to 6 (matches Chrome's per-host connection cap; higher
// values just queue at the network layer anyway). Retries each segment up
// to 3 times with exponential backoff before failing the whole job.
//
// Exports: runJob({...}) — single async entry. Throws on unrecoverable
// failure; caller (offscreen.js) routes the rejection back to the SW.

const DEFAULT_CONCURRENCY = 6;
const MAX_RETRIES = 3;
const BASE_BACKOFF_MS = 500;

// Per-segment timeout. Long enough for a sluggish CDN segment but short
// enough that a hung connection doesn't stall the whole job.
const SEGMENT_TIMEOUT_MS = 60_000;

// Codex review #18b: client-side payload caps. `arrayBuffer()` reads the
// whole response into memory before the server-side MAX_SEGMENT_BYTES
// can reject it, so a hostile manifest that points at a multi-GB
// response could exhaust the offscreen document's heap. These bounds
// are enforced BEFORE buffering — Content-Length check first, then a
// streaming reader that aborts at the byte cap.
//
// MAX_SEGMENT_BYTES mirrors the server default (api/main.py: 500 MB).
// MAX_KEY_BYTES is much tighter — AES-128 keys are 16 bytes; even
// allowing for header noise, anything past a few KB is wrong.
const MAX_SEGMENT_BYTES = 500 * 1024 * 1024;
const MAX_KEY_BYTES = 64 * 1024;


/**
 * Convert hex string ("0001..0f") to Uint8Array. Returns null on bad input.
 */
function hexToBytes(hex) {
  if (!hex || typeof hex !== 'string') return null;
  const cleaned = hex.startsWith('0x') || hex.startsWith('0X') ? hex.slice(2) : hex;
  if (cleaned.length === 0 || cleaned.length % 2 !== 0) return null;
  const out = new Uint8Array(cleaned.length / 2);
  for (let i = 0; i < out.length; i++) {
    const byte = parseInt(cleaned.slice(i * 2, i * 2 + 2), 16);
    if (Number.isNaN(byte)) return null;
    out[i] = byte;
  }
  return out;
}


/**
 * HLS sequence-number-derived IV: when EXT-X-KEY has no IV, RFC 8216 says
 * use the media sequence number as a 128-bit big-endian integer.
 */
function ivFromSequence(seq) {
  const iv = new Uint8Array(16);
  // big-endian; only fill the last 8 bytes (seq is at most 64-bit).
  const view = new DataView(iv.buffer);
  // High 32 bits stay zero (seq is well within 32-bit in practice).
  view.setUint32(8, Math.floor(seq / 0x100000000), false);
  view.setUint32(12, seq >>> 0, false);
  return iv;
}


/**
 * Codex review #9: decide whether `credentials: 'include'` is safe for
 * a given URL. The risk: a hostile or compromised manifest can list
 * segments at arbitrary origins (gmail.com, intranet auth pages, etc.).
 * Without scoping, the extension would send the user's session cookies
 * to those origins and — combined with DNR CORS relaxation — read
 * authenticated responses and upload them to NAS.
 *
 * Trust rule: include credentials only when the segment URL shares the
 * trustedBase URL's registrable-domain (approximated by hostname suffix
 * match). Cross-origin segments use `credentials: 'omit'` — the byte
 * stream still works because public CDN segments don't need auth, and
 * authenticated cross-origin segments would have been a leak vector
 * anyway.
 *
 * `trustedBase` is the resolved manifest URL (variant URL post-master-
 * resolve, or the user-submitted URL when no master chase happened).
 * Returns false on any URL parse error — fail-safe to omit credentials.
 */
function isTrustedForCredentials(segmentUrl, trustedBase) {
  if (!trustedBase) return false;
  let segHost, baseHost, segOrigin, baseOrigin;
  try {
    const seg = new URL(segmentUrl);
    const base = new URL(trustedBase);
    if (seg.protocol !== 'https:' && seg.protocol !== 'http:') return false;
    segHost = seg.hostname.toLowerCase();
    baseHost = base.hostname.toLowerCase();
    segOrigin = seg.origin;
    baseOrigin = base.origin;
  } catch (_) {
    return false;
  }
  // Exact origin match — always trusted.
  if (segOrigin === baseOrigin) return true;
  // Segment is a subdomain of the manifest's host: cdn.example.com is
  // trusted when manifest is on example.com. Strict: require literal
  // `.<base>` suffix (so example.com.attacker.com does NOT match
  // example.com).
  //
  // Codex adversarial-review: the inverse direction (manifest on a
  // subdomain claims trust over a parent host — e.g. manifest on
  // attacker.example.com pointing key/segment at example.com) is
  // REJECTED. Allowing it would let any compromised or
  // user-controlled subdomain turn the extension into an
  // authenticated cross-origin reader for the parent domain. We
  // can't compute eTLD+1 reliably without a Public Suffix List,
  // so we err on the side of NOT trusting upward; legitimate
  // player-on-subdomain / segs-on-apex streams either share an
  // origin or can be served from a same-or-subdomain CDN.
  if (segHost.endsWith('.' + baseHost)) return true;
  return false;
}


const FORBIDDEN_FETCH_HEADER_NAMES = new Set([
  'accept-charset',
  'accept-encoding',
  'access-control-request-headers',
  'access-control-request-method',
  'connection',
  'content-length',
  'cookie',
  'date',
  'dnt',
  'expect',
  'host',
  'keep-alive',
  'origin',
  'permissions-policy',
  'range',
  'referer',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
  'user-agent',
  'via',
  'x-http-method',
  'x-http-method-override',
  'x-method-override',
]);


function isForbiddenFetchHeaderName(name) {
  const lower = String(name || '').toLowerCase();
  return FORBIDDEN_FETCH_HEADER_NAMES.has(lower)
    || lower.startsWith('sec-')
    || lower.startsWith('proxy-');
}


/**
 * Codex adversarial-review: scope captured request headers to URLs
 * within the manifest's trust boundary. Cookies were already gated
 * via `isTrustedForCredentials`, but the captured `requestHeaders`
 * (Authorization, X-*, custom site tokens) used to ride on EVERY
 * segment/init/key fetch unconditionally. A malicious or compromised
 * manifest pointing a segment/key/init URI at attacker-controlled
 * `evil.com` would exfiltrate the original media-request bearer
 * tokens to that host. Server-side public-IP guards don't help —
 * the leak happens client-side, before any upload.
 *
 * Returns `requestHeaders` if `url` is the same origin as
 * `trustedBase`, or a deeper subdomain of it. For ANY other host,
 * returns an empty object. The DNR layer also withholds Referer /
 * Origin / UA spoofing for untrusted groups, so foreign origins never
 * receive captured auth headers or player-context spoof headers.
 *
 * Fail-safe: an untrusted segment that needed auth headers will
 * 401/403, surface a clean error, and the user can investigate. That
 * is strictly better than silently leaking credentials.
 */
function scopedRequestHeaders(url, trustedBase, requestHeaders) {
  if (!requestHeaders) return {};
  if (isTrustedForCredentials(url, trustedBase)) {
    const out = {};
    for (const [name, value] of Object.entries(requestHeaders)) {
      if (!isForbiddenFetchHeaderName(name) && value != null) {
        out[name] = String(value);
      }
    }
    return out;
  }
  return {};
}


function byteRangeHeader(byteRange) {
  if (!byteRange) return null;
  const offset = Number(byteRange.offset);
  const length = Number(byteRange.length);
  if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length)
      || offset < 0 || length <= 0) {
    throw new Error(
      `Invalid byte_range offset=${byteRange.offset} length=${byteRange.length}`
    );
  }
  const end = offset + length - 1;
  if (!Number.isSafeInteger(end) || end < offset) {
    throw new Error(
      `Invalid byte_range overflows safe integer: offset=${offset} length=${length}`
    );
  }
  return `bytes=${offset}-${end}`;
}


function mediaFetchHeaders(url, trustedBase, requestHeaders, byteRange) {
  const headers = scopedRequestHeaders(url, trustedBase, requestHeaders);
  const range = byteRangeHeader(byteRange);
  if (range) headers.Range = range;
  return headers;
}


function maxBytesForRange(byteRange) {
  if (!byteRange) return MAX_SEGMENT_BYTES;
  const length = Number(byteRange.length);
  return Math.min(MAX_SEGMENT_BYTES, length);
}


function assertRangeHonored(resp, bytes, byteRange, label) {
  if (!byteRange) return;
  if (resp.status !== 206) {
    throw new Error(`${label}: range request not honored (HTTP ${resp.status})`);
  }
  const expected = Number(byteRange.length);
  if (bytes.byteLength !== expected) {
    throw new Error(
      `${label}: range length mismatch, got ${bytes.byteLength}, expected ${expected}`
    );
  }
}


// In-memory cache so we only fetch each AES key once per job, not once per
// segment (HLS playlists typically rotate keys per playlist, not per
// segment, but per-segment metadata can still reference the same URI).
class KeyCache {
  constructor() {
    this.cache = new Map();
  }

  async getKey(keyUri, fetchHeaders, signal, trustedBase) {
    if (this.cache.has(keyUri)) return this.cache.get(keyUri);

    const promise = (async () => {
      // Codex review #9: scope credentials to manifest's trust domain.
      // Codex review (timeout-covers-body): use fetchBytesWithTimeout
      // so a stalled body read doesn't hang this key fetch forever.
      // Codex adversarial-review: also scope CAPTURED auth headers
      // to the trust boundary — sending Authorization/X-* tokens to
      // an attacker-controlled key URI would exfiltrate them.
      const { resp, bytes: buf } = await fetchBytesWithTimeout(keyUri, {
        credentials: isTrustedForCredentials(keyUri, trustedBase) ? 'include' : 'omit',
        headers: scopedRequestHeaders(keyUri, trustedBase, fetchHeaders),
        // Codex review (P1): refuse redirects. A trusted AES key URI
        // that 30x's to a foreign / private host would leak
        // credentials AND get its response (which we'd use as the
        // AES decrypt key!) read by the extension. `redirect: 'error'`
        // throws TypeError on any 30x; the existing key-fetch
        // failure path surfaces a clean job error.
        redirect: 'error',
      }, SEGMENT_TIMEOUT_MS, signal, MAX_KEY_BYTES, `key ${keyUri}`);
      if (!resp.ok) {
        throw new Error(`Key fetch failed (${resp.status}) for ${keyUri}`);
      }
      // Codex review #18b: cap key body at MAX_KEY_BYTES (64 KB). AES-
      // 128 keys are 16 bytes; the cap leaves slack for unexpected
      // encoding overhead but blocks a hostile key URI from streaming
      // gigabytes of garbage to fill memory.
      const keyMaterial = new Uint8Array(buf);
      if (keyMaterial.byteLength !== 16) {
        throw new Error(`Key length ${keyMaterial.byteLength} != 16 bytes for ${keyUri}`);
      }
      const cryptoKey = await crypto.subtle.importKey(
        'raw', keyMaterial, { name: 'AES-CBC' }, false, ['decrypt'],
      );
      return cryptoKey;
    })();

    this.cache.set(keyUri, promise);
    return promise;
  }
}


/**
 * Codex review #18b: read response body with a hard byte cap, streaming
 * via ReadableStream and aborting on overflow.
 *
 * Behavior:
 *   1. Reject upfront if Content-Length declares a value > maxBytes.
 *   2. If the body exposes `getReader()` (real browser fetch),
 *      accumulate chunks while running a byte counter — cancel the
 *      reader and throw if total exceeds maxBytes mid-stream.
 *   3. Fallback: if no streaming API (test mocks), call arrayBuffer()
 *      and re-check. This still bounds memory at one full response,
 *      but at least catches obviously oversized bodies.
 *
 * Returns an ArrayBuffer the caller can pass to crypto.subtle / fetch.
 */
async function readBodyWithCap(resp, maxBytes, label) {
  // Upfront Content-Length gate — saves the wire bytes when the server
  // is honest about its size.
  const declaredHdr = resp.headers && typeof resp.headers.get === 'function'
    ? resp.headers.get('content-length')
    : null;
  if (declaredHdr) {
    const declared = parseInt(declaredHdr, 10);
    if (Number.isFinite(declared) && declared > maxBytes) {
      // Cancel body so the socket / connection is freed promptly.
      try {
        if (resp.body && typeof resp.body.cancel === 'function') {
          resp.body.cancel();
        }
      } catch (_) { /* best-effort */ }
      throw new Error(
        `${label}: Content-Length ${declared} exceeds cap ${maxBytes}`
      );
    }
  }

  // Streaming path — preferred. Production Chrome always exposes
  // resp.body as a ReadableStream.
  if (resp.body && typeof resp.body.getReader === 'function') {
    const reader = resp.body.getReader();
    const chunks = [];
    let total = 0;
    try {
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (!value || value.byteLength === 0) continue;
        total += value.byteLength;
        if (total > maxBytes) {
          try { reader.cancel(); } catch (_) { /* best-effort */ }
          throw new Error(
            `${label}: response exceeded cap ${maxBytes} bytes mid-stream`
          );
        }
        chunks.push(value);
      }
    } catch (err) {
      try { reader.cancel(); } catch (_) { /* best-effort */ }
      throw err;
    }
    const out = new Uint8Array(total);
    let offset = 0;
    for (const c of chunks) {
      out.set(c, offset);
      offset += c.byteLength;
    }
    return out.buffer;
  }

  // Fallback for test environments where the mock doesn't provide a
  // ReadableStream. Still post-checks size.
  if (typeof resp.arrayBuffer !== 'function') {
    throw new Error(`${label}: response has neither body.getReader nor arrayBuffer`);
  }
  const buf = await resp.arrayBuffer();
  if (buf.byteLength > maxBytes) {
    throw new Error(
      `${label}: response size ${buf.byteLength} exceeds cap ${maxBytes}`
    );
  }
  return buf;
}


async function fetchWithTimeout(url, opts, timeoutMs, externalSignal) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  // Chain external signal so caller-driven cancel still propagates.
  const onAbort = () => ctrl.abort();
  if (externalSignal) {
    if (externalSignal.aborted) {
      ctrl.abort();
    } else {
      externalSignal.addEventListener('abort', onAbort);
    }
  }
  try {
    return await fetch(url, { ...opts, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
    if (externalSignal) externalSignal.removeEventListener('abort', onAbort);
  }
}


/**
 * Codex review: `fetchWithTimeout` clears the abort timer as soon as
 * `fetch()` resolves — but `fetch()` only awaits headers, not the
 * response body. A CDN that sends headers and then stalls mid-body
 * leaves `readBodyWithCap()` blocked forever, so one concurrency
 * worker hangs and the whole job neither finalizes nor aborts.
 *
 * This helper keeps the timer alive across BOTH the fetch and the
 * body read by routing both through the same AbortController. When
 * the timer fires, the underlying connection's reader.cancel()
 * unblocks `readBodyWithCap`, the read errors out, and `withRetry`
 * gets a clean "AbortError" it can retry or surface.
 *
 * Returns `{ resp, bytes }`. Caller checks `resp.ok` first; on
 * non-ok, `bytes` is null and the body has been cancelled to free
 * the socket promptly.
 */
async function fetchBytesWithTimeout(url, opts, timeoutMs, externalSignal, maxBytes, label) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const onAbort = () => ctrl.abort();
  if (externalSignal) {
    if (externalSignal.aborted) {
      ctrl.abort();
    } else {
      externalSignal.addEventListener('abort', onAbort);
    }
  }
  try {
    const resp = await fetch(url, { ...opts, signal: ctrl.signal });
    if (!resp.ok) {
      // Free the socket; body content is irrelevant for non-ok.
      try {
        if (resp.body && typeof resp.body.cancel === 'function') {
          resp.body.cancel();
        }
      } catch (_) { /* best-effort */ }
      return { resp, bytes: null };
    }
    // Body read still bounded by `timer` — if the CDN stalls mid-body,
    // ctrl.abort() fires and the underlying reader errors out.
    const bytes = await readBodyWithCap(resp, maxBytes, label);
    return { resp, bytes };
  } finally {
    clearTimeout(timer);
    if (externalSignal) externalSignal.removeEventListener('abort', onAbort);
  }
}


async function decryptIfNeeded(bytes, segment, keyCache, requestHeaders, signal, trustedBase) {
  if (!segment.key || !segment.key.uri) return bytes;

  const cryptoKey = await keyCache.getKey(segment.key.uri, requestHeaders, signal, trustedBase);
  let iv;
  if (segment.key.iv) {
    iv = hexToBytes(segment.key.iv);
    if (!iv || iv.length !== 16) {
      throw new Error(`Invalid IV for seq=${segment.seq}: ${segment.key.iv}`);
    }
  } else {
    iv = ivFromSequence(segment.sequence ?? segment.seq ?? 0);
  }
  return await crypto.subtle.decrypt({ name: 'AES-CBC', iv }, cryptoKey, bytes);
}


/**
 * Try a network request up to MAX_RETRIES times with exponential backoff.
 * Re-raises the final error if all attempts fail.
 */
async function withRetry(fn, label, signal) {
  let lastErr;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    if (signal && signal.aborted) {
      throw new Error(`${label}: cancelled`);
    }
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      // Don't retry on explicit abort.
      if (err && err.name === 'AbortError' && signal && signal.aborted) throw err;
      if (attempt < MAX_RETRIES - 1) {
        const delay = BASE_BACKOFF_MS * Math.pow(2, attempt);
        await new Promise((r) => setTimeout(r, delay));
      }
    }
  }
  throw new Error(`${label} failed after ${MAX_RETRIES} attempts: ${lastErr?.message ?? lastErr}`);
}


/**
 * Codex review #19b: build a NAS API URL that preserves any path
 * prefix on `nasEndpoint`.
 *
 * Earlier code used `new URL('/api/jobs/...', nasEndpoint)`. The
 * leading slash makes the path absolute at the origin root, so a
 * reverse-proxied endpoint like `https://host/webvideo2nas` would
 * have segment uploads land at `https://host/api/...`, missing the
 * `/webvideo2nas` prefix entirely. The /api/download path elsewhere
 * uses string concatenation (which preserves the prefix), so the
 * inconsistency only bites browser-side downloads.
 *
 * Fix: normalize the endpoint (strip trailing slashes), then append a
 * relative path. Works for `https://host`, `https://host/`,
 * `https://host/webvideo2nas`, and `https://host/webvideo2nas/`.
 */
function buildNasUrl(nasEndpoint, path, query) {
  const base = String(nasEndpoint || '').replace(/\/+$/, '');
  const tail = path.startsWith('/') ? path : '/' + path;
  let url = base + tail;
  if (query && Object.keys(query).length > 0) {
    const usp = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v != null) usp.set(k, String(v));
    }
    const qs = usp.toString();
    if (qs) url += (url.includes('?') ? '&' : '?') + qs;
  }
  return url;
}


async function uploadSegment({ nasEndpoint, apiKey, jobId, track, seq, bytes, signal }) {
  // Codex review #19b: build URL via buildNasUrl so that
  // reverse-proxied NAS endpoints (e.g. https://host/webvideo2nas)
  // keep their path prefix. The previous `new URL('/api/...', endpoint)`
  // form discarded the prefix and POSTed to the wrong root.
  const url = buildNasUrl(
    nasEndpoint,
    `/api/jobs/${encodeURIComponent(jobId)}/segments/${encodeURIComponent(seq)}`,
    { track },
  );
  const resp = await fetchWithTimeout(url, {
    method: 'PUT',
    headers: {
      'Authorization': `Bearer ${apiKey}`,
      'Content-Type': 'application/octet-stream',
    },
    body: bytes,
  }, SEGMENT_TIMEOUT_MS, signal);
  if (!resp.ok) {
    throw new Error(`PUT segment failed (${resp.status}): ${(await resp.text()).slice(0, 200)}`);
  }
}


async function uploadInit({ nasEndpoint, apiKey, jobId, track, bytes, signal }) {
  // Codex review #19b: same prefix-preserving fix as uploadSegment.
  const url = buildNasUrl(
    nasEndpoint,
    `/api/jobs/${encodeURIComponent(jobId)}/init`,
    { track },
  );
  const resp = await fetchWithTimeout(url, {
    method: 'PUT',
    headers: {
      'Authorization': `Bearer ${apiKey}`,
      'Content-Type': 'application/octet-stream',
    },
    body: bytes,
  }, SEGMENT_TIMEOUT_MS, signal);
  if (!resp.ok) {
    throw new Error(`PUT init failed (${resp.status}): ${(await resp.text()).slice(0, 200)}`);
  }
}


async function processOneSegment({
  segment, track, jobId, nasEndpoint, apiKey, requestHeaders, keyCache, signal,
  onProgress, trustedBase,
}) {
  await withRetry(async () => {
    // Codex review #9: only ride user session credentials when the
    // segment is on the manifest's trust domain.
    // Codex review (timeout-covers-body): fetchBytesWithTimeout keeps
    // the abort timer active across the body read, so a CDN that
    // stalls mid-stream after sending headers can't pin the offscreen
    // document indefinitely.
    // Codex review #18b: bounded streaming read. Server-side
    // MAX_SEGMENT_BYTES enforcement only triggers on PUT, which
    // happens AFTER the full body is buffered client-side. Without
    // this client cap, a hostile manifest could pin the offscreen
    // document to a multi-GB segment URL and exhaust the heap before
    // the server ever rejects it.
    // Codex adversarial-review: scope captured auth headers per-URL.
    // A malicious manifest pointing segment.url at evil.com used to
    // get Authorization / X-* bearer tokens; now untrusted hosts
    // receive only DNR-spoofed Referer/Origin/UA, never captured
    // auth.
    const { resp, bytes: rawBytes } = await fetchBytesWithTimeout(segment.url, {
      credentials: isTrustedForCredentials(segment.url, trustedBase) ? 'include' : 'omit',
      headers: mediaFetchHeaders(segment.url, trustedBase, requestHeaders, segment.byte_range),
      // Codex review (P1): refuse redirects. The trust decision was
      // made for `segment.url`; a 30x to a different host bypasses
      // that and would leak captured auth headers + cookies to the
      // redirect target. withRetry surfaces the resulting TypeError
      // as a segment-fetch failure.
      redirect: 'error',
    }, SEGMENT_TIMEOUT_MS, signal, maxBytesForRange(segment.byte_range), `segment ${segment.url}`);
    if (!resp.ok) {
      throw new Error(`fetch ${segment.url} -> ${resp.status}`);
    }
    assertRangeHonored(resp, rawBytes, segment.byte_range, `segment ${segment.url}`);
    let bytes = rawBytes;
    bytes = await decryptIfNeeded(bytes, segment, keyCache, requestHeaders, signal, trustedBase);
    await uploadSegment({
      nasEndpoint, apiKey, jobId, track, seq: segment.seq, bytes, signal,
    });
  }, `segment ${track}/${segment.seq}`, signal);

  if (onProgress) onProgress({ track, seq: segment.seq });
}


async function processInitSegment({
  initUrl, byteRange, track, jobId, nasEndpoint, apiKey, requestHeaders, signal, trustedBase,
}) {
  if (!initUrl) return;
  await withRetry(async () => {
    // Codex review (timeout-covers-body): fetchBytesWithTimeout keeps
    // the abort timer active through the body read so a stalled init
    // download doesn't hang this concurrency worker.
    // Codex review #18b: bounded read for init segment — same attack
    // surface as media segments.
    // Codex adversarial-review: same per-URL header scoping as media
    // segments — init URI on a foreign origin must NOT receive the
    // captured auth headers.
    const { resp, bytes } = await fetchBytesWithTimeout(initUrl, {
      credentials: isTrustedForCredentials(initUrl, trustedBase) ? 'include' : 'omit',
      headers: mediaFetchHeaders(initUrl, trustedBase, requestHeaders, byteRange),
      // Codex review (P1): refuse redirects — same rationale as
      // segment / key fetches. Trust was decided for `initUrl`;
      // a 30x bypasses that boundary.
      redirect: 'error',
    }, SEGMENT_TIMEOUT_MS, signal, maxBytesForRange(byteRange), `init ${initUrl}`);
    if (!resp.ok) {
      throw new Error(`init fetch ${initUrl} -> ${resp.status}`);
    }
    assertRangeHonored(resp, bytes, byteRange, `init ${initUrl}`);
    await uploadInit({
      nasEndpoint, apiKey, jobId, track, bytes, signal,
    });
  }, `init ${track}`, signal);
}


/**
 * Cooperative concurrency limiter. Schedules `tasks` fanned out over
 * `concurrency` workers, fails fast on first error.
 */
async function runWithConcurrency(tasks, concurrency) {
  const results = [];
  let cursor = 0;
  const errors = [];
  let aborted = false;

  async function worker() {
    while (cursor < tasks.length && !aborted) {
      const i = cursor++;
      try {
        results[i] = await tasks[i]();
      } catch (err) {
        errors.push(err);
        aborted = true;
      }
    }
  }

  const workers = [];
  for (let i = 0; i < Math.min(concurrency, tasks.length); i++) {
    workers.push(worker());
  }
  await Promise.all(workers);

  if (errors.length > 0) throw errors[0];
  return results;
}


/**
 * Run a single browser-side job from start to finish.
 *
 * @param {Object} opts
 * @param {string} opts.jobId
 * @param {string} opts.nasEndpoint - e.g. "http://192.168.1.100:52052"
 * @param {string} opts.apiKey
 * @param {Object} opts.plan - server-returned plan (tracks, segments, etc.)
 * @param {Object} [opts.requestHeaders] - sent on segment fetches (Referer/UA/etc)
 * @param {AbortSignal} [opts.signal] - cancellation signal
 * @param {Function} [opts.onProgress] - called as ({track, seq}) per segment done
 * @param {number} [opts.concurrency=6]
 */
export async function runJob({
  jobId, nasEndpoint, apiKey, plan,
  requestHeaders = {}, signal, onProgress,
  concurrency = DEFAULT_CONCURRENCY,
} = {}) {
  if (!jobId || !nasEndpoint || !apiKey || !plan) {
    throw new Error('runJob: missing required args');
  }

  const keyCache = new KeyCache();
  const tracks = plan.tracks || {};
  const trackNames = Object.keys(tracks);
  if (trackNames.length === 0) throw new Error('runJob: plan has no tracks');

  // Codex review #9: derive the trust base for credential scoping. The
  // resolved variant URL takes precedence (most specific), falling back
  // to the plan's source_url. Anything not matching this URL's
  // registrable domain gets credentials: 'omit' on fetch.
  const trustedBase = plan.selected_variant_url || plan.source_url || null;

  // Codex review #4: tracks whether we have already kicked off the
  // finalize POST. Once that fetch leaves the wire, the server may have
  // already processed the request even if our await throws (timeout,
  // network drop). The caller uses this flag to decide whether calling
  // /api/jobs/{id}/abort is safe — aborting a queued job destroys
  // staged data, so we only abort when finalize was never attempted.
  let finalizeAttempted = false;

  try {
    // 1) Init segments first (so they're definitely on disk before media).
    for (const trackName of trackNames) {
      const initUrl = tracks[trackName].init_segment_url || (trackName === 'video' ? plan.init_segment_url : null);
      const initByteRange = tracks[trackName].init_segment_byte_range
        || (trackName === 'video' ? plan.init_segment_byte_range : null);
      if (initUrl) {
        await processInitSegment({
          initUrl, byteRange: initByteRange, track: trackName, jobId, nasEndpoint, apiKey, requestHeaders, signal,
          trustedBase,
        });
      }
    }

    // 2) Media segments — flatten all tracks into one task list so concurrency
    //    is shared across video+audio rather than serialised per-track.
    const tasks = [];
    for (const trackName of trackNames) {
      const segs = tracks[trackName].segments || [];
      for (const segment of segs) {
        tasks.push(() => processOneSegment({
          segment, track: trackName, jobId, nasEndpoint, apiKey,
          requestHeaders, keyCache, signal, onProgress, trustedBase,
        }));
      }
    }

    await runWithConcurrency(tasks, concurrency);

    // 3) Finalize — tells the worker to start ffmpeg mux. From here on
    // the server has the chance to have committed; ANY rejection past
    // this set must not trigger an abort/staging-wipe.
    //
    // Codex review #17: retry transient 5xx (Redis blip, queue push
    // failure, connection reset). The server's resume-from-
    // browser_finalizing logic makes finalize idempotent — re-calling
    // it after a 500 just re-runs verify + rpush. Without retry, a
    // single transient outage strands a fully-uploaded job at
    // browser_finalizing until the 6h stale reaper.
    // Codex adversarial-review: `finalizeAttempted` is the SW's
    // signal for whether the abort/cleanup path may run. The previous
    // code set it to true unconditionally before the POST, treating
    // any rejection past this line as ambiguous (might-have-committed).
    // That is too coarse: a RECEIVED 4xx response means the server
    // got the request and rejected it pre-commit:
    //   - 409 from finalize_browser_job's verify branch explicitly
    //     rolls the job back to 'browser_uploading' before re-raising.
    //   - 409 "Job state X cannot be finalized" never enqueues.
    //   - 400/401/403/404 short-circuit before any state change.
    // In all 4xx cases the server row + staging are still ours to
    // abort, so we leave `finalizeAttempted = false` and let the SW
    // call /abort. Reserve the suppression for genuine ambiguity:
    // 5xx (response could come from after rpush) and network/timeout
    // (request status unknown). The variable starts false here and
    // only goes true if we hit one of those ambiguous paths.
    let lastStatus = 0;
    let lastNetworkErr = null;
    const FINALIZE_MAX_ATTEMPTS = 3;
    let succeeded = false;
    for (let attempt = 0; attempt < FINALIZE_MAX_ATTEMPTS; attempt++) {
      let resp;
      try {
        if (signal && signal.aborted) {
          throw new Error('Finalize cancelled before request was sent');
        }
        // Mark the attempt as ambiguous BEFORE the await — if the SW
        // worker dies between this line and resp coming back, the
        // request might still be in flight server-side.
        finalizeAttempted = true;
        // Codex review #19b: same prefix-preserving fix.
        resp = await fetchWithTimeout(
          buildNasUrl(
            nasEndpoint,
            `/api/jobs/${encodeURIComponent(jobId)}/finalize`,
          ),
          {
            method: 'POST',
            headers: {
              'Authorization': `Bearer ${apiKey}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({}),
          },
          SEGMENT_TIMEOUT_MS,
          signal,
        );
      } catch (netErr) {
        // Network error, timeout. Re-throw on cancellation.
        if (signal && signal.aborted) throw netErr;
        // finalizeAttempted stays true — request status genuinely
        // unknown.
        lastNetworkErr = netErr;
        if (attempt < FINALIZE_MAX_ATTEMPTS - 1) {
          await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
          continue;
        }
        throw netErr;
      }
      if (resp.ok) {
        succeeded = true;
        break;
      }
      lastStatus = resp.status;
      // 4xx: NOT transient AND server received-and-rejected pre-commit.
      // Downgrade finalizeAttempted so the SW can call /abort and
      // reclaim staging. The verify-failure case (409) needs the
      // body so the user sees what's missing.
      if (resp.status < 500) {
        finalizeAttempted = false;
        throw new Error(
          `Finalize failed (${resp.status}): ${(await resp.text()).slice(0, 200)}`
        );
      }
      // 5xx: ambiguous — the response could come from after rpush.
      // Keep finalizeAttempted=true; retry with linear backoff (1s, 2s).
      if (attempt < FINALIZE_MAX_ATTEMPTS - 1) {
        await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
      }
    }
    if (!succeeded) {
      throw new Error(
        `Finalize failed after ${FINALIZE_MAX_ATTEMPTS} attempts ` +
        `(last status ${lastStatus || 'network error'}` +
        (lastNetworkErr ? `: ${lastNetworkErr.message}` : '') +
        ')'
      );
    }

    return { jobId, totalSegments: tasks.length };
  } catch (err) {
    // Annotate so offscreen → SW message-passing can carry the flag
    // through. Plain Error / TypeError both accept arbitrary string
    // properties; if err is not an object (a thrown string, etc.) we
    // wrap it.
    if (err && typeof err === 'object') {
      err.finalizeAttempted = finalizeAttempted;
      throw err;
    }
    const wrapped = new Error(String(err));
    wrapped.finalizeAttempted = finalizeAttempted;
    throw wrapped;
  }
}


// Internals exposed for testing.
export const _internals = {
  hexToBytes, ivFromSequence, withRetry, runWithConcurrency,
  KeyCache, isTrustedForCredentials, scopedRequestHeaders,
  byteRangeHeader, mediaFetchHeaders,
  readBodyWithCap, buildNasUrl,
  fetchWithTimeout, fetchBytesWithTimeout,
  MAX_RETRIES, BASE_BACKOFF_MS, DEFAULT_CONCURRENCY,
  MAX_SEGMENT_BYTES, MAX_KEY_BYTES, SEGMENT_TIMEOUT_MS,
};
