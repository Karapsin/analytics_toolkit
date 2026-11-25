[SQL module index](index.md)

# SQL correctness audit closure

The audit is a reproduction inventory. This table records the selected contracts
and their fast regression evidence. Author regression results do not constitute
source approval. Publication requires complete managed checks, independent
source approval for the final candidate, and then the SQL Explorer visual-review
receipt. Disposable
integration cases are registered in the
[SQL coverage manifest](../../../integration/sql_coverage_manifest.json); they
are advisory during ordinary development and were not run locally for this repair.

Finding 28 uses partition-scoped keys: a partition column augments the keys, so
the same ID in two partitions represents two records. Findings 65 and 66 retain
at-least-once data-write retries, including lost acknowledgements. They do not
promise duplicate elimination. Structural cutover uncertainty retains recovery
artifacts and does not authorize blind replay. Finding 64 is repaired as a
correctness bug: closing a connection cannot cause successful work to replay.

| ID | Audit finding | Disposition | Regression node (`tests/sql/` unless qualified) |
| --- | --- | --- | --- |
| 1 | CH routing breaks qualified columns | fixed | `backends/ch/routing_contract.py::test_routing_preserves_bound_qualified_columns_and_stars`; `backends/ch/source_bindings.py::test_same_scope_database_qualified_sources_remain_distinct`; `backends/ch/function_insert.py::test_routing_keeps_function_target_columns_and_source_bindings` |
| 2 | Explorer database discovery fails with routing or healthy-shard mode | fixed | `explorer/completion.py::test_backend_namespace_discovery_uses_serial_metadata_queries` |
| 3 | Explorer persists incomplete metadata as complete | fixed | `explorer/metadata_identity.py::test_partial_catalog_never_prunes_complete_snapshot_and_recovers_without_usage` |
| 4 | Explicit all-replica queries can silently return partial results | fixed | `backends/ch/routing_contract.py::test_explicit_all_replicas_requires_physical_coverage_without_automatic_routing` |
| 5 | Healthy-shard mode can reject safe replica failover writes | fixed | `backends/ch/routing_contract.py::test_surviving_replica_in_every_shard_accepts_deterministic_write_key` |
| 6 | Valid two-argument cluster functions are rejected | fixed | `backends/ch/routing_contract.py::test_two_argument_cluster_functions_parse_quoted_components_and_preserve_extras`; `backends/ch/function_insert.py::test_function_target_columns_roundtrip_without_consuming_source`; `backends/ch/function_insert.py::test_public_outage_function_insert_with_columns_reaches_real_command` |
| 7 | Schema comparison hides meaningful literal differences | fixed | `backends/ch/routing_contract.py::test_canonical_ddl_preserves_literal_whitespace_but_ignores_permitted_identity` |
| 8 | Quoted database names containing dots break replica inspection | fixed | `backends/ch/routing_contract.py::test_replica_inspection_keeps_dotted_database_as_one_identity` |
| 9 | Routed `show_tables` returns indistinguishable replica duplicates | fixed | `backends/ch/logical_statistics.py::test_routed_show_tables_deduplicates_exact_identity_and_keeps_case` |
| 10 | Distributed-table statistics are inflated by replica count | fixed | `backends/ch/logical_statistics.py::test_logical_statistics_counts_replicated_shards_and_independent_hosts` |
| 11 | Transfer count limiting corrupts limited UNION queries | fixed | `backends/ch/routing_contract.py::test_count_limited_read_caps_outer_query_and_retains_existing_limits` |
| 12 | Per-host drop recovery loses endpoint ports | fixed | `connection/config/exact_credentials.py::test_recovery_uses_discovered_native_port_only` |
| 13 | Explorer merges distinct identifiers | fixed | `explorer/metadata_identity.py::test_exact_names_survive_sql_result_and_sqlite_roundtrip` |
| 14 | Explorer suggests other databases’ tables without qualification | fixed | `explorer/metadata_identity.py::test_persisted_unqualified_clickhouse_completion_uses_current_database` |
| 15 | Explorer completion loses required quoting | fixed | `explorer/metadata_identity.py::test_metadata_identifier_insertion_replaces_entire_quoted_fragment` |
| 16 | CH Decimal inserts silently lose precision | fixed | `core/exact_values.py::test_nullable_integer_and_decimal_never_cross_float` |
| 17 | HTTP inserts round nullable large integers | fixed | `core/exact_values.py::test_nullable_integer_and_decimal_never_cross_float` |
| 18 | Native transfers have the same nullable-integer corruption | fixed | `backends/ch/exact_transport.py::test_native_stream_header_is_consumed_once_and_tuple_cells_are_rows` |
| 19 | Native tuple-valued results can be mistaken for metadata | fixed | `backends/ch/exact_transport.py::test_real_driver_iter_query_result_header_contract` |
| 20 | CH nanosecond timestamps lose precision in two paths | fixed | `backends/ch/exact_transport.py::test_binary_temporal_ticks_remain_exact_for_both_transports`; `backends/ch/temporal_source_mode.py::test_native_exact_source_eager_stream_nested_fold_and_reconnect`; `dml/transfer/schema/write_timezone.py::test_transfer_preserves_both_source_dst_fold_instants_on_streaming_path` |
| 21 | Unsigned dataframe integers infer as signed `Int64` | fixed | `core/exact_values.py::test_unsigned_dataframe_inference_preserves_clickhouse_family` |
| 22 | Object-dtype datetimes infer as dates | fixed | `core/exact_values.py::test_dataframe_inference_preserves_decimal_datetime_and_date32` |
| 23 | Wide CH integers map incorrectly within CH | fixed | `core/type_resolution.py::test_clickhouse_native_type_identity_survives_structural_mapping` |
| 24 | Nested CH types are misclassified as scalar integers | fixed | `core/type_resolution.py::test_clickhouse_native_type_identity_survives_structural_mapping` |
| 25 | `Date32` targets narrow to `Date` | fixed | `core/type_resolution.py::test_clickhouse_native_type_identity_survives_structural_mapping` |
| 26 | HTTP transfers reject several valid DateTime64 precisions | fixed | `backends/ch/exact_transport.py::test_http_stream_uses_column_blocks_and_preserves_duplicate_names`; `backends/ch/temporal_source_mode.py::test_http_exact_source_eager_stream_nested_fold_and_client_isolation` |
| 27 | Partial-column CH upserts erase untouched fields | fixed | `dml/load/upsert_rows.py::test_existing_upsert_rejects_missing_default_or_extra_before_stage` |
| 28 | Moving an upsert key between partitions leaves stale rows | accepted partition-scoped key contract | `dml/load/upsert_rows.py::test_reordered_complete_upsert_is_canonical_and_partition_scoped` |
| 29 | Key-validated append can swap columns | fixed | `dml/load/upsert_rows.py::test_key_validated_append_uses_explicit_subset_column_order`; `dml/load/append_plan_columns.py::test_key_validated_append_plan_names_and_projects_incoming_columns`; `dml/load/append_plan_columns.py::test_parquet_append_plan_names_incoming_columns_and_cleans_owned_resources` |
| 30 | Several writes ignore local-only CH policy | fixed | `backends/ch/reconfigure/structural_safety.py::test_local_layout_never_owns_a_suffix_companion`; `dml/load/upsert_storage_scope.py::test_load_df_upsert_mutates_only_verified_existing_storage_scope`; `dml/load/upsert_storage_scope.py::test_transfer_finalization_upsert_mutates_only_verified_existing_storage_scope`; `dml/transfer/replacement_scope.py::test_public_transfer_replacement_rejects_unowned_physical_scope_before_cutover`; `dml/transfer/replacement_scope.py::test_public_transfer_replacement_accepts_a_verified_native_endpoint_alias`; `dml/transfer/replacement_scope.py::test_public_transfer_replacement_finishes_pair_to_remote_local_conversion`; `dml/transfer/replacement_scope.py::test_public_transfer_replacement_retains_completed_conversion_on_missing_target_metadata`; `backends/ch/storage_clauses.py::test_public_creation_default_clauses_match_selected_engine`; `backends/ch/storage_clauses.py::test_public_memory_keys_reject_before_replacement`; `dml/transfer/replacement_scope.py::test_public_transfer_memory_stage_uses_only_supported_storage_clauses`; `dml/transfer/facade_scope.py::test_public_transfer_rejects_replacement_that_leaves_an_original_peer_facade`; `dml/transfer/facade_scope.py::test_public_transfer_accepts_an_equivalent_complete_facade_scope_alias`; `backends/ch/local_function_insert.py::test_public_execute_keeps_explicit_function_insert_on_its_verified_remote_scope` |
| 31 | Query-based creation drops resolved CH creation policy | fixed | `backends/ch/policy_ctas.py::test_public_query_policy_keeps_independent_ddl_and_routing_scopes` |
| 32 | `ch_replace_table=True` is discarded with `sql=...` | fixed | `backends/ch/policy_ctas.py::test_public_query_replace_flag_controls_sql_plan_without_implying_drop`; `backends/ch/policy_ctas.py::test_query_sql_generation_preserves_replace_mode` |
| 33 | Default query creation can append to an existing table | fixed | `backends/ch/policy_ctas.py::test_public_query_creation_existing_modes_never_accidentally_append` |
| 34 | Reconfiguration cleanup can delete a pre-existing `_shard` | fixed | `backends/ch/reconfigure/structural_safety.py::test_local_to_pair_rejects_a_preexisting_companion_before_ddl` |
| 35 | Cross-cluster migration can copy rows into the live source | fixed | `backends/ch/reconfigure/structural_safety.py::test_relocation_rejects_copy_routing_to_original_storage` |
| 36 | Partial rename rollback can leave the public table missing | fixed | `backends/ch/reconfigure/structural_safety.py::test_count_failure_rolls_back_acknowledged_cutovers_in_reverse_order` |
| 37 | Conversion retains invalid MergeTree clauses on Distributed facades | fixed | `backends/ch/reconfigure/structural_safety.py::test_replacement_create_is_plain_and_facade_excludes_physical_properties`; `backends/ch/reconfigure/structural_safety.py::test_local_to_pair_reconfiguration_removes_only_facade_column_ttl` |
| 38 | Reconfiguration changes `rand()` into `randCanonical()` | fixed | `backends/ch/routing_contract.py::test_rand_and_randcanonical_remain_distinct_in_routed_defaults` |
| 39 | Guarded replacement loses optional Distributed engine arguments | fixed | `backends/ch/replace_routing.py::test_guarded_replacement_preserves_configured_distributed_template_arguments` |
| 40 | Readiness failures leak created stages | fixed | `dml/load/stage_ownership.py::test_load_readiness_failure_cleans_acknowledged_partial_pair_only`; `dml/transfer/row_batches/stage_ownership.py::test_keyed_worker_readiness_failure_cleans_acknowledged_stage_before_return`; `dml/transfer/row_batches/stage_registration.py::test_keyed_initialization_registers_partial_pair_before_readiness_returns`; `backends/ch/availability_writes.py::test_public_writes_finish_with_only_acknowledged_optional_facade_resources` |
| 41 | CH string-key literals mishandle backslashes | fixed | `dml/transfer/row_batches/predicate_literals.py::test_clickhouse_literals_escape_backslashes_and_preserve_precise_instants` |
| 42 | CH datetime transfer keys fail with fractions or timezone offsets | fixed | `dml/transfer/row_batches/predicate_literals.py::test_clickhouse_literals_escape_backslashes_and_preserve_precise_instants`; `dml/transfer/row_batches/predicate_literals.py::test_raw_clickhouse_key_literals_use_ticks_instead_of_naive_display` |
| 43 | Transfer placeholders ignore SQL context | fixed | `dml/transfer/row_batches/predicate_literals.py::test_inserted_placeholder_text_is_never_scanned_again` |
| 44 | Collision-generated stages evade stale cleanup | fixed | `dml/transfer/row_batches/predicate_literals.py::test_generated_collision_suffix_is_recognized` |
| 45 | Initial target failures leak source connections | fixed | `dml/transfer/row_batches/initialization_cleanup.py::test_target_initialization_failure_closes_acquired_source_exactly_once` |
| 46 | CH unsigned small integers narrow incorrectly in Trino | fixed | `core/type_resolution.py::test_cross_backend_mapping_never_uses_a_narrow_integer_domain` |
| 47 | Wide CH integers narrow to BIGINT in GP and Trino | fixed | `core/type_resolution.py::test_adaptive_inference_uses_complete_actual_values_before_text` |
| 48 | Trino timestamp inference uses precision 3 | fixed | `core/type_resolution.py::test_cross_backend_mapping_never_uses_a_narrow_integer_domain`; `dml/transfer/schema/write_timezone.py::test_inferred_cross_backend_ch_timestamps_preserve_physical_instants` |
| 49 | Trino identifiers containing `?` break typed inserts | fixed | `backends/insert_schema/identifier_markers.py::test_typed_trino_insert_does_not_rewrite_question_marks_in_identifiers_or_label` |
| 50 | GP identifiers containing `%` break dataframe inserts | fixed | `backends/insert_schema/identifier_markers.py::test_greenplum_execute_values_template_escapes_identifier_and_label_percents` |
| 51 | Trino metadata helpers discard identifier quoting | fixed | `backends/adapters/metadata_identifiers.py::test_trino_metadata_quotes_each_catalog_and_relation_component` |
| 52 | GP partition analysis loses identifier boundaries | fixed | `backends/adapters/metadata_identifiers.py::test_gp_partition_discovery_preserves_raw_dotted_and_quoted_names` |
| 53 | Trino Parquet staging mishandles timezone timestamps | fixed | `dml/transfer/parquet/exact_schema.py::test_dataframe_and_row_writers_apply_identical_uuid_timezone_and_numeric_schema` |
| 54 | Trino Parquet dataframe loads fail on UUID objects | fixed | `dml/transfer/parquet/exact_schema.py::test_dataframe_and_row_writers_apply_identical_uuid_timezone_and_numeric_schema` |
| 55 | Fallback Trino Decimal inference computes insufficient precision/scale | fixed | `dml/transfer/parquet/exact_schema.py::test_parquet_fallback_inference_scans_the_whole_column` |
| 56 | Credential whitespace is changed | fixed | `connection/config/exact_credentials.py::test_password_contents_survive_all_resolution_paths` |
| 57 | Distinct aliases can overwrite one CA bundle | fixed | `connection/config/exact_credentials.py::test_certificate_bundles_are_atomic_content_addressed_and_immutable` |
| 58 | String `verify` values can disable HTTPS verification | fixed | `connection/config/exact_credentials.py::test_clickhouse_verify_rejects_non_boolean_configuration` |
| 59 | The native extra omits compression dependencies | fixed | `release_routines/lib/artifact_smoke.py::_verify_installed_artifact (fresh wheel and sdist environments; lz4, zstd, CityHash)` |
| 60 | Connection diagnostics can falsely report Trino connectivity | fixed | `connection/config/connectivity_probe.py::test_connectivity_requires_consumed_probe_and_closes_once` |
| 61 | Nested async execution can deadlock | fixed | `orchestration/async_sql/nested_capacity.py::test_fully_saturated_nested_batches_share_capacity_across_loops` |
| 62 | `insert`/`execute_insert` discard affected-row counts | fixed | `dml/io/execute/outcomes.py::test_real_adapter_insert_counts_final_statement_only` |
| 63 | Default `execute_read` queries cannot be matched for cancellation | fixed | `dml/io/execute/outcomes.py::test_execute_read_adds_active_cancel_marker_without_label` |
| 64 | Cleanup failures can replay successful work | fixed | `dml/io/execute/outcomes.py::test_execute_read_connection_close_does_not_replay_or_mask_primary` |
| 65 | Direct Trino append retries can duplicate committed chunks | accepted at-least-once retry behavior | `dml/load/chunk_replay.py::test_trino_partial_chunk_replay_is_explicit_at_least_once` |
| 66 | Native CH passthrough has an ambiguous-write replay risk | accepted replay risk; consistent ClickHouse retries | `backends/ch/outage_replay.py::test_clickhouse_lost_ack_data_writes_have_explicit_at_least_once_semantics` |

