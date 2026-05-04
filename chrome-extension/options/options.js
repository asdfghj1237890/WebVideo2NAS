// Options Page Script for WebVideo2NAS — terminal/dotfile UI.

const NAV_FILES = {
  connection: 'connection.toml',
  profiles:   'profiles.toml',
  prefs:      'prefs.toml',
  about:      'about',
};

let currentNav = 'connection';
let theme = 'dark';
let savedSnapshot = { nasEndpoint: '', apiKey: '' }; // last persisted (for dirty + discard)

// Profiles state (loaded from chrome.storage.sync)
let profiles = [];           // [{ id, name, endpoint, apiKey, subdir }]
let activeProfileId = null;

// Subdir validation: relative path under /downloads on the NAS.
// Empty = save to root (current behavior). Reject traversal/absolute/control chars.
function sanitizeSubdir(input) {
  if (input == null) return '';
  let s = String(input).trim();
  if (!s) return '';
  s = s.replace(/\\/g, '/');
  return s.split('/').map(p => p.trim()).filter(Boolean).join('/');
}
function subdirError(input) {
  const s = sanitizeSubdir(input);
  if (!s) return null;
  if (s.length > 255) return 'too long (max 255 chars)';
  for (const part of s.split('/')) {
    if (part === '..' || part === '.') return `invalid component "${part}"`;
    if (/[\x00-\x1f<>:"|?*]/.test(part)) return `invalid char in "${part}"`;
    if (/^[a-zA-Z]:$/.test(part)) return 'drive letters not allowed';
  }
  return null;
}

function newProfileId() {
  return 'p_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 6);
}

function activeProfile() {
  return profiles.find(p => p.id === activeProfileId) || null;
}

let i18n = null;
function t(key, vars) { return i18n ? i18n.t(key, vars) : key; }
function tHtml(key, vars) { return i18n ? i18n.tHtml(key, vars) : t(key, vars); }

// ---------- DOM helpers ----------
function $(id) { return document.getElementById(id); }
function setText(id, text) { const el = $(id); if (el) el.textContent = text; }

// ---------- Theme ----------
function applyTheme(next) {
  theme = next === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', theme);
  const sun = $('themeIconSun');
  const moon = $('themeIconMoon');
  if (sun && moon) {
    // Show the icon you'd switch TO (not the current mode).
    sun.style.display  = theme === 'dark'  ? 'none'  : 'block';
    moon.style.display = theme === 'light' ? 'none'  : 'block';
  }
}

async function setTheme(next) {
  applyTheme(next);
  try { await chrome.storage.sync.set({ uiTheme: theme }); } catch (_) {}
}

// ---------- Nav ----------
// Only connection.toml goes through the explicit save/discard flow.
// profiles + prefs auto-save on edit; about is read-only.
const PANES_WITH_SAVE = new Set(['connection']);

function switchNav(id) {
  if (!NAV_FILES[id]) return;
  currentNav = id;

  document.querySelectorAll('.nav-item').forEach(b => {
    b.classList.toggle('active', b.dataset.nav === id);
  });
  document.querySelectorAll('.pane').forEach(p => {
    p.classList.toggle('active', p.dataset.pane === id);
  });

  const filename = NAV_FILES[id];
  setText('titleFilename', filename);
  setText('statusFilename', filename);

  // Show save/discard + unsaved counter only when the active pane uses them.
  const usesSave = PANES_WITH_SAVE.has(id);
  const bar = $('statusBar');
  if (bar) bar.classList.toggle('autosave-pane', !usesSave);

  recomputeGutter();
}

// ---------- Line gutter ----------
function recomputeGutter() {
  const editor = $('editor');
  const gutter = $('gutter');
  if (!editor || !gutter) return;
  // Count the number of "lines" in the active pane based on its scroll height.
  // Each row in the design is 22px; we compute lines from height.
  const activePane = editor.querySelector('.pane.active');
  if (!activePane) return;
  const lines = Math.max(20, Math.ceil(activePane.scrollHeight / 22) + 4);
  // Build gutter content lazily — only rebuild if line count changed.
  if (gutter.dataset.lines === String(lines)) return;
  gutter.dataset.lines = String(lines);
  gutter.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (let i = 1; i <= lines; i++) {
    const d = document.createElement('div');
    d.textContent = String(i);
    frag.appendChild(d);
  }
  gutter.appendChild(frag);
}

// ---------- Toggle helpers (true/false buttons) ----------
function setToggle(id, value) {
  const el = $(id);
  if (!el) return;
  el.dataset.value = value ? 'true' : 'false';
  el.textContent = value ? 'true' : 'false';
}
function getToggle(id) {
  const el = $(id);
  return el ? el.dataset.value === 'true' : true;
}

// ---------- Dirty tracking ----------
function isDirty() {
  const ep = $('nasEndpoint').value.trim();
  const ak = $('apiKey').value.trim();
  return ep !== savedSnapshot.nasEndpoint || ak !== savedSnapshot.apiKey;
}
function unsavedFieldCount() {
  const ep = $('nasEndpoint').value.trim();
  const ak = $('apiKey').value.trim();
  let n = 0;
  if (ep !== savedSnapshot.nasEndpoint) n++;
  if (ak !== savedSnapshot.apiKey) n++;
  return n;
}
function refreshDirtyIndicator() {
  const dirty = isDirty();
  const bar = $('statusBar');
  if (bar) bar.classList.toggle('dirty', dirty);
  setText('unsavedCount', String(unsavedFieldCount()));
  $('saveBtn').disabled = !dirty;
  $('discardBtn').disabled = !dirty;
}

// ---------- Toast (status bar) ----------
let toastTimer = null;
function showStatus(message, type) {
  const el = $('statusToast');
  if (!el) return;
  el.textContent = message;
  el.className = `status-toast ${type || 'info'}`;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  if (type !== 'error') {
    toastTimer = setTimeout(() => el.classList.add('hidden'), 4000);
  }
}

// ---------- Ping state ----------
function setPingState(rowId, state, label) {
  const row = $(rowId);
  if (!row) return;
  row.classList.remove('idle', 'pinging', 'ok', 'err');
  row.classList.add(state);
  const labelEl = row.querySelector('.label');
  if (labelEl) labelEl.textContent = label;
}

// ---------- i18n: localize all dynamic text ----------
function localizeStaticText() {
  document.title = t('options.pageTitle');

  // inline buttons
  setText('testBtnText', t('options.btn.testInline') || 'test');
  setText('apiKeyToggleText', getToggleVisibleLabel());
  setText('apiKeyCopyText', t('options.btn.copy') || 'copy');
  setText('saveHint', t('options.cmd.saveHint'));
  setText('discardHint', t('options.cmd.discardHint'));
  setText('autosaveHint', t('options.cmd.autosaveHint') || '↻ auto-saves on edit');

  // about steps
  setText('howToUseStep1', t('options.howToUse.step1'));
  setText('howToUseStep2', t('options.howToUse.step2'));
  setText('howToUseStep3', t('options.howToUse.step3'));
  setText('howToUseStep4', t('options.howToUse.step4'));
  setText('howToUseStep5', t('options.howToUse.step5'));

  setText('troubleshoot1', t('options.troubleshoot.s1'));
  setText('troubleshoot2', t('options.troubleshoot.s2'));
  setText('troubleshoot3', t('options.troubleshoot.s3'));
  setText('troubleshoot4', t('options.troubleshoot.s4'));

  setText('repoLinkText', t('options.repo.openInBrowser'));

  // language select "auto" label
  const autoOption = document.querySelector('#uiLanguage option[value=""]');
  if (autoOption) {
    const auto = t('options.uiLanguage.auto') || 'Auto';
    autoOption.textContent = `"auto"  ${auto}`;
  }

  // Inline comments next to toggles
  setText('autoDetectComment', '# ' + (t('options.autoDetect.help') || ''));
  setText('notifComment', '# ' + (t('options.showNotifications.help') || ''));
  setText('langComment', '# ' + (t('options.uiLanguage.help') || ''));
  setText('hiddenModeComment', '# ' + (t('options.hiddenMode.help') || ''));
  setText('hiddenModeUrlTemplateComment', '# ' + (t('options.hiddenModeUrlTemplate.help') || ''));

  // Profiles
  setText('addProfileText', t('options.profiles.addBtn') || '[profile.new] — save current as profile');
}

function getToggleVisibleLabel() {
  // Reflects what the button will DO when clicked.
  const masked = $('apiKey').classList.contains('masked');
  return masked ? (t('options.btn.show') || 'show') : (t('options.btn.hide') || 'hide');
}

function applyUiLanguage(uiLanguageRaw) {
  if (i18n) {
    const uiLanguage = uiLanguageRaw === 'zh' ? 'zh-TW' : (uiLanguageRaw || '');
    i18n.setLanguage((uiLanguage || '').trim());
  }
  localizeStaticText();
}

// ---------- Profiles ----------
function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function parseHostPort(endpoint) {
  try {
    const u = new URL(endpoint);
    return { host: u.hostname, port: u.port || (u.protocol === 'https:' ? '443' : '80') };
  } catch (_) {
    return { host: '', port: '' };
  }
}

async function persistProfiles() {
  await chrome.storage.sync.set({ nasProfiles: profiles, activeProfileId });
}

async function migrateOrLoadProfiles(stored) {
  profiles = Array.isArray(stored.nasProfiles) ? stored.nasProfiles.slice() : [];
  activeProfileId = stored.activeProfileId || null;

  // Migrate: if no profiles but legacy nasEndpoint exists, create a default profile
  if (profiles.length === 0 && stored.nasEndpoint) {
    const id = newProfileId();
    profiles = [{
      id,
      name: t('options.profiles.defaultName') || 'Default',
      endpoint: stored.nasEndpoint,
      apiKey: stored.apiKey || '',
      subdir: stored.nasOutputSubdir || '',
    }];
    activeProfileId = id;
    await persistProfiles();
  }

  // Backfill subdir on existing profiles loaded from older versions
  for (const p of profiles) {
    if (typeof p.subdir !== 'string') p.subdir = '';
  }

  // Ensure activeProfileId points to a real profile
  if (profiles.length > 0 && !profiles.some(p => p.id === activeProfileId)) {
    activeProfileId = profiles[0].id;
    await chrome.storage.sync.set({ activeProfileId });
  }
}

function renderProfilesPane() {
  const list = $('profilesList');
  if (!list) return;

  if (profiles.length === 0) {
    list.innerHTML = `<div class="comment"># No saved profiles yet — fill in connection.toml then click [+ profile.new]</div>`;
    setText('profileCount', '0');
    return;
  }
  setText('profileCount', String(profiles.length));

  const html = profiles.map(p => {
    const isActive = p.id === activeProfileId;
    const { host, port } = parseHostPort(p.endpoint);
    const safeName = escapeHtml(p.name || 'unnamed');
    const safeId = escapeHtml(p.id);
    const safeEndpoint = escapeHtml(p.endpoint || '');
    const safeHost = escapeHtml(host);
    const safePort = escapeHtml(port);
    const safeSubdir = escapeHtml(p.subdir || '');
    const activateLabel = t('options.profiles.activate') || 'activate';
    const deleteLabel = t('options.profiles.delete') || 'delete';
    const subdirHelp = t('options.profiles.subdirHelp') || '# subfolder under /downloads (blank = root)';
    const subdirPlaceholder = t('options.profiles.subdirPlaceholder') || 'e.g. anime/work-safe';
    return `
      <div class="profile-block">
        <div class="profile-head${isActive ? ' active' : ''}" data-pid="${safeId}" data-action="activate">
          <span>[profile.${safeId.replace(/^p_/, '').slice(0, 10)}]</span>
          ${isActive ? `<span class="marker">← ${escapeHtml(t('options.profiles.activeMark') || 'active')}</span>` : ''}
          <span class="actions">
            ${!isActive ? `<button class="inline-btn" type="button" data-action="activate" data-pid="${safeId}">${escapeHtml(activateLabel)}</button>` : ''}
            <button class="inline-btn" type="button" data-action="delete" data-pid="${safeId}">${escapeHtml(deleteLabel)}</button>
          </span>
        </div>
        <div class="kv">
          <span class="key">name</span>
          <span class="eq">=</span>
          <span class="val${isActive ? '' : ' readonly'}">"${safeName}"</span>
        </div>
        <div class="kv">
          <span class="key">endpoint</span>
          <span class="eq">=</span>
          <span class="val readonly" title="${safeEndpoint}">"${escapeHtml(p.endpoint || '')}"</span>
        </div>
        <div class="kv">
          <span class="key">host</span>
          <span class="eq">=</span>
          <span class="val readonly">"${safeHost}"</span>
        </div>
        <div class="kv">
          <span class="key">port</span>
          <span class="eq">=</span>
          <span class="val readonly">${safePort}</span>
        </div>
        <div class="kv">
          <span class="key">subdir</span>
          <span class="eq">=</span>
          <span class="kv-edit">
            <span class="quote">"</span>
            <input class="input profile-subdir" type="text"
                   data-pid="${safeId}"
                   value="${safeSubdir}"
                   placeholder="${escapeHtml(subdirPlaceholder)}"
                   autocomplete="off">
            <span class="quote">"</span>
          </span>
          <span class="comment-inline" data-subdir-comment="${safeId}">${escapeHtml(subdirHelp)}</span>
        </div>
      </div>
    `;
  }).join('');
  list.innerHTML = html;

  // Wire up clicks
  list.querySelectorAll('[data-action="activate"]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      switchActiveProfile(el.dataset.pid);
    });
  });
  list.querySelectorAll('[data-action="delete"]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteProfile(el.dataset.pid);
    });
  });

  // Wire up subdir inputs (debounced save on input, immediate on blur)
  list.querySelectorAll('.profile-subdir').forEach(input => {
    let timer = null;
    const commit = () => updateProfileSubdir(input.dataset.pid, input.value, input);
    input.addEventListener('input', () => {
      validateSubdirInputUI(input);
      clearTimeout(timer);
      timer = setTimeout(commit, 400);
    });
    input.addEventListener('blur', () => {
      clearTimeout(timer);
      commit();
    });
    input.addEventListener('click', (e) => e.stopPropagation());
  });

  recomputeGutter();
}

