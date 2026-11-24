from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..contracts import ProviderError, merge_parameters

__all__ = [
    "ParameterValidationError",
    "build_wire_parameters",
    "check_reserved_parameters",
    "merge_parameters",
    "parameter_error",
]

RESERVED_PARAMETER_KEYS = frozenset(
    {"model", "messages", "stream", "tools", "system"}
)


class ParameterValidationError(ValueError):
    def __init__(self, param: str | None, message: str) -> None:
        super().__init__(message)
        self.param = param


def check_reserved_parameters(raw: Mapping[str, Any]) -> None:
    if not isinstance(raw, Mapping):
        raise ParameterValidationError(None, "parameters must be a mapping")
    reserved = RESERVED_PARAMETER_KEYS.intersection(raw)
    if reserved:
        key = sorted(reserved)[0]
        raise ParameterValidationError(
            key, f"parameter '{key}' is reserved and managed by nextloop"
        )


def build_wire_parameters(
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        check_reserved_parameters(defaults)
        check_reserved_parameters(overrides)
    except ParameterValidationError as exc:
        raise ProviderError(str(exc), param=exc.param) from exc
    merged = merge_parameters(defaults, overrides)
    return {key: value for key, value in merged.items() if value is not None}


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
