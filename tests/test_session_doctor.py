import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from session_doctor import Diagnostics, Source, Unavailable

SCRIPT = Path(__file__).resolve().parents[1] / "session_doctor.py"
TID = "01900000-0000-7000-8000-000000000123"
OTHER = "01900000-0000-7000-8000-000000000456"
SECRET = "FAKE_SECRET_DO_NOT_INCLUDE_alpha_3571"
TIME = "2026-01-01T00:00:00.000Z"


class LineageLimitsTests(unittest.TestCase):
    def test_rejects_oversized_identifiers_in_source_metadata(self):
        source, diagnostics = Source(), Diagnostics()
        with self.assertRaisesRegex(Unavailable, "CLAUDE_LINEAGE_IDENTIFIER_TOO_LONG"):
            source.consume({"type": "user", "uuid": "x" * 257}, 1, diagnostics)

    def test_bounds_cumulative_lineage_metadata(self):
        source, diagnostics = Source(), Diagnostics()
        with patch("session_doctor.SOURCE_LINEAGE_BUDGET", 250):
            source.consume({"type": "user", "uuid": "root"}, 1, diagnostics)
            with self.assertRaisesRegex(Unavailable, "CLAUDE_LINEAGE_METADATA_BUDGET_EXCEEDED"):
                source.consume({"type": "assistant", "uuid": "next", "parentUuid": "root"}, 2, diagnostics)

    def test_bounds_attachment_chain(self):
        source, diagnostics = Source(), Diagnostics()
        source.consume({"type": "system", "subtype": "compact_boundary", "uuid": "boundary"}, 1, diagnostics)
        source.consume({"type": "attachment", "uuid": "first", "parentUuid": "boundary"}, 2, diagnostics)
        with patch("session_doctor.SOURCE_ATTACHMENT_LIMIT", 1):
            with self.assertRaisesRegex(Unavailable, "CLAUDE_ATTACHMENT_CHAIN_LIMIT"):
                source.consume({"type": "attachment", "uuid": "second", "parentUuid": "first"}, 3, diagnostics)


def write_jsonl(path, records):
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records))


def claude(role, text, **fields):
    return {"type": role, "message": {"role": role, "content": text},
            "cwd": "/synthetic/project", "timestamp": TIME, **fields}


def pair(n=1, **fields):
    identity = f"boundary-{n}"
    return [{"type": "system", "subtype": "compact_boundary", "uuid": identity},
            claude("user", f"Synthetic summary {n}. {SECRET}", isCompactSummary=True,
                   parentUuid=identity, **fields)]


def item(kind, payload):
    return {"timestamp": TIME, "type": kind, "payload": payload}


def message(text, role="user"):
    return {"type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}


def response(text, role="user"):
    return item("response_item", message(text, role))


def event(kind, **fields):
    return item("event_msg", {"type": kind, **fields})


MARKER = event("agent_message", message="<EXTERNAL SESSION IMPORTED>")


def cp(**overrides):
    return item("compacted", {"message": "fixture", "replacement_history": [message("new base")],
                              "window_number": 1, "resume_metadata": {}, **overrides})


class Fixture:
    def __init__(self, root):
        self.codex = root / "codex"
        self.claude = root / "claude"
        self.source = self.claude / "projects" / "fixture" / "synthetic.jsonl"
        self.rollout = self.codex / "sessions" / "2026" / "01" / "01" / f"rollout-2026-01-01T00-00-00-{TID}.jsonl"
        self.ledger = self.codex / "external_agent_session_imports.json"
        for path in (self.source, self.rollout):
            path.parent.mkdir(parents=True)

    def create(self, source=(), rollout=(), mode="paginated", **meta):
        write_jsonl(self.source, [claude("user", SECRET), claude("assistant", "initial answer"), *source])
        header = {"id": TID, "session_id": TID, "cwd": "/synthetic/project", "timestamp": TIME,
                  "originator": "synthetic", "cli_version": "0.0.0", **meta}
        if mode is not None:
            header["history_mode"] = mode
        write_jsonl(self.rollout, [item("session_meta", header), response(SECRET), response("initial answer", "assistant"), *rollout])
        self.refresh_mapping()

    def refresh_mapping(self, rows=()):
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.ledger.write_text(json.dumps({"records": [
            {"source_path": str(self.source), "content_sha256": digest, "imported_thread_id": TID,
             "imported_at": 1767225600, "title": SECRET}, *rows], "detected_connector_records": []}))

    def run(self, command="inspect", target=TID, **kwargs):
        flags = [value for key, arg in kwargs.items() for value in ("--" + key.replace("_", "-"), str(arg))]
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "--codex-home", str(self.codex),
                               "--claude-home", str(self.claude), *flags, command,
                               *([] if command == "list" else [target])],
                              text=True, capture_output=True, check=False)


class SessionDoctorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="session-doctor-test-")
        self.addCleanup(directory.cleanup)
        self.fixture = Fixture(Path(directory.name).resolve())

    def run_cli(self, diagnosis=None, command="inspect", **args):
        run = self.fixture.run(command, **args)
        self.assertEqual(run.returncode, 2 if diagnosis == "UNKNOWN" else 0, run.stderr)
        self.assertEqual(run.stderr, "")
        data = json.loads(run.stdout)
        if diagnosis:
            self.assertEqual(data["diagnosis"], diagnosis, data)
            self.assertTrue(data["evidence"])
            self.assertIn(data["confidence"], {"high", "medium", "low"})
        return data

    def test_no_compaction(self):
        self.fixture.create(rollout=[MARKER], mode=None)
        result = self.run_cli("NO_SOURCE_CHECKPOINT")
        self.assertEqual(result["codex"]["history_mode"], "legacy")
        self.assertEqual(result["codex"]["effective_replayed_history"]["items"], 2)
        self.assertEqual(self.run_cli(command="list")["sessions"], [
            {"thread_id": TID, "association": "MAPPED_HEADER_VERIFIED_SOURCE_HASH_NOT_CHECKED"}])

    def test_legacy_accepts_consistent_session_metadata_update(self):
        self.fixture.create(pair(), [MARKER, item("session_meta", {
            "id": TID, "session_id": TID, "history_mode": "legacy", "cwd": "/new/local/path"
        })], mode="legacy")
        self.assertEqual(self.run_cli("SOURCE_CHECKPOINT_LOST")["codex"]["session_meta_lines"], [1, 5])

    def test_metadata_with_different_lineage_blocks_diagnosis(self):
        self.fixture.create(pair(), [MARKER, item("session_meta", {
            "id": TID, "history_mode": "legacy", "history_base": {}
        })], mode="legacy")
        self.assertIn("SESSION_META_RECOVERY_CONFLICT",
                      self.run_cli("UNKNOWN")["codex_diagnostics"]["counts"])

    def test_attachment_chain_and_preserved_messages_in_logical_history(self):
        boundary, summary = pair()
        summary.update(uuid="summary", parentUuid="attachment")
        boundary["compactMetadata"] = {"preservedSegment": {
            "headUuid": "head", "tailUuid": "tail", "anchorUuid": "summary"
        }}
        self.fixture.create([
            claude("user", "preserve this", uuid="head"),
            claude("assistant", "and this", uuid="tail", parentUuid="head"),
            boundary, {"type": "attachment", "uuid": "attachment", "parentUuid": "boundary-1"},
            summary, claude("user", "new", uuid="new", parentUuid="tail"),
        ], [MARKER])
        result = self.run_cli("SOURCE_CHECKPOINT_LOST")["claude"]
        self.assertEqual(result["paired_checkpoints"][-1]["attachment_chain_records"], 1)
        self.assertEqual(result["latest_pair_summary_and_tail_projection"]["items"], 2)
        self.assertEqual(result["logical_history"]["preserved_source_lines"], [3, 4])
        self.assertEqual(result["logical_history"]["summary_preserved_and_logical_tail_projection"]["items"], 4)

    def test_paginated_checkpoint_without_resume_metadata(self):
        self.fixture.create(pair(), [cp(resume_metadata=None), response("new work"), MARKER])
        data = self.run_cli("CHECKPOINT_VALID_OTHER_CAUSE", token_budget=100)
        claude, codex = data["claude"], data["codex"]
        self.assertEqual(claude["paired_checkpoints"][0]["summary_line"], 4)
        self.assertEqual(claude["paired_checkpoints"][0]["before_boundary_import_projection"]["items"], 2)
        self.assertEqual(codex["compacted_lines"], [4])
        self.assertTrue(codex["latest_checkpoint"]["storage_can_bound_read"])
        self.assertTrue(codex["latest_checkpoint"]["core_can_bound_replay"])
        self.assertFalse(codex["latest_checkpoint"]["has_resume_metadata"])
        self.assertEqual(codex["effective_replayed_history"]["estimated_tokens"], 4)

    def test_multiple_checkpoints_use_newest(self):
        src = [*pair(1), claude("user", "between"), *pair(2), claude("user", "newest tail")]
        self.fixture.create(src, [cp(), response("between"), cp(window_number=2), response("last"), MARKER])
        result = self.run_cli("CHECKPOINT_VALID_OTHER_CAUSE", token_budget=100)
        self.assertEqual(result["claude"]["paired_checkpoint_count"], 2)
        self.assertEqual(result["claude"]["latest_pair_summary_and_tail_projection"]["items"], 2)
        self.assertEqual(result["codex"]["compacted_lines"], [4, 6])
        self.assertEqual(result["codex"]["replayed_replacement_line"], 6)
        self.assertEqual(result["codex"]["effective_replayed_history"]["items"], 2)

    def test_lost_checkpoint_and_flattened_summary(self):
        self.fixture.create(pair(), [response(pair()[1]["message"]["content"]), MARKER])
        data = self.run_cli("SOURCE_CHECKPOINT_LOST")
        self.assertIn("CLAUDE_SUMMARY_MATCHES_IMPORTED_ORDINARY_USER_ITEM", data["evidence"])
        self.assertEqual(data["codex"]["source_summary_as_ordinary_user_lines_before_last_marker"], [4])

    def test_summary_filtered_by_importer(self):
        self.fixture.create(pair(isMeta=True), [MARKER])
        data = self.run_cli("SOURCE_CHECKPOINT_LOST")
        self.assertIn("CLAUDE_SUMMARY_MARKED_META_IMPORTER_SKIPS_IT", data["evidence"])

    def test_missing_window_retains_full_replay_fallback(self):
        self.fixture.create(pair(), [cp(), response("old tail"), cp(window_number=None), response("new tail"), MARKER])
        data = self.run_cli("CHECKPOINT_INCOMPATIBLE")["codex"]
        self.assertEqual(data["latest_checkpoint"]["missing_core_fields"], ["window_number"])
        self.assertFalse(data["latest_checkpoint"]["storage_can_bound_read"])
        self.assertEqual(data["replayed_replacement_line"], 6)
        self.assertEqual(data["effective_replayed_history"]["items"], 2)

    def test_legacy_resume_metadata_required_for_bounding(self):
        self.fixture.create(pair(), [cp(resume_metadata=None), MARKER], mode="legacy")
        data = self.run_cli("CHECKPOINT_INCOMPATIBLE")["codex"]
        self.assertEqual(data["latest_checkpoint"]["missing_core_fields"], ["resume_metadata"])
        self.assertEqual(data["replayed_replacement_line"], 4)

    def test_legacy_with_resume_metadata_and_numeric_window_id(self):
        self.fixture.create(pair(), [cp(window_number=None, window_id=0,
                                        guardian_history=[message("guardian-only transcript")]), MARKER], mode="legacy")
        data = self.run_cli("CHECKPOINT_VALID_OTHER_CAUSE", token_budget=100)["codex"]
        self.assertTrue(data["latest_checkpoint"]["legacy_numeric_window_id"])
        self.assertTrue(data["latest_checkpoint"]["core_can_bound_replay"])
        self.assertFalse(data["latest_checkpoint"]["storage_can_bound_read"])
        self.assertEqual(data["effective_replayed_history"]["items"], 1)

    def test_newest_incomplete_does_not_allow_storage_stop_at_older_one(self):
        self.fixture.create(pair(), [cp(), cp(replacement_history=None), response("after")])
        data = self.run_cli("CHECKPOINT_INCOMPATIBLE")["codex"]
        self.assertEqual(data["compacted_lines"], [4, 5])
        self.assertFalse(data["latest_checkpoint"]["storage_can_bound_read"])
        self.assertIsNone(data["effective_replayed_history"])

    def test_valid_checkpoint_tail_over_token_and_item_budgets(self):
        self.fixture.create(pair(), [cp(), MARKER, response("z" * 4000), response("new turn")])
        data = self.run_cli("TAIL_OVERFLOW", token_budget=200, item_budget=2)
        self.assertEqual(data["codex"]["effective_replayed_history"]["items"], 3)
        self.assertIn("SUFFIX_PUSHES_HISTORY_OVER_ESTIMATED_TOKEN_BUDGET", data["evidence"])
        self.assertIn("SUFFIX_PUSHES_HISTORY_OVER_ITEM_BUDGET", data["evidence"])

    def test_replacement_alone_over_budget_is_not_tail_overflow(self):
        self.fixture.create(pair(), [cp(replacement_history=[message("x" * 100)]), MARKER])
        result = self.run_cli("UNKNOWN", token_budget=4)
        self.assertIn("REPLACEMENT_ALONE_EXCEEDS_BUDGET", result["evidence"])

    def test_dirty_jsonl_truncation_and_large_payload_drain(self):
        self.fixture.create(pair(), [response("x" * 5000), response("still read"), MARKER])
        with self.fixture.rollout.open("ab") as file:
            file.write(b'{"bad"\n{"partial"')
        result = self.run_cli("UNKNOWN", large_line_bytes=384, max_line_bytes=1024)
        counts = result["codex_diagnostics"]["counts"]
        self.assertEqual(counts, {"LARGE_JSONL_RECORD": 1, "JSONL_RECORD_OVER_PARSE_LIMIT": 1,
                                  "MALFORMED_JSONL": 1, "POSSIBLY_TRUNCATED_JSONL": 1})
        self.assertEqual(result["codex"]["import_marker_line"], 6)

    def test_large_but_parseable_record(self):
        self.fixture.create(pair(), [cp(), response("x" * 5000), MARKER])
        result = self.run_cli("TAIL_OVERFLOW", large_line_bytes=1024, token_budget=100)
        self.assertEqual(result["codex_diagnostics"]["counts"], {"LARGE_JSONL_RECORD": 1})

    def test_source_sha_drift_after_appending(self):
        self.fixture.create(pair(), [MARKER])
        with self.fixture.source.open("ab") as file:
            file.write(json.dumps(claude("user", "append")).encode() + b"\n")
        result = self.run_cli("UNKNOWN")
        self.assertEqual(result["association"]["source_sha256"], "mismatch")
        self.assertIn("MAPPED_SOURCE_CONTENT_SHA256_MISMATCH", result["evidence"])

    def test_codex_appended_conversation_preserved_and_counted(self):
        self.fixture.create(pair(), [response(pair()[1]["message"]["content"]), MARKER,
                                     event("task_started", turn_id="later-turn"),
                                     response("Codex follow-up"), response("Codex answer", "assistant")])
        result = self.run_cli("SOURCE_CHECKPOINT_LOST")["codex"]
        self.assertEqual(result["model_items_after_last_import_marker"], 2)
        self.assertEqual(result["effective_replayed_history"]["items"], 5)

    def test_ambiguous_ledger_and_reverted_rollout(self):
        self.fixture.create(pair(), [MARKER])
        newer = self.fixture.rollout.with_name(self.fixture.rollout.name.replace(TID, OTHER))
        newer.write_bytes(self.fixture.rollout.read_bytes())
        self.assertEqual(self.run_cli("UNKNOWN")["evidence"], ["AMBIGUOUS_ROLLOUT"])
        self.fixture.refresh_mapping([{"source_path": str(self.fixture.source), "content_sha256": "0" * 64,
                                       "imported_thread_id": TID, "imported_at": 0}])
        self.assertEqual(self.run_cli("UNKNOWN")["evidence"], ["AMBIGUOUS_IMPORT_MAPPING"])

    def test_session_meta_id_mismatches_filename(self):
        self.fixture.create(pair(), [MARKER], id=OTHER)
        self.assertEqual(self.run_cli("UNKNOWN")["evidence"], ["ROLLOUT_THREAD_ID_MISMATCH"])

    def test_valid_json_without_final_newline(self):
        self.fixture.create(rollout=[MARKER])
        self.fixture.rollout.write_bytes(self.fixture.rollout.read_bytes().rstrip(b"\n"))
        result = self.run_cli("NO_SOURCE_CHECKPOINT")
        self.assertTrue(result["codex"]["final_line_without_newline"])

    def test_empty_replacement_passes_field_gate_without_proving_safe_history(self):
        self.fixture.create(pair(), [cp(replacement_history=[]), response("tail")])
        result = self.run_cli("UNKNOWN", token_budget=100)
        self.assertTrue(result["codex"]["latest_checkpoint"]["core_can_bound_replay"])
        self.assertEqual(result["codex"]["effective_replayed_history"]["items"], 1)
        self.assertIn("EMPTY_REPLACEMENT_CONTENT_UNVERIFIED", result["evidence"])

    def test_opaque_checkpoint_compatibility_is_not_proven_by_gate(self):
        self.fixture.create(pair(), [cp(replacement_history=[
            {"type": "compaction", "encrypted_content": "opaque-placeholder"}
        ]), response("tail")])
        result = self.run_cli("UNKNOWN", token_budget=100)
        self.assertTrue(result["codex"]["latest_checkpoint"]["core_can_bound_replay"])
        self.assertIn("OPAQUE_PRODUCER_COMPATIBILITY_UNVERIFIED", result["evidence"])

    def test_malformed_tool_payload_does_not_leak_traceback(self):
        self.fixture.create(pair(), [cp(), item("response_item", {
            "type": "function_call", "call_id": "synthetic", "name": "synthetic",
            "arguments": SECRET, "namespace": []
        })])
        data = self.run_cli("UNKNOWN")
        self.assertEqual(data["codex_diagnostics"]["counts"], {"UNSAFE_RESPONSE_ITEM": 1})

    def test_unpaired_summary_and_sidechain_do_not_prove_loss(self):
        self.fixture.create([*pair(isSidechain=True), *pair(2)[:1], claude("user", "intervening"),
                             pair(2)[1]], [MARKER])
        data = self.run_cli("UNKNOWN")["claude"]
        self.assertEqual(data["paired_checkpoint_count"], 0)
        self.assertFalse(data["latest_markers_form_valid_pair"])

    def test_invalid_checkpoint_payload_not_treated_as_usable(self):
        self.fixture.create(pair(), [cp(), cp(window_number=True, replacement_history_metadata=[]), MARKER])
        result = self.run_cli("UNKNOWN")
        self.assertEqual(result["codex"]["compacted_count"], 2)
        self.assertEqual(result["codex"]["decoded_compacted_count"], 1)
        self.assertIsNone(result["codex"]["latest_checkpoint"])

    def test_rollback_and_lineage_are_explicitly_unmodeled(self):
        self.fixture.create(pair(), [cp(), event("thread_rolled_back", num_turns=1)], history_base={})
        result = self.run_cli("UNKNOWN", token_budget=100)
        self.assertEqual(result["codex_diagnostics"]["counts"], {
            "PAGINATED_LINEAGE_NOT_REPLAYED": 1, "ROLLBACK_REPLAY_NOT_SUPPORTED": 1})
        self.assertIsNone(result["codex"]["effective_replayed_history"])

    def test_rejects_symlink_to_source(self):
        self.fixture.create(pair(), [MARKER])
        original = self.fixture.source.with_name("different.jsonl")
        self.fixture.source.rename(original)
        self.fixture.source.symlink_to(original)
        data = self.run_cli("UNKNOWN")
        self.assertNotIn("claude", data)

    def test_read_only_and_report_redaction(self):
        self.fixture.create(pair(), [response(pair()[1]["message"]["content"]), MARKER])
        files = (self.fixture.source, self.fixture.rollout, self.fixture.ledger)
        for path in files:
            path.chmod(stat.S_IRUSR)

        def snapshot():
            return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size,
                             p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_mode)
                    for p in files}

        before = snapshot()
        tree = sorted(self.fixture.codex.parent.rglob("*"))
        self.run_cli(command="list")
        self.run_cli("SOURCE_CHECKPOINT_LOST")
        output = self.fixture.run("report")
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertNotIn(SECRET, output.stdout)
        self.assertNotIn(TID, output.stdout)
        self.assertNotIn(str(self.fixture.source), output.stdout)
        self.assertNotIn(before[self.fixture.source.name][0], output.stdout)
        self.assertEqual(before, snapshot())
        self.assertEqual(tree, sorted(self.fixture.codex.parent.rglob("*")))


if __name__ == "__main__":
    unittest.main()