function validateSubdirInputUI(input) {
  const err = subdirError(input.value);
  input.classList.toggle('input-error', !!err);
  const comment = document.querySelector(`[data-subdir-comment="${input.dataset.pid}"]`);
  if (comment) {
    if (err) {
      comment.textContent = '# ⚠ ' + err;
    } else {
      comment.textContent = t('options.profiles.subdirHelp')
        || '# subfolder under /downloads (blank = root)';
    }
  }
}

async function updateProfileSubdir(pid, raw, inputEl) {
  const p = profiles.find(x => x.id === pid);
  if (!p) return;
  const err = subdirError(raw);
  if (err) {
    showStatus((t('options.profiles.subdirInvalid') || 'Invalid subdir') + ': ' + err, 'error');
    return;
  }
  const cleaned = sanitizeSubdir(raw);
  if (p.subdir === cleaned) return;
  p.subdir = cleaned;
  if (inputEl && inputEl.value !== cleaned) inputEl.value = cleaned;
  await persistProfiles();
  // If active profile, also update the storage mirror so background.js picks it up
  if (pid === activeProfileId) {
    await chrome.storage.sync.set({ nasOutputSubdir: cleaned });
  }
  showStatus(t('options.profiles.subdirSaved') || 'subdir saved', 'success');
}

