"""审核进度边车文件。

进度与 OCR 中间产物一起持久化，Worker 重启后仍可读取，
也避免为只用于界面展示的短期状态修改数据库表结构。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGE_RANGES = {
    "queued": (0, 0),
    "extracting": (2, 10),
    "vision_ocr": (10, 55),
    "reviewing": (55, 95),
    "finalizing": (96, 99),
    "done": (100, 100),
    "failed": (0, 0),
}


def _percent(stage: str, current: int, total: int) -> int:
    start, end = STAGE_RANGES.get(stage, (0, 99))
    if total <= 0 or start == end:
        return start
    ratio = max(0.0, min(1.0, current / total))
    return round(start + (end - start) * ratio)


def write_progress(
    ocr_dir: str | Path,
    stage: str,
    current: int = 0,
    total: int = 0,
    message: str = "",
) -> dict[str, Any]:
    root = Path(ocr_dir)
    root.mkdir(parents=True, exist_ok=True)
    data = {
        "stage": stage,
        "current": int(current),
        "total": int(total),
        "percent": _percent(stage, int(current), int(total)),
        "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    target = root / "progress.json"
    temporary = root / "progress.json.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    return data


def read_progress(ocr_dir: str | Path | None) -> dict[str, Any]:
    if not ocr_dir:
        return {}
    target = Path(ocr_dir) / "progress.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}
