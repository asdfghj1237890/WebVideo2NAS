// Job identity, shared by the side panel, the service worker and the
// offscreen uploader.
//
// A NAS job id is only unique within that NAS. Two NAS restored from the same
// database backup hand out the same ids, so anything keyed on a bare job id
// can cross-contaminate once a profile switch puts both in play at once:
// one NAS's live progress merged onto the other's row, or a cancel aimed at
// one aborting the other's in-flight upload.
//
// This lives in its own file rather than inside a UI helper because the
// identity has to mean the same thing in all three contexts, and three
// separate copies of a "join these with a separator" rule is precisely how
// such a definition drifts apart.
//
// Namespaced by endpoint, not by endpoint+credential: rotating an API key must
// not orphan the progress of a job already running, and two profiles pointing
// at one NAS with different permissions still address the same job database.
// Request *validity* is a different question and does key on the credential —
// see nasTarget.id in sidepanelCore.

(function installNasIdentity(root) {
  if (!root || root.WV2NNasIdentity) return;

  // Array form rather than a hand-rolled separator: no character has to be
  // assumed absent from a URL or an id.
  function browserJobKey(scope, jobId) {
    const s = typeof scope === 'string' ? scope.trim() : '';
    const j = jobId == null ? '' : String(jobId);
    if (!s || !j) return null;
    return JSON.stringify([s, j]);
  }

  // Reads the job id back out of a composite key, for the places that still
  // have to talk to the NAS API in bare-id terms.
  function jobIdFromKey(key) {
    if (typeof key !== 'string') return null;
    try {
      const parsed = JSON.parse(key);
      if (Array.isArray(parsed) && parsed.length === 2 && typeof parsed[1] === 'string') {
        return parsed[1];
      }
    } catch (_e) { /* fall through */ }
    return null;
  }

  // Entries written before job identity carried a NAS. They name no NAS, so
  // there is no honest way to attribute them — dropping is correct rather than
  // guessing the current scope and risking the exact contamination this file
  // exists to prevent. chrome.storage.session clears on browser restart, so
  // the window is one session at most.
  function isLegacyBrowserJobKey(key) {
    if (typeof key !== 'string') return true;
    try {
      const parsed = JSON.parse(key);
      return !(Array.isArray(parsed) && parsed.length === 2
        && typeof parsed[0] === 'string' && typeof parsed[1] === 'string');
    } catch (_e) {
      return true;
    }
  }

  root.WV2NNasIdentity = Object.freeze({
    browserJobKey,
    jobIdFromKey,
    isLegacyBrowserJobKey,
  });
}((typeof globalThis !== 'undefined' && globalThis) || (typeof self !== 'undefined' && self) || this));
