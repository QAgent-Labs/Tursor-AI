'use strict';

const VERSION = '0.0.1';

/** @typedef {Record<string, string>} AnsiPalette */

function useColor() {
  if (process.env.NO_COLOR !== undefined && process.env.NO_COLOR !== '') {
    return false;
  }
  if (process.env.FORCE_COLOR === '0') {
    return false;
  }
  return Boolean(process.stdout.isTTY);
}

/** @returns {AnsiPalette} */
function c(enabled) {
  if (!enabled) {
    return {
      reset: '',
      bold: '',
      dim: '',
      white: '',
      bright: '',
      green: '',
      red: '',
      yellow: '',
      cyan: '',
    };
  }
  return {
    reset: '\x1b[0m',
    bold: '\x1b[1m',
    dim: '\x1b[2m',
    white: '\x1b[37m',
    bright: '\x1b[97m',
    green: '\x1b[92m',
    red: '\x1b[91m',
    yellow: '\x1b[93m',
    cyan: '\x1b[96m',
  };
}

/** Figlet-style block wordmark — every line same width (no ragged edges). */
const WORDMARK_WIDTH = 51;
const WORDMARK = [
  '████████╗██╗   ██╗██████╗ ███████╗ ██████╗ ██████╗',
  '╚══██╔══╝██║   ██║██╔══██╗██╔════╝██╔═══██╗██╔══██╗',
  '   ██║   ██║   ██║██████╔╝███████╗██║   ██║██████╔╝',
  '   ██║   ██║   ██║██╔══██╗╚════██║██║   ██║██╔══██╗',
  '   ██║   ╚██████╔╝██║  ██║███████║╚██████╔╝██║  ██║',
  '   ╚═╝    ╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝',
].map((line) => line.padEnd(WORDMARK_WIDTH, ' '));

/** RGB stops for left → right gradient (reversed vs backend CLI: green → cyan → violet). */
const GRADIENT_STOPS = [
  [34, 197, 94],
  [56, 189, 248],
  [139, 92, 246],
];

/** Wordmark: mix brand colors toward white (full opacity, pastel tint). */
const WORDMARK_LIGHTEN = 0.7;

/**
 * @param {[number, number, number]} rgb
 * @param {number} amount 0 = unchanged, 1 = white
 * @returns {[number, number, number]}
 */
function lightenTowardWhite(rgb, amount) {
  const t = Math.min(1, Math.max(0, amount));
  return [
    Math.round(rgb[0] + (255 - rgb[0]) * t),
    Math.round(rgb[1] + (255 - rgb[1]) * t),
    Math.round(rgb[2] + (255 - rgb[2]) * t),
  ];
}

/**
 * @param {[number, number, number]} rgb
 * @returns {[number, number, number]}
 */
function wordmarkFgRgb(rgb) {
  return lightenTowardWhite(rgb, WORDMARK_LIGHTEN);
}

/**
 * @param {number} r
 * @param {number} g
 * @param {number} b
 * @returns {number}
 */
function rgbTo256(r, g, b) {
  if (r === g && g === b) {
    if (r < 8) {
      return 16;
    }
    if (r > 248) {
      return 231;
    }
    return Math.round(((r - 8) / 247) * 24) + 232;
  }
  return (
    16 +
    36 * Math.round((r / 255) * 5) +
    6 * Math.round((g / 255) * 5) +
    Math.round((b / 255) * 5)
  );
}

function supportsTrueColor() {
  const ct = process.env.COLORTERM ?? '';
  if (/truecolor|24bit/i.test(ct)) {
    return true;
  }
  const term = process.env.TERM_PROGRAM ?? '';
  return term === 'vscode' || term === 'Apple_Terminal' || term === 'cursor';
}

/**
 * @param {number} t 0..1
 * @returns {string} SGR prefix (no reset)
 */
function fgAt(t, heavy) {
  const clamped = Math.min(1, Math.max(0, t));
  const [r, g, b] = wordmarkFgRgb(lerpGradient(GRADIENT_STOPS, clamped));
  if (supportsTrueColor()) {
    return `\x1b[${heavy ? '1;' : ''}38;2;${r};${g};${b}m`;
  }
  const code = rgbTo256(r, g, b);
  return `\x1b[${heavy ? '1;' : ''}38;5;${code}m`;
}

