"""Explicit local Codex App Server operations for verified recovery candidates."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from checkpoint_recovery import bounded_bundle_lines, read_manifest, validate
from native_verifier import find_verifier, runtime
from recovery_state import absolute_path, check_state_root, recovery_key, registry, state_directory
from session_doctor import (Inputs, ROLLOUT_NAME, UPSTREAM, VERSION, Unavailable, hash_only, parse,
                            read_only, session_meta, signature, thread_id, uint)

TURN_START = {"task_started", "turn_started"}
TURN_END = {"task_complete", "turn_complete"}
SMOKE_REPLY = "Recovery continuation OK"
RECOVERY_TITLE_SUFFIX = " (re)"
SMOKE_PROMPT = (
    "This is a continuation smoke test. Reply with exactly Recovery continuation OK. "
    "Do not use tools or repeat any conversation details."
)
SESSION_OVERRIDES = {
    "excludeTurns": True,
    "modelProvider": "openai",
    "sandbox": "read-only",
    "approvalPolicy": "never",
    "config": {"features.goals": False, "features.memories": False},
}


def local_home(flag, variable, default):
    return Path(flag or os.environ.get(variable) or Path.home() / default).expanduser().resolve()


def check_provider(home, cwd):
    """Use the signed-in OpenAI configuration, as in the verified A/B runs."""
    try:
        import tomllib
    except ImportError as exc:
        raise Unavailable("LIVE_COMMANDS_REQUIRE_PYTHON_3_11") from exc

    if os.environ.get("OPENAI_BASE_URL"):
        raise Unavailable("CUSTOM_OPENAI_ENDPOINT_UNSUPPORTED")
    config_paths = {home / "config.toml", *(p / ".codex/config.toml" for p in (cwd, *cwd.parents))}
    for path in config_paths:
        if not path.is_file():
            continue
        with path.open("rb") as file:
            config = tomllib.load(file)
        scope = [config]
        profiles = config.get("profiles")
        selected = config.get("profile")
        if isinstance(profiles, dict) and isinstance(selected, str) and isinstance(profiles.get(selected), dict):
            scope.append(profiles[selected])
        for settings in scope:
            providers = settings.get("model_providers")
            custom_openai = isinstance(providers, dict) and "openai" in providers
            if (settings.get("openai_base_url") or custom_openai
                    or settings.get("model_provider") not in (None, "openai")):
                raise Unavailable("CUSTOM_OPENAI_ENDPOINT_UNSUPPORTED")


class AppServer:
    """One stdio connection. Discard full history, model deltas and diagnostic text."""

    def __init__(self, home, cwd, binary="codex"):
        env = dict(os.environ, CODEX_HOME=str(home))
        self.process = subprocess.Popen(
            [str(binary), "app-server", "--listen", "stdio://"], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.events = queue.Queue(maxsize=64)
        self.stopping = threading.Event()
        self.request_id = 0
        self.reader = threading.Thread(target=self.read_messages, daemon=True)
        self.reader.start()

    def enqueue(self, event):
        while not self.stopping.is_set():
            try:
                self.events.put(event, timeout=0.2)
                return
            except queue.Full:
                continue

    def read_messages(self):
        try:
            while raw := self.process.stdout.readline(16 * 1024 * 1024 + 1):
                if len(raw) > 16 * 1024 * 1024:
                    break
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    if "method" in message:
                        self.enqueue({"id": message["id"], "method": message["method"]})
                        continue
                    result = message.get("result")
                    if isinstance(result, dict):
                        compact = {}
                        if "account" in result:
                            compact["account"] = result["account"] is not None or None
                        if isinstance(result.get("thread"), dict):
                            compact["thread"] = {k: v for k, v in result["thread"].items()
                                                 if k in ("id", "path", "ephemeral", "name", "preview", "status")}
                        if isinstance(result.get("turn"), dict):
                            compact["turn"] = {"id": result["turn"].get("id")}
                        self.enqueue({"id": message["id"], "result": compact})
                    else:
                        error = message.get("error")
                        self.enqueue({"id": message["id"],
                                      "error": {"code": error.get("code") if isinstance(error, dict) else None}})
                    continue
                method, params = message.get("method"), message.get("params")
                if isinstance(params, dict) and method in (
                        "thread/compacted", "context/compacted", "item/completed", "error", "turn/completed"):
                    compact = {key: params.get(key) for key in ("threadId", "thread_id")}
                    if isinstance(params.get("item"), dict):
                        compact["item"] = {"type": params["item"].get("type")}
                    if isinstance(params.get("turn"), dict):
                        compact["turn"] = {key: params["turn"].get(key) for key in ("id", "status")}
                    self.enqueue({"method": method, "params": compact})
        finally:
            self.enqueue(None)

    def write(self, message):
        self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, deadline):
        try:
            event = self.events.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise Unavailable("APP_SERVER_TIMEOUT") from exc
        if event is None:
            raise Unavailable("APP_SERVER_DISCONNECTED")
        if "id" in event and "method" in event:
            self.write({"id": event["id"], "error": {"code": -32601, "message": "Callback declined"}})
        return event

    def request(self, method, params, *, timeout=45, observe=None):
        self.request_id += 1
        expected = self.request_id
        self.write({"id": expected, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.receive(deadline)
            if event.get("id") == expected and "method" not in event:
                result = event.get("result")
                if not isinstance(result, dict):
                    error = event.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    raise Unavailable(f"APP_SERVER_REJECTED:{method}:{code if type(code) is int else 'unknown'}")
                return result
            if observe is not None:
                observe(event)
        raise Unavailable("APP_SERVER_TIMEOUT")

    def initialize(self):
        self.request("initialize", {
            "clientInfo": {"name": "session-doctor", "title": "Session Doctor", "version": VERSION},
            "capabilities": {"experimentalApi": True},
        }, timeout=30)
        self.write({"method": "initialized", "params": {}})
        account = self.request("account/read", {"refreshToken": False}, timeout=30)
        if account.get("account") is None:
            raise Unavailable("NO_ACTIVE_CODEX_ACCOUNT")

    def close(self):
        self.stopping.set()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=5)
        self.process.stdout.close()


@contextmanager
def signed_in_server(home, cwd, platform=None):
    if platform is not None and not platform.unchanged():
        raise Unavailable("CODEX_BINARY_CHANGED_DURING_PREFLIGHT")
    server = AppServer(home, cwd, platform.binary if platform is not None else "codex")
    try:
        server.initialize()
        yield server
    finally:
        server.close()


@dataclass
class Snapshot:
    meta: dict
    checkpoint: dict
    responses: list
    token_info: dict
    lines: int
    windows: tuple = ()
    turn_starts: int = 0


def item_identity(item):
    # The official fork can add local item IDs to previously unassigned records.
    value = {key: field for key, field in item.items()
             if key not in ("id", "id_allocation") and field is not None}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).digest()


def rollout_snapshot(root, path, checkpoint_window=None):
    meta = checkpoint = token = None
    responses = []
    windows = set()
    turn_starts = 0
    count = 0
    with read_only(root, path) as file:
        for count, raw in bounded_bundle_lines(file):
            if not raw.strip():
                continue
            row = parse(raw)
            kind, payload = row.get("type"), row.get("payload")
            if not isinstance(payload, dict):
                raise Unavailable("UNSAFE_LIVE_ROLLOUT_PAYLOAD")
            if kind == "session_meta" and meta is None:
                meta = payload
            elif kind == "compacted":
                if checkpoint_window is not None and payload.get("window_id") != checkpoint_window:
                    continue
                if checkpoint is not None:
                    raise Unavailable("LIVE_ROLLOUT_REQUIRES_ONE_CHECKPOINT")
                checkpoint = payload
            elif kind == "response_item":
                responses.append(item_identity(payload))
            elif kind == "event_msg" and payload.get("type") == "token_count":
                if isinstance(payload.get("info"), dict):
                    token = payload["info"]
            elif kind == "event_msg" and payload.get("type") in TURN_START:
                turn_starts += 1
                window = payload.get("model_context_window")
                if uint(window) and window:
                    windows.add(window)
    if (meta is None or checkpoint is None or token is None
            or not isinstance(checkpoint.get("replacement_history"), list)
            or not all(isinstance(item, dict) for item in checkpoint["replacement_history"])):
        raise Unavailable("LIVE_ROLLOUT_RECOVERY_FIELDS_MISSING")
    return Snapshot(meta, checkpoint, responses, token, count, tuple(sorted(windows)), turn_starts)


def has_continued(expected, actual):
    return len(actual.responses) > len(expected.responses) or actual.turn_starts > expected.turn_starts


def verify_fork(expected, actual, new_id, *, allow_continued=False):
    if (new_id == expected.meta.get("id") or actual.meta.get("id") != new_id
            or actual.meta.get("forked_from_id") != expected.meta.get("id")
            or actual.meta.get("history_mode", "legacy") != "legacy"):
        raise Unavailable("FORK_IDENTITY_OR_LINEAGE_MISMATCH")
    keys = ("window_number", "first_window_id", "previous_window_id", "window_id", "resume_metadata")
    if any(expected.checkpoint.get(k) != actual.checkpoint.get(k) for k in keys):
        raise Unavailable("FORK_CHECKPOINT_METADATA_MISMATCH")
    left = expected.checkpoint["replacement_history"]
    right = actual.checkpoint["replacement_history"]
    if list(map(item_identity, left)) != list(map(item_identity, right)):
        raise Unavailable("FORK_REPLACEMENT_HISTORY_MISMATCH")
    if expected.responses != actual.responses and (
            not allow_continued or expected.responses != actual.responses[:len(expected.responses)]):
        raise Unavailable("FORK_ALREADY_CONTINUED_OR_HISTORY_MISMATCH")
    if not allow_continued and expected.token_info != actual.token_info:
        raise Unavailable("FORK_STARTUP_BASELINE_MISMATCH")
    usage = expected.token_info.get("last_token_usage")
    if not isinstance(usage, dict) or not uint(usage.get("total_tokens")):
        raise Unavailable("FORK_STARTUP_BASELINE_MISMATCH")
    return {"response_items": len(actual.responses), "replacement_items": len(right),
            "startup_baseline_tokens": usage["total_tokens"], "checkpoint_count": 1}


@dataclass
class Preflight:
    bundle: Path
    inputs: Inputs
    expected: Snapshot
    original_files: tuple
    original_signatures: tuple
    digest: str
    candidate_digest: str
    native_validated: bool
    cwd: Path
    native_binary: Path | None
    source_digest: str

    def original_unchanged(self):
        rollout, ledger, source = self.original_files
        return (
            tuple(signature(p.stat(follow_symlinks=False)) for p in self.original_files) == self.original_signatures
            and hash_only(self.inputs.codex, rollout) == self.digest
            and hash_only(self.inputs.codex, ledger) == self.inputs.ledger_snapshot
            and hash_only(self.inputs.claude, source) == self.source_digest
        )


def prepare(args, write_enabled, target=None):
    codex = local_home(args.codex_home, "CODEX_HOME", ".codex")
    claude = local_home(args.claude_home, "CLAUDE_CONFIG_DIR", ".claude")
    bundle = absolute_path(args.candidate)
    if any(bundle.is_relative_to(root) for root in (codex, claude)):
        raise Unavailable("CANDIDATE_MUST_BE_ISOLATED")
    native = find_verifier(args, required=write_enabled)
    manifest = read_manifest(bundle)
    if manifest.get("startup_token_rebase_line") is None:
        raise Unavailable("LIVE_OPERATION_REQUIRES_REBASED_CANDIDATE")
    result = validate(bundle, Path(native) if native else None)
    native_validated = result["status"] == "NATIVE_VALIDATED"
    if native and not (native_validated and result["metrics"]["startup"]["uses_local_rebase"]):
        raise Unavailable("LIVE_OPERATION_REQUIRES_NATIVE_VALIDATOR")

    expected = rollout_snapshot(bundle, bundle / "candidate.jsonl")
    if hash_only(bundle, bundle / "candidate.jsonl") != manifest["candidate_sha256"]:
        raise Unavailable("CANDIDATE_CHANGED_DURING_PREFLIGHT")
    source_id = thread_id(expected.meta.get("id"))
    if source_id is None or expected.meta.get("history_mode", "legacy") != "legacy":
        raise Unavailable("LIVE_OPERATION_REQUIRES_LEGACY_SOURCE")
    if target == source_id:
        raise Unavailable("CANNOT_TEST_ORIGINAL_THREAD")
    if native_validated:
        startup = result["metrics"]["startup"]["startup_context_usage_tokens"]
        if ((expected.windows and startup >= min(expected.windows))
                or (args.token_budget is not None and startup > args.token_budget)):
            raise Unavailable("NATIVE_REPLAY_ESTIMATE_EXCEEDS_RECORDED_WINDOW")
        if (args.item_budget is not None
                and result["metrics"]["after"]["normalized_model_items"] > args.item_budget):
            raise Unavailable("NATIVE_REPLAY_EXCEEDS_ITEM_BUDGET")

    inputs = Inputs(SimpleNamespace(codex_home=codex, claude_home=claude,
                                   max_line_bytes=args.max_line_bytes))
    inputs.ledger()
    inputs.discover({source_id, target} if target is not None else {source_id})
    mapping, rollout = inputs.resolve(source_id)
    files = (rollout, inputs.ledger_path, mapping.path)
    signatures = tuple(signature(path.stat(follow_symlinks=False)) for path in files)
    cwd_value = expected.meta.get("cwd")
    cwd = Path(cwd_value) if isinstance(cwd_value, str) else None
    if cwd is None or not cwd.is_absolute() or not cwd.is_dir():
        raise Unavailable("RECOVERY_WORKING_DIRECTORY_UNAVAILABLE")
    ready = Preflight(bundle, inputs, expected, files, signatures, manifest["original_sha256"],
                      manifest["candidate_sha256"], native_validated, cwd, native, mapping.sha256)
    if not ready.original_unchanged():
        raise Unavailable("ORIGINAL_ROLLOUT_OR_LEDGER_CHANGED")
    if write_enabled:
        check_provider(codex, cwd)
    return ready


def result_failure(status, exc, **known):
    code = str(exc) if isinstance(exc, Unavailable) else "APP_SERVER_OR_ROLLOUT_UNAVAILABLE"
    return {"status": status, "evidence": [code], **known}


def active_rollout(home, thread, new_id):
    reported = thread.get("path")
    if not isinstance(reported, str) or not Path(reported).is_absolute():
        raise Unavailable("APP_SERVER_ROLLOUT_PATH_MISSING")
    path = Path(reported).resolve(strict=True)
    if (thread.get("id") != new_id or thread.get("ephemeral") is not False
            or not path.is_relative_to(home / "sessions") or path.suffix != ".jsonl"):
        raise Unavailable("APP_SERVER_THREAD_OR_PATH_MISMATCH")
    return path


def recovery_title(thread):
    for key in ("name", "preview"):
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip() + RECOVERY_TITLE_SUFFIX
    return "(re)"


def matching_fork(home, expected):
    """Find the persisted effect of an uncertain fork without making an RPC."""
    window = expected.checkpoint.get("window_id")
    if not thread_id(window):
        raise Unavailable("CANDIDATE_WINDOW_ID_INVALID")
    source = expected.meta["id"]
    matches = []
    uncertain = False
    old_recovery = False

    def discovery_error(_):
        raise Unavailable("FORK_DISCOVERY_INCOMPLETE")

    equivalent_keys = ("message", "window_number", "first_window_id", "previous_window_id", "resume_metadata")
    expected_items = None
    for root in (home / "sessions", home / "archived_sessions"):
        if root.is_symlink():
            raise Unavailable("FORK_DISCOVERY_INCOMPLETE")
        if not root.exists():
            continue
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=discovery_error):
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise Unavailable("FORK_DISCOVERY_INCOMPLETE")
            for name in names:
                filename = ROLLOUT_NAME.fullmatch(name)
                if not filename:
                    continue
                if name.endswith(".zst"):
                    uncertain = True
                    continue
                path = Path(directory) / name
                with read_only(home, path) as file:
                    head = file.readline(16 * 1024 * 1024 + 1)
                    if len(head) > 16 * 1024 * 1024:
                        raise Unavailable("FORK_DISCOVERY_INCOMPLETE")
                    meta = session_meta(parse(head))
                    if not thread_id(meta.get("id")) or thread_id(filename[1]) != thread_id(meta["id"]):
                        raise Unavailable("FORK_DISCOVERY_INCOMPLETE")
                    if meta.get("forked_from_id") != source:
                        continue
                    for _, raw in bounded_bundle_lines(file):
                        if raw.strip():
                            item = parse(raw)
                            if item.get("type") != "compacted":
                                continue
                            checkpoint = item.get("payload")
                            if not isinstance(checkpoint, dict):
                                raise Unavailable("FORK_DISCOVERY_INCOMPLETE")
                            if checkpoint.get("window_id") == window:
                                matches.append((meta["id"], path))
                                break
                            # A pre-registry recovery built from these same inputs has
                            # a different random window, but the same replacement.
                            if expected.checkpoint.get("replacement_history") is None or any(
                                    checkpoint.get(key) != expected.checkpoint.get(key) for key in equivalent_keys):
                                continue
                            other = checkpoint.get("replacement_history")
                            if not isinstance(other, list) or not all(isinstance(row, dict) for row in other):
                                continue
                            if expected_items is None:
                                expected_items = list(map(item_identity, expected.checkpoint["replacement_history"]))
                            old_recovery |= expected_items == list(map(item_identity, other))
    if len(matches) > 1:
        raise Unavailable("MULTIPLE_MATCHING_RECOVERY_THREADS")
    if matches:
        target, path = matches[0]
        snapshot = rollout_snapshot(home, path, checkpoint_window=window)
        continued = has_continued(expected, snapshot)
        verify_fork(expected, snapshot, target, allow_continued=continued)
        return target, path, continued
    if old_recovery:
        raise Unavailable("POSSIBLE_EXISTING_RECOVERY_REUSE_ORIGINAL_CANDIDATE")
    if uncertain:
        raise Unavailable("FORK_DISCOVERY_COMPRESSED_ROLLOUTS_UNVERIFIED")
    return None


def journal_location(args, ready):
    return check_state_root(state_directory(args), ready.inputs.codex, ready.inputs.claude)


def bound_candidate(ready, platform):
    window = ready.expected.checkpoint.get("window_id")
    if not thread_id(window):
        raise Unavailable("CANDIDATE_WINDOW_ID_INVALID")
    return {"source_thread_id": ready.expected.meta["id"], "candidate_path": str(ready.bundle),
            "candidate_sha256": ready.candidate_digest, "window_id": window,
            "codex_version": platform.version, "codex_build_sha256": platform.digest,
            "verifier_commit": UPSTREAM,
            "verifier_sha256": hash_only(ready.native_binary.parent, ready.native_binary)}


def assert_binding(entry, binding, ready):
    if entry and entry.get("candidate_sha256") != ready.candidate_digest:
        raise Unavailable("SOURCE_ALREADY_HAS_DIFFERENT_RECOVERY_CANDIDATE")
    for key in ("codex_version", "codex_build_sha256", "verifier_commit", "verifier_sha256"):
        if entry and entry.get("registration") and entry.get(key) != binding[key]:
            raise Unavailable("RECOVERY_VALIDATION_VERSION_CHANGED")


def register_candidate(args):
    ready = prepare(args, args.apply)
    source_id = ready.expected.meta["id"]
    key = recovery_key(ready.inputs.codex, source_id)
    state = journal_location(args, ready)
    if not args.apply:
        with registry(state) as journal:
            entry = journal.records.get(key, {})
        return {"status": "REGISTRATION_PREVIEW", "original_thread_id": source_id,
                "native_validated": ready.native_validated, "registered": False,
                "recorded_registration": entry.get("registration"), "recorded_thread_id": entry.get("thread_id"),
                "recovery_title_suffix": RECOVERY_TITLE_SUFFIX}
    platform = runtime()
    binding = bound_candidate(ready, platform)
    with registry(state, write=True) as journal:
        entry = journal.records.get(key)
        assert_binding(entry, binding, ready)
        found = matching_fork(ready.inputs.codex, ready.expected)
        if found:
            target, _, continued = found
            if entry and entry.get("thread_id") not in (None, target):
                raise Unavailable("RECOVERY_REGISTRY_THREAD_ID_MISMATCH")
            record = {**(entry or {}), **binding, "registration": "registered", "thread_id": target}
            if continued and record.get("test") is None:
                record["test"] = "already_continued"
            journal.put(key, record)
            return {"status": "ALREADY_REGISTERED", "registered": True, "thread_id": target,
                    "original_thread_id": source_id, "test_state": record.get("test")}
        if entry and entry.get("registration"):
            return {"status": "REGISTRATION_PENDING", "thread_id": entry.get("thread_id"),
                    "original_thread_id": source_id, "evidence": ["NO_MATCHING_PERSISTED_FORK_YET"]}
        return perform_registration(ready, platform, journal, key, binding)


def perform_registration(ready, platform, journal, key, binding):
    source_id = ready.expected.meta["id"]
    home = ready.inputs.codex
    new_id = None
    attempted = False
    title_error = None
    try:
        with signed_in_server(home, ready.cwd, platform) as server:
            if (not ready.original_unchanged()
                    or hash_only(ready.bundle, ready.bundle / "candidate.jsonl") != ready.candidate_digest):
                raise Unavailable("REGISTRATION_INPUT_CHANGED")
            original = server.request("thread/read", {"threadId": source_id, "includeTurns": False}).get("thread")
            if not isinstance(original, dict) or original.get("id") != source_id:
                raise Unavailable("ORIGINAL_THREAD_METADATA_MISMATCH")
            name = recovery_title(original)
            if (not ready.original_unchanged()
                    or hash_only(ready.bundle, ready.bundle / "candidate.jsonl") != ready.candidate_digest):
                raise Unavailable("REGISTRATION_INPUT_CHANGED")
            claim = journal.put(key, {**binding, "registration": "pending"})
            attempted = True
            answer = server.request("thread/fork", {
                **SESSION_OVERRIDES,
                # An unsupported path must not silently fall back to the original thread.
                "threadId": "isolated-rollout-path-required",
                "path": str(ready.bundle / "candidate.jsonl"),
                "ephemeral": False,
            }, timeout=110)
            thread = answer.get("thread") or {}
            new_id = thread_id(thread.get("id"))
            if not new_id or new_id == source_id:
                new_id = None
                raise Unavailable("FORK_DID_NOT_RETURN_DISTINCT_ID")
            claim = journal.put(key, {**claim, "thread_id": new_id})
            path = active_rollout(home, thread, new_id)
            proof = verify_fork(ready.expected, rollout_snapshot(home, path), new_id)
            try:
                server.request("thread/name/set", {"threadId": new_id, "name": name})
            except (OSError, Unavailable) as exc:
                title_error = exc
        if not ready.original_unchanged():
            raise Unavailable("ORIGINAL_ROLLOUT_OR_LEDGER_CHANGED")
        journal.put(key, {**claim, "registration": "registered", "title_verified": title_error is None})
        if title_error is not None:
            return result_failure("REGISTERED_TITLE_UNVERIFIED", title_error, thread_id=new_id,
                                  original_thread_id=source_id, registered=True,
                                  original_rollout_and_ledger_unchanged=True, **proof)
        return {"status": "REGISTERED", "thread_id": new_id, "original_thread_id": source_id,
                "registered": True, "recovery_title_applied": True,
                "original_rollout_and_ledger_unchanged": True, **proof}
    except (OSError, Unavailable, ValueError) as exc:
        status = "REGISTRATION_UNVERIFIED" if new_id else (
            "REGISTRATION_RESULT_UNKNOWN" if attempted else "BLOCKED")
        return result_failure(status, exc, thread_id=new_id, original_thread_id=source_id)


def target_rollout(ready, target):
    if target in ready.inputs.by_thread:
        raise Unavailable("CANNOT_TEST_IMPORTED_ORIGINAL_THREAD")
    candidates = ready.inputs.candidates.get(target, ())
    if (ready.inputs.scan_error or len(candidates) != 1 or candidates[0][1] != "verified_header"
            or not candidates[0][0].is_relative_to(ready.inputs.codex / "sessions")):
        raise Unavailable("FORK_ROLLOUT_MISSING_OR_AMBIGUOUS")
    return candidates[0][0]


class TurnObservation:
    def __init__(self, target):
        self.target = target
        self.completed = None
        self.counts = Counter()

    def observe(self, event):
        method, params = event.get("method"), event.get("params")
        if not isinstance(params, dict) or (params.get("threadId") or params.get("thread_id")) != self.target:
            return
        if method in ("thread/compacted", "context/compacted"):
            self.counts["compaction_notifications"] += 1
        elif method == "item/completed":
            item = params.get("item") or {}
            item_type = item.get("type")
            if item_type in ("contextCompaction", "context_compaction"):
                self.counts["compaction_notifications"] += 1
            elif item_type not in ("userMessage", "agentMessage", "reasoning", "plan"):
                self.counts["tool_items"] += 1
        elif method == "error":
            self.counts["error_notifications"] += 1
        elif method == "turn/completed":
            self.completed = params.get("turn") or {}


def persisted_smoke_result(home, path, first_new_line, turn_id):
    active = False
    result = {"completed_without_error": False, "confirmation_reply_saved": False,
              "persisted_compaction_markers": 0, "persisted_errors": 0, "persisted_tool_items": 0,
              "provider_input_tokens": None, "model_context_window": None, "turn_completed": False}
    attempts = 0
    with read_only(home, path) as file:
        for line, raw in bounded_bundle_lines(file):
            if line <= first_new_line or not raw.strip():
                continue
            row = parse(raw)
            kind, payload = row["type"], row["payload"]
            event = payload.get("type") if kind == "event_msg" else None
            if event in TURN_START:
                attempts += 1
                active = payload.get("turn_id") == turn_id
                if active:
                    result["model_context_window"] = payload.get("model_context_window")
            if kind == "compacted" or event == "context_compacted":
                result["persisted_compaction_markers"] += 1
            if event == "error":
                result["persisted_errors"] += 1
            if not active:
                continue
            if kind == "response_item" and payload.get("type") not in ("message", "reasoning"):
                result["persisted_tool_items"] += 1
            if kind == "token_usage_record" and payload.get("turn_id") == turn_id:
                result["provider_input_tokens"] = (payload.get("usage") or {}).get("input_tokens")
            elif event == "agent_message":
                reply = payload.get("message")
                if isinstance(reply, str) and reply.strip().rstrip(".!。！") == SMOKE_REPLY:
                    result["confirmation_reply_saved"] = True
            elif event in TURN_END and payload.get("turn_id") == turn_id:
                result["completed_without_error"] = payload.get("error") is None
                result["turn_completed"] = True
                break
    result["new_turns"] = attempts
    return result


def smoke_prompt(nonce):
    return f"{SMOKE_PROMPT} Local recovery test ID: {nonce}."


def read_test_attempt(home, path, entry):
    """Find the specific submitted user message, not an unrelated later turn."""
    active = None
    marked = set()
    with read_only(home, path) as file:
        for line, raw in bounded_bundle_lines(file):
            if line <= entry["test_start_line"] or not raw.strip():
                continue
            row = parse(raw)
            payload = row.get("payload", {})
            if row.get("type") != "event_msg":
                continue
            event = payload.get("type")
            if event in TURN_START:
                active = payload.get("turn_id")
            elif event in TURN_END:
                active = None
            elif active and event == "user_message" and payload.get("message") == smoke_prompt(entry["test_nonce"]):
                marked.add(active)
    if len(marked) > 1 or (marked and entry.get("test_turn_id") not in (None, next(iter(marked)))):
        raise Unavailable("AMBIGUOUS_PERSISTED_SMOKE_TEST")
    if not marked:
        return None
    turn = marked.pop()
    return turn, persisted_smoke_result(home, path, entry["test_start_line"], turn)


def passed_persisted(saved):
    count, window = saved["provider_input_tokens"], saved["model_context_window"]
    return (
        saved["completed_without_error"] and saved["confirmation_reply_saved"]
        and not saved["persisted_compaction_markers"] and not saved["persisted_errors"]
        and not saved["persisted_tool_items"]
        and saved["new_turns"] == 1 and uint(count) and uint(window) and 0 < count < window
    )


def test_continuation(args):
    target = args.thread_id
    ready = prepare(args, args.send, target)
    home = ready.inputs.codex
    path = target_rollout(ready, target)
    source = ready.expected.meta["id"]
    state = journal_location(args, ready)
    key = recovery_key(home, source)
    if not args.send:
        with registry(state) as journal:
            entry = journal.records.get(key, {})
        if entry.get("thread_id") not in (None, target):
            raise Unavailable("RECOVERY_REGISTRY_THREAD_ID_MISMATCH")
        snapshot = rollout_snapshot(home, path, checkpoint_window=ready.expected.checkpoint.get("window_id"))
        proof = verify_fork(ready.expected, snapshot, target, allow_continued=True)
        if entry.get("test") or has_continued(ready.expected, snapshot):
            return {"status": "CONTINUATION_ALREADY_ATTEMPTED", "thread_id": target, "sent": False,
                    "test_state": entry.get("test") or "already_continued"}
        return {"status": "CONTINUATION_PREVIEW", "thread_id": target, "sent": False,
                "native_validated": ready.native_validated, **proof}
    platform = runtime()
    binding = bound_candidate(ready, platform)
    with registry(state, write=True) as journal:
        entry = journal.records.get(key)
        assert_binding(entry, binding, ready)
        if entry and entry.get("thread_id") not in (None, target):
            raise Unavailable("RECOVERY_REGISTRY_THREAD_ID_MISMATCH")
        found = matching_fork(home, ready.expected)
        if not found or found[:2] != (target, path):
            raise Unavailable("UNVERIFIED_RECOVERY_TARGET")
        entry = {**(entry or {}), **binding, "registration": "registered", "thread_id": target}
        if entry.get("test") == "pending":
            persisted = read_test_attempt(home, path, entry)
            if persisted is None or not persisted[1]["turn_completed"]:
                return {"status": "CONTINUATION_PENDING", "thread_id": target, "sent": False,
                        "evidence": ["NO_COMPLETED_MATCHING_PERSISTED_TURN_YET"]}
            turn, saved = persisted
            intact = ready.original_unchanged()
            passed = intact and passed_persisted(saved)
            journal.put(key, {**entry, "test": "passed" if passed else "failed", "test_turn_id": turn})
            return {"status": "CONTINUATION_PASSED" if passed else "CONTINUATION_UNVERIFIED",
                    "thread_id": target, "turn_id": turn, "sent": False, "reconciled": True,
                    "original_rollout_and_ledger_unchanged": intact, "verification": saved}
        if entry.get("test") or found[2]:
            if not entry.get("test"):
                entry = journal.put(key, {**entry, "test": "already_continued"})
            return {"status": "CONTINUATION_ALREADY_ATTEMPTED", "thread_id": target, "sent": False,
                    "test_state": entry["test"], "turn_id": entry.get("test_turn_id")}
        verify_fresh_fork(ready.expected, rollout_snapshot(home, path), target)
        return perform_continuation(ready, target, path, platform, journal, key, entry)


def verify_fresh_fork(expected, actual, target):
    verify_fork(expected, actual, target)
    if has_continued(expected, actual):
        raise Unavailable("FORK_ALREADY_CONTINUED_OR_HISTORY_MISMATCH")


def perform_continuation(ready, target, path, platform, journal, key, entry):
    home = ready.inputs.codex
    turn_id = None
    attempted = False
    try:
        with signed_in_server(home, ready.cwd, platform) as server:
            read = server.request("thread/read", {"threadId": target, "includeTurns": False})
            thread = read.get("thread") or {}
            if active_rollout(home, thread, target) != path:
                raise Unavailable("TARGET_ROLLOUT_CHANGED")
            if (thread.get("status") or {}).get("type") not in ("idle", "notLoaded"):
                raise Unavailable("TARGET_THREAD_NOT_IDLE")
            resumed = server.request("thread/resume", {**SESSION_OVERRIDES, "threadId": target})
            thread = resumed.get("thread") or {}
            if active_rollout(home, thread, target) != path or (thread.get("status") or {}).get("type") != "idle":
                raise Unavailable("RESUMED_THREAD_NOT_IDLE_OR_CHANGED")
            before = rollout_snapshot(home, path)
            verify_fresh_fork(ready.expected, before, target)
            if not ready.original_unchanged():
                raise Unavailable("ORIGINAL_ROLLOUT_OR_LEDGER_CHANGED")
            observed = TurnObservation(target)
            nonce = str(uuid.uuid4())
            claim = journal.put(key, {**entry, "test": "pending", "test_nonce": nonce,
                                      "test_start_line": before.lines})
            attempted = True
            started = server.request("turn/start", {
                "threadId": target, "environments": [],
                "input": [{"type": "text", "text": smoke_prompt(nonce), "text_elements": []}],
            }, observe=observed.observe)
            turn_id = (started.get("turn") or {}).get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise Unavailable("TEST_TURN_ID_MISSING")
            claim = journal.put(key, {**claim, "test_turn_id": turn_id})
            deadline = time.monotonic() + 180
            while observed.completed is None or observed.completed.get("id") != turn_id:
                if time.monotonic() >= deadline:
                    raise Unavailable("APP_SERVER_TIMEOUT")
                observed.observe(server.receive(deadline))
        saved = persisted_smoke_result(home, path, before.lines, turn_id)
        intact = ready.original_unchanged()
        passed = (intact and observed.completed.get("status") == "completed"
                  and passed_persisted(saved) and not any(observed.counts.values()))
        if saved["turn_completed"]:
            journal.put(key, {**claim, "test": "passed" if passed else "failed"})
        return {"status": "CONTINUATION_PASSED" if passed else "CONTINUATION_UNVERIFIED",
                "thread_id": target, "turn_id": turn_id, "sent": True,
                "original_rollout_and_ledger_unchanged": intact, "verification": saved,
                "compaction_notifications": observed.counts["compaction_notifications"],
                "error_notifications": observed.counts["error_notifications"],
                "tool_items": observed.counts["tool_items"]}
    except (OSError, Unavailable, ValueError) as exc:
        status = "CONTINUATION_RESULT_UNKNOWN" if attempted else "BLOCKED"
        return result_failure(status, exc, thread_id=target, turn_id=turn_id, send_attempted=attempted)
