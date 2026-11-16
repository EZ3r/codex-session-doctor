#!/usr/bin/env python3
"""Offline diagnostics for local Claude Code -> Codex imports (Python 3.10+)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

VERSION = "0.1.0"
UPSTREAM = "0184dfa6a48f0110dd9c6835522492908e5abcf3"
MARKER = "<EXTERNAL SESSION IMPORTED>"
LEDGER = "external_agent_session_imports.json"
MAX_LEDGER = 32 * 1024 * 1024
SAMPLES = 20
SOURCE_IDENTIFIER_LIMIT = 256
SOURCE_LINEAGE_BUDGET = 32 * 1024 * 1024
SOURCE_ATTACHMENT_LIMIT = 4096
HEX_SHA = re.compile(r"[0-9a-f]{64}\Z")
ROLLOUT_NAME = re.compile(r"rollout-.*?([0-9a-fA-F-]{36})\.jsonl(?:\.zst)?\Z")


class Unavailable(Exception):
    """Carries only a fixed, non-sensitive diagnostic code."""


def thread_id(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def uint(value):
    return type(value) is int and 0 <= value < 2**64


def signature(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@contextmanager
def read_only(root: Path, path: Path):
    """Open a regular file beneath an explicit root, with no symlink traversal."""
    parts = path.relative_to(root).parts
    if not parts or any(p == ".." for p in parts):
        raise Unavailable("PATH_OUTSIDE_INPUT_ROOT")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, dir_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, dir_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise Unavailable("NOT_A_REGULAR_FILE")
            yield stream
            if signature(st) != signature(os.fstat(stream.fileno())) or signature(st) != signature(
                os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            ) or signature(st) != signature(path.stat(follow_symlinks=False)):
                raise Unavailable("FILE_CHANGED_DURING_READ")
    finally:
        os.close(directory)


def object_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def reject_constant(_):
    raise ValueError("non-JSON number")


def parse(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs,
                       parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def size(text):
    return len(text.encode("utf-8"))


def json_size(value):
    return size(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


@dataclass
class Diagnostics:
    counts: Counter = field(default_factory=Counter)
    samples: list = field(default_factory=list)
    blockers: int = 0

    def add(self, code, line=None, byte_count=None, *, blocking=False):
        self.counts[code] += 1
        self.blockers += int(blocking)
        if len(self.samples) < SAMPLES:
            sample = {"code": code}
            if line is not None:
                sample["line"] = line
            if byte_count is not None:
                sample["bytes"] = byte_count
            self.samples.append(sample)

    def output(self):
        return {"counts": dict(self.counts), "samples": self.samples, "blocking_count": self.blockers}


def hash_only(root, path):
    digest = hashlib.sha256()
    with read_only(root, path) as stream:
        remaining = os.fstat(stream.fileno()).st_size
        while remaining and (chunk := stream.read(min(remaining, 64 * 1024))):
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining:
            raise Unavailable("FILE_CHANGED_DURING_READ")
    return digest.hexdigest()


def scan_jsonl(root, path, consume: Callable, limits, diagnostics):
    digest = hashlib.sha256()
    lines = 0
    no_final_newline = False
    with read_only(root, path) as stream:
        # Bound traversal to the initial file length even if a writer keeps appending.
        remaining = os.fstat(stream.fileno()).st_size
        while remaining and (raw := stream.readline(min(remaining, limits.max_line_bytes + 1))):
            remaining -= len(raw)
            lines += 1
            if lines > 1_000_000:
                raise Unavailable("JSONL_RECORD_COUNT_LIMIT")
            digest.update(raw)
            length = len(raw)
            too_big = length > limits.max_line_bytes
            if too_big:
                while not raw.endswith(b"\n") and remaining:
                    raw = stream.readline(min(remaining, 64 * 1024))
                    if not raw:
                        break
                    remaining -= len(raw)
                    digest.update(raw)
                    length += len(raw)
            if length >= limits.large_line_bytes:
                diagnostics.add("LARGE_JSONL_RECORD", lines, length)
            if not remaining:
                no_final_newline = not raw.endswith(b"\n")
            if too_big:
                diagnostics.add("JSONL_RECORD_OVER_PARSE_LIMIT", lines, length, blocking=True)
                continue
            try:
                if not raw.strip():
                    continue
                record = parse(raw)
                consume(record, lines, diagnostics)
            except (ValueError, RecursionError, OverflowError):
                code = "MALFORMED_JSONL" if raw.endswith(b"\n") or remaining else "POSSIBLY_TRUNCATED_JSONL"
                diagnostics.add(code, lines, length, blocking=True)
        if remaining:
            raise Unavailable("FILE_CHANGED_DURING_READ")
    return {"lines": lines, "final_line_without_newline": no_final_newline}, digest.hexdigest()


@dataclass(frozen=True)
class Scale:
    items: int = 0
    bytes: int = 0
    approximate_items: int = 0

    def __add__(self, other):
        return Scale(self.items + other.items, self.bytes + other.bytes,
                     self.approximate_items + other.approximate_items)

    def output(self):
        return {"items": self.items, "estimated_model_bytes": self.bytes,
                "estimated_tokens": (self.bytes + 3) // 4,
                "items_with_additional_projection_uncertainty": self.approximate_items}


def source_text(record):
    """Plain summary text; do not treat tool content as a checkpoint summary."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text"
                                        and isinstance(p.get("text"), str) for p in content):
        text = "\n\n".join(p["text"] for p in content if p["text"].strip())
        return text or None
    return None


def unwrap(text):
    trimmed = text.strip()
    if trimmed.startswith("<user_query>") and trimmed.endswith("</user_query>"):
        return trimmed[12:-13].strip() or text
    return text


def source_message_size(record):
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text = source_text(record)
    if text is not None:
        return Scale(1, size(unwrap(text) if record["type"] == "user" else text))
    if isinstance(content, list):
        parts = [p for p in content if isinstance(p, dict) and p.get("type") not in (None, "thinking")]
        if parts:
            # The importer renders and truncates tool blocks. Use serialized blocks as a proxy.
            return Scale(1, json_size(parts), 1)
    return Scale()


@dataclass(frozen=True)
class SourceNode:
    line: int
    uid: str
    parent: str | None
    kind: str
    summary: bool
    tool_calls: tuple
    tool_results: tuple
    result_only: bool
    tool_origin: str | None


def source_node(record, line, uid, parent):
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    calls = tuple(block["id"] for block in blocks
                  if block.get("type") == "tool_use" and isinstance(block.get("id"), str))
    results = tuple(block["tool_use_id"] for block in blocks if block.get("type") == "tool_result"
                    and isinstance(block.get("tool_use_id"), str))
    origin = record.get("sourceToolAssistantUUID")
    identifiers = (uid, parent, origin, *calls, *results)
    if any(isinstance(value, str) and len(value) > SOURCE_IDENTIFIER_LIMIT for value in identifiers):
        raise Unavailable("CLAUDE_LINEAGE_IDENTIFIER_TOO_LONG")
    return SourceNode(line, uid, parent, record["type"], record.get("isCompactSummary") is True,
                      calls, results, bool(results) and len(results) == len(content),
                      origin if isinstance(origin, str) else None)