async function switchActiveProfile(id) {
  const p = profiles.find(x => x.id === id);
  if (!p) return;
  activeProfileId = id;
  // Push the profile's credentials into the live fields + storage so background
  // / sidepanel pick them up immediately.
  $('nasEndpoint').value = p.endpoint || '';
  $('apiKey').value      = p.apiKey   || '';
  savedSnapshot = { nasEndpoint: p.endpoint || '', apiKey: p.apiKey || '' };
  await chrome.storage.sync.set({
    nasEndpoint:     p.endpoint || '',
    apiKey:          p.apiKey   || '',
    nasOutputSubdir: p.subdir   || '',
    activeProfileId,
  });
  refreshDirtyIndicator();
  try { setText('pingHost', new URL(p.endpoint).host); } catch (_) {}
  setPingState('pingRow',     'idle', t('options.ping.idle') || 'not tested');
  setPingState('lastPingRow', 'idle', t('options.ping.idle') || 'not tested');
  setText('lastPingNote', '');
  renderProfilesPane();
  showStatus(t('options.profiles.switched', { name: p.name }) || `Switched to ${p.name}`, 'success');
}

async function deleteProfile(id) {
  const p = profiles.find(x => x.id === id);
  if (!p) return;
  const confirmMsg = (t('options.profiles.confirmDelete', { name: p.name })
    || `Delete profile "${p.name}"?`);
  if (!window.confirm(confirmMsg)) return;

  profiles = profiles.filter(x => x.id !== id);

  // If the active profile was deleted, switch to the first remaining one
  if (activeProfileId === id) {
    if (profiles.length > 0) {
      await switchActiveProfile(profiles[0].id);
    } else {
      activeProfileId = null;
    }
  }
  await persistProfiles();
  renderProfilesPane();
  showStatus(t('options.profiles.deleted', { name: p.name }) || `Deleted "${p.name}"`, 'info');
}

