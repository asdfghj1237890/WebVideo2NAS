import fs from 'node:fs';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { loadScriptIntoContext } from './helpers/load-script.js';

const profiles = [
  { id: 'home', name: 'Home NAS', endpoint: 'http://home.example', apiKey: 'home-key', subdir: 'movies' },
  { id: 'work', name: 'Work NAS', endpoint: 'http://work.example', apiKey: 'work-key', subdir: '' },
];
const mirror = p => ({ activeProfileId: p.id, nasEndpoint: p.endpoint, apiKey: p.apiKey, nasOutputSubdir: p.subdir });
const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness(initial = {}) {
  document.body.innerHTML = fs.readFileSync('sidepanel.html', 'utf8');
  const stored = { nasProfiles: structuredClone(profiles), ...mirror(profiles[0]), uiLanguage: 'zh-TW', ...initial };
  const listeners = [];
  const emit = patch => {
    const changes = {};
    for (const [key, value] of Object.entries(patch)) {
      if (JSON.stringify(stored[key]) !== JSON.stringify(value)) changes[key] = { oldValue: stored[key], newValue: value };
      stored[key] = value;
    }
    listeners.forEach(fn => fn(changes, 'sync'));
  };
  const chrome = {
    storage: {
      sync: { get: vi.fn(async () => structuredClone(stored)), set: vi.fn(async patch => emit(patch)) },
      local: { get: vi.fn(async () => ({})), set: vi.fn(async () => {}) },
      onChanged: { addListener: fn => listeners.push(fn) },
    },
    runtime: { openOptionsPage: vi.fn(), onMessage: { addListener() {} }, sendMessage: (_m, cb) => cb?.({}), lastError: null },
    tabs: { query: (_q, cb) => cb([]), onUpdated: { addListener() {} }, onActivated: { addListener() {} } },
  };
  const doc = {
    addEventListener() {},
    getElementById: document.getElementById.bind(document),
    querySelector: document.querySelector.bind(document),
    querySelectorAll: document.querySelectorAll.bind(document),
    createElement: document.createElement.bind(document),
    get activeElement() { return document.activeElement; },
    documentElement: document.documentElement, body: document.body,
  };
  const ctx = loadScriptIntoContext('i18n.js', { document: doc, window: {}, navigator: { language: 'zh-TW' }, chrome, fetch: vi.fn(async () => ({ ok: true })) });
  ctx.importScripts('sidepanel.js');
  const renderDetected = ctx.renderDetectedUrls;
  ctx.renderDetectedUrls = vi.fn();
  ctx.renderJobs = vi.fn();
  ctx.loadRecentJobs = vi.fn(async () => {});
  ctx.refreshOnboardingCoach = vi.fn();
  ctx.applySettingsSnapshot(structuredClone(stored));
  ctx.applyUiLanguage();
  ctx.setupEventListeners();
  return { ctx, chrome, stored, emit, renderDetected, select: document.getElementById('profileSelect') };
}

