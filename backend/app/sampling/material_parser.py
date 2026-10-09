"""产品描述材料信息提取。

识别结果只用于预填充“合并覆盖”的材料数量，不直接作为最终下样结论。
页面必须保留人工增删和数量修改能力。
"""
from __future__ import annotations

import io
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 20
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt"}

CQC_PATTERN = re.compile(r"\bCQC\s*[-_]?\s*(\d{8,20})\b", re.IGNORECASE)
SUPPLIER_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9·（）()\-]{2,70}?"
    r"(?:股份有限公司|有限责任公司|有限公司|公司|线材厂|材料厂|塑料厂|加工厂|电缆厂|工厂|研究院)"
)
BRAND_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:JR?-?70|J-?90|JGD-?70|HR?-?70|HII-?90|YG(?:-?\d+)?)\b",
    re.IGNORECASE,
)
SELECTED_BRAND_PATTERN = re.compile(
    r"(?:√|☑|■|◉|\[x\]|\(x\))\s*"
    r"(JR?-?70|J-?90|JGD-?70|HR?-?70|HII-?90|YG(?:-?\d+)?)\b",
    re.IGNORECASE,
)

CATEGORY_LABELS = {
    "copper_conductor": "铜导体",
    "aluminum_conductor": "铝导体",
    "insulation_normal": "普通绝缘",
    "insulation_90": "90℃绝缘",
    "sheath_normal": "普通护套",
    "sheath_90": "90℃护套",
    "outer_braid": "外编织层 YG",
    "other": "其他人工兼容组",
}


def _normalize_brand(value: str) -> str:
    value = value.upper().replace(" ", "")
    aliases = {
        "J70": "J-70", "JR70": "JR-70", "J90": "J-90", "JGD70": "JGD-70",
        "H70": "H-70", "HR70": "HR-70", "HII90": "HII-90",
    }
    return aliases.get(value, value)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    # PDF文字层常将竖排分类名拆成单字行。
    for label in ("导体", "绝缘", "屏蔽", "护套"):
        text = re.sub(rf"(?m)^\s*{label[0]}\s*\n\s*{label[1]}\s*$", label, text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            xml = package.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError("无法读取DOCX文档，请确认文件未损坏") from exc
    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        value = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace)).strip()
        if value:
            lines.append(value)
    return "\n".join(lines)


def _extract_pdf(data: bytes) -> tuple[str, str, list[str]]:
    import fitz

    warnings: list[str] = []
    document = fitz.open(stream=data, filetype="pdf")
    try:
        if len(document) > MAX_PDF_PAGES:
            raise ValueError(f"产品描述PDF最多支持{MAX_PDF_PAGES}页")
        page_texts = [page.get_text("text") for page in document]
        usable = sum(len(value.strip()) for value in page_texts)
        if usable >= 40:
            return "\n".join(page_texts), "pdf_text", warnings

        # 扫描件仅使用本地RapidOCR，不上传到外部AI服务。
        try:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR()
        except Exception:
            warnings.append("该PDF没有可提取的文字层，且本地OCR不可用，请人工填写材料覆盖数量")
            return "", "unavailable", warnings

        ocr_lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="sampling-material-ocr-") as temp_dir:
            for index, page in enumerate(document):
                image_path = Path(temp_dir) / f"page-{index + 1}.png"
                page.get_pixmap(dpi=180, alpha=False).save(str(image_path))
                result, _ = engine(str(image_path))
                if result:
                    ocr_lines.extend(str(row[1]) for row in result if len(row) > 1)
        warnings.append("该PDF为扫描件，已使用本地OCR识别，供应商名称和备案号需人工确认")
        return "\n".join(ocr_lines), "local_ocr", warnings
    finally:
        document.close()


