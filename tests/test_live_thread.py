import contextlib
import io
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import live_thread
from session_doctor import Unavailable, cli
from test_session_doctor import TID, OTHER, item, event, write_jsonl

WINDOW = "01900000-0000-7000-8000-000000000789"
HASH = "c" * 64


class LiveThreadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="doctor-live-tests-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "codex"
        self.state = self.root / "state"
        self.native = self.root / "native"
        self.native.write_bytes(b"validator fixture")
        self.path = self.home / "sessions" / "new.jsonl"
        self.path.parent.mkdir(parents=True)
        platform = patch("live_thread.runtime", return_value=SimpleNamespace(version="fixture", digest="d" * 64))
        platform.start()
        self.addCleanup(platform.stop)
        self.ready = SimpleNamespace(
            expected=SimpleNamespace(meta={"id": TID}, checkpoint={"window_id": WINDOW},
                                     responses=[], turn_starts=0),
            bundle=self.root / "bundle", native_binary=self.native,
            inputs=SimpleNamespace(codex=self.home, claude=self.root / "claude"),
            native_validated=False, cwd=self.home,
            original_unchanged=Mock(return_value=True), candidate_digest=HASH,
        )

    def cli_result(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            exit_code = cli(["--state-dir", str(self.state), *args])
        return exit_code, json.loads(out.getvalue())

    def test_both_commands_only_preview_by_default(self):
        with patch("live_thread.prepare", return_value=self.ready) as prepare, \
             patch("live_thread.target_rollout", return_value=self.path), \
             patch("live_thread.rollout_snapshot", return_value=SimpleNamespace(responses=[], turn_starts=0)), \
             patch("live_thread.verify_fork", return_value={}), \
             patch("live_thread.signed_in_server") as server:
            code, result = self.cli_result("register-candidate", str(self.ready.bundle))
            self.assertEqual((code, result["status"], result["registered"]),
                             (0, "REGISTRATION_PREVIEW", False))
            code, result = self.cli_result("test-continuation", str(self.ready.bundle), OTHER)
            self.assertEqual((code, result["status"], result["sent"]), (0, "CONTINUATION_PREVIEW", False))
            server.assert_not_called()
            self.assertTrue(all(call.args[1] is False for call in prepare.call_args_list))
            self.assertFalse(self.state.exists())

    def test_rpc_reader_keeps_verification_fields_and_discards_model_content(self):
        secret = "synthetic confidential payload"
        rows = [
            {"id": 1, "result": {"account": {"email": secret}}},
            {"method": "item/completed", "params": {"threadId": OTHER,
                                                     "item": {"type": "agentMessage", "text": secret}}},
            {"method": "item/agentMessage/delta", "params": {"delta": secret}},
            {"method": "turn/completed", "params": {"threadId": OTHER,
                                                     "turn": {"id": "turn", "status": "completed", "items": [secret]}}},
        ]
        server = live_thread.AppServer.__new__(live_thread.AppServer)
        server.events = queue.Queue()
        server.stopping = threading.Event()
        server.process = SimpleNamespace(stdout=io.BytesIO(
            "".join(json.dumps(row) + "\n" for row in rows).encode()))
        server.read_messages()
        messages = [server.events.get_nowait() for _ in range(4)]
        self.assertNotIn(secret, json.dumps(messages))
        self.assertEqual(messages[0]["result"], {"account": True})
        self.assertEqual(messages[2]["params"]["turn"], {"id": "turn", "status": "completed"})
        self.assertIsNone(messages[3])

    def test_explicit_registration_forks_once_and_names_only_the_new_thread(self):
        rpc = Mock()
        rpc.request.side_effect = [
            {"thread": {"id": TID, "name": "Synthetic Teacup Inventory", "preview": "First user message"}},
            {"thread": {"id": OTHER}},
            {},
        ]
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.hash_only", return_value=HASH), \
             patch("live_thread.active_rollout", return_value=self.path), \
             patch("live_thread.rollout_snapshot"), patch("live_thread.verify_fork", return_value={}), \
             patch("live_thread.signed_in_server", return_value=contextlib.nullcontext(rpc)):
            code, result = self.cli_result("register-candidate", str(self.ready.bundle), "--apply")
        self.assertEqual((code, result["status"], result["thread_id"]), (0, "REGISTERED", OTHER))
        requests = [call.args for call in rpc.request.call_args_list]
        self.assertEqual([method for method, _ in requests], ["thread/read", "thread/fork", "thread/name/set"])
        self.assertEqual(requests[0][1]["threadId"], TID)
        params = requests[1][1]
        self.assertNotEqual(params["threadId"], TID)
        self.assertFalse(params["ephemeral"])
        self.assertEqual(requests[2][1], {"threadId": OTHER, "name": "Synthetic Teacup Inventory (re)"})
        self.assertTrue(result["recovery_title_applied"])

    def test_recovery_title_uses_the_original_preview_when_untitled(self):
        self.assertEqual(live_thread.recovery_title({"name": None, "preview": "原始预览"}), "原始预览 (re)")

    def test_failed_rename_reports_the_thread_already_registered(self):
        rpc = Mock()
        rpc.request.side_effect = [
            {"thread": {"id": TID, "name": "Original"}},
            {"thread": {"id": OTHER}},
            Unavailable("APP_SERVER_TIMEOUT"),
        ]
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.hash_only", return_value=HASH), \
             patch("live_thread.active_rollout", return_value=self.path), \
             patch("live_thread.rollout_snapshot"), patch("live_thread.verify_fork", return_value={}), \
             patch("live_thread.signed_in_server", return_value=contextlib.nullcontext(rpc)):
            code, result = self.cli_result("register-candidate", str(self.ready.bundle), "--apply")
        self.assertEqual((code, result["status"], result["thread_id"], result["registered"]),
                         (2, "REGISTERED_TITLE_UNVERIFIED", OTHER, True))

    def test_registration_timeout_never_retries(self):
        rpc = Mock()
        rpc.request.side_effect = [{"thread": {"id": TID, "name": "Original"}}, Unavailable("APP_SERVER_TIMEOUT")]
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.hash_only", return_value=HASH), \
             patch("live_thread.signed_in_server", return_value=contextlib.nullcontext(rpc)):
            code, result = self.cli_result("register-candidate", str(self.ready.bundle), "--apply")
        self.assertEqual((code, result["status"]), (2, "REGISTRATION_RESULT_UNKNOWN"))
        self.assertEqual([call.args[0] for call in rpc.request.call_args_list], ["thread/read", "thread/fork"])
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.hash_only", return_value=HASH), \
             patch("live_thread.signed_in_server") as server:
            code, result = self.cli_result("register-candidate", str(self.ready.bundle), "--apply")
        self.assertEqual((code, result["status"]), (2, "REGISTRATION_PENDING"))
        server.assert_not_called()

    def test_send_resumes_verified_child_and_sends_one_turn(self):
        rpc = Mock()
        rpc.request.side_effect = [
            {"thread": {"status": {"type": "notLoaded"}}},
            {"thread": {"status": {"type": "idle"}}},
            {"turn": {"id": "smoke-turn"}},
        ]
        rpc.receive.return_value = {
            "method": "turn/completed",
            "params": {"threadId": OTHER, "turn": {"id": "smoke-turn", "status": "completed"}},
        }
        saved = {"completed_without_error": True, "confirmation_reply_saved": True,
                 "persisted_compaction_markers": 0, "persisted_errors": 0, "persisted_tool_items": 0, "new_turns": 1,
                 "provider_input_tokens": 200, "model_context_window": 1000, "turn_completed": True}
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.target_rollout", return_value=self.path), \
             patch("live_thread.matching_fork", return_value=(OTHER, self.path, False)), \
             patch("live_thread.active_rollout", return_value=self.path), \
             patch("live_thread.rollout_snapshot",
                   return_value=SimpleNamespace(lines=10, responses=[], turn_starts=0)), \
             patch("live_thread.verify_fork", return_value={}), \
             patch("live_thread.persisted_smoke_result", return_value=saved), \
             patch("live_thread.signed_in_server", return_value=contextlib.nullcontext(rpc)):
            code, result = self.cli_result("test-continuation", str(self.ready.bundle), OTHER, "--send")
        self.assertEqual((code, result["status"]), (0, "CONTINUATION_PASSED"))
        requests = [call.args for call in rpc.request.call_args_list]
        self.assertEqual([method for method, _ in requests], ["thread/read", "thread/resume", "turn/start"])
        self.assertTrue(all(params["threadId"] == OTHER for _, params in requests))
        self.assertEqual(requests[-1][1]["environments"], [])

    def test_original_id_never_reaches_server(self):
        self.ready.bundle.mkdir()
        manifest = {"startup_token_rebase_line": 2, "candidate_sha256": HASH}
        validation = {"status": "NATIVE_VALIDATED", "metrics": {"startup": {"uses_local_rebase": True}}}
        with patch("live_thread.find_verifier", return_value=self.native), \
             patch("live_thread.read_manifest", return_value=manifest), \
             patch("live_thread.validate", return_value=validation), \
             patch("live_thread.rollout_snapshot", return_value=self.ready.expected), \
             patch("live_thread.hash_only", return_value=HASH), \
             patch("live_thread.signed_in_server") as server:
            code, result = self.cli_result("test-continuation", str(self.ready.bundle), TID,
                                           "--native-test-bin", str(self.home / "native"), "--send")
        self.assertEqual((code, result["evidence"]), (2, ["CANNOT_TEST_ORIGINAL_THREAD"]))
        server.assert_not_called()

    def test_persisted_proof_excludes_historical_checkpoint_and_detects_new_one(self):
        rows = [
            item("compacted", {}),
            event("task_started", turn_id="smoke-turn", model_context_window=1000),
            event("agent_message", message=live_thread.SMOKE_REPLY + "."),
            item("token_usage_record", {"turn_id": "smoke-turn", "usage": {"input_tokens": 234}}),
            event("task_complete", turn_id="smoke-turn"),
        ]
        write_jsonl(self.path, rows)
        saved = live_thread.persisted_smoke_result(self.home, self.path, 1, "smoke-turn")
        self.assertEqual(saved["persisted_compaction_markers"], 0)
        self.assertTrue(saved["completed_without_error"])
        self.assertTrue(saved["confirmation_reply_saved"])
        self.assertEqual(saved["provider_input_tokens"], 234)
        self.assertTrue(live_thread.passed_persisted(saved))
        with self.path.open("ab") as stream:
            stream.write((json.dumps(event("task_started", turn_id="later-user-turn")) + "\n").encode())
        self.assertTrue(live_thread.passed_persisted(
            live_thread.persisted_smoke_result(self.home, self.path, 1, "smoke-turn")))
        rows.insert(2, item("response_item", {"type": "function_call"}))
        write_jsonl(self.path, rows)
        self.assertFalse(live_thread.passed_persisted(
            live_thread.persisted_smoke_result(self.home, self.path, 1, "smoke-turn")))
        rows.pop(2)
        rows.insert(2, item("compacted", {}))
        write_jsonl(self.path, rows)
        saved = live_thread.persisted_smoke_result(self.home, self.path, 1, "smoke-turn")
        self.assertEqual(saved["persisted_compaction_markers"], 1)

    def make_fork_fixture(self, parent=TID):
        self.path = self.path.parent / f"rollout-2026-01-01T00-00-00-{OTHER}.jsonl"
        payload = {"message": "synthetic", "replacement_history": [], "window_id": WINDOW,
                   "window_number": 1, "resume_metadata": {}}
        usage = {"last_token_usage": {"total_tokens": 140}}
        self.ready.expected = live_thread.Snapshot({"id": TID}, payload, [], usage, 3)
        write_jsonl(self.path, [
            item("session_meta", {"id": OTHER, "forked_from_id": parent, "history_mode": "legacy"}),
            item("compacted", payload), event("token_count", info=usage),
        ])

    def test_lost_fork_response_reconciles_persisted_thread_and_deduplicates(self):
        rpc = Mock()
        def respond(method, params, **kwargs):
            if method == "thread/read":
                return {"thread": {"id": TID, "name": "Original"}}
            self.assertEqual(method, "thread/fork")
            self.make_fork_fixture()
            raise Unavailable("APP_SERVER_DISCONNECTED")
        rpc.request.side_effect = respond
        self.make_fork_fixture()
        self.path.unlink()
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.hash_only", return_value=HASH), \
             patch("live_thread.signed_in_server", return_value=contextlib.nullcontext(rpc)) as server:
            args = ("register-candidate", str(self.ready.bundle), "--apply")
            self.assertEqual(self.cli_result(*args)[1]["status"], "REGISTRATION_RESULT_UNKNOWN")
            for _ in range(2):
                code, result = self.cli_result(*args)
                self.assertEqual((code, result["status"], result["thread_id"]), (0, "ALREADY_REGISTERED", OTHER))
            server.assert_called_once()

    def test_lost_turn_response_reconciles_unique_persisted_test_without_resend(self):
        self.make_fork_fixture()
        rpc = Mock()
        def respond(method, params, **_):
            if method == "thread/read":
                return {"thread": {"status": {"type": "notLoaded"}}}
            if method == "thread/resume":
                return {"thread": {"status": {"type": "idle"}}}
            self.assertEqual(method, "turn/start")
            with self.path.open("ab") as stream:
                rows = [
                    event("task_started", turn_id="retained-turn", model_context_window=1000),
                    event("user_message", message=params["input"][0]["text"]),
                    event("agent_message", message=live_thread.SMOKE_REPLY),
                    item("token_usage_record", {"turn_id": "retained-turn", "usage": {"input_tokens": 200}}),
                    event("task_complete", turn_id="retained-turn"),
                ]
                stream.writelines((json.dumps(row) + "\n").encode() for row in rows)
            raise Unavailable("APP_SERVER_TIMEOUT")
        rpc.request.side_effect = respond
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.target_rollout", return_value=self.path), \
             patch("live_thread.active_rollout", return_value=self.path), \
             patch("live_thread.signed_in_server", return_value=contextlib.nullcontext(rpc)) as server:
            args = ("test-continuation", str(self.ready.bundle), OTHER, "--send")
            self.assertEqual(self.cli_result(*args)[1]["status"], "CONTINUATION_RESULT_UNKNOWN")
            code, result = self.cli_result(*args)
            self.assertEqual((code, result["status"], result["sent"]), (0, "CONTINUATION_PASSED", False))
            self.assertTrue(result["reconciled"])
            self.assertEqual(self.cli_result(*args)[1]["status"], "CONTINUATION_ALREADY_ATTEMPTED")
            server.assert_called_once()

    def test_same_checkpoint_on_unrelated_thread_is_not_adopted(self):
        self.make_fork_fixture(parent="01900000-0000-7000-8000-000000000000")
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.target_rollout", return_value=self.path), \
             patch("live_thread.signed_in_server") as server:
            code, result = self.cli_result("test-continuation", str(self.ready.bundle), OTHER, "--send")
        self.assertEqual((code, result["evidence"]), (2, ["UNVERIFIED_RECOVERY_TARGET"]))
        server.assert_not_called()

    def test_previous_recovery_with_newly_built_window_blocks_duplicate(self):
        self.make_fork_fixture()
        self.ready.expected.checkpoint = {
            **self.ready.expected.checkpoint, "window_id": "01900000-0000-7000-8000-000000000123",
        }
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.signed_in_server") as server:
            code, result = self.cli_result("register-candidate", str(self.ready.bundle), "--apply")
        self.assertEqual((code, result["evidence"]),
                         (2, ["POSSIBLE_EXISTING_RECOVERY_REUSE_ORIGINAL_CANDIDATE"]))
        server.assert_not_called()

    def test_source_changes_after_metadata_read_prevent_fork(self):
        source = self.root / "claude.jsonl"
        source.write_text("original fixture")
        before = source.read_bytes()
        self.ready.original_unchanged = lambda: source.read_bytes() == before
        rpc = Mock()
        def respond(method, params):
            self.assertEqual(method, "thread/read")
            source.write_text("external fixture writer")
            return {"thread": {"id": TID, "name": "Original"}}
        rpc.request.side_effect = respond
        with patch("live_thread.prepare", return_value=self.ready), \
             patch("live_thread.hash_only", return_value=HASH), \
             patch("live_thread.signed_in_server", return_value=contextlib.nullcontext(rpc)):
            code, result = self.cli_result("register-candidate", str(self.ready.bundle), "--apply")
        self.assertEqual((code, result["evidence"]), (2, ["REGISTRATION_INPUT_CHANGED"]))
        rpc.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
