from __future__ import annotations

import io
import json

from watcher.output import compact_json, write_json


def test_compact_json_is_deterministic_and_unicode_safe() -> None:
    payload = {"z": "Lyon 6e", "a": [1, 2]}
    assert compact_json(payload) == '{"a":[1,2],"z":"Lyon 6e"}'


def test_write_json_emits_exactly_one_json_line() -> None:
    stream = io.StringIO()
    write_json({"status": "NO_CHANGE"}, stream)
    assert stream.getvalue().count("\n") == 1
    assert json.loads(stream.getvalue()) == {"status": "NO_CHANGE"}
