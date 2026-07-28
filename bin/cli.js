#!/usr/bin/env node

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const {
  VERSION,
  printBanner,
  printSuccess,
  printError,
  printWarn,
  printInfo,
  printHelp,
  shouldShowBanner,
} = require('./tursor-ai-ui');
const {
  PID_FILE,
  resolveAiHome,
  defaultHintPort,
  writeRuntime,
  clearRuntime,
  resolveAiPort,
  pollHealthUntilUp,
} = require('./tursor-ai-runtime');

const command = process.argv[2];
const argv = process.argv.slice(2);

if (shouldShowBanner(argv)) {
  printBanner();
}

function venvPython(aiHome) {
  const unix = path.join(aiHome, '.venv', 'bin', 'python');
  if (fs.existsSync(unix)) {
    return unix;
  }
  const win = path.join(aiHome, '.venv', 'Scripts', 'python.exe');
  if (fs.existsSync(win)) {
    return win;
  }
  return null;
}

switch (command) {
  case 'start': {
    void (async () => {
      try {
        const existingPid = fs.readFileSync(PID_FILE, 'utf-8');
        process.kill(existingPid, 0);
        printWarn(`Tursor-AI is already running (PID ${existingPid})`);
        const status = await resolveAiPort(argv);
        if (status.running && status.port) {
          writeRuntime({ port: status.port, pid: Number(existingPid) });
        }
        process.exit(0);
        return;
      } catch {
        /* not running */
      }

      const aiHome = resolveAiHome();
      const python = venvPython(aiHome);
      if (!python) {
        printError(
          `Python venv missing under ${aiHome}. Run the Tursor install script first.`,
        );
        process.exit(1);
        return;
      }

      const runPy = path.join(aiHome, 'run.py');
      if (!fs.existsSync(runPy)) {
        printError(`run.py not found in ${aiHome}`);
        process.exit(1);
        return;
      }

      printInfo('Starting Tursor-AI server…');
      const hintPort = defaultHintPort();
      const child = spawn(python, ['run.py'], {
        cwd: aiHome,
        detached: true,
        stdio: 'ignore',
        env: {
          ...process.env,
          TURSOR_AI_PORT: String(hintPort),
        },
      });

      child.unref();
      fs.mkdirSync(path.dirname(PID_FILE), { recursive: true });
      fs.writeFileSync(PID_FILE, String(child.pid));

      printInfo(`Waiting for health on port ${hintPort}…`);
      const status = await pollHealthUntilUp(hintPort);
      if (status.running && status.port) {
        writeRuntime({ port: status.port, pid: child.pid });
        printSuccess(`Tursor-AI running (PID ${child.pid})`);
        printInfo(`API origin http://127.0.0.1:${status.port}`);
        printInfo('Health at /health · Validate at /v1/validate');
        process.exit(0);
        return;
      }
      printWarn(
        `Process started (PID ${child.pid}) but /health did not respond in time.`,
      );
      printInfo('Run `tursorAI port` after a few seconds to resolve the port.');
      process.exit(0);
    })();

    break;
  }

  case 'stop': {
    try {
      const pid = fs.readFileSync(PID_FILE, 'utf-8');
      process.kill(pid);
      fs.unlinkSync(PID_FILE);
      clearRuntime();
      printSuccess('Tursor-AI stopped');
      process.exit(0);
    } catch {
      clearRuntime();
      printWarn('Tursor-AI is not running (no PID file or process gone)');
      process.exit(1);
    }
    break;
  }

  case 'status': {
    const jsonMode = argv.includes('--json');

    void (async () => {
      const status = await resolveAiPort(argv);

      if (jsonMode) {
        console.log(
          JSON.stringify({
            running: status.running,
            port: status.running ? status.port : null,
          }),
        );
        process.exit(status.running ? 0 : 1);
        return;
      }

      if (status.running && status.port) {
        printSuccess('Tursor-AI is healthy');
        printInfo(`Listening on http://127.0.0.1:${status.port}`);
        process.exit(0);
        return;
      }

      printError('Tursor-AI is not running');
      printInfo(`Tried http://127.0.0.1:${defaultHintPort()}/health`);
      process.exit(1);
    })();

    break;
  }

  case 'port': {
    const jsonMode = argv.includes('--json');

    void (async () => {
      const status = await resolveAiPort(argv);

      if (jsonMode) {
        const port = status.running ? status.port : null;
        console.log(
          JSON.stringify({
            running: status.running,
            port,
            origin:
              status.running && port ? `http://127.0.0.1:${port}` : null,
          }),
        );
        process.exit(status.running ? 0 : 1);
        return;
      }

      if (status.running && status.port) {
        console.log(String(status.port));
        process.exit(0);
        return;
      }

      process.exit(1);
    })();

    break;
  }

  case 'version':
    printSuccess(`Tursor AI CLI v${VERSION}`);
    process.exit(0);
    break;

  case 'help':
    printHelp();
    process.exit(0);
    break;

  default:
    if (command) {
      printError(`Unknown command: ${command}`);
      console.log('');
    }
    printHelp();
    process.exit(command ? 1 : 0);
}
