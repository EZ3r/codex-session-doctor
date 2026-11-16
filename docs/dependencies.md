# Dependency provenance

| Component | Use in this project | Source and licensing |
| --- | --- | --- |
| Python 3.11+ | Standard library CLI. No Python runtime packages are required. | [Python Software Foundation license](https://docs.python.org/3/license.html). The interpreter is user-supplied. |
| Setuptools | PEP 517 source and wheel builds. | [MIT](https://github.com/pypa/setuptools/blob/main/LICENSE). Build-only and installed separately. |
| Rust/Cargo and Git | Local optional validator build. | [Rust and Cargo](https://github.com/rust-lang/rust/blob/master/COPYRIGHT) use their upstream terms, including MIT/Apache-2.0 for core components. [Git](https://git-scm.com/about) is GPL-2.0. These executables are user-supplied. |
| Codex and its Cargo dependencies | Optional local build uses the exact upstream revision and its lockfile. Codex App Server is a separately installed integration. | [Codex Apache-2.0](https://github.com/openai/codex/blob/0184dfa6a48f0110dd9c6835522492908e5abcf3/LICENSE), [upstream notices](https://github.com/openai/codex/blob/0184dfa6a48f0110dd9c6835522492908e5abcf3/NOTICE), [dependency graph inputs](https://github.com/openai/codex/blob/0184dfa6a48f0110dd9c6835522492908e5abcf3/codex-rs/Cargo.lock) and [license policy](https://github.com/openai/codex/blob/0184dfa6a48f0110dd9c6835522492908e5abcf3/codex-rs/deny.toml). Individual Rust dependencies use their own licenses. |
| GitHub Actions | CI uses commit-pinned checkout and setup-python actions. | [checkout MIT](https://github.com/actions/checkout/blob/v6.0.2/LICENSE) and [setup-python MIT](https://github.com/actions/setup-python/blob/v6.2.0/LICENSE). Workflows fetch these actions. |

The source tree and Python wheel ship the small Session Doctor harness, the Python CLI and their notices. They do not contain any Rust dependency source, Codex checkout, or executable. A separate compliance review would be needed before distributing a compiled verifier, its dependencies, or a bundled Codex CLI.

The [reference draft by mh0pe](https://github.com/mh0pe/codex/pull/1/files) informed the problem statement and design comparison. Its patch is not included. See [NOTICE](../NOTICE) for attribution to Codex source and its summary marker.