Additional foundation regressions cover complete input profiles and certified
spools, tagged exact text, authoritative-schema rejection, duplicate result
labels, and schema-driven physical Parquet types in `tests/sql/core/` and
`tests/sql/dml/transfer/parquet/exact_schema.py`.

Timezone certification and inference are additionally covered by
`core/destination_timezone.py::test_canonical_iana_rules_include_future_dst_and_historical_offsets`,
`dml/transfer/schema/write_timezone.py::test_certified_transfer_cannot_rebind_to_a_different_timezone_during_retry`,
`dml/transfer/schema/write_timezone.py::test_load_rejects_ambiguous_or_missing_naive_wallclock_before_truncate`,
`dml/transfer/schema/write_timezone.py::test_inferred_transfer_preserves_unresolved_dst_wallclock_as_text`,
and `dml/transfer/schema/write_timezone.py::test_inferred_cross_backend_ch_timestamps_preserve_physical_instants`.
These cover the actual write context, strict authoritative validation, exact
text fallback, and recursive preservation of physical instants without
rewriting the source SQL types or inventing a DST fold.

Destination-native map-key identity is additionally covered by
`core/map_key_conversion.py::test_trino_uuid_key_collision_rejects_before_dictionary_construction`,
`core/map_key_conversion.py::test_unique_map_keys_use_final_native_identity_without_changing_scalar_values`,
`dml/transfer/schema/map_keys.py::test_transfer_rejects_map_keys_that_collide_after_authoritative_uuid_conversion`,
`dml/transfer/schema/map_keys.py::test_transfer_rejects_map_keys_that_collide_in_final_native_column_before_mutation`,
and `dml/transfer/schema/map_keys.py::test_load_existing_map_rejects_converted_uuid_key_collision_before_truncation`.
These include recursive maps, UUID normalization, `REAL` rounding and `CHAR`
padding, while preserving legal ClickHouse duplicate pairs and inferred maps
whose string keys remain distinct.

