#!/bin/sh
set -eu

usage() {
    printf '%s\n' 'Usage: ./upstream/run-repro.sh CODEX_GIT_CHECKOUT [--expect-fix]' >&2
    exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE GIT_OBJECT_DIRECTORY
unset GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS GIT_CONFIG_SYSTEM
export GIT_NO_REPLACE_OBJECTS=1 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
checkout=$1
checkout=$(CDPATH= cd -- "$checkout" && pwd -P)
commit=0184dfa6a48f0110dd9c6835522492908e5abcf3
test_filter=reproduces_flattened_claude_import_on_pinned_baseline
case "${2:-}" in
    '') ;;
    --expect-fix)
        commit=$(git --no-replace-objects -C "$checkout" rev-parse --verify HEAD)
        test_filter=expects_bounded_legacy_checkpoint_with_preserved_context
        ;;
    *) usage ;;
esac

assets=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
stage=$(mktemp -d "${TMPDIR:-/tmp}/session-doctor-repro.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
git -C "$stage" -c core.hooksPath=/dev/null init -q --template=
git -C "$stage" -c transfer.fsckObjects=true fetch -q --no-tags --depth=1 -- "$checkout" "$commit"
git -C "$stage" -c core.hooksPath=/dev/null checkout -q --detach "$commit"
export_dir="$stage/codex-rs/external-agent-migration/src/sessions"
cp -- "$assets/claude-minimal.jsonl" "$export_dir/session_doctor_claude_minimal.jsonl"
printf '\n' >> "$export_dir/export.rs"
cat "$assets/claude_compaction_regression.rs" >> "$export_dir/export.rs"
cd "$stage/codex-rs"
export CARGO_PROFILE_TEST_DEBUG=0 CARGO_PROFILE_DEV_DEBUG=0
if [ "$test_filter" = expects_bounded_legacy_checkpoint_with_preserved_context ]; then
    cargo test --locked -p codex-external-agent-migration --lib "$test_filter" -- --ignored
else
    cargo test --locked -p codex-external-agent-migration --lib "$test_filter"
fi
