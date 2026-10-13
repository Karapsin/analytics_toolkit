# Test stability audit and migration map

The audit scanned the full offline test tree and sampled support/contract tests in
every public area. The initial inventory had 3,496 test functions, 2,430 direct
monkeypatch calls, seven direct `inspect.getsource` calls, and 30 direct
`time.sleep`/`asyncio.sleep` calls (including integration). Counts are scan
indicators, not measured failure rates; aliased sleeps were examined separately.

## Classification and retained contracts

| Area | Classification and disposition |
| --- | --- |
| SQL orchestration and AB parallel/planning | Scheduling assumptions migrated to real-worker overlap checks and held cap checks; public facade calls and domain operation-fake seams reduce internal coupling. |
| SQL transfer runtime and partition maintenance | Focused private algorithm contracts retained; observe actual queue retries, condition waits and completed failures instead of delaying the test thread. |
| SQL architecture | Ownership, removed-import and dispatch rules retained as AST checks. Backend comparison counting now also detects reversed operands and tuple membership; five existing configuration-default comparisons are explicitly allowed. |
| Explorer | Shared metadata provider/coordinator setup moved to `_support`; unnecessary fake delays removed; journal start polling replaced by an Event wait that leaves the UI responsive. An area fixture returns empty offline metadata for unattended discovery and rejects unfaked user queries. |
| SQL drivers/comparisons | Existing scripted connections and SQL token comparisons retained, including negative cases for values, identifiers, parameters, order and cleanup. No broader SQL normalization. |
| AB statistics | Seeded randomness, independent statistical references, exported underscore compatibility and process/thread equivalence retained. Numerical tolerances unchanged. |
| Excel, dates/datetime and general | Cell formatting/order, date semantics, explicit clocks and project-path behavior retained; existing factories already provide isolated state. |
| DataLens | Typed SDK-local fakes and offline conformance against both supported SDK versions retained. No optional SDK imports added to common support. |
| Agent/release/repository | Workflow provenance/security, packaging, version and coverage-ratchet contracts retained. Exact policy checks are intentional; no new YAML parser dependency added. |
| Integration | Disposable-service scenarios and manifest unchanged; no local integration run. |

Private patches that isolate failures, algorithms, compatibility exports or genuine
external boundaries remain intentional. This migration does not replace every
private-helper test with a public API test or change expectations merely to make
a refactor pass. Common helpers contain no optional SDK/UI dependencies.

## Case preservation map

No existing test function was removed or renamed. Import-only consumers of the
moved Explorer support preserve their bodies and are omitted below. New support
regressions test insufficient/excess concurrency, cleanup after errors, failed
blocking attempts, mode isolation/restoration, aliases, quotes, comments,
multiline expressions, forbidden imports and SQL literals.

The coverage ratchet exposed a prefetch retry branch that a start-only signal
could miss. That case now observes the semaphore actually returning `False`
before releasing it. Coverage logs also exposed synthetic-host connection
attempts during unattended Explorer discovery; the default offline metadata
fixture and override-contract tests replace that environmental dependency.

