
// Session Doctor integration test. Included only in an isolated Codex checkout.
#[tokio::test]
#[ignore = "requires a Session Doctor candidate bundle"]
async fn session_doctor_native_reconstruction() {
    use std::io::BufRead;

    use codex_protocol::openai_models::InputModality;
    use codex_protocol::protocol::ThreadHistoryMode;
    use codex_protocol::protocol::TokenUsage;
    use codex_protocol::protocol::TruncationPolicy;
    use codex_rollout::RolloutRecorder;
    use serde_json::Value;

    let dir = std::path::PathBuf::from(
        std::env::var_os("SESSION_DOCTOR_NATIVE_DIR").expect("set SESSION_DOCTOR_NATIVE_DIR"),
    );
    let read_items = |name: &str| {
        let input = std::fs::File::open(dir.join(name)).expect("open candidate bundle");
        std::io::BufReader::new(input)
            .split(b'\n')
            .map(|line| line.expect("read candidate bundle"))
            .enumerate()
            .filter(|(_, line)| !line.iter().all(u8::is_ascii_whitespace))
            .map(|(index, line)| {
                codex_rollout::parse_rollout_line_bytes(&line)
                    .unwrap_or_else(|_| panic!("canonical decoder rejected JSONL row {}", index + 1))
                    .item
            })
            .collect::<Vec<_>>()
    };
    let manifest: Value = serde_json::from_reader(
        std::fs::File::open(dir.join("expectations.json")).expect("open expectations"),
    )
    .expect("decode validation metadata");
    let decoded = read_items("candidate.jsonl");
    let (items, stored_thread, parse_errors) =
        RolloutRecorder::load_rollout_items(&dir.join("candidate.jsonl"))
            .await
            .expect("load by the native Legacy rollout reader");
    assert_eq!(items.len(), decoded.len());
    assert_eq!(parse_errors, 0);
    assert!(stored_thread.is_some());
    drop(decoded);
    let expected = read_items("expected.jsonl");
    let compacted_indices = items
        .iter()
        .enumerate()
        .filter_map(|(index, item)| match item {
            RolloutItem::Compacted(cp) => Some((index, cp.as_ref())),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(compacted_indices.len(), 1);
    let (boundary, cp) = compacted_indices[0];
    let rebased = manifest.get("startup_token_rebase_line").is_some();
    assert!(matches!(items.first(), Some(RolloutItem::SessionMeta(meta))
        if meta.meta.history_mode == ThreadHistoryMode::Legacy && meta.meta.history_base.is_none()));
    assert_eq!(cp.window_number, Some(1));
    assert!(cp.resume_metadata.is_some());
    assert_eq!(
        cp.replacement_history.as_ref().map(Vec::len),
        manifest["replacement_items"].as_u64().map(|n| n as usize)
    );
    assert!(cp.window_id.as_ref().and_then(|id| id.validated()).is_some());

    let replay = |items: &[RolloutItem]| {
        ContextManager::reconstruct_rollout(
            items,
            ThreadHistoryMode::Legacy,
            ContextManager::default(),
            TruncationPolicy::Tokens(16_384),
        )
    };
    let without_checkpoint = items
        .iter()
        .enumerate()
        .filter(|(index, _)| *index != boundary && !(rebased && *index == boundary + 1))
        .map(|(_, item)| item.clone())
        .collect::<Vec<_>>();
    let original = replay(&without_checkpoint);
    let recovered = replay(&items);
    let expected = replay(&expected);
    assert_eq!(recovered.history.len(), expected.history.len());
    assert!(
        recovered.history.iter().zip(&expected.history).all(|(got, want)| got.item == want.item),
        "replayed model items differ in content or order"
    );
    assert_eq!(recovered.window_number, 1);
    assert!(recovered.window_id == cp.window_id);
    assert!(recovered.last_started_turn_id == original.last_started_turn_id);
    assert!(recovered.previous_turn_settings == original.previous_turn_settings);
    assert!(recovered.reference_context_item == original.reference_context_item);
    assert!(recovered.turn_attribution == original.turn_attribution);
    assert!(recovered.world_state_baseline == original.world_state_baseline);
    assert!(recovered.rejected_audio_file_ids == original.rejected_audio_file_ids);

    // Verify the pinned importer's own estimate and exclude any later real usage.
    let import_estimate = boundary.checked_sub(3).and_then(|start| {
        match &without_checkpoint[start..boundary] {
            [
                RolloutItem::EventMsg(EventMsg::AgentMessage(marker)),
                RolloutItem::EventMsg(EventMsg::TokenCount(event)),
                RolloutItem::EventMsg(EventMsg::TurnComplete(completion)),
            ] if marker.message == "<EXTERNAL SESSION IMPORTED>"
                && completion.turn_id.starts_with("external-import-turn-")
                && completion.error.is_none()
                && event.rate_limits.is_none() =>
            {
                let info = event.info.as_ref()?;
                let total_tokens = info.last_token_usage.total_tokens;
                (total_tokens > 0
                    && info.last_token_usage == TokenUsage { total_tokens, ..TokenUsage::default() }
                    && info.total_token_usage == info.last_token_usage
                    && info.last_usage_includes_reasoning.is_none()
                    && info.model_context_window.is_none())
                .then_some(total_tokens)
            }
            _ => None,
        }
    });
    let original_suffix = &without_checkpoint[boundary..];
    let eligible = import_estimate.is_some()
        && Session::last_token_info_from_rollout(original_suffix).is_none()
        && Session::last_token_usage_record_from_rollout(&without_checkpoint).is_none();
    let total_history = recovered.history.iter().fold(0_i64, |total, env| {
        total.saturating_add(crate::context_manager::estimate_item_token_count(&env.item))
    });
    let mut startup = ContextManager::default();
    // Resume installs HistoryReplacement::Reset, which calls replace_annotated
    // and aligns the unreported tail to the latest replayed model output.
    startup.replace_annotated(recovered.history.clone());
    let unreported = startup.estimated_tokens_since_last_usage();
    let covered = total_history.checked_sub(unreported).expect("usage boundary");
    let info = Session::last_token_info_from_rollout(&items);
    let seeded = info.as_ref().map(|info| info.last_token_usage.total_tokens);
    if rebased {
        assert!(eligible, "an authoritative usage record supersedes the import estimate");
        let Some(RolloutItem::EventMsg(EventMsg::TokenCount(reset))) = items.get(boundary + 1) else {
            panic!("missing isolated startup token rebase event");
        };
        let stored = reset.info.as_ref().expect("local token estimate");
        assert!(reset.rate_limits.is_none());
        assert!(stored.total_token_usage == TokenUsage::default());
        assert!(stored.last_token_usage == TokenUsage { total_tokens: covered, ..TokenUsage::default() });
        assert_eq!(stored.last_usage_includes_reasoning, Some(true));
        assert!(stored.model_context_window.is_none());
        assert_eq!(seeded, Some(covered));
    } else if eligible {
        assert_eq!(seeded, import_estimate);
    }
    startup.set_token_info(info);
    let startup_tokens = startup.get_total_token_usage();
    if rebased {
        assert_eq!(startup_tokens, total_history);
    }
    let summarize = |history| {
        let mut context = ContextManager::default();
        context.replace_annotated(history);
        context.normalize_history(&[InputModality::Text, InputModality::Image, InputModality::Audio]);
        let history = context.into_annotated_items();
        let tokens = history.iter().fold(0_i64, |total, env| {
            total.saturating_add(crate::context_manager::estimate_item_token_count(&env.item))
        });
        serde_json::json!({"normalized_model_items": history.len(), "estimated_tokens": tokens})
    };
    let metrics = serde_json::json!({
        "before": summarize(original.history),
        "after": summarize(recovered.history),
        "startup": {
            "original_import_estimate_tokens": import_estimate,
            "selected_last_usage_tokens": seeded,
            "covered_history_estimate_tokens": covered,
            "unreported_tail_estimate_tokens": unreported,
            "total_replayed_history_estimate_tokens": total_history,
            "startup_context_usage_tokens": startup_tokens,
            "eligible_import_estimate": eligible,
            "uses_local_rebase": rebased,
            "post_import_rollout_items": original_suffix.len(),
            "post_import_response_items": original_suffix.iter()
                .filter(|item| matches!(item, RolloutItem::ResponseItem(_))).count(),
        },
    });
    println!("SESSION_DOCTOR_NATIVE_METRICS {metrics}");
}
