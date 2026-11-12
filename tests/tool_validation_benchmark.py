from __future__ import annotations

import json
import time

from nextloop import ToolCall, ToolSpec
from nextloop.execute.tools import validate_calls


def run_once(count: int = 5000) -> dict[str, object]:
    spec = ToolSpec("read", "Read", {"type": "object", "properties": {"path": {"type": "string"}}})
    call = ToolCall("c", "read", json.dumps({"path": "a.txt"}))
    started = time.perf_counter()
    for _ in range(count):
        validated, issues = validate_calls([call], {"read": spec})
        assert not issues and len(validated) == 1
    return {"calls": count, "seconds": time.perf_counter() - started}


def main() -> None:
    print(json.dumps(run_once(), sort_keys=True))


if __name__ == "__main__":
    main()
