# Codex Session Doctor

An independent, unofficial tool for diagnosing and recovering Claude Code sessions imported into Codex. It is unaffiliated with and unsupported by OpenAI or Anthropic.

Session Doctor checks whether the import dropped a Claude compaction checkpoint. It can build and verify a candidate that restores the summary, preserved context, and logical tail while keeping the original imported transcript and subsequent Codex work.

## Install

From a checkout of this repository, run the following command with Python 3.11+ and [pipx](https://pipx.pypa.io/) on macOS or Linux.

```sh
pipx install .
session-doctor --version
```

The CLI uses the Python standard library. The fallback is `python3 -m pip install .` inside a virtual environment. To run the checkout directly, use `./session-doctor` in place of `session-doctor`.

## Quick start

Discover imported threads and select the **original imported thread ID**. The ID below is fictional. Replace it with the appropriate ID from `list` or your Codex Desktop thread link.

```sh
session-doctor list
THREAD_ID=00000000-0000-4000-8000-000000000101
session-doctor report "$THREAD_ID"
session-doctor recover "$THREAD_ID"
```

These commands are read-only. The preview checks the source mapping, recovery plan, compatible runtime versions and local status. It does not start Codex or run a verifier. `report` omits direct identifiers and conversation text. Treat even redacted output from real sessions as private.

To build a recovery candidate, install Git and a Rust toolchain managed by rustup. This downloads pinned public Codex source and Rust dependencies and can take several minutes and several GB of disk space.

```sh
session-doctor validator-build
session-doctor recover "$THREAD_ID" --build
```

`CANDIDATE_READY` means the private candidate passed native reconstruction and startup token-baseline checks. `CANDIDATE_ALREADY_BUILT` means the existing candidate was reverified. To run the check separately, pass the `candidate_path` printed by the build.

```sh
session-doctor validate-candidate /fictional/recovery/candidate-example
```

Explicitly request registration once the output satisfies you.

```sh
codex --version
session-doctor recover "$THREAD_ID" --register
```

Codex assigns a new ID and appends ` (re)` to the original title. If the original has no title, the tool uses its local preview. Registration preserves the source, original thread and import ledger. It does not start a model turn. The live workflow supports the reviewed Codex CLI `0.169.0-alpha.11.1` with an OpenAI sign-in. Local diagnostics are available without that runtime.

The following **optional** command sends one real model turn to the verified new thread. The restored history is sent as normal model context and the request uses model quota. Run this only if you want to make that request.

```sh
session-doctor recover "$THREAD_ID" --test
```

Copy the **new** `thread_id` from the registration result. On macOS, open its link in Codex Desktop. The ID below is fictional.

```sh
NEW_THREAD_ID=00000000-0000-4000-8000-000000000102
open "codex://threads/$NEW_THREAD_ID"
```

## Local state and interrupted operations

The default state directory is `${XDG_STATE_HOME:-$HOME/.local/state}/session-doctor`. Candidates may contain the complete Codex transcript, tool outputs and metadata. Keep them on a trusted local filesystem and out of version control. Use the same state directory for each operation on a given Codex home. Input locations default to `CODEX_HOME` and `CLAUDE_CONFIG_DIR` or the standard home directories. Global options such as `--state-dir`, `--codex-home`, and `--claude-home` go before the subcommand.

The journal records a durable claim before the local register or model-test RPC. If a response is interrupted, repeat the same operation with the same source ID and state directory. The command first looks for a uniquely matching persisted fork or the original marked test turn. If the result cannot be verified, it stays pending. Keep the original state and candidate while investigating. A manual continuation or an already attempted test prevents another automated turn. This protection is local to the saved journal and rollout evidence.

Use trusted sources and binaries. Advanced previews may run the local native verifier if it is available. Only override it with a binary you built and trust. Use `session-doctor --help` for advanced preview, build, verify, rebase, register and test commands.

## Compatibility

Automated recovery supports a uniquely mapped, unchanged Claude source and a supported Codex **Legacy** import. Diagnosis also distinguishes Paginated replay gates. Ambiguous or damaged records, unsupported schemas, unknown versions, existing conflicting candidates, and unverified fork evidence block the affected operation. Offline token estimates exclude some live request overhead and are not provider measurements.

The native verifier is built from [pinned Codex source](https://github.com/openai/codex/commit/0184dfa6a48f0110dd9c6835522492908e5abcf3). Changing Codex versions requires reviewing the writer, decoder, replay and App Server contracts, rebuilding the verifier and revalidating. This repository distributes source only. A cached validator is built locally.

## Development and upstream proposal

```sh
python3 -B -m unittest discover -s tests -v
```

Install the native verifier first to include the native-dependent cases. All checked-in fixtures and example results are synthetic. See the [upstream proposal](upstream/README.md), [synthetic JSONL](upstream/claude-minimal.jsonl), and [regression runner](upstream/run-repro.sh) for the importer issue. The proposal is separate from this CLI's workflow and registry.

[License](LICENSE) · [Attribution](NOTICE) · [Dependency provenance](docs/dependencies.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)
