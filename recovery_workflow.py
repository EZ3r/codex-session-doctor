"""Single staged entry point for the local recovery workflow."""
from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import checkpoint_recovery as recovery
import live_thread
from native_verifier import SUPPORTED_CODEX, find_verifier
from recovery_state import check_state_root, directory_fd, recovery_key, registry, state_directory
from session_doctor import Inputs, UPSTREAM, Unavailable, hash_only, inspect, signature


def operation_args(args, candidate, **extra):
    return SimpleNamespace(**{**vars(args), "candidate": Path(candidate), **extra})


def next_command(args, *command):
    options = []
    for flag, value in (
        ("--state-dir", args.state_dir),
        ("--codex-home", args.codex_home if args.codex_home != live_thread.local_home(None, "CODEX_HOME", ".codex") else None),
        ("--claude-home", args.claude_home if args.claude_home != live_thread.local_home(None, "CLAUDE_CONFIG_DIR", ".claude") else None),
        ("--token-budget", args.token_budget),
        ("--item-budget", args.item_budget),
    ):
        if value is not None:
            options.extend([flag, str(value)])
    if command[0] == "recover" and args.native_test_bin:
        command = (*command, "--native-test-bin", str(args.native_test_bin))
    checkout_script = Path(__file__).with_name("session-doctor")
    executable = "./session-doctor" if checkout_script.is_file() else "session-doctor"
    return shlex.join([executable, *options, *command])


def next_stage(args, entry, source):
    if entry.get("test") == "pending" or entry.get("registration") == "registered" and not entry.get("test"):
        return next_command(args, "recover", source, "--test")
    if entry.get("test"):
        return None
    if entry.get("candidate_path"):
        return next_command(args, "recover", source, "--register")
    return next_command(args, "recover", source, "--build")


def recover(args):
    source = args.thread_id
    args.codex_home = live_thread.local_home(args.codex_home, "CODEX_HOME", ".codex")
    args.claude_home = live_thread.local_home(args.claude_home, "CLAUDE_CONFIG_DIR", ".claude")
    state = check_state_root(state_directory(args), args.codex_home, args.claude_home)
    key = recovery_key(args.codex_home, source)
    with registry(state) as journal:
        entry = journal.records.get(key, {})
    if not any((args.build, args.register, args.test)):
        inputs = source_inputs(args, source)
        diagnosis = inspect(inputs, source)
        plan = recovery.analyze(inputs, source).plan
        native = find_verifier(args)
        existing = None
        persisted = None
        if entry.get("candidate_path"):
            existing = Path(entry["candidate_path"])
            if recovery.read_manifest(existing)["candidate_sha256"] != entry["candidate_sha256"]:
                raise Unavailable("RECOVERY_REGISTRY_CANDIDATE_MISMATCH")
            recovery.validate(existing, None)
            expected = live_thread.rollout_snapshot(existing, existing / "candidate.jsonl")
            try:
                found = live_thread.matching_fork(args.codex_home, expected)
                if found:
                    persisted = {"thread_id": found[0], "has_added_model_items": found[2]}
                    if entry.get("thread_id") not in (None, found[0]):
                        raise Unavailable("RECOVERY_REGISTRY_THREAD_ID_MISMATCH")
            except Unavailable as exc:
                persisted = {"evidence": str(exc)}
        next_step = next_stage(args, entry, source) if entry or plan["status"] != "BLOCKED" else None
        if next_step and not native:
            next_step = next_command(args, "validator-build")
        return {
            "status": "RECOVERY_PREVIEW", "original_thread_id": source,
            "diagnosis": diagnosis["diagnosis"], "diagnosis_evidence": diagnosis["evidence"],
            "plan": plan, "registry": entry, "persisted_fork": persisted,
            "native_validator_available": native is not None,
            "reviewed_codex_main_commit": UPSTREAM,
            "supported_live_codex_versions": sorted(SUPPORTED_CODEX),
            "live_codex_version_checked": False,
            "next": next_step,
        }
    if args.build:
        return build_stage(args, source, state, key)
    if not entry.get("candidate_path"):
        raise Unavailable("NO_RECORDED_CANDIDATE_RUN_RECOVER_BUILD")
    candidate = Path(entry["candidate_path"])
    if args.register:
        result = live_thread.register_candidate(operation_args(args, candidate, apply=True))
    else:
        if entry.get("registration") != "registered" or not entry.get("thread_id"):
            raise Unavailable("NO_VERIFIED_FORK_RUN_RECOVER_REGISTER")
        result = live_thread.test_continuation(operation_args(args, candidate,
                                                              thread_id=entry["thread_id"], send=True))
    with registry(state) as journal:
        updated = journal.records.get(key, entry)
    return {**result, "next": next_stage(args, updated, source)}


