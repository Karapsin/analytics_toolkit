from __future__ import annotations

import pytest
from release_routines.lib.check_minimum_constraints import validate_minimum_constraints


@pytest.mark.parametrize("reverse", [False, True])
def test_conditional_branches_each_require_their_exact_floor(reverse: bool) -> None:
    dependencies = [
        "driver>=0.5.14,<1; python_version < '3.13'",
        "driver>=1.4,<2; python_version >= '3.13'",
    ]
    if reverse:
        dependencies.reverse()
    constraints = (
        'driver==0.5.14; python_version < "3.13" # legacy floor\n'
        'driver==1.4.0; python_version >= "3.13"\n'
    )
    assert validate_minimum_constraints(dependencies, constraints) == []
    failures = validate_minimum_constraints(dependencies, constraints.replace("1.4.0", "1.3.0"))
    assert any("expected lower bound '1.4'" in failure for failure in failures)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("python_version < '3.13'", "python_version >= '3.13'"),
        ("python_version <= '3.12'", "python_version > '3.12'"),
        ("python_version <= '3.12'", "python_version >= '3.13'"),
        ("python_version == '3.13'", "python_version != '3.13'"),
        ("python_version != '3.13'", "python_version == '3.13'"),
        ("sys_platform == 'linux'", "sys_platform == 'win32'"),
    ],
)
def test_validator_accepts_provably_disjoint_partitions(left: str, right: str) -> None:
    dependencies = [f"driver>=1; {left}", f"driver>=2; {right}"]
    constraints = f"driver==1; {left}\ndriver==2; {right}\n"
    assert validate_minimum_constraints(dependencies, constraints) == []
    assert validate_minimum_constraints(list(reversed(dependencies)), constraints) == []


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("", ""),
        ("", "; python_version >= '3.13'"),
        ("; python_version >= '3.10'", "; python_version >= '3.13'"),
        ("; python_version >= '3.13'", "; python_version <= '3.13'"),
        ("; python_version == '3.13'", "; python_version >= '3.13'"),
        ("; python_version == '3.13'", "; sys_platform == 'linux'"),
        ("; python_version > '3.12' and sys_platform == 'linux'", "; python_version < '3.13'"),
        ("; '3.13' <= python_version", "; python_version < '3.13'"),
        ("; sys_platform < '2'", "; sys_platform > '10'"),
        ("; python_version < 'unknown'", "; python_version > 'unrecognized'"),
    ],
)
def test_duplicate_runtime_names_need_a_disjointness_proof(left: str, right: str) -> None:
    failures = validate_minimum_constraints(
        [f"driver>=1{left}", f"driver>=2{right}"], "driver==1\n"
    )
    assert any("overlapping or unproven-disjoint markers" in failure for failure in failures)


@pytest.mark.parametrize(
    "constraints",
    [
        "driver==1\n",
        "driver==1; python_version >= '3.14'\n",
        "driver==1; python_version >= '3.13'\ndriver==1; python_version >= '3.13'\n",
    ],
)
def test_conditional_floor_cannot_be_hidden_by_an_unconditional_or_mismatched_pin(
    constraints: str,
) -> None:
    failures = validate_minimum_constraints(["driver>=1; python_version >= '3.13'"], constraints)
    assert failures


@pytest.mark.parametrize(
    "constraint", ["driver==1.*", "driver[extra]==1", "driver==1,!=2", "not a pin"]
)
def test_constraint_parser_keeps_exact_pin_requirement(constraint: str) -> None:
    failures = validate_minimum_constraints(["driver>=1"], constraint)
    assert any("must be an exact name==version pin" in failure for failure in failures)


def test_inclusive_floor_parser_accepts_spacing_and_rejects_invalid_names() -> None:
    assert validate_minimum_constraints(["driver >=1, <2"], "driver==1\n") == []
    with pytest.raises(ValueError, match="Unsupported dependency requirement"):
        validate_minimum_constraints(["!"], "")
