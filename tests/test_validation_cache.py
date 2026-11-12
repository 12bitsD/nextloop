from __future__ import annotations

import json

import pytest
from jsonschema.exceptions import UnknownType

from nextloop import ToolCall, ToolSpec
from nextloop.execute.tools import validate_calls


def test_schema_cache_does_not_mix_different_schemas():
    string_spec = ToolSpec(
        "read", "Read", {"type": "object", "properties": {"path": {"type": "string"}}}
    )
    integer_spec = ToolSpec(
        "read", "Read", {"type": "object", "properties": {"path": {"type": "integer"}}}
    )
    call = ToolCall("c", "read", json.dumps({"path": "a.txt"}))

    assert validate_calls([call], {"read": string_spec})[1] == ()
    _, issues = validate_calls([call], {"read": integer_spec})

    assert len(issues) == 1
    assert "is not of type 'integer'" in issues[0].message


def test_invalid_schema_still_fails_on_first_validation():
    spec = ToolSpec("read", "Read", {"type": "not-a-type"})
    call = ToolCall("c", "read", "{}")

    with pytest.raises(UnknownType):
        validate_calls([call], {"read": spec})
