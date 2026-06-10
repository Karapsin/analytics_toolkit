[All AB functions](index.md)

# do_split

Deterministically sample users and assign AB groups, with optional
stratification and mandatory users.

```python
do_split(
    df,
    split_col="user_id",
    stratification_cols=None,
    mandatory_users_df=None,
    mandatory_users_group="any",
    target_sample_size=None,
    test_groups_num=1,
    compensate_mandatory_users=False,
    test_group_ratios=None,
    random_state=42,
    group_col="group_name",
)
```

## Inputs

- `df`: Source dataframe with users to split.
- `split_col`: User id column used for deterministic assignment.
- `target_sample_size`: Optional total sample size.
- `test_groups_num`: Number of test groups.
- `test_group_ratios`: Optional ratios for `[control, test_1, ...]`.
- `stratification_cols`: Optional column or columns for exact stratification.
- `mandatory_users_df`: Optional dataframe of users that must be included.
- `mandatory_users_group`: Forced group behavior for mandatory users.
- `compensate_mandatory_users`: Whether final group quotas include mandatory users.
- `random_state`: Random seed.
- `group_col`: Output group label column.

## Usage

```python
from analytics_toolkit.ab_utils import do_split

split_df = do_split(
    users_df,
    split_col="user_id",
    stratification_cols=["country", "platform"],
    target_sample_size=100_000,
    test_groups_num=2,
    random_state=42,
)
```

Output example:

```python
split_df[["user_id", "group_name"]].head()
#    user_id group_name
# 0        1    control
# 1        2     test_1
# 2        3     test_2
```

## Notes

- Output groups are named `control`, `test_1`, ..., `test_N`.
- `test_group_ratios` must contain positive numeric values and sum to `100`.
- Rare strata are assigned randomly while global group quotas are preserved.

[All AB functions](index.md)