async function addCurrentAsProfile() {
  const ep = $('nasEndpoint').value.trim();
  const ak = $('apiKey').value.trim();
  if (!ep || !ak) {
    showStatus(t('options.profiles.fillFirst') || 'Fill endpoint + api_key on connection.toml first', 'error');
    switchNav('connection');
    return;
  }
  const defaultName = (() => {
    try { return new URL(ep).hostname; } catch (_) { return 'profile'; }
  })();
  const promptMsg = t('options.profiles.namePrompt') || 'Name for this profile:';
  const name = (window.prompt(promptMsg, defaultName) || '').trim();
  if (!name) return;

  const id = newProfileId();
  // Inherit current active profile's subdir as a sensible default for the new one
  const inheritedSubdir = activeProfile()?.subdir || '';
  profiles.push({ id, name, endpoint: ep, apiKey: ak, subdir: inheritedSubdir });
  activeProfileId = id;
  await persistProfiles();
  await chrome.storage.sync.set({ nasOutputSubdir: inheritedSubdir });
  renderProfilesPane();
  showStatus(t('options.profiles.created', { name }) || `Saved profile "${name}"`, 'success');
}

// ---------- Save / Discard ----------
async function saveSettings() {
  if (!isDirty()) return;

  const nasEndpoint = $('nasEndpoint').value.trim();
  const apiKey      = $('apiKey').value.trim();

  if (!nasEndpoint) { showStatus(t('options.status.enterNasEndpoint'), 'error'); return; }
  if (!apiKey)      { showStatus(t('options.status.enterApiKey'),      'error'); return; }

  // Validate URL
  try {
    const url = new URL(nasEndpoint);
    if (!url.protocol.startsWith('http')) throw new Error('Invalid protocol');
  } catch (_) {
    showStatus(t('options.status.invalidUrl'), 'error');
    return;
  }

  const cleanEndpoint = nasEndpoint.replace(/\/$/, '');

  // Also push the new values into the active profile (if any) so profiles stay in sync.
  const ap = activeProfile();
  if (ap) {
    ap.endpoint = cleanEndpoint;
    ap.apiKey   = apiKey;
    await persistProfiles();
    renderProfilesPane();
  }

  await chrome.storage.sync.set({ nasEndpoint: cleanEndpoint, apiKey });

  $('nasEndpoint').value = cleanEndpoint;
  savedSnapshot = { nasEndpoint: cleanEndpoint, apiKey };
  refreshDirtyIndicator();
  showStatus(t('options.status.saved'), 'success');

  // Auto-test after save (non-blocking).
  setTimeout(testConnection, 400);
}

