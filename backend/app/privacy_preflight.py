"""内网内容复核、黑名单脱敏与外发拦截。

原始 OCR 数据只在本机/内网处理。外网审核请求只使用本模块生成的
脱敏副本；原始报告字段在外网审核完成后由本地元数据重新回填。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from backend.app.llm import LLMClient
from backend.app.settings_store import get_intranet_runtime_config


COMPANY_PLACEHOLDER = "[企业名称已脱敏]"
APPLICATION_PLACEHOLDER = "[申请编号已脱敏]"

_APPLICATION_PATTERN = re.compile(
    r"(?i)(?<![A-Z0-9])A\s*\d{4}\s*CCC(?:\s*[-_/]?\s*[A-Z0-9]){5,}(?![A-Z0-9])"
)
_LABELED_APPLICATION_PATTERN = re.compile(
    r"(?:申请编号|申请号)\s*[:：]?\s*([A-Z0-9][A-Z0-9\s/_-]{5,})",
    re.I,
)
_LABELED_REPORT_PATTERN = re.compile(
    r"(?:报告编号|报告号|Report\s*No\.?)\s*[:：]?\s*([A-Z0-9][A-Z0-9\s()/_-]{5,})",
    re.I,
)
_COMPANY_PATTERN = re.compile(
    r"(?<!\[)([\u4e00-\u9fffA-Za-z0-9（）()·]{2,60}"
    r"(?:股份有限公司|有限责任公司|有限公司|集团公司|研究院|研究所|检测中心|认证中心|公司))"
)
_LABELED_COMPANY_PATTERN = re.compile(
    r"(?:委托人|委托单位|申请人|申请企业|生产者|制造商|生产企业|企业名称)"
    r"\s*[:：]?\s*([^\n|<>]{2,100})"
)
_COMPANY_END_PATTERN = re.compile(
    r"^(.{2,80}?(?:股份有限公司|有限责任公司|有限公司|集团公司|研究院|研究所|检测中心|认证中心|公司))"
)

PREFLIGHT_SYSTEM_PROMPT = """你是部署在单位内网的报告内容复核助手。
你不进行标准审核，只检查同一份报告原文内部的文字与逻辑矛盾：
1. 申请编号、报告编号、型号、规格、电压等级、执行标准、样品数量是否前后一致；
2. 收样、试验、完成、签发等明示日期是否存在不可能的先后关系；
3. 原文同时给出计算前后数值与变化率时，复算算术是否一致；
4. 仅在表格同行或关联关系明确时，检查试验文字结果与 P/N 判定是否自相矛盾。

严格边界：
- 不查标准限值、试验适用性、应做/不应做的项目，不根据型号臆测标准要求。
- 输入可能是整份报告的一个样品批次；首页列出多个样品而当前批次只含一个样品，不是数量矛盾。
- 不将 OCR 空白、字段未识别、缺少可比对信息本身当作错误；证据不足时不输出。
- 不检查审核人、签名人、批准人等手写签名一致性。
- 只输出可由原文直接复核的异常线索；没有发现异常时输出空数组。
- 你的结果只是复核线索，不是“必须修改”结论。
- 新申请报告出具后，企业可能变更供应商并在后续报告中引用原报告；“新申请”与供应商变更备注同时出现本身不是矛盾。只有原文明确指向同一业务阶段且两项事实互斥时才提示，不因缺少完整历史而要求人工复核。
- 图形商标可能不进入PDF文字层；文字层空白与OCR识别出图形商标不是两个冲突原值。仅凭这种提取差异不提示报告商标不一致。
- 不把报告编号中的数字串推断为收样、完成或签发日期；没有明确编号编码约定时，不生成“编号日期段”一致性检查。
- 日期先后矛盾只引用原文实际日期并要求核实，不猜测或建议某个“正确年份”。

