"""审核流程：组装提示词、调用LLM、解析结果、生成意见书。"""
from __future__ import annotations

import json
import hashlib
import math
import re
import threading
from html import unescape
from copy import deepcopy
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, List, Callable, Optional
from backend.app.config import get_settings
from backend.app import prompts, knowledge
from backend.app.llm import LLMClient
from backend.app.settings_store import get_section


# Worker 可以同时处理多份报告，每份报告内部又会并发样品。
# 统一限制同一 Worker 进程最多两个审核请求，避免对上游模型
# 瞬时发出四个以上长请求而集中超时。
_REVIEW_AI_SEMAPHORE = threading.BoundedSemaphore(2)


# 模型偶尔会把“核对后确认正确”误标成 suggestion。这里校验的是“是否需要行动”
# 这一通用语义，而不是某个特定检测项目或“判N正确”这一句固定文案。
_NO_ACTION_PATTERNS = (
    re.compile(r"(?:无需|无须|不需|不必)(?:修改|更正|调整|处理|复核|采取行动)"),
    re.compile(r"(?:判(?:定|断|[A-Z])?|结论|报告(?:值|填写|判定)?|当前(?:值|判定|处理)?|该项|此项).{0,8}(?:正确|无误|(?<!不)合格|符合(?:标准|要求|规定))"),
    re.compile(r"(?:保持(?:原值|现状|不变)|不构成(?:问题|错误)|可接受)"),
    re.compile(r"(?:规则库|适用条件).{0,12}(?:未完全解析|尚未解析|不明确)"),
    re.compile(r"先核对适用条件"),
)


def _declares_no_action(item: dict[str, Any]) -> bool:
    """判断问题项是否明确表示当前报告不需要任何修改或复核。"""
    if item.get("action_required") is False:
        return True
    text = " ".join(str(item.get(field, "")) for field in ("should_be", "review_action"))
    return any(pattern.search(text) for pattern in _NO_ACTION_PATTERNS)


_CHECK_VERDICT = {
    "pass": "✅ 符合",
    "fail": "❌ 不符合",
    "not_applicable": "✅ N正确",
    "manual_review": "⚠️ 待复核",
    "notice": "ℹ️ 非阻断提示（条件未独立核实）",
}


def _markdown_cell(value: Any) -> str:
    text = str(value or "-")
    # 审核数值中的半角 ~ 表示范围，但 Markdown 渲染器会将多个单 ~ 两两配对为删除线。
    # 只转换数字范围分隔符，保留真正的 ~~Markdown删除线~~ 语法。
    text = re.sub(r"([0-9%℃°])~(?=[+\-−]?\d)", r"\1～", text)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _build_detailed_audit(result: dict[str, Any]) -> str:
    """根据结构化核对项生成可追溯的逐样品审核明细。"""
    lines = [
        "## 📋 报告逐项审核记录",
        "",
        f"- **申请编号：** {_markdown_cell(result.get('application_no'))}",
        f"- **报告编号：** {_markdown_cell(result.get('report_no'))}",
        f"- **企业名称：** {_markdown_cell(result.get('company'))}",
        f"- **产品单元：** {_markdown_cell(result.get('product_unit'))}",
        f"- **审核结论：** {_markdown_cell(result.get('conclusion'))}",
        "",
        "> 下表仅记录报告中已提取并实际核对的项目；原文缺失或识别不清的内容标为“待复核”，不会补猜数值。",
        "> “非阻断提示”仅用于已批准的原始记录未单列情形，不表示相关条件已独立核实或通过。",
        "",
    ]
    if result.get("report_checks"):
        lines.extend(["### 全报告一致性检查", "", "| 核对项目 | 报告具体值 | 要求 | 结论 | 原页 |",
                      "|---|---|---|---|---|"])
        for check in result["report_checks"]:
            lines.append("| " + " | ".join(_markdown_cell(value) for value in (
                check.get("item"), check.get("reported"), check.get("required"),
                _CHECK_VERDICT.get(check.get("verdict"), "⚠️ 待复核"),
                "、".join(str(p) for p in check.get("source_pages") or []))) + " |")
        lines.append("")
    for index, sample in enumerate(result.get("samples") or [], start=1):
        label = " ".join(str(sample.get(key, "")).strip() for key in ("model", "voltage", "spec") if sample.get(key))
        lines.extend([
            f"### 样品{index}：{label or '未识别样品'}",
            "",
        ])
        checks = sample.get("checks") or []
        if checks:
            lines.extend([
                "| 类别 | 核对项目 | 报告具体值 | 标准要求 | 核对结论 | 标准依据/说明 |",
                "|---|---|---|---|---|---|",
            ])
            for check in checks:
                verdict = _CHECK_VERDICT.get(str(check.get("verdict", "")), "⚠️ 待复核")
                lines.append(
                    "| " + " | ".join(_markdown_cell(value) for value in (
                        check.get("category"), check.get("item"), check.get("reported"),
                        check.get("required"), verdict, check.get("basis") or check.get("note"),
                    )) + " |"
                )
        else:
            lines.append("本次结果未返回结构化逐项核对记录，请结合报告原文人工复核。")
        for check in checks:
            calculation = check.get('pressure_calculation') or {}
            if calculation.get('steps'):
                lines.extend(['', '#### 高温压力计算过程', ''])
                lines.extend('- ' + _markdown_cell(step) for step in calculation['steps'])
        lines.append("")

    problem_items = [
        item for sample in result.get("samples") or [] for item in sample.get("items") or []
        if item.get("severity") in ("must_fix", "suggestion") and item.get("action_required") is not False
    ]
    problem_items.extend(item for item in result.get("report_items") or []
                         if item.get("severity") in ("must_fix", "suggestion")
                         and item.get("action_required") is not False)
    lines.extend(["## 审核结论汇总", ""])
    if not problem_items:
        lines.append("✅ 本次已核对项目未发现需要修改或人工复核的问题。")
    else:
        for number, item in enumerate(problem_items, start=1):
            level = "必须修改" if item.get("severity") == "must_fix" else "人工复核"
            action = item.get("review_action") or item.get("should_be") or "请结合原文处理"
            lines.append(f"{number}. **{level}｜{_markdown_cell(item.get('item'))}：** {_markdown_cell(action)}")
    return "\n".join(lines)


ProgressCallback = Optional[Callable[[str, int, int, str], None]]
MetadataCallback = Optional[Callable[[dict[str, str]], None]]


def _validate_review_payload(payload: Any) -> dict[str, Any]:
    """防止上游的错误对象或空结果被合并器默认成“合格”。"""
    if not isinstance(payload, dict):
        raise ValueError("AI审核结果不是JSON对象")
    if payload.get("error"):
        raise ValueError(f"AI审核返回错误：{payload['error']}")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("AI审核结果缺少非空samples，未生成可用的逐样品结果")
    if any(not isinstance(sample, dict) for sample in samples):
        raise ValueError("AI审核结果samples格式错误")
    return payload


def _review_output_budget(ai_settings: dict[str, Any]) -> int:
    """V4 推理模型需预留更多输出额度；不改变硅基流动原有上限。"""
    configured = int(ai_settings.get("max_tokens", 8192))
    provider = str(ai_settings.get("provider", ""))
    model = str(ai_settings.get("review_model", "")).lower()
    if provider == "deepseek" and "v4" in model:
        return min(16384, max(8192, configured))
    if provider == "intranet":
        return min(65536, max(8192, configured))
    return min(4096, configured)


def _parse_review_json(
    client: LLMClient,
    raw: str,
    model: str | None,
    max_tokens: int,
) -> dict[str, Any]:
    """解析审核JSON；仅在结构损坏时请模型做一次纯格式修复。

    修复提示禁止增删或更改业务值，修复后仍不能解析则直接失败，
    不会将残缺内容作为审核结果保存。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("AI审核返回空正文，可能是推理耗尽了输出Token")
    try:
        return _validate_review_payload(client.parse_json(raw))
    except json.JSONDecodeError as original_error:
        repaired = client.chat(
            """你是JSON结构修复器。只能修正逗号、引号、括号、换行和代码围栏等JSON语法。