describe('side-panel profile switching', () => {
  it('switches credentials, folder and profile in one write and refreshes the visible destination', async () => {
    const h = harness();
    expect(h.select.value).toBe('home');
    h.select.value = 'work';
    h.select.dispatchEvent(new Event('change'));
    await settle();
    expect(h.chrome.storage.sync.set).toHaveBeenCalledExactlyOnceWith(mirror(profiles[1]));
    expect(h.select.value).toBe('work');
    expect(document.getElementById('profileFolder').textContent).toBe('儲存至 /downloads');
    expect(h.ctx.__eval('currentNasTarget().endpoint')).toBe('http://work.example');
    expect(h.ctx.loadRecentJobs).toHaveBeenCalled();
    expect(h.chrome.runtime.openOptionsPage).not.toHaveBeenCalled();
  });

  it('uses freshly edited profile data, including a folder-only switch on the same NAS', async () => {
    const h = harness();
    h.stored.nasProfiles[1] = { ...profiles[0], id: 'work', name: 'Archive', subdir: 'archive' };
    await h.ctx.switchProfile('work');
    await settle();
    expect(h.chrome.storage.sync.set).toHaveBeenCalledExactlyOnceWith(mirror(h.stored.nasProfiles[1]));
    expect(h.select.value).toBe('work');
    expect(document.getElementById('profileFolder').textContent).toContain('/downloads/archive');
  });

  it('restores the original selection after a rejected write', async () => {
    const h = harness();
    h.chrome.storage.sync.set.mockRejectedValueOnce(new Error('quota'));
    expect(await h.ctx.switchProfile('work')).toBe(false);
    await settle();
    expect(h.select.value).toBe('home');
    expect(h.select.disabled).toBe(false);
    expect(h.stored.nasEndpoint).toBe(profiles[0].endpoint);
    expect(document.getElementById('profileFeedback').textContent).toContain('切換失敗');
  });

  it('does not resurrect a deleted profile or write a no-op selection', async () => {
    const h = harness();
    h.stored.nasProfiles.pop();
    expect(await h.ctx.switchProfile('work')).toBe(false);
    await settle();
    expect(h.select.options.length).toBe(1);
    expect(await h.ctx.switchProfile('home')).toBe(true);
    expect(h.chrome.storage.sync.set).not.toHaveBeenCalled();
  });

  it('prevents overlapping writes while a switch is pending', async () => {
    const h = harness();
    let release;
    h.chrome.storage.sync.set.mockImplementationOnce(patch => new Promise(resolve => { release = () => { h.emit(patch); resolve(); }; }));
    const switching = h.ctx.switchProfile('work');
    await settle();
    expect(h.select.disabled).toBe(true);
    expect(h.select.value).toBe('home');
    expect(await h.ctx.switchProfile('home')).toBe(false);
    release();
    await switching;
    await settle();
    expect(h.select.disabled).toBe(false);
    expect(h.select.value).toBe('work');
    expect(h.chrome.storage.sync.set).toHaveBeenCalledTimes(1);
  });

  it('reflects external profile changes and keeps user-supplied names as text', async () => {
    const h = harness();
    h.emit({ nasProfiles: [profiles[0], { ...profiles[1], name: '<img src=x onerror=alert(1)>' }], ...mirror(profiles[1]) });
    await settle();
    expect(h.select.value).toBe('work');
    expect(h.select.selectedOptions[0].textContent).toBe('<img src=x onerror=alert(1)>');
    expect(h.select.querySelector('img')).toBeNull();
    expect(h.select.outerHTML).not.toContain('work-key');
  });

  it('does not mislabel customized credentials as a saved profile', () => {
    const h = harness({ apiKey: 'custom-key' });
    expect(h.select.value).toBe('');
    expect(h.select.selectedOptions[0].textContent).toBe('目前連線（自訂）');
    expect(h.select.disabled).toBe(false);
  });

  it('supports fresh installs and legacy connections without creating profiles', () => {
    const h = harness({ nasProfiles: [], nasEndpoint: '', apiKey: '', activeProfileId: null });
    expect(h.select.disabled).toBe(true);
    expect(h.select.selectedOptions[0].textContent).toBe('尚未儲存 profile');
    document.getElementById('manageProfilesBtn').click();
    expect(h.chrome.runtime.openOptionsPage).toHaveBeenCalledOnce();
    h.emit({ nasEndpoint: 'http://legacy.example', apiKey: 'legacy-key' });
    expect(h.select.selectedOptions[0].textContent).toBe('目前連線（自訂）');
    expect(h.chrome.storage.sync.set).not.toHaveBeenCalled();
  });

  it('supports keyboard selection and preserves focus across a detection refresh', () => {
    const h = harness();
    const urls = Array.from({ length: 7 }, (_, i) => ({ url: `https://cdn.example.com/${i}.mp4` }));
    h.ctx.__eval(`detectedUrls = ${JSON.stringify(urls)};`);
    h.renderDetected();
    let tile = document.querySelector('.tile');
    tile.focus();
    tile.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
    expect(tile.getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('bulkBtn').disabled).toBe(false);
    h.renderDetected();
    tile = document.querySelector('.tile');
    expect(document.activeElement).toBe(tile);
    expect(tile.getAttribute('aria-checked')).toBe('true');
    tile.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    expect(tile.getAttribute('aria-checked')).toBe('false');
    expect(document.getElementById('bulkBtn').disabled).toBe(true);
  });
});

