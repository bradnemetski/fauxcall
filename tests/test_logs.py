from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING

import pytest
import structlog

from fauxcall.logs import (
    REDACTED,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    is_secret_key,
    redact_secrets,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    clear_context()
    structlog.reset_defaults()
    logging.getLogger().handlers = []


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_output_with_context_static_fields_and_redaction() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", "json", stream=stream, static={"fake": "petstore"})
    bind_context(request_id="req-1")
    get_logger("fauxcall.test").info(
        "request",
        status=200,
        headers={"Authorization": "Bearer abc", "Accept": "json", "nested": [{"api_key": "k"}]},
        password="hunter2",
    )
    (line,) = _lines(stream)
    assert line["event"] == "request"
    assert line["level"] == "info"
    assert line["logger"] == "fauxcall.test"
    assert line["request_id"] == "req-1"
    assert line["fake"] == "petstore"
    assert line["password"] == REDACTED
    assert line["headers"] == {
        "Authorization": REDACTED,
        "Accept": "json",
        "nested": [{"api_key": REDACTED}],
    }
    assert str(line["timestamp"]).endswith("Z")


def test_stdlib_libraries_flow_through_the_same_pipeline() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    logging.getLogger("uvicorn.error").info("server started on %s", "port 8000")
    logging.getLogger("httpx2").info("suppressed: below WARNING for noisy libraries")
    logging.getLogger("httpx2").warning("kept")
    events = [line["event"] for line in _lines(stream)]
    assert events == ["server started on port 8000", "kept"]


def test_exceptions_are_rendered_in_json() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)

    def explode() -> None:
        raise RuntimeError("boom")

    try:
        explode()
    except RuntimeError:
        get_logger().exception("failed")
    (line,) = _lines(stream)
    assert "RuntimeError: boom" in str(line["exception"])


def test_console_and_auto_formats() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "console", stream=stream)
    get_logger().info("hello", user="alice")
    assert "hello" in stream.getvalue()
    assert "user" in stream.getvalue()

    auto = io.StringIO()  # not a TTY -> JSON
    configure_logging(logging.INFO, "auto", stream=auto)
    get_logger().info("auto")
    assert _lines(auto)[0]["event"] == "auto"

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    tty = Tty()
    configure_logging("INFO", "auto", stream=tty)
    get_logger().info("on a terminal")
    assert "on a terminal" in tty.getvalue()
    assert not tty.getvalue().lstrip().startswith("{")


def test_level_filtering_and_clear_context() -> None:
    stream = io.StringIO()
    configure_logging("WARNING", "json", stream=stream)
    bind_context(session="s1")
    get_logger().info("dropped")
    clear_context()
    get_logger().warning("kept")
    (line,) = _lines(stream)
    assert line["event"] == "kept"
    assert "session" not in line


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("Authorization", True),
        ("x-api-key", True),
        ("apiKey", True),
        ("access_token", True),
        ("client_secret", True),
        ("Set-Cookie", True),
        ("session_id", True),
        ("status", False),
        ("tokens_used_count_label", True),
        ("name", False),
    ],
)
def test_is_secret_key(key: str, expected: bool) -> None:
    assert is_secret_key(key) is expected


def test_redaction_handles_tuples_and_deep_nesting() -> None:
    deep: dict[str, object] = {"token": "x"}
    for _ in range(12):
        deep = {"inner": deep}
    event = redact_secrets(None, "info", {"event": "e", "pair": ("a", {"secret": 1}), "deep": deep})
    assert event["pair"] == ("a", {"secret": REDACTED})
    assert event["event"] == "e"