/**
 * @param {typeof GRADIENT_STOPS} stops
 * @param {number} t 0..1
 * @returns {[number, number, number]}
 */
function lerpGradient(stops, t) {
  const clamped = Math.min(1, Math.max(0, t));
  const seg = (stops.length - 1) * clamped;
  const i = Math.min(stops.length - 2, Math.floor(seg));
  const f = seg - i;
  const a = stops[i];
  const b = stops[i + 1];
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ];
}

/**
 * Colored wordmark line (foreground gradient; no background fill).
 * @param {string} line
 * @param {number} row
 * @param {AnsiPalette} colors
 */
function paintWordmarkLine(line, row, colors) {
  if (!colors.cyan) {
    return line;
  }

  const ink = line.replace(/ /g, '');
  let inkIndex = 0;
  let out = '';

  for (const ch of line) {
    if (ch === ' ') {
      out += ' ';
      continue;
    }
    const t =
      ink.length > 1
        ? inkIndex / (ink.length - 1)
        : row / Math.max(1, WORDMARK.length - 1);
    inkIndex += 1;
    const heavy =
      ch === '█' || ch === '╗' || ch === '╝' || ch === '╔' || ch === '╚';
    out += `${fgAt(t, heavy)}${ch}\x1b[0m`;
  }
  return out;
}

/**
 * Gradient figlet wordmark (no frame) — matches backend CLI layout.
 * @param {{ subtitle?: string }} [opts]
 */
function printBanner(opts = {}) {
  const colors = c(useColor());
  const subtitle = opts.subtitle ?? 'Embeddings service · Tursor AI CLI';

  console.log('');
  WORDMARK.forEach((row, i) => {
    console.log(paintWordmarkLine(row, i, colors));
  });
  console.log(
    `${colors.bright}${colors.bold}${subtitle}${colors.reset}`,
  );
  console.log(`${colors.dim}v${VERSION}${colors.reset}`);
  console.log('');
}

/** @param {string} message */
function printSuccess(message) {
  const colors = c(useColor());
  console.log(
    `${colors.green}✔${colors.reset} ${colors.bright}${message}${colors.reset}`,
  );
}

/** @param {string} message */
function printError(message) {
  const colors = c(useColor());
  console.log(
    `${colors.red}✖${colors.reset} ${colors.bright}${message}${colors.reset}`,
  );
}

/** @param {string} message */
function printWarn(message) {
  const colors = c(useColor());
  console.log(
    `${colors.yellow}!${colors.reset} ${colors.white}${message}${colors.reset}`,
  );
}

/** @param {string} message */
function printInfo(message) {
  const colors = c(useColor());
  console.log(
    `${colors.cyan}→${colors.reset} ${colors.dim}${message}${colors.reset}`,
  );
}

/** Help text only (banner is printed once by the CLI entry). */
function printHelp() {
  const colors = c(useColor());
  /** @param {string} name */
  const cmd = (name) => `${colors.bright}${name}${colors.reset}`;
  console.log(`  ${cmd('tursorAI start')}     Start the Tursor-AI server (Python venv)`);
  console.log(`  ${cmd('tursorAI stop')}      Stop the Tursor-AI process`);
  console.log(
    `  ${cmd('tursorAI status')}    Check health (${colors.dim}--json${colors.reset} for scripts)`,
  );
  console.log(
    `  ${cmd('tursorAI port')}      Print Tursor-AI port (${colors.dim}--json${colors.reset} for origin + port)`,
  );
  console.log(`  ${cmd('tursorAI version')}   Show version`);
  console.log(`  ${cmd('tursorAI help')}      Show this help`);
  console.log('');
  console.log(
    `  ${colors.dim}Status probes http://127.0.0.1:<port>/health (default port 8000, override with --port)${colors.reset}`,
  );
  console.log('');
}

/** @param {string[]} argv */
function shouldShowBanner(argv) {
  return !argv.includes('--json');
}

module.exports = {
  VERSION,
  printBanner,
  printSuccess,
  printError,
  printWarn,
  printInfo,
  printHelp,
  shouldShowBanner,
};
