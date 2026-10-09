"""只读取独立 sampling/executable 目录，不接触报告审核规则。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.app.config import get_settings


class SamplingRuleError(RuntimeError):
    pass


class SamplingRuleRepository:
    def __init__(self, root: Path | None = None):
        self.root = root or (Path(get_settings().knowledge_dir) / "sampling" / "executable")
        self._manifest = self._read("releases/manifest.json")
        self._common = self._read("common/rules.json")
        self._units: dict[str, dict[str, Any]] = {}
        self._models: dict[str, dict[str, Any]] = {}
        self._models_by_unit: dict[str, list[dict[str, Any]]] = {}
        self._load()

    def _read(self, relative: str) -> dict[str, Any]:
        path = self.root / relative
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SamplingRuleError(f"下样规则文件无法读取：{path}: {exc}") from exc

    def _load(self) -> None:
        for path in sorted((self.root / "units").glob("unit-*.json")):
            data = self._read(str(path.relative_to(self.root)))
            code = str(data["unit_code"]).zfill(2)
            if code in self._units:
                raise SamplingRuleError(f"下样单元重复：{code}")
            self._units[code] = data
        for path in sorted((self.root / "catalogs").glob("unit-*-models.json")):
            data = self._read(str(path.relative_to(self.root)))
            for unit in data.get("units", []):
                code = str(unit["unit_code"]).zfill(2)
                bucket = self._models_by_unit.setdefault(code, [])
                for model in unit.get("models", []):
                    model_id = model["id"]
                    if model_id in self._models:
                        raise SamplingRuleError(f"下样型号重复：{model_id}")
                    self._models[model_id] = model
                    bucket.append(model)
        missing = {
            ref for unit in self._units.values() for ref in unit.get("model_refs", [])
            if ref not in self._models
        }
        if missing:
            raise SamplingRuleError(f"下样规则引用未知型号：{', '.join(sorted(missing))}")

    @property
    def version(self) -> str:
        return str(self._manifest.get("release_id") or self._manifest.get("package_version") or self._manifest.get("version") or "unknown")

    @property
    def common(self) -> dict[str, Any]:
        return self._common

    def unit(self, code: str) -> dict[str, Any]:
        normalized = str(code).zfill(2)
        if normalized not in self._units:
            raise SamplingRuleError(f"暂未建立单元{normalized}的下样规则")
        return self._units[normalized]

    def model(self, model_ref: str) -> dict[str, Any]:
        if model_ref not in self._models:
            raise SamplingRuleError(f"未知下样型号：{model_ref}")
        return self._models[model_ref]

    def catalog(self) -> list[dict[str, Any]]:
        rows = []
        for code, unit in sorted(self._units.items()):
            rows.append({
                "unit_code": code,
                "unit_name": unit["unit_name"],
                "status": unit["status"],
                "enabled": bool(unit.get("enabled")),
                "version": unit["version"],
                "open_questions": unit.get("open_questions", []),
                "models": self._models_by_unit.get(code, []),
            })
        return rows


@lru_cache(maxsize=1)
def get_sampling_repository() -> SamplingRuleRepository:
    return SamplingRuleRepository()
