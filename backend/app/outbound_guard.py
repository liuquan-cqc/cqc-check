"""外网模型请求的独立最终安全网。

本层不信任上游脱敏结果，而是在每次网络请求前重新根据
原始批次和报告元数据识别敏感值，并扫描实际待发送的完整消息。
审计文件不保存请求正文或命中原值。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from backend.app.privacy_preflight import (
    _deterministic_sensitive_values,
    scan_outbound_text,
)


GUARD_VERSION = "final-outbound-guard-v1"


class FinalOutboundBlocked(RuntimeError):
    """最终待发送消息仍包含受保护标识，已在网络请求前阻断。"""


class FinalOutboundGuard:
    """对单个审核批次的所有外网 chat 调用实施失败关闭。"""

    def __init__(
        self,
        source_text: str,
        metadata: dict[str, Any] | None,
        ocr_path: str,
        task_id: int | None,
        batch_number: int,
    ) -> None:
        self.companies, self.applications = _deterministic_sensitive_values(
            source_text, metadata
        )
        task_label = str(task_id) if task_id is not None else "manual"
        self.audit_dir = Path(ocr_path) / f"outbound_guard_task_{task_label}"
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.batch_number = int(batch_number)
        self.records: list[dict[str, Any]] = []

    def inspect(self, system: str, user: str) -> dict[str, Any]:
        """检查即将外发的精确文本；命中时先留审计再抛出阻断。"""
        request_text = f"{system}\n\n{user}"
        findings = scan_outbound_text(
            request_text,
            self.companies,
            self.applications,
            scan_companies=True,
            scan_applications=True,
        )
        call_number = len(self.records) + 1
        record = {
            "guard_version": GUARD_VERSION,
            "batch_number": self.batch_number,
            "call_number": call_number,
            "request_sha256": hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
            "request_chars": len(request_text),
            "known_company_count": len(self.companies),
            "known_application_count": len(self.applications),
            "finding_types": sorted({item["type"] for item in findings}),
            "finding_count": len(findings),
            "decision": "blocked" if findings else "allowed",
        }
        self.records.append(record)
        target = self.audit_dir / (
            f"batch_{self.batch_number:03d}_call_{call_number:02d}.json"
        )
        target.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if findings:
            types = "、".join(record["finding_types"])
            raise FinalOutboundBlocked(f"最终外发安全网已阻断请求：{types}")
        return record

    def summary(self) -> dict[str, Any]:
        return {
            "guard_version": GUARD_VERSION,
            "calls": len(self.records),
            "allowed": sum(item["decision"] == "allowed" for item in self.records),
            "blocked": sum(item["decision"] == "blocked" for item in self.records),
            "finding_types": sorted({
                finding
                for item in self.records
                for finding in item.get("finding_types") or []
            }),
        }


class GuardedLLMClient:
    """保持 LLMClient 接口，并确保每次 chat（包括格式修复）均经过安全网。"""

    def __init__(self, client: Any, guard: FinalOutboundGuard) -> None:
        self.client = client
        self.guard = guard

    def chat(self, system: str, user: str, **kwargs: Any) -> str:
        self.guard.inspect(system, user)
        return self.client.chat(system, user, **kwargs)

    def parse_json(self, text: str) -> dict[str, Any]:
        return self.client.parse_json(text)

