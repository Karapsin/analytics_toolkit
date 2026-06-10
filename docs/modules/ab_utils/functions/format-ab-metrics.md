[All AB functions](index.md)

# format_ab_metrics

Reshape metric comparison output into a wide presentation table.

```python
format_ab_metrics(
    df,
    label_cols=None,
    output_type=None,
    significance_alpha=None,
    significance_p_value=None,
    allow_repeated_groups=None,
    keep_simple_group_names=False,
)
```

## Inputs

- `df` - output dataframe from `compute_test_metrics`
- `label_cols` - columns that identify separate report sections
- `output_type` - output column family or families to include
- `significance_alpha` - optional p-value threshold for significant outputs
- `significance_p_value` - p-value source for significance checks
- `allow_repeated_groups` - groups that may repeat inside the same labels
- `keep_simple_group_names` - whether simple comparison output uses only group names

## Usage

```python
from analytics_toolkit.ab_utils import format_ab_metrics

formatted = format_ab_metrics(
    result_df,
    label_cols=["segment"],
    output_type=["metric_values", "p_values", "delta_relative_significant"],
    significance_alpha=0.05,
    significance_p_value="p_values",
)
```

Output example:

```python
formatted.head()
#   segment metric  control  test_1 p-value test_1 delta_relative_significant test_1
# 0     all orders     0.42    0.45             0.041                            7.1%
# 1     all    ctr     0.08    0.09             0.018                           12.5%
```

## Notes

- With the default `output_type`, output contains metric values by experiment
  group.
- Significant delta outputs keep deltas only when the configured p-value is
  below `significance_alpha`.

[All AB functions](index.md)
