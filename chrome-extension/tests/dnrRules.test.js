// Unit tests for the v2.5 DNR rule builder.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  buildHeaderRules,
  cleanupRules,
  installRules,
  urlsToRegexFilters,
  RULE_ID_BASE,
  _internals,
} from '../dnrRules.js';

describe('urlsToRegexFilters', () => {
  it('groups URLs sharing origin+dir into one regex', () => {
    const filters = urlsToRegexFilters([
      'https://cdn.example.com/v/seg0.ts',
      'https://cdn.example.com/v/seg1.ts',
      'https://cdn.example.com/v/seg2.ts',
    ]);
    expect(filters).toHaveLength(1);
    expect(filters[0].origin).toBe('https://cdn.example.com');
    // DNR uses RE2; `/` is a literal there, so we don't escape it. Only
    // regex metachars (`.`, etc.) get backslashed.
    expect(filters[0].regexFilter).toBe('^https://cdn\\.example\\.com/v/.*');
  });

  it('splits URLs across origins', () => {
    const filters = urlsToRegexFilters([
      'https://a.example.com/v/seg0.ts',
      'https://b.example.com/v/seg0.ts',
    ]);
    expect(filters).toHaveLength(2);
  });

  it('splits URLs across paths', () => {
    const filters = urlsToRegexFilters([
      'https://cdn.example.com/aud/seg0.m4s',
      'https://cdn.example.com/vid/seg0.m4s',
    ]);
    expect(filters).toHaveLength(2);
  });

  it('skips invalid URLs without crashing', () => {
    const filters = urlsToRegexFilters(['not-a-url', '', 'https://ok.example.com/v/seg.ts']);
    expect(filters).toHaveLength(1);
    expect(filters[0].origin).toBe('https://ok.example.com');
  });

  it('returns empty array for empty input', () => {
    expect(urlsToRegexFilters([])).toEqual([]);
  });
});

