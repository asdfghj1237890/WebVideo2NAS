import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONTENT_SRC = readFileSync(join(__dirname, '..', 'content.js'), 'utf8');

function loadContent(sendMessage) {
  const chrome = {
    runtime: {
      sendMessage,
    },
  };
  window.chrome = chrome;
  globalThis.chrome = chrome;

  const eval0 = (0, eval);
  eval0(CONTENT_SRC);
}

describe('content.js MAIN-world detection bridge', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  it('forwards deepsearch deep-hit messages to the extension runtime', async () => {
    const sendMessage = vi.fn();
    loadContent(sendMessage);

    window.dispatchEvent(new MessageEvent('message', {
      source: window,
      data: {
        type: 'WV2NAS_DEEP_DETECTED',
        kind: 'manifest-text-no-url',
        format: 'm3u8',
        source: 'atob',
      },
    }));

    await new Promise((resolve) => setTimeout(resolve, 10));

    expect(sendMessage).toHaveBeenCalledWith(expect.objectContaining({
      action: 'deepDetected',
      kind: 'manifest-text-no-url',
      format: 'm3u8',
      source: 'atob',
      pageUrl: window.location.href,
    }));
  });
});
