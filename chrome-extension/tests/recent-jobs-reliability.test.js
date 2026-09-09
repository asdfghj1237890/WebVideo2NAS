import fs from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import { loadScriptIntoContext } from './helpers/load-script.js';

const NAS = 'http://nas.example';
const job = (id = 'j1', progress = 20, status = 'browser_uploading') => ({
  id, title: id, progress, status, mode: 'browser',
});

function harness() {
  document.body.innerHTML = fs.readFileSync('sidepanel.html', 'utf8');
  let data = [job()];
  let failure = null;
  let snapshots = {};
  let now = 0;
  let frameId = 0;
  const frames = new Map();
  const chrome = { storage: { onChanged: { addListener() {} }, session: { get: async () => ({ wv2nasBrowserTransferSnapshots: structuredClone(snapshots) }) } } };
  const doc = {
    addEventListener() {},
    getElementById: document.getElementById.bind(document),
    querySelector: document.querySelector.bind(document),
    querySelectorAll: document.querySelectorAll.bind(document),
    createElement: document.createElement.bind(document),
    documentElement: document.documentElement, body: document.body,
  };
  const fetch = vi.fn(async () => {
    if (failure instanceof Error) throw failure;
    return { ok: !failure, status: failure || 200,
      headers: { get: name => name === 'X-WV2N-Active-Jobs' ? 'complete' : null },
      json: async () => structuredClone(data) };
  });
  const ctx = loadScriptIntoContext('i18n.js', {
    document: doc, window: {}, navigator: { language: 'zh-TW' }, chrome, fetch,
    performance: { now: () => now },
    requestAnimationFrame: fn => { frames.set(++frameId, fn); return frameId; },
    cancelAnimationFrame: id => frames.delete(id),
  });
  ctx.importScripts('sidepanel.js');
  ctx.refreshOnboardingCoach = () => {};
  ctx.__eval(`settings = {nasEndpoint: '${NAS}', apiKey: 'key', uiLanguage: 'zh-TW'};`);
  ctx.applyUiLanguage();
  const tick = elapsed => {
    now += elapsed;
    const pending = Array.from(frames.values());
    frames.clear();
    pending.forEach(fn => fn(now));
  };
  return {
    ctx, fetch, frames, tick,
    data: value => { data = value; }, fail: value => { failure = value; },
    snapshots: value => { snapshots = value; },
    notice: document.getElementById('jobsUpdateNotice'),
    list: document.getElementById('recentJobsList'),
    connection: document.getElementById('connectionStatus'),
  };
}