| Existing case | Replacement and preserved intent |
| --- | --- |
| `tests/repository/support.py::test_common_support_imports_do_not_load_optional_sdks` | Extend the existing isolated import check to every new common helper; optional SDK/UI modules remain blocked. |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_fail_fast_false_returns_exception_strings` | Public facade and centralized operation fake; original expectations retained |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_fail_fast_raises_original_exception` | Public facade and centralized operation fake; original expectations retained |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_inserts_labels_as_leading_columns` | Public facade and centralized operation fake; original expectations retained |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_limits_concurrency` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_lower_soft_cap_avoids_hard_cap_error` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_maps_pre_exp_df_and_honors_task_kwargs` | Public facade and centralized operation fake; original expectations retained |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_runs_tasks_and_preserves_input_order` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_soft_cap_limits_worker_execution` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/ab_utils/parallel/execution.py::test_parallel_compute_metrics_updates_progress_bar` | Public facade and centralized operation fake; original expectations retained |
| `tests/ab_utils/parallel/validation.py::test_parallel_compute_metrics_rejects_label_result_column_conflict` | Public facade and centralized operation fake; original expectations retained |
| `tests/ab_utils/planning/mde/sql_1.py::test_compute_mde_from_sql_parallelizes_day_size_after_validation` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/architecture/contract.py::test_adapter_owned_sql_fragments_stay_out_of_cleaned_generic_modules` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_backend_adapters_do_not_depend_on_generic_write_mode_or_type_maps` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_backend_policy_sql_templates_stay_backend_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_dataframe_type_inference_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_generic_analyze_support_policy_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_generic_clickhouse_callers_delegate_to_adapters` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_generic_identifier_helpers_use_backend_adapters` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_generic_io_backend_dispatch_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_generic_load_and_transfer_do_not_import_concrete_config_policy` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_generic_sql_backend_branch_debt_does_not_increase` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_generic_sql_modules_do_not_pin_literal_backend_sets` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_legacy_sql_compatibility_paths_are_removed` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_load_insert_backend_serialization_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_load_sql_insert_dispatch_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_parquet_load_stage_uses_target_backend_adapter` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract.py::test_show_tables_catalog_filter_policy_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract_2.py::test_sql_source_does_not_restore_removed_aggregation_imports` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract_2.py::test_stage_table_identifier_backend_policy_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract_2.py::test_table_info_name_resolution_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract_2.py::test_transfer_insert_page_sizing_policy_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract_2.py::test_trino_parquet_stage_sql_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/contract_2.py::test_upsert_backend_policy_is_capability_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/creation.py::test_create_table_clickhouse_option_policy_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/architecture/validation.py::test_trino_insert_chunk_size_validation_is_adapter_owned` | AST ownership/dependency rules replace source spelling; public/export checks retained |
| `tests/sql/backends/adapters/facade.py::test_sql_backend_dispatch_uses_adapter_boundary` | Adapter dispatch/argument forwarding exercised for all three backends; legacy guards retained |
| `tests/sql/backends/gp/partitions/errors.py::test_gp_analyze_partitioned_table_stops_scheduling_after_concurrent_failure` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/dml/io/execute/concurrency.py::test_execute_sql_list_returns_results_in_input_order` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/dml/io/execute/concurrency.py::test_execute_sql_soft_cap_limits_active_workers` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/dml/transfer/runtime/concurrency.py::test_connection_pool_concurrent_operations_never_exceed_capacity` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/dml/transfer/runtime/concurrency.py::test_queue_helpers_complete_successfully_and_get_honors_cancellation` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/dml/transfer/runtime/concurrency.py::test_waiting_connection_lease_honors_cancellation` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/dml/transfer/runtime/contract.py::test_assignment_slot_and_release_helpers_honor_state` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/dml/transfer/runtime/errors.py::test_queue_put_helpers_retry_full_queue_then_honor_cancellation` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/dml/transfer/runtime/validation.py::test_close_waits_for_inflight_open_and_rejects_late_connection` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/explorer/completion.py::test_coordinator_keyword_and_idle_worker_paths` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/explorer/journal_screen.py::test_cancelled_journal_workers_do_not_publish_late_results` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/async_sql/cancellation.py::test_async_sql_keyboard_interrupt_stops_queued_tasks` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/async_sql/cancellation.py::test_existing_loop_bridge_propagates_caller_interrupt_to_runner` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/async_sql/concurrency.py::test_async_sql_concurrency_limits_active_tasks` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_blank_and_none_start_comment_are_noops` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_dispatches_supported_task_types_and_preserves_order` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_fail_fast_false_returns_exceptions` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_fail_fast_false_returns_pipeline_exception` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_fail_fast_raises_first_exception` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_pipeline_can_run_nested_sync_async_sql` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_pipeline_runs_steps_sequentially_and_returns_last_result` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_pipeline_stops_on_first_step_exception` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_runs_from_inside_existing_event_loop` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_soft_cap_limits_top_level_worker_execution` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/async_sql/contract.py::test_async_sql_start_comment_does_not_change_load_df_or_pipeline` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract_2.py::test_async_sql_start_comment_prefixes_sql_fields` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract_2.py::test_async_sql_sync_task_dispatch_uses_callable_registry` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract_2.py::test_async_sql_task_context_task_id_is_included_in_task_output` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract_2.py::test_async_sql_task_start_comment_overrides_default` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/contract_2.py::test_async_sql_uses_generated_names_for_unnamed_task_sequence` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/errors.py::test_async_sql_fail_fast_false_prints_failed_task_query` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/errors.py::test_async_sql_fail_fast_true_prints_failed_transfer_sql` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/errors.py::test_async_sql_lower_soft_cap_avoids_hard_cap_error` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/async_sql/errors.py::test_async_sql_task_context_task_id_is_included_for_failed_tasks` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/progress.py::test_async_sql_suppresses_builtin_inner_progress` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/progress.py::test_async_sql_updates_progress_bar` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_default_hard_cap_rejects_six_workers` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_hard_cap_rejects_unthrottled_effective_concurrency` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_rejects_duplicate_task_names` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_rejects_non_string_task_start_comment` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_rejects_non_string_top_level_start_comment` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_rejects_removed_connection_task_arguments` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_rejects_removed_random_sleep_seconds` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_validates_concurrency` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_validates_hard_concurrency_cap` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_validates_pipeline_extra_fields` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_validates_pipeline_steps` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_validates_progress` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_validates_soft_concurrency_cap` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/async_sql/validation.py::test_async_sql_validates_task_input` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/concurrency.py::test_parallel_sql_concurrency_limits_active_top_level_work` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_dispatches_supported_task_types_and_preserves_order` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_fail_fast_false_returns_exceptions` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_fail_fast_raises_first_exception` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_nested_pipeline_respects_soft_cap` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_pipeline_runs_steps_sequentially_and_returns_last_result` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_soft_cap_limits_actual_worker_execution` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_soft_cap_limits_pipeline_step_execution` | Explicit rendezvous, completion or blocked-state signal; ordering/cap/cancellation assertions retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_start_comment_prefixes_sql_fields` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/contract.py::test_parallel_sql_task_context_task_id_is_included_in_task_output` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/contract_2.py::test_parallel_sql_uses_generated_names_for_unnamed_task_sequence` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/errors.py::test_parallel_sql_fail_fast_false_prints_failed_task_query` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/errors.py::test_parallel_sql_fail_fast_true_prints_failed_transfer_sql` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/errors.py::test_parallel_sql_task_context_task_id_is_included_for_failed_tasks` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/progress.py::test_parallel_sql_updates_progress_bar` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_default_hard_cap_rejects_six_workers` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_hard_cap_rejects_excessive_nested_concurrency` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_rejects_async_pipeline_steps` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_rejects_duplicate_task_names` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_rejects_removed_connection_task_arguments` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_validates_concurrency` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_validates_hard_concurrency_cap` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_validates_progress` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_validates_soft_concurrency_cap` | Public facade and centralized operation fake; original expectations retained |
| `tests/sql/orchestration/parallel_sql/validation.py::test_parallel_sql_validates_task_input` | Public facade and centralized operation fake; original expectations retained |

## Intentional real-time cases

The cursor-blink test crosses a real timer interval; the navigation notice test
crosses the periodic status-refresh interval. Both keep explicit reviewed
exceptions in the repository guard. Zero-duration asyncio sleeps only yield the
event loop. Metadata predicate polling is bounded in domain support because
those production worker APIs do not expose a completion Event. New worker-order
assumptions must use explicit signals rather than another sleep.

## Review rule

For a refactor-only change, explain any changed behavior expectation by naming
the contract that changed. Prefer updating a domain fake seam over many test
patch locations. Keep architecture checks in syntax/dependency terms and retain
strict public output, SQL safety, statistical and security expectations. A
changed fake must have a positive supported-interface case and a negative
regression case; keep minimum/latest and SDK conformance environments enabled.

## Cap-check release protocol and correction regressions

Overlap tests keep the original rendezvous mode. SQL async/threaded orchestration,
nested threaded pipelines, SQL execute batches and AB metric cap tests use held
workers with domain scheduling observation. Submission alone cannot release the
workers: each candidate in actual initial executor capacity must enter the leaf
operation or fail a real nonblocking thread-semaphore acquisition. The observer
then performs the normal blocking acquire. Async contention is observed with
`locked()` on the owning event loop before the normal acquire, preserving waiter
fairness, release and cancellation. Async patches are scoped to the tested call.
Candidates are deduplicated, and outer nested-pipeline work is excluded.

The controller waits for scheduling evidence and the expected worker count,
then checks the maximum while workers are still held. Every exit releases workers
before a bounded join; exceptions propagate and timeouts fail. Partial final
waves continue after release and retain original result/count/order assertions.

| Correction case | Regression intent |
| --- | --- |
| Repository held-probe cases | Detect excess workers with the consumer harness alone; fail insufficient parallelism; clean up after worker or controller errors; handle partial final waves. |
| Repository public SQL limiter bypass (threaded, nested, async) | Ignore the soft limiter intentionally and require the same consumer harness to reject excess concurrency, without a second worker barrier or scheduling delay. |
| Repository public executor capacity bypass (AB metrics, SQL execute) | Oversize the real pool and bypass the AB semaphore; require rejection of the extra operation worker. Retain a positive oversized-AB-pool case proving real semaphore contention enforces the cap. |
| Parquet stage wrapper defaults/explicit options | Select Trino once, forward positional and optional arguments to one adapter call, and return its unchanged SQL. |
| Parquet architecture ownership | Retain structural forbidden-code rules and restore delegation checks for all three generic wrappers. |

GitHub must run these focused offline cases and migrated consumers before the
existing full Python/dependency/SDK and quality matrix. Earlier CI success for
the original migration does not validate this correction.
