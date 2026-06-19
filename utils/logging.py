import logging, json, sys, os
from datetime import datetime, timezone, date as _date
from typing import Any, Dict, Optional


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        base: Dict[str, Any] = {
            "ts":     ts,
            "lvl":    record.levelname,
            "msg":    record.getMessage(),
            "logger": record.name,
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            base.update(extra)
        return json.dumps(base, ensure_ascii=False)


class DailyFileHandler(logging.FileHandler):
    """logs/YYMM/YYYY-MM-DD.log — 자정마다 새 날짜 파일로 자동 전환."""

    def __init__(self, base_dir: str = "logs"):
        self._base_dir = base_dir
        self._current_date: Optional[_date] = None
        path = self._today_path()
        super().__init__(path, encoding="utf-8", delay=False)

    def _today_path(self) -> str:
        now = datetime.now()
        self._current_date = now.date()
        folder = os.path.join(self._base_dir, now.strftime("%y%m"))
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, now.strftime("%Y-%m-%d") + ".log")

    def emit(self, record: logging.LogRecord) -> None:
        if datetime.now().date() != self._current_date:
            self.close()
            new_path = self._today_path()
            self.baseFilename = os.path.abspath(new_path)
            self.stream = self._open()
        super().emit(record)


def setup_root_logger(level: int = logging.INFO, log_dir: str = "logs") -> logging.Logger:
    logger = logging.getLogger()
    logger.setLevel(level)
    fmt = JsonFormatter()

    # stdout 핸들러 (중복 방지)
    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    )
    if not has_stream:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    # 파일 핸들러 (중복 방지)
    if not any(isinstance(h, DailyFileHandler) for h in logger.handlers):
        fh = DailyFileHandler(log_dir)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def setup_logging(level: int = logging.INFO, log_dir: str = "logs") -> logging.Logger:
    return setup_root_logger(level, log_dir)
