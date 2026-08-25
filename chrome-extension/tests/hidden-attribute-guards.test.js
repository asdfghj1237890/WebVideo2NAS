import fs from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

// The `hidden` attribute is only a UA stylesheet rule (display: none). Any
// author rule that sets `display` on the same element beats it, and the element
// stays on screen with whatever content it last had.
//
// This has now bitten twice in this codebase: the side panel's "0 on page"
// chip, and — after that was fixed in one stylesheet — the options sidebar
// badges, which kept showing "!" for a finished onboarding and an empty pill on
// a fresh install. Fixing the instance in front of you is not enough, because
// the defect is invisible until someone happens to look at the right state.
//
// So this is a static check rather than a behavioural one: find every element
// the scripts hide, resolve its selectors from the markup, and require the
// stylesheet to guard it. It fails on a new unguarded element without anyone
// remembering to add a case.

const EXT = path.resolve(process.cwd());

const SURFACES = [
  { name: 'side panel', html: 'sidepanel.html', css: 'sidepanel.css', js: 'sidepanel.js' },
  { name: 'options page', html: 'options/options.html', css: 'options/options.css', js: 'options/options.js' },
];

const read = (p) => fs.readFileSync(path.join(EXT, p), 'utf-8');

// Element ids the script hides, however it spells it.
function hiddenTargets(js) {
  const ids = new Set();
  const patterns = [
    /\$\(['"]([A-Za-z0-9_-]+)['"]\)\s*\.hidden\s*=/g,
    /getElementById\(['"]([A-Za-z0-9_-]+)['"]\)\s*\.hidden\s*=/g,
    /(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*\$\(['"]([A-Za-z0-9_-]+)['"]\)/g,
    /(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*document\.getElementById\(['"]([A-Za-z0-9_-]+)['"]\)/g,
  ];
  // First pass: direct assignments.
  for (const re of patterns.slice(0, 2)) {
    for (const m of js.matchAll(re)) ids.add(m[1]);
  }
  // Second pass: a variable bound to an element, then hidden or attribute-set.
  //
  // Scoped to a window after the binding rather than the whole file. Names like
  // `el` are reused constantly, and searching globally reported elements that
  // are never hidden at all — a check that cries wolf is a check someone
  // deletes. The window is generous enough for the real cases, which assign
  // within a few lines of the lookup.
  const WINDOW = 1200; // characters, ~30 lines
  for (const re of patterns.slice(2)) {
    for (const m of js.matchAll(re)) {
      const [, varName, id] = m;
      const scope = js.slice(m.index, m.index + WINDOW);
      const touches = new RegExp(
        `\\b${varName}\\s*\\.hidden\\s*=|\\b${varName}\\s*\\.setAttribute\\(['"]hidden['"]`,
      );
      if (touches.test(scope)) ids.add(id);
    }
  }
  return ids;
}

// The selectors that could plausibly carry a display rule for this element.
function selectorsFor(html, id) {
  const tag = new RegExp(`<[a-z]+[^>]*\\bid=["']${id}["'][^>]*>`, 'i').exec(html);
  if (!tag) return [];
  const classAttr = /\bclass=["']([^"']+)["']/.exec(tag[0]);
  const classes = classAttr ? classAttr[1].trim().split(/\s+/) : [];
  return [`#${id}`, ...classes.map((c) => `.${c}`)];
}

// Does the stylesheet set display for any of those selectors?
function setsDisplay(css, selectors) {
  return selectors.some((sel) => {
    const re = new RegExp(`(^|[,{}\\s])${sel.replace(/[.#]/g, '\\$&')}[^{,]*\\{[^}]*display\\s*:`, 'm');
    return re.test(css);
  });
}

// Is any of them guarded against the attribute?
function hasGuard(css, selectors) {
  return selectors.some((sel) => css.includes(`${sel}[hidden]`));
}

describe.each(SURFACES)('$name honours the hidden attribute', ({ html, css, js }) => {
  const markup = read(html);
  const styles = read(css);
  const script = read(js);
  const targets = [...hiddenTargets(script)];

  it('finds the elements the script hides', () => {
    // Guards the check itself: a parser that matched nothing would pass
    // everything below silently.
    expect(targets.length).toBeGreaterThan(0);
  });

  it('guards every hidden element that also has a display rule', () => {
    const unguarded = targets.filter((id) => {
      const selectors = selectorsFor(markup, id);
      if (!selectors.length) return false;          // not in this surface's markup
      if (!setsDisplay(styles, selectors)) return false;  // nothing to override it
      return !hasGuard(styles, selectors);
    });
    expect(unguarded).toEqual([]);
  });
});
