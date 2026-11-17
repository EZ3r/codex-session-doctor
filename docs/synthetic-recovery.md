English | [简体中文](synthetic-recovery.zh-CN.md)

# Walk through a recovery with synthetic data

This exercise generates a fictional imported history in a temporary directory. It uses no real Claude or Codex data and needs no account. Run from a source checkout of this version with `session-doctor` installed. To use the checkout directly, replace `session-doctor` in the function below with `./session-doctor`.

```sh
DEMO=$(mktemp -d)
DEMO=$(cd "$DEMO" && pwd -P)
THREAD_ID=01900000-0000-7000-8000-000000000123

XDG_STATE_HOME="$DEMO/bootstrap" SESSION_DOCTOR_NATIVE_TEST_BIN= python3 -B - "$DEMO" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tests"))
from test_checkpoint_recovery import create_recovery_fixture
from test_session_doctor import Fixture

create_recovery_fixture(Fixture(Path(sys.argv[1])))
PY

doctor() {
  session-doctor --claude-home "$DEMO/claude" --codex-home "$DEMO/codex" \
    --state-dir "$DEMO/private state" "$@"
}

doctor list
doctor report "$THREAD_ID"
doctor recover "$THREAD_ID"
```

The plan should show `SOURCE_CHECKPOINT_LOST` and `READY_FOR_ISOLATED_CANDIDATE`. The synthetic ledger points to the synthetic source. No verifier or Codex account is needed for these checks.

Now build the verifier and candidate. The verifier build may download pinned public source and Cargo dependencies. No model service is involved. If you already have a trusted native verifier, see [Native validation](../README.md#native-validation) and `session-doctor validator-build --help` for build options. You can also explicitly set `SESSION_DOCTOR_NATIVE_TEST_BIN` to your existing verifier for the candidate build.

```sh
doctor validator-build
doctor recover "$THREAD_ID" --build
```

The example reconstructs seven synthetic model items and prints `CANDIDATE_READY`. It preserves the original synthetic rollout and checks the adjusted startup estimate. An existing candidate may return `CANDIDATE_ALREADY_BUILT`.

The last two stages below are **illustrative only**. This fixture has no live Desktop thread or Codex account. The example leaves these commands commented out.

```sh
# doctor recover "$THREAD_ID" --register
# doctor recover "$THREAD_ID" --test
```

| Command | Read-only | Local files written | Codex state changed | Model quota | Success indicator |
| --- | --- | --- | --- | --- | --- |
| `list` | Yes | None | None | No | The fictional original thread appears. |
| `report THREAD_ID` | Yes | None | None | No | `SOURCE_CHECKPOINT_LOST`. |
| `recover THREAD_ID` | Yes | None | None | No | `RECOVERY_PREVIEW` and a plan ready for a candidate. |
| `validator-build` | No | Private verifier and build/cache files | None | No | `VALIDATOR_READY`. |
| `recover THREAD_ID --build` | No | Isolated candidate and journal | None | No | `CANDIDATE_READY` or `CANDIDATE_ALREADY_BUILT` with native verification. |
| `recover THREAD_ID --register` | No | Journal and new local thread files | Yes, creates a new thread | No | `REGISTERED` or `ALREADY_REGISTERED` with a new ID. |
| `recover THREAD_ID --test` | No | Journal and test turn in the new thread | Yes, adds a turn | Yes, can consume quota | `CONTINUATION_PASSED` with evidence from the new thread. |

All IDs, inputs, paths and output examples in this exercise are fictional or generated for the exercise. See the [main guide](../README.md) for live registration requirements and how to handle interrupted operations.
