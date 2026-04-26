// Render the WebVideo2NAS icon set (16 / 48 / 128 px) from inline SVG.
// Run with: node icons/generate.mjs    (sharp must be installed)
//
// Design language matches the sidepanel + options page:
//   • dark glass-ish background (oklch ~13% 0.008 250 → #161819)
//   • mint accent stroke (oklch ~78% 0.13 155 → #65d29c)
//   • two-tier rack server silhouette + status LEDs
//
// Each size has a hand-tuned SVG so small renders stay crisp.

import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const here = dirname(fileURLToPath(import.meta.url));

const COLORS = {
  bg:      '#161819',  // dark base
  border:  'rgba(255,255,255,0.08)',
  accent:  '#65d29c',  // mint
  accent2: '#3aa172',  // dimmer mint for secondary stroke
};

function svg128() {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%"   stop-color="#1b1d1f"/>
      <stop offset="100%" stop-color="#101213"/>
    </linearGradient>
    <radialGradient id="glow" cx="50%" cy="42%" r="60%">
      <stop offset="0%"   stop-color="${COLORS.accent}" stop-opacity="0.16"/>
      <stop offset="100%" stop-color="${COLORS.accent}" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <!-- Rounded background -->
  <rect width="128" height="128" rx="28" ry="28" fill="url(#bg)"/>
  <rect width="128" height="128" rx="28" ry="28" fill="url(#glow)"/>
  <rect x="0.5" y="0.5" width="127" height="127" rx="27.5" ry="27.5" fill="none" stroke="${COLORS.border}" stroke-width="1"/>

  <!-- Download arrow (signals "video → NAS") -->
  <g fill="none" stroke="${COLORS.accent}" stroke-width="6" stroke-linecap="round" stroke-linejoin="round">
    <path d="M64 18 L64 50"/>
    <path d="M50 38 L64 52 L78 38"/>
  </g>

  <!-- Two-tier rack server (slightly shorter to make room for the arrow) -->
  <g fill="none" stroke="${COLORS.accent}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round">
    <rect x="22" y="62" width="84" height="22" rx="5" ry="5"/>
    <rect x="22" y="90" width="84" height="22" rx="5" ry="5"/>
  </g>

  <!-- Status LEDs (left side of each tier) -->
  <circle cx="35" cy="73"  r="3" fill="${COLORS.accent}"/>
  <circle cx="35" cy="101" r="3" fill="${COLORS.accent}"/>

  <!-- Drive slot ticks (right side, dimmer mint) -->
  <g stroke="${COLORS.accent2}" stroke-width="2.4" stroke-linecap="round">
    <line x1="56" y1="73"  x2="92" y2="73"/>
    <line x1="56" y1="101" x2="92" y2="101"/>
  </g>
</svg>`;
}

function svg48() {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">
  <rect width="48" height="48" rx="10" ry="10" fill="#161819"/>
  <rect x="0.5" y="0.5" width="47" height="47" rx="9.5" ry="9.5" fill="none" stroke="${COLORS.border}" stroke-width="1"/>

  <!-- Download arrow -->
  <g fill="none" stroke="${COLORS.accent}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
    <path d="M24 7 L24 19"/>
    <path d="M19 14.5 L24 19.5 L29 14.5"/>
  </g>

  <!-- Two-tier rack -->
  <g fill="none" stroke="${COLORS.accent}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <rect x="9" y="23" width="30" height="9" rx="2" ry="2"/>
    <rect x="9" y="34" width="30" height="9" rx="2" ry="2"/>
  </g>

  <circle cx="13.5" cy="27.5" r="1.4" fill="${COLORS.accent}"/>
  <circle cx="13.5" cy="38.5" r="1.4" fill="${COLORS.accent}"/>

  <g stroke="${COLORS.accent2}" stroke-width="1.3" stroke-linecap="round">
    <line x1="20" y1="27.5" x2="34" y2="27.5"/>
    <line x1="20" y1="38.5" x2="34" y2="38.5"/>
  </g>
</svg>`;
}

function svg16() {
  // At 16px every pixel matters. One rack tier + a tiny down arrow above.
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <rect width="16" height="16" rx="3.5" ry="3.5" fill="#161819"/>

  <!-- Tiny download arrow -->
  <g fill="none" stroke="${COLORS.accent}" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M8 2 L8 6.5"/>
    <path d="M6 5 L8 7 L10 5"/>
  </g>

  <!-- One rack tier -->
  <g fill="none" stroke="${COLORS.accent}" stroke-width="1" stroke-linecap="round" stroke-linejoin="round">
    <rect x="2.5" y="9" width="11" height="4.5" rx="1" ry="1"/>
  </g>
  <circle cx="4.4" cy="11.25" r="0.75" fill="${COLORS.accent}"/>
</svg>`;
}

const targets = [
  { name: 'icon128.png', size: 128, svg: svg128() },
  { name: 'icon48.png',  size: 48,  svg: svg48()  },
  { name: 'icon16.png',  size: 16,  svg: svg16()  },
];

for (const { name, size, svg } of targets) {
  const out = join(here, name);
  await sharp(Buffer.from(svg))
    .resize(size, size, { fit: 'contain', kernel: sharp.kernel.lanczos3 })
    .png({ compressionLevel: 9 })
    .toFile(out);
  console.log(`wrote ${name} (${size}×${size})`);
}
