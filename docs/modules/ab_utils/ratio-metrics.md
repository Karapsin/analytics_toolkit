[AB utilities index](index.md)

# Ratio Metrics

The output also reports `variance_group_1`, `variance_group_2`, and `s.e.` for each
comparison. Mean metrics and `level="user"` ratio metrics use sample variances
with `ddof=1`; `level="agg"` ratio metrics use delta-method ratio variances.

Pass ratio metrics to
[compute_test_metrics](functions/compute-test-metrics.md) via `ratio_metrics`,
for example:

```python
ratio_metrics = [
    {"name": "ctr", "numerator": "clicks", "denominator": "views"},
    {"name": "ctr_user", "numerator": "clicks", "denominator": "views", "level": "user"},
]
```

Supported ratio options:

- `level`: `"agg"` (default) or `"user"`
- `invalid_denominator`: `"ignore"` (default)

Ratio row filtering:

- `level="user"`: rows with missing numerator/denominator or `denominator <= 0` are ignored
- `level="agg"`: rows with missing numerator/denominator are ignored; zero denominators are kept and contribute to the aggregate sums

[AB utilities index](index.md)
