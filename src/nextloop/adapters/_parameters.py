from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts import ProviderError, merge_parameters

__all__ = [
    "build_wire_parameters",
    "check_reserved_parameters",
    "merge_parameters",
]

RESERVED_PARAMETER_KEYS = frozenset(
    {"model", "messages", "stream", "tools", "system"}
)


def check_reserved_parameters(raw: Mapping[str, Any]) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError("parameters must be a mapping")
    reserved = RESERVED_PARAMETER_KEYS.intersection(raw)
    if reserved:
        key = sorted(reserved)[0]
        raise ValueError(
            f"parameter '{key}' is reserved and managed by nextloop"
        )


def build_wire_parameters(
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        check_reserved_parameters(defaults)
        check_reserved_parameters(overrides)
    except ValueError as exc:
        raise ProviderError(str(exc)) from exc
    merged = merge_parameters(defaults, overrides)
    return {key: value for key, value in merged.items() if value is not None}