Map-key metadata proofs and the explicit-stage/existing-target boundary are
covered by
`core/map_key_domains.py::test_map_only_restriction_recurses_without_changing_scalar_char_conversion`,
`dml/transfer/schema/map_keys.py::test_trino_bounded_map_keys_are_validated_before_char_target_mutation`,
`dml/transfer/schema/map_keys.py::test_trino_safe_map_key_identity_keeps_same_type_and_widening_lazy_streams`,
and `dml/transfer/schema/map_keys.py::test_unique_trino_char_map_conversion_freezes_and_loads_without_changing_stage_schema`.
These require distinct destination-native keys across both conversions, reject
collisions before mutation, and retain safe lazy paths and valid stage schemas.

Composite-key equality is additionally covered by
`core/composite_map_keys.py::test_composite_key_collisions_are_detected_before_materialization`,
`dml/transfer/schema/composite_map_keys.py::test_composite_map_key_collision_stops_transfer_before_mutation`,
`dml/transfer/schema/composite_map_keys.py::test_composite_map_key_collision_stops_load_before_truncate_create_or_insert`,
and `dml/transfer/schema/composite_map_keys.py::test_same_and_widened_composite_keys_certify_source_before_publication`.
These check destination-native equality inside `ROW` key fields, including
`CHAR` padding and `REAL` rounding, without losing valid entries or changing
the caller's stage schema.

Same-scope cross-database source bindings are additionally covered by
`backends/ch/source_bindings.py::test_same_scope_explicit_aliases_remain_authoritative`
and `backends/ch/source_bindings.py::test_same_scope_sources_remain_bound_across_cte_shadowing_and_correlation`.
The existing disposable scenario `audit.clickhouse.routing_metadata` includes
`integration/core/audit_contracts.py::test_clickhouse_routing_bindings_and_current_database_metadata`
for same-named tables in two databases, qualified projections/stars, and both
ClickHouse transports. It remains advisory and was not run locally.

Empty public metadata listings are covered by
`backends/ch/empty_listings.py::test_table_listing_keeps_coverage_for_empty_and_nonempty_results`
and `backends/ch/empty_listings.py::test_other_backend_table_listing_shapes_and_metadata_defaults_are_preserved`.
Both complete and partial ClickHouse listings retain coverage attributes on
empty results; canonical shapes and other backend defaults remain intact.

Existing-storage upsert scope and offline planning are additionally covered by
`dml/load/upsert_storage_scope.py::test_existing_layout_rewrites_prebuilt_partition_scope_without_changing_other_sql`,
`dml/load/upsert_storage_scope.py::test_outage_checkpoint_replay_respects_verified_local_or_selected_cluster_scope`,
`dml/load/upsert_storage_scope.py::test_upsert_dry_plan_keeps_desired_scope_without_querying_existing_storage`,
and `dml/load/upsert_storage_scope.py::test_transfer_upsert_dry_plan_defers_existing_storage_verification`.
Target preservation reads, partition mutation, final inserts and prepared-payload
replay use the same verified physical storage scope; stage policies remain
independent. Stateful coverage is provided by
`dml/load/upsert_storage_scope.py::test_routed_upsert_preserves_target_rows_and_unrelated_scope`
and `dml/load/upsert_storage_scope.py::test_load_df_upsert_keeps_all_target_operations_on_verified_storage`.
Offline plans preserve
desired policy and explicitly defer existing-layout verification. The existing
disposable `audit.upsert.ch` scenario also covers actual storage cluster `B`
with an invalid desired caller cluster `A` and independent local staging on both
ClickHouse transports; it was not run locally.

Backend literal regressions are additionally covered by
`dml/load/trino_partition_literals.py::test_load_df_upsert_quotes_actual_partition_strings`
and `dml/load/trino_partition_literals.py::test_unknown_partition_values_keep_only_the_synthetic_dry_plan_marker`.
Actual Trino string partition values remain data even when they resemble plan
markers; only an unknown partition sequence produces the synthetic marker.
`dml/transfer/row_batches/gp_key_literals.py::test_keyed_transfer_selects_timestamp_row_by_emitted_literal_semantics`
checks Greenplum key selection with non-UTC aware inputs and distinct source
instants, retaining UTC-aware and naive controls.

Facade TTL handling is also covered by
`backends/ch/reconfigure/structural_safety.py::test_new_facade_removes_column_ttl_without_mutating_source_ast`
and `backends/ch/reconfigure/structural_safety.py::test_managed_pair_identity_excludes_only_physical_column_ttl`.
Physical TTL declarations and permitted column defaults/comments are retained.
Only column TTL differences are excluded from pair identity; other column
identity checks remain active.
The disposable scenario `audit.clickhouse.column_ttl_reconfigure` in
`integration/core/audit_reconfiguration.py::test_local_to_pair_keeps_column_ttl_only_on_physical_storage`
checks facade/shard DDL and unexpired rows; it was not run locally.

Complete encoded-text bounds are covered by
`core/conversion_profiles.py::test_converted_profile_measures_complete_structured_text`,
`dml/transfer/schema/text_width.py::test_new_bounded_target_rejects_metadata_expanded_structured_text_before_creation`,
`dml/transfer/schema/text_width.py::test_exact_structured_text_width_is_accepted_without_changing_schema`,
`dml/transfer/schema/text_width.py::test_explicit_text_stage_rejects_overflow_even_when_existing_target_is_wider`,
`dml/load/encoded_text_bounds.py::test_encoded_dataframe_overflow_rejects_before_any_target_mutation`,
and `dml/load/encoded_text_bounds.py::test_encoded_dataframe_exact_bound_keeps_sql_null_and_typed_text`.
Python-encoded values must fit their own authoritative schema before target
creation or mutation, including structured-text metadata. Native SQL cast
semantics remain unchanged. The disposable cases
`audit.transfer.encoded_varchar.gp` and `audit.transfer.encoded_varchar.trino` in
`integration/core/audit_encoded_values.py::test_complete_structured_encoding_is_validated_before_creating_a_bounded_target`
cover rejection before CREATE and exact-width success; they were not run locally.

