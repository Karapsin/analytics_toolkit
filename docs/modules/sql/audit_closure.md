[SQL module index](index.md)

# SQL correctness audit closure

The audit is a reproduction inventory. This table records the selected contracts
and their fast regression evidence. Publication requires complete managed checks
and the SQL Explorer visual-review receipt. Disposable
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
| 1 | CH routing breaks qualified columns | fixed | `backends/ch/routing_contract.py::test_routing_preserves_bound_qualified_columns_and_stars`; `backends/ch/source_bindings.py::test_same_scope_database_qualified_sources_remain_distinct` |
| 2 | Explorer database discovery fails with routing or healthy-shard mode | fixed | `explorer/completion.py::test_backend_namespace_discovery_uses_serial_metadata_queries` |
| 3 | Explorer persists incomplete metadata as complete | fixed | `explorer/metadata_identity.py::test_partial_catalog_never_prunes_complete_snapshot_and_recovers_without_usage` |
| 4 | Explicit all-replica queries can silently return partial results | fixed | `backends/ch/routing_contract.py::test_explicit_all_replicas_requires_physical_coverage_without_automatic_routing` |
| 5 | Healthy-shard mode can reject safe replica failover writes | fixed | `backends/ch/routing_contract.py::test_surviving_replica_in_every_shard_accepts_deterministic_write_key` |
| 6 | Valid two-argument cluster functions are rejected | fixed | `backends/ch/routing_contract.py::test_two_argument_cluster_functions_parse_quoted_components_and_preserve_extras` |
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
| 29 | Key-validated append can swap columns | fixed | `dml/load/upsert_rows.py::test_key_validated_append_uses_explicit_subset_column_order` |
| 30 | Several writes ignore local-only CH policy | fixed | `backends/ch/reconfigure/structural_safety.py::test_local_layout_never_owns_a_suffix_companion`; `dml/load/upsert_storage_scope.py::test_load_df_upsert_mutates_only_verified_existing_storage_scope`; `dml/load/upsert_storage_scope.py::test_transfer_finalization_upsert_mutates_only_verified_existing_storage_scope` |
| 31 | Query-based creation drops resolved CH creation policy | fixed | `backends/ch/policy_ctas.py::test_public_query_policy_keeps_independent_ddl_and_routing_scopes` |
| 32 | `ch_replace_table=True` is discarded with `sql=...` | fixed | `backends/ch/policy_ctas.py::test_public_query_replace_flag_controls_sql_plan_without_implying_drop`; `backends/ch/policy_ctas.py::test_query_sql_generation_preserves_replace_mode` |
| 33 | Default query creation can append to an existing table | fixed | `backends/ch/policy_ctas.py::test_public_query_creation_existing_modes_never_accidentally_append` |
| 34 | Reconfiguration cleanup can delete a pre-existing `_shard` | fixed | `backends/ch/reconfigure/structural_safety.py::test_local_to_pair_rejects_a_preexisting_companion_before_ddl` |
| 35 | Cross-cluster migration can copy rows into the live source | fixed | `backends/ch/reconfigure/structural_safety.py::test_relocation_rejects_copy_routing_to_original_storage` |
| 36 | Partial rename rollback can leave the public table missing | fixed | `backends/ch/reconfigure/structural_safety.py::test_count_failure_rolls_back_acknowledged_cutovers_in_reverse_order` |
| 37 | Conversion retains invalid MergeTree clauses on Distributed facades | fixed | `backends/ch/reconfigure/structural_safety.py::test_replacement_create_is_plain_and_facade_excludes_physical_properties` |
| 38 | Reconfiguration changes `rand()` into `randCanonical()` | fixed | `backends/ch/routing_contract.py::test_rand_and_randcanonical_remain_distinct_in_routed_defaults` |
| 39 | Guarded replacement loses optional Distributed engine arguments | fixed | `backends/ch/replace_routing.py::test_guarded_replacement_preserves_configured_distributed_template_arguments` |
| 40 | Readiness failures leak created stages | fixed | `dml/load/stage_ownership.py::test_load_readiness_failure_cleans_acknowledged_partial_pair_only`; `dml/transfer/row_batches/stage_ownership.py::test_keyed_worker_readiness_failure_cleans_acknowledged_stage_before_return`; `dml/transfer/row_batches/stage_registration.py::test_keyed_initialization_registers_partial_pair_before_readiness_returns` |
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
and `dml/transfer/schema/composite_map_keys.py::test_safe_same_and_widened_composite_keys_keep_lazy_streaming`.
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
Live mutation uses the verified physical table and scope; offline plans preserve
desired policy and explicitly defer existing-layout verification. The existing
disposable `audit.upsert.ch` scenario also covers actual storage cluster `B`
with an invalid desired caller cluster `A` and independent local staging on both
ClickHouse transports; it was not run locally.

[SQL module index](index.md)
