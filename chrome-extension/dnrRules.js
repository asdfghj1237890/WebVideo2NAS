// ES module wrapper around the shared browser-side DNR core.
//
// background.js loads browserPipelineCore.js as a classic script via
// importScripts(); Vitest imports this file as an ES module. Keeping this thin
// wrapper means both paths exercise the same rule builder.

import './browserPipelineCore.js';

const core = globalThis.WV2NASBrowserPipeline;
if (!core) {
  throw new Error('WV2NASBrowserPipeline core failed to load');
}

export const RULE_ID_BASE = core.RULE_ID_BASE;

export function urlsToRegexFilters(urls) {
  return core.urlsToRegexFilters(urls);
}

export function buildHeaderRules(opts = {}) {
  return core.buildHeaderRules(opts);
}

/**
 * Convenience that wraps chrome.declarativeNetRequest.updateSessionRules.
 * Returns the rule IDs that were installed; caller stores them and passes
 * back to cleanupRules() when the job ends.
 */
export async function installRules(rules) {
  if (!rules || rules.length === 0) return [];
  const ids = rules.map((r) => r.id);
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: ids,
    addRules: rules,
  });
  return ids;
}

export async function cleanupRules(ruleIds) {
  if (!ruleIds || ruleIds.length === 0) return;
  try {
    await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: ruleIds });
  } catch (e) {
    console.warn('[wv2nas] DNR cleanup failed:', e);
  }
}

export const _internals = core._internals;