def source_inputs(args, source):
    inputs = Inputs(args)
    inputs.ledger()
    inputs.discover({source})
    return inputs


def build_stage(args, source, state, key):
    binary = find_verifier(args, required=True)
    with registry(state, write=True) as journal:
        entry = journal.records.get(key, {})
        inputs = source_inputs(args, source)
        planned = recovery.analyze(inputs, source)
        if planned.plan["status"] == "BLOCKED":
            return planned.plan
        if entry.get("candidate_path"):
            candidate = Path(entry["candidate_path"])
            manifest = recovery.read_manifest(candidate)
            if manifest["candidate_sha256"] != entry["candidate_sha256"]:
                raise Unavailable("RECOVERY_REGISTRY_CANDIDATE_MISMATCH")
            if manifest["original_sha256"] != planned.original_digest:
                raise Unavailable("ORIGINAL_ROLLOUT_CHANGED_SINCE_CANDIDATE_BUILT")
            result = recovery.validate(candidate, binary)
            check_budget(args, result, planned)
            check_source_snapshot(inputs, planned)
            return {"status": "CANDIDATE_ALREADY_BUILT", "candidate_path": str(candidate),
                    "validation": result, "next": next_stage(args, entry, source)}
        path = state / "candidates"
        with directory_fd(path, create=True):
            draft = recovery.build(inputs, planned, path)
            result = recovery.rebase_candidate(Path(draft["candidate_path"]), path, binary)
        candidate = Path(result["candidate_path"])
        check_budget(args, result, planned)
        check_source_snapshot(inputs, planned)
        journal.put(key, {
            "source_thread_id": source, "candidate_path": str(candidate),
            "candidate_sha256": recovery.read_manifest(candidate)["candidate_sha256"],
            "verifier_commit": UPSTREAM, "verifier_sha256": hash_only(binary.parent, binary),
        })
        return {**result, "status": "CANDIDATE_READY",
                "next": next_command(args, "recover", source, "--register")}


def check_budget(args, result, planned):
    startup = result["metrics"]["startup"]["startup_context_usage_tokens"]
    windows = planned.plan["recorded_context_windows_tokens"]
    if windows and startup >= min(windows):
        raise Unavailable("NATIVE_REPLAY_ESTIMATE_EXCEEDS_RECORDED_WINDOW")
    if args.token_budget is not None and startup > args.token_budget:
        raise Unavailable("NATIVE_REPLAY_ESTIMATE_EXCEEDS_TOKEN_BUDGET")
    if (args.item_budget is not None
            and result["metrics"]["after"]["normalized_model_items"] > args.item_budget):
        raise Unavailable("NATIVE_REPLAY_EXCEEDS_ITEM_BUDGET")


def check_source_snapshot(inputs, planned):
    paths = (planned.source_path, planned.rollout_path, inputs.ledger_path)
    if (planned.before != [signature(path.stat(follow_symlinks=False)) for path in paths]
            or hash_only(inputs.codex, inputs.ledger_path) != inputs.ledger_snapshot):
        raise Unavailable("INPUT_CHANGED_DURING_CANDIDATE_VALIDATION")