describe('collapsible destination', () => {
  it('remembers collapsing locally without changing the active profile', async () => {
    const h = harness();
    await h.ctx.initDestinationDisclosure();
    const details = document.getElementById('destinationDetails');
    expect(details.open).toBe(false);
    expect(document.getElementById('destinationToggleText').textContent).toBe('展開');
    expect(h.chrome.storage.local.set).not.toHaveBeenCalled();
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    await settle();
    expect(h.chrome.storage.local.set).toHaveBeenCalledExactlyOnceWith({ destinationCollapsed: false });
    expect(document.getElementById('destinationToggleText').textContent).toBe('收合');
    details.open = false;
    details.dispatchEvent(new Event('toggle'));
    await settle();
    expect(h.chrome.storage.local.set).toHaveBeenLastCalledWith({ destinationCollapsed: true });
    expect(h.chrome.storage.sync.set).not.toHaveBeenCalled();
    expect(document.getElementById('profileCompactName').textContent).toBe('Home NAS');
    expect(document.getElementById('destinationSummary').title).toBe('展開下載目的地');
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    await settle();
    expect(h.chrome.storage.local.set).toHaveBeenLastCalledWith({ destinationCollapsed: false });
    expect(document.getElementById('destinationSummary').title).toBe('收合下載目的地');
  });

  it('restores a collapsed panel and keeps its name and status current', async () => {
    const h = harness();
    h.chrome.storage.local.get.mockResolvedValue({ destinationCollapsed: true });
    await h.ctx.initDestinationDisclosure();
    expect(document.getElementById('destinationDetails').open).toBe(false);
    h.emit(mirror(profiles[1]));
    await settle();
    expect(document.getElementById('profileCompactName').textContent).toBe('Work NAS');
    h.ctx.setConnectionState('disconnected', 'Offline', 'http://work.example: offline');
    expect(document.getElementById('profileCompactStatus').textContent).toBe('未連線');
    expect(document.getElementById('profileCompactStatus').dataset.state).toBe('disconnected');
    expect(document.getElementById('destinationDetails').open).toBe(false);
    expect(h.chrome.storage.local.set).not.toHaveBeenCalled();
  });

  it('does not let a late preference read undo a user toggle', async () => {
    const h = harness();
    let release;
    h.chrome.storage.local.get.mockImplementation(() => new Promise(resolve => { release = resolve; }));
    const init = h.ctx.initDestinationDisclosure();
    const details = document.getElementById('destinationDetails');
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    release({ destinationCollapsed: true });
    await init;
    await settle();
    expect(details.open).toBe(true);
    expect(h.chrome.storage.local.set).toHaveBeenCalledWith({ destinationCollapsed: false });
  });

  it('keeps toggling usable when preference storage fails', async () => {
    const h = harness();
    h.chrome.storage.local.get.mockRejectedValue(new Error('unavailable'));
    h.chrome.storage.local.set.mockRejectedValue(new Error('unavailable'));
    await h.ctx.initDestinationDisclosure();
    const details = document.getElementById('destinationDetails');
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    await settle();
    details.open = false;
    details.dispatchEvent(new Event('toggle'));
    await settle();
    expect(details.open).toBe(false);
    expect(h.chrome.storage.local.set).toHaveBeenLastCalledWith({ destinationCollapsed: true });
  });

  it('honors an explicitly saved expanded preference', async () => {
    const h = harness();
    h.chrome.storage.local.get.mockResolvedValue({ destinationCollapsed: false });
    await h.ctx.initDestinationDisclosure();
    expect(document.getElementById('destinationDetails').open).toBe(true);
    expect(document.getElementById('destinationToggleText').textContent).toBe('收合');
    expect(h.chrome.storage.local.set).not.toHaveBeenCalled();
  });
});