Dataframe input-column validation is additionally covered by
`dml/load/incoming_columns.py::test_unknown_existing_target_columns_fail_before_destructive_load`,
`dml/load/incoming_columns.py::test_duplicate_sql_column_names_fail_before_destructive_load`,
`dml/load/incoming_columns.py::test_single_nonstring_label_is_copied_to_its_sql_name_without_mutating_input`,
`backends/gp/insertable_columns.py::test_gp_generated_columns_are_rejected_before_insert`,
and `backends/ch/insertable_columns.py::test_clickhouse_explicit_insertability_respects_transport_and_effective_setting`.
Unknown and noninsertable incoming fields are rejected before destructive
loading, with backend-supported input-column exceptions retained. Canonical SQL
names must be unique; valid subsets, reordered inputs and replacement schemas
remain supported. The disposable unknown-column cases for all three backends in
`integration/core/audit_dataframe_columns.py::test_unknown_dataframe_columns_are_rejected_before_truncating_existing_rows`
and ClickHouse computed-column cases in
`integration/core/audit_generated_columns.py::test_noninsertable_computed_columns_are_rejected_before_clearing_rows`
were not run locally.

Enum logical-value conversion is additionally covered by
`core/enum_values.py::test_logical_enum_containers_share_native_shape_and_leaf_conversion`,
`dml/load/enum_values.py::test_numeric_enum_values_reach_real_gp_adaptation_as_exact_primitives`,
`dml/load/enum_values.py::test_nested_array_enums_publish_native_integer_and_sql_null_leaves`,
and `dml/load/enum_values.py::test_enum_map_key_collision_rejects_before_clearing_existing_rows`.
Members are normalized through `.value` before certification and conversion,
including nested values. Native exact numbers, nulls and strings keep their
ordinary representations, while map-key collision checks remain active.

Boolean-to-integer certification is additionally covered by
`dml/load/numpy_scalars.py::test_numpy_boolean_integer_load_reaches_real_gp_adaptation_as_zero_and_one`,
`core/numpy_scalars.py::test_numpy_boolean_enum_and_default_inference_keep_their_logical_family`,
and `core/numpy_scalars.py::test_numpy_boolean_does_not_broaden_other_authoritative_numeric_policies`.
Accepted boolean inputs for authoritative integer columns reach transport as
native `0`/`1`; inference and other authoritative numeric policies are preserved.

Existing-target nullability is additionally covered by
`dml/load/not_null_columns.py::test_existing_not_null_column_rejects_null_before_clearing_seed_rows`,
`dml/load/not_null_columns.py::test_omitted_required_not_null_column_rejects_before_clearing_seed_rows`,
`dml/load/not_null_columns.py::test_complete_upsert_with_null_nonkey_required_value_rejects_before_staging`,
`dml/load/not_null_columns.py::test_native_array_with_null_elements_is_not_a_sql_null_column`,
and `dml/load/not_null_columns.py::test_explicit_text_encoding_of_null_container_is_not_a_sql_null_column`.
These check converted column nulls before mutation, while preserving non-null
containers, encoded text and valid append/truncate omissions. Complete-row upsert
requirements remain unchanged.

Captured transfer payloads are additionally covered by
`dml/transfer/not_null_targets.py::test_existing_not_null_transfer_rejects_before_clearing_seed_rows`,
`dml/transfer/not_null_targets.py::test_late_key_null_rejects_all_private_stages_before_any_live_publication`,
`dml/transfer/not_null_targets.py::test_partition_upsert_null_rejects_before_any_partition_mutation`,
`dml/table/stage_constraints.py::test_stage_constraint_probe_matches_native_casts_and_exact_identifier_identity`,
and `dml/table/stage_constraints.py::test_prepared_partition_replay_validates_the_same_payload_before_submission`.
Every key and writer contributes before live publication. Actual staged SQL
projections and prepared replay payloads retain their nullability checks;
dry plans leave this validation pending.

Greenplum native JSON validation is additionally covered by
`dml/load/gp_json.py::test_native_json_containers_load_through_real_gp_execute_values`,
`dml/load/gp_json.py::test_invalid_json_serialization_rejects_before_clearing_seed_rows`,
`dml/load/gp_json.py::test_custom_json_serializer_is_frozen_once_across_lost_acknowledgement_retry`,
`dml/load/gp_json.py::test_json_null_document_and_sql_null_obey_existing_not_null_constraint`,
`dml/load/gp_json.py::test_public_native_json_arrays_keep_sql_array_rank_and_json_element_values`,
`dml/load/gp_json.py::test_qualified_builtin_json_schema_keeps_native_container_semantics`,
and `dml/load/gp_json.py::test_explicit_json_encoding_cannot_bypass_existing_jsonb_nul_constraints`.
These preserve supported dataframe JSON payloads, stricter existing constraints
and the same certified documents through accepted write replay. The disposable
`audit.load.gp_json.json` and `audit.load.gp_json.jsonb` cases in
`integration/core/audit_gp_json.py::test_greenplum_json_existing_and_explicit_loads_preserve_native_values`
also cover explicit two-dimensional arrays under catalog rank erasure; they were
not run locally. Default query decoding still maps SQL NULL and JSON null to
`None` and can leave original SQL array rank ambiguous.

Greenplum native time/interval compatibility is additionally covered by
`dml/load/gp_temporal_values.py::test_native_time_and_interval_load_through_real_gp_execute_values`,
`dml/load/gp_temporal_values.py::test_full_precision_native_temporals_preserve_null_positions_and_exact_values`,
`dml/load/gp_temporal_values.py::test_existing_temporal_precision_rejects_inexact_values_before_clear`,
`dml/load/gp_temporal_values.py::test_explicit_lower_temporal_precision_uses_native_rounding`,
`core/clock_values.py::test_clock_roundtrips_real_spool_without_inventing_a_date`,
and `core/clock_values.py::test_pandas_duration_retains_asm8_ticks_and_unit_through_real_spool`.
The disposable `audit.load.gp_temporal.time` and `audit.load.gp_temporal.interval`
cases in
`integration/core/audit_gp_temporal_values.py::test_greenplum_native_time_and_interval_existing_and_explicit_loads`
cover native microseconds and nulls; they were not run locally. Timedelta values
carry day-time components and do not establish calendar-month identity.

Supported Greenplum built-in identities and exact decimal alignment are covered by
`dml/load/gp_type_identity.py::test_explicit_mapped_builtin_identity_loads_with_real_psycopg_adaptation`,
`dml/load/gp_type_identity.py::test_builtin_typmod_defaults_reject_overflow_before_any_write`,
`dml/load/gp_type_identity.py::test_numeric_default_scale_accepts_exact_zero_fractional_alignment`,
`core/decimal_alignment.py::test_authoritative_zero_scale_accepts_redundant_fractional_zeros`,
`core/decimal_alignment.py::test_legacy_decimal_profile_requires_raw_scale_without_observed_zero_proof`,
and `core/decimal_alignment.py::test_merging_legacy_unknown_scale_and_proven_zeros_keeps_unknown_raw_requirement`.
Raw schema SQL and quoted identity are retained. Only redundant fractional zeros
can be discarded; unknown profile facts do not authorize loss of a nonzero digit.

Existing and explicit Trino `TIME` compatibility is covered by
`dml/load/trino_time_values.py::test_native_time_uses_real_trino_adapter_and_sdk_formatter`,
`dml/load/trino_time_values.py::test_existing_time_precision_rejects_inexact_values_before_clearing_target`,
`dml/load/trino_time_values.py::test_explicit_time_precision_uses_native_rounding_and_midnight_wrap`,
`dml/load/trino_time_values.py::test_clock_retry_reuses_captured_offset_after_lost_insert_acknowledgement`,
`dml/load/trino_time_values.py::test_inferred_naive_clock_column_keeps_exact_native_time_and_sql_null`,
`dml/load/trino_time_values.py::test_inferred_unsupported_clock_offsets_keep_exact_typed_text`,
`dml/load/trino_time_values.py::test_unsupported_parquet_time_stage_rejects_before_clearing_target`,
and `dml/load/trino_time_values.py::test_parquet_append_omitting_nullable_time_stages_only_incoming_columns`.
The disposable `audit.load.trino_time` case in
`integration/core/audit_trino_time_values.py::test_trino_time_values_preserve_precision_and_stage_only_incoming_columns`
uses Iceberg `TIME(6)` and explicit conversions against the existing schema; it
was not run locally. Native inference retains exact supported clock values;
unsupported offsets use typed text. Connector precision and interval binding
follow the documented backend limits.