describe('recent jobs availability', () => {
  it('removes an old browser job absent from a complete snapshot even with cached live progress', async () => {
    const h = harness();
    await h.ctx.loadRecentJobs();
    h.ctx.handleBrowserJobProgress({ jobId: 'j1', nasScope: NAS, done: 100, total: 100 });
    h.data([]); // Finished, and its creation time is outside recent history.
    await h.ctx.loadRecentJobs();
    expect(h.ctx.__eval('jobs')).toHaveLength(0);
    expect(h.frames.size).toBe(0);
  });

  it('requests unfinished jobs even on a newly opened panel and keeps all returned rows', async () => {
    const h = harness();
    h.data([...Array.from({length: 20}, (_, i) => job(`new-${i}`, 100, 'completed')), job('old-download', 40, 'downloading')]);
    await h.ctx.loadRecentJobs();
    expect(h.fetch.mock.calls[0][0]).toContain('limit=20&include_active=true');
    expect(h.list.children.length).toBe(21);
    expect(h.list.textContent).toContain('old-download');
  });

  it('shows stale data on disconnect, stops motion, and recovers on the next successful poll', async () => {
    const h = harness();
    await h.ctx.loadRecentJobs();
    h.data([job('j1', 40)]);
    await h.ctx.loadRecentJobs();
    expect(h.frames.size).toBe(1);
    h.fail(new TypeError('Failed to fetch'));
    await h.ctx.pollRecentJobs();
    expect(h.notice.hidden).toBe(false);
    expect(h.notice.textContent).toContain('顯示上次資料');
    expect(h.notice.textContent).toContain('正在重試');
    expect(h.ctx.__eval('recentJobsState.lastSuccessAt')).toBeTypeOf('number');
    expect(h.list.textContent).toContain('40.00%');
    expect(h.list.classList.contains('jobs-stale')).toBe(true);
    expect(h.connection.classList.contains('disconnected')).toBe(true);
    expect(h.frames.size).toBe(0);
    // /health alone cannot clear a jobs API failure.
    h.ctx.setConnectionState('connected', 'Healthy', 'health: OK');
    expect(h.connection.classList.contains('disconnected')).toBe(true);
    h.fail(null);
    h.data([job('j1', 100, 'completed')]);
    await h.ctx.pollRecentJobs();
    expect(h.notice.hidden).toBe(true);
    expect(h.list.classList.contains('jobs-stale')).toBe(false);
    expect(h.connection.classList.contains('connected')).toBe(true);
    expect(h.list.textContent).toContain('已完成');
  });

  it.each([401, 500, new DOMException('Timed out', 'AbortError')])('shows a first-load failure instead of an empty history: %s', async failure => {
    const h = harness();
    h.fail(failure);
    await h.ctx.loadRecentJobs();
    expect(h.notice.hidden).toBe(false);
    expect(h.notice.textContent).toContain('無法取得 NAS 任務');
    expect(h.notice.textContent).not.toContain('上次資料');
    expect(h.list.textContent).toContain('暫時無法取得');
    expect(h.connection.classList.contains('disconnected')).toBe(true);
  });

  it('rejects malformed success payloads without losing the last usable snapshot', async () => {
    const h = harness();
    await h.ctx.loadRecentJobs();
    h.data({ detail: 'invalid data' });
    await h.ctx.loadRecentJobs();
    expect(h.notice.hidden).toBe(false);
    expect(h.notice.title).toContain('資料格式');
    expect(h.list.textContent).toContain('j1');
  });

  it('ignores a late failure after a newer poll has succeeded', async () => {
    const h = harness();
    let reject;
    h.fetch.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    const old = h.ctx.loadRecentJobs();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    await h.ctx.loadRecentJobs();
    reject(new Error('old failure'));
    await old;
    expect(h.notice.hidden).toBe(true);
    expect(h.connection.classList.contains('connected')).toBe(true);
  });

  it('does not attribute a failed request to a different NAS selected while it was pending', async () => {
    const h = harness();
    let reject;
    h.fetch.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    const old = h.ctx.loadRecentJobs();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    h.ctx.__eval("settings.nasEndpoint = 'http://nas-b.example';");
    await h.ctx.loadRecentJobs();
    reject(new Error('NAS A offline'));
    await old;
    expect(h.notice.hidden).toBe(true);
    expect(document.getElementById('statusText').textContent).toBe('nas-b.example');
  });
});

