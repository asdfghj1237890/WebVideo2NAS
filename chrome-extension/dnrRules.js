// v2.5 declarativeNetRequest helpers.
//
// When the extension fetches segments in browser context, we need the
// outgoing requests' Referer / Origin / User-Agent to match what the player
// sent — otherwise hotlink-protected CDNs return 403. webRequest can OBSERVE
// headers but not modify them in MV3; declarativeNetRequest can.
//
// We use SESSION rules (not dynamic rules) because:
//   - Session rules don't survive SW restart, which is exactly what we want:
//     a stale rule from a crashed prior job shouldn't keep spoofing.
//   - The MV3 rule limit for session is generous (5000) and per-extension.
//
// Lifecycle: build rules → install via updateSessionRules({addRules}) →
// segmentDownloader fetches → cleanupRules({removeRuleIds}) in finally.

// IDs allocated from this base. Keep clear of other extension features by
// using a dedicated band; the runtime errors loudly on collision so we'll
// notice if we ever clash. Per Codex review #2, callers running parallel
// jobs MUST pass distinct `idBase` values via buildHeaderRules({...,
// idBase}); a job-local slot allocator is the caller's responsibility
// (background.js maintains _wv2nasUsedDnrSlots for that purpose).
export const RULE_ID_BASE = 10_000;

const RESPONSE_RULE_ID_OFFSET = 50;

// Headers we'll *override on the request* (so the spoof is what reaches CDN).
// Cookie is intentionally NOT in this list — Cookie is auto-attached by
// the browser from cookie jar based on URL+credentials, and DNR-overriding
// it would be both noisy and unnecessary.
const REQUEST_OVERRIDE_HEADERS = ['referer', 'origin', 'user-agent'];

/**
 * Convert a list of segment URLs to a list of regex patterns suitable for
 * DNR `regexFilter`. We use one regex per URL prefix (origin + path-up-to-
 * filename) so the rule applies to the whole batch without bloating the
 * rule count.
 *
 * Returns an array of { origin: string, regexFilter: string } for the
 * unique (origin, base path) combinations.
 */
export function urlsToRegexFilters(urls) {
  const groups = new Map();
  for (const u of urls) {
    let parsed;
    try {
      parsed = new URL(u);
    } catch (_e) {
      continue;
    }
    const origin = `${parsed.protocol}//${parsed.host}`;
    // Use the directory part of the path; segments usually share a parent.
    const dir = parsed.pathname.replace(/[^/]*$/, '');
    const key = origin + dir;
    if (!groups.has(key)) {
      groups.set(key, { origin, dir });
    }
  }

  const filters = [];
  for (const { origin, dir } of groups.values()) {
    // Escape regex specials in the prefix.
    const escaped = (origin + dir).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    filters.push({ origin, regexFilter: `^${escaped}.*` });
  }
  return filters;
}

/**
 * Build DNR session rules to spoof Referer / Origin / User-Agent on segment
 * fetches AND relax CORS on the responses (so opaque-mode-fetch isn't
 * needed and we can read response.bodyData for AES decrypt).
 *
 * Codex review #10: CORS-relax rules ONLY emit for `trustedSegmentUrls`,
 * a subset of `segmentUrls` whose origins share the manifest's trust
 * domain. Foreign origins get neither header-spoof nor CORS relax. That
 * prevents captured Referer/Origin/UA leakage and defeats DNS-rebinding
 * exfil: even if a hostile manifest's URL flips between public and
 * private IPs across server-side check vs browser fetch, the browser
 * receives an unreadable cross-origin response.
 *
 * @param {Object} opts
 * @param {string[]} opts.segmentUrls - URLs that get header-spoof rules
 * @param {string[]} [opts.trustedSegmentUrls] - subset of segmentUrls
 *   whose origins are trusted (same registrable-domain as manifest);
 *   only these get CORS-relax. Default: all of segmentUrls (back-compat
 *   for single-trust-level callers; tests rely on this default).
 * @param {string} [opts.referer]
 * @param {string} [opts.origin]
 * @param {string} [opts.userAgent]
 * @param {number} [opts.idBase=RULE_ID_BASE]
 * @param {string} [opts.initiatorDomain] - Codex review #11: when set,
 *   each rule's condition gets `initiatorDomains: [initiatorDomain]`
 *   so the rule only matches requests originating from that hostname
 *   (typically the extension's own ID, i.e. `chrome.runtime.id`,
 *   producing chrome-extension://<id>/ initiator). Without this, the
 *   CORS-relax rules apply to ANY tab in the browser fetching a URL
 *   the regex matches — a malicious page that learns a CDN URL during
 *   the download window could read responses cross-origin.
 * @returns {{rules: chrome.declarativeNetRequest.Rule[], ruleIds: number[]}}
 */