function discardChanges() {
  $('nasEndpoint').value = savedSnapshot.nasEndpoint || '';
  $('apiKey').value      = savedSnapshot.apiKey      || '';
  refreshDirtyIndicator();
  showStatus(t('options.status.reverted') || 'Reverted to saved values', 'info');
}

// ---------- Preferences (auto-saved on change) ----------
async function savePreferences() {
  const autoDetect       = getToggle('autoDetect');
  const showNotifications = getToggle('showNotifications');
  const uiLanguage       = ($('uiLanguage').value || '').trim();
  // Hidden-mode (the AV-task quick-input flow). The toggle gates visibility
  // of the side-panel input box; the URL template is the per-site pattern
  // we substitute the user's input code into. Empty template falls back to
  // the missav default at the read site to avoid storing a placeholder.
  const hiddenMode               = getToggle('hiddenMode');
  const hiddenModeUrlTemplate    = ($('hiddenModeUrlTemplate').value || '').trim();
  await chrome.storage.sync.set({
    autoDetect, showNotifications, uiLanguage,
    hiddenMode, hiddenModeUrlTemplate,
  });
}

// ---------- Test connection ----------
async function testConnection() {
  const nasEndpoint = $('nasEndpoint').value.trim();
  const apiKey      = $('apiKey').value.trim();

  if (!nasEndpoint || !apiKey) {
    showStatus(t('options.status.enterBoth'), 'error');
    return;
  }

  setPingState('pingRow',     'pinging', t('options.ping.pinging') || 'pinging…');
  setPingState('lastPingRow', 'pinging', t('options.ping.pinging') || 'pinging…');
  setText('lastPingNote', '');
  $('testBtn').disabled = true;
  setText('testBtnText', t('options.btn.testing') || 'pinging…');

  const t0 = performance.now();
  try {
    // health validates auth; root carries the version (no auth needed). Run in parallel.
    const healthP = fetch(`${nasEndpoint}/api/health`, {
      method: 'GET',
      headers: { 'Authorization': `Bearer ${apiKey}` },
    });
    const rootP = fetch(`${nasEndpoint}/`).catch(() => null);
    const response = await healthP;

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    const data = await response.json();
    const ms = Math.max(1, Math.round(performance.now() - t0));

    if (data.status === 'healthy' || data.status === 'running') {
      setPingState('pingRow',     'ok', `${ms} ms`);
      setPingState('lastPingRow', 'ok', `${ms} ms`);

      // Pull server version from the root endpoint (best effort).
      let version = null;
      try {
        const rootRes = await rootP;
        if (rootRes && rootRes.ok) {
          const rootJson = await rootRes.json();
          if (rootJson && rootJson.version) version = String(rootJson.version);
        }
      } catch (_) { /* ignore */ }

      const note = document.createElement('span');
      note.className = 'comment-inline fade-up';
      note.textContent = version
        ? `# 200 OK · v${version} · ${ms}ms RTT`
        : `# 200 OK · ${ms}ms RTT`;
      const noteSlot = $('lastPingNote');
      if (noteSlot) {
        noteSlot.textContent = '';
        noteSlot.appendChild(note);
      }
      if (version) setText('serverVersion', `"${version}"`);
      showStatus(t('options.status.connectionOk'), 'success');

      // Sidebar host hint
      try {
        const u = new URL(nasEndpoint);
        setText('pingHost', u.host);
      } catch (_) {}
    } else {
      throw new Error(t('options.status.unexpectedResponse'));
    }
  } catch (error) {
    console.error('Connection test failed:', error);
    let label = t('options.ping.unreachable') || 'unreachable';
    let toast = t('options.status.connectionFailedPrefix');
    if (String(error.message).includes('Failed to fetch')) {
      toast += t('options.status.cannotReach');
    } else if (String(error.message).includes('401')) {
      toast += t('options.status.invalidApiKey');
      label = '401';
    } else if (String(error.message).includes('404')) {
      toast += t('options.status.apiNotFound');
      label = '404';
    } else {
      toast += error.message;
    }
    setPingState('pingRow',     'err', label);
    setPingState('lastPingRow', 'err', label);
    showStatus(toast, 'error');
  } finally {
    $('testBtn').disabled = false;
    setText('testBtnText', t('options.btn.testInline') || 'test');
  }
}

