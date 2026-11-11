"""Backend-native nested representations with verified lossless structure."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
from sqlglot import exp, parse_one

from .map_key_domains import map_key_conversion_is_injective

_TRINO_INFERRED_TIMESTAMP_SCALE = 6
_MAP_ARGUMENT_COUNT = 2

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile


def resolve_nested(profile: ColumnProfile, backend: str, source_type: str | None) -> str | None:  # noqa: C901, PLR0911, PLR0912 - native nested families
    from .type_resolution import (  # noqa: PLC0415 - recursive resolution
        resolve_profile,
    )

    if not profile.structurally_compatible:
        return None
    if not profile.families:
        from .value_profiles import ColumnProfile  # noqa: PLC0415 - empty family defaults

        kind, fields = nested_descriptor(source_type or "")
        if kind:
            profile = ColumnProfile(
                families={"array" if kind == "gp_array" else "tuple" if kind == "row" else kind},
                native_type=source_type,
                source_backend=profile.source_backend,
                children=[
                    ColumnProfile(native_type=native, source_backend=profile.source_backend)
                    for _, native in fields
                ],
                array_leaves=ColumnProfile(source_backend=profile.source_backend)
                if kind in {"array", "gp_array"}
                else None,
                null_count=profile.null_count,
            )
    if backend == "gp":
        if profile.families != {"array"} or not profile.gp_array_compatible:
            return None
        leaves = profile.array_leaves
        if leaves is None:
            return "TEXT[]"
        native = source_type or ""
        while nested_descriptor(native)[0] in {"array", "gp_array"}:
            native = nested_descriptor(native)[1][0][1]
        leaf = resolve_profile(
            leaves, backend, source_type=native or None, source_backend=profile.source_backend or ""
        )
        return None if leaf.text_fallback else leaf.target_type + "[]"
    if backend not in {"ch", "trino"}:
        return None
    if backend == "ch" and profile.null_count:
        return None
    if profile.families == {"array"}:
        kind, fields = nested_descriptor(source_type or "")
        child_source = profile.children[0].native_type if profile.children else None
        if kind == "gp_array" and profile.children:
            if "array" in profile.children[0].families and profile.children[0].families != {
                "array"
            }:
                return None
            child_source = (
                source_type if profile.children[0].families == {"array"} else fields[0][1]
            )
        child = (
            resolve_profile(
                profile.children[0],
                backend,
                source_type=child_source,
                source_backend=profile.source_backend or "",
            )
            if profile.children
            else None
        )
        if child and child.text_fallback:
            return None
        child_type = child.target_type if child else {"ch": "String", "trino": "VARCHAR"}[backend]
        return f"{'Array' if backend == 'ch' else 'ARRAY'}({child_type})"
    if profile.families == {"tuple"}:
        resolved = [
            resolve_profile(
                child,
                backend,
                source_type=child.native_type,
                source_backend=profile.source_backend or "",
            )
            for child in profile.children
        ]
        if any(child.text_fallback for child in resolved):
            return None
        children = [child.target_type for child in resolved]
        if backend == "ch":
            return "Tuple(" + ", ".join(children) + ")"
        names = [name for name, _ in nested_descriptor(source_type or "")[1]]
        rendered_fields: list[str] = []
        for index, native in enumerate(children):
            name = names[index] if index < len(names) else f"field{index}"
            identifier = exp.Identifier(this=name, quoted=True).sql(dialect="trino")
            rendered_fields.append(f"{identifier} {native}")
        return "ROW(" + ", ".join(rendered_fields) + ")"
    if profile.families == {"map"}:
        if backend == "trino" and (
            profile.map_duplicate_keys or profile.map_null_keys or not profile.map_comparable_keys
        ):
            return None
        if not profile.children:
            return "Map(String, String)" if backend == "ch" else "MAP(VARCHAR, VARCHAR)"
        key, value = (
            resolve_profile(
                child,
                backend,
                source_type=child.native_type,
                source_backend=profile.source_backend or "",
            )
            for child in profile.children
        )
        if profile.children[0].null_count or key.text_fallback or value.text_fallback:
            return None
        if backend == "trino" and profile.children[0].families & {"array", "tuple", "map"}:
            return None  # Connector/client key support is not proved for composite keys.
        return f"{'Map' if backend == 'ch' else 'MAP'}({key.target_type}, {value.target_type})"
    return None


def nested_descriptor(type_name: str) -> tuple[str, list[tuple[str, str]]]:
    """Resolve named ROW fields structurally, including quoted identifiers."""
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native type grammar

    parsed = parse_native_type(type_name).base
    if type_name.strip().endswith("[]"):
        return "gp_array", [("", type_name.strip()[:-2])]
    if parsed.name in {"array", "map"}:
        return parsed.name, [
            (f"field{index}", child) for index, child in enumerate(parsed.arguments)
        ]
    if parsed.name in {"row", "tuple"}:
        dialect = "trino" if parsed.name == "row" else "clickhouse"
        dtype = parse_one(type_name, read=dialect, into=exp.DataType)
        fields = []
        for index, field in enumerate(dtype.expressions):
            if isinstance(field, exp.ColumnDef):
                fields.append((field.name, field.args["kind"].sql(dialect=dialect)))
            else:
                fields.append((f"field{index}", field.sql(dialect=dialect)))
        return parsed.name, fields
    return "", []


def convert_nested(
    value: Any, type_name: str, backend: str = "", *, destination_timezone: str | None = None
) -> Any:
    from .type_resolution import ResolvedColumn  # noqa: PLC0415 - recursive exact conversion

    kind, children = nested_descriptor(type_name)
    if kind == "gp_array":
        native = children[0][1]
        while native.endswith("[]"):
            native = native[:-2]
        leaf = ResolvedColumn(
            native, target_backend=backend, destination_timezone=destination_timezone
        )
        return [
            convert_nested(item, type_name, backend, destination_timezone=destination_timezone)
            if isinstance(item, list) or (isinstance(item, np.ndarray) and item.ndim > 0)
            else leaf.convert(item)
            for item in value
        ]
    if kind == "array":
        conversion = ResolvedColumn(
            children[0][1], target_backend=backend, destination_timezone=destination_timezone
        )
        return [conversion.convert(item) for item in value]
    if kind in {"tuple", "row"}:
        items = [value[name] for name, _ in children] if isinstance(value, dict) else value
        return tuple(
            ResolvedColumn(
                native, target_backend=backend, destination_timezone=destination_timezone
            ).convert(item)
            for item, (_, native) in zip(items, children)
        )
    if kind == "map":
        key_type, value_type = (
            ResolvedColumn(
                native, target_backend=backend, destination_timezone=destination_timezone
            )
            for _, native in children
        )
        pairs = [(key_type.convert(key), value_type.convert(item)) for key, item in value.items()]
        from .map_values import converted_map  # noqa: PLC0415 - preserve converted key identity

        return converted_map(
            pairs, backend, mapping_input=isinstance(value, dict), key_type=children[0][1]
        )
    return value


def validate_nested(  # noqa: C901 - recursive authoritative nested schema
    profile: ColumnProfile,
    type_name: str,
    backend: str,
    *,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
) -> bool:
    from .type_resolution import validate_profile  # noqa: PLC0415 - recursive authority validation

    kind, children = nested_descriptor(type_name)
    if not kind:
        return False
    if kind == "gp_array":
        if profile.families - {"array"} or not profile.gp_array_compatible:
            msg = "Selected input is incompatible with authoritative rectangular array schema."
            raise ValueError(msg)
        if profile.array_leaves is not None:
            native = children[0][1]
            while native.endswith("[]"):
                native = native[:-2]
            validate_profile(
                profile.array_leaves,
                native,
                backend,
                allow_timestamp_precision_loss=allow_timestamp_precision_loss,
                destination_timezone=destination_timezone,
            )
        return True
    family = "tuple" if kind == "row" else kind
    if profile.families - {family} or not profile.structurally_compatible:
        msg = "Selected input is incompatible with authoritative nested schema."
        raise ValueError(msg)
    if (
        kind == "map"
        and backend == "trino"
        and (profile.map_duplicate_keys or profile.map_null_keys or not profile.map_comparable_keys)
    ):
        msg = "Selected input cannot preserve its keys in the authoritative map schema."
        raise ValueError(msg)
    if kind == "map" and profile.map_null_keys:
        msg = "Selected input contains NULL keys for the authoritative map schema."
        raise ValueError(msg)
    if kind in {"row", "tuple"} and profile.children and len(profile.children) != len(children):
        msg = "Selected tuple width differs from authoritative nested schema."
        raise ValueError(msg)
    for child, (_, native) in zip(profile.children, children):
        validate_profile(
            child,
            native,
            backend,
            allow_timestamp_precision_loss=allow_timestamp_precision_loss,
            destination_timezone=destination_timezone,
        )
    return True


def map_declared_nested(  # noqa: PLR0911 - native declaration families
    source_type: str, target_backend: str, source_backend: str
) -> str | None:
    from .models import SourceColumn  # noqa: PLC0415 - backend policy recursion
    from .source_schema import map_source_column  # noqa: PLC0415 - backend policy recursion
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native wrappers

    kind, fields = nested_descriptor(source_type)
    if not kind:
        return None
    if target_backend == "gp":
        if kind not in {"array", "gp_array"}:
            return None
        native = fields[0][1]
        while nested_descriptor(native)[0] in {"array", "gp_array"}:
            native = nested_descriptor(native)[1][0][1]
        if nested_descriptor(native)[0]:
            return None
        return map_source_column(SourceColumn("", native), target_backend, source_backend) + "[]"
    mapped = [
        (name, map_source_column(SourceColumn(name, native), target_backend, source_backend))
        for name, native in fields
    ]
    if kind in {"array", "gp_array"}:
        return f"{'Array' if target_backend == 'ch' else 'ARRAY'}({mapped[0][1]})"
    if kind == "map":
        key = (
            parse_native_type(mapped[0][1]).base.original
            if target_backend == "ch"
            else mapped[0][1]
        )
        return f"{'Map' if target_backend == 'ch' else 'MAP'}({key}, {mapped[1][1]})"
    if target_backend == "ch":
        return "Tuple(" + ", ".join(native for _, native in mapped) + ")"
    return (
        "ROW("
        + ", ".join(
            f"{exp.Identifier(this=name, quoted=True).sql(dialect='trino')} {native}"
            for name, native in mapped
        )
        + ")"
    )


def declared_nested_fits(  # noqa: PLR0911 - nested domain proof
    source_type: str, target_type: str, target_backend: str, source_backend: str | None
) -> bool | None:
    from .type_resolution import declared_domain_fits  # noqa: PLC0415 - recursive domain proof

    source, source_fields = nested_descriptor(source_type)
    target, target_fields = nested_descriptor(target_type)
    if not source and not target:
        return None
    if source == "gp_array" and target_backend != "gp":
        return False  # GP [] metadata does not encode the actual rank.
    if target == "gp_array":
        if source == "gp_array":
            source_leaf, target_leaf = source_fields[0][1], target_fields[0][1]
            while source_leaf.endswith("[]"):
                source_leaf = source_leaf[:-2]
            while target_leaf.endswith("[]"):
                target_leaf = target_leaf[:-2]
            return declared_domain_fits(
                source_leaf,
                target_leaf,
                target_backend,
                source_backend=source_backend,
            )
        if source != "array" or nested_descriptor(source_fields[0][1])[0]:
            return False  # Native nested arrays can be ragged or contain NULL subarrays.
        return declared_domain_fits(
            source_fields[0][1], target_fields[0][1], target_backend, source_backend=source_backend
        )
    if source == "map" and target_backend == "trino":
        return bool(
            source_backend == "trino"
            and target == "map"
            and len(source_fields) == len(target_fields) == _MAP_ARGUMENT_COUNT
            and map_key_conversion_is_injective(
                source_fields[0][1], target_fields[0][1], target_backend, source_backend
            )
            and declared_domain_fits(
                source_fields[1][1],
                target_fields[1][1],
                target_backend,
                source_backend=source_backend,
            )
        )
    same_shape = source == target or {source, target} <= {"tuple", "row"}
    return bool(
        same_shape
        and len(source_fields) == len(target_fields)
        and all(
            declared_domain_fits(left[1], right[1], target_backend, source_backend=source_backend)
            for left, right in zip(source_fields, target_fields)
        )
    )


def needs_timestamp_adaptation(type_name: str) -> bool:
    from .type_resolution import (  # noqa: PLC0415 - native precision contract
        is_timestamp_type,
        timestamp_scale,
    )

    kind, fields = nested_descriptor(type_name)
    return (
        any(needs_timestamp_adaptation(native) for _, native in fields)
        if kind
        else (
            is_timestamp_type(type_name)
            and timestamp_scale(type_name) > _TRINO_INFERRED_TIMESTAMP_SCALE
        )
    )