Trino clock map-key identity and payloads are covered by
`dml/load/trino_time_map_keys.py::test_trino_time_map_cast_collision_cannot_clear_existing_rows`,
`dml/load/trino_time_map_keys.py::test_trino_time_map_native_distinct_pairs_do_not_collapse_before_binding`,
`dml/load/trino_time_map_keys.py::test_trino_time_map_zoned_distinct_instants_retain_both_offset_keys`,
and `dml/load/trino_time_map_keys.py::test_trino_time_map_uncertifiable_keys_reject_before_mutation`.
These reject SQL-equivalent keys before clearing the target and preserve
SQL-distinct keys through native dictionary binding, including composite keys.
The disposable `audit.load.trino_time_map_keys` case in
`integration/core/audit_trino_time_values.py::test_trino_time_map_keys_are_certified_before_clearing_existing_rows`
also checks native wall-clock keys and unchanged physical `TIME(6)` precision;
it was not run locally.

Exact Trino timestamp map-key certification is additionally covered by
`dml/load/trino_timestamp_map_keys.py::test_trino_timestamp_map_equal_instants_cannot_clear_existing_rows`,
`dml/load/trino_timestamp_map_keys.py::test_trino_timestamp_map_nanosecond_distinctions_reach_native_p12`,
and `dml/load/trino_timestamp_map_keys.py::test_trino_timestamp_map_dst_fold_keeps_distinct_native_instants`.
These exercise the native parameter path, equivalent instants at different
source scales, distinct fractional ticks and composite keys before target
mutation. The disposable `audit.load.trino_timestamp_map_keys` case in
`integration/core/audit_trino_timestamp_map_keys.py::test_trino_timestamp_map_identity_rejects_equivalent_exact_keys_before_clear`
pairs engine-only precision-12 expressions with a physical Iceberg precision-6
map. It does not require a precision-12 table or a format upgrade and was not
run locally.

Raw Trino temporal reads and transfer certification are additionally covered by
`backends/trino/exact_reads.py::test_exact_reads_public_scalar_keeps_raw_nanosecond_timestamp`,
`backends/trino/exact_reads.py::test_exact_reads_public_map_preserves_nanosecond_distinct_keys`,
`backends/trino/exact_reads.py::test_exact_reads_all_supported_timestamp_precisions_keep_raw_negative_ticks`,
and `backends/trino/exact_reads.py::test_exact_reads_public_date_free_clock_preserves_all_native_ticks`.
These use actual SDK HTTP/raw JSON values before timestamp rounding.
`dml/transfer/exact_trino_values.py::test_exact_trino_transfer_late_nanoseconds_choose_whole_column_text`,
`dml/transfer/exact_trino_values.py::test_exact_trino_transfer_profiles_every_key_before_native_schema_publication`,
`dml/transfer/exact_trino_values.py::test_exact_trino_transfer_retry_replays_certified_pages_without_requery`,
`dml/transfer/exact_trino_values.py::test_exact_trino_transfer_ambiguous_late_wire_cannot_publish_a_partial_generation`,
and `dml/transfer/exact_trino_values.py::test_exact_trino_transfer_late_plain_calendar_failure_cannot_publish_partial_input`
cover complete-input resolution, exact replay and rejection of partial
generations before destination writes. The disposable
`audit.read.trino_exact_temporal` case in
`integration/core/audit_trino_exact_reads.py::test_trino_engine_reads_preserve_exact_temporals_and_nanosecond_map_keys`
uses engine-only timestamp/clock expressions through precision 12, with no
high-precision table DDL or format upgrade; it was not run locally.

Datetime replay certification is additionally covered by
`core/exact_values.py::test_native_fixed_offset_datetime_custom_label_certifies_and_replays`,
`core/exact_values.py::test_fixed_datetime_labels_offsets_and_fractions_survive_nested_native_replay`,
`core/exact_values.py::test_native_datetime_replay_keeps_naive_or_verified_iana_fold_and_nanoseconds`,
and `core/exact_values.py::test_legacy_datetime_display_label_without_an_encoded_offset_cannot_invent_awareness`.
These preserve fixed-offset display labels, verified named zones, exact fractions
and folds through real certified spools, including nested values, and reject
legacy records that cannot establish awareness from an encoded offset.

Trino buffered binary parameters are additionally covered by
`dml/load/trino_binary_buffers.py::test_trino_binary_buffer_memoryview_load_uses_supported_native_parameters`
and `backends/trino/binary_parameters.py::test_recursive_binary_native_leaves_reach_real_sdk_without_mutating_callers`.
The public regression uses the hash-verified minimum 0.320 formatter, with the
current 0.340 SDK as a positive control. The minimum-version fixture is limited
to formatter code running with current dependency imports.

Native clock parameter compatibility is additionally covered by
`dml/load/trino_clock_parameters.py::test_trino_clock_parameters_aware_time_keeps_native_cast_semantics`
and `dml/load/trino_clock_parameters.py::test_trino_clock_parameters_nested_casts_keep_offsets_and_plain_wall_fields`.
The minimum-formatter regressions preserve offsets for zoned clocks and
wall-clock fields for plain `TIME` casts, with the installed SDK as a positive
control. The fixture has the same formatter-only scope described above.

Trino native JSON casts are additionally covered by
`dml/load/trino_json_casts.py::test_trino_json_casts_accept_arbitrary_varchar_without_parsing_document_text`,
`dml/load/trino_json_casts.py::test_trino_json_casts_sql_null_remains_distinct_from_json_string_null`,
and `backends/trino/json_values.py::test_json_casts_decimal_transport_is_native_fixed_point_across_actual_sdks`.
These retain string-cast semantics, SQL NULL and exact native numeric parameters
on minimum/current formatter paths. The disposable
`audit.load.trino_json_parameters` case in
`integration/core/audit_trino_json_parameters.py::test_trino_json_parameters_keep_native_cast_semantics_on_engine`
compares parameter casts with independent native SQL controls in one engine-only
query. It creates no JSON storage table and was not run locally.

Trino native IP casts are additionally covered by
`dml/load/trino_ipaddress_casts.py::test_trino_ipaddress_casts_valid_ipv4_reaches_native_target`,
`dml/load/trino_ipaddress_casts.py::test_trino_ipaddress_casts_native_key_aliases_fail_before_stage`,
`backends/trino/ip_values.py::test_ipaddress_casts_reject_incompatible_container_wire_and_post_cast_key_collisions`,
and `backends/trino/ip_values.py::test_ipaddress_casts_native_clock_views_keep_both_final_and_stage_equality_domains`.
These preserve input wire values and packed bytes, validate native 16-byte IP
key identity, and retain declared clock equality in IP-bearing composite keys.
The disposable `audit.load.trino_ipaddress_parameters` case in
`integration/core/audit_trino_ipaddress_parameters.py::test_trino_ipaddress_parameters_keep_native_canonical_and_binary_identity`
compares seven parameters with independent native literals in one SDK query.
It performs no DNS lookup or storage DDL and was not run locally.

Greenplum native INET casts are additionally covered by
`dml/load/gp_inet_casts.py::test_gp_inet_casts_valid_string_reaches_native_execute_values`,
`dml/load/gp_inet_casts.py::test_gp_inet_casts_native_arrays_retain_real_psycopg_leaf_casts`,
`dml/load/gp_inet_casts.py::test_gp_inet_casts_native_key_aliases_reject_before_any_write`,
and `backends/gp/network_values.py::test_inet_values_native_key_views_retain_partition_mask_and_family`.
These preserve wire spelling, host bits, masks and family, including array
elements, while validating native effective-key identity before writes.
`dml/load/gp_inet_casts.py::test_gp_inet_casts_mixed_naive_aware_timetz_keys_reject_before_live_write`
covers native TIMETZ key validation with captured session settings. Resulting
duplicate keys reject before live writes, after private key validation.
The disposable `audit.load.gp_inet_parameters` case in
`integration/core/audit_gp_inet_parameters.py::test_greenplum_inet_parameters_preserve_native_hosts_and_masks`
compares four parameters with independent native INET controls in one query.
It creates no table and was not run locally.

