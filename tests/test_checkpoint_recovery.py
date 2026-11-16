import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from native_verifier import find_verifier

from test_session_doctor import (
    Fixture, MARKER, SCRIPT, SECRET, TID, claude, event, item, pair, response,
)

NATIVE_BINARY = find_verifier(SimpleNamespace())


def create_recovery_fixture(fixture):
    old_boundary, old_summary = pair(1)
    old_summary["uuid"] = "older-summary"
    old_summary["message"]["content"] += " Superseded fixture context." * 128
    boundary, summary = pair(2)
    boundary["compactMetadata"] = {"preservedSegment": {
        "headUuid": "preserve-head", "tailUuid": "preserve-tail", "anchorUuid": "summary",
    }}
    summary.update(uuid="summary", parentUuid="attachment")
    context = [
        old_boundary, old_summary,
        claude("user", "keep my constraint", uuid="preserve-head"),
        claude("assistant", "keep this result", uuid="preserve-tail", parentUuid="preserve-head"),
        boundary, {"type": "attachment", "uuid": "attachment", "parentUuid": "boundary-2"},
        summary, claude("user", "Claude continuation", uuid="tail", parentUuid="preserve-tail"),
    ]
    complete = "external-import-turn-5"
    rollout = [
        response(old_summary["message"]["content"]), response("keep my constraint"),
        response("keep this result", "assistant"),
        response(summary["message"]["content"]), response("Claude continuation"),
        event("task_started", turn_id=complete), MARKER,
        event("token_count", info={
            "total_token_usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                                  "reasoning_output_tokens": 0, "total_tokens": 750000},
            "last_token_usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                                 "reasoning_output_tokens": 0, "total_tokens": 750000},
        }),
        event("task_complete", turn_id=complete),
        event("task_started", turn_id="synthetic-codex-turn", model_context_window=475000),
        response("after import"),
        item("world_state", {"full": True, "state": {"fixture": {"step": 1}}}),
        item("response_item", {"type": "function_call", "name": "fixture_tool",
                               "arguments": "{}", "call_id": "synthetic-call"}),
        item("response_item", {"type": "function_call_output", "output": "synthetic tool output",
                               "call_id": "synthetic-call"}),
        item("world_state", {"full": False, "state": {"fixture": {"step": 2}}}),
        event("task_complete", turn_id="synthetic-codex-turn"),
    ]
    fixture.create(context, rollout, mode="legacy", multi_agent_version="v1", context_window={
        "window_id": "01900000-0000-7000-8000-000000000001",
    })
    header = json.loads(fixture.rollout.read_bytes().splitlines()[0])
    header["payload"]["originator"] = "fixture resumed"
    with fixture.rollout.open("ab") as stream:
        stream.write(json.dumps(header).encode() + b"\n")