describe('quality filters in the redesigned sidebar', () => {
  function qualityHarness(rows) {
    const h = harness();
    h.ctx.renderDetectedUrls = h.renderDetected;
    h.ctx.__eval(`detectedUrls = ${JSON.stringify(rows)};`);
    h.renderDetected();
    return h;
  }
  const rows = [
    { url: 'https://cdn.example.com/track-a.m3u8', qualityHeight: 720, playbackObserved: true },
    { url: 'https://cdn.example.com/track-b.m3u8', qualityHeight: 720, playbackObserved: true },
    { url: 'https://cdn.example.com/track-c.m3u8', qualityHeight: 360, playbackObserved: false },
  ];
  const chip = quality => document.querySelector(`#qualityChips [data-q="${quality}"]`);
  const visibleUrls = () => Array.from(document.querySelectorAll('.tile'), tile => tile.dataset.url);

  it('filters 720p and 360p below the bulk threshold, then restores all videos', () => {
    qualityHarness(rows);
    expect(document.getElementById('toolbar').hidden).toBe(false);
    expect(chip('720p').querySelector('.chip-count').textContent).toBe('2');
    expect(chip('360p').querySelector('.chip-count').textContent).toBe('1');
    chip('720p').focus();
    chip('720p').click();
    expect(visibleUrls()).toEqual(rows.slice(0, 2).map(row => row.url));
    expect(chip('720p').getAttribute('aria-pressed')).toBe('true');
    expect(document.activeElement).toBe(chip('720p'));
    chip('360p').click();
    expect(visibleUrls()).toEqual([rows[2].url]);
    expect(document.querySelector('.tile .send-btn').disabled).toBe(true);
    expect(chip('720p').getAttribute('aria-pressed')).toBe('false');
    expect(chip('360p').getAttribute('aria-pressed')).toBe('true');
    chip('all').click();
    expect(visibleUrls()).toEqual(rows.map(row => row.url));
    expect(chip('all').getAttribute('aria-pressed')).toBe('true');
  });

  it('paints All immediately when the selected quality disappears', () => {
    const h = qualityHarness(rows);
    chip('720p').click();
    const replacement = [
      { url: 'https://cdn.example.com/480p/video.mp4' },
      { url: 'https://cdn.example.com/360p/video.mp4' },
    ];
    h.ctx.__eval(`detectedUrls = ${JSON.stringify(replacement)};`);
    h.renderDetected();
    expect(chip('720p')).toBeNull();
    expect(chip('all').getAttribute('aria-pressed')).toBe('true');
    expect(visibleUrls()).toEqual(replacement.map(row => row.url));
  });

  it('does not invent resolutions for streams without quality metadata', () => {
    qualityHarness([{ url: 'https://cdn.example.com/master.m3u8' }]);
    expect(document.getElementById('toolbar').hidden).toBe(true);
    expect(chip('720p')).toBeNull();
    expect(chip('360p')).toBeNull();
    expect(visibleUrls()).toHaveLength(1);
  });
});

describe('an open options page follows the selected profile', () => {
  beforeEach(() => { document.body.innerHTML = fs.readFileSync('options/options.html', 'utf8'); });
  function optionsContext() {
    const ctx = loadScriptIntoContext('options/options.js', {
      document: { getElementById: document.getElementById.bind(document), addEventListener() {} },
      window: {}, chrome: {},
    });
    ctx.refreshOnboardingStatus = vi.fn();
    ctx.renderProfilesPane = vi.fn();
    ctx.setPingState = vi.fn();
    ctx.showStatus = vi.fn();
    ctx.__eval(`savedSnapshot = ${JSON.stringify({ nasEndpoint: profiles[0].endpoint, apiKey: profiles[0].apiKey })};`);
    document.getElementById('nasEndpoint').value = profiles[0].endpoint;
    document.getElementById('apiKey').value = profiles[0].apiKey;
    return ctx;
  }
  const changes = Object.fromEntries(Object.entries(mirror(profiles[1])).map(([k, v]) => [k, { newValue: v }]));

  it('updates clean connection fields and the active marker', () => {
    const ctx = optionsContext();
    ctx.applyExternalProfileChanges(changes);
    expect(document.getElementById('nasEndpoint').value).toBe(profiles[1].endpoint);
    expect(document.getElementById('apiKey').value).toBe(profiles[1].apiKey);
    expect(ctx.__eval('activeProfileId')).toBe('work');
    expect(ctx.renderProfilesPane).toHaveBeenCalled();
    expect(ctx.isDirty()).toBe(false);
  });

  it('preserves unsaved edits and discards them back to the new destination', () => {
    const ctx = optionsContext();
    document.getElementById('nasEndpoint').value = 'http://draft.example';
    ctx.applyExternalProfileChanges(changes);
    expect(document.getElementById('nasEndpoint').value).toBe('http://draft.example');
    expect(ctx.__eval('savedSnapshot.nasEndpoint')).toBe(profiles[1].endpoint);
    expect(ctx.isDirty()).toBe(true);
    ctx.discardChanges();
    expect(document.getElementById('nasEndpoint').value).toBe(profiles[1].endpoint);
    expect(document.getElementById('apiKey').value).toBe(profiles[1].apiKey);
    expect(ctx.isDirty()).toBe(false);
  });
});
