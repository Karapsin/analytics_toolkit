from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    models_module,
    pytest,
)


def test_adaptive_batch_sizer_can_target_memory_instead_of_time() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=1_000,
    )

    sizer.update(100.0, memory_bytes=400)
    assert sizer.current_size == 150
    sizer.update(1.0, memory_bytes=750)
    assert sizer.current_size == 150
    sizer.update(1.0, memory_bytes=2_000)
    assert sizer.current_size == 75

    unlimited = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=None,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=1_000,
    )
    unlimited.update(100.0, memory_bytes=400)
    assert unlimited.current_size == 150
    unlimited.update(100.0, memory_bytes=400)
    assert unlimited.current_size == 225

    no_measurement = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=1_000,
    )
    no_measurement.update(1.0)
    assert no_measurement.current_size == 100

    disabled = models_module.AdaptiveBatchSizer(
        enabled=False,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=1_000,
    )
    disabled.update(1.0, memory_bytes=10)
    assert disabled.current_size == 100


def test_adaptive_batch_sizer_first_rows_per_second_sample_schedules_shrink() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)

    assert sizer.current_size == 90
    assert sizer.baseline_size == 100
    assert sizer.baseline_rows_per_second == 100.0
    assert sizer.probe_direction == "shrink"
    assert sizer.is_experimental_size is True


def test_adaptive_batch_sizer_grow_equivalent_accepts_and_continues_growing() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(0.9, inserted_rows=90)
    sizer.update(1.1, inserted_rows=110)

    assert sizer.current_size == 121
    assert sizer.baseline_size == 110
    assert sizer.baseline_rows_per_second == pytest.approx(100.0)
    assert sizer.probe_direction == "grow"
    assert sizer.is_experimental_size is True


def test_adaptive_batch_sizer_grow_worse_rolls_back_to_last_good_size() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(0.9, inserted_rows=90)
    sizer.update(1.1, inserted_rows=110)
    sizer.update(121 / 80, inserted_rows=121)

    assert sizer.current_size == 110
    assert sizer.baseline_size == 110
    assert sizer.baseline_rows_per_second == pytest.approx(100.0)
    assert sizer.probe_direction is None
    assert sizer.is_experimental_size is False


def test_adaptive_batch_sizer_grows_shrinks_caps_floors_and_can_disable() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=1_000,
        min_size=500,
        max_size=2_000,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
    )

    sizer.update(4.9)
    assert sizer.current_size == 1_500
    sizer.update(4.9)
    assert sizer.current_size == 2_000
    sizer.update(10.0)
    assert sizer.current_size == 2_000
    sizer.update(21.0)
    assert sizer.current_size == 1_000
    sizer.update(21.0)
    assert sizer.current_size == 500
    sizer.update(21.0)
    assert sizer.current_size == 500

    disabled = models_module.AdaptiveBatchSizer(
        enabled=False,
        current_size=1_000,
        min_size=500,
        max_size=2_000,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
    )
    disabled.update(1.0)
    assert disabled.current_size == 1_000


def test_adaptive_batch_sizer_handles_unknown_probe_and_missing_baselines() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        target_seconds=10.0,
        target_rows_per_second_window=1,
    )

    assert sizer._try_schedule_rows_per_second_probe("grow") is False
    sizer.is_experimental_size = True
    sizer.probe_direction = "unknown"
    sizer.previous_rows_per_second = 100.0
    sizer.baseline_rows_per_second = 100.0
    sizer.update(1.0, inserted_rows=100)
    assert sizer.current_size == 100
    assert sizer.previous_rows_per_second == 100.0
    assert sizer.probe_direction == "unknown"

    sizer.baseline_size = None
    sizer._restore_rows_per_second_baseline()
    assert sizer.current_size == 100
    assert sizer.probe_direction is None
    assert sizer.is_experimental_size is False


def test_adaptive_batch_sizer_ignores_invalid_counts_and_durations() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        target_seconds=10.0,
    )

    for inserted_rows in (None, 0, -1):
        sizer.update(1.0, inserted_rows=inserted_rows)
    for duration_seconds in (0.0, -1.0):
        sizer.update(duration_seconds, inserted_rows=100)

    assert sizer.current_size == 100
    assert list(sizer.rows_per_second_samples) == []
    assert sizer.previous_rows_per_second is None


def test_adaptive_batch_sizer_memory_shrink_respects_minimum_at_size_one() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=1,
        min_size=1,
        max_size=10,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=100,
    )

    sizer.update(1.0, memory_bytes=101)

    assert sizer.current_size == 1


