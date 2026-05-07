// Codex review: offscreen.html and offscreen.js were exposed via
// web_accessible_resources with `<all_urls>`, so any page knowing
// the extension ID could iframe `chrome-extension://.../offscreen.html`,
// load offscreen.js in the extension origin, and double-handle the
// runtime messages an active browser-side download relies on.
//
// chrome.offscreen.createDocument() and offscreen.js's `import` of
// segmentDownloader.js work without web_accessible_resources because
// the offscreen API is privileged and uses the chrome-extension://
// scheme directly. So removing those entries is a strict security
// improvement with no functional regression.

import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const MANIFEST_PATH = path.resolve(__dirname, '..', 'manifest.json');


describe('manifest.json: web_accessible_resources hardening', () => {
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf-8'));

  it('does not expose offscreen.html to the open web', () => {
    const war = manifest.web_accessible_resources || [];
    for (const block of war) {
      const resources = block.resources || [];
      expect(resources).not.toContain('offscreen.html');
    }
  });

  it('does not expose offscreen.js to the open web', () => {
    const war = manifest.web_accessible_resources || [];
    for (const block of war) {
      const resources = block.resources || [];
      expect(resources).not.toContain('offscreen.js');
    }
  });

  it('does not expose segmentDownloader.js to the open web', () => {
    // Same risk class — segmentDownloader.js is privileged code that
    // performs network access on behalf of the user.
    const war = manifest.web_accessible_resources || [];
    for (const block of war) {
      const resources = block.resources || [];
      expect(resources).not.toContain('segmentDownloader.js');
    }
  });

  it('any web_accessible_resources block has no <all_urls> match for privileged scripts', () => {
    // Defensive: if a future commit adds a web_accessible_resources
    // block, ensure no <all_urls> match grants any of the privileged
    // browser-side files.
    const war = manifest.web_accessible_resources || [];
    const privileged = new Set([
      'offscreen.html', 'offscreen.js', 'segmentDownloader.js',
      'background.js', 'dnrRules.js',
    ]);
    for (const block of war) {
      const resources = block.resources || [];
      const matches = block.matches || [];
      if (matches.includes('<all_urls>')) {
        for (const r of resources) {
          expect(privileged.has(r)).toBe(false);
        }
      }
    }
  });
});


describe('manifest.json: least-privilege permissions', () => {
  // Codex review (P3): the manifest previously requested `scripting`
  // alongside `<all_urls>` host access, granting runtime
  // chrome.scripting.executeScript power that nothing in the codebase
  // actually uses. Combined with the broad host permission this is a
  // material attack-surface increase with zero functional benefit.
  // Reintroducing this permission requires a real chrome.scripting
  // call in the extension code; the test below blocks an accidental
  // re-add.
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf-8'));

  it('does not request the scripting permission', () => {
    const perms = manifest.permissions || [];
    expect(perms).not.toContain('scripting');
  });
});