严禁新增、删除、改写、概括或推测任何业务字段和数值。只输出修复后的JSON对象。""",
            f"请仅修复以下JSON的语法结构：\n\n{raw}",
            model=model,
            max_retries=0,
            max_tokens=max_tokens,
            json_mode=True,
        )
        try:
            return _validate_review_payload(client.parse_json(repaired))
        except json.JSONDecodeError as repair_error:
            raise ValueError(
                f"审核结果JSON结构损坏，自动修复后仍无法解析："
                f"原始错误 line {original_error.lineno} column {original_error.colno}；"
                f"修复错误 line {repair_error.lineno} column {repair_error.colno}"
            ) from repair_error


def _vision_cache_path(image_path: str) -> Path:
    image = Path(image_path)
    return image.with_suffix(".vision.md")


def _recognize_vision_page(client: LLMClient, image_path: str) -> str:
    cache_path = _vision_cache_path(image_path)
    try:
        cached = cache_path.read_text(encoding="utf-8")
        if cached.strip():
            return cached
    except OSError:
        pass

    # 此client是主审核配置，图片还没有经过内网脱敏；文本安全网无法
    # 识别图片中的公司/编号。不能把OCR缺失变成直接外发原图的旁路。
    # 已有OCR文本仍可在后续内网预审与最终安全网中处理。
    raise RuntimeError(
        '为避免原始报告图片绕过脱敏外发，已阻止未缓存图片页调用主审核视觉模型。'
        '请先通过已配置的MinerU或经验证的本地/内网OCR生成该页文本，再重试审核。'
    )


def _vision_ocr_pages(
    text: str,
    client: LLMClient,
    progress: ProgressCallback = None,
    max_workers: int = 3,
) -> str:
    """并行识别 [IMAGE:path] 页面，每页成功结果单独缓存。"""
    lines = text.splitlines()
    image_lines: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = re.search(r"\[IMAGE:(.+?)\]", line)
        if match:
            image_lines.append((index, match.group(1)))

    total = len(image_lines)
    if not total:
        if progress:
            progress("vision_ocr", 0, 0, "无需调用视觉模型")
        return text

    results: dict[int, str] = {}
    cached_count = 0
    pending: list[tuple[int, str]] = []
    for index, image_path in image_lines:
        cache_path = _vision_cache_path(image_path)
        try:
            cached = cache_path.read_text(encoding="utf-8")
        except OSError:
            cached = ""
        if cached.strip():
            results[index] = cached
            cached_count += 1
        else:
            pending.append((index, image_path))

    completed = cached_count
    if progress:
        message = f"已复用 {cached_count} 页识别结果" if cached_count else "正在识别复杂表格"
        progress("vision_ocr", completed, total, message)

    if pending:
        failed_pages = 0
        workers = max(1, min(int(max_workers), len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_recognize_vision_page, client, image_path): (index, image_path)
                for index, image_path in pending
            }
            for future in as_completed(futures):
                index, _ = futures[future]
                try:
                    results[index] = future.result()
                except Exception:
                    failed_pages += 1
                completed += 1
                if progress:
                    progress("vision_ocr", completed, total, f"视觉识别 {completed}/{total} 页")
        if failed_pages:
            raise RuntimeError(
                f'{failed_pages}页缺少可用OCR文本，已停止审核；'
                '原始报告图片不会发送给主审核模型。请完成MinerU或本地/内网OCR后重试。'
            )

    for index, result in results.items():
        lines[index] = result
    return "\n".join(lines)


def _split_report_text(text: str, max_chars: int = 32000) -> list[str]:
    """优先按页切分报告，避免在表格中间截断，同时控制单次模型输入。"""
    if len(text) <= max_chars:
        return [text]

    page_parts = re.split(r"(?=\n--- Page \d+)", text)
    page_parts = [part for part in page_parts if part.strip()]
    chunks: list[str] = []
    current = ""
    for part in page_parts:
        if current and len(current) + len(part) > max_chars:
            chunks.append(current)
            current = ""
        if len(part) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(part[i:i + max_chars] for i in range(0, len(part), max_chars))
        else:
            current += part
    if current:
        chunks.append(current)
    return chunks or [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _sample_label(sample_text: str, fallback_number: int) -> str:
    """从样品首页提取便于展示的型号、电压和规格。"""
    normalized = sample_text.replace("（", "(").replace("）", ")")
    full = re.search(
        r"((?:60245\s+IEC\s+\d+\s*\([A-Z]+\)|[A-Z]{1,8}(?:-[A-Z]{1,8})?)\s+"
        r"\d{3}/\d{3}V?\s+\d+(?:\+\d+)?\s*[xX×]\s*\d+(?:\.\d+)?(?:\s*mm(?:2|²)?)?)",
        normalized,
        re.I,
    )
    if full:
        return re.sub(r"\s+", " ", full.group(1)).strip()
    single_core = re.search(
        r"((?:60245\s+IEC\s+\d+\s*\([A-Z]+\)|[A-Z]{1,8}(?:-[A-Z]{1,8})?)\s+"
        r"\d{3}/\d{3}V?\s+)(\d+(?:\.\d+)?)\s*mm(?:2|²)\b",
        normalized,
        re.I,
    )
    if single_core:
        # 个别单芯报告页眉写“150mm²”，其结构项目另写“1×150”。
        # 在带明确mm²单位的页眉中规范化为1×，供原页证据绑定使用。
        return re.sub(r"\s+", " ", single_core.group(1)).strip() + f" 1×{single_core.group(2)}mm²"
    model = re.search(r"60245\s+IEC\s+\d+\s*\([A-Z]+\)|\b(?:YCW|YZW|YZ|YC|RVVP|RVV|BVR|BVV|BV)\b", normalized, re.I)
    return re.sub(r"\s+", " ", model.group(0)).strip() if model else f"样品{fallback_number}"


def _split_report_by_samples(
    text: str,
    max_samples_per_batch: int = 1,
    max_batch_chars: int = 28000,
) -> list[dict[str, Any]]:
    """
    按“样品名称”所在页切出完整样品块，默认每个样品单独审核。

    单样品批次能限制详细审核 JSON 的生成量，避免复杂报告在两个样品
    合并后触发上游 500 秒读取超时；调用方仍可显式调高批次数量。

    如果报告没有可靠的样品页边界，回退到旧的按页/字符切分，
    但仍不在单个页块中间截断。
    """
    from backend.app.ocr_identity import normalize_wrapped_inspections
    text, _ = normalize_wrapped_inspections(text)
    # MinerU 补充段也会重复“样品名称/试样型号”，不能把它误识别成额外样品。
    # 先仅在PDF文字层中识别样品边界，再以完整报告编号绑定补充表格。
    # 顺序和数量相同均不是样品身份证据；无法唯一绑定时不得静默漏掉表格。
    number_pattern = (
        r'报告编号\s*[:：]\s*([A-Za-z0-9]+(?:[./_-][ \t\r\n]*[A-Za-z0-9]+)*)'
    )
    def number_value(value: str) -> str:
        # 仅规范编号内部连接符后的排版空白；不纠正字符、不接受前缀相同。
        return re.sub(r'\s+', '', value)
    def report_numbers(value: str) -> set[str]:
        return {number_value(number) for number in re.findall(number_pattern, value)}
    mineru_match = re.search(r"^--- MinerU document parse[^\n]*---\s*$", text, flags=re.M)
    primary_text = text[:mineru_match.start()] if mineru_match else text
    mineru_text = text[mineru_match.end():] if mineru_match else ""
    # 固定的系统生成提示不是报告正文；只移除这一条精确提示，不吞掉
    # 任意前置文字或真实表格。完整原输入仍用于最终审核与安全检查。
    mineru_text = re.sub(
        r'^\s*\[表格审核时以本段的行列对应关系为准；前面的PDF文字层仅用于基础信息交叉核对\]\s*',
        '', mineru_text, count=1,
    )
    # MinerU全扫描报告没有独立PDF文字层，页码升级后会把完整的
    # `Page N`结构放在MinerU段。这时MinerU段就是主来源，不能将其
    # 当成“补充段”后回退为按字符数切分，否则会把3个样品切成2段。
    if (
        not re.search(r"^--- Page \d+[^\n]*---\s*$", primary_text, re.M)
        and re.search(r"^--- Page \d+[^\n]*---\s*$", mineru_text, re.M)
    ):
        primary_text = mineru_text
        mineru_text = ""
    pages = [part for part in re.split(r"(?=^--- Page \d+[^\n]*---\s*$)", primary_text, flags=re.M) if part.strip()]
    if re.search(r'^--- Page \d+[^\n]*---\s*$', mineru_text, re.M):
        # 页码来自同一报告的content_list；在进入样品切分前逐页合并，
        # 避免报告页眉缺失或出现在页尾时把整个补充段错挂到另一试样。
        from backend.app.rulebase import _source_inspection_numbers
        page_header = r'^--- Page (\d+)[^\n]*---[^\S\r\n]*\r?\n'
        supplements = {}
        for part in re.split(r'(?=^--- Page \d+)', mineru_text, flags=re.M):
            if not part.strip():
                continue
            header = re.match(page_header, part)
            if not header or int(header[1]) in supplements or not part[header.end():].strip():
                raise RuntimeError('补充表格页码缺失、重复或内容为空，请核对OCR结果后重试。')
            supplements[int(header[1])] = part[header.end():]
        primary_page_numbers = {
            int(match[1]) for page in pages
            if (match := re.match(page_header, page))
        }
        if set(supplements) != primary_page_numbers:
            raise RuntimeError('补充表格与原文页码不完整对应，请核对OCR结果后重试。')
        matched_pages = set()
        for index, page in enumerate(pages):
            header = re.match(page_header, page)
            if not header:
                continue
            page_number = int(header[1])
            supplement = supplements[page_number]
            own = _source_inspection_numbers({'text': page})
            other = _source_inspection_numbers({'text': supplement})
            if len(own) > 1 or len(other) > 1 or (own and other and own != other):
                raise RuntimeError('补充表格与同页文字层检验编号冲突，请核对OCR结果后重试。')
            own_report = report_numbers(page)
            other_report = report_numbers(supplement)
            if own_report and other_report and own_report != other_report:
                raise RuntimeError('补充表格与同页文字层报告编号冲突，请核对OCR结果后重试。')
            if page_number not in matched_pages:
                pages[index] += '\n【同一原始页MinerU表格补充】\n' + supplement
                matched_pages.add(page_number)
        mineru_text = ''
    from backend.app.rulebase import source_sample_start_indices
    starts = source_sample_start_indices([
        {"page": index + 1, "text": page} for index, page in enumerate(pages)
    ])
    if len(starts) >= 3:
        # 总首页也写“样品名称”，但其报告组成清单列出多个子报告；
        # 必须有清单与后续子报告号相互印证且没有试验表，才归为公共前言。
        overview = ''.join(pages[starts[0]:starts[1]])
        compact_overview = re.sub(r'\s+', '', re.sub(r'<[^>]+>', ' ', overview))
        child_numbers = set()
        for start in starts[1:]:
            match = re.search(number_pattern, pages[start])
            if match:
                child_numbers.add(number_value(match[1]))
        if (
            '报告组成' in compact_overview
            and len(child_numbers) >= 2
            and child_numbers <= set(re.findall(r'[A-Za-z0-9][A-Za-z0-9./_-]*', overview))
            and not re.search(r'检验编号|试样型号|检测项目|检验结果|单项评定', overview)
        ):
            starts = starts[1:]
    if not starts:
        chunks = _split_report_text(text, max_chars=max_batch_chars)
        return [
            {
                "text": chunk,
                "labels": [f"报告分段{index}"],
                "sample_start": index,
                "sample_end": index,
                "sample_count": 1,
                "total_samples": len(chunks),
                "sample_aware": False,
            }
            for index, chunk in enumerate(chunks, start=1)
        ]

    preamble = "".join(pages[:starts[0]])
    sample_blocks: list[dict[str, str]] = []
    for sample_index, page_index in enumerate(starts):
        end_page = starts[sample_index + 1] if sample_index + 1 < len(starts) else len(pages)
        block = "".join(pages[page_index:end_page])
        sample_blocks.append({
            "label": _sample_label(block, sample_index + 1),
            "text": block,
        })

    if mineru_text:
        report_markers = list(re.finditer(
            number_pattern, mineru_text,
        ))
        # 首个编号前的内容可能含其他样品表格，不可隐式归入首个样品。
        prefix = mineru_text[:report_markers[0].start()] if report_markers else mineru_text
        if prefix.strip():
            raise RuntimeError('补充表格归属无法确认：存在无编号前置内容，请核对OCR样品边界后重试。')
        group_starts = [report_markers[0].start()] if report_markers else []
        last_number = number_value(report_markers[0].group(1)) if report_markers else ""
        for marker in report_markers[1:]:
            number = number_value(marker.group(1))
            if number != last_number:
                group_starts.append(marker.start())
                last_number = number
        mineru_groups = [
            mineru_text[start:(group_starts[index + 1] if index + 1 < len(group_starts) else len(mineru_text))]
            for index, start in enumerate(group_starts)
        ] if report_markers else []
        primary_numbers = [report_numbers(sample['text']) for sample in sample_blocks]
        bindings = []
        for supplement in mineru_groups:
            numbers = report_numbers(supplement)
            matches = [i for i, own in enumerate(primary_numbers) if len(numbers) == 1 and own == numbers]
            if len(matches) != 1:
                raise RuntimeError('补充表格归属无法确认：报告编号缺失、冲突或对应多个样品，请核对OCR样品边界后重试。')
            bindings.append((matches[0], supplement))
        for index, supplement in bindings:
            sample_blocks[index]['text'] += (
                '\n\n【MinerU VLM表格补充；与本样品PDF文字层交叉核对】\n' + supplement
            )

    batches: list[dict[str, Any]] = []
    current: list[dict[str, str]] = []
    for sample in sample_blocks:
        candidate_chars = len(preamble) + sum(len(item["text"]) for item in current) + len(sample["text"])
        if current and (
            len(current) >= max(1, max_samples_per_batch)
            or candidate_chars > max_batch_chars
        ):
            batches.append({"samples": current})
            current = []
        current.append(sample)
    if current:
        batches.append({"samples": current})

    total_samples = len(sample_blocks)
    completed = 0
    result: list[dict[str, Any]] = []
    for batch in batches:
        samples = batch["samples"]
        count = len(samples)
        result.append({
            "text": preamble + "\n\n【本批次完整样品页面】\n" + "".join(item["text"] for item in samples),
            "labels": [item["label"] for item in samples],
            "sample_start": completed + 1,
            "sample_end": completed + count,
            "sample_count": count,
            "total_samples": total_samples,
            "sample_aware": True,
        })
        completed += count
    return result


def _batch_cache_path(ocr_path: str, task_id: int | None, batch_number: int) -> Path | None:
    if task_id is None:
        return None
    return Path(ocr_path) / f"review_task_{task_id}" / f"batch_{batch_number:03d}.json"


def _load_batch_cache(path: Path | None, fingerprint: str) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        # task_id 只能防止跨任务串缓存；同一任务重试时，如果提示词、
        # 模型、隐私配置或可执行规则已变，旧结果也必须失效。
        if payload.get("fingerprint") == fingerprint and isinstance(payload.get("result"), dict):
            return _validate_review_payload(payload["result"])
    except (OSError, ValueError, TypeError):
        pass
    return None


def _save_batch_cache(path: Path | None, fingerprint: str, result: dict[str, Any]) -> None:
    if path is None:
        return
    _validate_review_payload(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "fingerprint": fingerprint,
        "result": result,
    }, ensure_ascii=False)
    # 同目录唯一临时文件：并发写者不得共享 .tmp；完整关闭后原子替换。
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


_WHOLE_SAMPLE_MISSING_SCOPE = re.compile(
    r"(?:完整|全部|整份|全套).{0,6}(?:试验数据|试验报告|报告数据)|"
    r"^(?:样品)?(?:试验数据|试验报告|报告数据|数据完整性)(?:缺失)?$"
)
_MISSING_EVIDENCE = re.compile(
    r"(?:未提供|未取得|未获取|未提取|未找到|没有提供|缺失|不完整|"
    r"未在.{0,12}(?:页面|报告|文本)(?:中)?(?:出现|提供|找到))"
)


def _is_whole_sample_missing_entry(entry: dict[str, Any]) -> bool:
    """识别“整份样品数据缺失”，不把某个具体试验项目缺值算在内。"""
    item = str(entry.get("item", "")).strip()
    text = " ".join(str(entry.get(field, "")) for field in (
        "item", "reported", "required", "should_be", "review_action", "basis", "note",
    ))
    return bool(_MISSING_EVIDENCE.search(text) and _WHOLE_SAMPLE_MISSING_SCOPE.search(item + " " + text))


def _has_substantive_sample_checks(sample: dict[str, Any]) -> bool:
    """至少两项带报告值的具体核对记录，才足以推翻“整份数据缺失”。"""
    substantive = 0
    for check in sample.get("checks") or []:
        if _is_whole_sample_missing_entry(check):
            continue
        verdict = str(check.get("verdict", ""))
        reported = str(check.get("reported", "")).strip()
        if verdict in {"pass", "fail", "not_applicable"} and reported and not _MISSING_EVIDENCE.search(reported):
            substantive += 1
    return substantive >= 2


def _resolve_cross_batch_missing_conflicts(result: dict[str, Any]) -> dict[str, Any]:
    """后续分段已有具体数据时，撤销同一样品先前的“整份缺失”误报。"""
    resolved_labels: list[str] = []
    for sample in result.get("samples") or []:
        if not _has_substantive_sample_checks(sample):
            continue

        old_checks = sample.get("checks") or []
        old_items = sample.get("items") or []
        sample["checks"] = [check for check in old_checks if not _is_whole_sample_missing_entry(check)]
        sample["items"] = [item for item in old_items if not _is_whole_sample_missing_entry(item)]
        if len(sample["checks"]) != len(old_checks) or len(sample["items"]) != len(old_items):
            label = " ".join(
                str(sample.get(field, "")).strip() for field in ("model", "voltage", "spec")
                if sample.get(field)
            )
            if label:
                resolved_labels.append(label)

    if resolved_labels:
        result["remarks"] = [
            remark for remark in result.get("remarks") or []
            if not (
                _MISSING_EVIDENCE.search(str(remark))
                and _WHOLE_SAMPLE_MISSING_SCOPE.search(str(remark))
                and any(label.split()[0] in str(remark) for label in resolved_labels)
            )
        ]
    return result


def _merge_review_results_legacy(results: list[dict[str, Any]]) -> dict[str, Any]:
    """合并分段审核 JSON，按样品和检验项去重。"""
    merged: dict[str, Any] = {
        "application_no": "",
        "report_no": "",
        "company": "",
        "product_unit": "",
        "product_desc": "",
        "conclusion": "合格",
        "samples": [],
        "remarks": [],
        "detail": "",
    }
    samples: dict[tuple[str, ...], dict[str, Any]] = {}
    seen_items: set[tuple[str, ...]] = set()
    details: list[str] = []

    for part_number, result in enumerate(results, start=1):
        for field in ("application_no", "report_no", "company", "product_unit", "product_desc"):
            if not merged[field] and result.get(field):
                merged[field] = result[field]
        for remark in result.get("remarks") or []:
            if remark and remark not in merged["remarks"]:
                merged["remarks"].append(remark)
        if result.get("detail"):
            details.append(f"### 分段 {part_number}\n\n{result['detail']}")

        for sample in result.get("samples") or []:
            key = (
                str(sample.get("model", "")),
                str(sample.get("voltage", "")),
                str(sample.get("spec", "")),
                str(sample.get("_source_specimen_id") or ""),
            )
            target = samples.setdefault(key, {**sample, "items": []})
            target.setdefault("checks", [])
            seen_checks = {
                tuple(str(check.get(field, "")) for field in ("category", "item", "reported", "required", "verdict"))
                for check in target.get("checks") or []
            }
            for check in sample.get("checks") or []:
                check_key = tuple(str(check.get(field, "")) for field in (
                    "category", "item", "reported", "required", "verdict"
                ))
                if check_key not in seen_checks:
                    target["checks"].append(check)
                    seen_checks.add(check_key)
            for item in sample.get("items") or []:
                item_key = key + tuple(str(item.get(field, "")) for field in (
                    "item", "reported", "should_be", "standard", "severity"
                ))
                if item_key not in seen_items:
                    target["items"].append(item)
                    seen_items.add(item_key)

    merged["samples"] = list(samples.values())
    merged["detail"] = "\n\n".join(details)
    return _normalize_review_result(_resolve_cross_batch_missing_conflicts(merged))


def _merge_review_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """合并分段审核 JSON；新策略只在已验证且启用时生效。"""
    # 旧合并函数会在浅拷贝样品上追加 checks，合并全程不得反向污染分段输入。
    policy_input = deepcopy(results)
    legacy = _merge_review_results_legacy(deepcopy(results))
    from backend.app.executable_rules import apply_merge_policies
    policy_result = apply_merge_policies(policy_input, legacy)
    # 新策略重建了样品清单，必须在重建后再执行既有的“整份缺失”消解。
    return _normalize_review_result(_resolve_cross_batch_missing_conflicts(policy_result))


def _resolve_obsolete_rvs_impact_claims(result: dict[str, Any]) -> dict[str, Any]:
    """Archive only a wrong fixed-cable table claim; never infer whole-test P."""
    for sample in result.get('samples') or []:
        if not re.search(r'(?<![A-Z])RVS(?![A-Z])', str(sample.get('model') or '').upper()):
            continue
        verified = [c for c in sample.get('checks') or []
                    if c.get('impact_condition_checks') and c.get('evidence_status') == 'located'
                    and c.get('coverage_origin') == 'deterministic_source_recovery']
        if len(verified) != 1:
            continue
        source = verified[0]
        conditions = {c['field']: c for c in source['impact_condition_checks']}
        if conditions.get('裂纹结果', {}).get('verdict') != 'pass':
            continue
        mass = conditions.get('落锤质量', {}).get('reported')
        kept = []
        for old in sample.get('checks') or []:
            required = str(old.get('required') or '')
            basis = str(old.get('basis') or '')
            reported = str(old.get('reported') or '')
            title = re.sub(r'\s+', '', str(old.get('item') or ''))
            old_mass = re.findall(r'落锤(?:质量|重量)?\s*([0-9]+(?:\.[0-9]+)?)\s*g', reported)
            wrong_table_only = (old is not source and old.get('verdict') == 'fail'
                and re.fullmatch(r'(?:成品(?:电线|电缆|电线电缆)?)?低温冲击(?:试验)?', title)
                and re.search(r'2951\s*\.\s*14(?:-2008)?\s*表\s*2', basis + required)
                and len(re.findall(r'\d+\s*g', required)) == 1
                and not re.search(r'[；;]|温度|时间|裂纹|开裂|缺失|未做|未提供', required)
                and not re.search(r'有裂纹|开裂|不符合|超限|(?:^|[^A-Z])F(?:$|[^A-Z])', reported)
                and not re.search(r'有裂纹|开裂|时间不足|温度不符合|超限|未提供|未进行', str(old.get('note') or ''))
                and '无裂纹' in reported and len(old_mass) == 1 and float(old_mass[0]) == mass)
            if not wrong_table_only:
                kept.append(old)
                continue
            record = {'reason':'wrong_impact_table_family_source_confirmed', 'original_check':dict(old),
                      'replacement_verdict':source['verdict']}
            archive = sample.setdefault('superseded_model_checks', [])
            if record not in archive:
                archive.append(record)
            # Remove only the issue carrying the exact invalid requirement.
            retained = []
            for issue in sample.get('items') or []:
                compact = re.sub(r'\s+', '', str(issue.get('should_be') or ''))
                shorthand = re.fullmatch(r'落锤(?:重量|质量)?(\d+(?:\.\d+)?)g(?:[，,]P)?', compact)
                claimed_masses = re.findall(r'(\d+(?:\.\d+)?)\s*g', required)
                same_requirement = issue.get('should_be') == required or bool(
                    shorthand and len(claimed_masses) == 1 and float(shorthand[1]) == float(claimed_masses[0]))
                independent_issue = re.search(r'有裂纹|开裂|温度|时间|缺失|未进行',
                    ' '.join(str(issue.get(k) or '') for k in ('reported','review_action')))
                if (issue.get('item') == old.get('item') and same_requirement
                        and issue.get('standard') == basis and not independent_issue):
                    sample.setdefault('superseded_model_items', []).append(dict(issue))
                else:
                    retained.append(issue)
            sample['items'] = retained
        sample['checks'] = kept
    return result


def _supersede_pressure_model_passes(result: dict[str, Any]) -> dict[str, Any]:
    """同部件独立核验已有明细时，将原模型P留档，不并列显示相反结论。"""
    for sample in result.get('samples') or []:
        checks = sample.get('checks') or []
        authoritative = [c for c in checks if (c.get('coverage_origin')=='deterministic_source_recovery'
                         or (c.get('coverage_origin')=='deterministic_local_pressure_observation'
                             and c.get('local_pressure_observations') and c.get('local_pressure_depth_check')
                             and c.get('source_pages')))
                         and c.get('pressure_condition_checks') and c.get('evidence_status')=='located'
                         and '护套' in str(c.get('category') or '') and '高温压力' in str(c.get('item') or '')]
        impact_authoritative = [c for c in checks if c.get('coverage_origin')=='deterministic_source_recovery'
                                and c.get('impact_condition_checks') and c.get('evidence_status')=='located'
                                and c.get('verdict') in {'manual_review','fail'}]
        ozone_authoritative = [c for c in checks if c.get('coverage_origin')=='deterministic_ozone_report_conditions'
                               and c.get('ozone_report_observations') and c.get('source_pages')
                               and c.get('evidence_status')=='located' and c.get('item_code')=='OZONE_RESIST'
                               and c.get('category')=='绝缘机械性能']
        oven_authoritative = [c for c in checks if c.get('coverage_origin')=='local_pvc_oven_evidence'
                              and c.get('aging_condition_checks') and c.get('local_oven_observations')
                              and c.get('item_code')=='SHEATH_TENSILE_AFTER' and c.get('source_pages')
                              and c.get('evidence_status')=='located' and c.get('category')=='护套机械性能']
        loss_authoritative=[c for c in checks if c.get('coverage_origin')=='local_pvc_loss_evidence'
                            and c.get('loss_condition_checks') and c.get('local_loss_observations')
                            and c.get('item_code')=='SHEATH_LOSS_WEIGHT' and c.get('source_pages')
                            and c.get('evidence_status')=='located' and c.get('category')=='护套机械性能']
        if not authoritative and not impact_authoritative and not ozone_authoritative and not oven_authoritative and not loss_authoritative:
            continue
        kept = []
        for check in checks:
            label = str(check.get('category') or '') + str(check.get('item') or '')
            item = re.sub(r'\s+', '', str(check.get('item') or ''))
            pure_pressure_gap = (
                check.get('verdict')=='manual_review' and not check.get('coverage_origin')
                and '护套' in label and '绝缘' not in label
                and re.fullmatch(r'(?:护套)?高温压力(?:试验)?',item)
                and re.fullmatch(r'压痕(?:深度(?:中间值)?)?(?:/护套平均厚度)?(?:\s*[:：]\s*[/—-]+\s*(?:[（(]P[）)])?|\s*未(?:在表格中)?明确显示[，,；;]\s*(?:但)?单项评定P)',
                                 str(check.get('reported') or '').strip())
                and re.search(r'数据缺失|数值缺失|未识别|未提取|表格解析不完整',str(check.get('note') or ''))
                and not re.search(r'温度|时间|荷载|负荷|超限|不符合|材料|未做|未进行',
                                  str(check.get('note') or '')+str(check.get('required') or '')))
            replacements=[c for c in authoritative
                          if c.get('local_pressure_depth_check',{}).get('verdict') in {'pass','fail'}
                          and check.get('source_pages') and set(check['source_pages']) <= {
                              r['page'] for r in c.get('local_pressure_observations') or []}]
            if pure_pressure_gap and replacements:
                archive=sample.setdefault('superseded_model_checks',[])
                record={'reason':'missing_pressure_value_recovered_from_same_source_page',
                        'original_check':dict(check),'replacement_items':[c.get('item') for c in replacements]}
                if record not in archive:archive.append(record)
                retained=[]
                for old_item in sample.get('items') or []:
                    if (old_item.get('item')==check.get('item')
                            and old_item.get('reported')==check.get('reported')
                            and old_item.get('action_type')=='manual_review'
                            and old_item.get('severity')=='suggestion'
                            and not re.search(r'温度|时间|荷载|超限|不符合',str(old_item.get('review_action') or ''))):
                        saved={'reason':record['reason'],'original_item':dict(old_item)}
                        item_archive=sample.setdefault('superseded_model_items',[])
                        if saved not in item_archive:item_archive.append(saved)
                    else:
                        retained.append(old_item)
                sample['items']=retained
            elif (oven_authoritative and not check.get('coverage_origin') and check.get('verdict')=='manual_review'
                    and '护套' in label and '绝缘' not in label
                    and re.fullmatch(r'(?:护套)?(?:空气烘箱)?老化后(?:拉力|抗张强度和断裂伸长率)(?:试验)?',item)
                    and re.search(r'表格解析不完整|数据缺失|数值缺失|未识别|未提取',str(check.get('note') or ''))
                    and re.search(r'未(?:在表格中)?明确显示|数据缺失|数值缺失|未识别|未提取',str(check.get('reported') or ''))
                    and not re.search(r'\d|超限|不符合|不满足|未做|未进行|裂纹|错误|异常',
                                      str(check.get('reported') or '')+str(check.get('note') or ''))
                    and any(c.get('verdict')=='pass' and c.get('aging_condition_checks')
                            and all(r.get('verdict')=='pass' for r in c['aging_condition_checks'])
                            and check.get('source_pages') and set(check['source_pages'])<=set(c['source_pages'])
                            for c in oven_authoritative)):
                replacements=[c for c in oven_authoritative if c.get('verdict')=='pass'
                              and all(r.get('verdict')=='pass' for r in c['aging_condition_checks'])
                              and set(check['source_pages'])<=set(c['source_pages'])]
                reason='missing_oven_values_verified_from_same_source_page'
                record={'reason':reason,'original_check':dict(check),'replacement_items':[c['item'] for c in replacements]}
                archive=sample.setdefault('superseded_model_checks',[])
                if record not in archive:archive.append(record)
                retained=[]
                for old_item in sample.get('items') or []:
                    if (old_item.get('item')==check.get('item') and old_item.get('reported')==check.get('reported')
                            and old_item.get('action_type')=='manual_review' and old_item.get('severity')=='suggestion'
                            and old_item.get('source_pages')==check.get('source_pages')
                            and not re.search(r'超限|不符合|不满足|未做|未进行|裂纹|错误|异常',str(old_item.get('review_action') or ''))):
                        saved={'reason':reason,'original_item':dict(old_item)}
                        item_archive=sample.setdefault('superseded_model_items',[])
                        if saved not in item_archive:item_archive.append(saved)
                    else:retained.append(old_item)
                sample['items']=retained
            elif (loss_authoritative and not check.get('coverage_origin') and check.get('verdict')=='manual_review'
                    and '护套' in label and '绝缘' not in label
                    and re.fullmatch(r'(?:护套)?失重(?:试验)?',item)
                    and re.fullmatch(r'失重\s*[:：]\s*[/—-]+\s*(?:[（(]P[）)])?',str(check.get('reported') or '').strip())
                    and re.search(r'数据缺失|数值缺失|未识别|未提取',str(check.get('note') or ''))
                    and not re.search(r'温度|时间|超限|不符合|材料|未做|未进行',str(check.get('note') or '')+str(check.get('required') or ''))
                    and any(c.get('verdict')=='pass' and check.get('source_pages')
                            and set(check['source_pages'])<=set(c['source_pages']) for c in loss_authoritative)):
                replacements=[c for c in loss_authoritative if c.get('verdict')=='pass'
                              and set(check['source_pages'])<=set(c['source_pages'])]
                reason='missing_loss_value_verified_from_same_source_page'
                archive=sample.setdefault('superseded_model_checks',[])
                record={'reason':reason,'original_check':dict(check),'replacement_items':[c['item'] for c in replacements]}
                if record not in archive:archive.append(record)
                retained=[]
                for old_item in sample.get('items') or []:
                    missing_only=(old_item.get('item')==check.get('item')
                        and old_item.get('action_type')=='manual_review' and old_item.get('severity')=='suggestion'
                        and old_item.get('source_pages')==check.get('source_pages')
                        and re.fullmatch(r'失重数据缺失[，,；;]?\s*单项评定P',str(old_item.get('reported') or ''))
                        and not re.search(r'温度|时间|超限|不符合|材料|未做|未进行',str(old_item.get('review_action') or '')+str(old_item.get('should_be') or '')))
                    if missing_only:
                        saved={'reason':reason,'original_item':dict(old_item)}
                        item_archive=sample.setdefault('superseded_model_items',[])
                        if saved not in item_archive:item_archive.append(saved)
                    else:retained.append(old_item)
                sample['items']=retained
            elif (loss_authoritative and check not in loss_authoritative and check.get('verdict')=='pass'
                    and not check.get('coverage_origin') and '护套' in label and '绝缘' not in label
                    and re.fullmatch(r'(?:护套)?失重(?:试验)?',item)
                    and any(not check.get('source_pages') or set(check['source_pages'])<=set(c['source_pages'])
                            for c in loss_authoritative)):
                replacements=[c for c in loss_authoritative if not check.get('source_pages')
                              or set(check['source_pages'])<=set(c['source_pages'])]
                archive=sample.setdefault('superseded_model_checks',[])
                record={'reason':'same_component_source_loss_verification','original_check':dict(check),
                        'replacement_items':[c.get('item') for c in replacements]}
                if record not in archive:archive.append(record)
            elif (oven_authoritative and check not in oven_authoritative and check.get('verdict')=='pass'
                    and not check.get('coverage_origin') and '护套' in label and '绝缘' not in label
                    and re.fullmatch(r'(?:护套)?(?:空气烘箱)?老化后(?:拉力|抗张强度和断裂伸长率)(?:试验)?',item)
                    and any(not check.get('source_pages') or set(check['source_pages'])<=set(c['source_pages'])
                            for c in oven_authoritative)):
                replacements=[c for c in oven_authoritative if not check.get('source_pages')
                              or set(check['source_pages'])<=set(c['source_pages'])]
                archive=sample.setdefault('superseded_model_checks',[])
                record={'reason':'same_component_source_oven_verification','original_check':dict(check),
                        'replacement_items':[c.get('item') for c in replacements]}
                if record not in archive:archive.append(record)
            elif (ozone_authoritative and check not in ozone_authoritative and check.get('verdict')=='pass'
                    and '护套' not in label and not check.get('coverage_origin')
                    and re.fullmatch(r'(?:绝缘)?耐臭氧(?:试验)?', item)):
                archive = sample.setdefault('superseded_model_checks', [])
                record = {'reason':'same_component_source_ozone_verification', 'original_check':dict(check),
                          'replacement_items':[c.get('item') for c in ozone_authoritative]}
                if record not in archive:
                    archive.append(record)
            elif (impact_authoritative and check not in impact_authoritative and check.get('verdict')=='pass'
                    and not check.get('impact_condition_checks')
                    and re.fullmatch(r'(?:成品(?:电线|电缆|电线电缆)?)?低温冲击(?:试验)?', item)):
                archive = sample.setdefault('superseded_model_checks', [])
                record = {'reason':'same_sample_source_impact_verification', 'original_check':dict(check),
                          'replacement_items':[c.get('item') for c in impact_authoritative]}
                if record not in archive:
                    archive.append(record)
            elif (authoritative and check not in authoritative and check.get('verdict')=='pass'
                    and '护套' in label and '绝缘' not in label
                    and re.fullmatch(r'(?:护套)?高温压力(?:试验|[-—]压痕深度[-—]中间值)?', item)):
                archive = sample.setdefault('superseded_model_checks', [])
                record = {'reason':'same_component_source_pressure_verification', 'original_check':dict(check),
                          'replacement_items':[c.get('item') for c in authoritative]}
                if record not in archive:
                    archive.append(record)
            else:
                kept.append(check)
        sample['checks'] = kept
    return result


def _supersede_coordinate_table_artifacts(result: dict[str, Any], source_text: str,
        local_evidence: dict[str, Any] | None, source_pdf_sha256: str) -> dict[str, Any]:
    """Remove only model reviews proven to be flattened-table artefacts."""
    if not local_evidence or local_evidence.get('status')!='collected':return result
    from backend.app.rulebase import source_sample_registry,source_group_for_sample
    from backend.app.ocr_table_repair import attach_group
    registry=source_sample_registry(source_text)
    for sample in result.get('samples') or []:
        group=source_group_for_sample(sample,registry=registry)
        bound=attach_group(group,local_evidence,source_pdf_sha256,registry) if group else None
        if not bound:continue
        checks=sample.get('checks') or []
        model=re.sub(r'[^A-Z0-9]','',str(sample.get('model') or '').upper())
        shape=[d['round_shape'] for d in bound.get('_structure_dimensions',[]) if d.get('round_shape')]
        if model.startswith('RVVP') and len(shape)==1 and shape[0].get('flat_result_blank') is True:
            proof=shape[0]
            for check in checks:
                scope=' '.join(str(check.get(k) or '') for k in ('item','reported','note'))
                if (check.get('verdict')=='manual_review' and not check.get('coverage_origin')
                        and '结构检查' in str(check.get('item') or '')
                        and '外形尺寸-平均外径（扁）' in scope
                        and re.search(r'错位|混排|不匹配',scope)):
                    sample.setdefault('superseded_model_checks',[]).append(
                        {'reason':'coordinate_round_structure_row_binding','original_check':deepcopy(check)})
                    check.update(verdict='pass',reported=(f"原PDF坐标表：扁形平均外径结果列为空；"
                        f"椭圆度{proof['ovalness_percent']:g}%，判P"),
                        required='圆形RVVP不应把椭圆度结果错绑到扁形外径行',
                        note='原PDF坐标确认6属于椭圆度行，不是扁形外径实测值',
                        source_pages=[int(proof['page'])],evidence_status='located',
                        coverage_origin='coordinate_structure_row_recovery')
                    sample['items']=[item for item in sample.get('items') or []
                                     if not ('结构检查' in str(item.get('item') or '')
                                             and '扁' in ' '.join(str(item.get(k) or '') for k in ('reported','should_be','review_action')))]
        pressure=[o for o in bound.get('_pressure_depth_observations',[])
                  if o.get('component')=='绝缘' and o.get('verdict')=='pass']
        if len(pressure)==1:
            proof=pressure[0]
            for check in checks:
                scope=' '.join(str(check.get(k) or '') for k in ('reported','note','required'))
                if (check.get('verdict')=='manual_review' and not check.get('coverage_origin')
                        and re.fullmatch(r'(?:绝缘)?高温压力(?:试验)?',str(check.get('item') or ''))
                        and re.search(r'P/N|混排|错位|重复行',scope)
                        and not re.search(r'超限|不符合|未做|未进行|有裂纹|(?:^|[^A-Z])F(?:$|[^A-Z])',scope,re.I)):
                    sample.setdefault('superseded_model_checks',[]).append(
                        {'reason':'coordinate_insulation_pressure_row_binding','original_check':deepcopy(check)})
                    values='/'.join(f'{v:g}' for v in proof['reported_values'])
                    check.update(verdict='pass',reported=(f'压痕深度{values}%；{proof["temperature_c"]:g}℃/'
                        f'{proof["hours"]:g}h；报告判P'),required='压痕深度≤50%；温度70±2℃',
                        note='原PDF坐标行已分别绑定结果列与P评定，未使用合并HTML的P/N串',
                        source_pages=[int(proof['page'])],evidence_status='located',
                        coverage_origin='coordinate_insulation_pressure_recovery')
                    sample['items']=[item for item in sample.get('items') or []
                                     if not (item.get('item')==check.get('item')
                                             and re.search(r'P/N|混排|错位|重复行',' '.join(
                                                 str(item.get(k) or '') for k in ('reported','review_action','should_be'))))]
        authoritative={c.get('item'):c for c in checks
                       if c.get('coverage_origin')=='coordinate_lowtemp_row_recovery'
                       and c.get('verdict') in {'pass','not_applicable'} and c.get('source_pages')}
        kept=[]
        for check in checks:
            replacement=authoritative.get(check.get('item'))
            scope=' '.join(str(check.get(k) or '') for k in ('reported','required','note'))
            if (replacement is not None and check is not replacement and not check.get('coverage_origin')
                    and check.get('verdict')=='manual_review'
                    and set(check.get('source_pages') or [])<=set(replacement.get('source_pages') or [])
                    and re.search(r'无法判定适用性|未明确护套外径|表格|解析|错位|137',scope)
                    and not re.search(r'有裂纹|开裂|低于|超限|不符合|(?:^|[^A-Z])F(?:$|[^A-Z])',scope,re.I)):
                sample.setdefault('superseded_model_checks',[]).append(
                    {'reason':'coordinate_lowtemp_row_binding','original_check':deepcopy(check),
                     'replacement_item':replacement.get('item')})
                sample['items']=[item for item in sample.get('items') or [] if item.get('item')!=check.get('item')]
            else:kept.append(check)
        sample['checks']=kept
    return result


def _normalize_review_result(result: dict[str, Any]) -> dict[str, Any]:
    """统一行动语义并按真正需要处理的项目重算结论。"""
    for sample in result.get("samples") or []:
        # Keep combined checks intact. Consolidate only a duplicated, explicitly
        # sourced impact-dimension action under its independent impact item.
        impact_checks=[c for c in sample.get('checks') or []
            if c.get('coverage_origin')=='deterministic_source_recovery'
            and re.fullmatch(r'成品低温冲击(?:试验)?',str(c.get('item') or ''))
            and c.get('verdict')=='manual_review' and c.get('evidence_status')=='located'
            and c.get('source_pages') and c.get('deterministic_review_action')
            and (c.get('impact_dimension_evidence') or {}).get('status')=='missing_or_conflicting']
        if len(impact_checks)==1:
            impact=impact_checks[0]
            action=impact['deterministic_review_action']
            standalone=[i for i in sample.get('items') or [] if i.get('item')==impact['item']
                and i.get('severity')=='suggestion' and i.get('action_type')=='manual_review'
                and i.get('review_action')==action and i.get('source_pages')==impact['source_pages']]
            if len(standalone)==1:
                retained=[]
                for item in sample.get('items') or []:
                    label=str(item.get('item') or '')
                    combined=[c for c in sample.get('checks') or [] if c.get('item')==label
                        and c.get('verdict')=='manual_review' and c.get('deterministic_review_action')==action
                        and (c.get('impact_dimension_evidence') or {}).get('status')=='missing_or_conflicting']
                    parts=re.split(r'[、,，]',label)
                    duplicate=(len(parts)>1 and all(re.fullmatch(r'(?:绝缘|护套)低温(?:弯曲|拉伸)|成品低温冲击',p) for p in parts)
                        and '成品低温冲击' in parts and len(combined)==1
                        and item.get('severity')=='suggestion' and item.get('action_type')=='manual_review'
                        and item.get('review_action')==action)
                    if duplicate:
                        record={'reason':'same_sample_impact_dimension_action_consolidated',
                                'original_item':dict(item),'replacement_item':impact['item']}
                        archive=sample.setdefault('consolidated_action_items',[])
                        if record not in archive:archive.append(record)
                        related=standalone[0].setdefault('related_check_items',[])
                        if label not in related:related.append(label)
                    else:retained.append(item)
                sample['items']=retained
        # 后处理只更新 reported/basis；历史兼容字段必须同步，避免不同
        # 展示入口读到纠错前的值。差异留档，不触碰判定或原模型归档。
        for check in sample.get("checks") or []:
            for canonical, alias in (("reported", "reported_value"), ("basis", "standard_ref")):
                if canonical not in check or check[canonical] is None:
                    continue
                if alias in check and check[alias] != check[canonical]:
                    record = {"field": alias, "previous": check[alias],
                              "current": check[canonical], "reason": "final_canonical_field_sync"}
                    history = check.setdefault("display_field_history", [])
                    if record not in history:
                        history.append(record)
                check[alias] = check[canonical]
        spec = str(sample.get("spec", "")).strip()
        spec_match = re.fullmatch(r"(\d+)\s*[×xX*]\s*([0-9]+(?:\.[0-9]+)?)(?:\s*mm(?:2|²))?", spec, re.I)
        if spec_match:
            sample["spec"] = f"{int(spec_match.group(1))}×{float(spec_match.group(2)):g}mm²"
        for item in sample.get("items") or []:
            severity = str(item.get("severity", "")).lower()
            if severity == "ok" or _declares_no_action(item):
                item["severity"] = "ok"
                item["action_required"] = False
                item["action_type"] = "none"
                item["review_action"] = ""
            elif item.get("action_required") is True:
                item["action_type"] = (
                    "correction" if severity == "must_fix" else
                    "manual_review" if severity == "suggestion" else "none"
                )
        # items 是给审核员执行的行动清单；无需行动的说明已经保留在
        # checks 与确定性校验记录中，不再混入问题列表。
        sample["items"] = [
            item for item in (sample.get("items") or [])
            if item.get("severity") in ("must_fix", "suggestion")
            and item.get("action_required") is not False
        ]

    # 输出一致性守门：items 按 (item, reported, should_be) 去重，保证
    # 结论计数与实际复核条目一致；pass 备注不得残留未决的「需人工复核/
    # 需人工核对」措辞，否则降级为 manual_review 交回人工。
    for sample in result.get("samples") or []:
        seen_item_keys: set[tuple[str, str, str]] = set()
        deduped_items: list[dict[str, Any]] = []
        for item in sample.get("items") or []:
            item_key = (
                str(item.get("item", "")),
                str(item.get("reported", "")),
                str(item.get("should_be", "")),
            )
            if item_key in seen_item_keys:
                continue
            seen_item_keys.add(item_key)
            deduped_items.append(item)
        if len(deduped_items) != len(sample.get("items") or []):
            sample["items"] = deduped_items
        for check in sample.get("checks") or []:
            if check.get("verdict") != "pass":
                continue
            note = str(check.get("note") or "")
            if (
                ("需人工复核" in note or "需人工核对" in note)
                and not any(
                    neg in note
                    for neg in (
                        "不因此要求人工复核", "不要求人工复核",
                        "无需人工复核", "不需人工复核",
                    )
                )
            ):
                check["verdict"] = "manual_review"
                check["_consistency_guard"] = "pass_note_requested_manual_review"

    # 结论计数必须与审核员实际看到的条目一致：action items 仍是主体，
    # 但没有对应 item 的 fail/manual_review checks 也计入结论，
    # 避免「结论说合格、清单里却挂着待复核条目」式的数字分裂。
    def _labels_match(item_label: str, check_label: str) -> bool:
        a = re.sub(r"\s+", "", str(item_label))
        b = re.sub(r"\s+", "", str(check_label))
        if not a or not b:
            return False
        if a in b or b in a:
            return True
        # 分段标题 vs 行动项描述（如「绝缘低温弯曲/…适用性待复核」与
        # 「绝缘低温弯曲试验、绝缘低温拉伸试验」）：最长公共子串 ≥4 字
        # 即视为同一事项。
        prev = [0] * (len(b) + 1)
        best = 0
        for ca in a:
            cur = [0] * (len(b) + 1)
            for k, cb in enumerate(b, start=1):
                if ca == cb:
                    cur[k] = prev[k - 1] + 1
                    if cur[k] > best:
                        best = cur[k]
            prev = cur
        return best >= 4

    # 样品级事项在下方按「check↔items 分组」统一计数（同一事项的
    # check 与 items 只计一次），此处只初始化报告级计数。
    must_fix_count = 0
    review_count = 0
    for sample in result.get("samples") or []:
        active_items = [
            item for item in (sample.get("items") or [])
            if item.get("action_required") is not False
        ]
        matched_item_ids: set[int] = set()
        for check in sample.get("checks") or []:
            verdict = check.get("verdict")
            if verdict not in ("fail", "manual_review"):
                continue
            label = str(check.get("item", ""))
            severities = ("must_fix",) if verdict == "fail" else ("must_fix", "suggestion")
            matched = [
                idx for idx, item in enumerate(active_items)
                if idx not in matched_item_ids
                and item.get("severity") in severities
                and _labels_match(item.get("item", ""), label)
            ]
            if matched:
                # check 与其匹配的 items 视为同一事项，只计一次；
                # 同一 check 匹配多个 items 时按一个事项计。
                matched_item_ids.update(matched)
                if verdict == "fail":
                    must_fix_count += 1
                else:
                    review_count += 1
            elif verdict == "fail":
                must_fix_count += 1
            else:
                review_count += 1
        for idx, item in enumerate(active_items):
            if idx in matched_item_ids:
                continue
            if item.get("severity") == "must_fix":
                must_fix_count += 1
            elif item.get("severity") == "suggestion":
                review_count += 1
    must_fix_count += sum(1 for item in result.get("report_items") or []
                          if item.get("severity") == "must_fix" and item.get("action_required") is not False)
    review_count += sum(1 for item in result.get("report_items") or []
                        if item.get("severity") == "suggestion" and item.get("action_required") is not False)
    result["conclusion"] = (
        f"需修改{must_fix_count}处" if must_fix_count else
        f"待人工复核{review_count}处" if review_count else "合格"
    )
    if result.get("report_checks") or any(sample.get("checks") for sample in result.get("samples") or []):
        result["detail"] = _build_detailed_audit(result)
    return result


def _repair_malformed_deterministic_checks(result: dict[str, Any], source_text: str) -> dict[str, Any]:
    """用同一样品原页重建明显串列的确定性结果，成功前不改变结论。"""
    from backend.app.rulebase import (
        _dimension_source_evidence,
        _mechanical_source_evidence,
        _same_issue,
        source_group_for_sample,
        source_sample_registry,
    )

    registry = source_sample_registry(source_text)
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        source_group = source_group_for_sample(sample, registry=registry)
        if not source_group:
            continue
        allowed_pages = {int(page["page"]) for page in source_group.get("pages") or []}
        enriched_group = {**source_group, "pages": list(source_group.get("pages") or [])}
        for section in re.finditer(
            r"--- Page\s+(\d+)\s*\(([^)]*)\)\s*---\s*([\s\S]*?)(?=\n--- Page\s+\d+\s*\(|\Z)",
            source_text,
            re.I,
        ):
            page_no = int(section.group(1))
            if page_no in allowed_pages and "mineru" in section.group(2).lower():
                enriched_group["pages"].append({"page": page_no, "text": section.group(3)})
        for check in sample.get("checks") or []:
            if str(check.get("verdict") or "").lower() not in {"fail", "manual_review"}:
                continue
            label = f"{check.get('category', '')}{check.get('item', '')}"
            reported = str(check.get("reported") or "")
            suspicious = bool(
                re.search(r"\d(?:\.\d+)?e[+\-]\d+", reported, re.I)
                or reported.count("/") >= 8
                or ("外径" in label and "外形尺寸-平均外径" in reported)
            )
            # 护套老化表被纵向OCR串列后，模型有时已经谨慎地给出
            # manual_review，而 reported 本身不再含科学计数法或大量斜杠。
            # 这类检查仍应尝试用“同一样品、同一原页、完整项目标题及整组P”
            # 的严格证据恢复；证据条件不满足时保持人工复核，不猜测通过。
            strict_sheath_manual_candidate = bool(
                str(check.get("verdict") or "").lower() == "manual_review"
                and "护套" in label
                and "老化后" in label
            )
            strict_dimension_manual_candidate = bool(
                str(check.get("verdict") or "").lower() in {"fail", "manual_review"}
                and "外径" in label
                and check.get("source_pages")
            )
            if (
                not suspicious
                and not strict_sheath_manual_candidate
                and not strict_dimension_manual_candidate
            ):
                continue
            item_code = (
                "OD_MEAS" if "外径" in label else
                "SHEATH_TENSILE_AFTER" if "护套" in label and "老化后" in label else
                "SHEATH_TENSILE_BEFORE" if "护套" in label and "老化前" in label else
                "HEAT_PRESS_SHEATH" if "护套" in label and "高温压力" in label else ""
            )
            if not item_code:
                continue
            matrix_row = {
                "item_code": item_code,
                "item_name": check.get("item") or "",
                "standard_no": str(check.get("basis") or check.get("standard_ref") or ""),
                "table_no": "", "table_item_no": "",
            }
            recovered = (
                _dimension_source_evidence(matrix_row, enriched_group)
                if item_code == "OD_MEAS"
                else _mechanical_source_evidence(matrix_row, enriched_group, source_text)
            )
            # A P column is the report's claim, not numerical validation.
            # If extraction cannot establish a result, preserve uncertainty.
            if not recovered or recovered.get("verdict") != "pass":
                continue
            check.clear()
            check.update(recovered)
            sample["items"] = [
                item for item in (sample.get("items") or []) if not _same_issue(check, item)
            ]
            records.append({"item": check.get("item"), "status": "recovered_from_sample_source"})
    if records:
        result.setdefault("_deterministic_validation", {})["malformed_check_recovery"] = records
    return result


def _enforce_check_action_consistency(result: dict[str, Any]) -> dict[str, Any]:
    """任何最终fail都必须有行动项；可疑提取不得直接宣称报告不合格。"""
    from backend.app.rulebase import _same_issue

    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        sample.setdefault("items", [])
        source_bound_manual_checks = []
        # A source-bound unresolved action cannot coexist with a green verdict
        # for that exact check. Do not use fuzzy issue matching across components.
        for item in sample["items"]:
            if (item.get('severity') != 'suggestion' or item.get('action_type') != 'manual_review'
                    or item.get('action_required') is not True or _declares_no_action(item)
                    or item.get('evidence_status') != 'located'
                    or not item.get('source_pages') or not item.get('source_segments')):
                continue
            peers = [c for c in sample.get('checks') or []
                if str(c.get('item') or '').strip() == str(item.get('item') or '').strip()
                and c.get('evidence_status') == 'located'
                and set(c.get('source_pages') or []) == set(item['source_pages'])
                and set(c.get('source_segments') or []) == set(item['source_segments'])
                and (not item.get('category') or item['category'] == c.get('category'))]
            if len(peers) != 1:
                continue
            check = peers[0]
            if (check.get('verdict') == 'manual_review'
                    and ((check.get('action_consistency_resolution') or {}).get('reason') == 'source_bound_unresolved_action'
                         or check.get('source_aging_arithmetic'))):
                source_bound_manual_checks.append(check)
                continue
            if check.get('verdict') != 'pass':
                continue
            archive = sample.setdefault('superseded_model_checks', [])
            record = {'reason':'source_bound_unresolved_action_conflicts_with_pass',
                      'original_check':dict(check), 'replacement_items':[check.get('item')]}
            if record not in archive:
                archive.append(record)
            check['verdict'] = 'manual_review'
            check['action_consistency_resolution'] = {
                'reason':'source_bound_unresolved_action', 'previous_verdict':'pass',
                'source_pages':list(item['source_pages']),
                'source_segments':list(item['source_segments'])}
            records.append({'item':check.get('item'),'status':'pass_conflict_to_manual_review'})
            source_bound_manual_checks.append(check)
        for check in sample.get("checks") or []:
            if any(check is bound for bound in source_bound_manual_checks):
                # An exact source-bound action already exists, regardless of
                # fuzzy issue matching or slash-separated multi-core values.
                continue
            current_verdict = str(check.get("verdict") or "").lower()
            if current_verdict not in {"fail", "manual_review"}:
                continue
            numeric_proof = check.get('source_numeric_range')
            if numeric_proof and numeric_proof.get('observations'):
                exact_actions = [item for item in sample['items']
                    if item.get('source_numeric_range') == numeric_proof
                    and item.get('item') == check.get('item')
                    and item.get('category') == check.get('category')
                    and set(item.get('source_pages') or []) == set(check.get('source_pages') or [])
                    and item.get('action_required') is True and not _declares_no_action(item)
                    and (item.get('severity'),item.get('action_type')) ==
                        (('must_fix','correction') if current_verdict=='fail' else ('suggestion','manual_review'))]
                if len(exact_actions) == 1:
                    # Exact structured evidence is stronger than fuzzy label
                    # matching; don't duplicate a component-bound range issue.
                    continue
            label = f"{check.get('category', '')}{check.get('item', '')}"
            reported = str(check.get("reported") or "")
            required = str(check.get("required") or "")
            note = str(check.get("note") or "")

            # 结构大项只表示原表范围已覆盖，具体尺寸由独立规则复算；
            # 它自身不能因表内存在N项而判整项失败。
            if "结构检查" in label and "不替代" in note and "原表" in reported:
                check["verdict"] = "pass"
                records.append({"item": check.get("item"), "status": "aggregate_check_normalized"})
                continue

            malformed = bool(
                re.search(r"\d(?:\.\d+)?e[+\-]\d+", reported, re.I)
                or reported.count("/") >= 8
                or (
                    "外径" in label and "最小" in required and "最大" not in required
                    and reported.count("/") >= 1
                )
            )
            if (check.get('matrix_exclusion_resolution') or {}).get('status') == 'exclusion_objection_retracted_tests_not_passed':
                # This check is pending because its sole model objection was
                # disproven by the matrix, not because slash-separated pairs
                # prove an extraction failure. No measurements are passed.
                malformed = False
            matching = [
                item for item in sample["items"]
                if item.get("severity") in {"must_fix", "suggestion"}
                and item.get("action_required") is not False
                and not _declares_no_action(item)
                and _same_issue(check, item)
            ]
            if current_verdict == "manual_review" and matching:
                continue
            if malformed:
                check["verdict"] = "manual_review"
                sample["items"] = [item for item in sample["items"] if item not in matching]
                issue = {
                    "item": f"{check.get('item') or '检查项'}确定性提取异常",
                    "reported": reported,
                    "should_be": "重新从同一样品原页提取项目、限值、实测值和P/F/N，禁止使用串联数值自动判定",
                    "standard": check.get("basis") or check.get("standard_ref") or "",
                    "severity": "suggestion", "action_required": True,
                    "action_type": "manual_review",
                    "review_action": "回看所列原页确认表格列对应关系；修复提取前不自动放行或判不合格",
                    "evidence_status": check.get("evidence_status") or "not_located",
                    "source_pages": check.get("source_pages") or [],
                    "source_excerpt": check.get("source_excerpt") or "",
                }
                sample["items"].append(issue)
                records.append({"item": check.get("item"), "status": "malformed_to_manual_review"})
                continue

            if current_verdict == "manual_review":
                sample["items"].append({
                    "item": check.get("item") or "待人工复核项",
                    "reported": reported,
                    "should_be": required or "核对原页后确定P/F/N",
                    "standard": check.get("basis") or check.get("standard_ref") or "",
                    "severity": "suggestion", "action_required": True,
                    "action_type": "manual_review",
                    "review_action": (
                        str(check.get('precision_review_action'))
                        if (check.get('coverage_origin') == 'rubber_sheath_aging_n_recovery'
                            or check.get('precision_review_origin') == 'deterministic_pressure_force_display') and check.get('precision_review_action')
                        else str(check.get('deterministic_review_action') or "回看所列原页完成该项目判断")
                    ),
                    "evidence_status": check.get("evidence_status") or "not_located",
                    "source_pages": check.get("source_pages") or [],
                    "source_excerpt": check.get("source_excerpt") or "",
                })
                records.append({"item": check.get("item"), "status": "manual_review_action_created"})
                continue

            if matching:
                for item in matching:
                    item.update({
                        "severity": "must_fix", "action_required": True,
                        "action_type": "correction", "reported": reported,
                        "should_be": required or item.get("should_be") or "按对应标准要求更正",
                        "standard": check.get("basis") or item.get("standard") or "",
                        "review_action": item.get("review_action") or "更正报告值或评定并重新审核",
                    })
                records.append({"item": check.get("item"), "status": "existing_issue_upgraded"})
                continue

            sample["items"].append({
                "item": check.get("item") or "审核不符合项",
                "reported": reported,
                "should_be": required or "按对应标准要求更正",
                "standard": check.get("basis") or check.get("standard_ref") or "",
                "severity": "must_fix", "action_required": True,
                "action_type": "correction", "review_action": "更正报告值或评定并重新审核",
                "evidence_status": check.get("evidence_status") or "not_located",
                "source_pages": check.get("source_pages") or [],
                "source_excerpt": check.get("source_excerpt") or "",
            })
            records.append({"item": check.get("item"), "status": "missing_action_created"})
    if records:
        result.setdefault("_deterministic_validation", {})["check_action_consistency"] = records
    return result


def _plain_html_cell(value: str) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", value or ""))
    text = text.replace("$", " ").replace(r"\times", "×")
    text = re.sub(r"\\mathrm\{([^}]*)\}", r"\1", text)
    text = re.sub(r"mm\^\{?2\}?", "mm²", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _merge_source_bound_batches(partial_results: list[dict[str, Any]], source_text: str) -> dict[str, Any]:
    from copy import deepcopy
    from backend.app.rulebase import bind_batch_sample_identity, source_sample_registry, align_batch_source_registry
    batches = _split_report_by_samples(source_text)
    results = deepcopy(partial_results)
    for partial in results:
        for sample in partial.get('samples') or []:
            sample.pop('_source_specimen_id', None)
    records = []
    full_registry = source_sample_registry(source_text)
    if len(batches) == len(results):
        for index, (batch, partial) in enumerate(zip(batches, results), start=1):
            if not batch.get("sample_aware"):
                continue
            local_text = batch["text"].split("【本批次完整样品页面】", 1)[-1]
            registry = source_sample_registry(local_text)
            registry = align_batch_source_registry(registry, full_registry)
            records.extend(dict(batch=index, **record) for record in bind_batch_sample_identity(partial, registry))
    result = results[0] if len(results) == 1 else _merge_review_results(results)
    if records:
        result.setdefault("_deterministic_validation", {})["batch_source_identity_binding"] = records
    return result


def _split_sample_identity(value: str) -> tuple[str, str, str]:
    identity = _plain_html_cell(value)
    voltage_match = re.search(r"\d{2,3}\s*/\s*\d{2,3}\s*V?", identity, re.I)
    if not voltage_match:
        return identity, "", ""
    voltage = re.sub(r"\s+", "", voltage_match.group(0)).upper()
    if not voltage.endswith("V"):
        voltage += "V"
    model = identity[:voltage_match.start()].strip(" -：:")
    tail = identity[voltage_match.end():]
    from backend.app.structural_facts import canonical_spec
    spec = canonical_spec(tail.strip())
    if not spec and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*mm(?:2|²)\s*", tail, re.I):
        # Preserve explicit bare area for the model-specific identity resolver.
        spec = re.sub(r"\s+", "", tail).replace("mm2", "mm²")
    return model, voltage, spec


def _arsenic_report_entries(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for table in re.findall(r"<table\b[^>]*>.*?</table>", text or "", re.I | re.S):
        if "样品型号规格" not in table or not re.search(r"砷\s*(?:As|（As）)", table, re.I):
            continue
        identity_match = re.search(r"样品型号规格</td>\s*<td[^>]*>(.*?)</td>", table, re.I | re.S)
        material_match = re.search(r"材料类型</td>\s*<td[^>]*>(.*?)</td>", table, re.I | re.S)
        result_match = re.search(r"砷\s*(?:As|（As）)</td>\s*<td[^>]*>(.*?)</td>", table, re.I | re.S)
        if not identity_match or not result_match:
            continue
        identity = _plain_html_cell(identity_match.group(1))
        material_text = _plain_html_cell(material_match.group(1)) if material_match else ""
        if re.search(r"[☑✓√]\s*绝缘", material_text):
            material = "绝缘"
        elif re.search(r"[☑✓√]\s*护套", material_text):
            material = "护套"
        elif "绝缘" in material_text and "护套" not in material_text:
            material = "绝缘"
        elif "护套" in material_text and "绝缘" not in material_text:
            material = "护套"
        else:
            material = "材料类型待核对"
        reported = _plain_html_cell(result_match.group(1))
        key = (identity, material, reported)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"identity": identity, "material": material, "reported": reported})
    return entries


def _materials_special_scope(text: str) -> bool:
    plain = _plain_html_cell(text)
    declared = bool(re.search(r"试验依据标准.{0,500}6040[-—－]2019.{0,300}33047[.]1[-—－]2016", plain))
    conclusion = bool(re.search(r"试验结论.{0,100}红外光谱.{0,80}热重.{0,100}(?:砷|锑).{0,100}见附表", plain))
    # A report with actual conventional test headings is mixed/full scope,
    # even when its cover also declares materials analysis.
    conventional = bool(re.search(r"导体电阻|高温压力|老化前抗张|成品(?:电缆|电线电缆)?电压试验|绝缘电阻", plain))
    return declared and conclusion and not conventional


def _enforce_materials_special_scope(result: dict[str, Any]) -> dict[str, Any]:
    specialty = re.compile(r"红外|光谱|热重|砷|锑|元素|限用物质")
    conventional = re.compile(r"导体|电阻|电压|结构检查|厚度|外径|老化|拉力|失重|高温压力|低温|热冲击|热延伸|非污染|燃烧|阻燃|曲挠|浸矿物油|耐臭氧|标志|线芯")
    removed = []
    found = False
    for sample in result.get('samples') or []:
        for field in ('checks', 'items'):
            kept = []
            for entry in sample.get(field) or []:
                label = str(entry.get('item') or '')
                if specialty.search(label):
                    found = True
                    kept.append(entry)
                elif conventional.search(label):
                    removed.append({'model':sample.get('model'), 'item':label, 'field':field})
                else:
                    # Generic identity, privacy and document issues must survive.
                    kept.append(entry)
            sample[field] = kept
    if not found:
        samples = result.setdefault('samples', [])
        if not samples:
            samples.append({'model':'材料分析专项报告','spec':'','voltage':'','checks':[],'items':[]})
        samples[0].setdefault('items', []).append({
            'item':'材料分析专项结果尚未提取', 'reported':'声明了专项范围，但结构化结果未覆盖专项检测',
            'should_be':'核对红外、热重及砷锑附表', 'severity':'suggestion', 'action_required':True,
            'action_type':'manual_review','review_action':'核对专项附表，不能因排除型式试验矩阵而直接判合格'})
    validation = result.setdefault('_deterministic_validation', {})
    for key in ('required_item_coverage','lowtemp_impact_mass','lowtemp_conditioning_time'):
        validation.pop(key, None)
    validation['report_type_scope'] = {'type':'materials_analysis', 'basis':'declared_methods_and_conclusion_no_conventional_tests',
                                      'excluded_type_test_entries':removed, 'specialty_entries_present':found}
    return result


def _enforce_special_report_scope(result: dict[str, Any], text: str) -> dict[str, Any]:
    """砷元素专项报告只审核声明范围，不套用完整型式试验矩阵。"""
    source = str(text or "")
    if _materials_special_scope(source):
        return _enforce_materials_special_scope(result)
    arsenic_only = (
        "砷元素分析报告" in source
        and "砷元素快检分析测试结果" in source
        and not re.search(r"导体电阻\s*\(20|高温压力[-—]压痕|成品电线电缆电压试验", source)
    )
    if not arsenic_only:
        return result
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in _arsenic_report_entries(source):
        model, voltage, spec = _split_sample_identity(entry["identity"])
        sample = grouped.setdefault((model, voltage, spec), {
            "model": model, "voltage": voltage, "spec": spec, "checks": [], "items": [],
        })
        reported = entry["reported"]
        below_limit = bool(re.search(r"(?:小于|<|≤)\s*1000", reported))
        numeric_match = re.search(r"(\d+(?:\.\d+)?)", reported)
        numeric_pass = bool(numeric_match and float(numeric_match.group(1)) <= 1000)
        material = entry["material"]
        verdict = "pass" if (below_limit or numeric_pass) and material != "材料类型待核对" else "manual_review"
        sample["checks"].append({
            "category": "限用物质", "item": f"砷元素快检分析（{material}）",
            "reported": f"{reported} mg/kg" if "mg/kg" not in reported else reported,
            "required": "砷含量≤1000 mg/kg", "verdict": verdict,
            "basis": "CQC-C0101-2024 附件7",
            "note": "专项报告范围已确定，未套用完整型式试验矩阵",
        })
        if verdict != "pass":
            sample["items"].append({
                "item": f"砷元素快检分析（{material}）", "reported": reported or "未能提取结果",
                "should_be": "确认材料类型及砷含量是否≤1000 mg/kg",
                "standard": "CQC-C0101-2024 附件7", "severity": "suggestion",
                "action_required": True, "action_type": "manual_review",
                "review_action": "回看专项报告附表中的材料勾选和砷测试结果",
            })
    if not grouped:
        grouped[("砷元素专项报告", "", "")] = {
            "model": "砷元素专项报告", "voltage": "", "spec": "", "checks": [],
            "items": [{
                "item": "砷元素快检结果提取失败",
                "reported": "已识别为砷元素专项报告，但未定位到结构化附表结果",
                "should_be": "核对附表中的材料类型和砷含量",
                "standard": "CQC-C0101-2024 附件7", "severity": "suggestion",
                "action_required": True, "action_type": "manual_review", "review_action": "回看专项报告附表",
            }],
        }
    result["samples"] = list(grouped.values())
    result["product_desc"] = "砷元素快检专项报告"
    result["remarks"] = ["本报告为砷元素快检专项报告，仅审核专项报告声明的元素分析范围。"]
    result["detail"] = "已按CQC-C0101-2024附件7核对砷元素快检结果，未套用完整产品型式试验矩阵。"
    # 专项范围确认后，清除此前按完整型式试验生成的覆盖/低温等中间记录，
    # 避免终审区仍展示已被范围门控否决的无关检查。
    validation = result.setdefault("_deterministic_validation", {})
    for key in list(validation):
        if key not in {"privacy_logic", "outbound_guard"}:
            validation.pop(key, None)
    validation["report_type_scope"] = {
        "type": "arsenic_element_analysis", "scope": "arsenic_only",
        "parsed_entries": sum(len(sample["checks"]) for sample in grouped.values()),
    }
    return result


_CONVENTIONAL_TYPE_TEST_EVIDENCE = re.compile(
    r"导体电阻|高温压力(?:[-—]压痕)?|老化前抗张|成品(?:电缆|电线电缆)?电压试验|绝缘电阻"
)
_ARSENIC_SPECIAL_EVIDENCE = re.compile(
    r"砷元素(?:快检)?(?:分析|检测|测试|分析测试)?报告"
)
_SPECTRUM_SPECIAL_EVIDENCE = re.compile(
    r"(?:红外(?:光谱)?|图谱|光谱|材料(?:一致性)?)(?:分析|检测|检查|比对|测试)?报告"
)
_TYPE_TEST_TITLE_EVIDENCE = re.compile(r"(?:总|安全)?型式试验(?:检测)?报告(?:书)?")


def _bound_report_category(report_id: int) -> str | None:
    """读取浏览器入口已确认的报告类别；旧数据或无绑定报告返回None。"""
    try:
        from backend.app.browser_intake import BrowserReportBinding, BrowserReportScope
        from backend.app.database import SessionLocal
        db = SessionLocal()
        try:
            binding = db.get(BrowserReportBinding, report_id)
            if not binding:
                return None
            scope = db.get(BrowserReportScope, binding.scope_id)
            category = str(scope.category or "").strip() if scope else ""
            return category if category in {"type_test", "arsenic", "spectrum"} else None
        finally:
            db.close()
    except Exception:
        # 手工上传、旧数据库或测试环境可能没有浏览器分类表；此时使用正文证据。
        return None


def _special_report_category(text: str, report_id: int) -> str | None:
    """在加载任何型式试验规则前确定专项路线，混合范围失败关闭。"""
    plain = _plain_html_cell(text or "")
    bound = _bound_report_category(report_id)
    conventional = bool(_CONVENTIONAL_TYPE_TEST_EVIDENCE.search(plain))
    spectrum = bool(_SPECTRUM_SPECIAL_EVIDENCE.search(plain)) or bool(
        re.search(r"试验依据标准.{0,500}6040[-—－]2019.{0,300}33047[.]1[-—－]2016", plain)
        and re.search(r"试验结论.{0,100}红外光谱.{0,80}热重", plain)
    )
    arsenic = bool(_ARSENIC_SPECIAL_EVIDENCE.search(plain)) and bool(
        "砷元素快检分析测试结果" in plain or "砷元素快检" in plain
    )

    # 图谱报告的附表可能包含砷/锑，图谱证据始终优先于砷专项证据。
    detected = "spectrum" if spectrum else "arsenic" if arsenic else None
    selected = bound if bound in {"arsenic", "spectrum"} else detected

    if selected and conventional:
        raise ValueError(
            "专项报告标题与常规型式试验内容同时出现，已停止自动审核，请人工确认报告类别和文件范围"
        )
    if bound == "type_test" and detected and not conventional:
        raise ValueError(
            "入口类别为型式试验，但PDF正文呈现专项报告特征，已停止自动审核，请人工确认报告类别"
        )
    if bound in {"arsenic", "spectrum"} and detected and bound != detected:
        raise ValueError(
            "入口类别与PDF专项类别不一致，已停止自动审核，请人工确认报告类别"
        )
    if bound is None and detected is None and not (
        conventional or _TYPE_TEST_TITLE_EVIDENCE.search(plain)
    ):
        raise ValueError(
            "报告类别证据不足，已停止自动审核，请人工确认是型式试验、砷元素快检还是图谱检查"
        )
    return selected


_SPECIAL_FORBIDDEN_TERMS = re.compile(
    r"导体|电阻|电压|结构(?:检查|尺寸)?|厚度|外径|老化|拉力|断裂伸长|失重|"
    r"高温压力|低温|热冲击|热延伸|非污染|燃烧|阻燃|曲挠|浸矿物油|耐臭氧|标志|线芯"
)
_SPECIAL_ALLOWED_TERMS = {
    "arsenic": re.compile(r"砷|元素|限用物质|材料类型"),
    "spectrum": re.compile(r"红外|光谱|热重|砷|锑|元素|材料(?:一致性)?|图谱|限用物质|对照样品"),
}


def _enforce_special_route_whitelist(
    result: dict[str, Any], category: str
) -> dict[str, Any]:
    """模型即使越界也只允许专项检测条目进入最终结论。"""
    allowed = _SPECIAL_ALLOWED_TERMS[category]
    removed: list[dict[str, str]] = []
    for sample in result.get("samples") or []:
        for field in ("checks", "items"):
            kept = []
            for entry in sample.get(field) or []:
                scope = " ".join(str(entry.get(key) or "") for key in (
                    "category", "item", "reported", "required", "basis", "standard",
                    "note", "should_be", "review_action",
                ))
                if allowed.search(scope) and not _SPECIAL_FORBIDDEN_TERMS.search(scope):
                    kept.append(entry)
                else:
                    removed.append({
                        "sample": str(sample.get("model") or ""),
                        "field": field,
                        "item": str(entry.get("item") or "")[:120],
                    })
            sample[field] = kept

    # 报告级通用逻辑只保存在_review_meta，绝不能改变专项结论。
    result["report_checks"] = []
    result["report_items"] = []
    result["remarks"] = [
        "本报告已在OCR后进入独立专项审核路线，未加载产品型式试验知识库或必审矩阵。"
    ]
    result["product_unit"] = "砷元素快检" if category == "arsenic" else "图谱检查"
    result["product_desc"] = (
        "砷元素快检专项报告" if category == "arsenic" else "图谱/材料分析专项报告"
    )
    validation = result.setdefault("_deterministic_validation", {})
    validation["special_route_whitelist"] = {
        "category": category,
        "removed_count": len(removed),
        "removed_entries": removed,
    }
    return result


def _special_batch_cache_path(
    ocr_path: str, task_id: int | None, category: str, batch_number: int
) -> Path | None:
    if task_id is None:
        return None
    return (
        Path(ocr_path) / f"special_review_task_{task_id}" / category
        / f"batch_{batch_number:03d}.json"
    )


def _special_report_label(category: str) -> str:
    return "砷元素快检" if category == "arsenic" else "图谱检查"


def _review_special_report(
    *,
    category: str,
    report_id: int,
    text: str,
    metadata: dict[str, Any],
    ocr_identity_meta: dict[str, Any],
    ocr_path: str,
    ai_settings: dict[str, Any],
    review_token_budget: int,
    progress: ProgressCallback,
    task_id: int | None,
    parent_report_id: int | None,
    parent_review: dict[str, Any] | None,
) -> dict[str, Any]:
    """专项报告独立路线：脱敏、通用逻辑元数据、专项模型审核。"""
    del report_id  # 路线已在调用前绑定，避免专项函数再次触碰数据库。
    prompt = (
        prompts.ARSENIC_SPECIAL_REVIEW_PROMPT
        if category == "arsenic" else prompts.SPECTRUM_SPECIAL_REVIEW_PROMPT
    )
    chunks = _split_report_text(text)
    request_timeout = max(30.0, min(600.0, float(ai_settings.get("timeout_seconds", 120))))
    privacy_batches: list[dict[str, Any]] = []
    content_batches: list[dict[str, Any]] = []
    guard_batches: list[dict[str, Any]] = []
    partial_results: list[dict[str, Any]] = []
    route_fingerprint = hashlib.sha256(
        (f"special-report-route-v1-{category}\n" + prompt).encode("utf-8")
    ).hexdigest()

    from backend.app.privacy_preflight import (
        prepare_stable_privacy_preflight,
        save_preflight_artifact,
    )
    from backend.app.outbound_guard import FinalOutboundGuard, GuardedLLMClient
    from backend.app.external_content_review import prepare_external_content_review

    if progress:
        progress("reviewing", 0, len(chunks), f"正在进行{_special_report_label(category)}专项审核")
    for index, chunk in enumerate(chunks, start=1):
        privacy_settings = dict(ai_settings, intranet_content_review_enabled=False)
        preflight = prepare_stable_privacy_preflight(
            chunk, metadata, privacy_settings,
            ocr_path=ocr_path, task_id=task_id, batch_number=index,
            rules_fingerprint=route_fingerprint,
        )
        if preflight["mode"] != "disabled":
            save_preflight_artifact(ocr_path, task_id, index, preflight)
        privacy_batches.append({
            key: preflight.get(key) for key in (
                "mode", "model_status", "replacement_counts", "logic_checks",
                "outbound_findings", "outbound_blocked", "cache_reused",
            )
        })
        external_text = str(preflight["external_text"])
        client = LLMClient(timeout_seconds=request_timeout, ai_config=ai_settings)
        guard = FinalOutboundGuard(chunk, metadata, ocr_path, task_id, index)
        guarded_client = GuardedLLMClient(client, guard)
        with _REVIEW_AI_SEMAPHORE:
            content_review = prepare_external_content_review(
                external_text, ai_settings, guarded_client,
                ocr_path=ocr_path, task_id=task_id, batch_number=index,
            )
        content_batches.append(content_review)
        fingerprint = hashlib.sha256(json.dumps({
            "version": f"special-report-route-v1-{category}",
            "category": category,
            "prompt": prompt,
            "text": external_text,
            "provider": ai_settings.get("provider"),
            "base_url": ai_settings.get("base_url"),
            "model": ai_settings.get("review_model"),
            "api_key_hash": hashlib.sha256(
                str(ai_settings.get("api_key") or "").encode("utf-8")
            ).hexdigest(),
            "thinking": ai_settings.get("thinking_enabled"),
            "reasoning_effort": ai_settings.get("reasoning_effort"),
            "temperature": ai_settings.get("temperature"),
            "max_tokens": review_token_budget,
            "timeout_seconds": request_timeout,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        cache_path = _special_batch_cache_path(ocr_path, task_id, category, index)
        parsed = _load_batch_cache(cache_path, fingerprint)
        if parsed is None:
            with _REVIEW_AI_SEMAPHORE:
                raw = guarded_client.chat(
                    prompt,
                    "请仅按专项范围审核以下已脱敏报告内容，并严格输出JSON：\n\n" + external_text,
                    model=ai_settings.get("review_model"),
                    max_retries=2,
                    max_tokens=review_token_budget,
                    json_mode=True,
                )
                parsed = _parse_review_json(
                    guarded_client, raw,
                    model=ai_settings.get("review_model"),
                    max_tokens=review_token_budget,
                )
            _save_batch_cache(cache_path, fingerprint, parsed)
        partial_results.append(parsed)
        guard_batches.append(guard.summary())
        if progress:
            progress("reviewing", index, len(chunks), f"专项审核 {index}/{len(chunks)} 完成")

    result = partial_results[0] if len(partial_results) == 1 else _merge_review_results(partial_results)
    if category == "spectrum":
        result = _enforce_materials_special_scope(result)
    else:
        result = _enforce_special_report_scope(result, text)
    result = _enforce_special_route_whitelist(result, category)
    for field in ("application_no", "report_no", "company"):
        if metadata.get(field):
            result[field] = metadata[field]
    result = _normalize_review_result(result)
    if parent_report_id and parent_review:
        result["revision_comparison"] = _build_revision_comparison(
            parent_report_id, parent_review, result
        )

    from backend.app.external_content_review import merge_checks
    privacy_logic_checks = []
    for batch in privacy_batches:
        privacy_logic_checks.extend(batch.get("logic_checks") or [])
    result["_review_meta"] = {
        "report_route": category,
        "report_route_label": _special_report_label(category),
        "type_test_rulebase_loaded": False,
        "ocr_identity_recovery": ocr_identity_meta,
        "privacy_preflight": {
            "mode": "enforce" if privacy_batches and all(
                item.get("mode") == "enforce" for item in privacy_batches
            ) else ai_settings.get("privacy_preflight_mode", "disabled"),
            "batch_count": len(privacy_batches),
            "company_replacements": sum(int(
                (item.get("replacement_counts") or {}).get("company", 0)
            ) for item in privacy_batches),
            "application_replacements": sum(int(
                (item.get("replacement_counts") or {}).get("application_no", 0)
            ) for item in privacy_batches),
            "outbound_findings": sum(len(item.get("outbound_findings") or []) for item in privacy_batches),
            "logic_checks": privacy_logic_checks,
        },
        "external_content_review": {
            "provider": ai_settings.get("provider"),
            "model": ai_settings.get("review_model"),
            "batch_count": len(content_batches),
            "successful_batches": sum(item.get("status") == "ok" for item in content_batches),
            "failed_batches": sum(str(item.get("status", "")).startswith("error:") for item in content_batches),
            "logic_checks": merge_checks(content_batches),
        },
        "final_outbound_guard": {
            "guard_version": "final-outbound-guard-v1",
            "batch_count": len(guard_batches),
            "calls": sum(int(item.get("calls") or 0) for item in guard_batches),
            "allowed": sum(int(item.get("allowed") or 0) for item in guard_batches),
            "blocked": sum(int(item.get("blocked") or 0) for item in guard_batches),
            "finding_types": sorted({
                finding for item in guard_batches for finding in item.get("finding_types") or []
            }),
        },
    }
    if progress:
        progress("finalizing", 1, 1, "专项审核结果整理完成")
    return result


def _recover_sheath_aging_from_combined_checks(result: dict[str, Any]) -> dict[str, Any]:
    """把模型合并进绝缘项的护套老化后数据恢复为独立必审核对项。"""
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        checks = sample.get("checks") or []
        has_sheath_before = any(
            "护套" in f"{check.get('category', '')}{check.get('item', '')}"
            and "老化前" in f"{check.get('category', '')}{check.get('item', '')}" for check in checks
        )
        has_sheath_after = any(
            "护套" in f"{check.get('category', '')}{check.get('item', '')}"
            and "老化后" in f"{check.get('category', '')}{check.get('item', '')}" for check in checks
        )
        if not has_sheath_before or has_sheath_after:
            continue
        missing_item = next((item for item in (sample.get("items") or [])
                             if "护套老化后拉力试验审核覆盖缺失" in str(item.get("item", ""))), None)
        if not missing_item:
            continue
        source_check = next((
            check for check in checks
            if "老化后" in f"{check.get('category', '')}{check.get('item', '')}"
            and "护套" not in f"{check.get('category', '')}{check.get('item', '')}"
            and len(re.findall(r"老化后抗张强度[-—]?中间值", str(check.get("reported", "")))) >= 2
        ), None)
        if not source_check:
            continue
        segments = [part.strip() for part in re.split(r"[；;]", str(source_check.get("reported", ""))) if part.strip()]
        starts = [index for index, part in enumerate(segments) if "老化后抗张强度" in part]
        if len(starts) < 2:
            continue
        sheath_text = "；".join(segments[starts[-1]:])
        strengths = [float(value) for value in re.findall(r"老化后抗张强度[-—]?中间值[：:]\s*([0-9]+(?:\.[0-9]+)?)", sheath_text)]
        elongations = [float(value) for value in re.findall(r"老化后断裂伸长率[-—]?中间值[：:]\s*([0-9]+(?:\.[0-9]+)?)", sheath_text)]
        changes = [float(value) for value in re.findall(r"变化率[：:]\s*([+\-−]?\d+(?:\.\d+)?)", sheath_text)]
        if not strengths or not elongations or len(changes) < 2:
            continue
        passed = min(strengths) >= 10 and min(elongations) >= 150 and all(abs(value) <= 20 for value in changes) and source_check.get("verdict") == "pass"
        basis = str(missing_item.get("standard") or source_check.get("basis") or "对应产品标准护套老化后拉力项目")
        checks.append({
            "category": "护套机械性能", "item": "护套老化后拉力试验", "reported": sheath_text,
            "required": "老化后抗张强度≥10.0N/mm²、断裂伸长率≥150%，变化率均在±20%内",
            "verdict": "pass" if passed else "manual_review", "basis": basis,
            "note": "程序从同一合并核对项的第二组老化后数据恢复护套项目",
        })
        if passed:
            sample["items"] = [item for item in (sample.get("items") or []) if item is not missing_item]
        records.append({
            "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
            "item": "护套老化后拉力试验", "reported": sheath_text,
            "passed": passed, "source": "combined_aging_check_second_group",
        })
    if records:
        result.setdefault("_deterministic_validation", {})["recovered_required_checks"] = records
    return result


def _actionable_issues(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for item in (result or {}).get("report_items") or []:
        if item.get("severity") in ("must_fix", "suggestion") and item.get("action_required") is not False:
            issues.append({**item, "sample": "全报告"})
    for sample in (result or {}).get("samples") or []:
        sample_name = " ".join(str(sample.get(k, "")) for k in ("model", "voltage", "spec")).strip()
        for item in sample.get("items") or []:
            if item.get("severity") in ("must_fix", "suggestion") and item.get("action_required") is not False:
                issues.append({"sample": sample_name, **item})
    return issues


def _revision_context(parent_review: dict[str, Any] | None) -> str:
    """给模型的上一版问题清单；只给必要字段，避免扩大每批输出。"""
    issues = _actionable_issues(parent_review)
    if not issues:
        return ""
    compact = [{
        "sample": issue.get("sample", ""),
        "item": issue.get("item", ""),
        "old_value": issue.get("reported", ""),
        "required": issue.get("review_action") or issue.get("should_be", ""),
    } for issue in issues]
    return (
        "\n\n【上一版本待更正问题】\n" + json.dumps(compact, ensure_ascii=False) +
        "\n审核当前版本时，只处理属于本批次样品的问题。每个相关旧问题都必须在 checks 中用相同项目名给出新报告证据和 verdict；"
        "若仍不符合，同时写入 items。不得因为只复核旧问题而省略当前版本新出现的问题。"
    )


def _match_text(left: Any, right: Any) -> float:
    def normalize(value: Any) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _build_revision_comparison(
    parent_report_id: int,
    parent_review: dict[str, Any],
    current_review: dict[str, Any],
) -> dict[str, Any]:
    """确定性比较两版结果：旧问题是否解决，以及新版是否出现新增问题。"""
    old_issues = _actionable_issues(parent_review)
    current_issues = _actionable_issues(current_review)
    checks: list[dict[str, Any]] = []
    checks.extend({**check, "sample": "全报告"} for check in current_review.get("report_checks") or [])
    for sample in current_review.get("samples") or []:
        sample_name = " ".join(str(sample.get(k, "")) for k in ("model", "voltage", "spec")).strip()
        for check in sample.get("checks") or []:
            checks.append({"sample": sample_name, **check})

    matched_issue_indexes: set[int] = set()
    comparison_items: list[dict[str, Any]] = []
    counts = {"resolved": 0, "unresolved": 0, "manual_review": 0}
    for old in old_issues:
        def score(candidate: dict[str, Any], check: bool = False) -> float:
            if (old.get("scope") == "report") != (candidate.get("scope") == "report"):
                return 0.0
            if check and old.get("rule_code") == "REPORT_CHRONOLOGY_V1":
                required_pairs = set(old.get("chronology_evaluated_pairs") or [])
                if (candidate.get("rule_code") != old["rule_code"] or not required_pairs
                    or not required_pairs <= set(candidate.get("chronology_evaluated_pairs") or [])):
                    return 0.0
            candidate_item = f"{candidate.get('category', '')}{candidate.get('item', '')}" if check else candidate.get("item", "")
            item_score = _match_text(old.get("item"), candidate_item)
            sample_score = _match_text(old.get("sample"), candidate.get("sample"))
            return item_score * 0.8 + sample_score * 0.2

        issue_match = max(enumerate(current_issues), key=lambda pair: score(pair[1]), default=None)
        check_match = max(checks, key=lambda item: score(item, True), default=None)
        issue_score = score(issue_match[1]) if issue_match else 0.0
        check_score = score(check_match, True) if check_match else 0.0

        status = "manual_review"
        note = "新版审核结果中未找到足够明确的对应证据，请人工对照两版 PDF"
        new_evidence = "未定位到对应项目"
        if issue_match and issue_score >= 0.48:
            matched_issue_indexes.add(issue_match[0])
            current = issue_match[1]
            status = "unresolved"
            note = "新版仍存在对应问题"
            new_evidence = f"{current.get('reported', '')}；{current.get('review_action') or current.get('should_be', '')}"
        elif check_match and check_score >= 0.45:
            verdict = str(check_match.get("verdict", "")).lower()
            new_evidence = "；".join(filter(None, [
                str(check_match.get("reported", "")), str(check_match.get("required", "")),
                str(check_match.get("basis", "")), str(check_match.get("note", "")),
            ]))
            if verdict in ("pass", "not_applicable"):
                status, note = "resolved", "已更正"
            elif verdict == "fail":
                status, note = "unresolved", "新版仍不符合要求"
            else:
                status, note = "manual_review", "新版证据仍需人工确认"
        counts[status] += 1
        comparison_items.append({
            "sample": old.get("sample", ""),
            "original_item": old.get("item", ""),
            "original_reported": old.get("reported", ""),
            "required_correction": old.get("review_action") or old.get("should_be", ""),
            "new_evidence": new_evidence,
            "status": status,
            "note": note,
        })

    new_issues = [issue for index, issue in enumerate(current_issues) if index not in matched_issue_indexes]
    if counts["unresolved"]:
        overall_status = "部分更正"
    elif counts["manual_review"]:
        overall_status = "待人工复核"
    elif new_issues:
        overall_status = "原问题已解决，但发现新问题"
    else:
        overall_status = "更正通过"
    return {
        "parent_report_id": parent_report_id,
        "original_issue_count": len(old_issues),
        "resolved_count": counts["resolved"],
        "unresolved_count": counts["unresolved"],
        "manual_review_count": counts["manual_review"],
        "new_issue_count": len(new_issues),
        "status": overall_status,
        "items": comparison_items,
        "new_issues": new_issues,
    }


def _detect_standard_family(text: str, metadata: dict[str, Any] | None = None) -> str | None:
    """依据报告自身型号/产品单元锁定PVC或橡皮标准族，不让模型自行跨族选择。"""
    metadata = metadata or {}
    source = " ".join([
        str(metadata.get("product_unit", "")), str(metadata.get("product_desc", "")), text[:50000]
    ]).upper()
    pvc_score = 0
    rubber_score = 0
    for marker, weight in (("60227", 8), ("GB/T 5023", 7), ("聚氯乙烯", 7), ("(RVV)", 5), ("IEC 52", 4), ("IEC 53", 4)):
        if marker.upper() in source:
            pvc_score += weight
    for marker, weight in (("60245", 8), ("GB/T 5013", 7), ("橡套", 7), ("橡皮", 7), ("YZW", 5), ("YCW", 5), (" YZ ", 4)):
        if marker.upper() in source:
            rubber_score += weight
    if pvc_score == rubber_score == 0:
        return None
    return "pvc" if pvc_score > rubber_score else "rubber"


def _family_lock_context(standard_family: str | None) -> str:
    if standard_family == "pvc":
        return """