Greenplum native assignment compatibility is additionally covered by
`dml/load/gp_cidr_casts.py::test_gp_cidr_casts_valid_string_reaches_native_execute_values`,
`dml/load/gp_cidr_casts.py::test_gp_cidr_casts_registered_driver_adapts_once_for_checkpoint_and_replay`,
`dml/load/gp_cidr_casts.py::test_gp_cidr_casts_explicit_and_final_authorities_use_native_assignment_without_readaptation`,
and `dml/load/gp_cidr_casts.py::test_gp_cidr_casts_incompatible_final_authority_rejects_before_live_clear`.
These certify actual typed assignments before publication and reuse the captured
native rows through retries. Offline plans remain free of stage creation.
`backends/gp/native_assignment.py::test_domain_assignment_rejects_values_that_an_explicit_cast_can_truncate`
and `dml/load/gp_cidr_casts.py::test_gp_cidr_casts_domain_array_last_element_cannot_truncate_before_assignment`
cover assignment semantics with real driver adaptation and fake native oracles;
the latter does not claim arrays-of-domains support in the disposable GP6 stack.
Checkpoint identity is covered by
`dml/load/native_assignment.py::test_native_checkpoint_cannot_publish_to_reconfigured_alias_after_lost_target_ack`
and `dml/load/native_assignment.py::test_initial_open_snapshot_cannot_be_hidden_by_restoring_the_alias`.
Transfer coverage includes
`dml/transfer/gp_native_assignment.py::test_gp_native_transfer_certifies_the_actual_final_explicit_cast_chain`
and `dml/transfer/gp_native_assignment.py::test_gp_native_transfer_inner_retry_stops_deterministic_assignment_failures`.
Query creation is covered by
`dml/table/create_from_sql/gp_native_assignment.py::test_gp_query_cross_alias_projection_reads_certified_source_input`,
`dml/table/create_from_sql/gp_native_assignment.py::test_gp_query_native_lost_ack_reuses_certified_sql_payload`,
and `dml/table/create_from_sql/gp_native_assignment.py::test_gp_query_native_dry_plan_has_pending_validation_without_materialization`.
These retain each consumer's actual assignment or SQL-cast semantics. For
independent Greenplum databases, `source_db` selects the source connection:
the complete selected rows are frozen once and assigned into a target-owned
native checkpoint before replacing the target. An unrelated table with the
same name in the target database does not become the source. Shared-storage
alias replacement is covered separately by
`dml/table/create_from_sql/source_alias_identity.py::test_public_cross_alias_self_replacement_freezes_before_destroying_shared_storage`.
The
disposable `audit.load.gp_native_assignment` case in
`integration/core/audit_gp_assignment_preflight.py::test_greenplum_native_assignment_certifies_cidr_and_scalar_domain_before_clear`
covers GP6-supported CIDR, MACADDR, a scalar domain and CIDR arrays. It was not run
locally and makes no domain-array support claim.

ClickHouse native address compatibility is additionally covered by
`dml/load/ch_network_addresses.py::test_ch_network_addresses_reach_real_driver_column_encoding`,
`dml/load/ch_network_addresses.py::test_ch_network_addresses_invalid_last_value_rejects_before_live_clear_or_new_ddl`,
and `dml/load/ch_network_addresses.py::test_ch_network_addresses_equivalent_native_keys_reject_before_append_stage_creation`.
`core/network_values.py::test_address_family_survives_certified_binary_replay`
and `dml/transfer/ch_network_replay.py::test_ch_network_replay_public_transfer_keeps_native_addresses_and_exact_wire`
cover exact address objects in replay and actual transport codecs. These retain
the declared address family, nullable values and native key equality.

ClickHouse client capability and value certification are additionally covered by
`dml/load/ch_native_aliases.py::test_ch_native_aliases_keep_public_schema_values_and_real_driver_bytes`,
`backends/ch/client_validation.py::test_client_validation_certifies_actual_alias_binary`,
`backends/ch/client_validation.py::test_client_validation_rejects_nonnullable_null_before_sdk_default_fill`,
and `backends/ch/client_validation.py::test_client_validation_encodes_ordinary_normalization_physical_mismatch`.
These preserve raw schema declarations while checking the selected client's
actual writer types and converted values.
`backends/ch/client_validation.py::test_declared_domain_proof_preserves_unknown_count_and_rejects_narrowing`
keeps metadata-domain proofs distinct from complete value encoding.
`dml/load/client_validation.py::test_client_validation_empty_sql_only_creation_needs_no_unsupported_http_encoder`
and `dml/table/create_from_sql/client_validation.py::test_query_server_sql_copy_never_constructs_client_codecs`
cover SQL-only paths without client row codecs.
`dml/table/create_from_sql/client_validation.py::test_frozen_query_invalid_last_object_is_rejected_before_live_ddl`
and `dml/transfer/ch_client_validation.py::test_ch_client_validation_late_object_failure_scans_all_keys_before_publication`
cover complete frozen-input validation before publication.
`dml/load/client_validation.py::test_client_validation_retry_rejects_changed_alias_before_reusing_certified_values`
and `dml/table/create_from_sql/client_validation.py::test_frozen_query_retry_rejects_changed_actual_client_configuration`
cover frozen client and connection scope during certified retries.
`backends/ch/object_ddl.py::test_ch_legacy_object_ddl_preserves_native_type_and_structural_round_trip`,
`backends/ch/wait_type_comparison.py::test_ch_wait_type_comparison_recognizes_sql_integer_aliases`,
and `backends/ch/wait_type_comparison.py::test_ch_wait_type_comparison_preserves_enum_label_whitespace`
cover legacy Object declarations and alias-aware readiness comparisons without
erasing literal contents. The disposable cases
`integration/core/audit_ch_ip_values.py::test_clickhouse_ip_columns_keep_native_values_before_authoritative_mutation`
and `integration/core/audit_ch_storage_aliases.py::test_clickhouse_storage_aliases_preserve_native_rows_and_unflattened_metadata`
pair native-address and structural-alias behavior with the fast regressions.
They were not run locally.
`dml/load/ch_binary_strings.py::test_ch_binary_strings_mixed_unicode_and_bytes_keep_exact_http_encoding`
and `backends/ch/string_transport.py::test_http_string_transport_nested_values_keep_exact_bytes_and_null_order`
cover mixed text/binary HTTP String batches with real codecs, including nested
bytes and SQL NULL.

Trino composite native-key regressions are additionally covered by
`dml/load/trino_composite_map_keys.py::test_trino_composite_native_map_keys_reject_before_truncate`,
`dml/load/trino_composite_map_keys.py::test_trino_composite_native_map_keys_keep_distinct_rows_and_bare_return`,
and `dml/load/trino_composite_effective_keys.py::test_trino_composite_effective_key_aliases_reject_before_stage`.
These compose IPADDRESS alias identity with CHAR padding across every component,
reject duplicate native map and effective keys before mutation, and retain
distinct composite entries. Public fake-client loads use the real adapter and
SDK formatter; they do not claim live-server row-preservation evidence.
The disposable `audit.engine.trino_composite_ip_char_map_keys` case in
`integration/core/audit_trino_ipaddress_parameters.py::test_trino_engine_composes_ipaddress_and_char_identity_inside_row_map_keys`
uses engine-only SELECTs because the configured Iceberg and Hive catalogs cannot
persist IPADDRESS. It checks native equality and duplicate rejection, creates no
storage table, and was not run locally.

