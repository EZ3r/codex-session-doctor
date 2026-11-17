# npm release checklist

The source manifest uses `codex-session-doctor`. The public npm Registry returned HTTP 404 for both `codex-session-doctor` and `@ez3r/codex-session-doctor` when checked on November 17, 2026. A 404 means there was no package listing in that lookup. Availability can change and publishing also depends on npm naming rules, scope ownership and your account permissions.

## Before publishing

- Recheck the exact name on the public Registry. Use the scoped fallback only if needed and after verifying control of the scope. If the package name changes, update `package.json` and both installation guides, including the local tarball filename, then rerun package tests.
- Verify that `package.json` and `session_doctor.VERSION` contain the same version. `pyproject.toml` derives the Python distribution version from that constant.
- Run the Python and npm suites, including the npm installation's native replay case, with a trusted locally built verifier available. Confirm GitHub Actions on both supported platforms before release.
- Run `npm pack --dry-run` and `npm pack`. Inspect the generated archive rather than relying on ignored files in a checkout. Confirm the launcher, Python modules, original Rust harness, Apache-2.0 license and upstream NOTICE are present. Reject any session export, candidate, registry, log, credential, binary or unrelated file.
- Install the actual tarball in a fresh prefix and run `session-doctor --version`, `--help` and the synthetic diagnostic/replay checks. The installation and CLI checks only establish those behaviors. Live registration and provider tests have separate gates.
- Check the npm account with `npm whoami` and satisfy the account's publish authentication and 2FA requirements. Confirm the selected name and version are available to that account.

```sh
npm view codex-session-doctor name version --registry=https://registry.npmjs.org
npm whoami --registry=https://registry.npmjs.org
npm pack --dry-run
npm pack
tar -tzf codex-session-doctor-0.2.0.tgz
```

## Publish manually after review

For the unscoped package, publish the reviewed tarball. For the scoped fallback, use the exact tarball name reported by `npm pack` and `--access public`. No publish step runs in CI.

```sh
npm publish ./codex-session-doctor-0.2.0.tgz --registry=https://registry.npmjs.org
npm view codex-session-doctor@0.2.0 name version dist.integrity --registry=https://registry.npmjs.org
```

After publication, users can install with `npm install -g codex-session-doctor`. If the scoped fallback was selected, use `npm install -g @ez3r/codex-session-doctor`.