【标准体系硬锁定：PVC】
本报告已由程序识别为60227/GB/T 5023或JB/T 8734聚氯乙烯电缆体系。禁止引用GB/T 5013、JB/T 8735、IE/SE材料或YZ/YZW橡皮电缆参数。
线芯电压按对应产品表格的规定/标称绝缘厚度分档，不按报告实测平均厚度改档；先读取5023.5表9、8734.3表3～表5等规格表。
产品表明确给出固定试验电压时优先采用产品表：JB/T 8734.2-2016表8项次1.3规定BVV/BLVV、BVVB/BLVVB绝缘线芯电压为2000V，不得按0.6mm通用分档改成1500V。
曲挠线芯载流按GB/T 5023.2第3.1.4条：2/3芯全部线芯1A/mm²；4/5芯可三根线芯1A/mm²，或全部线芯√3/n A/mm²；上述负载电流允许+10%/-0%；五芯以上不进行载流试验，但未载流线芯应加载信号电流。报告仅写“施加电流”时不得把信号电流推断成负载电流。0.75mm²不得套用橡皮电缆表2的6A。
"""
    if standard_family == "rubber":
        return """

【标准体系硬锁定：橡皮】
本报告已由程序识别为60245/GB/T 5013或JB/T 8735橡皮电缆体系。禁止引用GB/T 5023、JB/T 8734的PVC材料和曲挠载流密度参数。
线芯电压按5013/8735产品表格的规定/标称绝缘厚度分档；YZ与YZW必须先区分，YZ没有对应规格直接行时不得套用YZW曲挠参数，标记需人工复核。
曲挠试验必须先判断适用怤5/5013或8735的规格边界：导体标称截面超过4mm²时不适用，判N正确，不得再要求补填曲挠参数。
"""
    return ""


def _spec_cores_area(sample: dict[str, Any]) -> tuple[int, float] | None:
    from backend.app.structural_facts import spec_facts
    spec = str(sample.get("spec", ""))
    voltage = re.search(r"\d{2,4}\s*/\s*\d{2,4}\s*V?", spec, re.I)
    if voltage:
        spec = spec[voltage.end():].strip()
    cores, area = spec_facts(spec)
    return (cores, area) if cores is not None and area is not None else None


def _reported_current(value: Any) -> float | None:
    text = str(value or "")
    match = re.search(r"电流\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*A\b", text, re.I)
    if not match:
        match = re.search(r"(?:^|[，,；;\s])([0-9]+(?:\.[0-9]+)?)\s*A\b", text, re.I)
    return float(match.group(1)) if match else None


def _pvc_current_requirement(cores: int, area: float) -> tuple[list[tuple[float, float]], str]:
    """Return permitted per-core current intervals, including +10%/-0%."""
    def interval(nominal: float) -> tuple[float, float]:
        return nominal, nominal * 1.10
    if cores <= 3:
        return [interval(area)], f"全部线芯按1A/mm²加载，每芯{area:g}A（允许+10%/-0%）"
    if cores <= 5:
        import math
        all_core = math.sqrt(3.0) / cores * area
        return [interval(area), interval(all_core)], (
            f"可选三根线芯按1A/mm²加载（每芯{area:g}A），或全部{cores}芯按√3/{cores}A/mm²加载（每芯{all_core:.3g}A），均允许+10%/-0%"
        )
    return [], "五芯以上不进行载流试验；未载流线芯应加载信号电流"


def _pvc_flex_current_verdict(
    cores: int,
    reported: float,
    valid_current_ranges: list[tuple[float, float]],
    *,
    explicit_load_current: bool,
) -> tuple[bool, str]:
    """Distinguish an explicitly loaded current from a template signal current."""
    if cores > 5:
        return (not explicit_load_current,
                "explicit_load_current" if explicit_load_current else "signal_current_not_contradicted")
    return (
        any(lower - 1e-9 <= reported <= upper + 1e-9
            for lower, upper in valid_current_ranges),
        "load_current",
    )


def _soft_cable_sample(sample: dict[str, Any]) -> bool:
    label = " ".join(str(sample.get(key, "")) for key in ("model", "spec")).upper()
    return any(marker in label for marker in (
        "RVV", "RVS", "RVB", "RVVP", "RVVY", "RVVYP", "SVR", "IEC 52", "IEC 53", "IEC 56", "IEC 57",
    ))


def _impact_dimension(sample: dict[str, Any]) -> tuple[float, bool, str] | None:
    """读取成品尺寸：扁形电缆按短轴，圆形按实测平均外径。"""
    for check in sample.get("checks") or []:
        label = f"{check.get('category', '')}{check.get('item', '')}"
        if not any(marker in label for marker in ("结构", "尺寸", "外径", "外形")):
            continue
        reported = str(check.get("reported", ""))
        # 芯数×截面（如41×0.75mm²）不是扁形外形尺寸；仅在原文明确写出外形/宽厚时解析二元尺寸。
        pair = None
        if re.search(r"外形|宽\s*[×xX*]\s*厚|短轴|长轴", reported):
            pair = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)(?!\d)", reported)
        if pair:
            parsed_spec = _spec_cores_area(sample)
            first, second = float(pair.group(1)), float(pair.group(2))
            # 模型有时自行给芯数×截面加上“外形”二字；数值与规格完全相同时仍不得当尺寸。
            if parsed_spec and abs(first - parsed_spec[0]) < 0.001 and abs(second - parsed_spec[1]) < 0.001:
                pair = None
        if pair:
            first, second = float(pair.group(1)), float(pair.group(2))
            return min(first, second), True, f"扁形短轴{min(first, second):g}mm（外形{first:g}×{second:g}mm）"
        # 「最大11.4mm实测9.9mm」并列时只采实测值；限值不能代替实测外径。
        measured = re.search(
            r"(?:平均外径|外径|外形尺寸平均值)[^；;。]*?实测\D{0,8}(\d+(?:\.\d+)?)(?:\s*[/／]\s*(\d+(?:\.\d+)?))?\s*mm",
            reported)
        if measured:
            value = max(float(g) for g in measured.groups() if g)
            return value, False, f"圆形实测外径{value:g}mm"
        diameter = re.search(r"(?:平均外径|外径)\D{0,16}(\d+(?:\.\d+)?)\s*mm", reported)
        if diameter and not re.search(r"(?:平均外径|外径)\D{0,16}(?:最大|最小)", reported):
            value = float(diameter.group(1))
            return value, False, f"圆形实测外径{value:g}mm"
        # 只有最大/最小限值而未记录实测值时，不得借限值充当 d。
        shape = re.search(r"外形尺寸平均值\D{0,12}(\d+(?:\.\d+)?)\s*mm", reported)
        if shape:
            value = float(shape.group(1))
            return value, False, f"圆形实测外径{value:g}mm"
    return None


def _soft_hammer_mass(diameter: float) -> int:
    for upper, mass in ((6.0, 100), (10.0, 200), (15.0, 300), (25.0, 400), (35.0, 500)):
        if diameter <= upper:
            return mass
    return 600


def _fixed_hammer_mass(diameter: float) -> int:
    """GB/T 2951.14表2固定敷设电缆的低温冲击落锤档位。"""
    for upper, mass in (
        (4.0, 100), (6.0, 200), (9.0, 300), (12.5, 400),
        (20.0, 500), (30.0, 750), (50.0, 1000), (75.0, 1250),
    ):
        if diameter <= upper:
            return mass
    return 1500


def _fixed_cable_sample(sample: dict[str, Any]) -> bool:
    """识别已确认按GB/T 2951.14表2判定的PVC固定敷设电缆。"""
    model = re.sub(r"[\s()（）_-]", "", str(sample.get("model") or "").upper())
    if "60227IEC01" in model:
        return True
    return bool(re.fullmatch(r"(?:Z[ABCD])?(?:BV|BLV|BVV|BLVV|BVVB|BLVVB)", model))


def _source_impact_dimension(page_text: str, sample: dict[str, Any]) -> tuple[float, bool, str] | None:
    """仅从结构表的“外径/外形尺寸”同一行读取实测值。

    不允许跨行搜索，否则“标志间距200mm”会被误当成成品外径；
    也不允许把“2×1.5”这类芯数×标称截面当成扁形外形。
    """
    parsed_spec = _spec_cores_area(sample)
    cell_sets: list[list[str]] = []
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", page_text, re.I | re.S):
        if re.search(r"外径-平均外径|外形尺寸-平均外径", row):
            cell_sets.append([
            re.sub(r"\s+", "", re.sub(r"<[^>]+>", " ", cell))
            for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)
            ])
    for line in page_text.splitlines():
        if "|" in line and re.search(r"外径-平均外径|外形尺寸-平均外径", line):
            cell_sets.append([re.sub(r"\s+", "", cell) for cell in line.split("|") if cell.strip()])

    for cells in cell_sets:
        verdict_positions = [idx for idx, value in enumerate(cells) if value.upper() in {"P", "F", "N"}]
        end = verdict_positions[-1] if verdict_positions else len(cells)
        candidates = cells[1:end]

        pairs: list[tuple[float, float]] = []
        for value in candidates:
            match = re.fullmatch(r"(?:最大|最小)?(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)", value)
            if not match:
                continue
            first, second = float(match.group(1)), float(match.group(2))
            if parsed_spec and abs(first - parsed_spec[0]) < 0.001 and abs(second - parsed_spec[1]) < 0.001:
                continue
            pairs.append((first, second))
        if pairs:
            first, second = pairs[-1]  # 同行中标准要求在前，实测值在后
            short_axis = min(first, second)
            return short_axis, True, f"扁形原页实测短轴{short_axis:g}mm（外形{first:g}×{second:g}mm）"

        scalars: list[float] = []
        for value in candidates:
            match = re.fullmatch(r"(?:最大|最小)?(\d+(?:\.\d+)?)", value)
            if match:
                scalars.append(float(match.group(1)))
        if scalars:
            actual = scalars[-1]  # 同行中标准限值在前，实测值在后
            if 0 < actual <= 100:
                return actual, False, f"圆形原页实测外径{actual:g}mm"
    return None


def _reported_hammer_mass(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"落锤(?:重量)?\D{0,8}(\d+)\s*g", text, re.I)
    # 低温冲击核对项的标签已限定试验类型，报告值常简写成
    # “低温冲击(-15℃,300g)”而不重复“落锤”二字。
    if not match:
        match = re.search(r"(?<!\d)(\d+)\s*g\b", text, re.I)
    return int(match.group(1)) if match else None


def _suppress_voltage_items_contradicted_by_pass_checks(result: dict[str, Any]) -> dict[str, Any]:
    """拦截同一输出中把0.6mm边界同时判对又判错的矛盾项。

    仅当同一样品的通过项明确同时写出“标称/规定厚度0.6mm”、
    “1500V”和“正确/符合”时，才移除要求改成2000V的问题项。
    """
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        model = re.sub(r"[\s()（）]", "", str(sample.get("model", "")).upper())
        parsed_spec = _spec_cores_area(sample)
        sample_evidence = " ".join(
            " ".join(str(check.get(field, "")) for field in (
                "category", "item", "reported", "required", "note", "basis"
            ))
            for check in sample.get("checks") or []
        ) + " " + " ".join(
            " ".join(str(item.get(field, "")) for field in (
                "item", "reported", "should_be", "review_action", "standard"
            ))
            for item in sample.get("items") or []
        )
        jbt_87342_fixed_2000 = (
            model in {"BVV", "BLVV", "BVVB", "BLVVB"}
            and bool(re.search(r"JB\s*/?\s*T\s*8734\.2", sample_evidence, re.I))
        )
        if jbt_87342_fixed_2000:
            for check in sample.get("checks") or []:
                check_text = " ".join(str(check.get(field, "")) for field in (
                    "category", "item", "reported", "required", "note", "basis"
                ))
                wrong_1500 = (
                    "绝缘线芯" in check_text
                    and bool(re.search(r"2000\s*V", str(check.get("reported", "")), re.I))
                    and bool(re.search(r"1500\s*V", f"{check.get('required', '')} {check.get('note', '')}", re.I))
                )
                if wrong_1500:
                    check["required"] = "JB/T 8734.2-2016表8项次1.3：绝缘线芯2000V、5min、不击穿"
                    check["verdict"] = "pass"
                    check["basis"] = "JB/T 8734.2-2016表8项次1.3；GB/T 5023.2-2008 2.3"
                    check["note"] = "产品表已直接规定2000V，优先于通用厚度分档；报告2000V正确"
            fixed_kept: list[dict[str, Any]] = []
            for item in sample.get("items") or []:
                item_text = " ".join(str(item.get(field, "")) for field in (
                    "item", "reported", "should_be", "review_action", "standard"
                ))
                contradicted = (
                    "绝缘线芯" in item_text
                    and bool(re.search(r"2000\s*V", str(item.get("reported", "")), re.I))
                    and bool(re.search(r"1500\s*V", f"{item.get('should_be', '')} {item.get('review_action', '')}", re.I))
                    and bool(re.search(r"8734\.2", item_text, re.I))
                )
                if contradicted:
                    records.append({
                        "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                        "reported": item.get("reported", ""),
                        "suppressed_should_be": item.get("should_be", ""),
                        "reason": "JB/T 8734.2-2016表8项次1.3已直接规定BVV/BVVB绝缘线芯2000V",
                    })
                else:
                    fixed_kept.append(item)
            sample["items"] = fixed_kept

        pass_text = " ".join(
            " ".join(str(check.get(field, "")) for field in ("category", "item", "reported", "required", "note", "basis"))
            for check in sample.get("checks") or []
            if str(check.get("verdict", "")).lower() == "pass"
            and "绝缘线芯电压" in f"{check.get('category', '')}{check.get('item', '')}"
        )
        located_pass_text = " ".join(
            " ".join(str(check.get(field, "")) for field in ("category", "item", "reported", "required", "note", "basis"))
            for check in sample.get("checks") or []
            if str(check.get("verdict", "")).lower() == "pass"
            and check.get("evidence_status") == "located"
            and "绝缘线芯电压" in f"{check.get('category', '')}{check.get('item', '')}"
        )
        confirmed_voltages = set(re.findall(r"(1500|2000|2500)\s*V", located_pass_text, re.I))
        if confirmed_voltages:
            voltage_kept: list[dict[str, Any]] = []
            for item in sample.get("items") or []:
                item_text = " ".join(str(item.get(field, "")) for field in (
                    "item", "reported", "should_be", "review_action", "standard"
                ))
                reported_voltage = re.search(r"(1500|2000|2500)\s*V", str(item.get("reported") or ""), re.I)
                requested_voltages = set(re.findall(
                    r"(1500|2000|2500)\s*V",
                    f"{item.get('should_be', '')} {item.get('review_action', '')}",
                    re.I,
                ))
                contradicted_by_pass = (
                    "绝缘线芯电压" in item_text
                    and reported_voltage is not None
                    and reported_voltage.group(1) in confirmed_voltages
                    and bool(requested_voltages - {reported_voltage.group(1)})
                )
                if contradicted_by_pass:
                    records.append({
                        "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                        "reported": item.get("reported", ""),
                        "suppressed_should_be": item.get("should_be", ""),
                        "reason": "同一样品已有结构化通过项明确确认该绝缘线芯试验电压",
                    })
                    continue
                voltage_kept.append(item)
            sample["items"] = voltage_kept
        boundary_confirmed = (
            bool(re.search(r"(?:标称|规定)(?:绝缘)?厚度", pass_text))
            and bool(re.search(r"0\.6\s*mm", pass_text, re.I))
            and bool(re.search(r"1500\s*V", pass_text, re.I))
            and bool(re.search(r"正确|符合", pass_text))
        )
        if not boundary_confirmed:
            continue
        kept: list[dict[str, Any]] = []
        for item in sample.get("items") or []:
            item_text = " ".join(str(item.get(field, "")) for field in (
                "item", "reported", "should_be", "review_action", "standard"
            ))
            contradicted = (
                "绝缘线芯电压" in item_text
                and bool(re.search(r"1500\s*V", str(item.get("reported", "")), re.I))
                and bool(re.search(r"2000\s*V", f"{item.get('should_be', '')} {item.get('review_action', '')}", re.I))
            )
            if contradicted:
                records.append({
                    "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                    "reported": item.get("reported", ""),
                    "suppressed_should_be": item.get("should_be", ""),
                    "reason": "同一样品通过项已明确确认标称/规定厚度0.6mm对应1500V",
                })
            else:
                kept.append(item)
        sample["items"] = kept
    if records:
        result.setdefault("_deterministic_validation", {})["insulated_core_voltage_boundary"] = records
    return result


_JBT87342_LOWTEMP_REQUIREMENTS = {
    "BV": (("绝缘低温弯曲试验", ("低温弯曲", "低温卷绕")),),
    "BLV": (
        ("绝缘低温弯曲试验", ("低温弯曲", "低温卷绕")),
        ("绝缘低温拉伸试验", ("低温拉伸",)),
        ("成品低温冲击试验", ("低温冲击",)),
    ),
    "BVR": (
        ("绝缘低温弯曲试验", ("低温弯曲", "低温卷绕")),
        ("绝缘低温拉伸试验", ("低温拉伸",)),
    ),
    "BVV/BLVV": (
        ("绝缘低温弯曲试验", ("低温弯曲", "低温卷绕")),
        ("绝缘低温拉伸试验", ("低温拉伸",)),
        ("护套低温弯曲试验", ("护套低温弯曲", "护套低温卷绕")),
        ("护套低温拉伸试验", ("护套低温拉伸",)),
        ("成品低温冲击试验", ("低温冲击",)),
    ),
    "BVVB/BLVVB": (
        ("绝缘低温弯曲试验", ("低温弯曲", "低温卷绕")),
        ("护套低温弯曲试验", ("护套低温弯曲", "护套低温卷绕")),
        ("成品低温冲击试验", ("低温冲击",)),
    ),
}


def _jbt87342_model_group(sample: dict[str, Any]) -> str | None:
    """只在返回证据明确引用8734.2时识别表8型号，避免误套60227型号。"""
    model = re.sub(r"[\s()（）]", "", str(sample.get("model", "")).upper())
    evidence = " ".join(
        " ".join(str(check.get(field, "")) for field in (
            "category", "item", "reported", "required", "note", "basis"
        ))
        for check in sample.get("checks") or []
    ) + " " + " ".join(
        " ".join(str(item.get(field, "")) for field in (
            "item", "reported", "should_be", "standard", "review_action"
        ))
        for item in sample.get("items") or []
    )
    if not re.search(r"JB\s*/?\s*T\s*8734\.2", evidence, re.I):
        return None
    if "BVVB" in model or "BLVVB" in model:
        return "BVVB/BLVVB"
    if "BLVV" in model or model == "BVV":
        return "BVV/BLVV"
    for candidate in ("BVR", "BLV", "BV"):
        if model == candidate:
            return candidate
    return None


def _explicit_not_done(text: str, aliases: tuple[str, ...] = ()) -> bool:
    """只识别与当前项目名称绑定的明确N/不适用。

    同一check可合并“弯曲P；拉伸N”，必须按分号内的具体项目
    分别判断，不能因拉伸N把弯曲也判为未做。
    """
    markers = aliases or ("低温弯曲", "低温卷绕", "低温拉伸", "低温冲击")
    return any(
        re.search(
            rf"{re.escape(marker)}[^；。\n]{{0,40}}(?:判\s*N|不适用|未做|未进行)",
            text,
            re.I,
        )
        for marker in markers
    )


def _recover_component_lowtemp_checks(result: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Repair mixed P/N model rows only from uniquely bound component pages."""
    from backend.app.rulebase import source_sample_registry, source_group_for_sample, _vertical_component_lowtemp_pair
    registry = source_sample_registry(source_text)
    for sample in result.get('samples') or []:
        if _jbt87342_model_group(sample) != 'BVV/BLVV':
            continue
        group = source_group_for_sample(sample, registry=registry)
        if not group: continue
        evidence = {'绝缘':[], '护套':[]}
        for page in group.get('pages') or []:
            pair = _vertical_component_lowtemp_pair(str(page.get('text') or ''))
            if pair: evidence[pair['component']].append((page,pair))
        for check in sample.get('checks') or []:
            label = str(check.get('category') or '')+' '+str(check.get('item') or '')
            components = [c for c in evidence if c in label]
            methods = [m for m,pattern in (('bend',r'低温(?:弯曲|卷绕)'),('tensile',r'低温拉伸')) if re.search(pattern,label)]
            states = set(re.findall(r'(?<![A-Z])[PFN](?![A-Z])',str(check.get('reported') or '')))
            if len(components)!=1 or len(methods)!=1 or not {'P','N'}.issubset(states) or check.get('verdict')=='fail':
                continue
            candidates = evidence[components[0]]
            if len(candidates)!=1: continue
            page,pair=candidates[0]; method=methods[0]; fact=pair[method]
            old_name,old_reported=check.get('item'),check.get('reported')
            name=components[0]+('低温弯曲' if method=='bend' else '低温拉伸')
            pattern=r'低温(?:弯曲|卷绕)试验' if method=='bend' else r'低温拉伸试验'
            part=re.split(pattern,str(page.get('text') or ''),maxsplit=1)[-1]
            part=re.split(r'低温(?:弯曲|卷绕|拉伸|冲击)试验|注\s*[:：]',part,maxsplit=1)[0]
            conditions=[]
            for regex,unit in ((r'温度\s*([-+]?\d+(?:\.\d+)?)\s*℃','℃'),(r'时间\s*(\d+(?:\.\d+)?)\s*h','h')):
                match=re.search(regex,part)
                if match: conditions.append(match[1]+unit)
            reported='；'.join([fact['value'],*conditions,fact['report_verdict']])
            check.update(item=name,reported=reported,verdict=fact['verdict'],
                         source_pages=[int(page['page'])],source_excerpt=name+'试验 '+reported,
                         evidence_status='located',coverage_origin='component_lowtemp_source_recovery',
                         note='按同一样品明确部件原页恢复，未借用另一部件的P/N；试样选择与条件仍由后续规则审核')
            # Remove only the exact ambiguous-row manual item. Keep other
            # evidence, numeric failures and model must-fix claims untouched.
            sample['items']=[item for item in sample.get('items') or [] if not (
                item.get('item')==old_name and item.get('reported')==old_reported
                and item.get('severity')=='suggestion' and item.get('action_type')=='manual_review')]
    return result