describe('recent job animation ownership', () => {
  it('uses one animation for API polling and live progress, and cancels it at completion', async () => {
    const h = harness();
    await h.ctx.loadRecentJobs();
    h.data([job('j1', 30)]);
    await h.ctx.loadRecentJobs();
    h.ctx.handleBrowserJobProgress({ jobId: 'j1', nasScope: NAS, done: 40, total: 100 });
    expect(h.frames.size).toBe(1);
    expect(Array.from(h.ctx.__eval('jobTweens.keys()'))).toEqual([JSON.stringify([NAS, 'j1'])]);
    h.tick(700);
    expect(h.list.querySelector('[data-ring-pct]').textContent).toBe('40');
    h.ctx.handleBrowserJobProgress({ jobId: 'j1', nasScope: NAS, done: 50, total: 100 });
    h.data([job('j1', 100, 'completed')]);
    await h.ctx.loadRecentJobs();
    expect(h.frames.size).toBe(0);
    expect(h.ctx.__eval('jobTweens.size')).toBe(0);
    expect(h.list.querySelector('[data-job-ring]')).toBeNull();
  });

  it('cancels callbacks targeting replaced or removed rows', async () => {
    const h = harness();
    await h.ctx.loadRecentJobs();
    h.data([job('j1', 50)]);
    await h.ctx.loadRecentJobs();
    expect(h.frames.size).toBe(1);
    h.list.replaceChildren(); // Sort/language changes recreate rows.
    h.ctx.renderJobs();
    expect(h.frames.size).toBe(0);
    expect(h.list.querySelector('[data-ring-pct]').textContent).toBe('50');
    h.data([job('j1', 60)]);
    await h.ctx.loadRecentJobs();
    h.data([]);
    await h.ctx.loadRecentJobs();
    expect(h.frames.size).toBe(0);
    expect(h.ctx.__eval('jobTweens.size')).toBe(0);
  });

  it('keeps same-numbered jobs on two NAS in separate animations', () => {
    const h = harness();
    h.ctx.__eval(`jobs = ['${NAS}', 'http://nas-b.example'].map(endpoint => withJobTarget(
      {id:'same',title:endpoint,status:'browser_uploading',progress:0}, nasTargetOf(endpoint,'key')));`);
    h.ctx.renderJobs();
    h.ctx.handleBrowserJobProgress({ jobId: 'same', nasScope: NAS, done: 20, total: 100 });
    h.ctx.handleBrowserJobProgress({ jobId: 'same', nasScope: 'http://nas-b.example', done: 80, total: 100 });
    expect(h.frames.size).toBe(2);
    h.tick(700);
    expect(Array.from(h.list.querySelectorAll('[data-ring-pct]'), el => el.textContent)).toEqual(['20', '80']);
  });
});

describe('visible transfer speed', () => {
  const stage = (mb, activeMs) => ({
    bytes: mb * 1024 * 1024, attemptedBytes: mb * 1024 * 1024,
    requests: mb, requestMs: activeMs, activeMs,
    mbPerSecond: activeMs > 0 ? mb / (activeMs / 1000) : 0,
  });
  const snapshot = (mb, cdnMs, nasMs) => ({
    [JSON.stringify([NAS, 'j1'])]: {
      done: mb, total: 20, percent: mb * 5, ts: Date.now(),
      transferTimings: { cdn: stage(mb, cdnMs), nas: stage(mb, nasMs) },
    },
  });

  it('shows late-arriving session metrics in separate rows and preserves expanded details across updates', async () => {
    const h = harness();
    await h.ctx.loadRecentJobs();
    expect(h.list.querySelector('[data-browser-transfer]')).toBeNull();
    h.snapshots(snapshot(8, 2000, 100));
    await h.ctx.loadRecentJobs();
    const details = h.list.querySelector('[data-browser-transfer]');
    expect(details).not.toBeNull();
    expect(details.querySelector('.job-transfer-heading').textContent).toBe('平均速度');
    expect(Array.from(details.querySelectorAll('.job-transfer-reading strong'), el => el.textContent)).toEqual(['4.00 MB/s', '80.0 MB/s']);
    expect(details.querySelector('summary').textContent).toContain('2.0s');
    expect(details.querySelector('[data-transfer-detail]').textContent).toContain('並非即時速度');
    details.open = true;
    h.snapshots(snapshot(12, 4000, 200));
    await h.ctx.loadRecentJobs();
    expect(h.list.querySelector('[data-browser-transfer]')).toBe(details);
    expect(details.open).toBe(true);
    expect(details.querySelectorAll('.job-transfer-stage')).toHaveLength(2);
    expect(Array.from(details.querySelectorAll('.job-transfer-reading strong'), el => el.textContent)).toEqual(['3.00 MB/s', '60.0 MB/s']);
  });

  it('does not label an unmeasured stage as zero speed', async () => {
    const h = harness();
    h.snapshots({ [JSON.stringify([NAS, 'j1'])]: {
      done: 1, total: 20, percent: 5, ts: Date.now(),
      transferTimings: { cdn: stage(1, 1000), nas: stage(0, 0) },
    } });
    await h.ctx.loadRecentJobs();
    expect(Array.from(h.list.querySelectorAll('.job-transfer-reading strong'), el => el.textContent)).toEqual(['1.00 MB/s', '—']);
  });
});
