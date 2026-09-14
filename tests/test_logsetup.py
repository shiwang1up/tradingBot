# tests/test_logsetup.py
import json
import logging

from tradebot.engine.logsetup import setup_logging


def test_jsonl_file_carries_run_id_epoch_and_ist_time(tmp_path):
    path = setup_logging(tmp_path / "logs", run_id="run-x")
    log = logging.getLogger("tradebot.test")
    log.info("hello %s", "world", extra={"symbol": "RELIANCE", "client_id": "abc"})
    logging.getLogger().handlers[-1].flush()
    line = json.loads(path.read_text().splitlines()[-1])
    assert line["run_id"] == "run-x" and line["msg"] == "hello world"
    assert isinstance(line["ts"], int) and line["time"].endswith("+05:30")
    assert line["symbol"] == "RELIANCE" and line["client_id"] == "abc"
    assert path.name.endswith(".jsonl")


def test_setup_without_dir_is_console_only(tmp_path):
    assert setup_logging(None, run_id="r") is None
    assert len(logging.getLogger().handlers) == 1