// ---------- API key show/hide/copy ----------
function toggleApiKeyMask() {
  const el = $('apiKey');
  el.classList.toggle('masked');
  setText('apiKeyToggleText', getToggleVisibleLabel());
}

async function copyApiKey() {
  const v = $('apiKey').value;
  if (!v) return;
  try {
    await navigator.clipboard.writeText(v);
    setText('apiKeyCopyText', t('options.btn.copied') || 'copied');
    $('apiKeyCopyBtn').classList.add('copied');
    setTimeout(() => {
      setText('apiKeyCopyText', t('options.btn.copy') || 'copy');
      $('apiKeyCopyBtn').classList.remove('copied');
    }, 1400);
  } catch (e) {
    showStatus(t('options.status.copyFailed') || 'Copy failed', 'error');
  }
}

// ---------- Init ----------
document.addEventListener('DOMContentLoaded', async () => {
  i18n = (typeof window !== 'undefined' && window.WV2N_I18N) ? window.WV2N_I18N : null;

  // Version in sidebar + about pane
  try {
    const v = chrome.runtime.getManifest().version || '-';
    setText('extVersion', v);
    setText('aboutVersion', `"${v}"`);
  } catch (_) {}

  // Load settings
  const settings = await chrome.storage.sync.get([
    'nasEndpoint', 'apiKey', 'nasOutputSubdir',
    'autoDetect', 'showNotifications',
    'uiLanguage', 'uiTheme',
    'nasProfiles', 'activeProfileId',
    'hiddenMode', 'hiddenModeUrlTemplate',
  ]);

  applyTheme(settings.uiTheme || 'dark');
  applyUiLanguage(settings.uiLanguage);
  await migrateOrLoadProfiles(settings);

  // Keep nasOutputSubdir mirror in sync with the active profile (background.js reads it)
  const ap = activeProfile();
  const apSubdir = (ap && ap.subdir) || '';
  if ((settings.nasOutputSubdir || '') !== apSubdir) {
    try { await chrome.storage.sync.set({ nasOutputSubdir: apSubdir }); } catch (_) {}
  }

  // Populate fields
  $('nasEndpoint').value = settings.nasEndpoint || '';
  $('apiKey').value      = settings.apiKey      || '';
  setToggle('autoDetect',       settings.autoDetect       !== false);
  setToggle('showNotifications', settings.showNotifications !== false);
  // hiddenMode default OFF (opt-in feature; only useful for the specific
  // AV-task workflow). url_template default is the missav pattern the user
  // referenced; saved value wins when present.
  setToggle('hiddenMode', settings.hiddenMode === true);
  $('hiddenModeUrlTemplate').value = settings.hiddenModeUrlTemplate || 'https://missav.ws/dm18/{code}';
  const uiLanguage = settings.uiLanguage === 'zh' ? 'zh-TW' : (settings.uiLanguage || '');
  $('uiLanguage').value = uiLanguage;

  savedSnapshot = {
    nasEndpoint: $('nasEndpoint').value.trim(),
    apiKey:      $('apiKey').value.trim(),
  };

  // Sidebar host hint
  if (savedSnapshot.nasEndpoint) {
    try { setText('pingHost', new URL(savedSnapshot.nasEndpoint).host); } catch (_) {}
  }

  // Wire up nav
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => switchNav(btn.dataset.nav));
  });

  // Wire up theme toggle
  $('themeToggleBtn').addEventListener('click', () => {
    setTheme(theme === 'light' ? 'dark' : 'light');
  });

  // Wire up connection inputs
  $('nasEndpoint').addEventListener('input', refreshDirtyIndicator);
  $('apiKey').addEventListener('input', refreshDirtyIndicator);

  // Wire up inline buttons
  $('testBtn').addEventListener('click', testConnection);
  $('apiKeyToggleBtn').addEventListener('click', toggleApiKeyMask);
  $('apiKeyCopyBtn').addEventListener('click', copyApiKey);

  // Wire up status bar buttons
  $('saveBtn').addEventListener('click', saveSettings);
  $('discardBtn').addEventListener('click', discardChanges);

  // Wire up profiles
  renderProfilesPane();
  const addBtn = $('addProfileBtn');
  if (addBtn) addBtn.addEventListener('click', addCurrentAsProfile);

  // Wire up preference toggles (auto-save on change)
  $('autoDetect').addEventListener('click', async () => {
    setToggle('autoDetect', !getToggle('autoDetect'));
    await savePreferences();
  });
  $('showNotifications').addEventListener('click', async () => {
    setToggle('showNotifications', !getToggle('showNotifications'));
    await savePreferences();
  });
  $('hiddenMode').addEventListener('click', async () => {
    setToggle('hiddenMode', !getToggle('hiddenMode'));
    await savePreferences();
  });
  $('hiddenModeUrlTemplate').addEventListener('input', () => { /* persisted on blur */ });
  $('hiddenModeUrlTemplate').addEventListener('blur', async () => {
    await savePreferences();
  });
  $('uiLanguage').addEventListener('change', async () => {
    await savePreferences();
    applyUiLanguage(($('uiLanguage').value || '').trim());
  });

  // Keyboard shortcuts: Ctrl/Cmd+S → save, Esc on connection pane → discard
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
      e.preventDefault();
      saveSettings();
    }
  });

  refreshDirtyIndicator();
  recomputeGutter();
  window.addEventListener('resize', recomputeGutter);
});
