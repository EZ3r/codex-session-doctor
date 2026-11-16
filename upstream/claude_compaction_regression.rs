// SPDX-License-Identifier: Apache-2.0

#[cfg(test)]
mod session_doctor_claude_regression {
    use super::*;

    fn import_fixture() -> ImportedExternalAgentSession {
        let directory = tempfile::tempdir().expect("synthetic fixture directory");
        let path = directory.path().join("session.jsonl");
        std::fs::write(&path, include_str!("session_doctor_claude_minimal.jsonl"))
            .expect("write synthetic fixture");
        load_session_for_import(&path)
            .expect("fixture is readable")
            .expect("fixture has a usable transcript")
    }

    fn message_contains(item: &ResponseItem, needle: &str) -> bool {
        let ResponseItem::Message { content, .. } = item else {
            return false;
        };
        content.iter().any(|item| match item {
            ContentItem::InputText { text, .. } | ContentItem::OutputText { text, .. } => {
                text.contains(needle)
            }
            _ => false,
        })
    }

    #[test]
    fn reproduces_flattened_claude_import_on_pinned_baseline() {
        let imported = import_fixture();
        assert!(
            !imported
                .rollout_items
                .iter()
                .any(|item| matches!(item, RolloutItem::Compacted(_)))
        );
        for text in ["SUPERSEDED:", "PRESERVED:", "SUMMARY:", "TAIL:"] {
            assert!(imported.rollout_items.iter().any(|item| {
                matches!(item, RolloutItem::ResponseItem(response) if message_contains(response, text))
            }), "missing synthetic sentinel {text}");
        }
    }

    #[test]
    #[ignore = "expected to fail until the import path emits a resumable legacy checkpoint"]
    fn expects_bounded_legacy_checkpoint_with_preserved_context() {
        let imported = import_fixture();
        let (position, checkpoint) = imported
            .rollout_items
            .iter()
            .enumerate()
            .rev()
            .find_map(|(position, item)| match item {
                RolloutItem::Compacted(checkpoint) => Some((position, checkpoint)),
                _ => None,
            })
            .expect("Claude compaction must become a native checkpoint");
        assert!(checkpoint.window_number.is_some());
        assert!(checkpoint.resume_metadata.is_some());
        let replacement = checkpoint
            .replacement_history
            .as_ref()
            .expect("replacement history");
        assert!(
            replacement
                .iter()
                .any(|item| message_contains(&item.item, "SUMMARY:"))
        );
        assert!(
            replacement
                .iter()
                .any(|item| message_contains(&item.item, "PRESERVED:"))
        );
        assert!(
            !replacement
                .iter()
                .any(|item| message_contains(&item.item, "SUPERSEDED:"))
        );
        assert!(imported.rollout_items[..position].iter().any(|item| {
            matches!(item, RolloutItem::ResponseItem(response) if message_contains(response, "SUPERSEDED:"))
        }), "archival transcript before the checkpoint must remain present");
        assert!(replacement.iter().any(|item| message_contains(&item.item, "TAIL:"))
            || imported.rollout_items[position + 1..].iter().any(|item| {
                matches!(item, RolloutItem::ResponseItem(response) if message_contains(response, "TAIL:"))
            }), "logical tail must survive on either side of the storage boundary");
    }
}
