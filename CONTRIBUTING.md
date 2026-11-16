# Contributing

Use Python 3.11+ and run `python3 -B -m unittest discover -s tests -v`. Changes to recovery or its native harness also need `./session-doctor validator-build` and a suite run with the verifier available. On a non-default state directory, pass its verified executable with `SESSION_DOCTOR_NATIVE_TEST_BIN`.

Generate inputs and expected results using fictional text and identifiers. Avoid recording any real session, title, path, log, account detail, candidate bundle or CLI output. Tests must use disposable homes. App Server writes and provider requests should be mocked.

Changes should preserve the read-only default, explicit mutation gates, source mapping, native replay validation and durable reconciliation. Place private vulnerability reports through the channel in [SECURITY.md](SECURITY.md).

Contributions to this repository are under Apache-2.0. Identify any added third-party material and preserve its applicable notices. For the Codex importer issue, follow the [upstream contributing policy](https://github.com/openai/codex/blob/main/docs/contributing.md) and share a synthetic reproduction in the existing issue.
