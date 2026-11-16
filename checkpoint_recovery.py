"""Build and verify isolated Legacy recovery candidates using the import transcript."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from session_doctor import (
    Diagnostics, MARKER, ROLLOUT_TYPES, UPSTREAM, Rollout, Scale, Source, Unavailable,
    hash_only, item_scale, parse, read_only, scan_jsonl, signature, thread_id, uint, unwrap,
)

SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking process. "
    "You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. "
    "Here is the summary produced by the other language model, use the information in this summary "
    "to assist with your own analysis:"
)
START = ("task_started", "turn_started")
COMPLETE = ("task_complete", "turn_complete")
IMPORT_EVENTS = (*START, *COMPLETE, "user_message", "agent_message", "token_count")
BUNDLE_FILES = ("candidate.jsonl", "expected.jsonl", "expectations.json")
NATIVE_BYTES_LIMIT = 128 * 1024 * 1024
NATIVE_RECORD_LIMIT = 250_000
MAX_REPLAY_BYTES = 16 * 1024 * 1024
NATIVE_TEST = "session::tests::session_doctor_native_reconstruction"


def text_field(obj, key, fallback=None):
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, str) else fallback


def tool_call(block):
    name = text_field(block, "name", "unknown")
    lines = [f"[external_agent_tool_call: {name}]"]
    args = block.get("input")
    if isinstance(args, dict):
        description = text_field(args, "description")
        command = text_field(args, "command")
        file = text_field(args, "file_path") if "file_path" in args else text_field(args, "file")
        lines.extend(f"{key}: {value}" for key, value in
                     (("description", description), ("command", command), ("file", file))
                     if value is not None)
    if len(lines) == 1 and "input" in block:
        rendered = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        lines.append("input: " + truncate(rendered, 2000))
    return "\n".join([*lines, "[/external_agent_tool_call]"])


def truncate(text, limit):
    return text if len(text) <= limit else text[:limit - 3] + "..."


def tool_result(block):
    content = block.get("content")
    if isinstance(content, list):
        content = "\n".join(text for part in content if (text := text_field(part, "text")))
    suffix = ": error" if block.get("is_error") is True else ""
    parts = [f"[external_agent_tool_result{suffix}]"]
    if isinstance(content, str) and content:
        parts.append(truncate(content, 4000))
    return "\n".join([*parts, "[/external_agent_tool_result]"])


def plain_projection(record):
    """Port the pinned import transformation. Recovery verifies it against the actual rollout."""
    kind = record.get("type")
    msg = record.get("message")
    if (kind not in ("user", "assistant") or not isinstance(msg, dict)
            or record.get("isMeta") is True or record.get("isSidechain") is True):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        output_only = True
        for block in (value for value in content if isinstance(value, dict)):
            block_kind = text_field(block, "type")
            if block_kind == "text":
                if value := text_field(block, "text"):
                    parts.append(value)
                    output_only = False
            elif block_kind == "tool_use":
                parts.append(tool_call(block))
                output_only = False
            elif block_kind == "tool_result":
                parts.append(tool_result(block))
            elif block_kind not in (None, "thinking"):
                parts.append(f"[external unsupported block: {block_kind}]")
                output_only = False
        text = "\n\n".join(part for part in parts if part.strip())
        if output_only:
            kind = "assistant"
    else:
        return None
    if not text.strip():
        return None
    return kind, unwrap(text) if kind == "user" else text


def digest_text(text):
    return hashlib.sha256(text.encode()).digest()


def response_identity(item):
    role = item.get("role")
    parts = item.get("content")
    kind = "input_text" if role == "user" else "output_text"
    if (item.get("type") != "message" or role not in ("user", "assistant")
            or not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
            or parts[0].get("type") != kind or not isinstance(parts[0].get("text"), str)):
        return None
    return role, digest_text(parts[0]["text"])


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def projected_size(items, *, replacement, metadata=None):
    metadata = {} if metadata is None else metadata
    return sum((item_scale(item, replacement=replacement, metadata=metadata.get(index))
                for index, item in enumerate(items)), Scale())


class ImportTranscript:
    def __init__(self, thread, source_items, selected):
        self.rollout = Rollout(thread, None)
        self.source_items = iter(source_items)
        self.selected = set(selected)
        self.response_by_line = {}
        self.import_count = 0
        self.mismatches = 0
        self.payload_bytes = 0
        self.post_import = []
        self.final_turn_id = None
        self.import_complete = None
        self.timestamp = None
        self.import_issues = set()
        self.post_types = Diagnostics()
        self.last_recorded_token_count = None
        self.last_recorded_count_is_import_estimate = None

    def consume(self, row, line, diag):
        self.rollout.consume(row, line, diag)
        kind = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return
        event = payload.get("type") if kind == "event_msg" else None
        if event == "token_count" and isinstance(payload.get("info"), dict):
            usage = payload["info"].get("last_token_usage")
            if isinstance(usage, dict) and uint(usage.get("total_tokens")):
                self.last_recorded_token_count = usage["total_tokens"]
                self.last_recorded_count_is_import_estimate = self.import_complete is None
        if row.get("ordinal") is not None:
            self.import_issues.add("ORDINAL_HISTORY_UNSUPPORTED")
        if self.import_complete is not None:
            if kind == "response_item":
                self.track_bytes(row)
                self.post_import.append(row)
            category = kind if isinstance(kind, str) and kind in ROLLOUT_TYPES else "unrecognized"
            self.post_types.add(category)
            if kind == "inter_agent_communication":
                self.import_issues.add("POST_IMPORT_INTER_AGENT_PROJECTION_UNSUPPORTED")
            return
        if kind == "response_item":
            self.import_count += 1
            source_line, projection = next(self.source_items, (None, None))
            if source_line is None or projection != response_identity(payload):
                self.mismatches += 1
            if source_line in self.selected:
                self.track_bytes(payload)
                self.response_by_line[source_line] = payload
            if row.get("metadata") is not None or any(payload.get(key) is not None for key in
                    ("id", "id_allocation", "encrypted_content", "phase", "status",
                     "internal_chat_message_metadata_passthrough")):
                self.import_issues.add("PRE_IMPORT_RESPONSE_HAS_RUNTIME_STATE")
        elif kind == "session_meta":
            if line != 1:
                self.import_issues.add("PRE_IMPORT_METADATA_UPDATE")
        elif kind != "event_msg" or event not in IMPORT_EVENTS:
            self.import_issues.add("PRE_IMPORT_RUNTIME_STATE_UNSUPPORTED")
        elif event in START:
            turn_id = payload.get("turn_id")
            if (self.rollout.marker_line or not isinstance(turn_id, str)
                    or not turn_id.startswith("external-import-turn-")
                    or payload.get("turn_attribution") is not None
                    or payload.get("root_turn_id") is not None):
                self.import_issues.add("IMPORT_TURN_START_UNVERIFIED")
            if self.rollout.marker_line is None:
                self.final_turn_id = turn_id
        elif event in COMPLETE:
            if payload.get("error") is not None:
                self.import_issues.add("IMPORT_COMPLETION_HAS_RUNTIME_ERROR")
            if self.rollout.marker_line:
                if not self.final_turn_id or payload.get("turn_id") != self.final_turn_id:
                    self.import_issues.add("IMPORT_COMPLETION_TURN_MISMATCH")
                else:
                    self.import_complete = line
                    self.timestamp = row.get("timestamp")
        if self.rollout.marker_line and self.import_complete is None and (
            kind != "event_msg" or not (event == "token_count" or
            (event == "agent_message" and payload.get("message") == MARKER))
        ):
            self.import_issues.add("IMPORT_MARKER_BOUNDARY_UNVERIFIED")

    def track_bytes(self, payload):
        self.payload_bytes += len(encode(payload))
        if self.payload_bytes > MAX_REPLAY_BYTES:
            raise Unavailable("RECOVERY_RETAINED_PAYLOAD_LIMIT")


@dataclass
class Recovery:
    plan: dict
    rollout_path: Path
    source_path: Path
    before: list
    original_digest: str
    original_records: int
    insert_after: int | None = None
    timestamp: str | None = None
    replacement: list | None = None
    post: list | None = None
    thread_metadata: dict | None = None
    final_turn_id: str | None = None


def analyze(inputs, thread):
    mapping, rollout_path = inputs.resolve(thread)
    paths = (mapping.path, rollout_path, inputs.ledger_path)
    before = [signature(path.stat(follow_symlinks=False)) for path in paths]
    source_diag, codex_diag = Diagnostics(), Diagnostics()
    seen_user = False

    def projection(record):
        nonlocal seen_user
        projected = plain_projection(record)
        if projected is None or (projected[0] != "user" and not seen_user):
            return None
        seen_user = True
        role, text = projected
        return role, digest_text(text)

    source = Source(projection)
    _, digest = scan_jsonl(inputs.claude, mapping.path, source.consume, inputs.args, source_diag)
    source_report = source.output(source_diag)
    selected = []
    if source_report["latest_markers_form_valid_pair"] and source.logical is not None:
        preserved, tail = source.logical
        selected = [source.checkpoints[-1]["summary_line"], *preserved, *tail]
    imported = ImportTranscript(thread, source.imported.items(), selected)
    file_report, rollout_digest = scan_jsonl(
        inputs.codex, rollout_path, imported.consume, inputs.args, codex_diag)
    codex, model, _, _ = imported.rollout.output(codex_diag)
    if (before != [signature(path.stat(follow_symlinks=False)) for path in paths]
            or hash_only(inputs.codex, inputs.ledger_path) != inputs.ledger_snapshot):
        raise Unavailable("INPUT_CHANGED_DURING_RECOVERY_ANALYSIS")

    plan = {
        "schema_version": 1, "codex_main_commit": UPSTREAM,
        "source_hash_matches": digest == mapping.sha256,
        "paired_claude_checkpoints": source_report["paired_checkpoint_count"],
        "history_mode": codex["history_mode"],
        "existing_codex_checkpoints": codex["compacted_count"],
        "native_validation": "not_run",
        "recorded_context_windows_tokens": codex["recorded_context_windows_tokens"],
        "last_recorded_token_count": imported.last_recorded_token_count,
        "last_recorded_count_is_import_estimate": imported.last_recorded_count_is_import_estimate,
        "before": model.output() if model is not None else None,
        "after": None, "checkpoint": None,
        "post_import_rollout_record_counts": dict(imported.post_types.counts),
        "token_method": "ceil(estimated model-visible UTF-8 bytes / 4); excludes runtime additions",
        "original_rollout_records": file_report["lines"],
        "original_rollout_kept_verbatim_in_candidate": True,
        "isolated_candidate_uses_original_identity_for_replay_only": True,
        "caller_budget": {"tokens": inputs.args.token_budget, "items": inputs.args.item_budget},
    }
    windows = codex["recorded_context_windows_tokens"]
    old_usage = imported.last_recorded_token_count
    plan["activation_checks"] = ["VERIFY_ISOLATED_REPLAY_AND_ALLOCATE_NEW_THREAD_ID"]
    if imported.last_recorded_count_is_import_estimate and old_usage is not None:
        plan["activation_checks"].append("REBASE_PRE_CHECKPOINT_IMPORTED_TOKEN_ESTIMATE")
        plan["last_recorded_count_exceeds_window"] = old_usage >= min(windows) if windows else None
    result = Recovery(plan, rollout_path, mapping.path, before, rollout_digest, file_report["lines"])
    blockers = set(imported.import_issues)
    if digest != mapping.sha256:
        blockers.add("SOURCE_HASH_MISMATCH")
    if source_diag.blockers:
        blockers.add("SOURCE_PARSE_OR_IDENTITY_UNSAFE")
    if codex_diag.blockers or model is None:
        blockers.add("CODEX_REPLAY_UNSAFE")
    if inputs.unreadable_headers or inputs.skipped_other_compressed:
        blockers.add("ROLLOUT_DISCOVERY_UNVERIFIED")
    if not source_report["latest_markers_form_valid_pair"]:
        blockers.add("LATEST_CLAUDE_CHECKPOINT_UNVERIFIED")
    elif source.logical is None:
        blockers.add(source.logical_error)
    if codex["compacted_count"]:
        blockers.add("CODEX_ALREADY_HAS_CHECKPOINT")
    if codex["history_mode"] != "legacy":
        blockers.add("RECOVERY_SUPPORTS_LEGACY_ONLY")
    if imported.rollout.marker_count != 1 or imported.import_complete is None:
        blockers.add("UNIQUE_COMPLETE_IMPORT_BOUNDARY_REQUIRED")
    meta = imported.rollout.meta or {}
    if (thread_id(meta.get("session_id", meta.get("id"))) != thread
            or any(meta.get(key) for key in ("history_base", "parent_thread_id", "forked_from_id",
                                            "attached_thread", "subagent_history_start_ordinal"))):
        blockers.add("RECOVERY_REQUIRES_ROOT_THREAD")
    if meta.get("multi_agent_version") not in ("v1", "v2", "disabled"):
        blockers.add("IMPORT_RUNTIME_VERSION_NOT_PERSISTED")
    if not valid_window(meta):
        blockers.add("IMPORT_INITIAL_WINDOW_ID_UNVERIFIED")
    if meta.get("dynamic_tools"):
        blockers.add("PRE_IMPORT_DYNAMIC_TOOLS_UNSUPPORTED")

    response_by_line = imported.response_by_line
    source_count, codex_count = len(source.imported), imported.import_count
    mismatch_count = imported.mismatches + max(0, source_count - codex_count)
    plan["import_transcript"] = {"source_items": source_count, "codex_items": codex_count,
                                 "mismatched_items": mismatch_count}
    if mismatch_count:
        blockers.add("IMPORT_TRANSCRIPT_NOT_IDENTICAL")

    if source_report["latest_markers_form_valid_pair"]:
        latest = source.checkpoints[-1]
        plan["checkpoint"] = {"boundary_line": latest["boundary_line"],
                              "summary_line": latest["summary_line"],
                              "attachments_before_summary": latest["attachment_chain_records"]}
        preserved, tail = source.logical or ([], [])
        selected = [latest["summary_line"], *preserved, *tail]
        if source.logical is not None and not mismatch_count and all(line in response_by_line for line in selected):
            first = response_by_line[selected[0]]
            summary_text = first["content"][0]["text"]
            restored = [{"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": SUMMARY_PREFIX + "\n" + summary_text}]}]
            restored.extend(response_by_line[line] for line in selected[1:])
            tool_meta = {i: row["metadata"] for i, row in enumerate(imported.post_import)
                         if row.get("metadata") is not None}
            post_size = projected_size([row["payload"] for row in imported.post_import],
                                       replacement=False, metadata=tool_meta)
            restored_size = projected_size(restored, replacement=True)
            after = restored_size + post_size
            plan.update({
                "after": after.output(),
                "replacement": restored_size.output(),
                "preserved": projected_size(restored[1:1 + len(preserved)], replacement=True).output(),
                "claude_logical_tail": projected_size(restored[1 + len(preserved):], replacement=True).output(),
                "codex_post_import": post_size.output(),
                "fits_recorded_context_estimate": context_estimate(after, codex),
                "checkpoint_insert_after_rollout_line": imported.import_complete,
            })
            result.replacement = restored
        else:
            blockers.add("EXACT_CHECKPOINT_PROJECTION_UNVERIFIED")
    result.insert_after, result.timestamp = imported.import_complete, imported.timestamp
    result.thread_metadata, result.final_turn_id = meta, imported.final_turn_id
    result.post = imported.post_import
    if plan.get("fits_recorded_context_estimate") is False:
        blockers.add("ESTIMATED_HISTORY_EXCEEDS_RECORDED_WINDOW")
    if plan["after"]:
        if inputs.args.token_budget is not None and plan["after"]["estimated_tokens"] > inputs.args.token_budget:
            blockers.add("ESTIMATED_HISTORY_EXCEEDS_CALLER_TOKEN_BUDGET")
        if inputs.args.item_budget is not None and plan["after"]["items"] > inputs.args.item_budget:
            blockers.add("ESTIMATED_HISTORY_EXCEEDS_CALLER_ITEM_BUDGET")
    plan["blockers"] = sorted(blockers)
    plan["status"] = "BLOCKED" if blockers else "READY_FOR_ISOLATED_CANDIDATE"
    return result


def context_estimate(amount, codex):
    windows = codex["recorded_context_windows_tokens"]
    return (amount.bytes + 3) // 4 < min(windows) if windows else None


def valid_window(meta):
    window = meta.get("context_window")
    window_id = window.get("window_id") if isinstance(window, dict) else None
    try:
        return uuid.UUID(window_id).version == 7
    except (ValueError, AttributeError, TypeError):
        return False


def new_window_id():
    """Allocate a fresh UUIDv7 for this isolated checkpoint's new context window."""
    random = uuid.uuid4().int
    ms = time.time_ns() // 1_000_000
    value = (ms << 80) | (7 << 76) | (random & ((1 << 76) - 1))
    return str(uuid.UUID(int=(value & ~(3 << 62)) | (2 << 62)))


