import json
import logging

from app.log import JsonFormatter


def test_json_formatter_includes_extra_fields():
    record = logging.makeLogRecord(
        {"name": "app.llm", "levelno": logging.INFO, "levelname": "INFO", "msg": "llm request"}
    )
    record.event, record.ticket_id, record.no_key = "llm_request", "t-1", True
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "llm request"
    assert line["event"] == "llm_request"
    assert line["ticket_id"] == "t-1"
    assert line["no_key"] is True
    assert line["level"] == "INFO"