ClickHouse role-specific drop ownership is additionally covered by
`dml/table/drop_scope.py::test_public_drop_keeps_foreign_resources_outside_each_verified_pair_role`,
`dml/table/drop_scope.py::test_public_drop_of_plain_local_table_preserves_an_independent_remote_homonym`,
and `backends/ch/drop_policy_scope.py::test_verified_layout_cleanup_preserves_foreign_coordinator_physical_table`.
These public-adapter and stateful fake-client regressions cover role flags,
actual facade/storage placement, selected-resource absence waits and retention
of unrelated resources; they do not claim live-server preservation evidence.
The disposable cases `audit.drop.clickhouse.managed_pair_scopes`,
`audit.drop.clickhouse.physical_role_only` and
`audit.drop.clickhouse.facade_role_only` share
`integration/core/audit_ch_drop_scope.py::test_drop_tables_keeps_owned_roles_in_their_verified_execution_scopes`.
They check retained UUIDs, full definitions and rows, and were not run locally.
`backends/ch/drop_relationship.py::test_outage_layout_rejects_companion_ownership_when_column_identity_differs`
and `backends/ch/drop_relationship.py::test_policy_cleanup_preserves_schema_mismatched_same_named_physical_storage`
cover ordered column and default identity as well as deterministic companion
names. They compare wrapped and raw inspection and check CTAS cleanup through
the real adapter against stateful resources, including retained UUIDs,
definitions and 100 physical rows; this is not a live-server reproduction.

Batch alias identity and bounded cleanup capacity additionally have regressions
in `dml/table/drop_scope.py::test_public_drop_batch_deduplicates_aliases_of_the_same_verified_resources`,
`dml/table/drop_scope.py::test_public_drop_batch_deduplicates_a_complete_storage_alias_and_explicit_physical_target`
and `dml/transfer/staged_keyed/contract.py::test_clickhouse_per_host_fallback_stays_inside_target_pool`.
These use actual adapter paths over stateful fake clients, including the
capacity-one recovery handoff; they do not establish live-server results.

Additional fake-client fault coverage is in `backends/ch/drop_certification.py`,
`backends/ch/drop_namespace_faults.py`, `backends/ch/drop_execution_faults.py`,
`backends/ch/drop_bounded_faults.py`, `backends/ch/drop_batch_identity.py` and
`backends/ch/lifecycle_compat.py`. These exercise metadata and configuration
failures, changed native identities, mandatory cluster resolution, and retained
compatibility helpers through actual adapter paths.

ClickHouse integer timestamp compatibility is additionally covered by
`dml/load/ch_integer_timestamps.py::test_ch_integer_timestamp_values_reach_public_native_writes`
and `dml/load/ch_integer_timestamps.py::test_ch_integer_timestamp_out_of_range_rejects_before_clear`.
These public loads exercise real binary writers for explicit and existing
schemas; integer values retain native seconds or declared-scale ticks.
The disposable `audit.load.clickhouse_integer_timestamps` scenario in
`integration/core/audit_ch_integer_timestamps.py::test_integer_timestamps_keep_native_units_for_explicit_and_existing_schemas`
checks stored values and rejected writes against both transports and was not
run locally.

Additional scope and wait regressions are
`dml/table/drop_overlap.py::test_public_drop_executes_the_verified_union_of_overlapping_requested_scopes`,
`backends/ch/drop_wait_boundaries.py::test_drop_wait_boundaries_poll_interval_cannot_exceed_timeout_or_hold_lease`,
`backends/ch/drop_wait_boundaries.py::test_drop_wait_boundaries_cancellation_interrupts_poll_and_releases_lease`,
and `backends/ch/lifecycle_compat.py::test_bounded_compat_runner_reports_native_endpoint_and_original_open_error`.
They use stateful fake endpoints and virtual time, rather than live-server or
wall-clock timeout evidence. The disposable
`audit.drop.clickhouse.overlapping_facade_first` and
`audit.drop.clickhouse.overlapping_physical_first` scenarios in
`integration/core/audit_ch_drop_scope.py::test_drop_tables_executes_overlapping_requested_scopes_once`
check the requested resource union and retained sentinel UUIDs, definitions and
rows; neither ran locally.

`dml/io/execute_create_scope.py::test_execute_create_pair_replace_preserves_foreign_local_physical_table`
and `dml/io/execute_create_scope.py::test_execute_create_pair_replace_same_placement_creates_and_populates_once`
exercise public query replacement through actual adapter logic over fake state.
The disposable `audit.create.clickhouse.execute_query_drop_scope` scenario in
`integration/core/audit_ch_drop_scope.py::test_execute_create_replaces_only_verified_existing_pair_resources`
checks successful replacement with unrelated local storage retained; it was not
run locally.

`dml/transfer/adaptive_snapshot_scope.py::test_public_adaptive_keyed_snapshot_rejects_scope_drift_before_target_mutation`
and `dml/transfer/adaptive_snapshot_scope.py::test_public_adaptive_keyed_snapshot_accepts_stable_partial_source_scope`
exercise native keyed snapshot materialization through real adapters over fake
source and target state. A changed scope cannot certify a checkpoint or mutate
the destination, while a stable selected partial scope remains supported.
The disposable `audit.transfer.clickhouse_adaptive_snapshot_scope` scenario in
`integration/core/audit_adaptive_snapshot_scope.py::test_changed_keyed_native_source_scope_cannot_publish_a_certified_checkpoint`
injects a real run-owned source-table removal between keyed appends and checks
retained target rows and source-stage cleanup. It was not run locally.

Native timestamp effective-key regressions include
`dml/load/ch_timestamp_keys.py::test_ch_timestamp_keys_reject_equivalent_native_partition_keys_before_create`,
`dml/load/ch_timestamp_keys.py::test_ch_timestamp_keys_nested_tuple_ticks_reject_before_target_creation`,
and `dml/load/ch_timestamp_keys.py::test_ch_timestamp_keys_distinct_dst_folds_survive_upfront_python_key_validation`.
Actual binary writers prove native equality or distinct instants; validation
rejects collisions before resource creation and preserves distinct DST folds.
`dml/load/ch_timestamp_keys.py::test_ch_timestamp_keys_existing_metadata_append_stages_authoritative_timestamp_columns`
checks that stage DDL and the real client encoder use the same verified types.
The disposable `audit.load.clickhouse_timestamp_native_keys` scenario in
`integration/core/audit_ch_integer_timestamps.py::test_timestamp_effective_keys_use_native_ticks_before_upsert_mutation`
checks stored native ticks and partition-scoped upserts; it was not run locally.

`dml/io/execute_create_scope.py::test_execute_create_if_not_exists_resolves_plain_target_scope_before_setup`,
`dml/io/execute_create_scope.py::test_execute_create_if_not_exists_retains_existing_coordinator_target_before_setup`,
and `dml/io/execute_create_scope.py::test_execute_create_default_rejects_existing_coordinator_target_before_remote_ddl`
cover exact existing coordinator and selected remote roles before setup or
creation. The disposable `audit.create.clickhouse.remote_plain_skip` scenario in
`integration/core/audit_ch_drop_scope.py::test_execute_create_skips_existing_remote_plain_target_before_setup`
checks retained UUIDs, definitions, rows and the zero-statement skip result;
it was not run locally.

`dml/load/ch_integer_timestamps.py::test_ch_authoritative_timestamp_array_accepts_tuple_sequences`
and `dml/load/ch_timestamp_keys.py::test_ch_timestamp_keys_hashable_array_payloads_reject_equal_native_keys_before_create`
preserve the legacy tuple-as-Array input contract when an authoritative
ClickHouse Array schema is present, while retaining default tuple inference.
The disposable `audit.load.clickhouse_authoritative_tuple_arrays` scenario in
`integration/core/audit_ch_integer_timestamps.py::test_authoritative_array_columns_keep_tuple_sequence_values`
checks stored negative ticks, SQL NULLs and caller preservation; it was not run
locally. `backends/ch/execute_create_healthy.py` separately covers ordinary
existence probes and new-table creation in stop and healthy modes without
weakening destructive ownership checks.

`dml/load/ch_integer_timestamps.py::test_ch_explicit_timestamp_aliases_accept_native_integer_seconds`
and `dml/load/ch_integer_timestamps.py::test_ch_explicit_timestamp_aliases_reject_out_of_range_before_clear`
cover supported `DateTime32` and `TIMESTAMP` SQL aliases using canonical server
metadata and real client binary writers. Additional public fault cases in
`backends/ch/drop_resource_recheck.py`, `backends/ch/drop_execution_faults_extra.py`,
`backends/ch/drop_bounded_faults.py`, and `dml/io/execute_create_presence.py`
cover changed ownership identities, delayed acknowledgements, cancellation,
capacity-one recovery and unverifiable current databases. These are fake-client
regressions, not live-server evidence.

