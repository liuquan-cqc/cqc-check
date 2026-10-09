"""脱敏后的第一阶段外网内容复核。

本层只查报告内部一致性，不查标准限值、适用性或应做项目。
复核线索仅供第二阶段主审核核实，不直接改变最终结论。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any


SYSTEM_PROMPT = """你是检测报告的“报告级通用逻辑”复核助手。输入已完成脱敏。
你不是试验内容审核员，不得审核任何检测项目、实测值或合格性。

只允许输出以下五类线索，scope 必须取对应英文值：
1. report_identifier：同一个报告编号、申请编号等报告身份字段在不同页出现无法由组成报告/子报告规则解释的冲突；
2. date_logic：报告明示日期存在不可能的先后顺序，例如收样早于抽样、完成早于检测开始、签发早于完成；
3. equipment_list：检测设备清单中同一设备编号对应相互冲突的信息，或报告明确的检测日期落在该设备明示的检定/校准有效期之外；
4. document_structure：页码与总页数自相矛盾、明确引用的组成页或附件实际缺失等报告结构问题。
5. sample_identity：同一个样品的型号、规格、电压或样品编号在不同页出现无法由“大类名称/具体型号”等层级关系解释的明确冲突。

必须遵守：
- 报告正文编号带组成部分后缀（例如 -C2-S），可以作为 manual_review 提醒人工确认组成关系，但不得直接称为编号错误；
- 完成日期与签发日期为同一天，可以作为 manual_review 提醒人工确认流程，但不得直接称为日期错误；
- 设备名称相同但设备编号不同，可以作为 manual_review 提醒人工确认设备身份，但不得直接称为设备错误；
- 不得自行假定设备检定周期应为一年或两年，只能比较报告明示的检测日期和有效期；
- 封面使用产品大类、正文使用具体型号或规格，不等于身份矛盾；
- 上述三类允许作为非裁决性 manual_review 输出；除此之外，只有明确矛盾或明确缺失才能输出。

严禁检查或提及：
- 任何试验项目行、试验条件、标准要求、实测值、变化率、修约、单位；
- P/N/F 或单项评定是否正确；
- 标准限值、试验适用性、应做/不应做项目；
- OCR 表格错位、空白字段、型号电压规格是否写全；
- 最终合格性、“必须修改”或其他裁决性结论。