def test_adaptive_batch_sizer_missing_memory_target_and_min_max_clamps() -> None:
    no_target = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
    )
    no_target._update_for_memory(1)
    assert no_target.current_size == 100

    min_memory = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=25,
        min_target_memory_bytes=50,
    )
    min_memory.update(1.0, memory_bytes=20)
    assert min_memory.current_size == 150

    max_seconds = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=50.0,
        max_target_seconds=20.0,
    )
    max_seconds.update(9.0)
    assert max_seconds.current_size == 150


def test_adaptive_batch_sizer_respects_batch_memory_bounds() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=100,
        min_target_memory_bytes=50,
        max_target_memory_bytes=50,
    )
    sizer.update(1.0, memory_bytes=75)
    assert sizer.current_size == 66


def test_adaptive_batch_sizer_respects_batch_seconds_bounds() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=2,
        min_size=1,
        max_size=8,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        min_target_seconds=20.0,
        max_target_seconds=40.0,
    )

    sizer.update(9.9)
    assert sizer.current_size == 3
    sizer.update(10.0)
    assert sizer.current_size == 3
    sizer.update(35.0)
    assert sizer.current_size == 3
    sizer.update(50.0)
    assert sizer.current_size == 1


def test_adaptive_batch_sizer_rows_per_second_respects_caps_and_small_deltas() -> None:
    small = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=5,
        min_size=1,
        max_size=10,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        adaptive_batch_size_step=0.1,
    )
    small.update(1.0, inserted_rows=5)
    assert small.current_size == 4

    min_capped = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=10,
        min_size=10,
        max_size=20,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        adaptive_batch_size_step=0.5,
    )
    min_capped.update(1.0, inserted_rows=10)
    assert min_capped.current_size == 10
    assert min_capped.is_experimental_size is False
    assert min_capped.noop_probe_size == 10
    assert min_capped.noop_probe_direction == "shrink"
    min_capped.update(1.0, inserted_rows=10)
    assert min_capped.current_size == 10
    assert min_capped.is_experimental_size is False

    max_capped = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=10,
        min_size=9,
        max_size=10,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        adaptive_batch_size_step=0.5,
    )
    max_capped.update(1.0, inserted_rows=10)
    max_capped.update(0.9, inserted_rows=9)
    assert max_capped.current_size == 10
    assert max_capped.is_experimental_size is False
    assert max_capped.noop_probe_size == 10
    assert max_capped.noop_probe_direction == "grow"


def test_adaptive_batch_sizer_shrink_better_accepts_smaller_size() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(0.75, inserted_rows=90)

    assert sizer.current_size == 81
    assert sizer.baseline_size == 90
    assert sizer.baseline_rows_per_second == 120.0
    assert sizer.probe_direction == "shrink"
    assert sizer.is_experimental_size is True


def test_adaptive_batch_sizer_shrink_equivalent_restores_and_switches_to_grow() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(0.9, inserted_rows=90)

    assert sizer.current_size == 110
    assert sizer.baseline_size == 100
    assert sizer.baseline_rows_per_second == 100.0
    assert sizer.probe_direction == "grow"
    assert sizer.is_experimental_size is True


def test_row_batch_dataframe_and_approximate_memory_include_rows(
    monkeypatch,
) -> None:
    batch = models_module.RowBatch(columns=["id"], rows=[(1,), (2,)])
    sized_values: list[Any] = []

    def fake_approx_sizeof(value: Any) -> int:
        sized_values.append(value)
        return 10 * len(sized_values)

    monkeypatch.setattr(models_module, "_approx_sizeof", fake_approx_sizeof)

    assert batch.to_dataframe(include_rows=True).to_dict("records") == [
        {"id": 1},
        {"id": 2},
    ]
    assert batch.approx_memory_bytes() == 30
    assert sized_values == [batch.columns, batch.rows]


def test_row_batch_recursive_size_handles_mappings_sequences_and_scalars(
    monkeypatch,
) -> None:
    monkeypatch.setattr(models_module.sys, "getsizeof", lambda _value: 1)

    value = {"key": [1, (2, {3}), frozenset({4})]}

    assert models_module._approx_sizeof(value) == 10
    assert models_module._approx_sizeof("scalar") == 1


def test_row_batch_recursive_size_stops_at_cycles_and_depth_limit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(models_module.sys, "getsizeof", lambda _value: 1)
    cyclic: list[Any] = []
    cyclic.append(cyclic)

    assert models_module._approx_sizeof(cyclic) == 1
    assert models_module._approx_sizeof([[[1]]], _max_depth=1) == 3