def _enforce_jbt87342_lowtemp_coverage(
    result: dict[str, Any], source_text: str = "",
) -> dict[str, Any]:
    """按8734.2表8校验低温必做项，未覆盖时禁止把报告判为合格。"""
    source_registry: list[dict[str, Any]] = []
    if source_text:
        from backend.app.rulebase import (
            _lowtemp_heading_verdict,
            _plain_table_text,
            source_group_for_sample,
            source_sample_registry,
        )
        source_registry = source_sample_registry(source_text)
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        group = _jbt87342_model_group(sample)
        if not group:
            continue
        checks = sample.get("checks") or []
        sample.setdefault("items", [])
        for required_name, aliases in _JBT87342_LOWTEMP_REQUIREMENTS[group]:
            if group == "BVV/BLVV" and any(method in required_name for method in ("弯曲", "拉伸")):
                # 这一对是条件替代项目，不可先按两个无条件必做项将
                # 合法N写成失败，再由后续规则尝试救回。完整必审矩阵
                # 仍负责原页覆盖；本函数尾部按同部件方法证据处理选择。
                records.append({"model_group": group, "item": required_name,
                                "status": "conditional_pair_deferred_to_matrix"})
                continue
            related = [
                check for check in checks
                if any(alias in f"{check.get('category', '')}{check.get('item', '')}" for alias in aliases)
            ]
            if required_name.startswith("护套"):
                related = [
                    check for check in related
                    if "护套" in f"{check.get('category', '')}{check.get('item', '')}"
                ]
            elif required_name.startswith("绝缘"):
                related = [
                    check for check in related
                    if "护套" not in f"{check.get('category', '')}{check.get('item', '')}"
                    or "绝缘" in f"{check.get('category', '')}{check.get('item', '')}"
                ]

            existing = [
                item for item in sample["items"]
                if required_name in str(item.get("item", ""))
            ]
            if related:
                combined = " ".join(
                    " ".join(str(check.get(field, "")) for field in (
                        "item", "reported", "required", "note"
                    ))
                    for check in related
                )
                source_verdict = ""
                source_group = None
                if source_registry:
                    source_group = source_group_for_sample(sample, registry=source_registry)
                if source_group:
                    plain_source = " ".join(
                        _plain_table_text(str(page.get("text") or ""))
                        for page in source_group.get("pages") or []
                    )
                    heading_pattern = (
                        r"低温(?:弯曲|卷绕)试验"
                        if "弯曲" in required_name else
                        r"低温拉伸试验"
                        if "拉伸" in required_name else
                        r"低温冲击试验"
                    )
                    source_verdict = _lowtemp_heading_verdict(plain_source, heading_pattern)
                if source_verdict == "P" and not any(check.get('verdict')=='fail' for check in related):
                    for item in existing:
                        sample["items"].remove(item)
                    for check in related:
                        check["verdict"] = "pass"
                        check["note"] = "程序从当前样品原页恢复P评定，不采信AI误读的N/未进行"
                    records.append({
                        "model_group": group, "item": required_name,
                        "status": "source_pass_overrode_model_not_done",
                    })
                    continue
                if _explicit_not_done(combined, aliases) and not existing:
                    sample["items"].append({
                        "item": required_name,
                        "reported": "报告明确标为N/不适用或未进行",
                        "should_be": "JB/T 8734.2-2016表8规定该型号必须进行本项目",
                        "standard": "JB/T 8734.2-2016表8低温项目矩阵",
                        "severity": "must_fix",
                        "action_required": True,
                        "action_type": "correction",
                        "review_action": f"核对原PDF对应行；如确为N或未进行，应补做{required_name}并更正报告",
                    })
                    for check in related:
                        check["verdict"] = "fail"
                        check["note"] = "程序覆盖校验：8734.2表8规定该型号必须进行本项目，不能判N/不适用"
                    records.append({"model_group": group, "item": required_name, "status": "explicit_not_done"})
                else:
                    records.append({"model_group": group, "item": required_name, "status": "covered"})
                continue

            if not existing:
                sample["items"].append({
                    "item": f"{required_name}审核覆盖缺失",
                    "reported": "AI返回的结构化核对项未覆盖该必做项目",
                    "should_be": "必须回看原PDF确认该项目的试验条件、结果和P/F/N评定",
                    "standard": "JB/T 8734.2-2016表8低温项目矩阵",
                    "severity": "suggestion",
                    "action_required": True,
                    "action_type": "manual_review",
                    "review_action": f"人工核对{required_name}；确认报告是否已做、结果是否符合且评定是否正确",
                })
            records.append({"model_group": group, "item": required_name, "status": "missing"})
    if records:
        result.setdefault("_deterministic_validation", {})["jbt87342_lowtemp_coverage"] = records
        result = _accept_completed_jbt87342_lowtemp_alternative(result)
        for alternative in (result.get("_deterministic_validation") or {}).get("jbt87342_lowtemp_alternatives") or []:
            surface = str(alternative.get("surface") or "")
            omitted = str(alternative.get("not_applicable_method") or "")
            selected = str(alternative.get("selected_method") or "")
            for row in records:
                item = str(row.get("item") or "")
                if surface and surface not in item:
                    continue
                if alternative.get("status") == "conditional_evidence_missing":
                    row["status"] = "conditional_evidence_missing"
                    continue
                if omitted and omitted in item:
                    row["status"] = "conditional_not_applicable"
                    row["selected_method"] = selected
                elif selected and selected in item:
                    row["status"] = "covered"
    return result


def _accept_completed_jbt87342_lowtemp_alternative(result: dict[str, Any]) -> dict[str, Any]:
    """BVV/BLVV低温弯曲与拉伸按试样尺寸二选一，完成一项即覆盖该组。"""
    def _dash_only(value: str) -> bool:
        # 只把整格横线/斜线认作未实施，避免把-15℃、-3等有效负数误判为空值。
        return bool(re.fullmatch(r"\s*[-—－/]+\s*", value))

    def _source_method_pass(check: dict[str, Any], bend: bool) -> bool:
        """只读取当前方法所在片段，避免借用相邻低温项目的P评定。"""
        source = str(check.get("source_excerpt") or "")
        if not source:
            return False
        heading = r"低温(?:弯曲|卷绕)试验" if bend else r"低温拉伸试验"
        matched = re.search(
            rf"{heading}(.*?)(?=低温(?:弯曲|卷绕|拉伸|冲击)试验|$)",
            source,
            re.I | re.S,
        )
        if not matched:
            return False
        method_part = matched.group(1)
        has_p = bool(re.search(r"(?:^|[\s|；;])P(?:$|[\s|；;])", method_part, re.I))
        if bend:
            return has_p and "无裂纹" in method_part
        return has_p and bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|％|N/mm|MPa)", method_part, re.I))

    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        if _jbt87342_model_group(sample) != "BVV/BLVV":
            continue
        checks = sample.get("checks") or []
        for surface in ("绝缘", "护套"):
            bend_name = f"{surface}低温弯曲"
            tensile_name = f"{surface}低温拉伸"
            pair_checks = {
                bend_name: [
                    check for check in checks
                    if surface in f"{check.get('category', '')}{check.get('item', '')}"
                    and any(marker in f"{check.get('category', '')}{check.get('item', '')}"
                            for marker in ("低温弯曲", "低温卷绕"))
                ],
                tensile_name: [
                    check for check in checks
                    if surface in f"{check.get('category', '')}{check.get('item', '')}"
                    and "低温拉伸" in f"{check.get('category', '')}{check.get('item', '')}"
                ],
            }
            performed: list[str] = []
            performed_status: dict[str, str] = {}
            unperformed: list[str] = []
            for name, related in pair_checks.items():
                bend_method = "弯曲" in name
                method_markers = ("低温弯曲", "低温卷绕") if "弯曲" in name else ("低温拉伸",)
                target_parts: list[str] = []
                for check in related:
                    reported = str(check.get("reported") or "")
                    item_label = str(check.get("item") or "")
                    if any(marker in item_label for marker in ("弯曲", "卷绕")) and "拉伸" in item_label:
                        segments = re.split(r"[；;。\n]", reported)
                        target_parts.extend(
                            segment for segment in segments
                            if any(marker in segment for marker in method_markers)
                            and not (('护套' if surface == '绝缘' else '绝缘') in segment
                                     and surface not in segment)
                        )
                    else:
                        target_parts.append(reported)
                text_value = " ".join(target_parts)
                missing = _dash_only(text_value) or bool(re.search(
                    r"未报告|未提供|不适用|未做|未进行|(?:^|[^A-Z])N(?:$|[^A-Z])",
                    text_value,
                    re.I,
                ))
                source_pass = any(_source_method_pass(check, bend_method) for check in related)
                # 合并记录可能同时写“弯曲N；拉伸200%”，必须基于上面切出的
                # 当前方法片段判断，不能让另一方法的N污染本方法实测值。
                substantive_verdicts = [
                    str(check.get("verdict")) for check in related
                    if check.get("verdict") in {"pass", "fail"}
                ]
                if source_pass or (bool(text_value.strip()) and not missing and substantive_verdicts):
                    performed.append(name)
                    performed_status[name] = (
                        "fail" if "fail" in substantive_verdicts else "pass" if source_pass or "pass" in substantive_verdicts
                        else "fail"
                    )
                elif related:
                    unperformed.append(name)
            if not performed and len(unperformed) == 2:
                sample["items"] = [
                    item for item in (sample.get("items") or [])
                    if bend_name not in str(item.get("item") or "")
                    and tensile_name not in str(item.get("item") or "")
                ]
                sample["items"].append({
                    "item": f"{bend_name}/{tensile_name}适用性待复核",
                    "reported": "两个替代方法均未取得完成证据",
                    "should_be": "按GB/T 2951.14试样尺寸条件二选一，不能同时强制要求",
                    "standard": "JB/T 8734.2-2016表8；GB/T 2951.14-2008",
                    "severity": "suggestion", "action_required": True,
                    "action_type": "manual_review",
                    "review_action": "回看原PDF和试样尺寸，确认应选的低温方法已经完成",
                })
                for related in pair_checks.values():
                    for check in related:
                        check["verdict"] = "manual_review"
                records.append({
                    "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                    "surface": surface, "status": "conditional_evidence_missing",
                })
                continue
            if len(performed) != 1 or len(unperformed) != 1:
                continue
            selected, omitted = performed[0], unperformed[0]
            selected_passed = performed_status.get(selected) == "pass"
            if selected_passed:
                # 原页已证明同侧替代方法完成且P，清除这对方法此前产生的错误补做/人工项。
                sample["items"] = [
                    item for item in (sample.get("items") or [])
                    if not (
                        surface in str(item.get("item") or "")
                        and any(marker in str(item.get("item") or "")
                                for marker in ("低温弯曲", "低温卷绕", "低温拉伸"))
                    )
                ]
            else:
                sample["items"] = [
                    item for item in (sample.get("items") or [])
                    if omitted not in str(item.get("item") or "")
                ]
            for check in pair_checks[selected]:
                if selected_passed:
                    check["verdict"] = "pass"
                    check["required"] = f"按GB/T 2951.14试样尺寸选择；原页已完成{selected}且评定P"
                    check["basis"] = "JB/T 8734.2-2016表8；GB/T 2951.14-2008 8.1/8.3或8.2/8.4"
                    check["note"] = "程序复核：当前方法原页已有完成证据，不沿用先前的不适用或人工复核状态"
            for check in pair_checks[omitted]:
                combined_pair_check = any(check is selected_check for selected_check in pair_checks[selected])
                # 被省略的方法不适用，不能把同一组合行中已实施方法的
                # 真实失败一起改成N。
                if combined_pair_check:
                    check["verdict"] = "pass" if selected_passed else "fail"
                else:
                    check["verdict"] = "not_applicable"
                check["required"] = (
                    f"按GB/T 2951.14试样尺寸选择；合并记录中{selected}已完成，{omitted}不适用"
                    if combined_pair_check
                    else f"按GB/T 2951.14试样尺寸选择；已完成{selected}，{omitted}不适用"
                )
                check["basis"] = "JB/T 8734.2-2016表8；GB/T 2951.14-2008 8.1/8.3或8.2/8.4"
                check["note"] = "程序复核：低温弯曲与低温拉伸为替代方法，不同时强制要求"
            records.append({
                "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                "surface": surface, "selected_method": selected,
                "not_applicable_method": omitted, "status": "alternative_covered",
            })
    if records:
        result.setdefault("_deterministic_validation", {})["jbt87342_lowtemp_alternatives"] = records
    return result


def _enforce_pvc_single_core_lowtemp_selection(
    result: dict[str, Any], standard_family: str | None, source_text: str = "",
) -> dict[str, Any]:
    """按GB/T 2951.14确定单芯无护套线的低温弯曲/拉伸方法。"""
    if standard_family != "pvc":
        return result
    source_registry: list[dict[str, Any]] = []
    if source_text:
        from backend.app.rulebase import source_sample_registry
        source_registry = source_sample_registry(source_text)
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        model = re.sub(r"[\s()（）-]", "", str(sample.get("model", "")).upper())
        parsed = _spec_cores_area(sample)
        single_core_model = any(marker in model for marker in (
            "60227IEC01", "60227IEC02", "BVR", "BLV",
        )) and not any(marker in model for marker in ("BLVV", "BLVVB"))
        if not parsed or parsed[0] != 1 or not single_core_model:
            continue
        dimension = _impact_dimension(sample)
        if not dimension or dimension[1]:
            continue
        diameter, _, dimension_note = dimension
        selected = "低温弯曲" if diameter <= 12.5 else "低温拉伸"
        omitted = "低温拉伸" if diameter <= 12.5 else "低温弯曲"
        selected_checks = [check for check in sample.get("checks") or []
                           if selected in f"{check.get('category', '')}{check.get('item', '')}" and "护套" not in f"{check.get('category', '')}{check.get('item', '')}"]
        omitted_checks = [check for check in sample.get("checks") or []
                          if omitted in f"{check.get('category', '')}{check.get('item', '')}" and "护套" not in f"{check.get('category', '')}{check.get('item', '')}"]
        performed = any(check.get("verdict") in {"pass", "fail"} and not re.search(
            r"未报告|未提供|不适用|未做|未进行|(?:^|[^A-Z])N(?:$|[^A-Z])", str(check.get("reported") or ""), re.I
        ) for check in selected_checks)
        if not performed and source_registry:
            from backend.app.rulebase import _lowtemp_heading_verdict, _plain_table_text, source_group_for_sample
            source_group = source_group_for_sample(sample, registry=source_registry)
            plain_source = " ".join(
                _plain_table_text(str(page.get("text") or ""))
                for page in ((source_group or {}).get("pages") or [])
            )
            performed = _lowtemp_heading_verdict(
                plain_source,
                r"低温拉伸试验" if selected == "低温拉伸" else r"低温(?:弯曲|卷绕)试验",
            ) == "P"
        if not performed:
            continue
        sample["items"] = [item for item in (sample.get("items") or [])
                           if not (omitted in str(item.get("item") or "") and "护套" not in str(item.get("item") or ""))]
        for check in omitted_checks:
            check["verdict"] = "not_applicable"
            check["required"] = f"{dimension_note}，应做{selected}，{omitted}不适用"
            check["basis"] = "GB/T 2951.14-2008 8.1.1、8.3.1"
            check["note"] = f"程序按单芯无护套电缆实测外径选择；{selected}已有完成证据，{omitted}判N正确"
        records.append({
            "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
            "diameter_mm": diameter, "selected_method": selected,
            "not_applicable_method": omitted, "status": "selection_confirmed",
        })
    if records:
        result.setdefault("_deterministic_validation", {})["pvc_single_core_lowtemp_selection"] = records
    return result


def _obsolete_impact_table_failure(check: dict[str, Any], soft: bool, source_group: dict[str, Any] | None) -> bool:
    """Only a single wrong-table mass claim can be resolved by the mass rule."""
    if check.get('verdict') != 'fail' or not source_group:
        return False
    required = str(check.get('required') or '')
    evidence = ' '.join(str(check.get(k) or '') for k in ('item','reported','required','note'))
    wrong_table = '2' if soft else '3'
    if not re.search(r'2951\s*[.]\s*14(?:-2008)?\s*表\s*'+wrong_table,str(check.get('basis') or '')+required):
        return False
    if len(re.findall(r'\d+\s*g\b',required)) != 1 or re.search(r'[；;]|温度|时间|裂纹|开裂|未做|缺失|未提供',required):
        return False
    if re.search(r'有裂纹|开裂|温度|时间|缺失|未提供|未进行|(?:^|[^A-Z])F(?:$|[^A-Z])',evidence,re.I):
        return False
    if '无裂纹' not in str(check.get('reported') or ''):
        return False
    from backend.app.rulebase import _plain_table_text
    found = []
    for page in source_group.get('pages') or []:
        plain = _plain_table_text(str(page.get('text') or ''))
        for match in re.finditer(r'低温冲击试验(?!机|仪)',plain):
            part = re.split(r'注\s*[:：]|低温(?:弯曲|拉伸)|热冲击|燃烧试验',plain[match.end():],maxsplit=1)[0]
            # Explicit result cell, never just a requirements phrase.
            if re.search(r'有裂纹|开裂|\|\s*F(?:\s*\||$)',part): return False
            found.append(bool(re.search(r'\|\s*无裂纹\s*\|\s*P(?:\s*\||$)',part)))
    return bool(found) and all(found)