class Source:
    def __init__(self, project=None):
        self.boundaries = []
        self.summaries = []
        self.main_boundary = None
        self.main_summary = None
        self.sidechain_markers = 0
        self.checkpoints = []
        self.pending = None
        self.all = Scale()
        self.tail = Scale()
        self.latest_digest = None
        self.latest_summary = Scale()
        self.saw_user = False
        self.nodes = {}
        self.duplicate_ids = set()
        self.message_lines = {}
        self.checkpoint_nodes = {}
        self.imported = {}
        self.project = project
        self.logical = None
        self.logical_error = None
        self.lineage_bytes = 0

    def walk(self, start, stops):
        """Follow explicit parent links and return the path in chronological order."""
        path = []
        current = start
        while current not in stops:
            node = self.nodes.get(current)
            if node is None:
                raise Unavailable("CLAUDE_LINEAGE_MISSING_PARENT")
            if node.uid in self.duplicate_ids:
                raise Unavailable("CLAUDE_ACTIVE_LINEAGE_UUID_REPEATED")
            if path and node.line >= path[-1].line:
                raise Unavailable("CLAUDE_LINEAGE_CYCLE_OR_FORWARD_PARENT")
            path.append(node)
            current = node.parent
        return list(reversed(path)), current

    def include_linked_tool_results(self, chain, visible_lines):
        """Verify parallel results omitted by the single parent chain."""
        included = {node.line for node in chain if node.line in self.message_lines}
        off_path = visible_lines - included
        if not off_path:
            return sorted(included)
        calls = defaultdict(list)
        existing_results = Counter()
        for node in chain:
            if node.kind == "assistant":
                for call in node.tool_calls:
                    calls[call].append(node)
            existing_results.update(node.tool_results)
        by_line = {node.line: node for node in self.nodes.values() if node.line in off_path}
        for line in sorted(off_path):
            node = by_line.get(line)
            if (node is None or node.uid in self.duplicate_ids or node.kind != "user"
                    or not node.result_only or node.summary):
                raise Unavailable("CLAUDE_TAIL_BRANCH_AMBIGUOUS")
            for result in node.tool_results:
                possible = calls[result]
                if (len(possible) != 1 or existing_results[result]
                        or node.parent != possible[0].uid or node.tool_origin != possible[0].uid
                        or possible[0].line >= node.line):
                    raise Unavailable("CLAUDE_PARALLEL_TOOL_RESULT_UNVERIFIED")
                existing_results[result] += 1
        return sorted(included | off_path)

    def logical_history(self, checkpoint):
        summary, segment, boundary_path = self.checkpoint_nodes[checkpoint["summary_line"]]
        if summary is None or not checkpoint["parent_verified"]:
            raise Unavailable("CLAUDE_CHECKPOINT_IDENTITY_UNVERIFIED")
        if summary in self.duplicate_ids or self.duplicate_ids.intersection(boundary_path):
            raise Unavailable("CLAUDE_ACTIVE_LINEAGE_UUID_REPEATED")
        preserved = []
        stop = {summary}
        if segment is not None:
            if not isinstance(segment, dict) or segment.get("anchorUuid") != summary:
                raise Unavailable("CLAUDE_PRESERVED_ANCHOR_MISMATCH")
            head, tail = (segment.get(key) for key in ("headUuid", "tailUuid"))
            if not isinstance(head, str) or not isinstance(tail, str):
                raise Unavailable("CLAUDE_PRESERVED_ENDPOINT_MISSING")
            body, _ = self.walk(tail, {head})
            first = self.nodes.get(head)
            if first is None or (body and first.line >= body[0].line):
                raise Unavailable("CLAUDE_PRESERVED_HEAD_UNVERIFIED")
            if first.uid in self.duplicate_ids:
                raise Unavailable("CLAUDE_ACTIVE_LINEAGE_UUID_REPEATED")
            preserved = [first, *body]
            if any(node.line >= checkpoint["boundary_line"] or node.summary for node in preserved):
                raise Unavailable("CLAUDE_PRESERVED_SEGMENT_CROSSES_CHECKPOINT")
            stop.add(tail)
        tail_lines = {line for line in self.message_lines if line > checkpoint["summary_line"]}
        by_line = {node.line: uid for uid, node in self.nodes.items() if node.line in tail_lines}
        if tail_lines != by_line.keys():
            raise Unavailable("CLAUDE_TAIL_MESSAGE_ID_MISSING")
        tail_nodes, _ = self.walk(by_line[max(tail_lines)] if tail_lines else summary, stop)
        if any(node.line <= checkpoint["summary_line"] or node.summary for node in tail_nodes):
            raise Unavailable("CLAUDE_TAIL_BRANCH_AMBIGUOUS")
        kept = []
        if preserved:
            span = {line for line in self.message_lines if preserved[0].line <= line <= preserved[-1].line}
            kept = self.include_linked_tool_results(preserved, span)
        return kept, self.include_linked_tool_results(tail_nodes, tail_lines)

    def consume(self, record, line, diagnostics):
        kind = record.get("type")
        if not isinstance(kind, str) or len(kind) > SOURCE_IDENTIFIER_LIMIT:
            diagnostics.add("CLAUDE_RECORD_TYPE_UNRECOGNIZED", line, blocking=True)
            return
        boundary = kind == "system" and record.get("subtype") == "compact_boundary"
        flag = record.get("isCompactSummary")
        side = record.get("isSidechain") is True
        uid, parent = record.get("uuid"), record.get("parentUuid")
        if any(isinstance(value, str) and len(value) > SOURCE_IDENTIFIER_LIMIT for value in (uid, parent)):
            raise Unavailable("CLAUDE_LINEAGE_IDENTIFIER_TOO_LONG")
        uid = uid if isinstance(uid, str) and uid else None
        parent_id = parent if isinstance(parent, str) and parent else None
        if uid and not side:
            if uid in self.nodes:
                diagnostics.add("CLAUDE_REPEATED_RECORD_UUID", line)
                self.duplicate_ids.add(uid)
            node = source_node(record, line, uid, parent_id)
            identifiers = (node.uid, node.parent, node.kind, node.tool_origin, *node.tool_calls, *node.tool_results)
            self.lineage_bytes += 64 + sum(32 + 4 * len(value) for value in identifiers if value is not None)
            if self.lineage_bytes > SOURCE_LINEAGE_BUDGET:
                raise Unavailable("CLAUDE_LINEAGE_METADATA_BUDGET_EXCEEDED")
            self.nodes[uid] = node
        if self.project is not None:
            projection = self.project(record)
            if projection is not None:
                self.imported[line] = projection
        if flag is not None and type(flag) is not bool:
            diagnostics.add("INVALID_CLAUDE_SUMMARY_FLAG", line, blocking=True)
        if boundary:
            self.boundaries.append(line)
            if not side:
                self.main_boundary = line
                if self.pending is not None:
                    diagnostics.add("CLAUDE_BOUNDARY_WITHOUT_SUMMARY", self.pending[0])
                identity = record.get("uuid")
                if identity is not None and (not isinstance(identity, str) or not identity.strip()):
                    diagnostics.add("INVALID_CLAUDE_BOUNDARY_ID", line, blocking=True)
                compact_metadata = record.get("compactMetadata")
                segment = compact_metadata.get("preservedSegment") if isinstance(compact_metadata, dict) else None
                if isinstance(segment, dict):
                    segment = {key: segment.get(key) for key in ("headUuid", "tailUuid", "anchorUuid")}
                    if any(isinstance(value, str) and len(value) > SOURCE_IDENTIFIER_LIMIT
                           for value in segment.values()):
                        raise Unavailable("CLAUDE_LINEAGE_IDENTIFIER_TOO_LONG")
                    segment = {key: value if isinstance(value, str) else None for key, value in segment.items()}
                elif segment is not None:
                    segment = False  # Malformed declaration. Keep evidence, not the payload.
                self.pending = (line, uid, uid, segment, [])
        if (not side and kind == "attachment" and self.pending and uid
                and parent_id is not None and parent_id == self.pending[2]):
            start, original, predecessor, segment, chain = self.pending
            if len(chain) >= SOURCE_ATTACHMENT_LIMIT:
                raise Unavailable("CLAUDE_ATTACHMENT_CHAIN_LIMIT")
            chain.append(predecessor)
            self.pending = (start, original, uid, segment, chain)
        if flag is True:
            self.summaries.append(line)
            if not side:
                self.main_summary = line
                text = source_text(record) if kind == "user" else None
                parent = record.get("parentUuid")
                if parent is not None and (not isinstance(parent, str) or not parent.strip()):
                    diagnostics.add("INVALID_CLAUDE_SUMMARY_PARENT", line, blocking=True)
                if self.pending and text is not None and (
                    parent is None or self.pending[1] is None or parent == self.pending[2]
                ):
                    self.latest_summary = Scale(1, size(text))
                    self.tail = Scale()
                    self.latest_digest = (hashlib.sha256(unwrap(text).encode()).digest()
                                          if record.get("isMeta") is not True else None)
                    self.checkpoint_nodes[line] = (uid, self.pending[3], (*self.pending[4], self.pending[2]))
                    self.checkpoints.append({"boundary_line": self.pending[0], "summary_line": line,
                                             "parent_verified": parent_id is not None and parent_id == self.pending[2],
                                             "attachment_chain_records": len(self.pending[4]),
                                             "has_preserved_segment": self.pending[3] is not None,
                                             "summary_skipped_by_importer_as_meta": record.get("isMeta") is True,
                                             "before_boundary_import_projection": self.all.output()})
                else:
                    diagnostics.add("UNPAIRED_OR_INVALID_CLAUDE_SUMMARY", line)
                self.pending = None
        elif not side and kind in ("user", "assistant") and record.get("isMeta") is not True:
            if self.pending and source_message_size(record).items:
                diagnostics.add("CLAUDE_BOUNDARY_WITHOUT_SUMMARY", self.pending[0])
                self.pending = None
        if kind in ("user", "assistant") and not side and record.get("isMeta") is not True:
            amount = source_message_size(record)
            if amount.items and (self.saw_user or kind == "user"):
                self.saw_user = True
                self.all += amount
                self.message_lines[line] = amount
                if self.checkpoints and line > self.checkpoints[-1]["summary_line"]:
                    self.tail += amount
        if side and (boundary or flag is True):
            self.sidechain_markers += 1

    def output(self, diagnostics):
        if self.pending:
            diagnostics.add("CLAUDE_BOUNDARY_WITHOUT_SUMMARY", self.pending[0])
        latest = self.checkpoints[-1] if self.checkpoints else None
        valid_pair = bool(latest and self.main_boundary == latest["boundary_line"]
                          and self.main_summary == latest["summary_line"])
        logical = None
        if valid_pair:
            try:
                self.logical = self.logical_history(latest)
                preserved, tail = self.logical
                preserved_size = sum((self.message_lines[line] for line in preserved), Scale())
                tail_size = sum((self.message_lines[line] for line in tail), Scale())
                logical = {"preserved_source_lines": preserved, "preserved_projection": preserved_size.output(),
                           "logical_tail_items": tail_size.items,
                           "summary_preserved_and_logical_tail_projection":
                               (self.latest_summary + preserved_size + tail_size).output()}
            except Unavailable as exc:
                self.logical_error = str(exc)
                diagnostics.add(str(exc), latest["summary_line"])
        return {"boundary_count": len(self.boundaries), "boundary_lines": self.boundaries,
                "summary_count": len(self.summaries), "summary_lines": self.summaries,
                "sidechain_marker_count": self.sidechain_markers,
                "paired_checkpoint_count": len(self.checkpoints), "paired_checkpoints": self.checkpoints,
                "latest_markers_form_valid_pair": valid_pair,
                "logical_history": logical, "logical_history_error": self.logical_error,
                "unbounded_import_projection": self.all.output(),
                "latest_pair_summary_and_tail_projection": (self.latest_summary + self.tail).output()
                if latest else None}


