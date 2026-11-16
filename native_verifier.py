"""Install a native replay verifier from the pinned upstream Codex checkout."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from recovery_state import directory_fd, state_directory, verify_private_files
from session_doctor import HEX_SHA, UPSTREAM, Unavailable, hash_only, parse, read_only, signature

SUPPORTED_CODEX = frozenset({"0.169.0-alpha.11.1"})
HARNESS = Path(__file__).resolve().parent / "session_doctor_native" / "reconstruction_test.rs"
SOURCE_URL = "https://github.com/openai/codex.git"


def cache_path(args):
    return state_directory(args) / "verifiers" / UPSTREAM


def harness_digest():
    return hashlib.sha256(HARNESS.read_bytes()).hexdigest()


def cached_verifier(cache):
    try:
        verify_private_files(cache, "manifest.json", "verify")
    except FileNotFoundError:
        return None
    with read_only(cache, cache / "manifest.json") as file:
        meta = parse(file.read(4096))
    if (meta.get("schema") != 1 or meta.get("codex_main_commit") != UPSTREAM
            or meta.get("harness_sha256") != harness_digest()
            or not isinstance(meta.get("binary_sha256"), str)
            or not HEX_SHA.fullmatch(meta["binary_sha256"])):
        raise Unavailable("NATIVE_VALIDATOR_REBUILD_REQUIRED")
    binary = cache / "verify"
    if not os.access(binary, os.X_OK) or hash_only(cache, binary) != meta["binary_sha256"]:
        raise Unavailable("NATIVE_VALIDATOR_CACHE_DIGEST_MISMATCH")
    return binary


def find_verifier(args, *, required=False):
    location = getattr(args, "native_test_bin", None) or os.environ.get("SESSION_DOCTOR_NATIVE_TEST_BIN")
    if location:
        binary = Path(location).expanduser().resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise Unavailable("NATIVE_VALIDATOR_NOT_EXECUTABLE")
        return binary
    binary = cached_verifier(cache_path(args))
    if binary is None and required:
        raise Unavailable("NATIVE_VALIDATOR_NOT_INSTALLED_RUN_VALIDATOR_BUILD")
    return binary


@dataclass(frozen=True)
class CodexRuntime:
    binary: Path
    version: str
    digest: str
    stamp: tuple

    def unchanged(self):
        return signature(self.binary.stat(follow_symlinks=False)) == self.stamp


def runtime():
    file = shutil.which("codex")
    if not file:
        raise Unavailable("CODEX_CLI_NOT_INSTALLED")
    binary = Path(file).resolve(strict=True)
    before = signature(binary.stat(follow_symlinks=False))
    try:
        result = subprocess.run([str(binary), "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired as exc:
        raise Unavailable("CODEX_VERSION_PROBE_TIMEOUT") from exc
    match = re.fullmatch(rb"codex-cli ([0-9A-Za-z.+-]+)\s*", result.stdout)
    if result.returncode or not match:
        raise Unavailable("CODEX_VERSION_UNRECOGNIZED")
    version = match[1].decode("ascii")
    if version not in SUPPORTED_CODEX:
        raise Unavailable("CODEX_VERSION_UNSUPPORTED_REQUIRES_RECOVERY_SCHEMA_REVIEW")
    digest = hash_only(binary.parent, binary)
    if before != signature(binary.stat(follow_symlinks=False)):
        raise Unavailable("CODEX_BINARY_CHANGED_DURING_PREFLIGHT")
    return CodexRuntime(binary, version, digest, before)


def git_checkout(source, destination, revision=UPSTREAM):
    """Fetch Git objects into neutral staging, without caller repository settings."""
    destination.mkdir()
    git = ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
           "-c", "transfer.fsckObjects=true"]
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    for args in (["init", "-q", "--template="],
                 ["fetch", "-q", "--no-tags", "--depth=1", "--", str(source), revision],
                 ["checkout", "-q", "--detach", revision]):
        proc = subprocess.run(git + args, cwd=destination, env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False)
        if proc.returncode:
            raise Unavailable("PINNED_CODEX_SOURCE_PREPARATION_FAILED")


def prepare_checkout(destination, local_source):
    source = SOURCE_URL if local_source is None else local_source.resolve(strict=True)
    git_checkout(source, destination)
    test_source = destination / "codex-rs/core/src/session/tests.rs"
    with test_source.open("ab") as target:
        target.write(HARNESS.read_bytes())


def compile_verifier(source, target, offline):
    command = ["cargo", "test", "--locked", "-p", "codex-core", "--lib", "--no-run",
               "--message-format=json"]
    if offline:
        command.append("--offline")
    env = dict(os.environ, CARGO_TARGET_DIR=str(target),
               CARGO_PROFILE_TEST_DEBUG="0", CARGO_PROFILE_DEV_DEBUG="0")
    with subprocess.Popen(command, cwd=source / "codex-rs", env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        executables = set()
        for raw in process.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if (msg.get("reason") == "compiler-artifact" and msg.get("profile", {}).get("test")
                    and msg.get("target", {}).get("name") == "codex_core" and msg.get("executable")):
                executables.add(Path(msg["executable"]).resolve())
        if process.wait() or len(executables) != 1:
            raise Unavailable("NATIVE_VALIDATOR_BUILD_FAILED")
    return executables.pop()


def build_verifier(args):
    cache = cache_path(args)
    parent = cache.parent
    with directory_fd(parent, create=True):
        if cache.exists():
            try:
                existing = cached_verifier(cache)
            except Unavailable:
                raise Unavailable("VALIDATOR_CACHE_REBUILD_REQUIRED") from None
            if existing == cache / "verify":
                return {"status": "VALIDATOR_READY", "codex_main_commit": UPSTREAM, "reused": True}
            raise Unavailable("VALIDATOR_CACHE_REBUILD_REQUIRED")
        if args.offline and args.codex_source is None:
            raise Unavailable("OFFLINE_VALIDATOR_BUILD_REQUIRES_CODEX_SOURCE")
        if shutil.which("cargo") is None:
            raise Unavailable("CARGO_NOT_INSTALLED_INSTALL_RUSTUP")
        with tempfile.TemporaryDirectory(prefix=".verifier-build-", dir=parent) as stage:
            stage = Path(stage)
            prepare_checkout(stage / "source", args.codex_source)
            executable = compile_verifier(stage / "source", stage / "target", args.offline)
            published = stage / "published"
            published.mkdir(mode=0o700)
            binary = published / "verify"
            shutil.copyfile(executable, binary)
            binary.chmod(0o700)
            manifest = {"schema": 1, "codex_main_commit": UPSTREAM,
                        "harness_sha256": harness_digest(), "binary_sha256": hash_only(published, binary)}
            result = published / "manifest.json"
            fd = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, sort_keys=True)
            published.rename(cache)
    return {"status": "VALIDATOR_READY", "codex_main_commit": UPSTREAM, "reused": False}