def _impact_source_failures(source_group: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Definite adverse observations, scoped to impact result/condition cells."""
    from backend.app.rulebase import _plain_table_text
    observations = []
    for page in (source_group or {}).get('pages') or []:
        plain = _plain_table_text(str(page.get('text') or ''))
        for heading in re.finditer(r'低温冲击试验(?![机仪])', plain):
            section = re.split(r'注\s*[:：]|低温(?:弯曲|拉伸)|热冲击|热稳定|燃烧|曲挠|成品电缆机械强度|--- Page',
                               plain[heading.end():], maxsplit=1)[0][:700]
            patterns = (
                ('zero_hammer_mass', r'落锤(?:质量|重量)?\s*[:：]?\s*(?:\|\s*)?0(?:\.0+)?\s*g\b'),
                # Require a whole result cell/line. Do not match requirements
                # such as 不得有裂纹 or a later, unrelated test's observation.
                ('cracked_result', r'(?:^|[|\n])\s*(?:有裂纹|出现裂纹|产生裂纹|开裂)\s*(?=[|\n]|$)'),
            )
            for kind, pattern in patterns:
                for match in re.finditer(pattern, section):
                    observations.append({'kind':kind, 'page':int(page.get('page') or 0),
                                         'excerpt':match[0].strip(' |\n')})
    return observations


def _impact_specimen_dimensions(source_group: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read explicitly labelled impact specimen d, never structure averages."""
    from backend.app.rulebase import _plain_table_text
    observations = []
    for page in (source_group or {}).get('pages') or []:
        plain = _plain_table_text(str(page.get('text') or ''))
        for heading in re.finditer(r'低温冲击试验(?![机仪])', plain):
            section = re.split(r'注\s*[:：]|热冲击|热稳定|燃烧|--- Page', plain[heading.end():],maxsplit=1)[0][:700]
            for match in re.finditer(r'(?:冲击)?试样(?:实测)?外径\s*(?:d\s*)?[:：]?\s*(\d+(?:\.\d+)?)\s*mm\b', section):
                observations.append({'diameter_mm':float(match[1]),'page':int(page['page']),
                                     'excerpt':match[0]})
    return observations


def _enforce_lowtemp_impact_mass(result: dict[str, Any], source_text: str = "") -> dict[str, Any]:
    """确定性复核软电缆低温冲击落锤，防止扁形长短轴混用。"""
    source_registry: list[dict[str, Any]] = []
    if source_text:
        from backend.app.rulebase import source_sample_registry
        source_registry = source_sample_registry(source_text)
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        soft_cable = _soft_cable_sample(sample)
        fixed_cable = _fixed_cable_sample(sample)
        if not soft_cable and not fixed_cable:
            continue
        if re.search(r'(?<![A-Z])RVS(?![A-Z])', str(sample.get('model') or '').upper()):
            # RVS impact dimensions must come from impact specimens, not the
            # structure table. The bound matrix source-condition path owns this
            # decision; never overwrite it using a generic cable diameter here.
            records.append({'sample': sample.get('model'),
                            'status': 'rvs_requires_specimen_bound_impact_conditions'})
            continue
        dimension = None
        dimension_source = "impact_specimen_source"
        from backend.app.rulebase import _plain_table_text, source_group_for_sample
        source_group = source_group_for_sample(sample, registry=source_registry)
        source_failures = _impact_source_failures(source_group)
        if source_failures:
            for check in sample.get('checks') or []:
                if '低温冲击' not in f"{check.get('category','')}{check.get('item','')}":
                    continue
                check.setdefault('impact_previous_model_claim', {
                    key:check.get(key) for key in ('reported','verdict','required','note')})
                check['impact_source_failures'] = source_failures
                check['source_pages'] = sorted({o['page'] for o in source_failures if o['page']>0})
                check['source_excerpt'] = '；'.join(o['excerpt'] for o in source_failures)
                check['reported'] = check['source_excerpt']
                check['verdict'] = 'fail'
                check['deterministic_review_action'] = '核实并整改原页低温冲击异常记录，重新评定；不得以P覆盖落锤0g或裂纹结果'
                check['note'] = '原页存在明确异常；试样尺寸缺失不能消除该异常'
            records.append({'sample':sample.get('model'),'status':'impact_source_explicit_failure',
                            'observations':source_failures})
            # A missing diameter cannot resolve these failures. Do not let the
            # model's mass-only correction path overwrite the source verdict.
            continue
        specimen_observations = _impact_specimen_dimensions(source_group)
        diameters = {o['diameter_mm'] for o in specimen_observations}
        if len(diameters)==1 and next(iter(diameters))>0:
            diameter = next(iter(diameters))
            dimension = (diameter, False, f'冲击试样明确记录外径{diameter:g}mm')
        if not dimension and soft_cable:
            # 冲击记录未单列试样外径时，允许借同一样品结构表的实测平均外径
            # （扁形按短轴）按表3档位核实落锤；借用口径在 note 与验证记录中明示。
            borrowed = _impact_dimension(sample)
            if borrowed is not None:
                borrowed_d, borrowed_flat, borrowed_note = borrowed
                if borrowed_d > 0:
                    dimension = (borrowed_d, borrowed_flat,
                                 borrowed_note+'（借结构表实测，冲击记录未单列试样外径）')
                    dimension_source = 'structure_average_outer_diameter_borrowed'
        if not dimension:
            for check in sample.get('checks') or []:
                if '低温冲击' not in f"{check.get('category','')}{check.get('item','')}":
                    continue
                if check.get('verdict')=='not_applicable':
                    continue
                check['impact_dimension_evidence'] = {'status':'missing_or_conflicting',
                                                      'observations':specimen_observations}
                if check.get('verdict')=='pass':
                    check['impact_record_gap_only'] = bool(
                        not specimen_observations and source_group
                        and not re.search(r'(?:冲击)?试样(?:实测)?外径',
                                          _plain_table_text(str(source_group.get('text') or ''))))
                    check['impact_record_table_family']='soft' if soft_cable else 'fixed'
                    check['verdict']='manual_review'
                check['deterministic_review_action']='核对低温冲击试样实测外径与落锤记录；不能直接用结构平均外径或短轴代替试样尺寸'
            records.append({'sample':sample.get('model'),'status':'impact_specimen_dimension_unverified'})
            continue
        diameter, flat, dimension_note = dimension
        related_checks = [
            check for check in sample.get("checks") or []
            if "低温冲击" in f"{check.get('category', '')}{check.get('item', '')}"
        ]
        reported_mass = next(
            (mass for check in related_checks if (mass := _reported_hammer_mass(check.get("reported"))) is not None),
            None,
        )
        if reported_mass is None:
            # 冲击行已从原页定位但简写报告值未带落锤质量时，从同页冲击段直接读取。
            for page in (source_group or {}).get('pages') or []:
                plain = _plain_table_text(str(page.get('text') or ''))
                for heading in re.finditer(r'低温冲击试验(?![机仪])', plain):
                    mass = _reported_hammer_mass(plain[heading.start():heading.start()+700])
                    if mass is not None:
                        reported_mass = mass
                        break
                if reported_mass is not None:
                    break
        if reported_mass is None:
            continue
        required_mass = _soft_hammer_mass(diameter) if soft_cable else _fixed_hammer_mass(diameter)
        correct = reported_mass == required_mass
        basis = (
            "GB/T 2951.14-2008 表3（软电缆；扁形按短轴）"
            if soft_cable else "GB/T 2951.14-2008 表2（固定敷设圆形电缆）"
        )
        for check in related_checks:
            obsolete_mass_only = correct and _obsolete_impact_table_failure(check, soft_cable, source_group)
            previous_required = str(check.get('required') or '')
            mass_requirement = f"{dimension_note}，落锤{required_mass}g，无裂纹"
            if obsolete_mass_only:
                check['resolved_parameter_claim'] = {'required':previous_required,'basis':check.get('basis'),
                                                     'reason':'wrong_impact_table_family_source_confirmed'}
                previous_required = ''
                check['verdict'] = 'pass'
                check['note'] = '已撤销唯一的落锤表族误用判断；原页明确无裂纹P，其他条件仍独立检查'
            if mass_requirement not in previous_required:
                check['required'] = '；'.join(v for v in (previous_required,mass_requirement) if v)
            check["basis"] = basis
            explicit_failure = bool(re.search(r'有裂纹|开裂|(?:^|[^A-Z])F(?:$|[^A-Z])',str(check.get('reported') or ''),re.I))
            if not correct or explicit_failure:
                check['verdict'] = 'fail'
            # Matching mass proves only a condition, not the outcome. Keep
            # prior fail/manual states rather than manufacturing a whole pass.
            mass_note = (
                f"程序复核：{dimension_note}，报告{reported_mass}g"
                f"{'符合' if correct else '不符合'}{'软电缆表3' if soft_cable else '固定敷设表2'}档位"
            )
            previous_note = str(check.get('note') or '')
            if mass_note not in previous_note:
                check['note'] = '；'.join(v for v in (previous_note,mass_note) if v)
        sample["items"] = [
            item for item in (sample.get("items") or [])
            if not (
                "低温冲击" in str(item.get("item", ""))
                and not re.search(r'裂纹|开裂|时间|温度|[、，,；;]',str(item.get('item') or ''))
                and "落锤" in " ".join(str(item.get(field, "")) for field in (
                    "item", "reported", "should_be", "review_action"
                ))
            )
            and not (str(item.get("item", "")) == "成品低温冲击落锤重量")
        ]
        if not correct:
            sample["items"].append({
                "item": "成品低温冲击落锤重量",
                "reported": f"{reported_mass}g（{dimension_note}）",
                "should_be": f"{required_mass}g",
                "standard": basis,
                "severity": "must_fix",
                "action_required": True,
                "action_type": "correction",
                "review_action": f"按{dimension_note}对应档位，将落锤重量更正为{required_mass}g并重新评定",
            })
        if not flat:
            sample["items"] = [
                item for item in (sample.get("items") or [])
                if not (
                    "结构检查" in str(item.get("item") or "")
                    and re.search(r"未显示宽\s*[×xX*]\s*厚|需确认外形尺寸", " ".join(
                        str(item.get(field) or "") for field in ("reported", "should_be", "review_action")
                    ))
                )
            ]
        records.append({
            "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
            "table": 3 if soft_cable else 2,
            "shape": "flat" if flat else "round", "dimension_mm": diameter,
            "reported_g": reported_mass, "required_g": required_mass, "passed": correct,
            "dimension_source": dimension_source,
        })
    if records:
        result.setdefault("_deterministic_validation", {})["lowtemp_impact"] = records
    return result


def _enforce_lowtemp_conditioning_time(result: dict[str, Any], source_text: str,
        local_evidence: dict[str, Any] | None = None, source_pdf_sha256: str = "") -> dict[str, Any]:
    """按原页复核低温时间，并接受标准明确允许的预冷缩短时间。

    GB/T 2951.14-2008 8.1.4允许设备与试样均预冷时，试样固定后1h
    即可开始弯曲/卷绕；8.5.5允许冲击设备预冷且试样达温时缩短至1h。报告已明确
    填写这些标准允许值时，不再制造“必须16h”的无意义提示。
    """
    from backend.app.rulebase import (
        _is_sheath_mechanical_page,
        _plain_table_text,
        _sheath_lowtemp_bend_source_state,
        source_group_for_sample,
        source_sample_registry,
    )

    source_registry = source_sample_registry(source_text)
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        model_key = re.sub(r"[\s()（）-]", "", str(sample.get("model") or "").upper())
        parsed_spec = _spec_cores_area(sample)
        large_rubber_lowtemp_exempt = bool(
            parsed_spec and parsed_spec[1] > 16
            and ("60245IEC66" in model_key or "YCW" in model_key)
        )
        plain_pages: list[tuple[str, str]] = []
        source_group = source_group_for_sample(sample, registry=source_registry)
        sheath_method_state = _sheath_lowtemp_bend_source_state(source_group)
        # 该函数可能在历史结果上重放：先清理上一次由本规则生成的占位提示，
        # 再依当前原页证据重建，避免16h已定位后仍残留“证据缺口”。
        sample["items"] = [
            item for item in (sample.get("items") or [])
            if not (
                str(item.get("standard") or "") == "GB/T 2951.14-2008"
                and item.get("action_type") == "manual_review"
                and (
                    "低温弯曲" in str(item.get("item") or "")
                    or "低温卷绕" in str(item.get("item") or "")
                )
            )
        ]
        bend_not_required = sheath_method_state == "false" or "60245IEC53" in model_key
        if bend_not_required:
            sample["items"] = [
                item for item in sample["items"]
                if not (
                    "护套低温弯曲" in str(item.get("item") or "")
                    and (
                        "60245IEC53" in model_key
                        or item.get("action_type") in {"manual_review", "correction"}
                    )
                )
            ]
        if source_group:
            for page in source_group.get("pages") or []:
                page_text = str(page.get("text") or "")
                plain = _plain_table_text(page_text)
                compact = re.sub(r"\W+", "", page_text)
                page_scope = (
                    "护套低温弯曲" if _is_sheath_mechanical_page(page_text) else
                    "绝缘低温弯曲" if "绝缘机械性能" in compact else
                    "低温弯曲"
                )
                plain_pages.append((plain, page_scope))
        source_hours: dict[str, list[int]] = {}
        for plain_page, bend_scope in plain_pages:
            for marker, scope in ((r"(?:低温弯曲|低温卷绕)", bend_scope), ("低温冲击", "低温冲击")):
                for match in re.finditer(
                    rf"{marker}试验.{{0,420}}?时间\s*(?:\|\s*)?(\d+|[lI])\s*(?:\|\s*)?h",
                    plain_page,
                    re.I | re.S,
                ):
                    raw_hours = match.group(1)
                    match_scope = scope
                    if scope != "低温冲击":
                        sheath_before = plain_page.rfind("护套机械性能", 0, match.start())
                        insulation_before = plain_page.rfind("绝缘机械性能", 0, match.start())
                        if sheath_before > insulation_before:
                            match_scope = "护套低温弯曲"
                        elif insulation_before >= 0:
                            match_scope = "绝缘低温弯曲"
                        elif plain_page.find("护套机械性能", match.end()) >= 0:
                            # 纵向OCR可能把“绝缘机械性能”拆散；如果护套章节
                            # 明确出现在本行之后，则页面前半部的弯曲行属于绝缘。
                            match_scope = "绝缘低温弯曲"
                    source_hours.setdefault(match_scope, []).append(
                        1 if raw_hours.lower() in {"l", "i"} else int(raw_hours)
                    )
        if source_group and not source_hours.get("低温冲击"):
            # RapidOCR纵向表有时会在“低温冲击”和“时间16h”之间插入
            # “品/电线电缆试验”等碎片，HTML平铺后超出旧窗口。仍严格
            # 限定在当前样品页组，并只向后读取明确的时间字段。
            for page in source_group.get("pages") or []:
                raw_page = str(page.get("text") or "")
                for match in re.finditer(
                    r"低温冲击(?:试验)?.{0,900}?时间\s*(?:\|\s*)?(\d+|[lI])\s*(?:\|\s*)?h",
                    raw_page,
                    re.I | re.S,
                ):
                    raw_hours = match.group(1)
                    source_hours.setdefault("低温冲击", []).append(
                        1 if raw_hours.lower() in {"l", "i"} else int(raw_hours)
                    )
        if source_group and not source_hours.get("低温冲击"):
            allowed_pages = {int(page["page"]) for page in source_group.get("pages") or []}
            page_sections = re.finditer(
                r"--- Page\s+(\d+)\s*\([^)]*\)\s*---\s*([\s\S]*?)(?=\n--- Page\s+\d+\s*\(|\Z)",
                source_text,
                re.I,
            )
            for section in page_sections:
                if int(section.group(1)) not in allowed_pages:
                    continue
                for match in re.finditer(
                    r"低温冲击(?:试验)?.{0,900}?时间\s*(?:\|\s*)?(\d+|[lI])\s*(?:\|\s*)?h",
                    section.group(2),
                    re.I | re.S,
                ):
                    raw_hours = match.group(1)
                    source_hours.setdefault("低温冲击", []).append(
                        1 if raw_hours.lower() in {"l", "i"} else int(raw_hours)
                    )
        if local_evidence and source_group:
            from backend.app.ocr_table_repair import attach_group
            bound=attach_group(source_group,local_evidence,source_pdf_sha256,source_registry)
            impacts=bound.get('_impact_observations') or []
            if len(impacts)==1:
                hours_value=impacts[0]['facts']['hours']
                previous=source_hours.get('低温冲击') or []
                if not previous or set(previous)=={hours_value}:
                    source_hours['低温冲击']=[hours_value]
            for observation in bound.get('_lowtemp_bend_observations') or []:
                scope=('护套低温弯曲' if observation.get('component')=='护套' else '绝缘低温弯曲')
                hours_value=observation.get('hours')
                previous=source_hours.get(scope) or []
                if hours_value is not None and (not previous or set(previous)=={hours_value}):
                    source_hours[scope]=[hours_value]
        source_offsets = {scope: 0 for scope in source_hours}
        for check in sample.get("checks") or []:
            label = f"{check.get('category', '')}{check.get('item', '')}"
            if check.get('required_item_reported_not_applicable') is True:
                # 必做项目明确判N已经是确定性报告缺陷；不得再因为N行没有
                # 状态调节时间，把它降级回“人工复核时间缺口”。
                continue
            if str(check.get("verdict") or "") == "not_applicable":
                continue
            scope = (
                "低温冲击" if "低温冲击" in label else
                "绝缘低温弯曲" if "绝缘" in label and "低温弯曲" in label else
                "护套低温弯曲" if "护套" in label and "低温弯曲" in label else
                "低温弯曲" if "低温弯曲" in label else None
            )
            if not scope:
                continue
            # 圆形护套平均外径>12.5 mm时报告已明确选择并通过低温拉伸，
            # 则弯曲属于二选一未选方法。要求三个同一样品、已定位且判P的
            # 证据同时成立，不能只凭“同页出现拉伸”做泛化推断。
            if scope == "护套低温弯曲":
                diameter_checks=[]
                for candidate in sample.get('checks') or []:
                    if (candidate is check or candidate.get('verdict')!='pass'
                            or not candidate.get('source_pages')
                            or '外径' not in f"{candidate.get('category','')}{candidate.get('item','')}"):
                        continue
                    match=re.search(r'平均外径\D{0,8}(\d+(?:\.\d+)?)(?:\s*mm?)?',
                                    f"{candidate.get('reported','')} {candidate.get('note','')}",re.I)
                    if match and float(match.group(1))>12.5:
                        diameter_checks.append((candidate,float(match.group(1))))
                tensile_checks=[]
                for candidate in sample.get('checks') or []:
                    if (candidate is check or candidate.get('verdict')!='pass'
                            or not candidate.get('source_pages')
                            or '护套低温拉伸' not in f"{candidate.get('category','')}{candidate.get('item','')}"):
                        continue
                    match=re.search(r'外径\D{0,8}(\d+(?:\.\d+)?)\s*mm?\s*>\s*12\.5\s*mm?.*适用低温拉伸',
                                    f"{candidate.get('reported','')} {candidate.get('note','')}",re.I)
                    if match:
                        tensile_checks.append((candidate,float(match.group(1))))
                if (len(diameter_checks)==1 and len(tensile_checks)==1
                        and abs(diameter_checks[0][1]-tensile_checks[0][1])<1e-9):
                    diameter=diameter_checks[0][1]
                    check['verdict']='not_applicable'
                    check['required']='圆形护套平均外径>12.5mm时采用低温拉伸，低温弯曲不适用'
                    check['note']=f'程序按同一样品外径{diameter:g}mm及护套低温拉伸P的绑定证据确认二选一方法'
                    sample['items']=[item for item in sample.get('items') or []
                        if '护套低温弯曲' not in str(item.get('item') or '')]
                    records.append({'sample':' '.join(str(sample.get(k,'')) for k in ('model','voltage','spec')).strip(),
                                    'item':scope,'status':'alternate_tensile_method_confirmed'})
                    continue
            # 原页已明确选用护套低温拉伸时，弯曲项就是不适用。
            # 拉伸4h不得被套用为弯曲的16h状态调节证据。
            if scope == "护套低温弯曲" and bend_not_required:
                check["verdict"] = "not_applicable"
                check["note"] = (
                    "程序复核：60245 IEC 53(YZ)产品表未列护套低温弯曲，该项不进入必审"
                    if "60245IEC53" in model_key else
                    "程序复核：原页已选用护套低温拉伸，弯曲项不适用；未将拉伸4h套用为弯曲时间"
                )
                continue
            if large_rubber_lowtemp_exempt:
                continue
            hours_match = re.search(r"(?:时间|冷却)\D{0,10}(\d+)\s*h", str(check.get("reported") or ""), re.I)
            hours = int(hours_match.group(1)) if hours_match else None
            evidence_source = "model_check"
            candidates = source_hours.get(scope) or source_hours.get("低温弯曲") or []
            offset_key = scope if scope in source_hours else "低温弯曲"
            offset = source_offsets.get(offset_key, 0)
            if offset < len(candidates):
                hours = candidates[offset]
                source_offsets[offset_key] = offset + 1
                evidence_source = "sample_source_page"
            elif candidates:
                # 同一物理样品被模型分成多个同名check时，原页只有一个时间行。
                # 重复check复用该同范围证据，不再制造第二个“未定位”缺口。
                hours = candidates[-1]
                evidence_source = "sample_source_page_reused_for_duplicate_check"
            if hours is None:
                if str(check.get("verdict") or "") == "not_applicable":
                    continue
                # 状态调节时间为标准固定条件：未定位到时间证据时按标准允许条件视为满足，
                # 不再降级为人工复核，也不再生成“状态调节时间证据缺口”项。
                existing_required = str(check.get("required") or "").strip()
                time_requirement = "常规16h；按标准预冷条件可缩短至不少于1h（标准固定条件，未单独举证视为满足）"
                check["required"] = f"{existing_required}；{time_requirement}" if existing_required else time_requirement
                note_add = "程序复核：未定位到状态调节时间；该条件为标准固定条件，视为满足"
                old_note = str(check.get("note") or "").strip()
                check["note"] = f"{old_note}；{note_add}" if old_note else note_add
                # 清理同一样品可能残留的旧版时间缺口项
                sample["items"] = [
                    item for item in (sample.get("items") or [])
                    if "状态调节时间证据缺口" not in str(item.get("item") or "")
                ]
                continue
            minimum_hours = 1
            if hours >= minimum_hours:
                sample["items"] = [
                    item for item in (sample.get("items") or [])
                    if not (
                        scope in str(item.get("item") or "")
                        and (
                            "状态调节时间证据缺口" in str(item.get("item") or "")
                            or (
                                re.search(r"(?:时间|冷却)\D{0,10}\d+\s*h", str(item.get("reported") or ""), re.I)
                                and "16h" in str(item.get("should_be") or "").replace(" ", "")
                                and str(item.get("severity") or "") == "suggestion"
                            )
                        )
                    )
                ]
                unresolved_conditions = any(
                    condition.get('verdict') != 'pass'
                    for condition in check.get('impact_condition_checks') or []
                ) or (check.get('impact_dimension_evidence') or {}).get('status') == 'missing_or_conflicting'
                # Time alone cannot prove that an allegedly missing project,
                # its temperature, load and result have all been verified.
                unresolved_conditions = unresolved_conditions or bool(re.search(
                    r'未列|未报告|未提供|未进行|未做|缺少|缺失|冲突',
                    str(check.get('reported') or '')))
                if check.get("verdict") == "manual_review" and not unresolved_conditions and not re.search(
                    r"(?:有裂纹|开裂|不符合|超限|(?:^|[^A-Z])F(?:$|[^A-Z]))", str(check.get("reported") or ""), re.I
                ):
                    check["verdict"] = "pass"
                conditioning_note = (
                    f"程序复核：原页{hours}h，符合GB/T 2951.14-2008"
                    f"预冷条件下不少于{minimum_hours}h的允许值"
                )
                if conditioning_note not in str(check.get('note') or ''):
                    check['note'] = '；'.join(filter(None, [str(check.get('note') or ''), conditioning_note]))
                records.append({
                    "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                    "item": scope, "reported_hours": hours, "required_hours": minimum_hours,
                    "evidence_source": evidence_source, "status": "accepted_shortened_conditioning",
                })
                continue
            check["verdict"] = "fail"
            existing_required = str(check.get("required") or "").strip()
            time_requirement = f"预冷条件下状态调节时间不少于{minimum_hours}h"
            check["required"] = f"{existing_required}；{time_requirement}" if existing_required else time_requirement
            check["note"] = f"程序复核：当前样品原页为{hours}h，低于标准允许的最短{minimum_hours}h"
            item_label = scope
            canonical = {
                "item": item_label,
                "reported": f"时间{hours}h",
                "should_be": f"常规16h；满足预冷条件时不少于{minimum_hours}h",
                "standard": "GB/T 2951.14-2008",
                "severity": "suggestion",
                "action_required": True,
                "action_type": "manual_review",
                "review_action": f"核对并将{item_label}状态调节时间更正为不少于{minimum_hours}h后重新评定",
            }
            existing = next((item for item in (sample.get("items") or [])
                             if item_label in str(item.get("item") or "")
                             and re.search(r"(?:时间|冷却)\D{0,10}\d+\s*h", str(item.get("reported") or ""), re.I)), None)
            if existing is None:
                sample.setdefault("items", []).append(canonical)
            else:
                existing.update(canonical)
            records.append({
                "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                "item": item_label,
                "reported_hours": hours,
                "required_hours": minimum_hours,
                "evidence_source": evidence_source,
                "status": "below_minimum",
            })
    if records:
        result.setdefault("_deterministic_validation", {})["lowtemp_conditioning_time"] = records
    return result


def _apply_original_record_notice_policy(result: dict[str, Any]) -> dict[str, Any]:
    """User-approved report scope; never turn missing measurements into pass.

    Applied after all failure/input guards. Only two typed, bounded causes
    qualify; all other unknowns retain their existing blocking behavior.
    """
    def _impact_record_scope(check: dict[str, Any]) -> str:
        """Keep P/F/crack matching inside the current low-temperature row.

        Source excerpts may contain the rest of the page, including the footer
        legend ``F means failed``.  That legend is not a failure result for the
        current impact test and must not prevent the approved notice policy.
        """
        source = str(check.get('source_excerpt') or '')
        heading = re.search(r'(?:成品(?:电线|电缆|电线电缆)?)?低温冲击(?:试验)?', source)
        if heading:
            source = source[heading.start():]
        source = re.split(
            r'(?:燃烧性能|成品电缆机械强度|曲挠试验|绝缘线芯电压试验|'
            r'绝缘电阻|热冲击试验|失重试验|注\s*[:：]|---\s*Page)',
            source,
            maxsplit=1,
        )[0]
        return str(check.get('reported') or '') + source

    for sample in result.get('samples') or []:
        for check in sample.get('checks') or []:
            if check.get('verdict')!='manual_review' or check.get('evidence_status')!='located' or not check.get('source_pages'):
                continue
            name=str(check.get('item') or '')
            peers=[c for c in sample.get('checks') or [] if c is not check and c.get('item')==name]
            if any(c.get('verdict')=='fail' for c in peers):
                continue
            items=[i for i in sample.get('items') or [] if i.get('item')==name and i.get('action_required') is not False]
            check_masses={float(v) for v in re.findall(
                r'落锤(?:质量|重量)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*g',str(check.get('reported') or ''))}
            equivalent_impact_items=(
                bool(re.fullmatch(r'(?:成品(?:电缆)?|护套)?低温冲击(?:试验)?',name))
                and len(check_masses)==1 and all(
                    i.get('severity')=='suggestion'
                    and i.get('action_type')=='manual_review'
                    and i.get('source_pages')==check.get('source_pages')
                    and {float(v) for v in re.findall(
                        r'落锤(?:质量|重量)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*g',str(i.get('reported') or ''))}==check_masses
                    for i in items))
            if any(i.get('severity')=='must_fix' or i.get('source_pages')!=check.get('source_pages')
                   or (i.get('reported')!=check.get('reported') and not equivalent_impact_items) for i in items):
                continue
            reason=None
            impact_conditions=check.get('impact_condition_checks') or []
            impact_scope=_impact_record_scope(check)
            typed_impact_gap=(len(impact_conditions)==2
                and sum(c.get('verdict')!='pass' for c in impact_conditions)==1
                and all(c.get('verdict')=='pass' or (
                    c.get('field')=='落锤质量' and c.get('verdict')=='unknown'
                    and c.get('reason')=='specimen_diameter_missing')
                    for c in impact_conditions))
            legacy_impact_gap=(not impact_conditions
                and bool(re.search(r'无裂纹',impact_scope))
                and bool(re.search(r'(?:^|[^A-Z])P(?:$|[^A-Z])',impact_scope,re.I))
                and not re.search(r'有裂纹|开裂|(?:^|[^A-Z])F(?:$|[^A-Z])',
                                  impact_scope,re.I))
            if (re.fullmatch(r'(?:成品(?:电缆)?|护套)?低温冲击(?:试验)?',name)
                    and check.get('impact_record_gap_only') is True
                    and not (check.get('impact_dimension_evidence') or {}).get('observations')
                    and not check.get('impact_source_failures')
                    and (typed_impact_gap or legacy_impact_gap)):
                reported=str(check.get('reported') or '')
                mass=re.search(r'落锤(?:质量|重量)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*g',reported)
                peer_masses={float(m) for c in [check,*peers] for m in re.findall(
                    r'落锤(?:质量|重量)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*g',str(c.get('reported') or ''))}
                possible_masses={'soft':{100,200,300,400,500,600},
                                 'fixed':{100,200,300,400,500,750,1000,1250,1500}}.get(check.get('impact_record_table_family'),set())
                if (mass and len(peer_masses)==1 and float(mass[1]) in possible_masses
                        and (typed_impact_gap or re.search(r'无裂纹|未开裂|不开裂',reported))):
                    reason='impact_specimen_record_not_listed'
                    message='报告未单列冲击试样实测外径，落锤质量与试样尺寸的匹配关系未独立核实；不因此要求人工复核，不代表该条件已通过。'
            conditions=check.get('pressure_condition_checks') or []
            unknown=[c for c in conditions if c.get('verdict')!='pass']
            if (check.get('coverage_origin')=='deterministic_local_pressure_observation'
                    and re.fullmatch(r'(?:护套)?高温压力(?:试验)?',name)
                    and {c.get('field') for c in conditions}=={'压痕深度','温度','时间','荷载'}
                    and len(conditions)==4 and len(unknown)==1 and unknown[0].get('field')=='荷载'
                    and unknown[0].get('verdict')=='unknown' and unknown[0].get('reason')=='display_precision_overlap'
                    and (check.get('local_pressure_depth_check') or {}).get('verdict')=='pass'):
                reason='pressure_unrounded_setting_not_listed'
                message='压痕、温度和时间已独立核验；荷载显示值与允许值的舍入区间相容，未修约设定值未独立核实。不因此要求人工复核，不代表实际荷载已通过。'
            aging=check.get('aging_condition_checks') or []
            aging_unknown=[c for c in aging if c.get('verdict')=='unknown']
            if (check.get('coverage_origin')=='local_pvc_oven_evidence'
                    and check.get('verdict')=='manual_review' and aging
                    and not any(c.get('verdict')=='fail' for c in aging)
                    and len(aging_unknown)==1
                    and aging_unknown[0].get('reason')=='display_precision_overlap'):
                reason='aging_change_unrounded_middle_value_not_listed'
                message='老化前后显示中间值的修约区间与报告变化率相容；未修约中间值未独立核实。不因此要求人工复核，不代表重新计算值替代原始记录。'
            if reason is None:
                continue
            check['original_record_notice']={'policy':'report-record-scope-v1','reason':reason,
                'previous_verdict':check['verdict'],'previous_review_action':check.get('deterministic_review_action'),
                'previous_note':check.get('note'),'condition_verified':False,'message':message}
            check.update(verdict='notice',action_required=False,note=message)
            check.pop('deterministic_review_action',None)
            check['required']=str(check.get('required') or '')+'；'+message
            for item in items:
                if item.get('severity')=='suggestion' and item.get('action_type')=='manual_review':
                    archive=sample.setdefault('superseded_model_items',[])
                    record={'reason':reason,'original_item':dict(item),'policy':'report-record-scope-v1'}
                    if record not in archive:archive.append(record)
                    item.update(action_required=False,action_type='none')
        # Model/source recovery may describe the same missing-record limitation
        # twice. Display it once while retaining both source claims in history.
        kept=[]
        seen={}
        notice_keys=set()
        for candidate in sample.get('checks') or []:
            notice=candidate.get('original_record_notice') or {}
            if candidate.get('verdict')=='notice' and notice.get('reason')=='impact_specimen_record_not_listed':
                masses=tuple(float(m) for m in re.findall(
                    r'落锤(?:质量|重量)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*g',str(candidate.get('reported') or '')))
                notice_keys.add((candidate.get('item'),tuple(sorted(candidate.get('source_pages') or [])),masses))
        for check in sample.get('checks') or []:
            notice=check.get('original_record_notice') or {}
            masses=tuple(float(m) for m in re.findall(r'落锤(?:质量|重量)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*g',str(check.get('reported') or '')))
            key=(check.get('item'),tuple(sorted(check.get('source_pages') or [])),masses)
            if (check.get('verdict')=='manual_review' and key in notice_keys
                    and not re.search(r'有裂纹|开裂|(?:^|[^A-Z])F(?:$|[^A-Z])',
                                      _impact_record_scope(check),re.I)):
                archive=sample.setdefault('consolidated_notice_checks',[])
                archive.append({'reason':'same_source_original_record_notice','original_check':dict(check)})
                continue
            if check.get('verdict')!='notice' or notice.get('reason')!='impact_specimen_record_not_listed':
                kept.append(check)
                continue
            if key not in seen:
                seen[key]=len(kept);kept.append(check)
                continue
            index=seen[key]
            old=kept[index]
            if check.get('coverage_origin') and not old.get('coverage_origin'):
                kept[index]=check
            else:
                old=check
            record={'reason':'same_source_original_record_notice','original_check':dict(old)}
            archive=sample.setdefault('consolidated_notice_checks',[])
            if record not in archive:archive.append(record)
        sample['checks']=kept
        # 后续检查/项目一致性可能按同一个确定性fail重建同名整改项。
        # 只对本规则生成的“必做冲击判N”做完全同键去重。
        item_seen=set();deduped=[]
        for item in sample.get('items') or []:
            if str(item.get('item') or '')!='成品低温冲击不应判N':
                deduped.append(item);continue
            key=(item.get('item'),item.get('severity'),str(item.get('reported') or ''),
                 tuple(item.get('source_pages') or []),item.get('action_type'))
            if key in item_seen:
                sample.setdefault('consolidated_required_items',[]).append(dict(item))
                continue
            item_seen.add(key);deduped.append(item)
        sample['items']=deduped
    return result


def _suppress_non_actionable_manual_reviews(result: dict[str, Any]) -> dict[str, Any]:
    """删除没有给出确定冲突或必要输入缺口的泛化人工复核提示。"""
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        # 送样范围另有独立流程。不可一面删除整改提示，一面仍展示未经核验的“通过”。
        scoped_checks = []
        for check in sample.get("checks") or []:
            if str(check.get("item") or "").strip() == "认证单元划分与型式试验送样规则":
                sample.setdefault("_out_of_scope_checks", []).append({
                    "original_check": dict(check),
                    "reason": "认证单元/送样属独立下样流程，不纳入报告内容审核结论",
                })
            else:
                scoped_checks.append(check)
        if "checks" in sample:
            sample["checks"] = scoped_checks
        kept = []
        for item in sample.get("items") or []:
            name = str(item.get("item") or "")
            reported = str(item.get("reported") or "")
            should_be = str(item.get("should_be") or "")
            action = str(item.get("review_action") or "")
            manual = item.get("action_type") == "manual_review"
            generic_flame_review = (
                manual
                and "成束阻燃性能" in name
                and "是否" in f"{should_be} {action}"
                and not re.search(r"应为|不应|最大|最小|[<>]", should_be)
            )
            report_reference_scope_guess = (
                manual
                and "备注引用报告编号" in name
                and bool(re.search(r"确认.*是否适用", f"{should_be} {action}"))
                and not re.search(r"与.*不一致|冲突|两个不同", f"{item.get('reported', '')} {should_be}")
            )
            sampling_scope_issue = "认证单元划分与型式试验送样规则" in name
            accepted_precooled_lowtemp = (
                manual
                and bool(re.search(r"低温(?:弯曲|卷绕|冲击|拉伸)", name))
                and "预冷" in f"{reported} {should_be} {action}"
                and (
                    bool(re.search(r"低温(?:弯曲|卷绕|冲击)", name))
                    and bool(re.search(r"(?:^|\D)1(?:\.0)?\s*(?:h|小时)(?:\D|$)", reported, re.I))
                    or bool(re.search(r"低温拉伸", name))
                    and bool(re.search(r"(?:30\s*(?:min|分钟)|0\.5\s*(?:h|小时))", reported, re.I))
                )
            )
            if generic_flame_review or report_reference_scope_guess or sampling_scope_issue or accepted_precooled_lowtemp:
                if sampling_scope_issue:
                    reason = "认证单元/送样属独立下样流程，不纳入报告内容审核结论"
                elif accepted_precooled_lowtemp:
                    reason = "GB/T 2951.14-2008已允许在设备和试样预冷条件下采用该缩短时间"
                else:
                    reason = "未定位到确定冲突，仅泛化要求人工重新审全项"
                records.append({
                    "sample": " ".join(str(sample.get(key) or "") for key in ("model", "voltage", "spec")).strip(),
                    "item": name,
                    "reason": reason,
                })
                continue
            kept.append(item)
        sample["items"] = kept

    if records:
        result.setdefault("_deterministic_validation", {})["non_actionable_manual_review_suppression"] = records
    return result


def _not_applicable_report_state(reported: str) -> str:
    """判断“本项不适用”时报告究竟是判N、实际做了，还是证据不足。

    performed 优先于 N：合并项可能同时出现“弯曲N；拉伸115%”，
    不能因整段中存在N就把另一个已做试验判为通过。
    """
    text_value = str(reported or "").strip()
    if re.search(
        r"(?:^|[^A-Z])(?:P|F)(?:$|[^A-Z])|通过|合格|不合格|无裂纹|未击穿|"
        r"不发生电流断路|不发生导体间短路|\d+(?:\.\d+)?\s*%",
        text_value,
        re.I,
    ):
        return "performed"
    if re.search(
        r"(?:^|[^A-Z])N(?:$|[^A-Z])|未做|未进行|不适用|"
        r"未提供(?:数据|结果)?|结果未填写|参数空白|结果\s*[-—－]|"
        r"(?:^|[;；，、\s])[-—－](?:$|[;；，、\s])",
        text_value,
        re.I,
    ):
        return "not_done"
    return "unknown"


def _source_text_for_review_sample(sample: dict[str, Any], source_text: str) -> str:
    """以型号别名+电压+单根截面唯一绑定当前样品原页；不按数组顺序猜测。"""
    if not source_text:
        return ""
    parsed = _spec_cores_area(sample)
    voltage_match = re.search(r"(\d{2,4})\s*/\s*(\d{2,4})", str(sample.get("voltage") or ""))
    model_text = str(sample.get("model") or "").upper()
    alias_match = re.search(r"(?:YCW|YZW|YZ|YC)", model_text)
    if not parsed or not voltage_match or not alias_match:
        return ""
    alias = alias_match.group(0)
    voltage = f"{voltage_match.group(1)}/{voltage_match.group(2)}"
    candidates: list[str] = []
    for batch in _split_report_by_samples(source_text):
        labels = batch.get("labels") or []
        if len(labels) != 1:
            continue
        label = str(labels[0])
        label_spec = _spec_cores_area({"spec": label})
        if not label_spec and parsed[0] == 1:
            # 部分单芯报告页眉只写“150mm²”，而结果JSON会规范化为
            # “1×150mm²”。仅在当前样品明确为单芯时允许这一等价写法。
            single_area = re.search(r"(?<![×xX*\d])([0-9]+(?:\.[0-9]+)?)\s*mm\s*(?:²|2)\b", label, re.I)
            if single_area:
                label_spec = (1, float(single_area.group(1)))
        label_voltage = re.search(r"(\d{2,4})\s*/\s*(\d{2,4})", label)
        if not label_spec or not label_voltage:
            continue
        if label_spec != parsed or f"{label_voltage.group(1)}/{label_voltage.group(2)}" != voltage:
            continue
        if not re.search(rf"(?<![A-Z]){re.escape(alias)}(?![A-Z])", label.upper()):
            continue
        candidates.append(str(batch.get("text") or ""))
    return candidates[0] if len(candidates) == 1 else ""


def _source_applicability_state(sample: dict[str, Any], source_text: str, item_kind: str) -> tuple[str, str]:
    """从当前样品原页的单项评定读取是否真正实施，避免把标准要求数字当成实测值。"""
    sample_source = _source_text_for_review_sample(sample, source_text)
    if not sample_source:
        return "unknown", ""
    pattern = r"低温弯曲试验" if item_kind == "lowtemp" else r"曲挠试验"
    max_chars = 1800 if item_kind == "lowtemp" else 1200
    candidates: list[tuple[str, str]] = []
    for heading in re.finditer(pattern, sample_source, re.I):
        # “电线曲挠试验机”属于设备清单，不是试验结果。真实项目区段应
        # 出现施加条件、往复运动或浸水电压等字段。
        if item_kind == "flexing" and sample_source[heading.end():heading.end() + 1] == "机":
            continue
        block = sample_source[heading.start():heading.start() + max_chars]
        stop = re.search(r"注\s*[:：]|\n--- Page \d+", block)
        if stop:
            block = block[:stop.start()]
        if item_kind == "lowtemp":
            if "低温拉伸试验" not in block:
                continue
        else:
            if not any(marker in block for marker in ("施加电流", "往复运动", "浸水电压试验")):
                continue
            # 曲挠后的浸水电压和后续阻燃项目是独立项目，其P/F不能反向
            # 污染曲挠试验本身的适用性判定。
            next_item = re.search(r"曲挠试验后浸水电压试验", block)
            if next_item:
                block = block[:next_item.start()]
        verdicts = [value.upper() for value in re.findall(
            r"(?:^|[^A-Z])([PFN])(?:$|[^A-Z])", block, re.I,
        )]
        state = (
            "performed" if any(value in {"P", "F"} for value in verdicts)
            else "not_done" if "N" in verdicts or re.search(r"[-—－]{1,}", block)
            else "unknown"
        )
        excerpt = re.sub(r"\s+", " ", block).strip()[:700]
        candidates.append((state, excerpt))
    if not candidates:
        return "unknown", ""
    # 同一报告通常依次包含PDF文字层与MinerU重复表格。这里使用首个完整、
    # 有状态的项目区段，避免后续压缩HTML把整列其他项目的P串入本项目。
    for state, excerpt in candidates:
        if state != "unknown":
            return state, excerpt
    return candidates[0]


def _enforce_rubber_lowtemp_area_limit(
    result: dict[str, Any], standard_family: str | None, source_text: str = "",
) -> dict[str, Any]:
    """5013.4 5.4：低温试验仅针对导体标称截面≤16 mm²的电缆。

    YCW 1×150 等大截面报告即使护套外径超过12.5 mm，也不应据此生成
    “缺少低温拉伸”的修改建议。这里按单根导体标称截面判断，而不是把
    芯数乘以截面或把成品外径当成适用性条件。
    """
    if standard_family != "rubber":
        return result
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        model = str(sample.get("model", "")).upper().replace(" ", "")
        if "60245IEC66" not in model and "YCW" not in model:
            continue
        parsed = _spec_cores_area(sample)
        if not parsed or parsed[1] <= 16:
            continue
        related_items = []
        for item in sample.get("items") or []:
            label = str(item.get("item", ""))
            # AI 的项目名可能是“护套低温拉伸”，也可能只写
            # “低温拉伸试验”。适用怤5 IEC 66/YCW 且截面超过16mm²
            # 时，弯曲/拉伸都不适用，不应再依赖“护套”二字才拦截。
            if "低温" in label and any(marker in label for marker in ("弯曲", "拉伸")):
                related_items.append(item)
        if related_items:
            sample["items"] = [item for item in sample.get("items") or [] if item not in related_items]

        related_checks = []
        for check in sample.get("checks") or []:
            label = f"{check.get('category', '')}{check.get('item', '')}"
            if "低温" not in label or not any(marker in label for marker in ("弯曲", "拉伸")):
                continue
            related_checks.append(check)

        reported_values = [str(check.get("reported", "")) for check in related_checks]
        reported_values.extend(str(item.get("reported", "")) for item in related_items)
        states = [_not_applicable_report_state(value) for value in reported_values if value.strip()]
        # 明确实测P/F优先拦截；否则N/横线已能证明未实施。
        # 不允许模型附加的“结果未填写”泛化提示把明确N覆盖为unknown。
        state = (
            "performed" if "performed" in states else
            "not_done" if "not_done" in states else
            "unknown"
        )
        source_state, source_excerpt = _source_applicability_state(sample, source_text, "lowtemp")
        if source_state != "unknown":
            state = source_state
            reported_values.append(f"原页证据：{source_excerpt}")

        for check in related_checks:
            check["verdict"] = {
                "performed": "fail", "not_done": "not_applicable", "unknown": "manual_review",
            }[state]
            check["required"] = "导体标称截面>16 mm²，低温弯曲/拉伸不适用，可判N"
            check["basis"] = "GB/T 5013.4-2008 5.4"
            check["note"] = (
                f"程序按单根导体标称截面复核；{parsed[1]:g} mm²超过16 mm²，"
                + ("报告却填写了明确试验结果" if state == "performed" else
                   "报告已判N/未进行" if state == "not_done" else
                   "报告填写状态不足以确认是否实施")
            )

        joined_reported = "；".join(value for value in reported_values if value.strip()) or "未定位到明确P/N或实测结果"
        if state == "performed":
            sample.setdefault("items", []).append({
                "item": "大截面低温试验适用性", "reported": joined_reported,
                "should_be": "导体标称截面>16 mm²，低温弯曲和低温拉伸均不应实施，应判N/不适用",
                "standard": "GB/T 5013.4-2008 5.4", "severity": "must_fix", "action_required": True,
                "action_type": "correction", "review_action": "删除不适用的低温试验结果并将相应项评定为N",
            })
        elif state == "unknown":
            sample.setdefault("items", []).append({
                "item": "大截面低温试验填写状态待复核", "reported": joined_reported,
                "should_be": "确认报告是否为N/未实施；证据不足不得判通过",
                "standard": "GB/T 5013.4-2008 5.4", "severity": "suggestion", "action_required": True,
                "action_type": "manual_review", "review_action": "人工回看原PDF低温项P/N与结果列",
            })
        records.append({
            "sample": " ".join(str(sample.get(k, "")) for k in ("model", "voltage", "spec")).strip(),
            "section_mm2": parsed[1],
            "status": "performed_but_not_applicable" if state == "performed" else "not_applicable" if state == "not_done" else "manual_review",
            "standard": "GB/T 5013.4-2008 5.4",
        })
    if records:
        result.setdefault("_deterministic_validation", {})["lowtemp_area_limit"] = records
    return result


def _strip_flexing_from_combined_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """从“曲挠试验、单根阻燃”这类模型合并项中只移除曲挠部分。

    若整条只是曲挠则返回None；否则保留阻燃等独立问题，避免
    在拦截曲挠误报时误删真正需要复核的项目。
    """
    label = str(entry.get("item", ""))
    if "曲挠" not in label:
        return dict(entry)
    parts = [part.strip() for part in re.split(r"[、，,/；;]+", label) if part.strip()]
    remaining = [part for part in parts if "曲挠" not in part]
    if not remaining:
        return None
    cleaned = dict(entry)
    cleaned["item"] = "、".join(remaining)
    for field in ("reported", "required", "should_be"):
        value = str(cleaned.get(field, ""))
        segments = [segment.strip() for segment in re.split(r"[；;\n]+", value) if segment.strip()]
        kept = [segment for segment in segments if "曲挠" not in segment]
        if kept:
            cleaned[field] = "；".join(kept)
    if "阻燃" in cleaned["item"]:
        for field in ("basis", "standard"):
            value = str(cleaned.get(field, ""))
            refs = [ref.strip() for ref in re.split(r"[；;]+", value) if ref.strip()]
            flame_refs = [ref for ref in refs if any(marker in ref for marker in ("18380", "19666"))]
            if flame_refs:
                cleaned[field] = "；".join(flame_refs)
    if "review_action" in cleaned:
        cleaned["review_action"] = cleaned.get("should_be") or f"人工复核{cleaned['item']}"
    if "note" in cleaned:
        cleaned["note"] = f"曲挠部分已按截面边界排除；仍需复核{cleaned['item']}"
    return cleaned


def _enforce_rubber_flexing_applicability(
    result: dict[str, Any], standard_family: str | None, source_text: str = "",
) -> dict[str, Any]:
    """确定怤5/5013与8735橡皮软电缆的曲挠截面上限。

    所有单芯橡皮电缆以及导体标称截面超过4 mm²的多芯橡皮软电缆，
    动态曲挠试验不适用。这一判断只解析已识别的型号规格，不增加AI请求；
    超过18芯还需同时确认绞合层数，证据不足时不在这里自动下结论。
    """
    if standard_family != "rubber":
        return result
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        parsed = _spec_cores_area(sample)
        if not parsed:
            continue
        model = str(sample.get("model", "")).upper().replace(" ", "")
        # GB/T 5013.6 的60245 IEC 81(YH)/82(YHF)是电焊机电缆，
        # 适用“静态曲挠试验”（两根铅垂线距离最大45cm）。
        # 这不是GB/T 5013.2的滑轮往复动态曲挠，不能套用>4mm²不适用边界。
        welding_cable = bool(
            re.search(r"60245IEC(?:81|82)", model)
            or re.search(r"(?:^|[^A-Z])YHF?(?:$|[^A-Z])", model)
        )
        if welding_cable:
            continue
        cores, section = parsed
        if cores != 1 and section <= 4:
            continue
        if not any(marker in model for marker in ("60245IEC", "YCW", "YZW", "YZ", "YC")):
            continue
        # GB/T5013.3-2008 Table2 has no dynamic flexing item for IEC03(YG).
        # Absence of a non-required row is not missing evidence of its N cell.
        # Keep the existing conflict path whenever either source or model
        # actually contains a flexing statement; do not delete such entries.
        yg_model = re.sub(r'[()（）]', '', model)
        if (cores == 1 and re.fullmatch(r'(?:Z[ABCD]-)?60245IEC03(?:YG)?', yg_model)
                and source_text and re.search(r'5013\s*[.．]\s*3', source_text)
                and '曲挠' not in source_text
                and not any('曲挠' in str(entry) for field in ('checks','items')
                            for entry in sample.get(field) or [])):
            records.append({'sample':str(sample.get('model'))+' '+str(sample.get('spec')),
                            'status':'not_required_absent', 'standard':'GB/T 5013.3-2008 表2'})
            continue
        evidence_text = " ".join(
            str(value or "")
            for check in (sample.get("checks") or [])
            for value in (check.get("basis"), check.get("required"))
        ) + " " + " ".join(str(item.get("standard", "")) for item in (sample.get("items") or []))
        basis = (
            "GB/T 5013.2-2008 3.1.1"
            if "5013" in evidence_text or "60245IEC" in model
            else "JB/T 8735.1-2016 6.3.2"
        )
        not_applicable_reason = (
            "单芯橡皮电缆不进行动态曲挠试验"
            if cores == 1
            else f"导体标称截面{section:g} mm²>4 mm²，曲挠试验不适用"
        )
        flex_items = [item for item in (sample.get("items") or []) if "曲挠" in str(item.get("item", ""))]
        kept_items: list[dict[str, Any]] = []
        for item in sample.get("items") or []:
            if "曲挠" not in str(item.get("item", "")):
                kept_items.append(item)
                continue
            cleaned_item = _strip_flexing_from_combined_entry(item)
            if cleaned_item:
                kept_items.append(cleaned_item)
        sample["items"] = kept_items
        flex_checks = [
            check for check in (sample.get("checks") or [])
            if "曲挠" in f"{check.get('category', '')}{check.get('item', '')}"
        ]
        reported_values = [str(check.get("reported", "")) for check in flex_checks]
        reported_values.extend(str(item.get("reported", "")) for item in flex_items)
        states = [_not_applicable_report_state(value) for value in reported_values if value.strip()]
        state = (
            "performed" if "performed" in states else
            "not_done" if "not_done" in states else
            "unknown"
        )
        source_state, source_excerpt = _source_applicability_state(sample, source_text, "flexing")
        if source_state != "unknown":
            state = source_state
            reported_values.append(f"原页证据：{source_excerpt}")
        for check in sample.get("checks") or []:
            label = f"{check.get('category', '')}{check.get('item', '')}"
            if "曲挠" not in label:
                continue
            cleaned_check = _strip_flexing_from_combined_entry(check)
            if cleaned_check:
                check.clear()
                check.update(cleaned_check)
                continue
            check["verdict"] = {
                "performed": "fail", "not_done": "not_applicable", "unknown": "manual_review",
            }[state]
            check["required"] = f"{not_applicable_reason}，判N正确"
            check["basis"] = basis
            check["note"] = "程序已先判定截面适用怤5/5013或8735曲挠边界，未再套用滑轮和负载参数"
        joined_reported = "；".join(value for value in reported_values if value.strip()) or "未定位到明确P/N或实测结果"
        if state == "performed":
            sample.setdefault("items", []).append({
                "item": "单芯曲挠试验适用性" if cores == 1 else "超截面曲挠试验适用性",
                "reported": joined_reported,
                "should_be": f"{not_applicable_reason}，应判N/不适用",
                "standard": basis, "severity": "must_fix", "action_required": True,
                "action_type": "correction", "review_action": "删除不适用的曲挠试验结果并将相应项评定为N",
            })
        elif state == "unknown":
            sample.setdefault("items", []).append({
                "item": "单芯曲挠试验填写状态待复核" if cores == 1 else "超截面曲挠试验填写状态待复核",
                "reported": joined_reported,
                "should_be": "确认报告是否为N/未实施；证据不足不得判通过",
                "standard": basis, "severity": "suggestion", "action_required": True,
                "action_type": "manual_review", "review_action": "人工回看原PDF曲挠项P/N与结果列",
            })
        records.append({
            "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
            "cores": cores, "section_mm2": section,
            "status": "performed_but_not_applicable" if state == "performed" else "not_applicable" if state == "not_done" else "manual_review",
            "standard": basis,
        })
    if records:
        result.setdefault("_deterministic_validation", {})["rubber_flexing_applicability"] = records
    return result


def _require_manual_review_for_ycw_lowtemp_selection(
    result: dict[str, Any], standard_family: str | None,
) -> dict[str, Any]:
    """YCW 小截面护套低温弯曲/拉伸选择冲突只进入人工复核。

    报告表格把两个项目排在同一合并单元格中时，即使 MinerU 能读出
    “弯曲P、拉伸N”，也不能仅凭解析文本自动判定实验室漏做试验；需要
    审核员回看原表对应行和 GB/T 2951.14 的试样选择条件后再决定是否退回。
    """
    if standard_family != "rubber":
        return result
    for sample in result.get("samples") or []:
        model = str(sample.get("model", "")).upper().replace(" ", "")
        parsed = _spec_cores_area(sample)
        if ("60245IEC66" not in model and "YCW" not in model) or not parsed or parsed[1] > 16:
            continue
        lowtemp_items = [
            item for item in (sample.get("items") or [])
            if "低温" in str(item.get("item", ""))
            and any(marker in str(item.get("item", "")) for marker in ("弯曲", "拉伸"))
        ]
        if lowtemp_items:
            sample["items"] = [item for item in (sample.get("items") or []) if item not in lowtemp_items]
            reported = "；".join(str(item.get("reported", "")) for item in lowtemp_items if item.get("reported"))
            sample["items"].append({
                "item": "护套低温弯曲/低温拉伸项目选择",
                "reported": reported or "解析文本中的低温弯曲/拉伸P-N对应关系需核实",
                "should_be": "人工回看原PDF中低温弯曲/低温拉伸对应行，并按试样选择条件确认P/N是否正确",
                "standard": "GB/T 5013.4-2008及GB/T 2951.14-2008",
                "severity": "suggestion", "action_required": True,
                "action_type": "manual_review",
                "review_action": "核对原PDF表格行、成品外径及GB/T 2951.14试样选择条件，确认应做低温弯曲还是低温拉伸",
            })
        for check in sample.get("checks") or []:
            label = f"{check.get('category', '')}{check.get('item', '')}"
            if "低温" in label and "弯曲" in label and "拉伸" in label and check.get("verdict") == "fail":
                check["verdict"] = "manual_review"
                check["note"] = "解析结果显示弯曲P、拉伸N；合并表格存在行错位风险，须回看原PDF后再下修改结论"
    return result


def _suppress_extra_passed_test_issues(result: dict[str, Any]) -> dict[str, Any]:
    """标准未要求但报告加做且判P的试验不构成修改或复核项。"""
    for sample in result.get("samples") or []:
        removed: list[dict[str, Any]] = []
        for item in sample.get("items") or []:
            label = str(item.get("item", ""))
            if "低温" in label:
                continue
            reported = str(item.get("reported", ""))
            expected = str(item.get("should_be", ""))
            standard = str(item.get("standard", ""))
            says_not_required = bool(re.search(
                r"(?:\bN\b|不适用|不要求|未列)",
                reported + " " + expected + " " + standard,
                re.I,
            ))
            passed = bool(re.search(
                r"(?:(?:^|[^A-Z])P(?:$|[^A-Z])|合格|通过|无裂纹|未击穿)",
                reported,
                re.I,
            ))
            if says_not_required and passed:
                removed.append(item)
        if not removed:
            continue
        sample["items"] = [item for item in (sample.get("items") or []) if item not in removed]
        for removed_item in removed:
            item_label = re.sub(r"\W+", "", str(removed_item.get("item", "")))
            for check in sample.get("checks") or []:
                check_label = re.sub(r"\W+", "", f"{check.get('category', '')}{check.get('item', '')}")
                if item_label and (item_label in check_label or check_label in item_label):
                    check["verdict"] = "pass"
                    check["note"] = "标准未要求但报告加做且结果合格，不构成修改或人工复核项"
    return result


_CONFIRMED_FLEXING_SETUPS = (
    # (model marker, cores, section, current A, voltage V, pulley mm, weight kg)
    ("60245IEC57", 4, 2.5, 20.0, 400.0, 160.0, 2.5),
    ("YZ", 5, 0.75, 6.0, 400.0, 80.0, 1.0),
    ("YZWB", 2, 0.75, 9.0, 230.0, 80.0, 1.0),
)


def _confirmed_flexing_setup(sample: dict[str, Any]) -> tuple[float, float, float, float] | None:
    model = re.sub(r"[\s()（）-]", "", str(sample.get("model", "")).upper())
    parsed = _spec_cores_area(sample)
    if not parsed:
        return None
    cores, section = parsed
    for marker, expected_cores, expected_section, current, voltage, pulley, weight in _CONFIRMED_FLEXING_SETUPS:
        if marker == "YZ" and ("YZWB" in model or "YZW" in model or "60245IEC53" not in model):
            continue
        if marker not in model:
            continue
        if cores == expected_cores and abs(section - expected_section) <= 0.001:
            return current, voltage, pulley, weight
    return None


def _source_impact_mass(source_text: str) -> tuple[int, bool] | None:
    """从当前样品原文中取低温冲击落锤和P评定。"""
    for heading in re.finditer(r"低温冲击试验(?:\s*\|)?", source_text):
        tail = source_text[heading.start():heading.start() + 900]
        mass_match = re.search(r"落锤(?:重量|质量)(?:\s*\|)?\s*(\d+)(?:\s*\|)?\s*g", tail, re.I)
        if not mass_match:
            continue
        passed = bool(re.search(r"(?:^|[^A-Z])P(?:$|[^A-Z])", tail, re.I))
        return int(mass_match.group(1)), passed
    return None


def _confirmed_number_pattern(value: float) -> str:
    """匹配数值等价写法，例如1、1.0和1.00。"""
    if float(value).is_integer():
        return rf"{int(value)}(?:\.0+)?"
    return re.escape(f"{value:g}")


def _is_single_lowtemp_bend_check(check: dict[str, Any]) -> bool:
    """Local bend corrections must never replace a merged project or component row."""
    item = re.sub(r"\s+", "", str(check.get("item") or ""))
    if not re.fullmatch(r"(?:绝缘|护套)?低温弯曲(?:试验)?", item):
        return False
    context = " ".join(str(check.get(key) or "") for key in ("category", "item", "reported"))
    if "绝缘" in context and "护套" in context:
        return False
    return not re.search(r"老化|高温压力|热冲击|失重|抗张|热收缩|低温冲击", context)


def _apply_confirmed_false_positive_guards(
    result: dict[str, Any],
    source_text: str,
    standard_family: str | None,
) -> dict[str, Any]:
    """按原页证据和已确认业务口径压制稳定误报。

    不按报告ID写特判；只使用型号/规格、原页P/N、参数和已确认规则。
    无完整证据时保留原警报。
    """
    from backend.app.condition_rules import FALSE
    from backend.app.rulebase import (
        _dimension_source_evidence, _lowtemp_heading_verdict, _plain_table_text,
        _sheath_lowtemp_bend_source_state,
        source_group_for_sample, source_sample_registry,
    )

    source_registry = source_sample_registry(source_text)
    global_plain_source = _plain_table_text(source_text)
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        group = source_group_for_sample(sample, registry=source_registry)
        plain_source = " ".join(
            _plain_table_text(str(page.get("text") or ""))
            for page in ((group or {}).get("pages") or [])
        )
        modified_report_declared = bool(
            re.search(r"原报告.{0,100}修改报告", global_plain_source, re.I)
            and re.search(r"原报告.{0,260}(?:报告)?作废", global_plain_source, re.I)
        )
        original_issue_match = re.search(
            r"原报告\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:发布|签发|发放)",
            global_plain_source,
            re.I,
        )
        original_issue_date = (
            tuple(map(int, original_issue_match.groups())) if original_issue_match else None
        )
        model = re.sub(r"[\s()（）]", "", str(sample.get("model", "")).upper())
        flame_retardant_model = bool(re.match(r"Z[BCD]", model))
        spec_numbers = re.findall(r"\d+(?:\.\d+)?", str(sample.get("spec") or ""))
        rvvp_area = (
            float(spec_numbers[-1])
            if len(spec_numbers) >= 2 and any(marker in model for marker in ("RVVP", "RVVPS"))
            else None
        )
        flexing_setup = _confirmed_flexing_setup(sample) if standard_family == "rubber" else None
        flexing_evidence = " ".join(
            [plain_source]
            + [
                " ".join(str(check.get(field, "")) for field in ("item", "reported", "required", "basis"))
                for check in sample.get("checks") or []
                if "曲挠" in f"{check.get('category', '')}{check.get('item', '')}"
            ]
        )
        confirmed_flexing = False
        if flexing_setup:
            current, voltage, pulley, weight = flexing_setup
            current_pattern = _confirmed_number_pattern(current)
            voltage_pattern = _confirmed_number_pattern(voltage)
            pulley_pattern = _confirmed_number_pattern(pulley)
            weight_pattern = _confirmed_number_pattern(weight)
            confirmed_flexing = all((
                bool(re.search(rf"施加电流\D{{0,12}}{current_pattern}\s*(?:\|\s*)?A", flexing_evidence, re.I)),
                bool(re.search(rf"施加电压\D{{0,12}}{voltage_pattern}\s*(?:\|\s*)?V", flexing_evidence, re.I)),
                bool(re.search(rf"滑轮直径\D{{0,12}}{pulley_pattern}\s*(?:\|\s*)?mm", flexing_evidence, re.I)),
                bool(re.search(rf"重锤重量\D{{0,12}}{weight_pattern}\s*(?:\|\s*)?kg", flexing_evidence, re.I)),
            ))

        welding_cable_model = bool(re.search(r"60245IEC(?:81|82)", model))
        welding_static_flexing_pass = bool(
            welding_cable_model
            and re.search(
                r"静态曲挠试验.{0,260}?(?:最大\s*(?:\|\s*)?45|45\s*(?:\|\s*)?(?:cm)?).{0,180}?"
                r"(?:33\s*(?:\|\s*)?P|P\s*(?:\|\s*)?33)",
                global_plain_source,
                re.I | re.S,
            )
        )
        source_confirms_insulated_core_2500 = bool(
            re.search(
                r"绝缘线芯电压试验\s*\(?\s*2500\s*V\s*[,，]?\s*5\s*min\s*\)?"
                r".{0,180}?不击穿.{0,180}?(?:^|[^A-Z])P(?:$|[^A-Z])",
                plain_source,
                re.I | re.S,
            )
        )
        heat_shrink_source_n = bool(re.search(
            r"热收缩试验.{0,360}?(?:——|[-—－]).{0,80}?(?:^|[^A-Z])N(?:$|[^A-Z])",
            plain_source,
            re.I | re.S,
        ))

        lowtemp_tensile_pass = bool(re.search(
            r"低温拉伸试验[^P]{0,550}(?:\d+(?:\.\d+)?\s*\|\s*)P\s*(?:\||$)",
            plain_source,
            re.I,
        ))
        lowtemp_tensile_hours_match = re.search(
            r"低温拉伸试验.{0,520}?时间\s*(?:\|\s*)?(\d+)\s*(?:\|\s*)?h",
            plain_source,
            re.I | re.S,
        )
        lowtemp_tensile_hours = int(lowtemp_tensile_hours_match.group(1)) if lowtemp_tensile_hours_match else None
        bvv_blvv_selection = (model == "BVV" or "BLVV" in model) and lowtemp_tensile_pass
        sheath_method_state = _sheath_lowtemp_bend_source_state(group)
        sheath_tensile_selected = sheath_method_state == FALSE
        bend_heading = re.search(r"低温弯曲试验", plain_source)
        tensile_heading = re.search(r"低温拉伸试验", plain_source)
        sheath_lowtemp_rows_all_blank = False
        if bend_heading and tensile_heading and bend_heading.start() < tensile_heading.start():
            bend_tail = plain_source[bend_heading.start():tensile_heading.start()]
            tensile_tail = plain_source[tensile_heading.start():tensile_heading.start() + 520]
            sheath_lowtemp_rows_all_blank = (
                _lowtemp_heading_verdict(plain_source, r"低温弯曲试验") == ""
                and _lowtemp_heading_verdict(plain_source, r"低温拉伸试验") == ""
                and bool(re.search(r"(?:^|\|)\s*[-—－]+\s*(?:\||$)", bend_tail))
                and bool(re.search(r"(?:^|\|)\s*[-—－]+\s*(?:\||$)", tensile_tail))
            )
        single_flame_match = re.search(
            r"单根阻燃性能(.{0,1200}?)(?:成束阻燃性能|以下空白|注\s*[:：])",
            plain_source,
            re.I | re.S,
        )
        single_flame_blank_on_source = False
        if single_flame_match:
            single_block = single_flame_match.group(1)
            blank_cells = re.findall(r"(?:^|\|)\s*[-—－]+\s*(?=\||$)", single_block)
            source_verdicts = re.findall(r"(?:^|[^A-Z])([PFN])(?:$|[^A-Z])", single_block, re.I)
            single_flame_blank_on_source = len(blank_cells) >= 4 and not source_verdicts

        # 业务口径：认证检测报告已给出单根垂直燃烧两个炭化高度并判P时，
        # 不强制在报告中重复展开方法标准的供火时间。仅抑制“未明示”类提示，
        # 已填供火时间但数值错误、高度超限或未判P仍须保留。
        single_flame_height_pass = False
        for check in sample.get("checks") or []:
            check_label = str(check.get("item", ""))
            check_reported = str(check.get("reported", ""))
            if "单根垂直燃烧" not in check_label or str(check.get("verdict", "")).lower() != "pass":
                continue
            upper_match = re.search(r"(?:起点|起始点)[^\d]{0,20}(\d+(?:\.\d+)?)", check_reported)
            lower_match = re.search(r"(?:向下延伸|下延伸)[^\d]{0,20}(\d+(?:\.\d+)?)", check_reported)
            if (
                upper_match and lower_match
                and float(upper_match.group(1)) > 50
                and float(lower_match.group(1)) <= 540
                and bool(re.search(r"(?:^|[^A-Z])P(?:$|[^A-Z])", check_reported, re.I))
            ):
                single_flame_height_pass = True
                break

        kept: list[dict[str, Any]] = []
        for item in sample.get("items") or []:
            label = str(item.get("item", ""))
            reported = str(item.get("reported", ""))
            expected = str(item.get("should_be", ""))
            combined = f"{label} {reported} {expected} {item.get('standard', '')}"
            reason = ""

            # 只把“报告值与应填值已经明确冲突”的项目升级为需修改。
            # 泛化的“请确认”仍不会因此变成阻断项。
            concrete_unit_typo = bool(
                re.search(r"(?<![A-Za-z])500\s*m(?!m)", reported, re.I)
                and re.search(r"500\s*mm", expected, re.I)
            )
            concrete_report_number_conflict = bool(
                "报告编号一致性" in label
                and len(set(re.findall(
                    r"(?<![A-Z0-9])\d{5}-A\d{9}(?![A-Z0-9])", reported.upper()
                ))) >= 2
                and re.search(r"不一致|应一致|前缀", combined)
            )
            quantity_values = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*m\b", reported, re.I)
            concrete_quantity_conflict = bool(
                "样品数量" in label
                and len(set(quantity_values)) >= 2
                and re.search(r"不一致|应一致|矛盾", combined)
            )
            concrete_model_conflict = bool(
                re.search(r"型号.*一致性|产品型号|结论.*型号|型号.*结论", label)
                and (
                    re.search(r"不一致|误写|错写|多写|应为|笔误", combined)
                    or (
                        "封面" in reported and "结论页" in reported
                        and re.search(r"应前后一致|应一致", expected)
                    )
                )
            )
            if any((
                concrete_unit_typo,
                concrete_report_number_conflict,
                concrete_quantity_conflict,
                concrete_model_conflict,
            )):
                item.update({
                    "severity": "must_fix",
                    "action_required": True,
                    "action_type": "correction",
                    "review_action": str(item.get("review_action") or "按已定位的冲突更正报告，并保持前后一致"),
                })

            blank_flame = bool(re.search(r"未填|空白|横线|检验结果\s*[-—]|结果\s*为?\s*[-—]", combined))
            droplet_blank_n_on_source = bool(re.search(
                r"燃烧滴落物.{0,180}?[-—](?:\s*\|)?\s*(?:判)?N(?:\s*\||\s|$)",
                plain_source,
                re.I | re.S,
            ))
            if "单根垂直燃烧" in label and (
                (flame_retardant_model and blank_flame)
                or (flame_retardant_model and single_flame_blank_on_source)
                or (blank_flame and bool(re.search(r"不适用|未列|应判N", expected, re.I)))
                or ("滴落物" in combined and bool(re.search(r"[-—].{0,16}(?:判)?N|N.{0,16}[-—]", combined, re.I)))
                or ("滴落物" in combined and droplet_blank_n_on_source)
            ):
                reason = "已确认阻燃填写口径：该横线/N不构成报告问题"
            elif (
                "单根垂直燃烧" in combined
                and "供火时间" in combined
                and single_flame_height_pass
                and bool(re.search(r"未填|未写|未明示|未提供|没有提供|缺失|需补|建议补", combined))
            ):
                reason = "已确认报告填写口径：炭化高度完整且判P即可，不强制明示供火时间"
            elif "成束阻燃" in label:
                time_match = re.search(r"熄灭时间\D{0,18}(\d+(?:\.\d+)?)\s*min\D{0,12}N", combined, re.I)
                height_pass = bool(re.search(r"最大炭化高度[^P]{0,80}P", combined, re.I))
                if time_match and float(time_match.group(1)) <= 60 and height_pass:
                    reason = "已确认口径：炭化高度判P且熄灭时间不超限，该行判N可接受"
                else:
                    # 固定布局：要求“m min / 最大2.5 — / 高度 / PN”。
                    # P对应炭化高度，N对应未单列熄灭时间；只在当前样品
                    # 原页的完整布局成立且高度不超过2.5m时消解。
                    block_match=re.search(
                        r"成束阻燃性能\s*[（(]?([ABC])类[）)]?.{0,700}?"
                        r"m\s*min.{0,260}?最大\s*(?:\|\s*)?2\.5\s*(?:\|\s*)?[-—－]+"
                        r".{0,180}?(\d+(?:\.\d+)?)\s*(?:\|\s*)?P\s*(?:\|\s*)?N",
                        plain_source,re.I|re.S)
                    if block_match and float(block_match.group(2))<=2.5:
                        reason='已确认口径：原页P对应炭化高度，N对应不单列熄灭时间；只记录高度判P可以接受'
            elif "报告编号一致性" in label and modified_report_declared:
                reason = "原页已明确声明新旧报告号关系及原报告作废，不构成编号矛盾"
            elif "报告编号一致性" in label:
                numbers = list(dict.fromkeys(re.findall(
                    r"[A-Z0-9]+(?:[-/][A-Z0-9]+){2,}", reported.upper()
                )))
                component_relation = False
                if "报告的组成" in global_plain_source and "产品一致性确认检验报告" in global_plain_source:
                    cores = [re.sub(r"-S$", "", value) for value in numbers]
                    component_relation = any(
                        child.startswith(parent + "-")
                        for parent in cores
                        for child in cores
                        if parent != child
                    )
                if component_relation:
                    reason = "报告组成表已明确登记主报告号与子检验报告号，不构成编号不一致"
            elif (
                "检测设备检定有效期" in label
                and modified_report_declared
                and original_issue_date is not None
            ):
                expiry_dates = [
                    tuple(map(int, groups))
                    for groups in re.findall(
                        r"(\d{4})[-./年]\s*(\d{1,2})[-./月]\s*(\d{1,2})日?",
                        reported,
                    )
                ]
                if expiry_dates and all(expiry >= original_issue_date for expiry in expiry_dates):
                    reason = "修改报告的完成日不是原试验日；设备有效期均覆盖原报告签发日"
            elif "完成日期" in label and "日期逻辑" in label:
                dates = [
                    tuple(map(int, groups))
                    for groups in re.findall(
                        r"(\d{4})\s*(?:年|[./-])\s*(\d{1,2})\s*(?:月|[./-])\s*(\d{1,2})日?",
                        reported,
                    )
                ]
                if len(dates) >= 2 and dates[0] <= dates[1]:
                    reason = "试验完成日不晚于主检/审核签署日，符合先完成试验再审核签署的正常流程"
            elif (
                "认证单元划分与型式试验送样规则" in label
                and not re.search(r"不符合|超出|超过|未覆盖|送样错误|型号错误", combined)
            ):
                reason = "认证单元/送样属独立抽样规则审核；未指出具体违规事实的泛化确认不阻断报告内容审核"
            elif "曲挠" in label and confirmed_flexing and bool(re.search(r"人工核对|需人工|需核对", expected)):
                reason = "型号规格与已确认曲挠参数完全匹配"
            elif "超截面曲挠" in label and welding_static_flexing_pass:
                reason = "60245 IEC 81/82电焊机电缆适用静态曲挠；原页两铅垂线距离33cm≤45cm且判P"
            elif "低温" in label and welding_cable_model:
                reason = "GB/T 5013.6-2008表2的60245 IEC 81/82试验项目不含低温试验，不应生成状态调节时间缺口"
            elif (
                "产品名称一致性" in label
                and re.search(r"60245IEC(?:81|82)", model)
                and "电焊机电缆" in reported
                and "电焊机电缆" in expected
            ):
                reason = "主报告使用认证产品类别名称，子报告使用YH/YHF样品名称；型号同为60245 IEC 81/82，不构成样品身份冲突"
            elif (
                "绝缘线芯电压" in label
                and bool(
                    re.search(r"60245IEC66", model)
                    or re.sub(r"[^A-Z0-9]", "", model).endswith(("YC", "YCW"))
                )
                and bool(re.search(r"2500\s*V", reported, re.I))
                and bool(re.search(r"2000\s*V", expected, re.I))
                and (source_confirms_insulated_core_2500 or "未击穿" in reported or standard_family == "rubber")
            ):
                reason = "JB/T 8735.2-2016表8明确规定YC/YCW绝缘线芯为2500V电压试验，不应按2000V分档"
            elif (
                rvvp_area is not None
                and rvvp_area > 0.4
                and "热收缩" in label
                and (
                    re.search(r"横线|判N|不适用|应为N|\bN\b", combined, re.I)
                    or heat_shrink_source_n
                )
                and not re.search(r"不合格|实测|超限|未做必做", combined)
            ):
                reason = "已确认业务条件：RVVP/RVVPS标称截面>0.4mm²时热收缩不适用，横线/N可接受"
            elif bvv_blvv_selection and "低温弯曲" in label:
                reason = "BVV/BLVV原页已实施低温拉伸并判P，弯曲/拉伸二选一"
            elif sheath_tensile_selected and "护套低温弯曲" in label:
                reason = "原页低温弯曲为横线/N且同页低温拉伸判P；按已确认口径采用拉伸方法"
            elif (
                sheath_lowtemp_rows_all_blank
                and label in {"护套低温弯曲", "护套低温拉伸"}
                and not re.search(r"不合格|超(?:过|出)|低于|矛盾|错误|\bF\b", combined, re.I)
            ):
                reason = "原页低温弯曲/拉伸均为横线；模型的重复方法建议不单独出项"
            elif (
                "护套低温弯曲" in label
                and "低温拉伸" in reported
                and (
                    bool(re.search(r"(?:^|\D)4\s*h", reported, re.I))
                    or lowtemp_tensile_hours == 4
                )
            ):
                reason = "原警报项目名称错配；护套低温拉伸空气冷却不少于4h符合规则61"
            elif (
                "外径测量" in label
                and bool(re.search(r"空|未填|横线|缺失", combined))
                and bool(re.search(
                    r"外径-平均外径\s*\|\s*mm\s*\|\s*.*?\|\s*(?:\|\s*)?"
                    r"\d+(?:\.\d+)?\s*\|\s*P",
                    plain_source,
                    re.I | re.S,
                ))
            ):
                reason = "当前样品原页同一平均外径项目已有实测值并判P；上下限分行空栏不构成缺测"
            elif label in {"绝缘厚度测量", "护套厚度测量", "外径测量"}:
                item_code = {
                    "绝缘厚度测量": "THICKNESS_MEAS",
                    "护套厚度测量": "SHEATH_THICKNESS_MEAS",
                    "外径测量": "OD_MEAS",
                }[label]
                dimension_evidence = _dimension_source_evidence({
                    "item_code": item_code,
                    "item_name": label,
                    "standard_no": str(item.get("standard") or ""),
                    "table_no": "",
                    "table_item_no": "",
                }, group)
                vague_recheck = bool(re.search(r"核对|确认|复核", str(item.get("review_action") or "") + expected))
                concrete_problem = bool(re.search(
                    r"不符合|超限|低于|高于|错误|矛盾|缺失|漏填|计算错误|判定错误",
                    combined,
                ))
                if dimension_evidence and dimension_evidence.get("verdict") == "pass" and vague_recheck and not concrete_problem:
                    reason = "原页尺寸实测值已由程序按限值核验且判P；泛化的重复确认建议无行动价值"

            if reason:
                records.append({
                    "sample": " ".join(str(sample.get(key, "")) for key in ("model", "spec")).strip(),
                    "item": label,
                    "reason": reason,
                })
            else:
                kept.append(item)
        sample["items"] = kept

        # “最大2.5— / 高度 / PN”的严格原页布局已经说明P属于炭化高度，
        # 不能只删提示却留下manual_review检查，否则后续一致性步骤会再次
        # 生成同一提示。
        bundle_match=re.search(
            r"成束阻燃性能\s*[（(]?([ABC])类[）)]?.{0,700}?m\s*min.{0,260}?"
            r"最大\s*(?:\|\s*)?2\.5\s*(?:\|\s*)?[-—－]+.{0,180}?"
            r"(\d+(?:\.\d+)?)\s*(?:\|\s*)?P\s*(?:\|\s*)?N",
            plain_source,re.I|re.S)
        if bundle_match and float(bundle_match.group(2))<=2.5:
            for check in sample.get('checks') or []:
                if ('成束阻燃' in str(check.get('item') or '')
                        and check.get('verdict')=='manual_review'
                        and set(check.get('source_pages') or [])):
                    check['verdict']='pass'
                    check['note']='程序按原页固定布局确认：P对应最大炭化高度，N对应未单列熄灭时间；只记录高度判P可以接受'

        if welding_cable_model:
            for check in sample.get("checks") or []:
                if "低温" not in f"{check.get('category', '')}{check.get('item', '')}":
                    continue
                check["verdict"] = "not_applicable"
                check["note"] = "GB/T 5013.6-2008表2的60245 IEC 81/82试验项目不含低温试验"

        if bvv_blvv_selection:
            for check in sample.get("checks") or []:
                # Sample-wide tensile P may belong to the other component.
                # Never overwrite component-bound facts or an existing result.
                if check.get('coverage_origin') == 'component_lowtemp_source_recovery' or check.get('verdict') in {'pass','fail'}:
                    continue
                if not _is_single_lowtemp_bend_check(check):
                    continue
                label = f"{check.get('category', '')}{check.get('item', '')}"
                if "低温弯曲" not in label:
                    continue
                check["verdict"] = "not_applicable"
                check["required"] = "按试样尺寸在低温弯曲/拉伸中二选一；原页已实施拉伸并判P"
                check["basis"] = "JB/T 8734.2-2016表8；GB/T 2951.14-2008；活动规则25"
                check["note"] = "程序按原页P/N配对复核，不再将二选一的弯曲N判为漏做"

        if sheath_tensile_selected:
            for check in sample.get("checks") or []:
                if not _is_single_lowtemp_bend_check(check):
                    continue
                label = f"{check.get('category', '')}{check.get('item', '')}"
                if "护套低温弯曲" not in label:
                    continue
                check["verdict"] = "not_applicable"
                check["required"] = "采用低温拉伸方法时，低温弯曲可填横线或判N"
                check["basis"] = "GB/T 2951.14-2008；已确认业务口径"
                check["note"] = "程序按原页配对确认：低温弯曲为横线/N，同页低温拉伸有实测结果并判P"

        for check in sample.get("checks") or []:
            label = f"{check.get('category', '')}{check.get('item', '')}"
            reported = str(check.get("reported", ""))
            if (
                "护套低温弯曲" in label
                and _is_single_lowtemp_bend_check(check)
                and "低温拉伸" in reported
                and (re.search(r"(?:^|\D)4\s*h", reported, re.I) or lowtemp_tensile_hours == 4)
            ):
                # 时间满足不等于温度、伸长率和方法选择全部满足，不自动改写整项结论。
                time_note = "低温拉伸时间单项核对：4h满足空气冷却不少于4h；不代替其他条件审核"
                if time_note not in str(check.get("note") or ""):
                    check["note"] = "；".join(filter(None, [str(check.get("note") or ""), time_note]))
                if lowtemp_tensile_hours == 4 and not re.search(r"(?:^|\D)4\s*h", reported, re.I):
                    check["reported"] = f"{reported.rstrip('； ')}；时间4h"

        source_impact = _source_impact_mass(plain_source)
        if source_impact and source_impact[1]:
            source_mass = source_impact[0]
            for check in sample.get("checks") or []:
                label = f"{check.get('category', '')}{check.get('item', '')}"
                if "低温冲击" not in label:
                    continue
                original_reported = str(check.get("reported", ""))
                recovered_reported = re.sub(
                    r"落锤\s*(?:重量)?\s*\d+\s*g",
                    f"落锤{source_mass}g",
                    original_reported,
                    flags=re.I,
                )
                if recovered_reported == original_reported and not re.search(r"落锤", recovered_reported):
                    recovered_reported = "；".join(
                        value for value in (recovered_reported.strip("； "), f"落锤{source_mass}g") if value
                    )
                check["reported"] = recovered_reported
                check["note"] = (
                    str(check.get("note") or "") +
                    f"；程序从当前样品原页恢复落锤{source_mass}g和P评定"
                ).strip("；")
                if check.get("verdict") != "fail":
                    # 原页已确定性恢复 P：manual_review 等未决判定同步收敛，
                    # 避免「恢复P评定」与「待人工复核」在同一条目并存。
                    check["verdict"] = "pass"
                stale_note = re.findall(
                    r"[^；]*(?:需人工复核是否漏做|需人工核对是否遗漏)[^；]*；?",
                    str(check.get("note") or ""),
                )
                if stale_note:
                    check["note"] = str(check.get("note") or "")
                    for seg in stale_note:
                        check["note"] = check["note"].replace(seg, "")
                    check["note"] = check["note"].strip("；")
            corrected_items: list[dict[str, Any]] = []
            for item in sample.get("items") or []:
                label = str(item.get("item", ""))
                if "低温冲击" not in label:
                    corrected_items.append(item)
                    continue
                item_text = " ".join(
                    str(item.get(key, ""))
                    for key in ("item", "reported", "should_be", "review_action")
                )
                n_reading = any(m in item_text for m in ("判N", "不适用", "未进行", "漏做"))
                discrepancy = any(
                    m in item_text for m in ("不符合", "错位", "未提取", "缺失", "冲突")
                )
                if item.get("severity") != "must_fix" and n_reading and not discrepancy:
                    records.append({
                        "sample": " ".join(str(sample.get(key, "")) for key in ("model", "spec")).strip(),
                        "item": label,
                        "reason": f"当前样品原页低温冲击结果为{source_mass}g、P；N解读为误读",
                    })
                    continue
                if "分段结论冲突" in label or "落锤重量" in label:
                    records.append({
                        "sample": " ".join(str(sample.get(key, "")) for key in ("model", "spec")).strip(),
                        "item": label,
                        "reason": f"当前样品原页唯一结果为{source_mass}g、P",
                    })
                    continue
                fixed_item = dict(item)
                fixed_item["reported"] = re.sub(
                    r"落锤\s*(?:重量)?\s*\d+\s*g",
                    f"落锤{source_mass}g",
                    str(fixed_item.get("reported", "")),
                    flags=re.I,
                )
                corrected_items.append(fixed_item)
            sample["items"] = corrected_items

    if records:
        result.setdefault("_deterministic_validation", {})["confirmed_false_positive_guards"] = records
    return result


def _enforce_minimum_thickness_formula(result: dict[str, Any]) -> dict[str, Any]:
    """按规定厚度确定性复算绝缘/护套最薄处限值。

    绝缘为规定值的90%-0.1mm，护套为规定值的85%-0.1mm。仅在同一样品
    的结构检查中能提取规定值、且报告标准要求与复算值一致时拦截AI误报。
    """
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        structure_text = " ".join(
            f"{check.get('reported', '')} {check.get('required', '')}"
            for check in sample.get("checks") or []
            if "结构" in f"{check.get('category', '')}{check.get('item', '')}"
        )
        kept: list[dict[str, Any]] = []
        for item in sample.get("items") or []:
            label = str(item.get("item", ""))
            material = "绝缘" if "绝缘" in label else "护套" if "护套" in label else ""
            if not material or "最薄" not in label or "标准" not in label:
                kept.append(item)
                continue
            nominal_match = re.search(
                rf"{material}平均厚度[^；;,，]*?(?:≥|规定(?:值|厚度)?\s*)\s*([0-9]+(?:\.[0-9]+)?)",
                structure_text,
            )
            reported_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(item.get("reported", "")))
            if not nominal_match or not reported_match:
                kept.append(item)
                continue
            nominal = float(nominal_match.group(1))
            reported = float(reported_match.group(1))
            factor = 0.90 if material == "绝缘" else 0.85
            calculated = round(nominal * factor - 0.1 + 1e-9, 2)
            if abs(reported - calculated) > 0.011:
                kept.append(item)
                continue
            records.append({
                "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                "material": material, "nominal_mm": nominal,
                "reported_requirement_mm": reported, "calculated_mm": calculated,
            })
            for check in sample.get("checks") or []:
                if material in f"{check.get('category', '')}{check.get('item', '')}" and "最薄" in f"{check.get('item', '')}{check.get('reported', '')}":
                    check["note"] = (
                        f"程序复算：{material}规定厚度{nominal:g}mm，最薄处限值="
                        f"{nominal:g}×{factor:.2f}-0.1={calculated:.2f}mm；报告要求值正确"
                    )
        sample["items"] = kept
    if records:
        result.setdefault("_deterministic_validation", {})["minimum_thickness"] = records
    return result


