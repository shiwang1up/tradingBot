"""Logging per spec 13: JSON lines to <logs>/YYYY-MM-DD.jsonl (daily file) plus readable console.
Every line carries the run id, the epoch and an ISO-8601 IST timestamp."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional, Union

from tradebot.engine.clock import iso_ist, date_of


class _JsonFormatter(logging.Formatter):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        ts = int(record.created)
        line = {
            "ts": ts, "time": iso_ist(ts), "level": record.levelname, "logger": record.name,
            "run_id": self.run_id, "msg": record.getMessage(),
        }
        for key in ("symbol", "client_id"):
            if hasattr(record, key):
                line[key] = getattr(record, key)
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def setup_logging(logs_dir: Union[str, Path, None], run_id: str, console_level: int = logging.INFO) -> Optional[Path]:
    """Install the console handler and, when `logs_dir` is given, today's JSONL file handler.
    Returns the log file path. Idempotent per process: earlier handlers are replaced."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(console)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # the Anthropic SDK logs every request at INFO otherwise
    if not logs_dir:
        return None
    d = Path(logs_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{date_of(int(time.time())).isoformat()}.jsonl"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(_JsonFormatter(run_id))
    root.addHandler(fh)
    return path