def compacted_row(recovery):
    meta = recovery.thread_metadata
    previous = meta["context_window"]["window_id"]
    reports = recovery.plan
    assert reports["status"] == "READY_FOR_ISOLATED_CANDIDATE"
    return {
        "timestamp": recovery.timestamp,
        "type": "compacted",
        "payload": {
            "message": recovery.replacement[0]["content"][0]["text"].removeprefix(SUMMARY_PREFIX + "\n"),
            "replacement_history": recovery.replacement,
            "window_number": 1,
            "first_window_id": previous,
            "previous_window_id": previous,
            "window_id": new_window_id(),
            "resume_metadata": {
                "last_started_turn_id": recovery.final_turn_id,
                "multi_agent_version": meta["multi_agent_version"],
                "previous_turn_settings": None,
            },
        },
    }


def create_private(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def isolated_output(output, *other_protected):
    from recovery_state import absolute_path, directory_fd
    output = absolute_path(output)
    configured_homes = [Path(os.environ[key]).expanduser()
                        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR") if os.environ.get(key)]
    protected = (Path.home() / ".claude", Path.home() / ".codex",
                 *configured_homes, *other_protected)
    if any(output.is_relative_to(root.resolve()) for root in protected):
        raise Unavailable("OUTPUT_MUST_BE_AN_EXISTING_ISOLATED_DIRECTORY")
    with directory_fd(output, private=False) as fd:
        state = os.fstat(fd)
        if state.st_uid != os.geteuid() or state.st_mode & 0o022:
            raise Unavailable("OUTPUT_DIRECTORY_HAS_UNSAFE_OWNER_OR_PERMISSIONS")
    return output


def bounded_bundle_lines(file):
    for index in range(1, NATIVE_RECORD_LIMIT + 2):
        line = file.readline(MAX_REPLAY_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_REPLAY_BYTES or index > NATIVE_RECORD_LIMIT:
            raise Unavailable("NATIVE_BUNDLE_RECORD_LIMIT")
        yield index, line


def build(inputs, recovery, output):
    if recovery.plan["status"] != "READY_FOR_ISOLATED_CANDIDATE":
        return recovery.plan
    output = isolated_output(output, inputs.codex, inputs.claude)
    paths = (recovery.source_path, recovery.rollout_path, inputs.ledger_path)
    if recovery.before != [signature(path.stat(follow_symlinks=False)) for path in paths]:
        raise Unavailable("INPUT_CHANGED_BEFORE_CANDIDATE_BUILD")
    if recovery.rollout_path.stat().st_size > NATIVE_BYTES_LIMIT:
        raise Unavailable("NATIVE_BUNDLE_SIZE_LIMIT")
    row = compacted_row(recovery)
    directory = Path(tempfile.mkdtemp(prefix=".candidate-incomplete-", dir=output))
    created = []
    try:
        candidate = directory / "candidate.jsonl"
        created.append(candidate)
        hasher = hashlib.sha256()
        with read_only(inputs.codex, recovery.rollout_path) as original, create_private(candidate) as target:
            for line, raw in bounded_bundle_lines(original):
                hasher.update(raw)
                target.write(raw)
                if line == recovery.insert_after:
                    if not raw.endswith(b"\n"):
                        raise Unavailable("IMPORT_END_NEEDS_JSONL_NEWLINE")
                    target.write(encode(row))
        if hasher.hexdigest() != recovery.original_digest:
            raise Unavailable("ORIGINAL_ROLLOUT_CHANGED_DURING_BUILD")
        if (recovery.before != [signature(path.stat(follow_symlinks=False)) for path in paths]
                or hash_only(inputs.codex, inputs.ledger_path) != inputs.ledger_snapshot):
            raise Unavailable("INPUT_CHANGED_DURING_CANDIDATE_BUILD")
        expected = directory / "expected.jsonl"
        created.append(expected)
        with create_private(expected) as target:
            for item in recovery.replacement:
                target.write(encode({"timestamp": recovery.timestamp, "type": "response_item", "payload": item}))
            for item in recovery.post:
                target.write(encode(item))
        spec = directory / "expectations.json"
        created.append(spec)
        with create_private(spec) as target:
            target.write(encode({
                "codex_main_commit": UPSTREAM,
                "original_sha256": recovery.original_digest,
                "original_rows": recovery.original_records,
                "added_line": recovery.insert_after + 1,
                "replacement_items": len(recovery.replacement),
                "candidate_sha256": hash_only(directory, candidate),
                "expected_sha256": hash_only(directory, expected),
            }))
        published = output / f"candidate-{uuid.uuid4()}"
        directory.rename(published)
        return {**recovery.plan, "status": "ISOLATED_CANDIDATE_BUILT", "candidate_path": str(published)}
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        directory.rmdir()
        raise


def read_manifest(root):
    with read_only(root, root / BUNDLE_FILES[2]) as stream:
        manifest = parse(stream.read(16 * 1024))
    if manifest.get("codex_main_commit") != UPSTREAM:
        raise Unavailable("CANDIDATE_VERSION_MISMATCH")
    checkpoint_line = manifest.get("added_line")
    if not uint(checkpoint_line) or not checkpoint_line:
        raise Unavailable("CANDIDATE_BOUNDARY_MISMATCH")
    if ("startup_token_rebase_line" in manifest and
            manifest["startup_token_rebase_line"] != checkpoint_line + 1):
        raise Unavailable("CANDIDATE_STARTUP_TOKEN_BOUNDARY_MISMATCH")
    return manifest


def validate(path, native_test_bin):
    from recovery_state import absolute_path, verify_private_files
    root = absolute_path(path)
    from session_doctor import checkpoint
    verify_private_files(root, *BUNDLE_FILES)
    candidate, expected, _ = (root / name for name in BUNDLE_FILES)
    if candidate.stat().st_size + expected.stat().st_size > NATIVE_BYTES_LIMIT:
        raise Unavailable("NATIVE_BUNDLE_SIZE_LIMIT")
    manifest = read_manifest(root)
    if any(hash_only(root, file) != manifest.get(key) for file, key in
           ((candidate, "candidate_sha256"), (expected, "expected_sha256"))):
        raise Unavailable("CANDIDATE_BUNDLE_DIGEST_MISMATCH")
    digest = hashlib.sha256()
    count = 0
    expected_additions = {manifest["added_line"]: "compacted"}
    rebase_line = manifest.get("startup_token_rebase_line")
    if rebase_line is not None:
        expected_additions[rebase_line] = "event_msg"
    found = set()
    with read_only(root, candidate) as stream:
        for count, raw in bounded_bundle_lines(stream):
            if count not in expected_additions:
                digest.update(raw)
            else:
                row = parse(raw)
                payload = row.get("payload")
                if (row.get("type") != expected_additions[count]
                        or not isinstance(payload, dict)
                        or (count == rebase_line and
                            payload.get("type") != "token_count")):
                    raise Unavailable("CANDIDATE_BOUNDARY_MISMATCH")
                if count != rebase_line:
                    try:
                        decoded, _ = checkpoint(payload, count)
                    except ValueError as exc:
                        raise Unavailable("CANDIDATE_CHECKPOINT_SCHEMA_UNSUPPORTED") from exc
                    if not decoded["has_replacement_history"] or not decoded["has_resume_metadata"]:
                        raise Unavailable("CANDIDATE_CHECKPOINT_SCHEMA_UNSUPPORTED")
                found.add(count)
    if (found != set(expected_additions) or count - len(found) != manifest.get("original_rows")
            or digest.hexdigest() != manifest.get("original_sha256")):
        raise Unavailable("CANDIDATE_ORIGINAL_ROLLOUT_BYTES_MISMATCH")
    if native_test_bin is None:
        return {"status": "NATIVE_VALIDATION_REQUIRED", "original_rollout_preserved": True}
    with read_only(root, expected) as stream:
        for _ in bounded_bundle_lines(stream):
            pass
    binary = native_test_bin.resolve(strict=True)
    env = dict(os.environ, SESSION_DOCTOR_NATIVE_DIR=str(root))
    try:
        run = subprocess.run([str(binary), NATIVE_TEST, "--exact", "--ignored", "--nocapture"],
                             capture_output=True, text=True, env=env, timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise Unavailable("NATIVE_VALIDATION_TIMED_OUT") from exc
    if run.returncode != 0 or "1 passed; 0 failed" not in run.stdout:
        raise Unavailable("NATIVE_CANDIDATE_RECONSTRUCTION_FAILED")
    prefix = "SESSION_DOCTOR_NATIVE_METRICS "
    values = [line.split(prefix, 1)[1] for line in run.stdout.splitlines() if prefix in line]
    if len(values) != 1:
        raise Unavailable("NATIVE_CANDIDATE_METRICS_MISSING")
    metrics = parse(values[0].encode())
    for field in ("before", "after"):
        measurement = metrics.get(field)
        if not isinstance(measurement, dict) or set(measurement) != {"normalized_model_items", "estimated_tokens"}:
            raise Unavailable("NATIVE_CANDIDATE_METRICS_INVALID")
        if not all(uint(value) for value in measurement.values()):
            raise Unavailable("NATIVE_CANDIDATE_METRICS_INVALID")
    startup = metrics.get("startup")
    if startup is not None:
        check_startup_metrics(startup)
    if rebase_line is not None and (startup is None or not startup["uses_local_rebase"]):
        raise Unavailable("NATIVE_STARTUP_REBASE_NOT_VERIFIED")
    if any(hash_only(root, file) != manifest.get(key) for file, key in
           ((candidate, "candidate_sha256"), (expected, "expected_sha256"))):
        raise Unavailable("CANDIDATE_CHANGED_DURING_VALIDATION")
    return {"status": "NATIVE_VALIDATED", "codex_main_commit": UPSTREAM,
            "original_rollout_preserved": True, "native_test": NATIVE_TEST, "metrics": metrics}


def check_startup_metrics(startup):
    count_keys = {"covered_history_estimate_tokens", "unreported_tail_estimate_tokens",
                  "total_replayed_history_estimate_tokens", "startup_context_usage_tokens",
                  "post_import_rollout_items", "post_import_response_items"}
    optional_counts = {"original_import_estimate_tokens", "selected_last_usage_tokens"}
    flags = {"eligible_import_estimate", "uses_local_rebase"}
    if (not isinstance(startup, dict) or set(startup) != count_keys | optional_counts | flags
            or not all(uint(startup[key]) for key in count_keys)
            or not all(startup[key] is None or uint(startup[key]) for key in optional_counts)
            or not all(type(startup[key]) is bool for key in flags)):
        raise Unavailable("NATIVE_STARTUP_METRICS_INVALID")


def startup_estimate_event(timestamp, covered_tokens):
    zero = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
            "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0}
    return {"type": "event_msg", "timestamp": timestamp, "payload": {
        "type": "token_count", "rate_limits": None, "info": {
            "total_token_usage": zero,
            "last_token_usage": {**zero, "total_tokens": covered_tokens},
            "last_usage_includes_reasoning": True, "model_context_window": None,
        },
    }}


def rebase_candidate(path, output, native_test_bin, *protected_homes):
    """Publish a separately validated copy with a locally measured startup estimate."""
    from recovery_state import absolute_path
    root = absolute_path(path)
    output = isolated_output(output, root, *protected_homes)
    manifest = read_manifest(root)
    if "startup_token_rebase_line" in manifest:
        raise Unavailable("CANDIDATE_ALREADY_REBASED")
    baseline = validate(root, native_test_bin)["metrics"].get("startup")
    if baseline is None:
        raise Unavailable("NATIVE_STARTUP_VALIDATOR_REQUIRED")
    if not baseline["eligible_import_estimate"]:
        raise Unavailable("STARTUP_USAGE_PROVENANCE_NOT_ELIGIBLE")
    directory = Path(tempfile.mkdtemp(prefix=".candidate-incomplete-", dir=output))
    try:
        candidate, expected, spec = (directory / name for name in BUNDLE_FILES)
        with read_only(root, root / candidate.name) as old, create_private(candidate) as new:
            for line, raw in bounded_bundle_lines(old):
                new.write(raw)
                if line == manifest["added_line"]:
                    if not raw.endswith(b"\n"):
                        raise Unavailable("CANDIDATE_BOUNDARY_MISMATCH")
                    new.write(encode(startup_estimate_event(
                        parse(raw)["timestamp"], baseline["covered_history_estimate_tokens"])))
        with read_only(root, root / expected.name) as old, create_private(expected) as new:
            shutil.copyfileobj(old, new)
        with create_private(spec) as file:
            file.write(encode({
                **manifest, "startup_token_rebase_line": manifest["added_line"] + 1,
                "candidate_sha256": hash_only(directory, candidate),
            }))
        result = validate(directory, native_test_bin)
        startup = result["metrics"]["startup"]
        if (startup["startup_context_usage_tokens"] != baseline["total_replayed_history_estimate_tokens"]
                or startup["post_import_rollout_items"] != baseline["post_import_rollout_items"]
                or startup["post_import_response_items"] != baseline["post_import_response_items"]):
            raise Unavailable("NATIVE_STARTUP_REBASE_CHANGED_REPLAY")
        published = output / f"candidate-rebased-{uuid.uuid4()}"
        directory.rename(published)
        return {**result, "candidate_path": str(published), "startup_before_rebase": baseline}
    except BaseException:
        shutil.rmtree(directory)
        raise
