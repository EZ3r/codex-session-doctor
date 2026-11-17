"use strict";

const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const repo = path.resolve(__dirname, "..");
const launcher = path.join(repo, "bin", "session-doctor.js");
const pkg = require("../package.json");
const core = path.join(repo, "session_doctor.py");
const harness = "session_doctor_native/reconstruction_test.rs";
const thread = "01900000-0000-7000-8000-000000000123";

function temporary(t) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "session-doctor-npm-")));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function fixtureEnv(directory) {
  const env = {
    ...process.env,
    PATH: `${path.dirname(process.execPath)}:${process.env.PATH}`,
    HOME: directory,
    CODEX_HOME: path.join(directory, "codex"),
    CLAUDE_CONFIG_DIR: path.join(directory, "claude"),
    XDG_STATE_HOME: path.join(directory, "state"),
    npm_config_cache: path.join(directory, "npm-cache"),
    npm_config_userconfig: path.join(directory, "user.npmrc"),
    npm_config_globalconfig: path.join(directory, "global.npmrc"),
    npm_config_update_notifier: "false",
  };
  delete env.SESSION_DOCTOR_PYTHON;
  return env;
}

function run(executable, args, options = {}) {
  const result = spawnSync(executable, args, { encoding: "utf8", timeout: 120000, ...options });
  assert.ifError(result.error);
  return result;
}

function success(result) {
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return result.stdout;
}

function stubPython(directory, script) {
  const location = path.join(directory, "Python 路径 with spaces");
  fs.writeFileSync(location, "#!/usr/bin/env node\n" + script, { mode: 0o700 });
  return location;
}

