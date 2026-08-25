import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// The first-run onboarding is the one surface a brand-new user cannot skip, so
// a locale that silently falls back to English there is worse than an English
// string buried in an advanced pane. These tests pin onboarding to full
// coverage across every supported dictionary.
//
// Scope note: the non-en dictionaries are only partially translated in general
// (ja/ko/fr/es/pt each omit ~80 non-onboarding keys and fall back through t()).
// That predates onboarding and is deliberately left alone here — this file
// asserts parity for the onboarding namespace only, so it fails on a
// regression we introduced rather than on a backlog we did not.

function loadI18n() {
  const ctx = loadScriptIntoContext('i18n.js', {
    window: {},
    navigator: { language: 'en' },
    document: { documentElement: { setAttribute: () => {}, lang: '' } },
  });
  return ctx.window.WV2N_I18N;
}

const ONBOARDING_PREFIX = 'onboarding.';

// Every onboarding key the UI actually asks for, by surface. Kept explicit
// rather than derived, so deleting a key from i18n.js and its call site at the
// same time still trips this test.
const REQUIRED_KEYS = [
  'onboarding.nav.label',
  'onboarding.intro1',
  'onboarding.intro2',
  'onboarding.step1.title',
  'onboarding.step1.body',
  'onboarding.step1.action',
  'onboarding.step2.title',
  'onboarding.step2.body',
  'onboarding.step2.action',
  'onboarding.step3.title',
  'onboarding.step3.body',
  'onboarding.step4.title',
  'onboarding.step4.body',
  'onboarding.status.todo',
  'onboarding.status.done',
  'onboarding.done',
  'onboarding.doneHint',
  'onboarding.doneToast',
  'onboarding.coach.step1',
  'onboarding.coach.step1.action',
  'onboarding.coach.step2',
  'onboarding.coach.step3',
  'onboarding.coach.dismiss',
];

// Values that are intentionally identical across locales, so the
// "not just the English fallback" check below must not flag them.
const NOT_TRANSLATED_BY_DESIGN = new Set([
  'onboarding.nav.label', // a filename shown in the sidebar, not prose
]);

describe('onboarding i18n coverage', () => {
  const i18n = loadI18n();

  it('exposes every supported language', () => {
    expect(i18n.SUPPORTED_LANGS.length).toBeGreaterThan(1);
    expect(i18n.SUPPORTED_LANGS).toContain('en');
  });

  it.each(REQUIRED_KEYS)('en defines %s', (key) => {
    i18n.setLanguage('en');
    expect(i18n.t(key)).not.toBe(key);
  });

  for (const lang of loadI18n().SUPPORTED_LANGS) {
    it(`${lang} resolves every onboarding key`, () => {
      i18n.setLanguage(lang);
      const unresolved = REQUIRED_KEYS.filter((key) => i18n.t(key) === key);
      expect(unresolved).toEqual([]);
    });
  }

  // t() falls back to English for a missing key, so "resolves" alone would pass
  // on a dictionary that defines nothing. Compare against the English string to
  // prove the locale carries its own copy.
  for (const lang of loadI18n().SUPPORTED_LANGS) {
    if (lang === 'en') continue;
    it(`${lang} translates onboarding rather than falling back to English`, () => {
      i18n.setLanguage('en');
      const english = Object.fromEntries(REQUIRED_KEYS.map((k) => [k, i18n.t(k)]));
      i18n.setLanguage(lang);
      const fellBack = REQUIRED_KEYS
        .filter((k) => !NOT_TRANSLATED_BY_DESIGN.has(k))
        .filter((k) => i18n.t(k) === english[k]);
      expect(fellBack).toEqual([]);
    });
  }

  it('every onboarding key present in en is present in all other locales', () => {
    i18n.setLanguage('en');
    const enOnboarding = REQUIRED_KEYS.filter((k) => k.startsWith(ONBOARDING_PREFIX));
    const gaps = {};
    for (const lang of i18n.SUPPORTED_LANGS) {
      i18n.setLanguage(lang);
      const missing = enOnboarding.filter((k) => i18n.t(k) === k);
      if (missing.length) gaps[lang] = missing;
    }
    expect(gaps).toEqual({});
  });
});