_RUBBER_FLEXING_TABLE1: dict[tuple[int, float], tuple[float, float]] = {
    # (芯数, 标称截面): (负重kg, 滑轮直径mm)
    (2, 0.75): (1.0, 80.0), (3, 0.75): (1.0, 80.0),
    (4, 0.75): (1.0, 80.0), (5, 0.75): (1.0, 80.0),
    (2, 1.0): (1.0, 120.0), (2, 1.5): (1.0, 120.0),
    (2, 2.5): (1.5, 120.0), (2, 4.0): (2.5, 160.0),
    (3, 1.0): (1.0, 120.0), (3, 1.5): (1.5, 120.0),
    (3, 2.5): (2.0, 160.0), (3, 4.0): (3.0, 160.0),
    (4, 1.0): (1.5, 120.0), (4, 1.5): (1.5, 160.0),
    (4, 2.5): (2.5, 160.0), (4, 4.0): (3.5, 200.0),
    (5, 1.0): (1.5, 120.0), (5, 1.5): (2.5, 160.0),
    (5, 2.5): (3.0, 160.0), (5, 4.0): (4.0, 200.0),
}

_RUBBER_FLEXING_TABLE2_CURRENT = {
    0.75: 6.0, 1.0: 10.0, 1.5: 14.0, 2.5: 20.0, 4.0: 25.0,
}

