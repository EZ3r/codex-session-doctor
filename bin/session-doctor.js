#!/usr/bin/env node
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const { constants } = require("node:os");
const path = require("node:path");

const CORE = path.join(__dirname, "..", "session_doctor.py");
const PYTHON_FLAGS = ["-E", "-S", "-B"];
const VERSION_PROBE = "import sys; print('%d.%d.%d' % sys.version_info[:3])";
const FORWARDED_SIGNALS = ["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"];

function fail(message) {
  process.stderr.write(`session-doctor: ${message}\n`);
  process.exitCode = 1;
}

function findPython() {
  const override = process.env.SESSION_DOCTOR_PYTHON;
  const candidates = override === undefined
    ? ["python3", "python3.14", "python3.13", "python3.12", "python3.11", "python"]
    : [override];
  const versions = new Set();
  for (const command of candidates) {
    if (!command) continue;
    const probe = spawnSync(command, [...PYTHON_FLAGS, "-c", VERSION_PROBE], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    });
    const match = /^(\d+)\.(\d+)\.\d+$/.exec(probe.stdout?.trim() ?? "");
    if (probe.error || probe.status !== 0 || !match) continue;
    const [, major, minor] = match;
    if (Number(major) > 3 || (Number(major) === 3 && Number(minor) >= 11)) return command;
    versions.add(match[0]);
  }
  const found = versions.size ? ` Found Python ${[...versions].join(", ")}.` : " No usable Python interpreter found.";
  fail(`Python 3.11 or newer is required.${found} Install it and put it on PATH, or set SESSION_DOCTOR_PYTHON to its executable path.`);
  return null;
}

function run() {
  if (process.platform !== "darwin" && process.platform !== "linux") {
    fail("The npm CLI supports macOS and Linux. Windows is not supported.");
    return;
  }
  const python = findPython();
  if (!python) return;

  const child = spawn(python, [...PYTHON_FLAGS, CORE, ...process.argv.slice(2)], {
    stdio: "inherit",
    shell: false,
  });
  const handlers = FORWARDED_SIGNALS.map((signal) => {
    const forward = () => child.kill(signal);
    process.on(signal, forward);
    return [signal, forward];
  });
  function removeHandlers() {
    for (const [signal, forward] of handlers) process.off(signal, forward);
  }
  child.once("error", (error) => {
    removeHandlers();
    fail(`Could not start Python (${error.code ?? "unknown error"}). Check SESSION_DOCTOR_PYTHON and PATH.`);
  });
  child.once("exit", (code, signal) => {
    removeHandlers();
    if (signal) {
      process.exitCode = 128 + constants.signals[signal];
      process.kill(process.pid, signal);
      return;
    }
    process.exitCode = code ?? 1;
  });
}

run();