`core/domain_proofs.py::test_proof_requires_destination_to_cover_entire_source_domain`
and `dml/transfer/schema/date_authority.py` additionally cover the DATE-family
metadata proof and public transfer preflight. An explicit textual stage cannot
clear an existing nullable DATE target before all selected values are validated;
canonical ISO-date casts and typed DATE inputs remain supported. The disposable
`audit.transfer.trino_date_domain_preflight` scenario in
`integration/core/audit_date_domain_preflight.py` checks the same behavior with
stored DATE rows and an unchanged schema. It was not run locally. Fake GP
mutation effects do not establish live transactional data loss.

`dml/load/trino_dates.py`, `dml/load/gp_dates.py`, and
`dml/transfer/schema/direct_date_stage.py` additionally preserve supported
authoritative DATE assignments through public dataframe loads and transfers.
They check complete validation before mutation, native parameter formatting,
unchanged raw replay input, and Trino DATE key equality. Backend helper tests
cover recursive conversions and preserve GP datetime offsets without guessing
the server's resulting calendar day. Disposable DATE scenarios remain unrun
locally.

`dml/load/trino_booleans.py` and `core/trino_boolean_values.py` preserve
supported signed-BIGINT casts into explicit or existing Trino BOOLEAN columns.
`dml/load/trino_booleans.py::test_trino_booleans_public_ascii_string_casts_preserve_native_false_and_true`,
`dml/load/trino_booleans.py::test_trino_booleans_padded_string_cast_rejects_before_target_mutation`
and `dml/load/trino_booleans.py::test_trino_booleans_finite_numeric_casts_preserve_zero_and_exact_nonzero_values`
extend this coverage to exact ASCII `true`/`false`, `t`/`f` and `1`/`0`, with
case-insensitive letters and no trimming, plus native DOUBLE and finite DECIMAL
casts within precision/scale 38. Numeric zero/nonzero conversion preserves exact
decimals, floating NaN NULL handling and infinite DOUBLE values. These restore
casts rejected before mutation; they are not target-loss reproductions. Invalid text
fails before mutation with sanitized diagnostics. These checks preserve
integer/text inference and exercise the installed
parameter formatter with fake database I/O. The disposable
`audit.load.trino_boolean_values` scenario was not run locally.

The Greenplum Boolean regressions reproduce rejection of supported text before
mutation. The latent `bool('false')` conversion hazard was established from
source; the old profile gate blocked it, so it is not a runtime-corruption or
target-loss reproduction.
`dml/load/gp_booleans.py::test_gp_boolean_load_preserves_false_values_through_real_psycopg_formatter`,
`dml/load/gp_booleans.py::test_gp_boolean_text_input_preserves_postgresql_forms`
and `dml/load/gp_booleans.py::test_gp_invalid_late_boolean_rejects_before_target_clear`
cover false-value preservation, PostgreSQL text forms and rejection before
mutation using the real parameter formatter with fake database I/O. The bounded
built-in conversion uses ASCII case-insensitive unique prefixes and ASCII-only
whitespace trimming; domains retain server-assignment validation. Numeric casts
and inference are unchanged.

`dml/load/ch_booleans.py::test_ch_booleans_public_integer_zero_one_reaches_exact_native_bytes`
and `dml/load/ch_booleans.py::test_ch_booleans_public_numeric_nonzero_matches_actual_default_encoders`
cover ClickHouse Boolean numeric casts with real HTTP/native encoder bytes and
fake database I/O. The numeric profile rejection occurred after the independent
client-byte controls passed; it is a compatibility failure, not target loss.
Exact tiny Decimal nonzero values remain true without a floating-point detour
or an artificial integer/Decimal storage-width bound. Integer inference remains
covered by `dml/load/ch_booleans.py::test_ch_boolean_integer_zero_one_inference_remains_integer`.
`dml/load/ch_booleans.py::test_ch_booleans_invalid_last_value_rejects_before_native_encoder_or_target_mutation`
checks sanitized rejection of late private text before encoding or mutation,
preserving existing rows, schema and caller data.

`dml/transfer/schema/intermediate_boolean_stage.py::test_explicit_text_boolean_stage_preserves_strings_and_validates_final_cast`
and `dml/transfer/schema/intermediate_boolean_stage.py::test_explicit_text_boolean_stage_rejects_late_private_input_before_clearing`
cover converted validation views for explicit GP text and Trino VARCHAR stages
feeding existing built-in Boolean columns. Supported casts were blocked by the
raw profile gate; initial invalid-stage probes stopped at that gate too, so they
did not prove invalid admission or target loss. The stage schema and raw string
payload remain unchanged while every selected value is checked against the
destination backend's Boolean rules before publication. These fast tests do not
establish live-server evidence.

`dml/load/trino_bigints.py::test_trino_bigints_public_load_keeps_explicit_int_assignment_and_signed_bounds`,
`dml/load/trino_integer_text.py::test_trino_integer_text_public_casts_preserve_native_grammar_and_width_bounds`,
`dml/load/gp_integers.py::test_gp_integer_text_load_preserves_exact_native_values_and_width`
and `dml/load/ch_integer_text.py::test_ch_integer_text_load_preserves_native_width_values_and_driver_bytes`
cover authoritative integer conversions before mutation. Direct Trino BIGINT
retains Python conversion; other native text grammars and destination widths
remain distinct. Invalid-late and range controls preserve the target, and
`dml/load/ch_integer_text.py::test_ch_numeric_strings_without_authority_keep_inferred_string`
preserves ordinary String inference. These are compatibility and real-client
parameter/byte checks with fake database I/O, not live-server evidence.

`dml/load/gp_numeric_text.py::test_gp_numeric_text_native_assignment_keeps_exact_values_and_physical_schema`,
`dml/load/gp_numeric_text.py::test_gp_numeric_text_native_signed_zero_preserves_exact_physical_ieee_bits`,
`dml/load/trino_numeric_text.py::test_trino_numeric_text_public_native_parameters_preserve_finite_values_exactly`
and `dml/load/ch_numeric_text.py::test_ch_numeric_text_public_write_retains_exact_native_values_and_bytes`
cover finite floating-point and exact Decimal text. Native parameter and byte
controls retain signed zero, with a typed Greenplum adapter at the last
parameter boundary. Native ClickHouse Float text cases exercise the candidate's
typed conversion path; they do not establish legacy native-client support.
Nonfinite text cannot become SQL NULL. Inference and unrelated temporal context
remain unchanged. Disposable numeric scenarios were not run locally.

`dml/transfer/schema/numeric_text_stage.py::test_numeric_text_stage_preserves_raw_strings_and_exact_finite_final_values`
and `dml/transfer/schema/numeric_text_stage.py::test_numeric_text_stage_rejects_unsafe_last_cast_before_any_mutation`
check explicit textual stages without rewriting their schema, payload, profiles
or replay spool. One complete scan validates finite native casts, including
range, scale, nullability and private-invalid guards.
`dml/transfer/schema/bigint_native_cast_boundary.py::test_varchar_stage_uses_native_bigint_cast_grammar_before_target_clear`
and `dml/transfer/schema/bigint_native_cast_boundary.py::test_direct_bigint_authority_retains_python_integer_conversion`
preserve the deliberate distinction between Trino's native staged SQL parser
and direct Python BIGINT conversion. These are fast fake-I/O regressions;
disposable scenarios remain unrun locally.

`dml/load/gp_effective_keys.py::test_gp_effective_keys_reject_converted_aliases_before_target_append`
and `dml/load/gp_effective_keys.py::test_gp_effective_keys_reject_final_native_character_assignment_collision`
check stored-stage and final native key equality before keyed append. The latter
uses a final owned assignment checkpoint when explicit incoming types differ
from actual target types. Equivalent integer aliases retain the common path in
`dml/load/gp_effective_keys.py::test_gp_effective_keys_keep_equivalent_integer_alias_on_common_stage_path`;
`dml/load/gp_effective_keys.py::test_gp_effective_keys_offline_native_assignment_remains_pending_without_io`
retains pending offline plans without database I/O. Ordinary unkeyed append
semantics are unchanged. These are stateful fake/real-adapter regressions, not
live database mutation evidence.

[SQL module index](index.md)