_JBT8735_FLEXING_TABLE5_CURRENT = {
    0.3: 2.5, 0.4: 2.5, 0.5: 5.0, 0.75: 9.0,
    1.0: 11.0, 1.5: 14.0, 2.5: 20.0, 4.0: 25.0,
}


def _rubber_flexing_table_requirement(sample: dict[str, Any]) -> dict[str, Any] | None:
    """按对应标准系列的曲挠表返回2～5芯曲挠条件。"""
    model = re.sub(r"[\s()（）-]", "", str(sample.get("model", "")).upper())
    if not any(marker in model for marker in ("60245IEC53", "60245IEC57", "60245IEC66", "YZ", "YZW", "YC", "YCW")):
        return None
    parsed = _spec_cores_area(sample)
    if not parsed:
        return None
    cores, area = parsed
    key = next((candidate for candidate in _RUBBER_FLEXING_TABLE1 if candidate[0] == cores and abs(candidate[1] - area) <= 0.001), None)
    is_iec_5013 = "60245IEC" in model
    current_table = _RUBBER_FLEXING_TABLE2_CURRENT if is_iec_5013 else _JBT8735_FLEXING_TABLE5_CURRENT
    current_key = next((candidate for candidate in current_table if abs(candidate - area) <= 0.001), None)
    if key is None or current_key is None:
        return None
    weight, pulley = _RUBBER_FLEXING_TABLE1[key]
    full_current = current_table[current_key]
    currents = [full_current]
    if cores in (4, 5):
        currents.append(full_current * math.sqrt(3.0 / cores))
    heavy_duty_product = bool(
        "60245IEC66" in model
        or (not is_iec_5013 and ("YCW" in model or re.search(r"(?:^|Z[ABCD])YC$", model)))
    )
    return {
        "cores": cores, "area": area, "weight": weight, "pulley": pulley,
        "currents": currents, "voltage": 230.0 if cores == 2 else 400.0,
        "immersion_voltage": 2000.0 if heavy_duty_product or cores == 2 or area > 1.0 else 1500.0,
        "basis": (
            "GB/T 5013.2-2008 表1、表2及3.1.5；GB/T 5013.4-2008对应产品试验表"
            if is_iec_5013
            else "JB/T 8735.1-2016 表4、表5及6.3.2；JB/T 8735.2-2016对应产品试验表"
        ),
    }


def _reported_flexing_number(text: str, label: str, unit: str) -> float | None:
    matched = re.search(rf"(?:{label})\D{{0,12}}(\d+(?:\.\d+)?)\s*{unit}\b", text, re.I)
    return float(matched.group(1)) if matched else None


def _flexing_parameter_candidates(evidence: str) -> dict[str, set[float]]:
    """Read labelled fields or an explicit A/V/mm/kg tuple; ambiguity stays unknown.

    Do not search for a bare voltage: the subsequent immersion voltage belongs
    to a separate test. Do not borrow missing components across tuple matches.
    """
    setup = re.split(r"曲挠(?:试验)?后(?:浸水)?电压", evidence, maxsplit=1)[0]
    patterns = {
        "current": r"(?:施加|负载)?电流\s*[:：]?\s*(\d+(?:\.\d+)?)\s*A\b",
        "voltage": r"(?:施加)?电压\s*[:：]?\s*(\d+(?:\.\d+)?)\s*V\b",
        "pulley": r"滑轮(?:直径)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*mm\b",
        "weight": r"重锤(?:重量|质量)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*kg\b",
    }
    values = {key: {float(v) for v in re.findall(pattern, setup, re.I)} for key, pattern in patterns.items()}
    number = r"(\d+(?:\.\d+)?)\s*"
    tuples = re.findall(r"曲挠(?:试验)?\s*[:：]?\s*" + number + r"A\s*/\s*" +
                        number + r"V\s*/\s*" + number + r"mm\s*/\s*" + number + r"kg\b", setup, re.I)
    for parts in tuples:
        for key, value in zip(patterns, parts):
            values[key].add(float(value))
    return values


def _flexing_parameter_values(evidence: str) -> dict[str, float | None]:
    return {key: next(iter(found)) if len(found) == 1 else None
            for key, found in _flexing_parameter_candidates(evidence).items()}


