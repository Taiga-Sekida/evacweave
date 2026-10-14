import json
import logging

from packages.services.structured_log import structured_logger, trace_field


def test_logger_writes_one_json_line_per_entry_to_stdout(capsys):
    logger = structured_logger("evacweave.test.stdout")
    logger.info("agent_step", extra={"fields": {"step": 2, "action": "finish"}})
    try:
        raise ValueError("secret payload")
    except ValueError:
        logger.exception("agent_failed")
    first, second = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert first == {
        "severity": "INFO",
        "message": "agent_step",
        "logger": "evacweave.test.stdout",
        "step": 2,
        "action": "finish",
    }
    assert second["severity"] == "ERROR"
    assert second["error_type"] == "ValueError"
    assert "secret payload" not in json.dumps(second)  # exceptions are typed, not echoed


def test_logger_is_configured_once():
    logger = structured_logger("evacweave.test.once")
    assert structured_logger("evacweave.test.once") is logger
    assert len(logger.handlers) == 1 and logger.propagate is False
    assert logger.level == logging.INFO


def test_trace_field_accepts_only_well_formed_headers():
    header = "0123456789abcdef0123456789abcdef/42;o=1"
    assert trace_field("proj", header) == {
        "logging.googleapis.com/trace": "projects/proj/traces/0123456789abcdef0123456789abcdef"
    }
    for bad in (None, "", "not-a-trace", "0123/1;o=1", header + "\n", header + "\n" + header):
        assert trace_field("proj", bad) == {}
    assert trace_field("", header) == {}