test("reports a missing Python interpreter", (t) => {
  const root = temporary(t);
  const result = run(process.execPath, [launcher, "--help"], {
    cwd: root, env: { ...fixtureEnv(root), PATH: root },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Python 3\.11 or newer is required.*No usable Python interpreter found/);
  assert.equal(result.stdout, "");
});

test("reports an unsupported version and honors the executable override", (t) => {
  const root = temporary(t);
  const python = stubPython(root, 'console.log("3.10.14");');
  const result = run(process.execPath, [launcher, "--version"], {
    cwd: root, env: { ...fixtureEnv(root), SESSION_DOCTOR_PYTHON: python },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Python 3\.11 or newer is required.*Found Python 3\.10\.14/);
  assert.equal(result.stdout, "");
});

test("forwards literal arguments, cwd, stdin, stdout, stderr and exit code", (t) => {
  const root = temporary(t);
  const python = stubPython(root, `
    const argv = process.argv.slice(2);
    if (argv[3] === "-c") {
      console.log("3.12.3");
    } else {
      const input = require("node:fs").readFileSync(0, "utf8");
      console.log(JSON.stringify({ argv, cwd: process.cwd(), input }));
      console.error("fixture stderr");
      process.exitCode = 23;
    }
  `);
  const args = ["--state-dir", "候选 dir's ; $(touch injected) &", "recover", thread, ""];
  const result = run(process.execPath, [launcher, ...args], {
    cwd: root, env: { ...fixtureEnv(root), SESSION_DOCTOR_PYTHON: python }, input: "fixture stdin\n",
  });
  assert.equal(result.status, 23);
  assert.deepEqual(JSON.parse(result.stdout), {
    argv: ["-E", "-S", "-B", core, ...args], cwd: root, input: "fixture stdin\n",
  });
  assert.equal(result.stderr, "fixture stderr\n");
  assert.equal(fs.existsSync(path.join(root, "injected")), false);
});

test("forwards a signal sent to the launcher PID and propagates the child's signal", { timeout: 10000 }, async (t) => {
  const root = temporary(t);
  const python = stubPython(root, `
    if (process.argv[5] === "-c") {
      console.log("3.12.3");
    } else {
      console.log(process.pid);
      setInterval(() => {}, 1000);
    }
  `);
  const cli = spawn(process.execPath, [launcher, "list"], {
    cwd: root, env: { ...fixtureEnv(root), SESSION_DOCTOR_PYTHON: python }, stdio: ["ignore", "pipe", "ignore"],
  });
  let childPid;
  t.after(() => {
    if (cli.exitCode !== null || cli.signalCode !== null) return;
    if (childPid) {
      try { process.kill(childPid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; }
    }
    cli.kill("SIGKILL");
  });
  const exited = once(cli, "exit");
  const [first] = await Promise.race([
    once(cli.stdout, "data"),
    exited.then(() => { throw new Error("launcher stopped before starting its child"); }),
  ]);
  childPid = Number(first.toString().trim());
  assert.ok(Number.isInteger(childPid) && childPid > 0);
  cli.kill("SIGTERM");
  assert.deepEqual(await exited, [null, "SIGTERM"]);
});

test("packs an allowlisted tarball and installs it offline outside the checkout", { timeout: 120000 }, async (t) => {
  const root = temporary(t);
  const staging = path.join(root, "source");
  const artifacts = path.join(root, "tarballs");
  const directory = path.join(root, "离线 工作区");
  const prefix = path.join(root, "安装 prefix");
  fs.mkdirSync(artifacts);
  fs.mkdirSync(directory);
  const allowed = [...pkg.files, "LICENSE", "README.md", "package.json"].sort();
  for (const file of allowed) {
    const target = path.join(staging, file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(path.join(repo, file), target);
    fs.chmodSync(target, fs.statSync(path.join(repo, file)).mode & 0o777);
  }
  for (const file of ["registry.json", "bin/ignored.log", "candidate-rebased-demo/candidate.jsonl",
    "session_doctor_native/ignored.jsonl", "reports/private.json", "upstream/claude-minimal.jsonl", ".env"]) {
    const target = path.join(staging, file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, "SYNTHETIC EXCLUSION CANARY");
  }
  assert.deepEqual(Object.keys(pkg.scripts), ["test"]);
  assert.deepEqual(Object.keys(pkg.dependencies ?? {}), []);
  assert.deepEqual(Object.keys(pkg.optionalDependencies ?? {}), []);
  assert.deepEqual(Object.keys(pkg.peerDependencies ?? {}), []);
  const env = fixtureEnv(root);
  function pack(...opts) {
    return JSON.parse(success(run("npm", ["pack", "--offline", "--json", "--pack-destination", artifacts, ...opts],
      { cwd: staging, env })))[0];
  }
  const dryRun = pack("--dry-run");
  const packed = pack();
  assert.deepEqual(dryRun.files.map((file) => file.path).sort(), allowed);
  assert.deepEqual(packed.files.map((file) => file.path).sort(), allowed);
  const tarball = path.join(artifacts, packed.filename);
  assert.ok(fs.statSync(tarball).size < 256 * 1024, "tarball must contain source and docs only");
  const members = success(run("tar", ["-tzf", tarball])).trim().split("\n").sort();
  assert.deepEqual(members, allowed.map((file) => `package/${file}`).sort());

  for (const home of [env.CODEX_HOME, env.CLAUDE_CONFIG_DIR]) {
    fs.mkdirSync(home);
    fs.writeFileSync(path.join(home, "unexpected-read-canary"), "synthetic private marker");
  }
  const marker = path.join(root, "python-started");
  const trap = stubPython(root, `require("node:fs").writeFileSync(${JSON.stringify(marker)}, "unexpected");`);
  success(run("npm", ["install", "--global", "--prefix", prefix, "--offline", "--no-audit", "--no-fund", tarball],
    { cwd: directory, env: { ...env, SESSION_DOCTOR_PYTHON: trap } }));
  assert.equal(fs.existsSync(marker), false);
  for (const home of [env.CODEX_HOME, env.CLAUDE_CONFIG_DIR]) {
    assert.deepEqual(fs.readdirSync(home), ["unexpected-read-canary"]);
    assert.equal(fs.readFileSync(path.join(home, "unexpected-read-canary"), "utf8"), "synthetic private marker");
  }
  assert.equal(fs.existsSync(env.XDG_STATE_HOME), false);
  const installed = path.join(prefix, "bin", "session-doctor");
  const module = path.join(prefix, "lib", "node_modules", pkg.name);
  assert.deepEqual(fs.readFileSync(path.join(module, harness)), fs.readFileSync(path.join(repo, harness)));
  const poisoned = path.join(directory, "python startup");
  fs.mkdirSync(poisoned);
  fs.writeFileSync(path.join(poisoned, "sitecustomize.py"), 'raise SystemExit("unexpected sitecustomize import")\n');
  env.PYTHONPATH = poisoned;
  const invoke = (...args) => run(installed, args, { cwd: directory, env });
  assert.equal(success(invoke("--version")).trim(), `session-doctor ${pkg.version}`);
  assert.match(success(invoke("--help")), /recover/);
  assert.equal(invoke("invalid-command").status, 2);

  const makeFixture = [
    "import sys",
    "from pathlib import Path",
    "sys.path.insert(0, sys.argv[1])",
    "from test_checkpoint_recovery import create_recovery_fixture",
    "from test_session_doctor import Fixture",
    "create_recovery_fixture(Fixture(Path(sys.argv[2])))",
  ].join("\n");
  success(run("python3", ["-E", "-S", "-B", "-c", makeFixture, path.join(repo, "tests"), root],
    { cwd: repo, env }));
  const listing = JSON.parse(success(invoke("list")));
  assert.ok(listing.sessions.some((item) => item.thread_id === thread));
  const report = JSON.parse(success(invoke("report", thread)));
  assert.equal(report.diagnosis, "SOURCE_CHECKPOINT_LOST");
  const preview = JSON.parse(success(invoke("recover", thread)));
  assert.equal(preview.status, "RECOVERY_PREVIEW");
  assert.equal(preview.plan.status, "READY_FOR_ISOLATED_CANDIDATE");
  assert.equal(fs.existsSync(env.XDG_STATE_HOME), false);

  await t.test("installed CLI builds and verifies with the native replay validator",
    { skip: !env.SESSION_DOCTOR_NATIVE_TEST_BIN }, () => {
      const built = JSON.parse(success(invoke("recover", thread, "--build")));
      assert.equal(built.status, "CANDIDATE_READY");
      assert.equal(built.metrics.startup.uses_local_rebase, true);
      assert.equal(built.metrics.after.normalized_model_items, 7);
      const checked = JSON.parse(success(invoke("validate-candidate", built.candidate_path)));
      assert.equal(checked.status, "NATIVE_VALIDATED");
    });
});