def _source_immersion_observations(group: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep requirement and result separate in bound post-flexing rows."""
    import hashlib
    from backend.app.rulebase import _plain_table_text
    observations = []
    for page in group.get('pages') or []:
        plain = _plain_table_text(str(page.get('text') or '')).replace('|', ' ')
        titles = list(re.finditer(r'曲挠(?:试验)?后浸水电压试验', plain))
        for index, title in enumerate(titles):
            preceding = plain[:title.start()]
            mechanical_titles = list(re.finditer(r'曲挠试验(?!后|机|仪)', preceding))
            mechanical_count = None
            if len(mechanical_titles) == 1:
                mechanical = re.sub(r'\s+', '', preceding[mechanical_titles[0].end():])
                mechanical_result = re.search(r'((?:通过)+)P$', mechanical)
                if mechanical_result:
                    mechanical_count = mechanical_result.group(1).count('通过')
            end = titles[index+1].start() if index+1 < len(titles) else len(plain)
            tail = plain[title.end():end]
            tail = re.split(r'以下空白|注\s*[:：]|--- Page|燃\s*烧\s*性\s*能|单根阻燃性能|成束阻燃性能', tail, maxsplit=1)[0]
            compact = re.sub(r'\s+', '', tail)
            condition = re.match(r'[（(](\d+(?:\.\d+)?)V[,，/](\d+(?:\.\d+)?)min[)）]', compact, re.I)
            values = [float(v) for v in condition.groups()] if condition else None
            result = compact[condition.end():] if condition else ''
            # Only this narrow, complete row grammar can supply a missing
            # model outcome. A requirement alone or a stray P is insufficient.
            complete = bool(re.fullmatch(r'不击穿(?:(?:未击穿)+|均未击穿)P', result, re.I))
            failure = bool(re.fullmatch(r'不击穿(?:(?:均未击穿|未击穿|发生击穿|已击穿|击穿|不合格|未通过)+)[PF]', result, re.I)
                           and not complete)
            binding = page.get('_cross_layer_binding') or {}
            layer = binding.get('layer')
            verified_binding = (binding if layer in ('native', 'supplement')
                                and hashlib.sha256(str(page.get('text') or '').encode()).hexdigest()
                                == binding.get(layer + '_sha256') else {})
            observations.append({'page': page.get('page'), 'conditions': values,
                'complete_pass': complete and len(titles) == 1, 'explicit_failure': failure,
                'excerpt': tail.strip()[:700], 'unique_row': len(titles) == 1,
                'preceding_flexing_pass_count': mechanical_count,
                'cross_layer_binding': verified_binding})
    for observation in observations:
        binding = observation['cross_layer_binding']
        if observation['complete_pass'] or not observation['unique_row'] or binding.get('layer') != 'supplement':
            continue
        peers = [o for o in observations if o['complete_pass']
                 and o['cross_layer_binding'].get('layer') == 'native'
                 and all(o['cross_layer_binding'].get(k) == binding.get(k)
                         for k in ('inspection_number','page','native_sha256','supplement_sha256'))
                 and o['conditions'] == observation['conditions']]
        if len(peers) != 1:
            continue
        damaged = re.sub(r'\s+', '', observation['excerpt'])
        intact = re.sub(r'\s+', '', peers[0]['excerpt'])
        # The terminal immersion subrow must itself corroborate all voltage
        # results. Mechanical positive-cell counts are separate provenance,
        # not the number of immersion specimens. Keep that difference visible.
        immersion_tail = re.search(r'((?:未击穿)+)PP$', damaged)
        clean = re.sub(r'既不发生电流断路[，,、。.]?也不发生导体间(?:以及试样和滑轮间的)?短路[。.]?', '', damaged)
        clean = re.sub(r'(?:不|未)(?:得|应)?发生(?:电流|导体间)?(?:断路|短路|击穿)|未击穿|不击穿', '', clean)
        if (damaged.count('未击穿') == intact.count('未击穿') > 0
                and peers[0].get('preceding_flexing_pass_count') is not None
                and damaged.count('通过') > 0
                and immersion_tail is not None
                and immersion_tail.group(1).count('未击穿') == intact.count('未击穿')
                and '在试验期间经15000次往复运动后' in damaged
                and not re.search(r'击穿|不合格|未通过|F|断路|短路', clean, re.I)):
            observation['resolved_duplicate_source'] = 'native_same_specimen_page_complete_immersion_row'
            observation['mechanical_result_count_comparison'] = {
                'native': peers[0]['preceding_flexing_pass_count'],
                'supplement': damaged.count('通过'),
                'scope': 'Only the complete immersion subrow is reconciled; mechanical evidence is unchanged'}
    return observations


def _source_flexing_parameters(sample: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Read only uniquely bound sample trial sections, never the equipment list."""
    from backend.app.rulebase import source_group_for_sample, _plain_table_text
    group = source_group_for_sample(sample, source_text)
    if not group:
        return {"status": "unbound", "values": {}, "excerpts": []}
    plain = _plain_table_text(str(group.get("text") or "")).replace("|", " ")
    values = {key: set() for key in ("current", "voltage", "pulley", "weight")}
    excerpts = []
    failure_excerpts = []
    for match in re.finditer(r"(?<!后)曲挠试验(?!后|机|仪)", plain):
        block = plain[match.end():match.end() + 1000]
        block = re.split(r"曲挠(?:试验)?后|浸水电压|铜皮软线|注\s*[:：]|--- Page", block, maxsplit=1)[0]
        if "施加电流" not in block or "滑轮" not in block or "重锤" not in block:
            continue
        parsed = _flexing_parameter_candidates(block)
        for key, found in parsed.items():
            values[key].update(found)
        if any(parsed.values()):
            excerpts.append(re.sub(r"\s+", " ", block).strip()[:500])
            if _flexing_explicit_failure(block):
                failure_excerpts.append(re.sub(r"\s+", " ", block).strip()[:1000])
    conflicts = [key for key, found in values.items() if len(found) > 1]
    immersion = sorted({(float(v), float(t)) for v, t in re.findall(
        r"曲挠(?:试验)?后浸水电压试验\s*[（(]?\s*(\d+)\s*V\s*[,，/]\s*(\d+(?:\.\d+)?)\s*min",
        plain, re.I)})
    return {"status": "conflict" if conflicts else "located" if excerpts else "not_located",
            "values": {key: next(iter(found)) if len(found) == 1 else None for key, found in values.items()},
            "conflicts": conflicts, "excerpts": list(dict.fromkeys(excerpts)),
            "failure_excerpts": list(dict.fromkeys(failure_excerpts)),
            "immersion_observations": _source_immersion_observations(group),
            "immersion_conditions": immersion}


def _flexing_explicit_failure(text: str) -> bool:
    # Remove negative descriptions of failures before looking for affirmative
    # failure evidence. Requirements such as 不发生电流断路 are not failures.
    clean = re.sub(r'(?:不|未)(?:得|应)?发生(?:电流|导体间)?(?:断路|短路|击穿)|未击穿|不击穿', '', re.sub(r'\s+', '', text))
    return bool(re.search(r'不合格|未通过|发生(?:电流|导体间)?(?:断路|短路|击穿)|已击穿|(?:^|[^A-Z])F(?:$|[^A-Z])', clean, re.I))


def _enforce_rubber_flexing_setup(result: dict[str, Any], standard_family: str | None, source_text: str = "") -> dict[str, Any]:
    """以标准表格确定性复核橡皮软电缆曲挠参数，不让模型自行类推。"""
    if standard_family != "rubber":
        return result
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        requirement = _rubber_flexing_table_requirement(sample)
        if not requirement:
            continue
        checks = [check for check in sample.get("checks") or [] if "曲挠" in f"{check.get('category', '')}{check.get('item', '')}"]
        evidence = "；".join(str(check.get("reported", "")) for check in checks)
        if not evidence:
            continue
        reported = _flexing_parameter_values(evidence)
        source_parameters = _source_flexing_parameters(sample, source_text) if source_text else {}
        source_failures = source_parameters.get('failure_excerpts') or []
        outcome_failure = _flexing_explicit_failure(evidence) or bool(source_failures)
        if source_failures:
            evidence += '；原页明确失败证据：' + '；'.join(source_failures)
            for check in checks:
                if re.fullmatch(r'曲挠(?:试验)?后(?:浸水)?电压(?:试验)?', str(check.get('item') or '').strip()):
                    continue
                check.setdefault('model_reported_before_source_recovery', check.get('reported') or '')
                check['reported'] = '原页曲挠试验明确失败：' + '；'.join(source_failures)
                check['reported_value'] = check['reported']
                check['source_failure_evidence'] = list(source_failures)
                check['coverage_origin'] = 'source_flexing_failure_recovery'
        for key, value in (source_parameters.get("values") or {}).items():
            if key in source_parameters.get("conflicts", []):
                reported[key] = None
            elif value is not None:
                reported[key] = value
        if any(value is None for value in reported.values()):
            # A result explicitly N is handled by the applicability matrix.
            # For performed tests, unavailable input must not preserve a model pass.
            if outcome_failure:
                for check in checks:
                    check['verdict'] = 'fail'
                origin = 'rubber_flexing_setup:explicit_failure'
                sample['items'] = [i for i in sample.get('items') or [] if i.get('validation_origin') != origin]
                sample['items'].append({'item':'曲挠试验失败结果', 'reported':evidence,
                    'should_be':'参数缺失不消除已报告的失败结果', 'standard':requirement['basis'],
                    'severity':'must_fix', 'action_required':True, 'action_type':'correction',
                    'validation_origin':origin, 'review_action':'核实并处理已报告的断路、短路、击穿或失败结果'})
            if _not_applicable_report_state(evidence) != "not_done":
                labels = {"current": "电流", "voltage": "电压", "pulley": "滑轮直径", "weight": "重锤质量"}
                missing = [labels[key] for key, value in reported.items() if value is None]
                origin = "rubber_flexing_setup:missing_input"
                sample["items"] = [i for i in sample.get("items") or [] if i.get("validation_origin") != origin]
                sample["items"].append({
                    "item": "曲挠试验参数证据不完整", "reported": evidence,
                    "should_be": "须有同一样品的电流、电压、滑轮直径和重锤质量才能完成参数核验",
                    "standard": requirement["basis"], "severity": "suggestion",
                    "action_required": True, "action_type": "manual_review",
                    "review_action": "核对原页并补齐或澄清：" + "、".join(missing),
                    "validation_origin": origin,
                })
                for check in checks:
                    if check.get("verdict") == "pass":
                        check["verdict"] = "manual_review"
                records.append({"sample": str(sample.get("model")) + " " + str(sample.get("spec")),
                                "reported": reported, "required": requirement, "matched": None,
                                "missing_fields": missing})
            continue
        matches = {
            "current": any(abs(float(reported["current"]) - value) <= 0.06 for value in requirement["currents"]),
            "voltage": abs(float(reported["voltage"]) - requirement["voltage"]) <= 0.01,
            "pulley": abs(float(reported["pulley"]) - requirement["pulley"]) <= 0.01,
            "weight": abs(float(reported["weight"]) - requirement["weight"]) <= 0.01,
        }
        needs_immersion = any("浸水" in str(check.get("item", "")) for check in checks)
        immersion_pass = not needs_immersion or bool(re.search(
            rf"{int(requirement['immersion_voltage'])}\s*V?\s*[/，,]\s*5\s*min.{{0,30}}(?:未击穿|P)",
            evidence,
            re.I,
        )) or bool(re.search(
            rf"曲挠(?:试验)?后浸水电压.{{0,80}}{int(requirement['immersion_voltage'])}\s*V.{{0,50}}(?:未击穿|P)",
            evidence,
            re.I,
        ))
        source_immersion = source_parameters.get("immersion_conditions") or []
        if len(source_immersion) == 1:
            immersion_pass = (source_immersion[0] == (requirement["immersion_voltage"], 5.0)
                               and bool(re.search(r"未击穿|不击穿", evidence)))
        immersion_known_failure = (
            len(source_immersion) == 1
            and source_immersion[0] != (requirement["immersion_voltage"], 5.0)
        ) or bool(re.search(r"曲挠(?:试验)?后.{0,100}(?:发生击穿|已击穿|不合格)", evidence))
        immersion_observations = source_parameters.get('immersion_observations') or []
        if immersion_observations:
            expected = [requirement['immersion_voltage'], 5.0]
            immersion_known_failure = immersion_known_failure or any(
                o['explicit_failure'] or (o['conditions'] is not None and o['conditions'] != expected)
                for o in immersion_observations)
            active_immersion = [o for o in immersion_observations if not o.get('resolved_duplicate_source')]
            immersion_pass = bool(active_immersion) and all(o['complete_pass'] and o['conditions'] == expected
                                 for o in active_immersion) and not immersion_known_failure
        confirmed_failure = outcome_failure or immersion_known_failure or not all(matches.values())
        correct = all(matches.values()) and immersion_pass and not confirmed_failure
        currents_text = "或".join(f"{value:.2f}".rstrip("0").rstrip(".") + "A" for value in requirement["currents"])
        required_text = (
            f"{requirement['cores']}芯×{requirement['area']:g}mm²：电流{currents_text}、"
            f"电压{requirement['voltage']:g}V、滑轮直径{requirement['pulley']:g}mm、"
            f"重锤{requirement['weight']:g}kg；曲挠后{requirement['immersion_voltage']:g}V/5min不击穿"
        )
        for check in checks:
            if immersion_observations:
                check['source_immersion_observations'] = immersion_observations
            check["required"] = required_text
            check["basis"] = requirement["basis"]
            check["verdict"] = "fail" if confirmed_failure else "pass" if correct else "manual_review"
            # Separate post-flexing voltage checks do not inherit a mechanical
            # break/short failure from the preceding flexing test.
            if re.fullmatch(r'曲挠(?:试验)?后(?:浸水)?电压(?:试验)?', str(check.get('item') or '').strip()):
                voltage_failure = immersion_known_failure or _flexing_explicit_failure(str(check.get('reported') or ''))
                check['verdict'] = 'fail' if voltage_failure else 'pass' if immersion_pass else 'manual_review'
                check['required'] = f"曲挠后{requirement['immersion_voltage']:g}V/5min不击穿"
            check["note"] = "程序按电缆类型、芯数和标称截面逐格映射标准表，不使用相邻规格类推。"
        sample["items"] = [
            item for item in (sample.get("items") or [])
            if not (
                "曲挠" in str(item.get("item", ""))
                and not _flexing_explicit_failure(' '.join(str(item.get(field) or '') for field in ('reported','review_action')))
                and any(marker in " ".join(str(item.get(field, "")) for field in ("item", "reported", "should_be"))
                        for marker in ("滑轮", "重锤", "电流", "电压", "参数"))
            )
        ]
        if not correct:
            wrong_fields = [name for name, matched in matches.items() if not matched]
            if not immersion_pass:
                wrong_fields.append("immersion")
            if outcome_failure:
                wrong_fields.append('outcome')
            label_map = {"current": "电流", "voltage": "电压", "pulley": "滑轮直径", "weight": "重锤", "immersion": "曲挠后浸水电压", "outcome": "已报告的断路、短路、击穿或失败结果"}
            sample["items"].append({
                "item": "曲挠试验参数", "reported": evidence, "should_be": required_text,
            "standard": requirement["basis"],
                "severity": "must_fix" if confirmed_failure else "suggestion", "action_required": True,
                "action_type": "correction" if confirmed_failure else "manual_review",
                "review_action": ("核对并更正：" if confirmed_failure else "补充原页证据后判断：")
                                 + "、".join(label_map[name] for name in wrong_fields),
            })
        records.append({
            "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
            "reported": reported, "required": requirement, "matched": correct,
            "source_parameters": source_parameters,
        })
    if records:
        result.setdefault("_deterministic_validation", {})["rubber_flexing_setup"] = records
    return result


def _enforce_non_pollution_applicability(result: dict[str, Any], standard_family: str | None) -> dict[str, Any]:
    """GB/T 5023.5 表10：60227 IEC 53(RVV) 项次5非污染试验为T。"""
    if standard_family != "pvc":
        return result
    for sample in result.get("samples") or []:
        model = str(sample.get("model", "")).upper().replace(" ", "")
        if "60227IEC53" not in model:
            continue
        checks = [check for check in sample.get("checks") or [] if "非污染" in f"{check.get('category', '')}{check.get('item', '')}"]
        for check in checks:
            reported = str(check.get("reported", ""))
            applicability = "适用（T）；按GB/T 2951.12-2008 8.1.4进行"
            previous_required = str(check.get('required') or '')
            if applicability not in previous_required:
                check['required'] = '；'.join(v for v in (previous_required, applicability) if v)
            check['applicability_basis'] = "GB/T 5023.5-2008 表10 项次5"
            negative = bool(re.search(r"不合格|未通过|不通过|不符合|超限|(?:^|[^A-Z])F(?:$|[^A-Z])", reported, re.I))
            positive = bool(re.search(r"(?:^|[^A-Z])P(?:$|[^A-Z])|通过|合格", reported, re.I))
            if negative:
                check['verdict'] = 'fail'
            elif positive and check.get('verdict') not in ('fail', 'manual_review'):
                check["verdict"] = "pass"
            note = "表10项次5为T，仅确认项目适用性；试验结果与条件须独立核查"
            if note not in str(check.get('note') or ''):
                check['note'] = '；'.join(v for v in (str(check.get('note') or ''), note) if v)
        sample["items"] = [
            item for item in (sample.get("items") or [])
            if not ("非污染" in str(item.get("item", "")) and str(item.get("should_be", "")).strip().upper() == "N")
        ]
    return result


_NUMERIC_ROW = re.compile(r"^[+\-]?\d+(?:\.\d+)?(?:\s+[+\-]?\d+(?:\.\d+)?)*\s*$")


def _numeric_values_after(lines: list[str], heading_index: int, requirement_marker: str) -> list[float]:
    requirement_seen = False
    for line in lines[heading_index + 1:heading_index + 14]:
        stripped = line.strip()
        if re.search(r"老化|试验|中间值|变化率|^--- Page", stripped) or stripped in {"P", "F", "N", "—", "--"}:
            break
        if requirement_marker in stripped:
            requirement_seen = True
            continue
        if requirement_seen and _NUMERIC_ROW.fullmatch(stripped):
            return [float(value) for value in re.findall(r"[+\-]?\d+(?:\.\d+)?", stripped)]
    return []


def _round_change(value: float) -> int:
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def _middle_value(values: list[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _sample_source_blocks(text: str) -> list[str]:
    pages = [part for part in re.split(r"(?=^--- Page \d+[^\n]*---\s*$)", text, flags=re.M) if part.strip()]
    starts = [index for index, page in enumerate(pages) if re.search(r"样品\s*名称\s*[:：]", page)]
    if not starts:
        # 部分模板只在每个子报告首页重复“试样型号和规格”。
        starts = [index for index, page in enumerate(pages) if re.search(r"试样型号\s*\n?和规格", page)]
    return [
        "".join(pages[start:(starts[index + 1] if index + 1 < len(starts) else len(pages))])
        for index, start in enumerate(starts)
    ]


def _vertical_aging_elongation_groups(block: str, *, include_ambiguous: bool = False) -> list[dict[str, Any]]:
    """Read explicit same-page median rows; never infer material by row order."""
    groups = []
    for match in re.finditer(r'^--- Page (\d+) \(text layer\) ---\s*\n(.*?)(?=^--- Page |\Z)', block, re.M | re.S):
        page, text = int(match.group(1)), match.group(2)
        compact = re.sub(r'\s+', '', text)
        components = [c for c in ('绝缘', '护套') if c + '机械性能' in compact]
        if len(components) != 1:
            continue
        lines = [re.sub(r'\s+', '', line) for line in text.splitlines() if line.strip()]
        def row(title, marker):
            indices = [i for i, line in enumerate(lines) if line == title]
            # Multiple aging methods on one page are separated below, not guessed.
            if len(indices) != 1:
                return None
            i = indices[0] + 1
            if i < len(lines) and lines[i] == '%': i += 1
            if i >= len(lines) or lines[i] != marker: return None
            i += 1
            if i >= len(lines) or not re.fullmatch(r'(?:±)?\d+(?:\.\d+)?', lines[i]): return None
            limit = lines[i]; i += 1
            values = []
            while i < len(lines) and re.fullmatch(r'[+-]?\d+(?:\.\d+)?', lines[i]):
                values.append(lines[i]); i += 1
            if not values or i >= len(lines) or lines[i] not in ('P', 'F'): return None
            return {'values':values, 'limit':limit, 'evaluation':lines[i]}
        # Scope to original + first oven aging, before air-bomb/oil methods.
        has_oven_heading = any('空气烘箱老化' in line for line in lines)
        ends = [i for i,line in enumerate(lines) if '空气弹老化' in line or '浸矿物油' in line]
        if ends: lines = lines[:min(ends)]
        oven_indices = [i for i,line in enumerate(lines) if '空气烘箱老化' in line]
        if not has_oven_heading: continue
        titles = ('老化前断裂伸长率-中间值', '老化后断裂伸长率-中间值',
                  '老化前后断裂伸长率变化率')
        positions = [[i for i,line in enumerate(lines) if line == title] for title in titles]
        if any(len(indices) != 1 for indices in positions): continue
        before = row('老化前断裂伸长率-中间值', '最小')
        after = row('老化后断裂伸长率-中间值', '最小')
        change = row('老化前后断裂伸长率变化率', '最大')
        if not all((before,after,change)): continue
        if len({len(r['values']) for r in (before,after,change)}) != 1: continue
        ambiguity = ('空气烘箱老化试验标题落在后续其他试验段，无法与当前结果可靠对应' if not oven_indices else
                     '同页存在多个空气烘箱老化试验标题' if len(oven_indices) != 1 else
                     '老化前值、烘箱试验标题、老化后值及变化率的先后顺序不一致'
                     if not positions[0][0] < oven_indices[0] < positions[1][0] < positions[2][0] else '')
        if ambiguity:
            if include_ambiguous:
                groups.append({'page':page,'component':components[0],'columns':[],
                               'ambiguity':ambiguity,'before':before,'after':after,'change':change})
            continue
        def interval(value):
            places = len(value.split('.')[1]) if '.' in value else 0
            half = .5 * 10 ** -places
            return float(value)-half, float(value)+half
        columns = []
        for a,b,c in zip(before['values'],after['values'],change['values']):
            lo,hi = interval(a); blo,bhi = interval(b); clo,chi = interval(c)
            if lo <= 0 or blo < 0: break
            calculated = ((blo/hi-1)*100, (bhi/lo-1)*100)
            columns.append({'before':a,'after':b,'reported_change':c,
                            'calculated_interval':list(calculated),
                            'inconsistent':calculated[1] < clo or calculated[0] > chi})
        if len(columns) != len(before['values']): continue
        groups.append({'page':page,'component':components[0], 'columns':columns,
                       'before':before,'after':after,'change':change})
    return groups


def _enforce_source_aging_arithmetic(result: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Flag incompatible source values even if the model supplied no objection."""
    from backend.app.rulebase import source_sample_registry, source_group_for_sample
    registry = source_sample_registry(source_text)
    for sample in result.get('samples') or []:
        source = source_group_for_sample(sample, registry=registry)
        if not source:
            continue
        groups = _vertical_aging_elongation_groups(str(source.get('text') or ''), include_ambiguous=True)
        for group in groups:
            if sum(g['page'] == group['page'] and g['component'] == group['component'] for g in groups) != 1:
                continue
            bad = [i for i,c in enumerate(group['columns'],1) if c['inconsistent']]
            ambiguity = group.get('ambiguity') or ''
            if not bad and not ambiguity:
                continue
            component, page = group['component'], group['page']
            candidates = [c for c in sample.get('checks') or []
                if c.get('item') in ('老化后拉力试验',component+'老化后拉力试验')
                and component in str(c.get('category') or '')
                and set(c.get('source_pages') or []) == {page}]
            title = component+'老化前后伸长率算术一致性'
            proof = {'origin':'source_aging_arithmetic','page':page,'component':component,
                     'columns':group['columns'],'mismatch_columns':bad,
                     'scope':'source_binding_ambiguous' if ambiguity else 'source_values_inconsistent_not_product_failure'}
            if ambiguity: proof['ambiguity'] = ambiguity
            explanation = ambiguity or '；'.join(
                f"第{i}列：老化前{group['columns'][i-1]['before']}%、老化后{group['columns'][i-1]['after']}%，"
                f"填写变化率{group['columns'][i-1]['reported_change']}%，与原值显示精度允许的复算区间不重合"
                for i in bad)
            if len(candidates) == 1:
                check = candidates[0]
                title = check['item']
                if check.get('verdict') not in ('fail','manual_review'):
                    archive = sample.setdefault('superseded_model_checks',[])
                    record = {'reason':'source_aging_arithmetic','original_check':dict(check)}
                    if record not in archive: archive.append(record)
                    check['verdict'] = 'manual_review'
            else:
                matching = [c for c in sample.get('checks') or []
                    if c.get('item') == title and c.get('source_pages') == [page]]
                if len(matching) > 1: continue
                check = matching[0] if matching else {'item':title,'category':component+'机械性能',
                    'reported':explanation,'required':'同列老化前后中间值与变化率应相互一致',
                    'basis':'报告数据算术一致性；不替代材料限值核验', 'verdict':'manual_review',
                    'source_pages':[page],'evidence_status':'located'}
                if not matching: sample.setdefault('checks',[]).append(check)
            check['source_aging_arithmetic'] = proof
            arithmetic_note = explanation+'；请核对原始记录，不能擅自补改数字或据此认定产品不合格'
            if arithmetic_note not in str(check.get('note') or ''):
                check['note'] = '；'.join(filter(None,[str(check.get('note') or ''),arithmetic_note]))
            items = [i for i in sample.get('items') or []
                if i.get('item') == title and set(i.get('source_pages') or []) == {page}
                and i.get('action_required') is True and i.get('action_type')=='manual_review']
            if items:
                for item in items: item['source_aging_arithmetic'] = proof
            else:
                sample.setdefault('items',[]).append({'item':title,'reported':explanation,
                    'should_be':('核对同页试验段归属与读取顺序，明确老化前后值及变化率的对应关系'
                                 if ambiguity else '核对该列老化前后原值及变化率，确认哪一项记录有误'),
                    'standard':'报告数据算术一致性；不替代材料限值核验',
                    'severity':'suggestion','action_required':True,'action_type':'manual_review',
                    'review_action':'回看同页及实验室原始记录；不得猜测缺失数字或自动放行',
                    'evidence_status':'located','source_pages':[page],
                    'source_segments':check.get('source_segments') or [],'source_aging_arithmetic':proof})
    return result


def _aging_elongation_groups(block: str) -> list[dict[str, Any]]:
    lines = block.splitlines()
    before_indexes = [index for index, line in enumerate(lines) if "老化前断裂伸长率-中间值" in line]
    groups: list[dict[str, Any]] = []
    for group_index, before_index in enumerate(before_indexes):
        end = before_indexes[group_index + 1] if group_index + 1 < len(before_indexes) else len(lines)
        # 兼容个别报告把“老化后”误排成“老化前后”的表头；
        # 数值仍位于空气烘箱老化条件之后，可确定为老化后数值。
        after_index = next((
            i for i in range(before_index + 1, end)
            if re.search(r"老化(?:前后|后)断裂伸长率-中间值", lines[i])
        ), None)
        change_index = next((i for i in range((after_index or before_index) + 1, end) if "老化前后断裂伸长率变化率" in lines[i]), None)
        if after_index is None or change_index is None:
            continue
        before = _numeric_values_after(lines[:after_index], before_index, "最小150")
        after = _numeric_values_after(lines[:change_index], after_index, "最小150")
        reported = _numeric_values_after(lines[:end], change_index, "最大±20")
        if not before or len(before) != len(after) or len(before) != len(reported):
            continue
        # 行标题已声明每列是中间值；各列对应不同试样/线芯，不能再次跨列取中间值。
        # 列数不一致或分母无效时不宣称已完成复算。
        if any(value <= 0 for value in before):
            continue
        calculated = [
            _round_change((new - old) / old * 100.0)
            for old, new in zip(before, after)
        ]
        groups.append({
            "before": before, "after": after, "reported": reported,
            "calculated": calculated,
        })
    return groups


def _enforce_aging_change_rates(result: dict[str, Any], source_text: str) -> dict[str, Any]:
    """对已标明中间值的行逐列复算；不对原始试片行套用本规则。"""
    from backend.app.rulebase import source_group_for_sample, source_sample_registry
    source_registry = source_sample_registry(source_text)
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        source_group = source_group_for_sample(sample, registry=source_registry)
        if not source_group:
            continue
        block = str(source_group.get("text") or "")
        groups = _aging_elongation_groups(block)
        for group_index, group in enumerate(groups, start=1):
            reported_change = group["reported"]
            mismatch_columns = [
                index for index, (reported, calculated) in enumerate(
                    zip(reported_change, group["calculated"]), start=1
                ) if reported != calculated
            ]
            mismatch = bool(mismatch_columns)
            material = "绝缘" if group_index == 1 else "护套" if group_index == 2 else f"第{group_index}组材料"
            sample["items"] = [
                item for item in (sample.get("items") or [])
                if item.get("validation_origin") != f"aging_elongation_change:{group_index}"
            ]
            related_checks = [
                check for check in sample.get("checks") or []
                if material in f"{check.get('category', '')}{check.get('item', '')}" and "老化" in f"{check.get('category', '')}{check.get('item', '')}"
            ]
            calculation = "、".join(f"{value:+d}%" for value in group["calculated"])
            for check in related_checks:
                note = (
                    f"程序按各列老化前后中间值分别复算变化率：{calculation}"
                )
                if note not in str(check.get("note", "")):
                    check["note"] = "；".join(filter(None, [str(check.get("note", "")), note]))
                if mismatch and str(check.get("item", "")) == f"{material}老化前后断裂伸长率变化率":
                    check["verdict"] = "fail"
            if mismatch:
                sample["items"].append({
                    "item": f"{material}老化前后断裂伸长率变化率",
                    "reported": (
                        f"各列老化前中间值{group['before']}%，"
                        f"老化后中间值{group['after']}%，"
                        f"报告变化率{reported_change}%"
                    ),
                    "should_be": f"按(new-old)/old×100%逐列复算为{calculation}",
                    "standard": "对应绝缘/护套材料机械性能表（变化率±20%）",
                    "severity": "must_fix", "action_required": True, "action_type": "correction",
                    "review_action": f"核对第{mismatch_columns}列{material}断裂伸长率变化率及原始值",
                    "validation_origin": f"aging_elongation_change:{group_index}",
                })
            records.append({
                "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
                "material": material, **group, "reported_change": reported_change,
                "mismatch_columns": mismatch_columns,
                "passed": not mismatch,
            })
    if records:
        result.setdefault("_deterministic_validation", {})["aging_elongation_change"] = records
    return result


def _enforce_standard_family(
    result: dict[str, Any],
    standard_family: str | None,
    source_text: str = "",
) -> dict[str, Any]:
    """对高风险曲挠电流做后端确定性复核，拦截模型跨标准族拼接。"""
    if standard_family != "pvc":
        return result
    source_registry: list[dict[str, Any]] = []
    if source_text:
        from backend.app.rulebase import source_sample_registry
        source_registry = source_sample_registry(source_text)
    records: list[dict[str, Any]] = []
    for sample in result.get("samples") or []:
        parsed = _spec_cores_area(sample)
        if not parsed:
            continue
        cores, area = parsed
        valid_current_ranges, required = _pvc_current_requirement(cores, area)
        evidence_values: list[float] = []
        related_checks: list[dict[str, Any]] = []
        for check in sample.get("checks") or []:
            label = f"{check.get('category', '')}{check.get('item', '')}"
            if "曲挠" in label:
                current = _reported_current(check.get("reported"))
                if current is not None:
                    evidence_values.append(current)
                    related_checks.append(check)
        for item in sample.get("items") or []:
            if "曲挠" in str(item.get("item", "")) and "电流" in str(item.get("item", "")):
                current = _reported_current(item.get("reported"))
                if current is not None:
                    evidence_values.append(current)
        evidence_source = "model_output"
        from backend.app.rulebase import _plain_table_text, source_group_for_sample
        source_group = source_group_for_sample(sample, registry=source_registry)
        if source_group:
            plain_source = " ".join(
                _plain_table_text(str(page.get("text") or ""))
                for page in (source_group.get("pages") or [])
            )
            source_current = re.search(
                r"曲挠试验.{0,900}?施加电流\s*(?:\|\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:\|\s*)?A",
                plain_source,
                re.I | re.S,
            )
            if source_current:
                evidence_values.insert(0, float(source_current.group(1)))
                evidence_source = "sample_source_page"
        if not evidence_values:
            continue
        reported = evidence_values[0]
        # 对五芯以上，模板中的“施加电流”可表示标准要求的信号电流；
        # 只有原页明确写成“负载/载流电流”时才可判定违规。
        explicit_load_current = False
        if source_group:
            explicit_load_current = bool(re.search(
                r"(?:负载|载流)(?:试验)?电流\s*(?:\|\s*)?[0-9]+(?:\.[0-9]+)?\s*(?:\|\s*)?A",
                plain_source,
                re.I,
            ))
        is_valid, current_role = _pvc_flex_current_verdict(
            cores,
            reported,
            valid_current_ranges,
            explicit_load_current=explicit_load_current,
        )
        for check in related_checks:
            check["required"] = required
            check["basis"] = "GB/T 5023.2-2008 第3.1.4条（PVC曲挠线芯载流）"
            if is_valid:
                check["verdict"] = "pass"
                if cores > 5:
                    check["note"] = f"程序标准族校验：五芯以上页面仅见施加电流{reported:g}A，未见负载/载流表述，按信号电流处理"
                else:
                    check["note"] = f"程序标准族校验：RVV/PVC体系，报告电流{reported:g}A符合要求及+10%/-0%公差；不得套用5013橡皮电缆负载电流表"
            else:
                check["verdict"] = "fail"
                check["note"] = f"程序标准族校验：报告电流{reported:g}A不符合PVC曲挠载流规则"
        # 删除模型生成的同项目错误结论，再按确定性结果决定是否重建。
        sample["items"] = [
            item for item in (sample.get("items") or [])
            if not (
                "曲挠" in str(item.get("item", ""))
                and any(
                    marker in " ".join(str(item.get(field, "")) for field in (
                        "item", "reported", "should_be", "review_action",
                    ))
                    for marker in ("电流", "载流")
                )
            )
        ]
        if not is_valid:
            sample["items"].append({
                "item": "曲挠试验负载电流",
                "reported": f"{reported:g}A",
                "should_be": required,
                "standard": "GB/T 5023.2-2008 第3.1.4条",
                "severity": "must_fix",
                "action_required": True,
                "action_type": "correction",
                "review_action": f"按PVC曲挠载流规则更正：{required}",
            })
        records.append({
            "sample": " ".join(str(sample.get(key, "")) for key in ("model", "voltage", "spec")).strip(),
            "reported_current_a": reported,
            "required": required,
            "passed": is_valid,
            "evidence_source": evidence_source,
            "current_role": current_role,
        })
    if records:
        result.setdefault("_deterministic_validation", {})["pvc_flexing_current"] = records
    return result


def review_report(
    report_id: int,
    pdf_path: str,
    ocr_path: str,
    progress: ProgressCallback = None,
    metadata_callback: MetadataCallback = None,
    task_id: int | None = None,
    parent_report_id: int | None = None,
    parent_review: dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """执行完整审核，返回JSON结果。"""
    from backend.app.extract import extract_pdf, extract_report_info
    from backend.app.settings_store import get_ai_runtime_config
    ai_settings = get_ai_runtime_config()
    review_token_budget = _review_output_budget(ai_settings)

    # 第1/2步：提取PDF文字（含OCR占位符），重试时优先复用缓存。
    if progress:
        progress("extracting", 0, 1, "正在提取 PDF 文字和表格页")
    text, ocr_save_path = extract_pdf(pdf_path, ocr_path)
    metadata = extract_report_info(text)
    if metadata_callback:
        metadata_callback(metadata)
    if progress:
        progress("extracting", 1, 1, "PDF 文字提取完成")

    # 对占位符图片做并行视觉 OCR，每页结果可跨重试复用。
    client = LLMClient(ai_config=ai_settings)
    ocr_settings = get_section("ocr")
    text = _vision_ocr_pages(
        text,
        client,
        progress=progress,
        max_workers=int(ocr_settings.get("vision_concurrency", 3)),
    )

    from backend.app.ocr_identity import prepare_review_source
    text, ocr_identity_meta = prepare_review_source(
        text, pdf_path, ocr_path, ocr_settings, _split_report_by_samples, progress)

    # 专项报告必须在任何型式试验标准识别、规则库加载和必审覆盖之前
    # 直接进入独立路线；不能再依赖终审阶段的事后关键词过滤。
    special_category = _special_report_category(text, report_id)
    if special_category:
        return _review_special_report(
            category=special_category,
            report_id=report_id,
            text=text,
            metadata=metadata,
            ocr_identity_meta=ocr_identity_meta,
            ocr_path=ocr_path,
            ai_settings=ai_settings,
            review_token_budget=review_token_budget,
            progress=progress,
            task_id=task_id,
            parent_report_id=parent_report_id,
            parent_review=parent_review,
        )

    # 第3/4步：优先按完整样品分批，不再按字符数从试验表中间截断。
    # Local coordinate evidence stays internal: it is not inserted into model
    # prompts or the source text that is redacted for external review.
    from backend.app.extract import collect_local_table_evidence, collect_text_layer
    local_evidence = (collect_local_table_evidence(pdf_path,text,ocr_path)
                      if ocr_settings.get('enabled',True) else {'status':'disabled','pages':[]})
    # 文字层证人：随 local_evidence 一起流入确定性核验，不进 AI 提示词。
    try:
        local_evidence['text_layer_pages'] = (collect_text_layer(pdf_path, ocr_path) or {}).get('pages') or {}
    except Exception:
        local_evidence['text_layer_pages'] = {}
    from backend.app.ocr_supplement import supplement_oven_pages
    local_evidence = supplement_oven_pages(pdf_path, text, local_evidence, ocr_path, ocr_settings)
    from backend.app.ocr_table_repair import supplement as supplement_table_repair
    local_evidence = supplement_table_repair(pdf_path, text, local_evidence, ocr_path)
    source_pdf_sha256 = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    standard_family = _detect_standard_family(text, metadata)
    from backend.app.rulebase import get_structured_rules_context
    # 报告级命中信息仅用于结果元数据，不再整份塞入每个 AI 请求。
    _, structured_meta = get_structured_rules_context(text, metadata, standard_family)
    from backend.app.iteration import get_active_rules_context
    iteration_rules, review_meta = get_active_rules_context(standard_family)
    review_meta['ocr_identity_recovery'] = ocr_identity_meta
    review_meta['table_structure_repair'] = local_evidence.get('table_repair_summary', {'enabled': False})
    review_meta['local_table_evidence']={
        'status':local_evidence['status'],'reason':local_evidence.get('reason'),
        'pages':[p['page'] for p in local_evidence.get('pages',[])],
        'boundary':'internal coordinate evidence, not an independent audit pass'}
    revision_context = _revision_context(parent_review)
    family_context = _family_lock_context(standard_family)
    batches = _split_report_by_samples(text)
    partial_results_by_index: dict[int, dict[str, Any]] = {}
    privacy_meta_by_index: dict[int, dict[str, Any]] = {}
    external_content_meta_by_index: dict[int, dict[str, Any]] = {}
    outbound_guard_meta_by_index: dict[int, dict[str, Any]] = {}
    completed_units = 0
    total_units = batches[0]["total_samples"] if batches else 0
    # 遵循管理员在系统设置中配置的模型超时。旧逻辑无论
    # 界面设为多少都强制封顶 240 秒，导致上游仍在生成时
    # 本地提前中断。保留 30–600 秒安全边界，当前配置 500 秒
    # 将如实生效。
    request_timeout = max(30.0, min(600.0, float(ai_settings.get("timeout_seconds", 120))))
    cache_dir = Path(ocr_path) / f"review_task_{task_id}" if task_id is not None else None
    # 已成功的样品会通过 batch 缓存直接复用，只补跑失败样品。
    # 单份报告内串行审核样品，两个 Worker 任务可各占一个全局 AI 槽位。
    # 这样既保留跨报告并发，又避免大报告一次占满两个槽位。
    batch_concurrency = 1

    def review_one_batch(index: int, batch: dict[str, Any]) -> tuple[int, dict[str, Any], bool]:
        labels = "、".join(batch["labels"])
        unit_name = (
            f"样品 {batch['sample_start']}-{batch['sample_end']}/{batch['total_samples']}"
            if batch["sample_aware"] and batch["sample_count"] > 1 else
            f"样品 {batch['sample_start']}/{batch['total_samples']}"
            if batch["sample_aware"] else
            f"分段 {index}/{len(batches)}"
        )
        # 只向模型提供当前样品命中的结构化规则和已发布纠错。
        # 旧流程每个样品都重复携带整个系列的叙述知识库，
        # 使真实请求达到4万字并在上游长时间排队/推理。
        # 分批文本前面含报告首页概要，其中会列出整份报告的所有型号。
        # 规则匹配必须只看“当前完整样品页”，否则 RVV 等通用别名
        # 会同时命中 5023.5 和 8734.3，把 4mm² 参数误用到 0.75mm²。
        sample_rule_text = batch["text"].split("【本批次完整样品页面】", 1)[-1]
        batch_rules, _ = get_structured_rules_context(sample_rule_text, {}, standard_family)
        batch_knowledge = batch_rules
        if iteration_rules:
            batch_knowledge += "\n\n【已发布人工审核规则】\n" + iteration_rules

        # 单位内网安全预审与外网主审核相互独立。shadow 模式只生成
        # 脱敏副本和审计证据；enforce 模式才把脱敏副本送给主审核模型。
        from backend.app.privacy_preflight import (
            prepare_stable_privacy_preflight,
            save_preflight_artifact,
        )
        from backend.app.executable_rules import active_rules_fingerprint
        rules_fingerprint = active_rules_fingerprint()
        from backend.app.ocr_table_repair import batch_addendum
        repair_batch_text = batch_addendum(batch["text"], local_evidence)
        # 内网语义模型已退出正式流程；本层只做确定性脱敏与残留检查。
        privacy_settings = dict(ai_settings, intranet_content_review_enabled=False)
        preflight = prepare_stable_privacy_preflight(
            repair_batch_text, metadata, privacy_settings,
            ocr_path=ocr_path, task_id=task_id, batch_number=index,
            rules_fingerprint=rules_fingerprint,
        )
        if preflight["mode"] != "disabled":
            save_preflight_artifact(ocr_path, task_id, index, preflight)
            privacy_meta_by_index[index] = {
                key: preflight.get(key) for key in (
                    "mode", "model_status", "replacement_counts", "logic_checks",
                    "outbound_findings", "outbound_blocked", "cache_reused",
                )
            }
        external_batch_text = str(preflight["external_text"])
        batch_client = LLMClient(timeout_seconds=request_timeout, ai_config=ai_settings)
        from backend.app.outbound_guard import FinalOutboundGuard, GuardedLLMClient
        final_guard = FinalOutboundGuard(
            batch["text"], metadata, ocr_path, task_id, index
        )
        guarded_client = GuardedLLMClient(batch_client, final_guard)
        from backend.app.external_content_review import prepare_external_content_review
        with _REVIEW_AI_SEMAPHORE:
            external_content_review = prepare_external_content_review(
                external_batch_text,
                ai_settings,
                guarded_client,
                ocr_path=ocr_path,
                task_id=task_id,
                batch_number=index,
            )
        external_content_meta_by_index[index] = external_content_review
        system_prompt = prompts.COMPACT_REVIEW_PROMPT.replace(
            "{knowledge}", batch_knowledge
        ).replace(
            "{report_text}", external_batch_text
        ) + family_context + revision_context
        fingerprint = hashlib.sha256((
            "sample-batch-v12-independent-report-level-review\n"
            + str(ai_settings.get("provider")) + "\n"
            + str(ai_settings.get("base_url")) + "\n"
            + hashlib.sha256(str(ai_settings.get("api_key") or "").encode("utf-8")).hexdigest() + "\n"
            + str(ai_settings.get("mock_enabled")) + "\n"
            + str(ai_settings.get("review_model")) + "\n"
            + str(ai_settings.get("privacy_preflight_mode")) + "\n"
            + str(ai_settings.get("privacy_company_blacklist_enabled")) + "\n"
            + str(ai_settings.get("privacy_application_blacklist_enabled")) + "\n"
            + str(ai_settings.get("thinking_enabled")) + "\n"
            + str(ai_settings.get("reasoning_effort")) + "\n"
            + str(ai_settings.get("temperature")) + "\n"
            + str(ai_settings.get("timeout_seconds")) + "\n"
            + str(review_token_budget) + "\n"
            + rules_fingerprint + "\n"
            + prompts.COMPACT_REVIEW_PROMPT + "\n" + batch_knowledge + "\n" + family_context + "\n"
            + revision_context + "\n" + external_batch_text
        ).encode("utf-8")).hexdigest()
        cache_path = _batch_cache_path(ocr_path, task_id, index)
        cached = _load_batch_cache(cache_path, fingerprint)
        if cached is not None:
            outbound_guard_meta_by_index[index] = final_guard.summary()
            return index, cached, True
        with _REVIEW_AI_SEMAPHORE:
            raw = guarded_client.chat(
                system_prompt,
                f"""这是报告的{unit_name}，本批次包含：{labels}。请审核并严格输出 JSON。
只对“本批次完整样品页面”中的实际受试样品生成 samples；首页所列单元覆盖型号仅作为报告级背景，不得误当成本批次受试样品。
每个样品的试验表在本批次内是连续的，不得引用其他样品的数值。
不得把定向规则中其他型号、其他产品标准或其他截面的参数套用到当前样品。
只有当报告值与当前型号和规格的明确定向规则冲突时才能报问题；不得根据相邻规格推断限值。
定向规则未收录某规格精确参数时，不得仅因“参数未入库”生成 suggestion。
samples.items 只保留 action_required=true 的 must_fix 和 suggestion 项。
正确项目只写入合并后的checks，不得为了保留说明而标成suggestion。
suggestion 仅表示证据不足且必须执行一项具体人工复核；必须填写 review_action。
结构化规则中标为required的每个项目都必须在samples.checks中明确出现，item字段使用规则库项目原名；
不得为压缩输出省略必审项目。conditional项目也必须给出适用性判断，无法确定时用manual_review。
允许把同类项目合并在一条check中，但item字段必须逐一列出所有被覆盖的规则库项目原名。
detail必须为空字符串。""",
                model=ai_settings.get("review_model"),
                # 外网DNS/连接瞬时抖动不应使整份报告从MinerU开始重跑；
                # 仅在当前样品批次内做有限指数退避重试。
                max_retries=2,
                max_tokens=review_token_budget,
                json_mode=True,
            )
            parsed = _parse_review_json(
                guarded_client,
                raw,
                model=ai_settings.get("review_model"),
                max_tokens=review_token_budget,
            )
        outbound_guard_meta_by_index[index] = final_guard.summary()
        _save_batch_cache(cache_path, fingerprint, parsed)
        return index, parsed, False

    if progress:
        progress("reviewing", 0, total_units, f"AI 正在按样品审核，共 {total_units} 个")
    failures: list[Exception] = []
    with ThreadPoolExecutor(max_workers=batch_concurrency) as executor:
        futures = {
            executor.submit(review_one_batch, index, batch): (index, batch)
            for index, batch in enumerate(batches, start=1)
        }
        for future in as_completed(futures):
            index, batch = futures[future]
            try:
                result_index, batch_result, reused = future.result()
                partial_results_by_index[result_index] = batch_result
                completed_units += batch["sample_count"]
                if progress:
                    labels = "、".join(batch["labels"])
                    prefix = "已复用" if reused else "已完成"
                    progress("reviewing", completed_units, total_units, f"{prefix}样品 {index}/{total_units}：{labels}")
            except Exception as exc:
                failures.append(exc)
    if failures:
        raise failures[0]
    partial_results = [partial_results_by_index[index] for index in range(1, len(batches) + 1)]

    if progress:
        progress("finalizing", 0, 1, "正在合并分段审核结果")
    result = _merge_source_bound_batches(partial_results, text)

    # 原页样品主清单是唯一身份来源：AI漏样、重复或调整顺序都不得导致后续证据串样。
    from backend.app.rulebase import reconcile_result_samples_with_source
    result = reconcile_result_samples_with_source(result, text)
    result = _recover_component_lowtemp_checks(result, text)

    # OCR 字段直接来自报告原文，优先级高于模型推测。
    for field in ("application_no", "report_no", "company", "product_unit"):
        if metadata.get(field):
            result[field] = metadata[field]
    result = _enforce_standard_family(result, standard_family, text)
    from backend.app.rulebase import validate_review_result
    result = validate_review_result(result, standard_family)
    result = _suppress_voltage_items_contradicted_by_pass_checks(result)
    result = _enforce_jbt87342_lowtemp_coverage(result, text)
    result = _enforce_rubber_lowtemp_area_limit(result, standard_family, text)
    result = _enforce_rubber_flexing_applicability(result, standard_family, text)
    result = _require_manual_review_for_ycw_lowtemp_selection(result, standard_family)
    result = _suppress_extra_passed_test_issues(result)
    result = _apply_confirmed_false_positive_guards(result, text, standard_family)
    result = _enforce_minimum_thickness_formula(result)
    result = _enforce_lowtemp_impact_mass(result, text)
    result = _enforce_lowtemp_conditioning_time(result, text, local_evidence, source_pdf_sha256)
    result = _accept_completed_jbt87342_lowtemp_alternative(result)
    result = _enforce_rubber_flexing_setup(result, standard_family, text)
    result = _suppress_non_actionable_manual_reviews(result)
    result = _enforce_non_pollution_applicability(result, standard_family)
    from backend.app.executable_rules import apply_required_input_guards
    result = apply_required_input_guards(result)
    result = _enforce_aging_change_rates(result, text)
    from backend.app.rulebase import enforce_required_item_coverage
    result = enforce_required_item_coverage(result, standard_family, text,
        local_evidence=local_evidence if local_evidence and local_evidence.get('status')=='collected' else None,
        source_pdf_sha256=source_pdf_sha256)
    result = _recover_sheath_aging_from_combined_checks(result)
    result = _enforce_pvc_single_core_lowtemp_selection(result, standard_family, text)
    # 必审覆盖是最后一轮可能新增人工项的步骤。覆盖完成后必须再次执行
    # 原页确定性消解，避免已经确认的落锤、16h和组合项目被重新报缺失。
    result = _enforce_lowtemp_impact_mass(result, text)
    result = _enforce_lowtemp_conditioning_time(result, text, local_evidence, source_pdf_sha256)
    result = _accept_completed_jbt87342_lowtemp_alternative(result)
    result = _apply_confirmed_false_positive_guards(result, text, standard_family)
    result = _suppress_non_actionable_manual_reviews(result)
    result = _enforce_special_report_scope(result, text)
    # 报告范围整理可能从check重建item；再次清理已确认属于
    # 独立下样流程或标准已允许的泛化人工提示。
    result = _suppress_non_actionable_manual_reviews(result)
    result = _repair_malformed_deterministic_checks(result, text)
    result = _resolve_obsolete_rvs_impact_claims(result)
    result = _supersede_pressure_model_passes(result)
    result = _enforce_source_aging_arithmetic(result, text)
    from backend.app.executable_rules import apply_source_numeric_ranges
    result = apply_source_numeric_ranges(result, text, standard_family)
    result = _enforce_check_action_consistency(result)
    result = _suppress_non_actionable_manual_reviews(result)
    result = _supersede_pressure_model_passes(result)
    result = _supersede_coordinate_table_artifacts(result, text, local_evidence, source_pdf_sha256)
    from backend.app.executable_rules import apply_report_chronology
    result = apply_report_chronology(result, text)
    result = _apply_original_record_notice_policy(result)
    from backend.app.ocr_table_repair import enforce_np_arithmetic
    result = enforce_np_arithmetic(result, text, local_evidence, source_pdf_sha256)
    result = _normalize_review_result(result)
    from backend.app.pressure_explanation import append_final_pressure_explanations
    result = append_final_pressure_explanations(result)
    if parent_report_id and parent_review:
        result["revision_comparison"] = _build_revision_comparison(parent_report_id, parent_review, result)
    review_meta["standard_family"] = standard_family
    review_meta["structured_rulebase"] = structured_meta
    if privacy_meta_by_index:
        from backend.app.executable_rules import merge_privacy_logic_checks
        privacy_batches = [privacy_meta_by_index[index] for index in sorted(privacy_meta_by_index)]
        review_meta["privacy_preflight"] = {
            # Report what was actually sent, not the retired semantic
            # pre-review switch that may still be `disabled` in old settings.
            "mode": (
                "enforce"
                if privacy_batches and all(item.get("mode") == "enforce" for item in privacy_batches)
                else ai_settings.get("privacy_preflight_mode", "disabled")
            ),
            "semantic_model_used": any(
                item.get("model_status") == "ok" for item in privacy_batches
            ),
            "batch_count": len(privacy_batches),
            "company_replacements": sum(
                int((item.get("replacement_counts") or {}).get("company", 0))
                for item in privacy_batches
            ),
            "application_replacements": sum(
                int((item.get("replacement_counts") or {}).get("application_no", 0))
                for item in privacy_batches
            ),
            "model_errors": sum(
                1 for item in privacy_batches if str(item.get("model_status", "")).startswith("error:")
            ),
            "outbound_findings": sum(len(item.get("outbound_findings") or []) for item in privacy_batches),
            "logic_checks": merge_privacy_logic_checks(privacy_batches),
        }
    if external_content_meta_by_index:
        from backend.app.external_content_review import merge_checks
        content_batches = [
            external_content_meta_by_index[index]
            for index in sorted(external_content_meta_by_index)
        ]
        review_meta["external_content_review"] = {
            "provider": ai_settings.get("provider"),
            "model": ai_settings.get("review_model"),
            "batch_count": len(content_batches),
            "successful_batches": sum(
                item.get("status") == "ok" for item in content_batches
            ),
            "failed_batches": sum(
                str(item.get("status", "")).startswith("error:")
                for item in content_batches
            ),
            "cache_reused_batches": sum(
                bool(item.get("cache_reused")) for item in content_batches
            ),
            "logic_checks": merge_checks(content_batches),
        }
    if outbound_guard_meta_by_index:
        guard_batches = [
            outbound_guard_meta_by_index[index]
            for index in sorted(outbound_guard_meta_by_index)
        ]
        review_meta["final_outbound_guard"] = {
            "guard_version": "final-outbound-guard-v1",
            "batch_count": len(guard_batches),
            "calls": sum(int(item.get("calls") or 0) for item in guard_batches),
            "allowed": sum(int(item.get("allowed") or 0) for item in guard_batches),
            "blocked": sum(int(item.get("blocked") or 0) for item in guard_batches),
            "finding_types": sorted({
                finding
                for item in guard_batches
                for finding in item.get("finding_types") or []
            }),
        }
    result["_review_meta"] = review_meta

    if progress:
        progress("finalizing", 1, 1, "审核结果整理完成")

    return result


def build_markdown(result: Dict[str, Any]) -> str:
    """根据JSON结果生成固定结构的审核意见书Markdown。"""
    lines = []
    lines.append("# CCC电线电缆检测报告审核意见书")
    lines.append("")
    lines.append(f"**申请编号**：{result.get('application_no', '')}")
    lines.append(f"**报告编号**：{result.get('report_no', '')}")
    lines.append(f"**企业名称**：{result.get('company', '')}")
    lines.append(f"**产品单元**：{result.get('product_unit', '')}")
    lines.append(f"**产品描述**：{result.get('product_desc', '')}")
    lines.append(f"**审核结论**：{result.get('conclusion', '')}")
    lines.append("")

    comparison = result.get("revision_comparison") or {}
    if comparison:
        lines.append("## 更正验证")
        lines.append("")
        lines.append(f"**验证结论**：{comparison.get('status', '')}")
        lines.append(
            f"原问题 {comparison.get('original_issue_count', 0)} 项；"
            f"已解决 {comparison.get('resolved_count', 0)} 项；"
            f"未解决 {comparison.get('unresolved_count', 0)} 项；"
            f"待人工复核 {comparison.get('manual_review_count', 0)} 项；"
            f"新增问题 {comparison.get('new_issue_count', 0)} 项。"
        )
        lines.append("")
        lines.append("| 样品 | 原问题 | 原报告值 | 要求 | 新版证据 | 验证状态 |")
        lines.append("|---|---|---|---|---|---|")
        status_text = {"resolved": "已解决", "unresolved": "未解决", "manual_review": "待人工复核"}
        for item in comparison.get("items") or []:
            lines.append(
                f"| {_markdown_cell(item.get('sample', ''))} | {_markdown_cell(item.get('original_item', ''))} | "
                f"{_markdown_cell(item.get('original_reported', ''))} | {_markdown_cell(item.get('required_correction', ''))} | "
                f"{_markdown_cell(item.get('new_evidence', ''))} | {status_text.get(item.get('status'), item.get('status', ''))} |"
            )
        lines.append("")

    # 需修改项汇总表
    lines.append("## 需修改项汇总")
    lines.append("")
    lines.append("| 样品 | 项目 | 报告原值 | 应改值 | 标准依据 | 结论 |")
    lines.append("|---|---|---|---|---|---|")
    for item in result.get("report_items") or []:
        if item.get("severity") in ("must_fix", "suggestion") and item.get("action_required") is not False:
            lines.append("| " + " | ".join(_markdown_cell(value) for value in (
                "全报告", item.get("item"), item.get("reported"), item.get("should_be"),
                item.get("standard"), item.get("severity"))) + " |")
    for sample in result.get("samples", []):
        model = sample.get("model", "")
        for item in sample.get("items", []):
            if item.get("severity") in ("must_fix", "suggestion"):
                lines.append(
                    f"| {model} | {item.get('item', '')} | {item.get('reported', '')} | "
                    f"{item.get('should_be', '')} | {item.get('standard', '')} | {item.get('severity', '')} |"
                )
    lines.append("")

    # 备注建议
    lines.append("## 备注建议")
    lines.append("")
    for remark in result.get("remarks", []):
        lines.append(f"- {remark}")
    lines.append("")

    # 审核明细
    lines.append("## 审核明细")
    lines.append("")
    lines.append("| 样品 | 项目 | 报告原值 | 应改值 | 标准依据 | 结论 |")
    lines.append("|---|---|---|---|---|---|")
    for sample in result.get("samples", []):
        model = sample.get("model", "")
        for item in sample.get("items", []):
            lines.append(
                f"| {model} | {item.get('item', '')} | {item.get('reported', '')} | "
                f"{item.get('should_be', '')} | {item.get('standard', '')} | {item.get('severity', '')} |"
            )
    lines.append("")
    lines.append(result.get("detail", ""))
    return "\n".join(lines)