def command(fixture, action, *arguments, env=None):
    args = [sys.executable, "-B", str(SCRIPT)]
    candidate_only = action in ("validate-candidate", "rebase-candidate")
    if not candidate_only:
        args += ["--codex-home", str(fixture.codex), "--claude-home", str(fixture.claude)]
    args += [action]
    if not candidate_only:
        args += [TID]
    return subprocess.run(args + list(map(str, arguments)), capture_output=True, text=True, env=env)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="session-doctor-recovery-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.fixture = Fixture(self.root)
        create_recovery_fixture(self.fixture)
        self.output = self.root / "isolated"
        self.output.mkdir()

    def run_command(self, action, *args, code=0):
        result = command(self.fixture, action, *args)
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def build(self):
        result = self.run_command("build-candidate", "--output", self.output)
        self.assertEqual(result["status"], "ISOLATED_CANDIDATE_BUILT")
        return Path(result["candidate_path"])

    def rewrite(self, path, transform):
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        transform(records)
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in records))

    def snapshot(self):
        return [(hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mtime_ns,
                 p.stat().st_ctime_ns) for p in
                (self.fixture.source, self.fixture.rollout, self.fixture.ledger)]

    def test_plan_and_bundle_preserve_all_bytes_and_expected_model_order(self):
        before = self.snapshot()
        plan = self.run_command("plan")
        self.assertEqual(plan["status"], "READY_FOR_ISOLATED_CANDIDATE")
        self.assertNotIn(SECRET, json.dumps(plan))
        self.assertEqual(plan["paired_claude_checkpoints"], 2)
        self.assertEqual(plan["preserved"]["items"], 2)
        self.assertEqual(plan["claude_logical_tail"]["items"], 1)
        self.assertEqual(plan["codex_post_import"]["items"], 3)
        self.assertEqual(plan["before"]["items"], 10)
        self.assertEqual(plan["after"]["items"], 7)
        self.assertTrue(plan["last_recorded_count_exceeds_window"])
        self.assertIn("REBASE_PRE_CHECKPOINT_IMPORTED_TOKEN_ESTIMATE", plan["activation_checks"])
        self.assertEqual(plan["post_import_rollout_record_counts"]["world_state"], 2)
        self.assertEqual(list(self.output.iterdir()), [])

        candidate = self.build()
        raw = (candidate / "candidate.jsonl").read_bytes().splitlines(keepends=True)
        original = self.fixture.rollout.read_bytes().splitlines(keepends=True)
        insertion = plan["checkpoint_insert_after_rollout_line"]
        self.assertEqual(raw[:insertion] + raw[insertion + 1:], original)
        check = json.loads(raw[insertion])["payload"]
        self.assertEqual(check["window_number"], 1)
        self.assertEqual(check["resume_metadata"]["multi_agent_version"], "v1")
        replacement = check["replacement_history"]
        self.assertIn("Synthetic summary 2", replacement[0]["content"][0]["text"])
        self.assertNotIn("Synthetic summary 1", json.dumps(replacement))
        self.assertEqual([value["content"][0]["text"] for value in replacement[1:]],
                         ["keep my constraint", "keep this result", "Claude continuation"])
        result = command(self.fixture, "validate-candidate", candidate,
                         env={**os.environ, "SESSION_DOCTOR_NATIVE_TEST_BIN": "",
                              "XDG_STATE_HOME": str(self.root / "empty-state")})
        self.assertEqual((result.returncode, json.loads(result.stdout)["status"]),
                         (2, "NATIVE_VALIDATION_REQUIRED"))
        self.assertEqual(before, self.snapshot())

    def test_drift_and_transcript_mismatch_block_build(self):
        self.rewrite(self.fixture.rollout, lambda rows: rows[1]["payload"]["content"][0].update(text="altered"))
        plan = self.run_command("build-candidate", "--output", self.output, code=2)
        self.assertIn("IMPORT_TRANSCRIPT_NOT_IDENTICAL", plan["blockers"])
        self.assertFalse(list(self.output.iterdir()))
        self.rewrite(self.fixture.source, lambda rows: rows[-1]["message"].update(content="altered"))
        self.assertIn("SOURCE_HASH_MISMATCH", self.run_command("plan", code=2)["blockers"])

    def test_disconnected_tail_and_conflicting_preserved_anchor_block_build(self):
        self.rewrite(self.fixture.source, lambda rows: rows[-1].update(parentUuid="missing"))
        self.fixture.refresh_mapping()
        self.assertIn("CLAUDE_LINEAGE_MISSING_PARENT", self.run_command("plan", code=2)["blockers"])
        def break_anchor(rows):
            rows[-4]["compactMetadata"]["preservedSegment"]["anchorUuid"] = "wrong"
        self.rewrite(self.fixture.source, break_anchor)
        self.fixture.refresh_mapping()
        self.assertIn("CLAUDE_PRESERVED_ANCHOR_MISMATCH", self.run_command("plan", code=2)["blockers"])

    def test_parallel_results_follow_verified_call_ids(self):
        def parallel(rows):
            rows.append(claude("assistant", [
                {"type": "tool_use", "id": "one", "name": "fixture"},
                {"type": "tool_use", "id": "two", "name": "fixture"},
            ], uuid="parallel", parentUuid="tail"))
            for call in ("one", "two"):
                rows.append(claude("user", [{"type": "tool_result", "tool_use_id": call,
                                            "content": "fixture"}], uuid=call, parentUuid="parallel",
                                   sourceToolAssistantUUID="parallel"))
            rows.append(claude("assistant", "completed", uuid="after-tools", parentUuid="two"))
        self.rewrite(self.fixture.source, parallel)
        self.fixture.refresh_mapping()
        report = command(self.fixture, "report")
        claude_report = json.loads(report.stdout)["claude"]
        self.assertEqual(claude_report["logical_history"]["logical_tail_items"], 5)
        self.rewrite(self.fixture.source, lambda rows: rows[-3].update(sourceToolAssistantUUID="wrong"))
        self.fixture.refresh_mapping()
        report = command(self.fixture, "report")
        self.assertEqual(json.loads(report.stdout)["claude"]["logical_history_error"],
                         "CLAUDE_PARALLEL_TOOL_RESULT_UNVERIFIED")

    def test_ambiguous_mapping_and_missing_import_end_block(self):
        self.rewrite(self.fixture.rollout, lambda rows: rows.pop(11))
        self.assertIn("UNIQUE_COMPLETE_IMPORT_BOUNDARY_REQUIRED", self.run_command("plan", code=2)["blockers"])
        ledger = json.loads(self.fixture.ledger.read_text())
        ledger["records"] *= 2
        self.fixture.ledger.write_text(json.dumps(ledger))
        plan = self.run_command("plan", code=2)
        self.assertEqual(plan["evidence"], ["AMBIGUOUS_IMPORT_MAPPING"])

    def test_refuses_output_in_session_home(self):
        data = self.run_command("build-candidate", "--output", self.fixture.codex, code=2)
        self.assertEqual(data["evidence"], ["OUTPUT_MUST_BE_AN_EXISTING_ISOLATED_DIRECTORY"])

    def test_rebase_respects_explicit_alternate_session_home(self):
        draft = self.build()
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--codex-home", str(self.fixture.codex),
                                 "rebase-candidate", str(draft), "--output", str(self.fixture.codex),
                                 "--native-test-bin", sys.executable], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["evidence"], ["OUTPUT_MUST_BE_AN_EXISTING_ISOLATED_DIRECTORY"])

    def test_modified_candidate_fails_validation(self):
        candidate = self.build()
        with (candidate / "candidate.jsonl").open("ab") as file:
            file.write(b"\n")
        self.assertEqual(self.run_command("validate-candidate", candidate, code=2)["evidence"],
                         ["CANDIDATE_BUNDLE_DIGEST_MISMATCH"])

    def test_unknown_checkpoint_field_blocks_even_with_updated_manifest_hash(self):
        candidate = self.build()
        bundle = candidate / "candidate.jsonl"
        spec = candidate / "expectations.json"
        manifest = json.loads(spec.read_text())
        lines = bundle.read_bytes().splitlines(keepends=True)
        checkpoint_index = manifest["added_line"] - 1
        row = json.loads(lines[checkpoint_index])
        row["payload"]["new_protocol_field"] = True
        lines[checkpoint_index] = json.dumps(row).encode() + b"\n"
        bundle.write_bytes(b"".join(lines))
        manifest["candidate_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
        spec.write_text(json.dumps(manifest))
        self.assertEqual(self.run_command("validate-candidate", candidate, code=2)["evidence"],
                         ["CANDIDATE_CHECKPOINT_SCHEMA_UNSUPPORTED"])

    @unittest.skipUnless(NATIVE_BINARY, "native test binary unavailable")
    def test_native_reconstructs_complete_synthetic_candidate(self):
        candidate = self.build()
        result = self.run_command("validate-candidate", candidate, "--native-test-bin",
                                  NATIVE_BINARY)
        self.assertEqual(result["status"], "NATIVE_VALIDATED")
        self.assertEqual(result["metrics"]["before"]["normalized_model_items"], 10)
        self.assertEqual(result["metrics"]["after"]["normalized_model_items"], 7)
        self.assertLess(result["metrics"]["after"]["estimated_tokens"],
                        result["metrics"]["before"]["estimated_tokens"])
        prior = result["metrics"]["startup"]
        self.assertEqual(prior["original_import_estimate_tokens"], 750000)
        self.assertEqual(prior["selected_last_usage_tokens"], 750000)
        self.assertTrue(prior["eligible_import_estimate"])
        self.assertFalse(prior["uses_local_rebase"])

        result = self.run_command("rebase-candidate", candidate, "--output", self.output,
                                  "--native-test-bin", NATIVE_BINARY)
        self.assertEqual(result["status"], "NATIVE_VALIDATED")
        fixed = result["metrics"]["startup"]
        self.assertTrue(fixed["uses_local_rebase"])
        self.assertEqual(fixed["startup_context_usage_tokens"], fixed["total_replayed_history_estimate_tokens"])
        self.assertEqual(fixed["startup_context_usage_tokens"],
                         fixed["selected_last_usage_tokens"] + fixed["unreported_tail_estimate_tokens"])
        self.assertGreater(fixed["unreported_tail_estimate_tokens"], 0)
        self.assertLess(fixed["startup_context_usage_tokens"], 750000)
        self.assertEqual(fixed["post_import_response_items"], prior["post_import_response_items"])

    @unittest.skipUnless(NATIVE_BINARY, "native test binary unavailable")
    def test_rebase_rejects_a_newer_token_count(self):
        rows = [json.loads(line) for line in self.fixture.rollout.read_bytes().splitlines()]
        recent = next(row for row in rows if row["payload"].get("type") == "token_count")
        recent["payload"]["info"]["last_token_usage"]["total_tokens"] = 33
        with self.fixture.rollout.open("ab") as stream:
            stream.write(json.dumps(recent).encode() + b"\n")
        candidate = self.build()
        output = self.run_command("rebase-candidate", candidate, "--output", self.output,
                                  "--native-test-bin", NATIVE_BINARY, code=2)
        self.assertEqual(output["evidence"], ["STARTUP_USAGE_PROVENANCE_NOT_ELIGIBLE"])
        self.assertEqual(list(self.output.iterdir()), [candidate])


if __name__ == "__main__":
    unittest.main()
