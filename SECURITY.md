# Security policy

The current release line is 0.1.x. Treat experimental live recovery as an operation on local Codex state. The primary diagnostic commands are read-only. Registration and a paid-model continuation require separate explicit commands.

Report vulnerabilities through the repository's private GitHub vulnerability reporting form if enabled. If private reporting is unavailable, use a maintainer's verified private contact channel. Please share the affected version, the security boundary, and a synthetic reproduction. Keep credentials, real session content, recovery bundles, real rollout or registry files, and identifying diagnostics out of public issues, forks, and attachments.

Examples and regression data in this repository must be generated from scratch. Pseudonymized real transcripts, output from real sessions, and binary builds with local provenance are unsuitable as fixtures.

The default state directory holds private transcript copies. Use a trusted local account, filesystem, executable search path and Rust toolchain. Passing a native verifier override authorizes execution of that local program with access to the selected bundle. Registry locking coordinates this CLI within one state directory. Other processes sharing the same OS account retain their normal access.
