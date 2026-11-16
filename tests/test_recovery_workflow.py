import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import native_verifier
import recovery_state
from session_doctor import Unavailable
from test_checkpoint_recovery import NATIVE_BINARY, create_recovery_fixture
from test_session_doctor import Fixture, TID


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="doctor-workflow-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.fixture = Fixture(self.root)
        create_recovery_fixture(self.fixture)
        self.state = self.root / "state"
        self.executable = Path(__file__).resolve().parents[1] / "session-doctor"

    def run_stage(self, *args, env=None, options=()):
        run = subprocess.run([str(self.executable), "--state-dir", str(self.state),
                              "--codex-home", str(self.fixture.codex), "--claude-home",
                              str(self.fixture.claude), *options, "recover", TID, *map(str, args)],
                             text=True, capture_output=True, env=env)
        self.assertEqual(run.stderr, "")
        return run.returncode, json.loads(run.stdout)

    def source_snapshot(self):
        inputs = (self.fixture.source, self.fixture.rollout, self.fixture.ledger)
        return [(hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns,
                 path.stat().st_ctime_ns) for path in inputs]

    def test_readonly_workflow_leaves_registry_and_source_untouched(self):
        before = self.source_snapshot()
        names = sorted(self.root.rglob("*"))
        code, data = self.run_stage(env={**os.environ, "SESSION_DOCTOR_NATIVE_TEST_BIN": ""})
        self.assertEqual((code, data["status"]), (0, "RECOVERY_PREVIEW"))
        self.assertTrue(data["next"].endswith(" validator-build"))
        self.assertIn(str(self.state), data["next"])
        self.assertFalse(data["live_codex_version_checked"])
        self.assertEqual(data["registry"], {})
        self.assertEqual(before, self.source_snapshot())
        self.assertEqual(names, sorted(self.root.rglob("*")))

    @unittest.skipUnless(NATIVE_BINARY, "native test binary unavailable")
    def test_build_reuses_verified_candidate_without_writing_sources(self):
        before = self.source_snapshot()
        code, data = self.run_stage("--build", "--native-test-bin", NATIVE_BINARY)
        self.assertEqual((code, data["status"]), (0, "CANDIDATE_READY"), data)
        self.assertTrue(data["metrics"]["startup"]["uses_local_rebase"])
        self.assertEqual(data["metrics"]["after"]["normalized_model_items"], 7)
        code, again = self.run_stage("--build", "--native-test-bin", NATIVE_BINARY)
        self.assertEqual((code, again["status"], again["candidate_path"]),
                         (0, "CANDIDATE_ALREADY_BUILT", data["candidate_path"]))
        self.assertEqual(before, self.source_snapshot())
        code, early_test = self.run_stage("--test")
        self.assertEqual((code, early_test["evidence"]), (2, ["NO_VERIFIED_FORK_RUN_RECOVER_REGISTER"]))

    def test_valid_checkpoint_exceeding_budget_blocks_plan(self):
        code, data = self.run_stage(options=("--token-budget", "2"))
        self.assertEqual(code, 0)
        self.assertIsNone(data["next"])
        self.assertEqual(data["plan"]["status"], "BLOCKED")
        self.assertIn("ESTIMATED_HISTORY_EXCEEDS_CALLER_TOKEN_BUDGET", data["plan"]["blockers"])
        self.assertFalse(self.state.exists())

    def test_corrupt_registry_fails_closed_and_does_not_leak_text(self):
        self.state.mkdir(mode=0o700)
        filename = self.state / "registry.json"
        filename.write_text('{"schema":1,"recoveries":{"confidential fixture": "broken"}}')
        filename.chmod(0o600)
        original = filename.read_bytes()
        code, data = self.run_stage("--register")
        self.assertEqual((code, data["evidence"]), (2, ["RECOVERY_REGISTRY_CORRUPT"]))
        self.assertNotIn("confidential fixture", json.dumps(data))
        self.assertEqual(filename.read_bytes(), original)

    def test_registry_rejects_symlinks_and_insecure_directories(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        self.state.symlink_to(target)
        self.assertEqual(self.run_stage()[0], 2)
        self.assertEqual(list(target.iterdir()), [])
        self.state.unlink()
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o757)
        self.assertEqual(self.run_stage()[0], 2)
        self.assertEqual(list(self.state.iterdir()), [])
        self.state.chmod(0o700)
        (self.state / "registry.json").symlink_to(target / "missing")
        self.assertEqual(self.run_stage()[0], 2)
        self.assertFalse((target / "missing").exists())

    def test_lock_prevents_concurrent_writes_and_missing_registry_blocks(self):
        with recovery_state.registry(self.state, write=True):
            with self.assertRaisesRegex(Unavailable, "RECOVERY_OPERATION_ALREADY_RUNNING"):
                with recovery_state.registry(self.state, write=True):
                    pass
        (self.state / "registry.json").unlink()
        self.assertEqual(self.run_stage()[1]["evidence"], ["RECOVERY_REGISTRY_MISSING"])

    def test_foreign_owned_ancestor_blocks_even_with_private_leaf(self):
        self.state.mkdir(mode=0o700)
        inode = self.root.stat().st_ino
        real_fstat = os.fstat

        def foreign_ancestor(fd):
            st = real_fstat(fd)
            if st.st_ino == inode:
                return SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=st.st_mode)
            return st

        with patch("recovery_state.os.fstat", side_effect=foreign_ancestor):
            with self.assertRaisesRegex(Unavailable, "PRIVATE_PATH_HAS_UNTRUSTED_ANCESTOR_OWNER"):
                with recovery_state.directory_fd(self.state):
                    self.fail("untrusted ancestor must be rejected")

    def test_native_source_stages_real_commit_with_replace_refs_disabled(self):
        source = self.root / "git-source"
        env = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")

        def git(*args):
            return subprocess.check_output(["git", "-C", str(source), *args], env=env,
                                           stderr=subprocess.DEVNULL).decode().strip()

        source.mkdir()
        git("init", "-q", "--template=")
        marker = source / "marker.txt"
        for text in ("trusted fixture", "alternate fixture"):
            marker.write_text(text)
            git("add", "marker.txt")
            git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-qm", text)
        original, replacement = git("rev-list", "--reverse", "HEAD").splitlines()
        git("replace", original, replacement)
        staging = self.root / "staged"
        native_verifier.git_checkout(source, staging, original)
        self.assertEqual((staging / "marker.txt").read_text(), "trusted fixture")

    def test_runtime_upgrade_rejected_before_app_server(self):
        fake_cli = self.root / "codex-bin"
        fake_cli.write_text("Codex CLI fixture")
        with patch("native_verifier.shutil.which", return_value=str(fake_cli)), \
             patch("native_verifier.subprocess.run",
                   return_value=SimpleNamespace(stdout=b"codex-cli 100.2.0\n", returncode=0)) as probe:
            with self.assertRaisesRegex(Unavailable, "CODEX_VERSION_UNSUPPORTED"):
                native_verifier.runtime()
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0], [str(fake_cli), "--version"])


if __name__ == "__main__":
    unittest.main()