def optional_string(obj, key):
    return obj.get(key) is None or isinstance(obj[key], str)


def item_scale(item, *, replacement=False, metadata=None):
    """Estimate replayed history, before runtime prompt normalization and injection."""
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        raise ValueError("invalid response item")
    kind = item["type"]
    if kind == "message":
        role, content = item.get("role"), item.get("content")
        if not isinstance(role, str) or not isinstance(content, list):
            raise ValueError("invalid message")
        if not all(optional_string(item, key) for key in ("encrypted_content", "status")):
            raise ValueError("invalid message options")
        if item.get("phase") not in (None, "commentary", "final_answer"):
            raise ValueError("invalid phase")
        total = 0
        uncertain = item.get("encrypted_content") is not None
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("invalid content")
            if part.get("type") in ("input_text", "output_text") and isinstance(part.get("text"), str):
                if any(part.get(key) is not None and not isinstance(part[key], list)
                       for key in ("annotations", "logprobs")):
                    raise ValueError("invalid content options")
                total += size(part["text"])
            elif part.get("type") == "input_image" and any(isinstance(part.get(key), str)
                                                          for key in ("image_url", "file_id")):
                total += 7373
                uncertain = True
            elif part.get("type") == "input_audio" and any(isinstance(part.get(key), str)
                                                          for key in ("audio_url", "file_id")):
                uncertain = True
            else:
                raise ValueError("unknown message content")
        return Scale(1, total, int(uncertain)) if replacement or role != "system" else Scale()
    if kind in ("compaction", "compaction_summary", "context_compaction", "reasoning"):
        if not optional_string(item, "encrypted_content"):
            raise ValueError("invalid encrypted content")
        encrypted = item.get("encrypted_content")
        n = size(encrypted or "")
        if kind in ("compaction", "compaction_summary") and encrypted is None:
            raise ValueError("missing encrypted content")
        if kind == "reasoning" and not isinstance(item.get("summary"), list):
            raise ValueError("invalid reasoning")
        return Scale(1, max(0, n * 3 // 4 - 650) if kind == "reasoning" else (n + 2) // 3)
    if kind in ("function_call", "custom_tool_call"):
        args = "arguments" if kind == "function_call" else "input"
        if not all(isinstance(item.get(key), str) for key in ("name", "call_id", args)) or not optional_string(item, "namespace"):
            raise ValueError("invalid call")
        return Scale(1, size(item["name"]) + size(item.get("namespace") or "") + size(item[args]), 1)
    if kind in ("function_call_output", "custom_tool_call_output"):
        output = item.get("output")
        if not isinstance(output, (str, list)):
            raise ValueError("invalid call output")
        return Scale(1, (size(output) if isinstance(output, str) else json_size(output)) + sum(
            size(item[key]) for key in ("name", "call_id", "namespace") if isinstance(item.get(key), str)
        ), 1)
    if kind in ("agent_message", "additional_tools", "tool_search_call", "tool_search_output",
                "local_shell_call", "web_search_call", "image_generation_call"):
        return Scale(1, json_size(item), 1)
    if kind == "configuration_update":
        return Scale(1) if replacement or (metadata or {}).get("harness_authored_configuration") is True else Scale()
    if kind == "compaction_trigger":
        return Scale(1, 0, 1) if replacement else Scale()
    raise ValueError("unknown response item variant")


def window_id_wire(value):
    return isinstance(value, str) or (isinstance(value, dict)
           and isinstance(value.get("salt"), str) and uint(value.get("ordinal"))
           and uint(value.get("value")))


def resume_shape(meta):
    if not isinstance(meta, dict) or not optional_string(meta, "last_started_turn_id"):
        return False
    if set(meta) - {"next_item_ordinal", "observation_clock_floor_ms", "rejected_audio_file_ids",
                    "dynamic_tools", "multi_agent_version", "last_started_turn_id",
                    "previous_turn_settings", "turn_attribution"}:
        return False
    ordinal, clock = meta.get("next_item_ordinal"), meta.get("observation_clock_floor_ms")
    audio = meta.get("rejected_audio_file_ids", [])
    tools, previous, attribution = (meta.get(k) for k in ("dynamic_tools", "previous_turn_settings",
                                                         "turn_attribution"))
    return (meta.get("multi_agent_version") in (None, "disabled", "v1", "v2")
            and (ordinal is None or uint(ordinal))
            and (clock is None or (type(clock) is int and -(2**63) <= clock < 2**63))
            and isinstance(audio, list) and all(isinstance(a, str) for a in audio)
            and (tools is None or (isinstance(tools, dict) and thread_id(tools.get("thread_id"))
                                   and isinstance(tools.get("tools"), list)
                                   and all(isinstance(t, dict) for t in tools["tools"])))
            and (previous is None or (isinstance(previous, dict) and isinstance(previous.get("model"), str)
                                      and optional_string(previous, "comp_hash")
                                      and (previous.get("realtime_active") is None
                                           or type(previous["realtime_active"]) is bool)))
            and (attribution is None or (isinstance(attribution, dict)
                                         and isinstance(attribution.get("turn_id"), str))))


def checkpoint(payload, line):
    if set(payload) - {"message", "replacement_history", "replacement_history_metadata",
                       "guardian_history", "retained_context", "mcp_resource_origins", "window_number",
                       "first_window_id", "previous_window_id", "window_id", "compaction_response_id",
                       "latest_token_usage_record", "resume_metadata"}:
        raise ValueError("unknown checkpoint recovery fields")
    history, number, metadata = (payload.get(key) for key in
                                  ("replacement_history", "window_number", "resume_metadata"))
    wire_meta = payload.get("replacement_history_metadata")
    guardian = payload.get("guardian_history")
    if (not isinstance(payload.get("message"), str)
            or (history is not None and not isinstance(history, list))
            or (number is not None and not uint(number))
            or (metadata is not None and not resume_shape(metadata))
            or (wire_meta is not None and (not isinstance(wire_meta, list) or history is None
                or len(history) != len(wire_meta) or not all(isinstance(m, dict) for m in wire_meta)))
            or any(payload.get(k) is not None and not window_id_wire(payload[k])
                   for k in ("first_window_id", "previous_window_id"))
            or not optional_string(payload, "compaction_response_id")
            or any(payload.get(k) is not None and not isinstance(payload[k], dict)
                   for k in ("retained_context", "mcp_resource_origins", "latest_token_usage_record"))
            or (guardian is not None and (not isinstance(guardian, list)
                                          or not all(isinstance(item, dict) for item in guardian)))):
        raise ValueError("invalid checkpoint envelope")
    win_id = payload.get("window_id")
    if win_id is not None and not (window_id_wire(win_id) or uint(win_id)):
        raise ValueError("invalid window id")
    legacy = number is None and uint(win_id)
    if legacy:
        number = win_id
    amount = Scale()
    opaque = False
    for index, item in enumerate(history or []):
        amount += item_scale(item, replacement=True, metadata=wire_meta[index] if wire_meta else None)
        opaque |= item["type"] in ("compaction", "compaction_summary", "context_compaction")
    return {"line": line, "window_number": number, "legacy_numeric_window_id": legacy,
            "has_replacement_history": history is not None, "has_resume_metadata": metadata is not None,
            "opaque_model_compatibility_unverified": opaque,
            "replacement": amount.output() if history is not None else None}, amount


ROLLOUT_TYPES = frozenset(("session_meta", "response_item", "compacted", "event_msg", "turn_context",
                          "world_state", "dynamic_tools_updated", "retained_context", "token_usage_record",
                          "realtime_item", "inter_agent_communication", "inter_agent_communication_metadata",
                          "security_risk_score"))


def session_meta(record):
    payload = record.get("payload")
    if record.get("type") == "session_meta" and isinstance(payload, dict):
        return payload
    return {}


def recovery_identity(meta):
    """Legacy allows appended metadata snapshots, but recovery identity must stay stable."""
    return (thread_id(meta.get("id")), thread_id(meta.get("session_id", meta.get("id"))),
            meta.get("history_mode", "legacy"), meta.get("history_base"),
            meta.get("parent_thread_id"), meta.get("forked_from_id"),
            meta.get("forked_from_ordinal_exclusive"), meta.get("attached_thread", False),
            meta.get("subagent_history_start_ordinal"), meta.get("context_window"),
            meta.get("multi_agent_version"))


class Rollout:
    def __init__(self, target, summary_digest):
        self.target = target
        self.summary_digest = summary_digest
        self.mode = None
        self.seen_meta = False
        self.meta = None
        self.meta_lines = []
        self.recorded_windows = set()
        self.lineage = False
        self.checkpoint_lines = []
        self.checkpoints = []
        self.full = Scale()
        self.tail = Scale()
        self.suffix_model_items = 0
        self.marker_line = None
        self.marker_count = 0
        self.flattened = []
        self.rollbacks = []
        self.legacy = []
        self.retained = []

    def consume(self, record, line, diagnostics):
        kind, payload = record.get("type"), record.get("payload")
        if kind == "compacted":
            self.checkpoint_lines.append(line)
        if (not isinstance(kind, str) or kind not in ROLLOUT_TYPES or not isinstance(payload, dict)
                or not isinstance(record.get("timestamp"), str)
                or (record.get("ordinal") is not None and not uint(record["ordinal"]))):
            diagnostics.add("UNSUPPORTED_ROLLOUT_ENVELOPE", line, blocking=True)
            return
        if kind != "session_meta" and not self.seen_meta:
            diagnostics.add("ROLLOUT_BEFORE_SESSION_META", line, blocking=True)
        if kind == "session_meta":
            if thread_id(payload.get("id")) != self.target:
                diagnostics.add("ROLLOUT_THREAD_ID_MISMATCH", line, blocking=True)
            self.meta_lines.append(line)
            if self.seen_meta:
                if recovery_identity(payload) != recovery_identity(self.meta):
                    diagnostics.add("SESSION_META_RECOVERY_CONFLICT", line, blocking=True)
                elif self.mode != "legacy":
                    diagnostics.add("PAGINATED_SESSION_META_BOUNDARY_UNVERIFIED", line, blocking=True)
                return
            self.meta = payload
            self.seen_meta = True
            self.mode = payload.get("history_mode", "legacy")
            if self.mode not in ("legacy", "paginated"):
                self.mode = None
                diagnostics.add("UNKNOWN_HISTORY_MODE", line, blocking=True)
            self.lineage = payload.get("history_base") is not None
        elif kind == "compacted":
            try:
                fields, base = checkpoint(payload, line)
            except (ValueError, RecursionError, OverflowError):
                diagnostics.add("UNSAFE_COMPACTED_PAYLOAD", line, blocking=True)
                return
            if not fields["has_replacement_history"]:
                self.legacy.append(line)
            self.checkpoints.append((fields, base, self.full))
            self.tail = Scale()
        elif kind == "response_item":
            metadata = record.get("metadata")
            try:
                if metadata is not None and not isinstance(metadata, dict):
                    raise ValueError("invalid envelope metadata")
                amount = item_scale(payload, metadata=metadata)
            except (ValueError, RecursionError, OverflowError):
                diagnostics.add("UNSAFE_RESPONSE_ITEM", line, blocking=True)
                return
            self.full += amount
            self.tail += amount
            if self.marker_line is not None:
                self.suffix_model_items += amount.items
            if (self.summary_digest and payload["type"] == "message" and payload["role"] == "user"
                    and len(payload["content"]) == 1):
                part = payload["content"][0]
                if (part["type"] == "input_text" and
                        hashlib.sha256(part["text"].encode()).digest() == self.summary_digest):
                    self.flattened.append(line)
        elif kind == "event_msg":
            if not isinstance(payload.get("type"), str):
                diagnostics.add("UNSAFE_ROLLOUT_EVENT", line, blocking=True)
            elif payload["type"] == "thread_rolled_back":
                self.rollbacks.append(line)
            elif payload["type"] in ("turn_started", "task_started"):
                window = payload.get("model_context_window")
                if uint(window) and window:
                    self.recorded_windows.add(window)
            elif payload["type"] == "agent_message" and payload.get("message") == MARKER:
                self.marker_count += 1
                self.marker_line = line
                self.suffix_model_items = 0
        elif kind == "inter_agent_communication":
            self.full += Scale(1, json_size(payload), 1)
            self.tail += Scale(1, json_size(payload), 1)
            if self.marker_line is not None:
                self.suffix_model_items += 1
        elif kind == "retained_context":
            self.retained.append(line)

    def output(self, diagnostics):
        if not self.seen_meta:
            diagnostics.add("MISSING_SESSION_META", blocking=True)
        if self.lineage:
            diagnostics.add("PAGINATED_LINEAGE_NOT_REPLAYED", blocking=True)
        latest, base, prefix = self.checkpoints[-1] if self.checkpoints else (None, Scale(), Scale())
        fields = latest.copy() if latest and latest["line"] == self.checkpoint_lines[-1] else None
        usable_gate = None
        missing = []
        if fields and self.mode:
            if not fields["has_replacement_history"]:
                missing.append("replacement_history")
            if fields["window_number"] is None:
                missing.append("window_number")
            if self.mode == "legacy" and not fields["has_resume_metadata"]:
                missing.append("resume_metadata")
            usable_gate = not missing
            fields["storage_can_bound_read"] = (self.mode == "paginated" and fields["has_replacement_history"]
                                                and fields["window_number"] is not None)
            fields["core_can_bound_replay"] = usable_gate
            fields["missing_core_fields"] = missing
        # Entire rollouts may be loaded while replacement history is still replayed. With no rollback,
        # the newest replacement-history compaction survives the core reverse segment pass.
        selected = next(((cp, amount, before) for cp, amount, before in reversed(self.checkpoints)
                         if cp["has_replacement_history"]), None)
        selected_line = selected[0]["line"] if selected else 0
        if self.rollbacks and (not usable_gate or any(line > selected_line for line in self.rollbacks)):
            diagnostics.add("ROLLBACK_REPLAY_NOT_SUPPORTED", self.rollbacks[-1], blocking=True)
        if self.legacy and self.legacy[-1] > selected_line:
            diagnostics.add("LEGACY_SUMMARY_REBUILD_NOT_SUPPORTED", self.legacy[-1])
        if self.retained and self.retained[-1] > selected_line:
            diagnostics.add("RETAINED_CONTEXT_RUNTIME_PROJECTION_UNVERIFIED", self.retained[-1])
        effective = None
        after = None
        if not diagnostics.blockers and not (self.legacy and self.legacy[-1] > selected_line):
            if selected:
                after = self.tail  # no later CompactedItem can exist here
                effective = selected[1] + after
            else:
                effective = self.full
        return {"history_mode": self.mode, "compacted_count": len(self.checkpoint_lines),
                "session_meta_lines": self.meta_lines,
                "recorded_context_windows_tokens": sorted(self.recorded_windows),
                "decoded_compacted_count": len(self.checkpoints), "compacted_lines": self.checkpoint_lines,
                "latest_checkpoint": fields,
                "replayed_replacement_line": selected_line if selected and effective is not None else None,
                "before_latest_checkpoint": prefix.output() if fields else None,
                "all_response_history_without_checkpoints": self.full.output(),
                "suffix_after_latest_checkpoint": self.tail.output() if fields else None,
                "effective_replayed_history": effective.output() if effective is not None else None,
                "import_marker_line": self.marker_line, "import_marker_count": self.marker_count,
                "model_items_after_last_import_marker": self.suffix_model_items if self.marker_line else None,
                "source_summary_as_ordinary_user_lines_before_last_marker": [line for line in self.flattened
                                                                              if line < (self.marker_line or 0)],
                "rollbacks": len(self.rollbacks)}, effective, after, base


@dataclass(frozen=True)
class Mapping:
    path: Path
    thread: str
    sha256: str


class Inputs:
    def __init__(self, args):
        self.args = args
        self.codex = args.codex_home.resolve()
        self.claude = args.claude_home.resolve()
        self.projects = self.claude / "projects"
        self.ledger_path = self.codex / LEDGER
        self.ledger_snapshot = None
        self.mappings = []
        self.by_thread = defaultdict(list)
        self.by_path = Counter()
        self.malformed_entries = 0
        self.unscoped_entries = 0
        self.candidates = {}
        self.unreadable_headers = 0
        self.skipped_other_compressed = 0
        self.scan_error = False

    def ledger(self):
        try:
            with read_only(self.codex, self.ledger_path) as stream:
                raw = stream.read(MAX_LEDGER + 1)
            if len(raw) > MAX_LEDGER:
                raise Unavailable("IMPORT_LEDGER_TOO_LARGE")
            data = parse(raw)
            if not isinstance(data.get("records"), list):
                raise ValueError("invalid records")
        except FileNotFoundError as exc:
            raise Unavailable("IMPORT_LEDGER_NOT_FOUND") from exc
        except (ValueError, RecursionError, OverflowError) as exc:
            raise Unavailable("INVALID_IMPORT_LEDGER") from exc
        self.ledger_snapshot = hashlib.sha256(raw).hexdigest()
        for entry in data["records"]:
            if not isinstance(entry, dict):
                self.malformed_entries += 1
                continue
            tid = thread_id(entry.get("imported_thread_id"))
            digest, source = entry.get("content_sha256"), entry.get("source_path")
            if not tid or not isinstance(digest, str) or not HEX_SHA.fullmatch(digest) or not isinstance(source, str) or "\0" in source:
                self.malformed_entries += 1
                continue
            path = Path(source)
            if not path.is_absolute() or ".." in path.parts or path.suffix != ".jsonl" or not path.is_relative_to(self.projects):
                self.unscoped_entries += 1
            row = Mapping(path, tid, digest)
            self.mappings.append(row)
            self.by_thread[tid].append(row)
            self.by_path[path] += 1

    def is_claude(self, path):
        return path.is_absolute() and ".." not in path.parts and path.suffix == ".jsonl" and path.is_relative_to(self.projects)

    def discover(self, wanted):
        """Scan local headers once; matching the filename alone can select a reverted file."""
        if not wanted:
            return

        def error(_):
            self.scan_error = True

        self.candidates = {tid: [] for tid in wanted}
        for root in (self.codex / "sessions", self.codex / "archived_sessions"):
            try:
                if root.is_symlink() or (root.exists() and not root.is_dir()):
                    self.scan_error = True
                    continue
                if not root.exists():
                    continue
                for directory, dirs, files in os.walk(root, onerror=error, followlinks=False):
                    # Session trees normally contain only date directories and rollout files.
                    if any((Path(directory) / name).is_symlink() for name in dirs):
                        self.scan_error = True
                    dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
                    for filename in files:
                        match = ROLLOUT_NAME.fullmatch(filename)
                        if not match:
                            continue
                        filename_id = thread_id(match[1])
                        path = Path(directory) / filename
                        if filename.endswith(".zst"):
                            if filename_id in wanted:
                                self.candidates[filename_id].append((path, "compressed"))
                            else:
                                self.skipped_other_compressed += 1
                            continue
                        try:
                            with read_only(self.codex, path) as stream:
                                raw = stream.readline(self.args.max_line_bytes + 1)
                                if len(raw) > self.args.max_line_bytes:
                                    raise ValueError("header exceeds limit")
                                record = parse(raw)
                                if not isinstance(record.get("timestamp"), str):
                                    raise ValueError("invalid timestamp")
                                tid = thread_id(session_meta(record).get("id"))
                                if not tid:
                                    raise ValueError("invalid session metadata")
                        except (Unavailable, OSError, ValueError, RecursionError, OverflowError):
                            self.unreadable_headers += 1
                            if filename_id in wanted:
                                self.candidates[filename_id].append((path, "invalid_header"))
                            continue
                        if tid in wanted:
                            self.candidates[tid].append((path, "verified_header"))
                        if filename_id in wanted and filename_id != tid:
                            self.candidates[filename_id].append((path, "id_mismatch"))
            except OSError:
                self.scan_error = True

    def resolve(self, tid):
        options = self.by_thread.get(tid, [])
        if not options:
            raise Unavailable("NO_IMPORT_MAPPING")
        if len(options) != 1 or self.by_path[options[0].path] != 1:
            raise Unavailable("AMBIGUOUS_IMPORT_MAPPING")
        if self.malformed_entries:
            raise Unavailable("IMPORT_LEDGER_HAS_UNSAFE_ENTRIES")
        mapping = options[0]
        if not self.is_claude(mapping.path):
            raise Unavailable("MAPPED_SOURCE_OUTSIDE_CLAUDE_PROJECTS")
        candidates = self.candidates.get(tid, [])
        if self.scan_error:
            raise Unavailable("ROLLOUT_DIRECTORY_SCAN_INCOMPLETE")
        if len(candidates) > 1:
            raise Unavailable("AMBIGUOUS_ROLLOUT")
        if not candidates:
            raise Unavailable("ROLLOUT_NOT_FOUND")
        path, status = candidates[0]
        if status != "verified_header":
            raise Unavailable({"compressed": "COMPRESSED_ROLLOUT_UNSUPPORTED",
                               "id_mismatch": "ROLLOUT_THREAD_ID_MISMATCH",
                               "invalid_header": "UNSAFE_ROLLOUT_HEADER"}[status])
        return mapping, path

    def diagnostics(self):
        return {"unsafe_ledger_entries": self.malformed_entries,
                "ledger_entries_outside_claude_projects": self.unscoped_entries,
                "unreadable_rollout_headers": self.unreadable_headers,
                "other_compressed_rollouts_not_indexed": self.skipped_other_compressed,
                "rollout_directory_scan_incomplete": self.scan_error}


def classify(result, effective, after, base, args):
    source, codex = result["claude"], result["codex"]
    latest = codex["latest_checkpoint"]
    if (result["association"]["source_sha256"] != "match"
            or result["claude_diagnostics"]["blocking_count"]
            or result["codex_diagnostics"]["blocking_count"]):
        return "UNKNOWN", "low", ["SOURCE_HASH_OR_PARSE_INTEGRITY_UNVERIFIED"]
    if latest and latest["core_can_bound_replay"] is False:
        evidence = ["LATEST_CHECKPOINT_CANNOT_BOUND_CORE_REPLAY"]
        if codex["replayed_replacement_line"] is not None:
            evidence.append("FULL_REPLAY_STILL_USES_REPLACEMENT_HISTORY")
        return "CHECKPOINT_INCOMPATIBLE", "high", evidence
    if latest and latest["core_can_bound_replay"]:
        evidence = ["LATEST_CHECKPOINT_MEETS_BOUNDED_REPLAY_FIELD_GATE"]
        if effective is None or after is None:
            return "UNKNOWN", "low", evidence + ["EFFECTIVE_MODEL_HISTORY_UNVERIFIED"]
        if not base.items:
            return "UNKNOWN", "low", evidence + ["EMPTY_REPLACEMENT_CONTENT_UNVERIFIED"]
        if latest["opaque_model_compatibility_unverified"]:
            return "UNKNOWN", "low", evidence + ["OPAQUE_PRODUCER_COMPATIBILITY_UNVERIFIED"]
        if effective.approximate_items or result["codex_diagnostics"]["counts"].get("RETAINED_CONTEXT_RUNTIME_PROJECTION_UNVERIFIED"):
            return "UNKNOWN", "low", evidence + ["RUNTIME_PROMPT_PROJECTION_UNVERIFIED"]
        budget_tests = [(effective.items, base.items, args.item_budget, "ITEM_BUDGET"),
                        ((effective.bytes + 3) // 4, (base.bytes + 3) // 4, args.token_budget,
                         "ESTIMATED_TOKEN_BUDGET")]
        exceeded = [(prefix, limit, label) for total, prefix, limit, label in budget_tests
                    if limit is not None and total > limit]
        if any(prefix > limit for prefix, limit, _ in exceeded):
            return "UNKNOWN", "low", evidence + ["REPLACEMENT_ALONE_EXCEEDS_BUDGET"]
        if exceeded:
            return "TAIL_OVERFLOW", "medium", evidence + [
                "SUFFIX_PUSHES_HISTORY_OVER_" + label for _, _, label in exceeded]
        if args.token_budget is None:
            return "UNKNOWN", "low", evidence + ["SUPPLY_EFFECTIVE_HISTORY_TOKEN_BUDGET"]
        return "CHECKPOINT_VALID_OTHER_CAUSE", "medium", evidence + ["ANALYZED_HISTORY_WITHIN_SUPPLIED_BUDGET"]
    if source["latest_markers_form_valid_pair"] and not codex["compacted_count"]:
        evidence = ["VALID_CLAUDE_PAIR_NO_CODEX_COMPACTED_ITEM"]
        if codex["source_summary_as_ordinary_user_lines_before_last_marker"]:
            evidence.append("CLAUDE_SUMMARY_MATCHES_IMPORTED_ORDINARY_USER_ITEM")
        if source["paired_checkpoints"][-1]["summary_skipped_by_importer_as_meta"]:
            evidence.append("CLAUDE_SUMMARY_MARKED_META_IMPORTER_SKIPS_IT")
        confidence = "high" if source["paired_checkpoints"][-1]["parent_verified"] else "medium"
        return "SOURCE_CHECKPOINT_LOST", confidence, evidence
    if not source["boundary_count"] and not source["summary_count"]:
        return "NO_SOURCE_CHECKPOINT", "high", ["NO_CLAUDE_COMPACTION_MARKERS_IN_HASH_VERIFIED_SOURCE"]
    return "UNKNOWN", "low", ["CLAUDE_COMPACTION_MARKERS_UNPAIRED_OR_INVALID"]


def inspect(inputs, tid):
    args = inputs.args
    result = {"schema_version": 1, "codex_main_commit": UPSTREAM,
              "token_estimator": "ceil(model_visible_utf8_bytes / 4); no provider tokenizer or injected context",
              "checkpoint_validation_scope": "local wire checks and replay field gate; no provider validation",
              "budgets": {"effective_history_tokens": args.token_budget, "effective_history_items": args.item_budget},
              "association": {"ledger_mapping": "unverified", "rollout_header": "unverified",
                              "source_sha256": "unverified", "runtime_selected_rollout": "not_checked"},
              "discovery": inputs.diagnostics()}
    try:
        mapping, rollout_path = inputs.resolve(tid)
        result["association"].update(ledger_mapping="unique", rollout_header="thread_id_match")
        input_paths = (mapping.path, rollout_path)
        before = [signature(path.stat(follow_symlinks=False)) for path in input_paths]
        source, source_diags = Source(), Diagnostics()
        source_info, digest = scan_jsonl(inputs.claude, mapping.path, source.consume, args, source_diags)
        result["association"]["source_sha256"] = "match" if digest == mapping.sha256 else "mismatch"
        result["claude"] = {**source_info, **source.output(source_diags)}
        result["claude_diagnostics"] = source_diags.output()
        rollout, rollout_diags = Rollout(tid, source.latest_digest), Diagnostics()
        # Duplicate content is compared locally. No digest, excerpt or path enters the result.
        rollout_info, _ = scan_jsonl(inputs.codex, rollout_path, rollout.consume, args, rollout_diags)
        codex, effective, after, base = rollout.output(rollout_diags)
        result["codex"] = {**rollout_info, **codex}
        result["codex_diagnostics"] = rollout_diags.output()
        if hash_only(inputs.codex, inputs.ledger_path) != inputs.ledger_snapshot:
            raise Unavailable("IMPORT_LEDGER_CHANGED_DURING_INSPECTION")
        if before != [signature(path.stat(follow_symlinks=False)) for path in input_paths]:
            raise Unavailable("FILE_CHANGED_DURING_INSPECTION")
        status, confidence, evidence = classify(result, effective, after, base, args)
        if result["association"]["source_sha256"] == "mismatch":
            evidence.append("MAPPED_SOURCE_CONTENT_SHA256_MISMATCH")
        if result["discovery"]["unreadable_rollout_headers"] or result["discovery"]["other_compressed_rollouts_not_indexed"]:
            if confidence == "high":
                confidence = "medium"
            evidence.append("ADDITIONAL_ROLLOUT_HEADERS_NOT_VERIFIED")
    except Unavailable as exc:
        status, confidence, evidence = "UNKNOWN", "low", [str(exc)]
    except OSError:
        status, confidence, evidence = "UNKNOWN", "low", ["INPUT_UNREADABLE_OR_CHANGED"]
    result.update(diagnosis=status, confidence=confidence, evidence=evidence)
    return result


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def cli(argv=None):
    parser = argparse.ArgumentParser(prog="session-doctor", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--codex-home", type=Path, help="Codex home or a local snapshot")
    parser.add_argument("--claude-home", type=Path, help="Claude home containing projects/")
    parser.add_argument("--state-dir", type=Path, help="private local recovery registry, candidates and validator cache")
    parser.add_argument("--token-budget", type=positive_int, help="tokens available to persisted history after overhead")
    parser.add_argument("--item-budget", type=positive_int, help="item slots available to persisted history")
    parser.add_argument("--large-line-bytes", type=positive_int, default=256 * 1024)
    parser.add_argument("--max-line-bytes", type=positive_int, default=16 * 1024 * 1024)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("list", help="list mapped Claude import candidates (IDs are local identifiers)")
    for command in ("inspect", "report"):
        sub = subs.add_parser(command, help="diagnose one thread" if command == "inspect" else "print redacted JSON to stdout")
        sub.add_argument("thread_id", type=thread_id)
    subs.add_parser("plan", help="print a redacted checkpoint recovery plan").add_argument("thread_id", type=thread_id)
    build = subs.add_parser("build-candidate", help="write an isolated rollout candidate")
    build.add_argument("thread_id", type=thread_id)
    build.add_argument("--output", type=Path, required=True)
    validate = subs.add_parser("validate-candidate", help="verify an isolated candidate")
    validate.add_argument("candidate", type=Path)
    validate.add_argument("--native-test-bin", type=Path)
    rebase = subs.add_parser("rebase-candidate", help="verify and copy an isolated candidate with a new startup estimate")
    rebase.add_argument("candidate", type=Path)
    rebase.add_argument("--output", type=Path, required=True)
    rebase.add_argument("--native-test-bin", type=Path)
    register = subs.add_parser("register-candidate", help="preview or register an isolated candidate as a new local thread")
    register.add_argument("candidate", type=Path)
    register.add_argument("--native-test-bin", type=Path)
    register.add_argument("--apply", action="store_true", help="register a new thread through Codex App Server")
    continuation = subs.add_parser("test-continuation", help="preview or send one continuation test on a fresh recovery thread")
    continuation.add_argument("candidate", type=Path)
    continuation.add_argument("thread_id", type=thread_id)
    continuation.add_argument("--native-test-bin", type=Path)
    continuation.add_argument("--send", action="store_true", help="send one real model turn to the new thread")
    workflow = subs.add_parser("recover", help="preview or explicitly perform one recovery stage")
    workflow.add_argument("thread_id", type=thread_id)
    workflow.add_argument("--native-test-bin", type=Path)
    stages = workflow.add_mutually_exclusive_group()
    stages.add_argument("--build", action="store_true")
    stages.add_argument("--register", action="store_true")
    stages.add_argument("--test", action="store_true")
    verifier = subs.add_parser("validator-build", help="build and cache the pinned native replay verifier")
    verifier.add_argument("--codex-source", type=Path, help="use the pinned commit from an existing Git checkout")
    verifier.add_argument("--offline", action="store_true", help="use existing Cargo downloads only")
    args = parser.parse_args(argv)
    candidate_only = args.command in ("validate-candidate", "rebase-candidate",
                                      "register-candidate", "test-continuation", "recover", "validator-build")
    if not candidate_only:
        args.codex_home = args.codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        args.claude_home = args.claude_home or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if hasattr(args, "thread_id") and args.thread_id is None:
        parser.error("thread_id must be a UUID")
    if args.large_line_bytes > args.max_line_bytes or args.max_line_bytes > 64 * 1024 * 1024:
        parser.error("require large-line-bytes <= max-line-bytes <= 64 MiB")
    try:
        if not candidate_only:
            inputs = Inputs(args)
            inputs.ledger()
        if args.command == "validate-candidate":
            from checkpoint_recovery import validate as validate_recovery
            from native_verifier import find_verifier
            result = validate_recovery(args.candidate, find_verifier(args))
        elif args.command == "rebase-candidate":
            from checkpoint_recovery import rebase_candidate
            from native_verifier import find_verifier
            homes = [home.expanduser() for home in (args.codex_home, args.claude_home) if home is not None]
            result = rebase_candidate(args.candidate, args.output, find_verifier(args, required=True), *homes)
        elif args.command == "register-candidate":
            from live_thread import register_candidate
            result = register_candidate(args)
        elif args.command == "test-continuation":
            from live_thread import test_continuation
            result = test_continuation(args)
        elif args.command == "recover":
            from recovery_workflow import recover
            result = recover(args)
        elif args.command == "validator-build":
            from live_thread import local_home
            from native_verifier import build_verifier
            from recovery_state import check_state_root, state_directory
            check_state_root(state_directory(args), local_home(args.codex_home, "CODEX_HOME", ".codex"),
                             local_home(args.claude_home, "CLAUDE_CONFIG_DIR", ".claude"))
            result = build_verifier(args)
        elif args.command == "list":
            tids = {row.thread for row in inputs.mappings if inputs.is_claude(row.path)}
            inputs.discover(tids)
            sessions = []
            for tid in sorted(tids):
                try:
                    inputs.resolve(tid)
                    state = "MAPPED_HEADER_VERIFIED_SOURCE_HASH_NOT_CHECKED"
                except Unavailable as exc:
                    state = str(exc)
                sessions.append({"thread_id": tid, "association": state})
            result = {"codex_main_commit": UPSTREAM, "sessions": sessions, "discovery": inputs.diagnostics()}
        elif args.command in ("plan", "build-candidate"):
            from checkpoint_recovery import analyze, build as build_recovery
            if args.thread_id in inputs.by_thread:
                inputs.discover({args.thread_id})
            recovery = analyze(inputs, args.thread_id)
            result = (recovery.plan if args.command == "plan" else
                      build_recovery(inputs, recovery, args.output))
        else:
            if args.thread_id in inputs.by_thread:
                inputs.discover({args.thread_id})
            result = inspect(inputs, args.thread_id)
    except (OSError, RuntimeError, Unavailable, ValueError) as exc:
        result = {"codex_main_commit": UPSTREAM, "diagnosis": "UNKNOWN", "confidence": "low",
                  "evidence": [str(exc) if isinstance(exc, Unavailable) else "INPUT_UNREADABLE_OR_CHANGED"]}
    if args.command == "inspect":
        result["thread_id"] = args.thread_id
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    failed_statuses = {"BLOCKED", "NATIVE_VALIDATION_REQUIRED", "REGISTRATION_UNVERIFIED",
                       "REGISTRATION_RESULT_UNKNOWN", "REGISTERED_TITLE_UNVERIFIED",
                       "CONTINUATION_UNVERIFIED", "CONTINUATION_RESULT_UNKNOWN",
                       "REGISTRATION_PENDING", "CONTINUATION_PENDING"}
    return 2 if result.get("diagnosis") == "UNKNOWN" or result.get("status") in failed_statuses else 0


def main():
    sys.dont_write_bytecode = True
    return cli()


if __name__ == "__main__":
    sys.modules["session_doctor"] = sys.modules[__name__]
    sys.exit(main())
