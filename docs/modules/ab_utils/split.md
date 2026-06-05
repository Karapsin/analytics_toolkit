[AB utilities index](index.md)

# Split Users

`do_split` creates deterministic AB group assignments:

```python
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

The result contains sampled input rows plus `group_name` and
`is_mandatory_user`. Groups are named `control`, `test_1`, ..., `test_N`.
When `test_group_ratios` is omitted, all groups are equal size. Custom ratios
must be ordered as `[control, test_1, ...]`, contain positive numeric values,
and sum to `100`.

`mandatory_users_df` can be used to guarantee selected users are included. It
must contain the same id column as `split_col`; ids missing from `df` are
warned about and ignored. `mandatory_users_group` supports:

- `"any"`: mandatory users are included and assigned like regular sampled users
- `"control"`: mandatory users are forced into control
- `"test_any"`: mandatory users are split across test groups
- `"test_1"`, `"test_2"`, etc.: mandatory users are forced into that exact test group

With `compensate_mandatory_users=False`, group ratios are applied to randomized
users and forced mandatory users are added afterward. With
`compensate_mandatory_users=True`, group ratios are applied to final counts
including mandatory users; impossible final quotas raise `ValueError`.

Stratification uses exact tuples of `stratification_cols`; missing values share
a stable missing bucket. Rare strata are still assigned randomly while global
group quotas are preserved.

[AB utilities index](index.md)