describe('buildHeaderRules', () => {
  it('emits request + response rules per origin group', () => {
    const { rules, ruleIds } = buildHeaderRules({
      segmentUrls: [
        'https://cdn.example.com/v/seg0.ts',
        'https://cdn.example.com/v/seg1.ts',
      ],
      referer: 'https://player.example.com/watch',
      origin: 'https://player.example.com',
      userAgent: 'Mozilla/5.0 spoofed',
    });
    // 1 group → 1 request rule + 1 response rule
    expect(rules).toHaveLength(2);
    expect(ruleIds).toHaveLength(2);

    const reqRule = rules.find((r) => r.id < _internals.RESPONSE_RULE_ID_OFFSET + RULE_ID_BASE);
    expect(reqRule.action.type).toBe('modifyHeaders');
    expect(reqRule.action.requestHeaders).toEqual([
      { header: 'referer', operation: 'set', value: 'https://player.example.com/watch' },
      { header: 'origin', operation: 'set', value: 'https://player.example.com' },
      { header: 'user-agent', operation: 'set', value: 'Mozilla/5.0 spoofed' },
    ]);

    const respRule = rules.find((r) => r.id >= _internals.RESPONSE_RULE_ID_OFFSET + RULE_ID_BASE);
    expect(respRule.action.responseHeaders).toEqual([
      { header: 'access-control-allow-origin', operation: 'set', value: '*' },
      { header: 'access-control-allow-credentials', operation: 'remove' },
    ]);
  });

  it('omits request rule entirely when no spoof headers given', () => {
    // Caller may want only the CORS-relax behavior.
    const { rules } = buildHeaderRules({
      segmentUrls: ['https://cdn.example.com/v/seg0.ts'],
    });
    // No request rule, only response rule
    expect(rules).toHaveLength(1);
    expect(rules[0].action.responseHeaders).toBeDefined();
    expect(rules[0].action.requestHeaders).toBeUndefined();
  });

  it('returns empty when no urls', () => {
    expect(buildHeaderRules({ segmentUrls: [] })).toEqual({ rules: [], ruleIds: [] });
  });

  it('IDs start at RULE_ID_BASE and stay distinct between groups', () => {
    const { ruleIds } = buildHeaderRules({
      segmentUrls: [
        'https://a.example.com/v/seg.ts',
        'https://b.example.com/v/seg.ts',
      ],
      referer: 'https://x',
    });
    // 2 groups, each with request+response = 4 rules total
    expect(ruleIds).toHaveLength(4);
    expect(new Set(ruleIds).size).toBe(4); // all distinct
    expect(Math.min(...ruleIds)).toBe(RULE_ID_BASE);
  });

  // Regression — Codex review #2: parallel browser-side jobs MUST get
  // disjoint rule ID ranges so one job's cleanup doesn't remove another
  // job's active rules.
  it('idBase param shifts the entire range so two concurrent jobs disjoint', () => {
    const { ruleIds: idsA } = buildHeaderRules({
      segmentUrls: ['https://a.example.com/v/seg.ts'],
      referer: 'r-a', idBase: 10000,
    });
    const { ruleIds: idsB } = buildHeaderRules({
      segmentUrls: ['https://a.example.com/v/seg.ts'],
      referer: 'r-b', idBase: 10100,
    });
    // No overlap between the two jobs' IDs — that's the whole point.
    const intersection = idsA.filter((id) => idsB.includes(id));
    expect(intersection).toEqual([]);
    expect(Math.min(...idsA)).toBe(10000);
    expect(Math.min(...idsB)).toBe(10100);
  });

  it('idBase defaults to RULE_ID_BASE when omitted', () => {
    const { ruleIds } = buildHeaderRules({
      segmentUrls: ['https://a.example.com/v/seg.ts'],
      referer: 'r',
    });
    expect(Math.min(...ruleIds)).toBe(RULE_ID_BASE);
  });

  it('regex filter pins to URL prefix and escapes dots/slashes', () => {
    const { rules } = buildHeaderRules({
      segmentUrls: ['https://a.b.c/v/seg.ts'],
      referer: 'r',
    });
    // anchored ^ + escaped dots; `/` is literal in RE2 so no backslash.
    expect(rules[0].condition.regexFilter).toBe('^https://a\\.b\\.c/v/.*');
  });

  // Codex review #8: rules built for the manifest URL pre-init must
  // produce a regex that matches the manifest URL. Without this,
  // browser-side mode would fall back to NAS-fetch for protected
  // manifests — exactly the scenario the feature is meant to fix.
  // The buildHeaderRules contract is just "rules cover whatever URLs
  // you pass in"; the actual ordering (rules installed BEFORE manifest
  // fetch) is enforced in background.js's runBrowserSideJob.
  it('rules built for a single manifest URL produce a regex that matches it', () => {
    const manifestUrl = 'https://protected.example.com/auth/master.m3u8?token=abc';
    const { rules, ruleIds } = buildHeaderRules({
      segmentUrls: [manifestUrl],
      referer: 'https://player.example.com/watch?id=42',
      origin: 'https://player.example.com',
      userAgent: 'Mozilla/5.0',
    });
    expect(rules.length).toBeGreaterThan(0);
    expect(ruleIds.length).toBeGreaterThan(0);

    const filters = rules.map((r) => r.condition.regexFilter);
    const matched = filters.some((rf) => new RegExp(rf).test(manifestUrl));
    expect(matched).toBe(true);

    const reqRule = rules.find((r) => r.action.requestHeaders);
    expect(reqRule).toBeDefined();
    const headers = reqRule.action.requestHeaders.map((h) => h.header);
    expect(headers).toEqual(expect.arrayContaining(['referer', 'origin', 'user-agent']));
  });

  // Codex review #10: trustedSegmentUrls subset gates CORS-relax.
  //
  // Codex adversarial-review (P2 hardening): also gates the request
  // header-spoof rule. A malicious public HTTPS segment URL in an
  // otherwise-accepted manifest used to receive the captured
  // Referer/Origin/UA. Referers on player pages routinely carry
  // signed URLs or session tokens — leaking those to attacker-
  // controlled hosts is credential-adjacent exfiltration. Foreign
  // origins now get NEITHER request-header-spoof NOR CORS-relax;
  // requests go out with the browser's defaults.
  it('foreign-origin URLs (NOT in trustedSegmentUrls) get NEITHER header-spoof NOR CORS-relax', () => {
    const trustedUrl = 'https://cdn.example.com/v/seg.ts';
    const foreignUrl = 'https://attacker.evil/v/seg.ts';
    const { rules } = buildHeaderRules({
      segmentUrls: [trustedUrl, foreignUrl],
      trustedSegmentUrls: [trustedUrl],
      referer: 'https://player.example.com/',
      origin: 'https://player.example.com',
      userAgent: 'UA',
    });

    // Header-spoof rules: ONLY the trusted origin is covered. The
    // foreign URL gets NO DNR rule, so it inherits the browser's
    // default outgoing headers.
    const reqRules = rules.filter((r) => r.action.requestHeaders);
    expect(reqRules).toHaveLength(1);
    expect(new RegExp(reqRules[0].condition.regexFilter).test(trustedUrl)).toBe(true);
    expect(new RegExp(reqRules[0].condition.regexFilter).test(foreignUrl)).toBe(false);

    // CORS-relax rules: ONLY trusted origin covered (unchanged).
    const respRules = rules.filter((r) => r.action.responseHeaders);
    expect(respRules).toHaveLength(1);
    expect(new RegExp(respRules[0].condition.regexFilter).test(trustedUrl)).toBe(true);
    expect(new RegExp(respRules[0].condition.regexFilter).test(foreignUrl)).toBe(false);
  });

  it('explicit empty trustedSegmentUrls produces NO CORS-relax rules at all', () => {
    const { rules } = buildHeaderRules({
      segmentUrls: ['https://cdn.example.com/v/seg.ts'],
      trustedSegmentUrls: [],
      referer: 'r',
    });
    const respRules = rules.filter((r) => r.action.responseHeaders);
    expect(respRules).toEqual([]);
  });

  // Codex adversarial-review (P2): the same scoping now ALSO applies
  // to request header-spoof rules. With everything untrusted, no
  // rules are emitted at all — outgoing requests inherit the
  // browser's defaults instead of leaking the captured Referer.
  it('explicit empty trustedSegmentUrls also drops header-spoof rules', () => {
    const { rules, ruleIds } = buildHeaderRules({
      segmentUrls: [
        'https://cdn.example.com/v/seg.ts',
        'https://other.example.org/seg.ts',
      ],
      trustedSegmentUrls: [],
      referer: 'https://player.example.com/watch?token=secret',
      origin: 'https://player.example.com',
      userAgent: 'UA',
    });
    expect(rules).toEqual([]);
    expect(ruleIds).toEqual([]);
  });

  // Codex adversarial-review (high) regression guard: callers
  // that DON'T pass `trustedSegmentUrls` explicitly fall through
  // to the default-all-trusted shortcut. Production callers
  // MUST pass an explicit set so an unvalidated host doesn't get
  // CORS-relax. This test documents the default-trust default
  // (back-compat for unit tests) and the explicit-empty refusal.
  it('explicit `trustedSegmentUrls: [url]` for the lone segmentUrl emits both rule types', () => {
    // Phase-1 use case: master URL is the only segmentUrl AND
    // we've vetted it. Pass it as the explicit trusted set.
    const url = 'https://cdn.example.com/master.m3u8';
    const { rules } = buildHeaderRules({
      segmentUrls: [url],
      trustedSegmentUrls: [url],
      referer: 'https://example.com/watch',
      origin: 'https://example.com',
      userAgent: 'UA',
    });
    expect(rules.filter((r) => r.action.requestHeaders)).toHaveLength(1);
    expect(rules.filter((r) => r.action.responseHeaders)).toHaveLength(1);
  });

  it('explicit `trustedSegmentUrls: []` for a single segment emits NO rules', () => {
    // The protective configuration: segmentUrls has the URL but
    // it's NOT in the trusted set. Both rule types skip — no
    // header-spoof leaks, no CORS-relax exfil channel.
    const url = 'https://internal.corp.example/manifest.m3u8';
    const { rules, ruleIds } = buildHeaderRules({
      segmentUrls: [url],
      trustedSegmentUrls: [],
      referer: 'https://example.com/watch',
      origin: 'https://example.com',
      userAgent: 'UA',
    });
    expect(rules).toEqual([]);
    expect(ruleIds).toEqual([]);
  });

  it('mixed trust: trusted gets BOTH spoof + CORS, untrusted gets NEITHER', () => {
    const trusted = 'https://cdn.example.com/v/seg.ts';
    const untrusted = 'https://attacker.example.org/seg.ts';
    const { rules } = buildHeaderRules({
      segmentUrls: [trusted, untrusted],
      trustedSegmentUrls: [trusted],
      referer: 'https://player.example.com/watch?token=secret',
      origin: 'https://player.example.com',
      userAgent: 'UA',
    });
    // 1 group = 1 request rule + 1 response rule, both for the
    // trusted URL only.
    const reqRules = rules.filter((r) => r.action.requestHeaders);
    const respRules = rules.filter((r) => r.action.responseHeaders);
    expect(reqRules).toHaveLength(1);
    expect(respRules).toHaveLength(1);
    // Both rules MATCH trusted, neither matches untrusted.
    for (const r of [...reqRules, ...respRules]) {
      const re = new RegExp(r.condition.regexFilter);
      expect(re.test(trusted)).toBe(true);
      expect(re.test(untrusted)).toBe(false);
    }
  });

  it('omitted trustedSegmentUrls defaults to all-trusted (back-compat)', () => {
    // Existing single-trust-level callers (and most existing tests)
    // pass only segmentUrls. The default should preserve the
    // pre-Codex-#10 behavior.
    const { rules } = buildHeaderRules({
      segmentUrls: ['https://cdn.example.com/v/seg.ts'],
      referer: 'r',
    });
    const respRules = rules.filter((r) => r.action.responseHeaders);
    expect(respRules.length).toBeGreaterThan(0);
  });

  // Codex review #11: DNR rules MUST be scoped to the extension's own
  // initiator. Without this, every tab in the browser benefits from
  // the CORS-relax response rewrite while a job is running — a
  // malicious page that learns or guesses a CDN URL during the
  // download window could read responses cross-origin.
  it('initiatorDomain produces initiatorDomains on every rule condition', () => {
    const { rules } = buildHeaderRules({
      segmentUrls: ['https://cdn.example.com/v/seg.ts'],
      referer: 'https://player.example.com/',
      initiatorDomain: 'abcdef1234567890',  // simulated extension ID
    });
    expect(rules.length).toBeGreaterThan(0);
    for (const r of rules) {
      expect(r.condition.initiatorDomains).toEqual(['abcdef1234567890']);
    }
  });

  it('omitted initiatorDomain leaves rules unscoped (back-compat for tests)', () => {
    const { rules } = buildHeaderRules({
      segmentUrls: ['https://cdn.example.com/v/seg.ts'],
      referer: 'r',
    });
    expect(rules.length).toBeGreaterThan(0);
    for (const r of rules) {
      expect(r.condition.initiatorDomains).toBeUndefined();
    }
  });

  it('initiator scoping covers BOTH header-spoof and CORS-relax rules', () => {
    // The whole point: a tab issuing fetch() to a CDN URL during the
    // download window should not benefit from EITHER the header
    // override (less critical) OR the CORS rewrite (the actual
    // exfiltration vector).
    const { rules } = buildHeaderRules({
      segmentUrls: ['https://cdn.example.com/v/seg.ts'],
      trustedSegmentUrls: ['https://cdn.example.com/v/seg.ts'],
      referer: 'r', origin: 'o', userAgent: 'u',
      initiatorDomain: 'extid',
    });
    const requestRules = rules.filter((r) => r.action.requestHeaders);
    const responseRules = rules.filter((r) => r.action.responseHeaders);
    expect(requestRules.length).toBeGreaterThan(0);
    expect(responseRules.length).toBeGreaterThan(0);
    for (const r of [...requestRules, ...responseRules]) {
      expect(r.condition.initiatorDomains).toEqual(['extid']);
    }
  });

  it('packs more than 50 trusted URL prefixes into collision-free rules', () => {
    const urls = Array.from(
      { length: 56 },
      (_v, i) => `https://cdn.example.com/media/shard-${i}/seg.m4s`
    );

    const { rules, ruleIds } = buildHeaderRules({
      segmentUrls: urls,
      trustedSegmentUrls: urls,
      referer: 'https://example.com/watch',
      origin: 'https://example.com',
      userAgent: 'UA',
      idBase: 10000,
      initiatorDomain: 'extid',
    });

    const requestRules = rules.filter((r) => r.action.requestHeaders);
    const responseRules = rules.filter((r) => r.action.responseHeaders);

    expect(requestRules.length).toBeGreaterThan(0);
    expect(requestRules.length).toBeLessThanOrEqual(_internals.RESPONSE_RULE_ID_OFFSET);
    expect(responseRules.length).toBe(requestRules.length);
    expect(new Set(ruleIds).size).toBe(ruleIds.length);

    for (const url of urls) {
      expect(requestRules.some((r) => new RegExp(r.condition.regexFilter).test(url))).toBe(true);
      expect(responseRules.some((r) => new RegExp(r.condition.regexFilter).test(url))).toBe(true);
    }
  });
});