export function buildHeaderRules({
  segmentUrls = [], trustedSegmentUrls, referer, origin, userAgent,
  idBase = RULE_ID_BASE, initiatorDomain,
} = {}) {
  const allFilters = urlsToRegexFilters(segmentUrls);
  if (allFilters.length === 0) return { rules: [], ruleIds: [] };

  // Default: every URL is trusted (back-compat for callers that don't
  // do per-origin trust analysis — single-trust-level deployments).
  const trustedSet = new Set(
    urlsToRegexFilters(trustedSegmentUrls === undefined ? segmentUrls : trustedSegmentUrls)
      .map((f) => f.regexFilter)
  );

  const requestHeaderActions = [];
  if (referer) {
    requestHeaderActions.push({ header: 'referer', operation: 'set', value: referer });
  }
  if (origin) {
    requestHeaderActions.push({ header: 'origin', operation: 'set', value: origin });
  }
  if (userAgent) {
    requestHeaderActions.push({ header: 'user-agent', operation: 'set', value: userAgent });
  }

  // Codex review #11: build a condition factory that always layers in
  // the initiator scoping when an initiatorDomain is provided. Without
  // it, CORS-relax rules apply to ANY tab fetching a matching URL —
  // the rule's URL filter doesn't restrict who BENEFITS from the
  // response rewrite, only what the rewrite targets.
  function makeCondition(regexFilter) {
    const cond = {
      regexFilter,
      resourceTypes: ['xmlhttprequest', 'media', 'other'],
    };
    if (initiatorDomain) {
      cond.initiatorDomains = [initiatorDomain];
    }
    return cond;
  }

  const rules = [];
  let ruleId = idBase;
  for (const { regexFilter } of allFilters) {
    const trusted = trustedSet.has(regexFilter);
    // Request header-spoof rule: ONLY for trusted origin groups.
    //
    // Codex adversarial-review: a malicious public HTTPS segment URL
    // in an otherwise-accepted manifest used to receive the captured
    // Referer/Origin/User-Agent. Referers for player pages routinely
    // contain signed URLs or session query params (e.g. ?token=...,
    // ?expires=..., ?auth=...) — leaking those to attacker-controlled
    // hosts is credential-adjacent exfiltration even when cookies
    // and Authorization were already scoped out.
    //
    // Foreign origins now get NO DNR rule, so requests go out with
    // the browser's defaults (no Referer or extension-origin Referer).
    // Hotlink-protected legitimate cross-CDN streams are a niche
    // pattern; same-or-sub-origin CDNs are the common case and they
    // STILL get the spoof.
    if (requestHeaderActions.length > 0 && trusted) {
      rules.push({
        id: ruleId,
        priority: 1,
        action: {
          type: 'modifyHeaders',
          requestHeaders: requestHeaderActions,
        },
        condition: makeCondition(regexFilter),
      });
    }
    // Response CORS-relax: ONLY when this origin group is trusted.
    // Codex review #10: trusting all origins here is what lets a DNS-
    // rebinding manifest exfiltrate intranet content via the browser.
    if (trusted) {
      rules.push({
        id: ruleId + RESPONSE_RULE_ID_OFFSET,
        priority: 1,
        action: {
          type: 'modifyHeaders',
          responseHeaders: [
            { header: 'access-control-allow-origin', operation: 'set', value: '*' },
            { header: 'access-control-allow-credentials', operation: 'remove' },
          ],
        },
        condition: makeCondition(regexFilter),
      });
    }
    ruleId += 1;
  }

  return { rules, ruleIds: rules.map((r) => r.id) };
}

/**
 * Convenience that wraps chrome.declarativeNetRequest.updateSessionRules.
 * Returns the rule IDs that were installed; caller stores them and passes
 * back to cleanupRules() when the job ends.
 */
export async function installRules(rules) {
  if (!rules || rules.length === 0) return [];
  // Remove any rules with overlapping IDs first — defensive against a
  // prior crashed job that didn't clean up.
  const ids = rules.map((r) => r.id);
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: ids,
    addRules: rules,
  });
  return ids;
}

export async function cleanupRules(ruleIds) {
  if (!ruleIds || ruleIds.length === 0) return;
  try {
    await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: ruleIds });
  } catch (e) {
    // Don't throw from cleanup paths; log and move on.
    console.warn('[wv2nas] DNR cleanup failed:', e);
  }
}

// Exposed for tests only; importable but not part of the public API.
export const _internals = { REQUEST_OVERRIDE_HEADERS, RESPONSE_RULE_ID_OFFSET };
