[AB utilities index](index.md)

# Experiment Splitting

Experiment splitting starts with a stable user identifier and ends with an
analysis-ready dataframe that contains the original user rows plus the assigned
group column. Use [do_split](functions/do-split.md) when the assignment should
be reproducible across runs.

```python
from analytics_toolkit.ab_utils import do_split

split_df = do_split(
    users_df,
    split_col="user_id",
    stratification_cols=["country", "platform"],
    target_sample_size=100_000,
    test_groups_num=2,
    test_group_ratios=[100 / 6, (100 / 6) * 2, (100 / 6) * 3],
    random_state=42,
)
```

The output contains sampled input rows plus `group_name` and
`is_mandatory_user`. Groups are named `control`, `test_1`, ..., `test_N`.
When custom ratios are supplied, they are ordered as `[control, test_1, ...]`
and must sum to `100`.

Use mandatory users when specific ids must appear in the experiment. They can be
assigned like regular sampled users, forced into control, spread across test
groups, or forced into an exact test group. With compensation enabled, final
group quotas include mandatory users; impossible final quotas raise
`ValueError`.

Stratification uses exact tuples of `stratification_cols`; missing values share
a stable missing bucket. Rare strata are still assigned randomly while global
group quotas are preserved.

[AB utilities index](index.md)
