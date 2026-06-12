// Shared pure helpers for the browser-side HLS/DASH pipeline.
//
// This file is intentionally a classic script, not an ES module:
// - background.js is an MV3 classic service worker and loads it with importScripts()
// - dnrRules.js imports it for side-effect in Vitest/module contexts
// Keep browser/network side effects out of this file; it should stay easy to
// test and safe to load in both environments.

(function installBrowserPipelineCore(root) {
  if (!root || root.WV2NASBrowserPipeline) return;

  const RULE_ID_BASE = 10_000;
  const DNR_PER_JOB_RANGE = 100;
  const DNR_RESPONSE_RULE_ID_OFFSET = 50;
  const DNR_MAX_SLOTS = 50;
  const DNR_MAX_PACKED_REGEX_FILTER_LENGTH = 1800;

  const REQUEST_OVERRIDE_HEADERS = ['referer', 'origin', 'user-agent'];

  function urlsToRegexFilters(urls) {
    const groups = new Map();
    for (const u of urls || []) {
      let parsed;
      try {
        parsed = new URL(u);
      } catch (_e) {
        continue;
      }
      const origin = `${parsed.protocol}//${parsed.host}`;
      const dir = parsed.pathname.replace(/[^/]*$/, '');
      const key = origin + dir;
      if (!groups.has(key)) {
        groups.set(key, { origin, dir });
      }
    }

    const filters = [];
    for (const { origin, dir } of groups.values()) {
      const escaped = (origin + dir).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      filters.push({ origin, regexFilter: `^${escaped}.*` });
    }
    return filters;
  }

  function regexFilterToPackedTerm(regexFilter) {
    if (regexFilter.startsWith('^') && regexFilter.endsWith('.*')) {
      return `${regexFilter.slice(1, -2)}.*`;
    }
    return regexFilter.startsWith('^') ? regexFilter.slice(1) : regexFilter;
  }

  function packedRegexLength(terms) {
    if (terms.length === 1) return 1 + terms[0].length;
    return '^(?:'.length + terms.join('|').length + ')'.length;
  }

  function packRegexFilters(regexFilters) {
    const packed = [];
    let current = [];

    function flush() {
      if (current.length === 0) return;
      packed.push(current.length === 1 ? `^${current[0]}` : `^(?:${current.join('|')})`);
      current = [];
    }

    for (const regexFilter of Array.from(new Set(regexFilters || []))) {
      const term = regexFilterToPackedTerm(regexFilter);
      if (
        current.length > 0
        && packedRegexLength([...current, term]) > DNR_MAX_PACKED_REGEX_FILTER_LENGTH
      ) {
        flush();
      }
      current.push(term);
    }
    flush();

    return packed;
  }

  function makeDnrCondition(regexFilter, initiatorDomain) {
    const cond = {
      regexFilter,
      resourceTypes: ['xmlhttprequest', 'media', 'other'],
    };
    if (initiatorDomain) {
      cond.initiatorDomains = [initiatorDomain];
    }
    return cond;
  }

  function buildHeaderRules({
    segmentUrls = [], trustedSegmentUrls, referer, origin, userAgent,
    idBase = RULE_ID_BASE, initiatorDomain,
  } = {}) {
    const allFilters = urlsToRegexFilters(segmentUrls);
    if (allFilters.length === 0) return { rules: [], ruleIds: [] };

    const trustedSet = new Set(
      urlsToRegexFilters(trustedSegmentUrls === undefined ? segmentUrls : trustedSegmentUrls)
        .map((f) => f.regexFilter),
    );

    let ruleFilters = allFilters
      .map((f) => f.regexFilter)
      .filter((regexFilter) => trustedSet.has(regexFilter));
    if (ruleFilters.length === 0) return { rules: [], ruleIds: [] };

    if (ruleFilters.length > DNR_RESPONSE_RULE_ID_OFFSET) {
      const originalFilterCount = ruleFilters.length;
      ruleFilters = packRegexFilters(ruleFilters);
      if (ruleFilters.length > DNR_RESPONSE_RULE_ID_OFFSET) {
        throw new Error(
          `Too many trusted segment URL groups (${originalFilterCount}) for DNR slot; `
          + `packed to ${ruleFilters.length}, max ${DNR_RESPONSE_RULE_ID_OFFSET}`,
        );
      }
    }

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

    const rules = [];
    let ruleId = idBase;
    for (const regexFilter of ruleFilters) {
      if (requestHeaderActions.length > 0) {
        rules.push({
          id: ruleId,
          priority: 1,
          action: {
            type: 'modifyHeaders',
            requestHeaders: requestHeaderActions,
          },
          condition: makeDnrCondition(regexFilter, initiatorDomain),
        });
      }
      rules.push({
        id: ruleId + DNR_RESPONSE_RULE_ID_OFFSET,
        priority: 1,
        action: {
          type: 'modifyHeaders',
          responseHeaders: [
            { header: 'access-control-allow-origin', operation: 'set', value: '*' },
            { header: 'access-control-allow-credentials', operation: 'remove' },
          ],
        },
        condition: makeDnrCondition(regexFilter, initiatorDomain),
      });
      ruleId += 1;
    }

    return { rules, ruleIds: rules.map((r) => r.id) };
  }

  function buildDnrRules(opts = {}) {
    return buildHeaderRules(opts).rules;
  }

  function allocateDnrSlot(usedSlots, maxSlots = DNR_MAX_SLOTS) {
    for (let slot = 0; slot < maxSlots; slot++) {
      if (!usedSlots.has(slot)) {
        usedSlots.add(slot);
        return slot;
      }
    }
    throw new Error(`Browser-side DNR slots exhausted (max ${maxSlots} concurrent jobs).`);
  }

  function releaseDnrSlot(usedSlots, slot) {
    usedSlots.delete(slot);
  }

  function isTrustedDnrUrl(segmentUrl, trustedBase) {
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
    } catch (_e) {
      return false;
    }
    if (segOrigin === baseOrigin) return true;
    if (segHost.endsWith('.' + baseHost)) return true;
    return false;
  }

  function matchesTrustedCdnSuffix(host, suffixes) {
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

  function isTrustedSegmentDnrUrl(segmentUrl, trustedBase, trustedCdnSuffixes) {
    if (isTrustedDnrUrl(segmentUrl, trustedBase)) return true;
    try {
      return matchesTrustedCdnSuffix(
        new URL(segmentUrl).hostname.toLowerCase(),
        trustedCdnSuffixes,
      );
    } catch (_e) {
      return false;
    }
  }

  function planSegmentUrls(plan) {
    const out = new Set();
    if (plan && plan.init_segment_url) out.add(plan.init_segment_url);
    for (const trackName of Object.keys((plan && plan.tracks) || {})) {
      const t = plan.tracks[trackName] || {};
      if (t.init_segment_url) out.add(t.init_segment_url);
      for (const s of t.segments || []) {
        if (s.url) out.add(s.url);
        if (s.key && s.key.uri) out.add(s.key.uri);
      }
    }
    return Array.from(out);
  }

  function filterFetchHeaders(rawHeaders) {
    const out = {};
    if (!rawHeaders || typeof rawHeaders !== 'object') return out;
    const skip = new Set([
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
      'sec-ch-ua',
      'sec-ch-ua-mobile',
      'sec-ch-ua-platform',
    ]);
    for (const [k, v] of Object.entries(rawHeaders)) {
      const lower = k.toLowerCase();
      if (
        !skip.has(lower)
        && !lower.startsWith('sec-')
        && !lower.startsWith('proxy-')
        && v != null
      ) {
        out[k] = String(v);
      }
    }
    return out;
  }

  function pickBestHlsVariant(masterText, masterUrl) {
    const lines = String(masterText || '').split(/\r?\n/);
    let bestBw = -1;
    let bestUrl = null;
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line.startsWith('#EXT-X-STREAM-INF:')) continue;
      const bwMatch = line.match(/BANDWIDTH=(\d+)/);
      const bw = bwMatch ? parseInt(bwMatch[1], 10) : 0;
      let j = i + 1;
      while (j < lines.length && (lines[j].trim() === '' || lines[j].trim().startsWith('#'))) {
        j++;
      }
      if (j < lines.length) {
        try {
          const u = new URL(lines[j].trim(), masterUrl).href;
          if (bw > bestBw) {
            bestBw = bw;
            bestUrl = u;
          }
        } catch (_e) {
          // Ignore malformed variant lines.
        }
      }
    }
    return bestUrl;
  }

  function classifyIpv4Literal(host) {
    const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
    if (!m) return null;
    const octets = [1, 2, 3, 4].map((i) => parseInt(m[i], 10));
    if (octets.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) {
      return { safe: false, reason: 'invalid IPv4 literal' };
    }
    const [a, b, c] = octets;
    if (a === 0) return { safe: false, reason: 'unspecified (0/8)' };
    if (a === 10) return { safe: false, reason: 'RFC 1918 (10/8)' };
    if (a === 127) return { safe: false, reason: 'IPv4 loopback (127/8)' };
    if (a === 169 && b === 254) {
      return { safe: false, reason: 'link-local (169.254/16) - incl. AWS metadata' };
    }
    if (a === 172 && b >= 16 && b <= 31) {
      return { safe: false, reason: 'RFC 1918 (172.16/12)' };
    }
    if (a === 192 && b === 168) return { safe: false, reason: 'RFC 1918 (192.168/16)' };
    if (a === 100 && b >= 64 && b <= 127) return { safe: false, reason: 'shared CGN (100.64/10)' };
    if (a === 192 && b === 0 && c === 0) return { safe: false, reason: 'IETF special-use (192.0.0/24)' };
    if (a === 192 && b === 0 && c === 2) return { safe: false, reason: 'TEST-NET-1' };
    if (a === 198 && (b === 18 || b === 19)) return { safe: false, reason: 'benchmarking (198.18/15)' };
    if (a === 198 && b === 51 && c === 100) return { safe: false, reason: 'TEST-NET-2' };
    if (a === 203 && b === 0 && c === 113) return { safe: false, reason: 'TEST-NET-3' };
    if (a >= 224) return { safe: false, reason: 'multicast/reserved (>=224)' };
    return { safe: true };
  }

  function parseIpv4Octets(raw) {
    const classified = classifyIpv4Literal(raw);
    if (!classified) return null;
    if (!classified.safe && classified.reason === 'invalid IPv4 literal') return null;
    const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(raw);
    if (!m) return null;
    return [1, 2, 3, 4].map((i) => parseInt(m[i], 10));
  }

  function expandIpv6Literal(inner) {
    if (!inner || inner.includes('%')) return null;
    let text = inner.toLowerCase();
    const lastColon = text.lastIndexOf(':');
    const maybeIpv4Tail = lastColon >= 0 ? text.slice(lastColon + 1) : '';
    if (maybeIpv4Tail.includes('.')) {
      const octets = parseIpv4Octets(maybeIpv4Tail);
      if (!octets) return null;
      const high = ((octets[0] << 8) | octets[1]).toString(16);
      const low = ((octets[2] << 8) | octets[3]).toString(16);
      text = `${text.slice(0, lastColon)}:${high}:${low}`;
    }

    const compressed = text.includes('::');
    if ((text.match(/::/g) || []).length > 1) return null;
    const sides = text.split('::');
    const left = sides[0] ? sides[0].split(':') : [];
    const right = compressed && sides[1] ? sides[1].split(':') : [];
    if (left.some((g) => !g) || right.some((g) => !g)) return null;

    let groups;
    if (compressed) {
      const fill = 8 - left.length - right.length;
      if (fill < 1) return null;
      groups = [...left, ...Array(fill).fill('0'), ...right];
    } else {
      groups = left;
      if (groups.length !== 8) return null;
    }

    if (groups.length !== 8) return null;
    const parsed = [];
    for (const group of groups) {
      if (!/^[0-9a-f]{1,4}$/.test(group)) return null;
      parsed.push(parseInt(group, 16));
    }
    return parsed;
  }

  function classifyIpv6Literal(inner) {
    const groups = expandIpv6Literal(inner);
    if (!groups) return { safe: false, reason: 'invalid IPv6 literal' };
    if (groups.slice(0, 5).every((g) => g === 0) && groups[5] === 0xffff) {
      const ip4 = [
        (groups[6] >> 8) & 0xff,
        groups[6] & 0xff,
        (groups[7] >> 8) & 0xff,
        groups[7] & 0xff,
      ].join('.');
      return classifyIpv4Literal(ip4) || {
        safe: false,
        reason: 'invalid IPv4-mapped IPv6 literal',
      };
    }
    if (groups.every((g) => g === 0)) return { safe: false, reason: 'IPv6 unspecified' };
    if (groups.slice(0, 7).every((g) => g === 0) && groups[7] === 1) {
      return { safe: false, reason: 'IPv6 loopback' };
    }
    if ((groups[0] & 0xfe00) === 0xfc00) return { safe: false, reason: 'IPv6 unique-local' };
    if ((groups[0] & 0xffc0) === 0xfe80) return { safe: false, reason: 'IPv6 link-local' };
    if ((groups[0] & 0xffc0) === 0xfec0) return { safe: false, reason: 'IPv6 site-local' };
    if ((groups[0] & 0xff00) === 0xff00) return { safe: false, reason: 'IPv6 multicast' };
    if (groups[0] === 0x2001 && groups[1] === 0x0db8) {
      return { safe: false, reason: 'IPv6 documentation range (2001:db8/32)' };
    }
    if (groups[0] === 0x2001 && groups[1] <= 0x01ff) {
      return { safe: false, reason: 'IPv6 IETF special-use (2001::/23)' };
    }
    if (groups[0] === 0x3ffe) {
      return { safe: false, reason: 'IPv6 6bone documentation/deprecated range' };
    }
    if ((groups[0] & 0xe000) !== 0x2000) {
      return { safe: false, reason: 'IPv6 literal is not global unicast' };
    }
    return { safe: true };
  }

  function isManifestUrlSafeForBrowser(url, pageUrl, trustedCdnSuffixes) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_e) {
      return { safe: false, reason: 'malformed URL' };
    }
    if (parsed.protocol !== 'https:') {
      return {
        safe: false,
        reason: `plain ${parsed.protocol} rejected (HTTPS-only - DNS rebinding mitigation)`,
      };
    }
    const host = parsed.hostname.toLowerCase();
    if (!host) return { safe: false, reason: 'no host' };
    if (
      host === 'localhost'
      || host === '0.0.0.0'
      || host === 'broadcasthost'
      || host.endsWith('.localhost')
    ) {
      return { safe: false, reason: `local hostname ${host}` };
    }
    if (host.startsWith('[') && host.endsWith(']')) {
      return classifyIpv6Literal(host.slice(1, -1));
    }
    const ipv4Safety = classifyIpv4Literal(host);
    if (ipv4Safety) return ipv4Safety;
    if (pageUrl) {
      if (!isTrustedDnrUrl(url, pageUrl) && !matchesTrustedCdnSuffix(host, trustedCdnSuffixes)) {
        return {
          safe: false,
          reason: (
            `host ${host} is not same-site with page (${pageUrl}); `
            + `refusing browser fetch - split-horizon DNS / internal-CA `
            + `hosts cannot be distinguished client-side from public hosts. `
            + `If this is a known cross-site CDN, add its host suffix to `
            + `'Trusted cross-site CDN suffixes' in extension options.`
          ),
        };
      }
    }
    return { safe: true };
  }

  function httpsEquivalent(rawUrl) {
    try {
      const parsed = new URL(rawUrl);
      parsed.protocol = 'https:';
      return parsed.href;
    } catch (_e) {
      return null;
    }
  }

  function canUseNasDirectForBrowserUnsafeUrl(url, pageUrl) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_e) {
      return false;
    }
    if (parsed.protocol !== 'http:') return false;

    const httpsUrl = httpsEquivalent(url);
    if (!httpsUrl) return false;
    const httpsPageUrl = pageUrl ? httpsEquivalent(pageUrl) : null;
    const hostSafety = isManifestUrlSafeForBrowser(httpsUrl, httpsPageUrl);
    return !!hostSafety.safe;
  }

  root.WV2NASBrowserPipeline = Object.freeze({
    RULE_ID_BASE,
    DNR_PER_JOB_RANGE,
    DNR_RESPONSE_RULE_ID_OFFSET,
    DNR_MAX_SLOTS,
    DNR_MAX_PACKED_REGEX_FILTER_LENGTH,
    REQUEST_OVERRIDE_HEADERS,
    urlsToRegexFilters,
    buildHeaderRules,
    buildDnrRules,
    allocateDnrSlot,
    releaseDnrSlot,
    isTrustedDnrUrl,
    isTrustedSegmentDnrUrl,
    matchesTrustedCdnSuffix,
    planSegmentUrls,
    filterFetchHeaders,
    pickBestHlsVariant,
    classifyIpv4Literal,
    parseIpv4Octets,
    expandIpv6Literal,
    classifyIpv6Literal,
    isManifestUrlSafeForBrowser,
    httpsEquivalent,
    canUseNasDirectForBrowserUnsafeUrl,
    _internals: Object.freeze({
      REQUEST_OVERRIDE_HEADERS,
      RESPONSE_RULE_ID_OFFSET: DNR_RESPONSE_RULE_ID_OFFSET,
      MAX_PACKED_REGEX_FILTER_LENGTH: DNR_MAX_PACKED_REGEX_FILTER_LENGTH,
    }),
  });
}((typeof globalThis !== 'undefined' && globalThis) || (typeof self !== 'undefined' && self) || this));