describe('install/cleanup wiring', () => {
  beforeEach(() => {
    globalThis.chrome = {
      declarativeNetRequest: {
        updateSessionRules: vi.fn().mockResolvedValue(),
      },
    };
  });

  it('installRules forwards to updateSessionRules with addRules + dedupe removeRuleIds', async () => {
    const rules = [
      { id: 10000, priority: 1, action: { type: 'modifyHeaders' }, condition: { regexFilter: '^https://a/' } },
    ];
    const ids = await installRules(rules);
    expect(ids).toEqual([10000]);
    expect(chrome.declarativeNetRequest.updateSessionRules).toHaveBeenCalledWith({
      removeRuleIds: [10000],
      addRules: rules,
    });
  });

  it('installRules no-ops on empty array', async () => {
    const ids = await installRules([]);
    expect(ids).toEqual([]);
    expect(chrome.declarativeNetRequest.updateSessionRules).not.toHaveBeenCalled();
  });

  it('cleanupRules removes by ID', async () => {
    await cleanupRules([10000, 15000]);
    expect(chrome.declarativeNetRequest.updateSessionRules).toHaveBeenCalledWith({
      removeRuleIds: [10000, 15000],
    });
  });

  it('cleanupRules swallows errors (logged, not raised)', async () => {
    chrome.declarativeNetRequest.updateSessionRules = vi.fn().mockRejectedValue(new Error('nope'));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await cleanupRules([10000]); // should not throw
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it('cleanupRules no-ops on empty', async () => {
    await cleanupRules([]);
    expect(chrome.declarativeNetRequest.updateSessionRules).not.toHaveBeenCalled();
  });
});