def extract_description_text(filename: str, data: bytes) -> tuple[str, str, list[str]]:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        if suffix == ".doc":
            raise ValueError("旧版DOC暂不支持自动识别，请另存为DOCX或PDF")
        raise ValueError("仅支持PDF、DOCX和TXT产品描述")
    if not data:
        raise ValueError("上传文件为空")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("单个产品描述文件不能超过15MB")
    if suffix == ".pdf":
        text, method, warnings = _extract_pdf(data)
    elif suffix == ".docx":
        text, method, warnings = _extract_docx(data), "docx_text", []
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030", errors="replace")
        method, warnings = "plain_text", []
    return _normalize_text(text), method, warnings


def _supplier_records(section: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    lines = [line.strip(" \t;；,，") for line in section.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        matches = list(SUPPLIER_PATTERN.finditer(line))
        for position, match in enumerate(matches):
            name = match.group(0).strip(" \t;；,，")
            if name in {"有限公司", "公司"} or "供应商" in name:
                continue
            # 备案号只能归属本供应商之后、下一供应商之前的文字。
            end = matches[position + 1].start() if position + 1 < len(matches) else len(line)
            nearby = line[match.end():end]
            if position == len(matches) - 1 and index + 1 < len(lines):
                continuation = lines[index + 1]
                if CQC_PATTERN.search(continuation) and not SUPPLIER_PATTERN.search(continuation) and re.fullmatch(r"[\s（()）:：CQCcqc0-9_\-、,，;；]+", continuation):
                    nearby += " " + continuation
            filing_matches = CQC_PATTERN.findall(nearby)
            filings = [f"CQC{number}" for number in filing_matches]
            current = records.setdefault(name, {"name": name, "cqc_numbers": []})
            current["cqc_numbers"] = sorted(set(current["cqc_numbers"] + filings))
            if "CQC" in nearby.upper() and not filings:
                current["cqc_needs_review"] = True
    return list(records.values())


def _selected_brands(section: str) -> tuple[list[str], bool]:
    selected = sorted({_normalize_brand(value) for value in SELECTED_BRAND_PATTERN.findall(section)})
    if selected:
        return selected, False
    mentioned = sorted({_normalize_brand(value) for value in BRAND_PATTERN.findall(section)})
    # 文字提取丢失勾选标记时，保留候选但强制人工确认。
    return mentioned, bool(mentioned)


def _section_map(text: str) -> dict[str, str]:
    body = re.split(r"(?m)^\s*说\s*明\s*[:：]", text, maxsplit=1)[0]
    markers: list[tuple[int, str]] = []
    patterns = {
        "conductor": r"(?m)(?:^\s*导体\s*$|^\s*导体材料名称)",
        "insulation": r"(?m)(?:^\s*绝缘\s*$|^\s*绝缘材料名称)",
        "shield": r"(?m)(?:^\s*屏蔽\s*$|^\s*屏蔽材料名称)",
        "outer_braid": r"(?m)(?:^\s*外编织层\s*$|^\s*编织层材料)",
        "sheath": r"(?m)(?:^\s*护套\s*$|^\s*护套材料名称)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, body)
        if match:
            markers.append((match.start(), key))
    markers.sort()
    sections: dict[str, str] = {}
    for index, (start, key) in enumerate(markers):
        end = markers[index + 1][0] if index + 1 < len(markers) else len(body)
        sections[key] = body[start:end]
    return sections


def _group(
    *, group_code: str, category: str, suppliers: list[dict[str, Any]], brands: list[str],
    needs_review: bool, source: str,
) -> dict[str, Any]:
    total = len(suppliers)
    filed = sum(1 for supplier in suppliers if supplier["cqc_numbers"])
    return {
        "group_code": group_code,
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "compatibility_group": group_code,
        "total_items": total,
        "cqc_filed_items": filed,
        "model_refs": [],
        "suppliers": suppliers,
        "material_brands": brands,
        "needs_review": needs_review or any(s.get("cqc_needs_review") for s in suppliers),
        "source": source,
    }


def recognize_material_groups(text: str, source: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    sections = _section_map(text)
    groups: list[dict[str, Any]] = []
    warnings: list[str] = []

    conductor = sections.get("conductor", "")
    if conductor:
        suppliers = _supplier_records(conductor)
        selected_copper = bool(re.search(r"(?:√|☑|■|◉)\s*[^\n]{0,30}铜", conductor))
        selected_aluminum = bool(re.search(r"(?:√|☑|■|◉)\s*[^\n]{0,30}铝", conductor))
        if suppliers and (selected_copper or not selected_aluminum):
            groups.append(_group(group_code="铜导体供应商", category="copper_conductor", suppliers=suppliers,
                                 brands=[], needs_review=not selected_copper, source=source))
        if suppliers and selected_aluminum:
            groups.append(_group(group_code="铝导体供应商", category="aluminum_conductor", suppliers=suppliers,
                                 brands=[], needs_review=False, source=source))

    for section_key, normal_category, high_category, label in (
        ("insulation", "insulation_normal", "insulation_90", "绝缘"),
        ("sheath", "sheath_normal", "sheath_90", "护套"),
    ):
        section = sections.get(section_key, "")
        if not section:
            continue
        suppliers = _supplier_records(section)
        brands, uncertain_marks = _selected_brands(section)
        if not suppliers:
            # 空白模板也会列出全部可选牌号，不能因此生成0项覆盖组。
            continue
        if suppliers and not brands:
            warnings.append(f"{source or '产品描述'}：识别到{label}供应商，但未识别到材料牌号，请人工增加材料组")
            continue
        for brand in brands:
            category = high_category if "90" in brand else normal_category
            groups.append(_group(group_code=f"{brand} {label}", category=category, suppliers=suppliers,
                                 brands=[brand], needs_review=uncertain_marks or not suppliers, source=source))
        if uncertain_marks:
            warnings.append(f"{source or '产品描述'}：{label}牌号的勾选标记不清晰，已按文档中出现的牌号生成待确认项")

    braid = sections.get("outer_braid", "")
    if braid:
        suppliers = _supplier_records(braid)
        if suppliers:
            brands, uncertain = _selected_brands(braid)
            groups.append(_group(group_code="外编织层 YG", category="outer_braid", suppliers=suppliers,
                                 brands=brands or ["YG"], needs_review=uncertain, source=source))

    if re.search(r"委外辐照|外部辐照|辐照加工", text):
        warnings.append(f"{source or '产品描述'}：检测到委外辐照信息，请人工核对“电缆料供应商+辐照厂”组合数量")
    if not groups:
        warnings.append(f"{source or '产品描述'}：未形成可自动填入的材料组，请使用人工录入")
    if any(s.get("cqc_needs_review") for g in groups for s in g["suppliers"]):
        warnings.append("部分供应商备案号格式不完整，未计入备案数量；仅需确认这些标记项")
    return groups, warnings


def merge_recognition_results(documents: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    warnings: list[str] = []
    for document in documents:
        warnings.extend(document.get("warnings") or [])
        for group in document.get("groups") or []:
            key = (group["category"], group["group_code"])
            current = merged.get(key)
            if current is None:
                merged[key] = dict(group)
                continue
            supplier_map = {supplier["name"]: dict(supplier) for supplier in current["suppliers"]}
            for supplier in group["suppliers"]:
                existing = supplier_map.setdefault(supplier["name"], dict(supplier))
                existing["cqc_numbers"] = sorted(set(existing.get("cqc_numbers", []) + supplier.get("cqc_numbers", [])))
            current["suppliers"] = list(supplier_map.values())
            current["total_items"] = len(current["suppliers"])
            current["cqc_filed_items"] = sum(1 for supplier in current["suppliers"] if supplier["cqc_numbers"])
            current["needs_review"] = bool(current["needs_review"] or group["needs_review"])
            current["source"] = "、".join(dict.fromkeys(filter(None, [current.get("source"), group.get("source")])))
    groups = list(merged.values())
    return {
        "documents": [{key: value for key, value in document.items() if key != "groups"} for document in documents],
        "groups": groups,
        "warnings": list(dict.fromkeys(warnings)),
        "group_count": len(groups),
        "review_count": sum(1 for group in groups if group.get("needs_review")),
    }
