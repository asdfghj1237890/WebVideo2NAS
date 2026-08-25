// Pure sidepanel helpers shared between the UI script and unit tests.

(function installSidepanelCore(root) {
  if (!root || root.WV2NSidepanelCore) return;

  function parseTrustedCdnSuffixesInput(raw) {
    if (typeof raw !== 'string') return [];
    const seen = new Set();
    for (const part of raw.split(/[,\n]+/)) {
      let s = part.trim();
      if (!s) continue;
      if (s.includes('://')) {
        try { s = new URL(s).hostname; } catch (_e) { /* keep raw on parse fail */ }
      }
      s = s.replace(/^\.+/, '').toLowerCase();
      if (s) seen.add(s);
    }
    return Array.from(seen);
  }

  function deriveTrustedCdnSuffix(urlOrHost) {
    if (typeof urlOrHost !== 'string' || !urlOrHost) return null;
    const raw = urlOrHost.trim();
    if (!raw) return null;
    let host = raw;
    try {
      if (raw.includes('://')) {
        host = new URL(raw).hostname;
      } else if (raw.includes('/') || raw.includes(':')) {
        host = new URL(`https://${raw}`).hostname;
      }
    } catch (_e) {
      return null;
    }
    if (!host) return null;
    host = host.toLowerCase().replace(/^\.+|\.+$/g, '');
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) return null;
    if (host.startsWith('[')) return null;
    const parts = host.split('.').filter(Boolean);
    if (parts.length < 2) return null;
    if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(host)) return null;
    return host;
  }

  function hostMatchesAnyTrustedSuffix(host, suffixes) {
    if (!Array.isArray(suffixes) || suffixes.length === 0) return false;
    if (typeof host !== 'string' || !host) return false;
    const h = host.toLowerCase();
    for (const raw of suffixes) {
      if (typeof raw !== 'string') continue;
      const s = raw.trim().toLowerCase().replace(/^\.+/, '');
      if (!s) continue;
      if (h === s || h.endsWith('.' + s)) return true;
    }
    return false;
  }

  function extractQualitiesFromUrl(url) {
    const raw = String(url || '');
    const lower = raw.toLowerCase();
    if (!lower) return [];

    const allowed = new Set([2160, 1440, 1080, 720, 540, 480, 360, 240]);
    const found = new Set();

    const pMatches = lower.matchAll(/(?<![0-9])([0-9]{3,4})p(?![a-z0-9])/g);
    for (const m of pMatches) {
      const n = Number(m[1]);
      if (allowed.has(n)) found.add(n);
    }

    const qMatches = lower.matchAll(/[?&](?:res|resolution|quality|q|height|h)=([0-9]{3,4})\b/g);
    for (const m of qMatches) {
      const n = Number(m[1]);
      if (allowed.has(n)) found.add(n);
    }

    return Array.from(found).sort((a, b) => b - a).map((n) => `${n}p`);
  }

  function getMaxQualityNumber(url) {
    const qualities = extractQualitiesFromUrl(url);
    if (!qualities.length) return -1;
    let max = -1;
    for (const q of qualities) {
      const n = parseInt(String(q).replace(/[^0-9]/g, ''), 10);
      if (Number.isFinite(n) && n > max) max = n;
    }
    return max;
  }

  function formatDuration(totalSeconds) {
    const sec = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (n) => String(n).padStart(2, '0');
    if (h > 0) return `${pad(h)}:${pad(m)}:${pad(s)}`;
    return `${pad(m)}:${pad(s)}`;
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  function containsIpAddress(url) {
    const ipv4QueryPattern = /[?&]ip=(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/;
    return ipv4QueryPattern.test(url);
  }

  // ---- NAS identity, request generations, cancel outcomes ----------------
  //
  // Nine adversarial review passes found the same defect in five places: a
  // mutable global `settings` object read again when a response landed, by
  // which time a profile switch may have pointed it at a different NAS. Each
  // was patched individually and two of those patches introduced the next
  // one. The primitives below exist so the invariant is structural rather
  // than something every call site has to remember:
  //
  //   * a request cannot be issued without naming the NAS it is for;
  //   * a response cannot be applied without proving that NAS is still the
  //     current one and that no newer request superseded it;
  //   * cancel outcomes are a named transition table, not inline conditions.

  // A NAS's identity as a frozen value. Frozen because the whole bug class
  // came from identity that could change underneath an in-flight request.
  // Returns null when either half is missing — there is no such thing as a
  // half-configured target, and callers must handle its absence explicitly.
  function nasTarget(endpoint, apiKey) {
    const e = typeof endpoint === 'string' ? endpoint.trim() : '';
    const k = typeof apiKey === 'string' ? apiKey.trim() : '';
    if (!e || !k) return null;
    return Object.freeze({
      endpoint: e,
      apiKey: k,
      // Array form rather than a hand-rolled separator: no character has to
      // be assumed absent from a URL or a token.
      id: JSON.stringify([e, k]),
    });
  }

  function sameNasTarget(a, b) {
    return !!a && !!b && a.id === b.id;
  }

  // Per-stream generation counters. A stream is one logical sequence of
  // requests — 'jobs', 'connection', 'detected' — where only the newest
  // response may paint.
  //
  // begin() returns a predicate that takes the *current* target as an
  // argument rather than reading a global. That is deliberate: the caller has
  // to state what "current" means, which is exactly the step that was being
  // skipped when each of these races was written.
  function createRequestGuard() {
    const generations = new Map();
    return {
      begin(stream, target) {
        const next = (generations.get(stream) || 0) + 1;
        generations.set(stream, next);
        return function isStillCurrent(currentTarget) {
          return generations.get(stream) === next
            && sameNasTarget(target, currentTarget);
        };
      },
      // Test seam: how many requests a stream has issued.
      generationOf(stream) {
        return generations.get(stream) || 0;
      },
    };
  }

  // ---- Cancel outcomes ---------------------------------------------------
  //
  // The NAS commits a cancel before it answers, so a response — or the lack
  // of one — does not always say whether it took effect. Three outcomes, not
  // two.
  const CANCEL_OUTCOME = Object.freeze({
    CANCELLED: 'cancelled',   // definitely cancelled
    REJECTED: 'rejected',     // definitely not cancelled
    UNKNOWN: 'unknown',       // the answer does not tell us
  });

  // A reverse proxy can answer 504 after the backend committed, and the API
  // commits the status flip before it reads job metadata, so a post-commit
  // exception surfaces as 500 on a cancel that did happen. 408 and 429 are
  // equally uninformative. Deterministic 4xx really does mean "not
  // cancelled" — DELETE is idempotent, so an already-cancelled job answers
  // 2xx and a 404 is a genuinely unknown job.
  function classifyCancelResponse(status) {
    const code = Number(status);
    if (code >= 200 && code < 300) return CANCEL_OUTCOME.CANCELLED;
    if (code === 408 || code === 429 || code >= 500) return CANCEL_OUTCOME.UNKNOWN;
    return CANCEL_OUTCOME.REJECTED;
  }

  // A request that never produced a response says nothing either way.
  function classifyCancelFailure() {
    return CANCEL_OUTCOME.UNKNOWN;
  }

  // Folds an outcome and the reconciliation answer into the action to take.
  // reconciled is true / false / null, where null means "could not ask".
  // Only a confirmed cancellation may stop the browser-side upload: claiming
  // one that did not happen would strand a job the NAS is still running.
  function resolveCancelAction(outcome, reconciled) {
    if (outcome === CANCEL_OUTCOME.CANCELLED) return 'apply';
    if (outcome === CANCEL_OUTCOME.REJECTED) return 'report';
    return reconciled === true ? 'apply' : 'report';
  }

  root.WV2NSidepanelCore = Object.freeze({
    parseTrustedCdnSuffixesInput,
    deriveTrustedCdnSuffix,
    hostMatchesAnyTrustedSuffix,
    extractQualitiesFromUrl,
    getMaxQualityNumber,
    formatDuration,
    escapeHtml,
    containsIpAddress,
    nasTarget,
    sameNasTarget,
    createRequestGuard,
    CANCEL_OUTCOME,
    classifyCancelResponse,
    classifyCancelFailure,
    resolveCancelAction,
  });
}((typeof globalThis !== 'undefined' && globalThis) || (typeof window !== 'undefined' && window) || this));
