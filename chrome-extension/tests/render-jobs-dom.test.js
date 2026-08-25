import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// renderJobs against a real-enough DOM, deliberately NOT stubbed.
//
// Every other sidepanel test replaces renderJobs with a no-op, which is how a
// regression that emptied the list on every poll passed 528 tests: rows were
// given a composite DOM id while the survivor set still held bare ids, and the
// cleanup pass recovered identity by string-slicing the id back apart. Each
// freshly created row was judged vanished and removed immediately.
//
// The lesson is narrower than "add a DOM test": identity was being round-
// tripped through a presentation attribute. It is carried as data now, and
// these tests hold the rendered result to that.

const NAS_A = 'http://nas-a.example:52052';
const NAS_B = 'http://nas-b.example:52052';

function makeElement(tag) {
  const el = {
    tagName: tag,
    id: '',
    className: '',
    textContent: '',
    innerHTML: '',
    title: '',
    style: {},
    dataset: {},
    children: [],
    parentNode: null,
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) { if (on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {},
    setAttribute() {},
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0 }),
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    appendChild(child) { child.parentNode = el; el.children.push(child); return child; },
    insertBefore(child, before) {
      child.parentNode = el;
      const i = el.children.indexOf(before);
      if (i === -1) el.children.push(child); else el.children.splice(i, 0, child);
      return child;
    },
    remove() {
      if (!el.parentNode) return;
      const i = el.parentNode.children.indexOf(el);
      if (i !== -1) el.parentNode.children.splice(i, 1);
      el.parentNode = null;
    },
  };
  return el;
}

function makeCtx() {
  const list = makeElement('div');
  const byId = new Map();
  const document = {
    addEventListener() {},
    getElementById: (id) => {
      if (id === 'recentJobsList') return list;
      // Rows register themselves once renderJobs assigns their id.
      for (const child of list.children) if (child.id === id) return child;
      return byId.get(id) || null;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => makeElement(tag),
    documentElement: { setAttribute() {} },
    body: { appendChild() {} },
  };
  const ctx = loadScriptIntoContext('sidepanel.js', {
    chrome: {
      storage: {
        sync: { get: async () => ({}) },
        local: { get: async () => ({}), set: async () => {} },
        session: { get: async () => ({}) },
        onChanged: { addListener() {} },
      },
      runtime: {
        onMessage: { addListener() {} },
        openOptionsPage: () => {},
        sendMessage: async () => {},
        lastError: null,
      },
      tabs: {
        query: (_q, cb) => cb([]),
        onUpdated: { addListener() {} },
        onActivated: { addListener() {} },
      },
    },
    document,
    window: {},
    navigator: { clipboard: {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => [] }),
  });
  // getJobInnerHtml touches i18n-heavy helpers; the structure is what matters
  // here, not the markup.
  ctx.getJobInnerHtml = () => '';
  ctx.bindJobEvents = () => {};
  return { ctx, list };
}

const row = (id, scope, key) => `{
  id: '${id}', title: 'Job ${id}', status: 'completed', progress: 100,
  __nasTarget: sidepanelCore.nasTarget('${scope}', '${key}')
}`;

describe('renderJobs keeps the rows it just created', () => {
  it('does not remove every row on the pass that created them', () => {
    const { ctx, list } = makeCtx();
    ctx.__eval(`jobs = [${row('j1', NAS_A, 'ka')}, ${row('j2', NAS_A, 'ka')}];`);

    ctx.renderJobs();

    // The regression this file exists for: both rows vanished immediately.
    expect(list.children.length).toBe(2);
  });

  it('is stable across repeated renders', () => {
    const { ctx, list } = makeCtx();
    ctx.__eval(`jobs = [${row('j1', NAS_A, 'ka')}];`);

    ctx.renderJobs();
    ctx.renderJobs();
    ctx.renderJobs();

    expect(list.children.length).toBe(1);
  });

  it('carries identity as data rather than parsing it back out of the id', () => {
    const { ctx, list } = makeCtx();
    ctx.__eval(`jobs = [${row('j1', NAS_A, 'ka')}];`);

    ctx.renderJobs();

    const key = ctx.__eval(`jobKeyFor(jobs[0])`);
    expect(list.children[0].dataset.jobKey).toBe(key);
  });

  it('gives two NAS with the same job id two separate rows', () => {
    const { ctx, list } = makeCtx();
    ctx.__eval(`jobs = [${row('same', NAS_A, 'ka')}, ${row('same', NAS_B, 'kb')}];`);

    ctx.renderJobs();

    expect(list.children.length).toBe(2);
    expect(list.children[0].id).not.toBe(list.children[1].id);
    expect(list.children[0].dataset.jobKey).not.toBe(list.children[1].dataset.jobKey);
  });

  it('removes a row that really did vanish, and only that one', () => {
    const { ctx, list } = makeCtx();
    ctx.__eval(`jobs = [${row('j1', NAS_A, 'ka')}, ${row('j2', NAS_A, 'ka')}];`);
    ctx.renderJobs();
    expect(list.children.length).toBe(2);

    ctx.__eval(`jobs = [${row('j1', NAS_A, 'ka')}];`);
    ctx.renderJobs();

    expect(list.children.length).toBe(1);
    const key = ctx.__eval(`jobKeyFor(jobs[0])`);
    expect(list.children[0].dataset.jobKey).toBe(key);
  });
});
