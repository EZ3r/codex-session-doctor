English | [简体中文](README.zh-CN.md)

# Codex Session Doctor

Session Doctor diagnoses Claude Code sessions imported into Codex and can recover a usable model context in a new thread. It is an independent community project unaffiliated with OpenAI or Anthropic.

## Why an imported thread can get too large

Claude Code periodically compacts its session. A compaction boundary marks where a summary, preserved messages and later messages replace the earlier working context. The reviewed Codex importer drops the boundary and can import the summary as a regular user message. A later request can then exceed the model's context window or the API's input-item limit because the original prefix remains in the reconstructed model history.

Session Doctor checks the import ledger and the local history. It uses the verified Claude checkpoint to create an isolated Codex candidate. The full imported transcript remains available for browsing. Codex's model history starts from the checkpoint's replacement, then replays the remaining records. The archived prefix stays on disk and outside the effective model context. [Upstream analysis and reproduction](https://github.com/EZ3r/codex-session-doctor/tree/main/upstream)

## Features

- Find ledger-linked imports and produce redacted, read-only diagnoses.
- Recover a verified summary, preserved context and logical tail while keeping subsequent Codex work.
- Validate the candidate and the startup token estimate using a locally built native Codex verifier.
- Explicitly register a separate local thread and optionally run one real continuation test.
- Reconcile interrupted registration and test requests against observed, persisted results.

## Requirements and supported versions

| Workflow | Requirements |
| --- | --- |
| npm CLI | macOS or Linux, Node.js 22+, npm, and Python 3.11+ |
| Python or source CLI | macOS or Linux and Python 3.11+ |
| Build a native verifier | Git and a Rust toolchain managed by rustup. The pinned Codex checkout selects the toolchain. |
| Register or test a thread | Reviewed Codex CLI `0.169.0-alpha.11.1` on PATH, a signed-in OpenAI account, and native validation |

Windows is currently unsupported. Automated recovery supports a uniquely mapped, unchanged source and a supported Codex **Legacy** import. It can diagnose Paginated replay gates, but automated recovery of Paginated history is outside the supported workflow. Unknown runtime versions, ambiguous or changed sources, unsafe schemas and unverified forks block the relevant operation. Other Codex versions require reviewing the writer, decoder, replay and App Server contracts and rebuilding the verifier.

## Installation

### npm

Install globally from the [public npm Registry](https://www.npmjs.com/package/codex-session-doctor).

```sh
npm install -g codex-session-doctor
session-doctor --version
```

To install a local build, run these commands from a checkout that contains `package.json`.

```sh
npm pack
npm install -g ./codex-session-doctor-0.2.0.tgz
session-doctor --version
```

The npm package contains the same Python core and Rust harness as the source checkout. The npm and Python distributions share a version number. npm installation links the command and installs the packaged files without running lifecycle scripts. At launch, the wrapper looks for Python 3.11+ on PATH. Set `SESSION_DOCTOR_PYTHON` to a trusted Python executable if it is installed elsewhere. The wrapper starts it directly, so paths can contain spaces and non-ASCII characters.

```sh
SESSION_DOCTOR_PYTHON="$(command -v python3.13)" session-doctor --version
```

### pipx

In a checkout, use an existing [pipx installation](https://pipx.pypa.io/). Choose a Python 3.11+ interpreter.

```sh
git clone https://github.com/EZ3r/codex-session-doctor.git
cd codex-session-doctor
pipx install .
session-doctor --version
```

A Python virtual environment with `python -m pip install .` works as well.

### From source

From the checkout, run the existing Python entry point. Use `./session-doctor` in place of the installed command throughout this guide.

```sh
./session-doctor --help
./session-doctor list
```

## Quick start

Select an **original imported thread ID** from `list` or copy it from its Codex Desktop thread link. Replace the fictional ID below before running the last two commands.

```sh
session-doctor list
THREAD_ID=00000000-0000-4000-8000-000000000101
session-doctor report "$THREAD_ID"
session-doctor recover "$THREAD_ID"
```

`report` omits direct identifiers and conversation text. A plain `recover` checks the mapping, plans the recovery and reports which runtime versions are supported. It does not start Codex, run the native verifier, create files, or send a model request. To practice on synthetic data, use the [offline walkthrough](docs/synthetic-recovery.md).

## Recovery workflow

### 1. Build and validate locally

```sh
session-doctor validator-build
session-doctor recover "$THREAD_ID" --build
```

`validator-build` explicitly builds the native verifier. A first build may fetch public source and dependencies, take several minutes, and use several GB of disk space. `recover --build` creates a private bundle and verifies the candidate, reconstructed history and startup token baseline. Look for `CANDIDATE_READY` or `CANDIDATE_ALREADY_BUILT`. Use the reported `candidate_path` to validate it again.

```sh
CANDIDATE_PATH=/fictional/recovery/candidate-example
session-doctor validate-candidate "$CANDIDATE_PATH"
```

### 2. Register a new thread

```sh
codex --version
session-doctor recover "$THREAD_ID" --register
```

This operation uses Codex App Server and its experimental fork-from-path support. Codex allocates a new ID and sets the original title with a ` (re)` suffix. It uses the local preview if the original has no title. Registration creates the new thread in Codex and updates Session Doctor's journal. It preserves the original thread, source and import ledger and starts no model turn. Look for `REGISTERED` or `ALREADY_REGISTERED` and copy the **new** `thread_id`. On macOS, open it in Codex Desktop. On other supported installations, open the link using a registered Codex link handler.

```sh
NEW_THREAD_ID=00000000-0000-4000-8000-000000000102
open "codex://threads/$NEW_THREAD_ID"
```

### 3. Decide whether to send a test turn

```sh
session-doctor recover "$THREAD_ID" --test
```

This separate opt-in command sends one real turn to the verified new thread. Its recovered history is submitted as normal model context, and the request can consume model quota. `CONTINUATION_PASSED` reports verified completion and local evidence, including whether compaction occurred. The command still accepts the original ID and looks up the registered thread in the journal.

## How checkpoint recovery works

- **Compaction** shrinks the model's working history. A **checkpoint** marks the resulting replacement and the information needed to resume from it.
- **Legacy history** is the Codex rollout mode supported for automated recovery. A usable checkpoint must meet the pinned runtime's history and resume-metadata checks.
- **`replacement_history`** is the checkpoint's new model-history base. Session Doctor combines the verified Claude summary and preserved context with the logical continuation, then lets Codex replay the later rollout records.
- The verifier checks the native decoder, reconstructed model history and startup estimate. Estimates are approximate and omit some runtime overhead. Only a real continuation can test provider behavior.

## Native validation

The npm package contains Rust test source. `validator-build` builds it locally against [a pinned Codex commit](https://github.com/openai/codex/commit/0184dfa6a48f0110dd9c6835522492908e5abcf3). The builder can use a local checkout that contains the pinned commit. Use `--offline` only when the Cargo dependencies are already cached.

```sh
CODEX_SOURCE=/fictional/src/codex
session-doctor validator-build --codex-source "$CODEX_SOURCE" --offline
```

The same native gates apply to the npm, Python and source CLIs. `NATIVE_VALIDATED` means the verifier ran successfully. `NATIVE_VALIDATION_REQUIRED` means only candidate integrity was checked. Advanced previews may execute an available local verifier. Only provide overrides built and trusted by you.

## Safety and privacy

- npm installation does not run the CLI, read session files, install Python, Rust, Git or Codex, or submit model requests. Runtime uses a trusted local Python interpreter. Use a trusted PATH and state directory.
- `list`, `report` and plain `recover` make no changes. `validator-build` and `recover --build` write local verifier or candidate state. `--register` creates a separate local Codex thread. Only the explicit test command starts a real model turn.
- The advanced commands have the same boundaries. `register-candidate --apply` creates a thread, and `test-continuation --send` submits a real turn. See `session-doctor --help` for those and the separate candidate build, validation and rebasing commands.
- Keep candidate bundles private. They may contain the complete transcript, tool outputs and metadata. Keep even redacted real-session reports out of public reports and version control.
- Keep state in one private directory for a given Codex home. The default is `${XDG_STATE_HOME:-$HOME/.local/state}/session-doctor`. `CODEX_HOME` and `CLAUDE_CONFIG_DIR` can select the input homes. Put global options such as `--state-dir`, `--codex-home` and `--claude-home` before the subcommand.
- The durable journal claims the operation before sending a registration or test request. Repeating an interrupted operation with the same original ID and state directory checks for the persisted result before submitting anything. Unverifiable requests remain pending. A manual continuation or an earlier attempted test prevents another automated test.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `session-doctor: command not found` | Check the npm global prefix with `npm prefix -g` and ensure its `bin` directory is on PATH. |
| Python is missing or too old | Install Python 3.11+ separately, or set `SESSION_DOCTOR_PYTHON` to its executable. |
| `UNKNOWN` or the source hash changed | Read the evidence codes in `report`. The tool requires an unambiguous source whose bytes match the ledger. Keep the original and obtain the exact source snapshot before trying a recovery. |
| Validator missing or validation fails | Run `validator-build`. Check the Git/Rust prerequisites, pinned revision, and the state directory in use. Resolve the validation error before registration. |
| Runtime unsupported | Confirm `codex --version` against the supported version above. Read-only diagnosis remains available. |
| Registration or test is pending | Keep the bundle and state directory. Repeat the same explicitly requested operation using the original ID so the journal can check the persisted result. |

## Development and tests

```sh
npm test
python3 -B -m unittest discover -s tests -v
npm pack --dry-run
npm pack
```

The npm suite installs the tarball offline into a temporary prefix and uses synthetic histories. To run the installed-package native replay case, set `SESSION_DOCTOR_NATIVE_TEST_BIN` to a trusted built verifier. The Python tests also use an installed default verifier when available. The npm [release checklist](docs/npm-release.md) describes the packaging and publication checks.

## Upstream, contributing and license

The proposed importer fix is independent of Session Doctor. See the [upstream proposal and Rust reproduction](https://github.com/EZ3r/codex-session-doctor/tree/main/upstream) for [OpenAI Codex issue #34656](https://github.com/openai/codex/issues/34656).

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Apache-2.0 license](LICENSE) · [Attribution](NOTICE) · [Dependency provenance](docs/dependencies.md) · [Changelog](CHANGELOG.md)