仅输出严格 JSON：
{"logic_checks":[{"scope":"report_identifier或date_logic或equipment_list或document_structure或sample_identity","item":"检查项","result":"warning或manual_review","note":"明确冲突的原文证据"}]}
无线索时输出 {"logic_checks":[]} 。不得输出 pass 填充项，最多输出 8 条。
""".strip()


ALLOWED_SCOPES = {
    "report_identifier", "date_logic", "equipment_list", "document_structure",
    "sample_identity",
}

# 即使模型没有服从提示词，也不允许试验内容线索进入第二阶段或前端。
FORBIDDEN_CONTENT_TERMS = (
    "抗张", "断裂伸长", "变化率", "修约", "最薄处", "椭圆度", "耐擦",
    "失重", "热冲击", "低温弯曲", "低温拉伸", "高温压力", "热稳定",
    "标准要求", "实测值", "检验结果", "单项评定", "判p", "判n", "判f",
    "p判定", "n判定", "f判定",
)


def _validate(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or set(payload) != {"logic_checks"}:
        raise ValueError("内容复核必须只返回 logic_checks")
    raw_checks = payload.get("logic_checks")
    if not isinstance(raw_checks, list):
        raise ValueError("logic_checks 必须为数组")
    checks: list[dict[str, str]] = []
    for raw in raw_checks:
        if not isinstance(raw, dict) or set(raw) != {"scope", "item", "result", "note"}:
            raise ValueError("内容复核字段不完整")
        if any(not isinstance(raw.get(key), str) for key in ("scope", "item", "result", "note")):
            raise ValueError("内容复核字段类型错误")
        scope = raw["scope"].strip().lower()
        if scope not in ALLOWED_SCOPES:
            raise ValueError("内容复核超出报告级通用逻辑范围")
        result = raw["result"].strip().lower()
        if result not in {"warning", "manual_review"}:
            raise ValueError("内容复核不允许直接产生通过或修改结论")
        evidence = f"{raw['item']} {raw['note']}".lower()
        if any(term in evidence for term in FORBIDDEN_CONTENT_TERMS):
            # 单条越界线索静默丢弃，避免模型把试验内容带入后续主审核。
            continue
        checks.append({
            "scope": scope,
            "item": raw["item"].strip()[:80] or "逻辑一致性",
            "result": result,
            "note": raw["note"].strip()[:300],
        })
    return checks[:8]


def _fingerprint(text: str, ai_settings: dict[str, Any]) -> str:
    material = {
        "version": "external-content-review-v2-report-level-only",
        "text": text,
        "provider": ai_settings.get("provider"),
        "base_url": ai_settings.get("base_url"),
        "model": ai_settings.get("review_model"),
        "thinking": ai_settings.get("thinking_enabled"),
        "reasoning_effort": ai_settings.get("reasoning_effort"),
        "temperature": ai_settings.get("temperature"),
        "api_key_hash": hashlib.sha256(
            str(ai_settings.get("api_key") or "").encode("utf-8")
        ).hexdigest(),
        "prompt_hash": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _cache_path(ocr_path: str, task_id: int | None, batch_number: int) -> Path | None:
    if task_id is None:
        return None
    return Path(ocr_path) / f"external_content_review_task_{task_id}" / f"batch_{batch_number:03d}.json"


def _load(path: Path | None, fingerprint: str) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = payload.get("result")
        if payload.get("fingerprint") != fingerprint or not isinstance(result, dict):
            return None
        if result.get("status") != "ok" or not isinstance(result.get("logic_checks"), list):
            return None
        return {**result, "cache_reused": True}
    except (OSError, ValueError, TypeError):
        return None


def _save(path: Path | None, fingerprint: str, result: dict[str, Any]) -> None:
    if path is None or result.get("status") != "ok":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "version": "external-content-review-cache-v2-report-level-only",
        "fingerprint": fingerprint,
        "result": {
            "status": "ok",
            "provider": result.get("provider"),
            "model": result.get("model"),
            "logic_checks": result.get("logic_checks") or [],
        },
    }, ensure_ascii=False)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def prepare_external_content_review(
    text: str,
    ai_settings: dict[str, Any],
    client: Any,
    *,
    ocr_path: str,
    task_id: int | None,
    batch_number: int,
) -> dict[str, Any]:
    """使用当前外网主审核供应商做第一阶段复核。

    text 必须是上游已脱敏且通过残留检查的副本；client 必须是
    GuardedLLMClient，使每次请求仍经过最终外发安全网。
    """
    if not ai_settings.get("external_content_review_enabled", True):
        return {
            "status": "disabled", "provider": ai_settings.get("provider"),
            "model": ai_settings.get("review_model"), "logic_checks": [],
            "cache_reused": False,
        }
    if ai_settings.get("mock_enabled"):
        return {
            "status": "ok", "provider": ai_settings.get("provider"),
            "model": ai_settings.get("review_model"), "logic_checks": [],
            "cache_reused": False,
        }
    fingerprint = _fingerprint(text, ai_settings)
    path = _cache_path(ocr_path, task_id, batch_number)
    cached = _load(path, fingerprint)
    if cached is not None:
        return cached
    last_error: Exception | None = None
    for format_attempt in range(2):
        try:
            system_prompt = SYSTEM_PROMPT
            if format_attempt:
                system_prompt += "\n\n上一次格式无法校验。请重新生成，仅输出符合约定的严格 JSON 对象。"
            raw = client.chat(
                system_prompt,
                "请复核以下已脱敏的当前样品报告内容：\n\n" + text,
                model=ai_settings.get("review_model"),
                max_retries=2,
                max_tokens=min(8192, max(1024, int(ai_settings.get("max_tokens", 8192)))),
                json_mode=True,
            )
            result = {
                "status": "ok",
                "provider": ai_settings.get("provider"),
                "model": ai_settings.get("review_model"),
                "logic_checks": _validate(client.parse_json(raw)),
                "cache_reused": False,
            }
            _save(path, fingerprint, result)
            return result
        except Exception as exc:
            last_error = exc
    # 第一阶段是非裁决性线索层，失败不得阻断第二阶段主审核。
    return {
        "status": f"error:{type(last_error).__name__}",
        "provider": ai_settings.get("provider"),
        "model": ai_settings.get("review_model"),
        "logic_checks": [],
        "cache_reused": False,
    }


def prompt_context(result: dict[str, Any]) -> str:
    checks = result.get("logic_checks") or []
    if not checks:
        return ""
    lines = [
        "【第一阶段外网内容复核线索（不是最终结论）】",
        "必须回到报告原文和结构化规则核实；证据不足时不得生成修改项。",
    ]
    for item in checks:
        lines.append(f"- {item['item']}：{item['result']}；{item['note']}")
    return "\n".join(lines)


def merge_checks(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for result in results:
        for item in result.get("logic_checks") or []:
            key = (str(item.get("item")), str(item.get("result")), str(item.get("note")))
            if key not in seen:
                seen.add(key)
                merged.append({
                    "scope": str(item.get("scope") or ""),
                    "item": key[0], "result": key[1], "note": key[2],
                })
    return merged