严格输出JSON，不要输出报告全文，不要添加解释：
{
  "logic_checks": [
    {"item":"检查项", "result":"warning/manual_review", "note":"不超过80字，同时写明两个相互矛盾的原文事实"}
  ]
}
找不到异常时使用空数组。"""

PREFLIGHT_FORMAT_RETRY_PROMPT = """上一次返回的JSON语法不完整。请重新对同一份报告执行内容复核，并严格遵守：
1. 只输出一个可被标准JSON解析器直接解析的对象，不得使用Markdown围栏或解释文字；
2. 顶层只允许logic_checks一个键，值为数组；
3. 每个logic_checks元素只包含item、result、note，result只能是warning或manual_review；
4. 字符串内部不得使用未转义的英文双引号；每个数组元素之间必须有逗号；
5. 不要输出报告全文。
"""


def _is_excluded_personnel_signature_check(item: Any) -> bool:
    """人员签名不进入自动审核。

    手写签名的 OCR 结果不稳定，不能支撑人员身份一致性判定。
    """
    if not isinstance(item, dict):
        return False
    content = f"{item.get('item') or ''} {item.get('note') or ''}"
    personnel = re.search(r"审核人|签名人|批准人|编制人|检验人|人员", content)
    signature = re.search(r"签名|签字|手写|姓名", content)
    consistency = re.search(r"一致|不一致|不符|相符|核对|确认|识别", content)
    return bool(signature and (personnel or consistency))


class PrivacyPreflightError(RuntimeError):
    """内网安全预审无法完成。"""


class PrivacyPreflightBlocked(PrivacyPreflightError):
    """出口复检仍发现敏感信息，禁止外发。"""


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = re.sub(r"\s+", "", str(raw or "")).strip("：:;,，。|<>")
        if len(value) < 3 or value in seen or value in {COMPANY_PLACEHOLDER, APPLICATION_PLACEHOLDER}:
            continue
        seen.add(value)
        result.append(value)
    return result


def _deterministic_sensitive_values(
    text: str,
    metadata: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    metadata = metadata or {}
    companies = [str(metadata.get("company") or "")]
    applications = [str(metadata.get("application_no") or "")]

    applications.extend(match.group(1) for match in _LABELED_APPLICATION_PATTERN.finditer(text))
    applications.extend(match.group(0) for match in _APPLICATION_PATTERN.finditer(text))

    for match in _LABELED_COMPANY_PATTERN.finditer(text):
        candidate = re.sub(r"\s+", "", match.group(1)).strip("：:;,，。|<>")
        end_match = _COMPANY_END_PATTERN.match(candidate)
        if end_match:
            companies.append(end_match.group(1))
    companies.extend(match.group(1) for match in _COMPANY_PATTERN.finditer(text))
    return _unique(companies), _unique(applications)


def _normalized_identifier(value: str) -> str:
    return re.sub(r"[\s/_-]+", "", str(value or "")).upper()


def _explicit_voltage_verdict_pairs(text: str) -> list[tuple[str, str]]:
    """只读同一行明确的结果/评定字段，不扫描HTML属性或标准要求。"""
    import html
    pairs: list[tuple[str, str]] = []
    outcome = r"(?:未发生击穿|没有击穿|未击穿|不击穿|无击穿|发生击穿|已击穿|击穿)"
    def accept(value: str, verdict: str) -> None:
        value = html.unescape(re.sub(r"<[^>]*>", " ", value)).strip()
        verdict = html.unescape(re.sub(r"<[^>]*>", " ", verdict)).strip().upper()
        if verdict not in {"P", "N"} or not re.fullmatch(outcome + r"(?:[\s、，,;/]+" + outcome + r")*", value):
            return
        observations = re.findall(outcome, value)
        if verdict == "P" and any(v in {"击穿", "发生击穿", "已击穿"} for v in observations):
            pairs.append(("击穿", verdict))
        elif verdict == "N" and observations and all(v not in {"击穿", "发生击穿", "已击穿"} for v in observations):
            pairs.append(("未击穿", verdict))
    for table in re.findall(r"<table\b[^>]*>.*?</table>", text, re.I | re.S):
        if not all(label in table for label in ("检验结果", "单项评定")):
            continue
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.I | re.S):
            cells = re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", row, re.I | re.S)
            if len(cells) < 2:
                continue
            # Merged result/verdict cells need independent row binding; never guess.
            if any(int(value) != 1 for attrs, _ in cells[-2:]
                   for value in re.findall(r"(?:rowspan|colspan)\s*=\s*[\"']?(\d+)", attrs, re.I)):
                continue
            accept(cells[-2][1], cells[-1][1])
    plain = re.sub(r"<table\b[^>]*>.*?</table>", "\n", text, flags=re.I | re.S)
    for line in plain.splitlines():
        if "<" in line or ">" in line:
            continue
        match = re.search(r"(?:检验结果|实测结果|试验结果)\s*[:：]\s*(.*?)\s*[;；|]?\s*(?:判定|单项评定)\s*[:：]\s*([PN])(?:\s|$)", line, re.I)
        if match:
            accept(match[1].rstrip(" ;；|"), match[2])
    return list(dict.fromkeys(pairs))


def _deterministic_logic_checks(text: str) -> list[dict[str, str]]:
    """先做不依赖模型的编号和明显P/N一致性检查。"""
    checks: list[dict[str, str]] = []
    application_values = {
        _normalized_identifier(match.group(1))
        for match in _LABELED_APPLICATION_PATTERN.finditer(text)
        if _normalized_identifier(match.group(1))
    }
    report_values = {
        _normalized_identifier(match.group(1))
        for match in _LABELED_REPORT_PATTERN.finditer(text)
        if _normalized_identifier(match.group(1))
    }
    if application_values:
        checks.append({
            "item": "申请编号一致性",
            "result": "pass" if len(application_values) == 1 else "warning",
            "note": "各处识别到的申请编号一致" if len(application_values) == 1 else "识别到多个不同申请编号，需人工核对",
        })
    if report_values:
        checks.append({
            "item": "报告编号一致性",
            "result": "pass" if len(report_values) == 1 else "warning",
            "note": "各处识别到的报告编号一致" if len(report_values) == 1 else "识别到多个不同报告编号，需人工核对",
        })
    voltage_pairs = _explicit_voltage_verdict_pairs(text)
    if ("未击穿", "N") in voltage_pairs:
        checks.append({"item": "电压试验P/N逻辑", "result": "warning", "note": "发现未击穿结果附近判N，需核对表格行列"})
    if ("击穿", "P") in voltage_pairs:
        checks.append({"item": "电压试验P/N逻辑", "result": "warning", "note": "发现击穿结果附近判P，需核对表格行列"})
    return checks


def _fuzzy_literal_pattern(value: str, application: bool = False) -> re.Pattern[str]:
    pieces: list[str] = []
    for char in re.sub(r"\s+", "", value):
        if application and char in "-_/":
            pieces.append(r"[\s/_-]*")
        else:
            pieces.append(re.escape(char) + r"\s*")
    return re.compile("".join(pieces), re.I if application else 0)


def redact_sensitive_text(
    text: str,
    company_names: list[str],
    application_numbers: list[str],
    *,
    redact_companies: bool = True,
    redact_applications: bool = True,
) -> tuple[str, dict[str, int]]:
    """对文本副本执行黑名单替换，返回脱敏文本和替换计数。"""
    redacted = str(text)
    counts = {"company": 0, "application_no": 0}

    if redact_applications:
        for value in sorted(application_numbers, key=len, reverse=True):
            redacted, count = _fuzzy_literal_pattern(value, application=True).subn(
                APPLICATION_PLACEHOLDER, redacted
            )
            counts["application_no"] += count
        redacted, count = _APPLICATION_PATTERN.subn(APPLICATION_PLACEHOLDER, redacted)
        counts["application_no"] += count

    if redact_companies:
        for value in sorted(company_names, key=len, reverse=True):
            redacted, count = _fuzzy_literal_pattern(value).subn(COMPANY_PLACEHOLDER, redacted)
            counts["company"] += count
        redacted, count = _COMPANY_PATTERN.subn(COMPANY_PLACEHOLDER, redacted)
        counts["company"] += count

    return redacted, counts


def scan_outbound_text(
    text: str,
    company_names: list[str],
    application_numbers: list[str],
    *,
    scan_companies: bool = True,
    scan_applications: bool = True,
) -> list[dict[str, str]]:
    """出口复检只返回类型和原因，不在日志中复制命中的敏感原文。"""
    findings: list[dict[str, str]] = []
    if scan_applications:
        if _APPLICATION_PATTERN.search(text):
            findings.append({"type": "application_no", "reason": "仍符合申请编号格式"})
        elif any(_fuzzy_literal_pattern(value, application=True).search(text) for value in application_numbers):
            findings.append({"type": "application_no", "reason": "仍包含已识别申请编号"})
    if scan_companies:
        if _COMPANY_PATTERN.search(text):
            findings.append({"type": "company", "reason": "仍包含企业或机构名称特征"})
        elif any(_fuzzy_literal_pattern(value).search(text) for value in company_names):
            findings.append({"type": "company", "reason": "仍包含已识别企业名称"})
    return findings


def _validate_model_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PrivacyPreflightError("内网预审模型没有返回JSON对象")
    if set(payload) - {"logic_checks"}:
        raise PrivacyPreflightError("内网内容复核结果包含不支持的字段")
    if not isinstance(payload.get('logic_checks'), list):
        raise PrivacyPreflightError("内网内容复核结果缺少 logic_checks 数组")
    logic_checks: list[dict[str, str]] = []
    for item in payload.get("logic_checks") or []:
        if not isinstance(item, dict):
            raise PrivacyPreflightError("内网预审逻辑检查必须为对象")
        if (any(not isinstance(item.get(key), str) for key in ('item','result','note'))
                or item['result'].lower() not in {'warning','manual_review'}):
            raise PrivacyPreflightError("内网内容复核字段或状态无效")
        if _is_excluded_personnel_signature_check(item):
            continue
        result = str(item.get("result") or "manual_review").lower()
        if result not in {"warning", "manual_review"}:
            result = "manual_review"
        logic_checks.append({
            "item": str(item.get("item") or "逻辑一致性")[:80],
            "result": result,
            "note": str(item.get("note") or "")[:300],
        })
    return {
        "company_names": [],
        "application_numbers": [],
        "logic_checks": logic_checks[:20],
    }


def _call_intranet_preflight(text: str, client: LLMClient | None = None) -> dict[str, Any]:
    runtime = get_intranet_runtime_config()
    if runtime.get("mock_enabled"):
        return {"company_names": [], "application_numbers": [], "logic_checks": []}
    if not runtime.get("api_key"):
        raise PrivacyPreflightError("已启用内网安全预审，但尚未配置内网 API Key")
    client = client or LLMClient(
        # 辅助复核不得长时间占用主流程，单批最多等待 90 秒。
        timeout_seconds=min(90.0, float(runtime.get("timeout_seconds", 600))),
        ai_config=runtime,
    )
    user_prompt = "请只对以下 MinerU/OCR 报告内容执行文字与内部逻辑复核：\n\n" + text
    format_errors: list[str] = []
    for format_attempt in range(2):
        try:
            raw = client.chat(
                PREFLIGHT_SYSTEM_PROMPT if format_attempt == 0 else PREFLIGHT_SYSTEM_PROMPT + "\n\n" + PREFLIGHT_FORMAT_RETRY_PROMPT,
                user_prompt,
                model=runtime.get("review_model"),
                # Network errors retain the client's bounded transport policy.
                max_retries=0,
                max_tokens=min(262144, int(runtime.get("max_tokens", 262144))),
                json_mode=True,
            )
        except ValueError as exc:
            # Invalid response envelopes / empty content are generated-output
            # failures too. Never echo the endpoint's text into report errors.
            format_errors.append(type(exc).__name__)
            continue
        try:
            return _validate_model_payload(client.parse_json(raw))
        except Exception as exc:
            # 不对模型内容猜测性补逗号，也不把原始返回写入日志；
            # 仅请内网模型重新生成严格JSON。
            format_errors.append(type(exc).__name__)
    final_error = format_errors[-1] if format_errors else "unknown parse error"
    raise PrivacyPreflightError(
        f"内网内容复核结果格式无效（2次格式生成均失败）：{final_error}"
    )


def _logic_summary(logic_checks: list[dict[str, str]]) -> str:
    actionable = [item for item in logic_checks if item.get("result") != "pass"]
    if not actionable:
        return ""
    lines = ["【单位内网内容复核线索（已脱敏，不得直接作为修改结论）】"]
    for item in actionable:
        lines.append(f"- {item['item']}：{item['result']}；{item['note']}")
    return "\n".join(lines)


def prepare_privacy_preflight(
    text: str,
    metadata: dict[str, Any] | None,
    ai_settings: dict[str, Any],
    *,
    client: LLMClient | None = None,
) -> dict[str, Any]:
    """生成内网预审结果和可供外发的文本副本。"""
    configured_mode = str(ai_settings.get("privacy_preflight_mode") or "disabled")
    # 脱敏与内容复核互相独立：无论内网模型是否可用，都必须先生成
    # 确定性脱敏副本。内网复核失败只记录状态，不阻断外网主审核。
    mode = "enforce"

    companies, applications = _deterministic_sensitive_values(text, metadata)
    model_payload = {"company_names": [], "application_numbers": [], "logic_checks": []}
    if ai_settings.get("intranet_content_review_enabled", False):
        try:
            model_payload = _call_intranet_preflight(text, client=client)
            model_status = "ok"
        except Exception as exc:
            # 复核是非裁决性辅助层。超时、断网或输出格式错误时不应
            # 使主审核失败，也不应向结果中写入模型返回原文。
            model_status = f"error:{type(exc).__name__}"
    else:
        model_status = "disabled"

    # 模型补充项只有确实出现在原文时才进入黑名单，避免模型臆造值导致误删。
    for value in model_payload["company_names"]:
        if _fuzzy_literal_pattern(value).search(text):
            companies.append(value)
    for value in model_payload["application_numbers"]:
        if _fuzzy_literal_pattern(value, application=True).search(text):
            applications.append(value)
    companies, applications = _unique(companies), _unique(applications)

    redact_companies = bool(ai_settings.get("privacy_company_blacklist_enabled", True))
    redact_applications = bool(ai_settings.get("privacy_application_blacklist_enabled", True))
    sanitized_text, replacement_counts = redact_sensitive_text(
        text,
        companies,
        applications,
        redact_companies=redact_companies,
        redact_applications=redact_applications,
    )
    logic_checks = _deterministic_logic_checks(text)
    seen_logic = {(item["item"], item["result"], item["note"]) for item in logic_checks}
    for item in model_payload["logic_checks"]:
        key = (item["item"], item["result"], item["note"])
        if key not in seen_logic:
            logic_checks.append(item)
            seen_logic.add(key)
    summary, _ = redact_sensitive_text(
        _logic_summary(logic_checks),
        companies,
        applications,
        redact_companies=redact_companies,
        redact_applications=redact_applications,
    )
    candidate = sanitized_text + ("\n\n" + summary if summary else "")
    findings = scan_outbound_text(
        candidate,
        companies,
        applications,
        scan_companies=redact_companies,
        scan_applications=redact_applications,
    )
    blocked = bool(findings and ai_settings.get("privacy_outbound_block_enabled", True))
    if blocked:
        types = "、".join(sorted({item["type"] for item in findings}))
        raise PrivacyPreflightBlocked(f"外发安全复检未通过：{types}")

    return {
        "mode": mode,
        "configured_mode": configured_mode,
        "external_text": candidate if mode == "enforce" else text,
        "sanitized_text": candidate,
        "logic_checks": logic_checks,
        "replacement_counts": replacement_counts,
        "outbound_findings": findings,
        "outbound_blocked": blocked,
        "model_status": model_status,
        # 仅在当前批次内存中供后续确定性规则重建摘要后二次脱敏；
        # save_preflight_artifact 与最终 review_meta 均不会持久化这些原值。
        "_privacy_company_names": companies,
        "_privacy_application_numbers": applications,
        "_privacy_redact_companies": redact_companies,
        "_privacy_redact_applications": redact_applications,
        "_privacy_outbound_block_enabled": bool(
            ai_settings.get("privacy_outbound_block_enabled", True)
        ),
    }


def preflight_cache_fingerprint(
    text: str,
    metadata: dict[str, Any] | None,
    ai_settings: dict[str, Any],
    *,
    rules_fingerprint: str = "",
) -> str:
    """Bind deterministic privacy output to source text and safety settings."""
    privacy_keys = (
        "privacy_preflight_mode", "privacy_preflight_logic_enabled",
        "privacy_company_blacklist_enabled", "privacy_application_blacklist_enabled",
        "privacy_outbound_block_enabled",
    )
    material = {
        "version": "privacy-safety-cache-v6-content-review",
        "text": text,
        "metadata": metadata or {},
        "privacy": {key: ai_settings.get(key) for key in privacy_keys},
        "content_review": {
            "enabled": bool(ai_settings.get("intranet_content_review_enabled", False)),
            "base_url": str(ai_settings.get("intranet_base_url") or ""),
            "model": str(ai_settings.get("intranet_review_model") or ""),
            "thinking_enabled": bool(ai_settings.get("intranet_thinking_enabled", False)),
            "api_key_fingerprint": hashlib.sha256(
                str(ai_settings.get("intranet_api_key") or "").encode("utf-8")
            ).hexdigest(),
            "prompt_fingerprint": hashlib.sha256(
                PREFLIGHT_SYSTEM_PROMPT.encode("utf-8")
            ).hexdigest(),
        },
        "rules_fingerprint": rules_fingerprint,
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _preflight_cache_path(ocr_path: str, task_id: int | None, batch_number: int) -> Path | None:
    if task_id is None:
        return None
    return Path(ocr_path) / f"privacy_preflight_task_{task_id}" / f"batch_{batch_number:03d}_stable_cache.json"


def _valid_preflight_cache_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if (result.get('mode') != 'enforce'
        or result.get('model_status') not in {'ok', 'disabled'}):
        return False
    if result.get('outbound_findings') != [] or result.get('outbound_blocked') is not False:
        return False
    if not all(isinstance(result.get(k), str) and result[k].strip() for k in ('external_text','sanitized_text')):
        return False
    checks = result.get('logic_checks')
    counts = result.get('replacement_counts')
    return (isinstance(checks, list) and all(isinstance(c, dict) and all(isinstance(c.get(k),str) for k in ('item','result','note')) for c in checks)
            and isinstance(counts,dict) and all(type(counts.get(k)) is int and counts[k]>=0 for k in ('company','application_no')))


def load_preflight_cache(
    ocr_path: str,
    task_id: int | None,
    batch_number: int,
    fingerprint: str,
) -> dict[str, Any] | None:
    path = _preflight_cache_path(ocr_path, task_id, batch_number)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        result = payload.get("result")
        if payload.get("fingerprint") != fingerprint or not _valid_preflight_cache_result(result):
            return None
        result = dict(result)
        result["cache_reused"] = True
        return result
    except (OSError, ValueError, TypeError):
        return None


def save_preflight_cache(
    ocr_path: str,
    task_id: int | None,
    batch_number: int,
    fingerprint: str,
    result: dict[str, Any],
) -> bool:
    """仅缓存enforce模式的无敏感外发副本；不持久化原始黑名单值。"""
    path = _preflight_cache_path(ocr_path, task_id, batch_number)
    if (
        path is None or not _valid_preflight_cache_result(result)
    ):
        return False
    safe_result = {
        key: result.get(key) for key in (
            "mode", "external_text", "sanitized_text", "logic_checks",
            "replacement_counts", "outbound_findings", "outbound_blocked", "model_status",
        )
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "version": "privacy-preflight-stable-cache-v1",
        "fingerprint": fingerprint,
        "result": safe_result,
    }, ensure_ascii=False)
    # 保证各写者使用独立临时文件，读者只看到完整旧版或新版。
    import tempfile
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return True


def prepare_stable_privacy_preflight(
    text: str,
    metadata: dict[str, Any] | None,
    ai_settings: dict[str, Any],
    *,
    ocr_path: str,
    task_id: int | None,
    batch_number: int,
    rules_fingerprint: str,
    client: LLMClient | None = None,
) -> dict[str, Any]:
    """同任务同批次只生成一次内网预审产物，配置或原文变化自动失效。"""
    fingerprint = preflight_cache_fingerprint(
        text, metadata, ai_settings, rules_fingerprint=rules_fingerprint
    )
    cached = load_preflight_cache(ocr_path, task_id, batch_number, fingerprint)
    if cached is not None:
        return cached
    result = prepare_privacy_preflight(text, metadata, ai_settings, client=client)
    from backend.app.executable_rules import normalize_privacy_logic_checks

    result = normalize_privacy_logic_checks(result, text)
    result["cache_reused"] = False
    save_preflight_cache(ocr_path, task_id, batch_number, fingerprint, result)
    return result


def save_preflight_artifact(
    ocr_path: str,
    task_id: int | None,
    batch_number: int,
    result: dict[str, Any],
) -> None:
    """保存本地试运行证据；文件中不记录原始敏感值。"""
    task_label = str(task_id) if task_id is not None else "manual"
    target_dir = Path(ocr_path) / f"privacy_preflight_task_{task_label}"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"batch_{batch_number:03d}_sanitized.txt").write_text(
        str(result.get("sanitized_text") or ""), encoding="utf-8"
    )
    audit = {
        "mode": result.get("mode"),
        "model_status": result.get("model_status"),
        "replacement_counts": result.get("replacement_counts"),
        "logic_checks": result.get("logic_checks"),
        "outbound_findings": result.get("outbound_findings"),
        "outbound_blocked": bool(result.get("outbound_blocked")),
        "cache_reused": bool(result.get("cache_reused")),
    }
    (target_dir / f"batch_{batch_number:03d}_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
