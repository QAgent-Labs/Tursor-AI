const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

const TURSOR_AI_HOME = path.join(os.homedir(), '.tursor-ai');
const PID_FILE = path.join(TURSOR_AI_HOME, '.tursor-ai.pid');
const RUNTIME_FILE = path.join(TURSOR_AI_HOME, 'runtime.json');

function resolveAiHome() {
  const fromEnv = process.env.TURSOR_AI_HOME?.trim();
  if (fromEnv) {
    return fromEnv;
  }
  return TURSOR_AI_HOME;
}

function defaultHintPort() {
  const fromEnv = Number(process.env.TURSOR_AI_PORT);
  if (Number.isFinite(fromEnv) && fromEnv > 0) {
    return fromEnv;
  }
  return 8000;
}

/** @param {string[]} argv */
function parsePortFlag(argv) {
  const portIdx = argv.indexOf('--port');
  if (portIdx === -1) {
    return null;
  }
  const parsed = Number.parseInt(argv[portIdx + 1], 10);
  if (Number.isFinite(parsed) && parsed > 0) {
    return parsed;
  }
  return null;
}

function readRuntimeHint() {
  try {
    const raw = fs.readFileSync(RUNTIME_FILE, 'utf8');
    const data = JSON.parse(raw);
    if (typeof data.port === 'number' && data.port > 0) {
      return data.port;
    }
  } catch {
    /* missing or invalid */
  }
  return null;
}

/** @param {{ port: number; pid?: number | null }} info */
function writeRuntime(info) {
  fs.mkdirSync(TURSOR_AI_HOME, { recursive: true });
  const payload = {
    port: info.port,
    pid: info.pid ?? null,
    origin: `http://127.0.0.1:${info.port}`,
    updatedAt: new Date().toISOString(),
  };
  fs.writeFileSync(RUNTIME_FILE, `${JSON.stringify(payload, null, 2)}\n`);
}

function clearRuntime() {
  try {
    fs.unlinkSync(RUNTIME_FILE);
  } catch {
    /* already gone */
  }
}

/**
 * @param {number} hintPort
 * @returns {Promise<{ running: boolean; port: number | null }>}
 */
function probeHealth(hintPort) {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${hintPort}/health`, (res) => {
      let body = '';
      res.on('data', (chunk) => {
        body += chunk;
      });
      res.on('end', () => {
        if (res.statusCode !== 200) {
          resolve({ running: false, port: null });
          return;
        }
        let port = hintPort;
        try {
          const data = JSON.parse(body);
          if (typeof data.port === 'number' && data.port > 0) {
            port = data.port;
          }
        } catch {
          /* use hintPort */
        }
        resolve({ running: true, port });
      });
    });

    req.on('error', () => {
      resolve({ running: false, port: null });
    });

    req.setTimeout(5000, () => {
      req.destroy();
      resolve({ running: false, port: null });
    });
  });
}

/**
 * @param {string[]} argv
 * @returns {Promise<{ running: boolean; port: number | null }>}
 */
async function resolveAiPort(argv) {
  const explicit = parsePortFlag(argv);
  const hints = [explicit, readRuntimeHint(), defaultHintPort()].filter(
    (p, i, arr) => typeof p === 'number' && p > 0 && arr.indexOf(p) === i,
  );

  for (const hint of hints) {
    const result = await probeHealth(hint);
    if (result.running) {
      return result;
    }
  }

  return { running: false, port: null };
}

/**
 * @param {number} hintPort
 * @param {number} maxMs
 * @param {number} intervalMs
 */
async function pollHealthUntilUp(hintPort, maxMs = 45_000, intervalMs = 500) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    const result = await probeHealth(hintPort);
    if (result.running && result.port) {
      return result;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return { running: false, port: null };
}

module.exports = {
  TURSOR_AI_HOME,
  PID_FILE,
  RUNTIME_FILE,
  resolveAiHome,
  defaultHintPort,
  parsePortFlag,
  readRuntimeHint,
  writeRuntime,
  clearRuntime,
  probeHealth,
  resolveAiPort,
  pollHealthUntilUp,
};
