from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from ..contracts import ProviderError, merge_parameters

__all__ = [
    "ParameterValidationError",
    "build_wire_parameters",
    "merge_parameters",
    "parameter_error",
    "to_wire_parameters",
    "validate_parameters",
]

RESERVED_PARAMETER_KEYS = frozenset(
    {"model", "messages", "stream", "tools", "system"}
)


class ParameterValidationError(ValueError):
    def __init__(self, param: str | None, message: str) -> None:
        super().__init__(message)
        self.param = param


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_string_list(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > 0
        and all(isinstance(item, str) and item != "" for item in value)
    )


# bits:explain(BCS003): single validation table; split scatters field rules
def validate_parameters(raw: Mapping[str, Any]) -> None:
    if not isinstance(raw, Mapping):
        raise ParameterValidationError(None, "parameters must be a mapping")
    for key, value in raw.items():
        # bits:allow(BCS007): Python has no brace blocks
        if not isinstance(key, str) or not key:
            raise ParameterValidationError(
                None, "parameter names must be non-empty strings"
            )
        if key in RESERVED_PARAMETER_KEYS:
            raise ParameterValidationError(
                key, f"parameter '{key}' is reserved and managed by nextloop"
            )
        if value is None:
            continue
        if key == "temperature":
            if not _is_number(value) or not math.isfinite(value) or value < 0:
                raise ParameterValidationError(
                    key, "temperature must be a finite number >= 0"
                )
        elif key == "top_p":
            if (
                not _is_number(value)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ParameterValidationError(
                    key, "top_p must be a finite number between 0 and 1"
                )
        elif key == "top_k":
            if not _is_int(value) or value < 1:
                raise ParameterValidationError(
                    key, "top_k must be an integer >= 1"
                )
        elif key in ("max_tokens", "max_completion_tokens"):
            if not _is_int(value) or value < 1:
                raise ParameterValidationError(
                    key, f"{key} must be an integer >= 1"
                )
        elif key == "seed":
            if not _is_int(value):
                raise ParameterValidationError(key, "seed must be an integer")
        elif key in ("presence_penalty", "frequency_penalty"):
            if (
                not _is_number(value)
                or not math.isfinite(value)
                or not -2 <= value <= 2
            ):
                raise ParameterValidationError(
                    key, f"{key} must be a finite number between -2 and 2"
                )
        elif key == "stop":
            if not (
                (isinstance(value, str) and value != "")
                or _valid_string_list(value)
            ):
                raise ParameterValidationError(
                    key,
                    "stop must be a non-empty string or list of non-empty strings",
                )
        elif key == "stop_sequences" and not _valid_string_list(value):
            raise ParameterValidationError(
                key, "stop_sequences must be a list of non-empty strings"
            )
    if (
        raw.get("max_tokens") is not None
        and raw.get("max_completion_tokens") is not None
    ):
        raise ParameterValidationError(
            "max_completion_tokens",
            "max_tokens and max_completion_tokens are mutually exclusive",
        )


def to_wire_parameters(
    merged: Mapping[str, Any], protocol: str
) -> dict[str, Any]:
    validate_parameters(merged)
    wire = dict(merged)
    if protocol == "anthropic":
        if "stop" in wire and "stop_sequences" in wire:
            raise ParameterValidationError(
                "stop",
                "use either 'stop' or 'stop_sequences' for Anthropic, not both",
            )
        if "stop" in wire:
            stop = wire.pop("stop")
            wire["stop_sequences"] = (
                [stop] if isinstance(stop, str) else list(stop)
            )
    if isinstance(wire.get("stop"), tuple):
        wire["stop"] = list(wire["stop"])
    return wire


def without_unset_parameters(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if value is not None}


def build_wire_parameters(
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
    protocol: str,
) -> dict[str, Any]:
    try:
        validate_parameters(overrides)
        merged = without_unset_parameters(merge_parameters(defaults, overrides))
        return to_wire_parameters(merged, protocol)
    except ParameterValidationError as exc:
        raise ProviderError(
            f"Invalid model parameters: {exc}", param=exc.param
        ) from exc


def _extract_error_fields(
    error_text: str,
) -> tuple[str | None, str | None, str | None]:
    try:
        payload = json.loads(error_text)
    except ValueError:
        return None, None, None
    if not isinstance(payload, Mapping):
        return None, None, None
    error = payload.get("error")
    if isinstance(error, Mapping):
        param = error.get("param")
        code = error.get("code") or error.get("type")
        message = error.get("message")
        return (
            param if isinstance(param, str) and param else None,
            code if isinstance(code, str) and code else None,
            message if isinstance(message, str) and message else None,
        )
    detail = payload.get("detail")
    if isinstance(detail, list) and detail and isinstance(detail[0], Mapping):
        first = detail[0]
        loc = first.get("loc")
        param = (
            loc[-1]
            if isinstance(loc, list) and loc and isinstance(loc[-1], str)
            else None
        )
        code = first.get("type")
        message = first.get("msg")
        return (
            param,
            code if isinstance(code, str) and code else None,
            message if isinstance(message, str) and message else None,
        )
    return None, None, None


def parameter_error(
    status: int, error_text: str, provider_name: str
) -> ProviderError:
    param, code, message = _extract_error_fields(error_text)
    retryable = status in (408, 409, 429) or status >= 500
    if param is not None:
        detail = message or error_text
        text = (
            f"{provider_name} HTTP {status} "
            f"rejected parameter '{param}': {detail}"
        )
    elif message is not None and not retryable:
        text = f"{provider_name} HTTP {status}: {message}"
    else:
        text = f"{provider_name} HTTP {status}: {error_text}"
    return ProviderError(
        text,
        retryable=retryable,
        code=code or str(status),
        param=param,
    )
