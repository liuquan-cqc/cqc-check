"""PDF文本提取：优先pymupdf文字层；扫描件则渲染图片+OCR。"""
import os
import re
from pathlib import Path
from typing import Tuple, List
from backend.app.config import get_settings


EXTRACTION_CACHE_MARKER = "<!-- extraction-cache-v3-row-safe-mineru-tables -->"


def _local_ocr_runtime_identity() -> dict | None:
    """Identify shipped default OCR models/config, not just a package version."""
    import hashlib
    from importlib.metadata import distribution, version, PackageNotFoundError
    try:
        package=distribution('rapidocr-onnxruntime')
        files={}
        for entry in package.files or []:
            path=Path(str(entry))
            if path.parts[0]!='rapidocr_onnxruntime' or path.suffix not in {'.onnx','.yaml','.yml','.txt','.py'}:
                continue
            files[str(path)]=hashlib.sha256(Path(package.locate_file(entry)).read_bytes()).hexdigest()
        if not any(name.endswith('.onnx') for name in files):
            return None
        return {'rapidocr':package.version,'onnxruntime':version('onnxruntime'),
                'pymupdf':version('PyMuPDF'),'files':files}
    except (PackageNotFoundError,OSError):
        return None


def _valid_local_table_evidence(value: object, identity: dict) -> bool:
    """Reject truncated, malformed or off-page evidence before cache reuse."""
    import math
    def number(value):
        return type(value) in (int,float) and math.isfinite(value)
    if not isinstance(value,dict) or value.get('identity')!=identity or value.get('status')!='collected':
        return False
    pages=value.get('pages')
    if not isinstance(pages,list) or len(pages)!=len(identity['pages']):
        return False
    for expected,page in zip(identity['pages'],pages):
        if not isinstance(page,dict) or type(page.get('page')) is not int or page['page']!=expected:
            return False
        if any(type(page.get(key)) is not int or page[key]<=0 for key in ('width','height')):
            return False
        rows=page.get('rows')
        if not isinstance(rows,list) or not rows:
            return False
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('text'),str) or not row['text'].strip():
                return False
            score=row.get('confidence')
            if not number(score) or not 0<=score<=1:
                return False
            box=row.get('box')
            if not isinstance(box,list) or len(box)!=4:
                return False
            for point in box:
                if not isinstance(point,list) or len(point)!=2 or not all(number(n) for n in point):
                    return False
                if not (0<=point[0]<=page['width'] and 0<=point[1]<=page['height']):
                    return False
            if len({point[0] for point in box})<2 or len({point[1] for point in box})<2:
                return False
        alternates=page.get('unit_corroboration_pages',[])
        measurements=page.get('measurement_corroboration_pages',[])
        if not isinstance(measurements,list) or len(measurements)>2:
            return False
        if measurements:
            # Validate both groups independently, without allowing nested
            # support or duplicate scales within either group.
            shadow={**page,'unit_corroboration_pages':measurements}
            shadow.pop('measurement_corroboration_pages',None)
            if not _valid_local_table_evidence({'status':'collected','identity':{**identity,'pages':[expected]},'pages':[shadow]}, {**identity,'pages':[expected]}):
                return False
        if not isinstance(alternates,list) or len(alternates)>2:
            return False
        scales=set()
        for alternate in alternates:
            if not isinstance(alternate,dict) or alternate.get('unit_corroboration_pages') or alternate.get('measurement_corroboration_pages'):
                return False
            scale=alternate.get('render_scale')
            if type(scale) is not int or scale not in (3,4) or scale in scales:
                return False
            scales.add(scale)
            if any(not number(alternate.get(k)) or abs(alternate[k]-page[k]*scale/2)>2 for k in ('width','height')):
                return False
            alternate_identity={**identity,'pages':[expected]}
            if not _valid_local_table_evidence({'status':'collected','identity':alternate_identity,'pages':[alternate]},alternate_identity):
                return False
    return True


def collect_local_table_evidence(pdf_path: str, text: str, ocr_dir: str) -> dict:
    """Bounded batches preserve completed evidence without dropping 9+ pages."""
    from backend.app.mineru_pages import collapsed_measurement_pages
    pages = sorted(set(collapsed_measurement_pages(text)) | set(
        _validated_auxiliary_page_hints(pdf_path, text, ocr_dir)
    ) | set(_paddle_crosscheck_pages(text)))
    if len(pages) <= 8:
        return _collect_local_table_batch(pdf_path, text, ocr_dir, pages)
    if len(pages) > 64:
        return {'status':'unresolved','reason':'local_page_budget_exceeded','requested_pages':pages,'pages':[]}
    batches=[]
    for offset in range(0,len(pages),8):
        batch=_collect_local_table_batch(pdf_path,text,ocr_dir,pages[offset:offset+8])
        if batch.get('status')!='collected':
            return {'status':'unresolved','reason':'local_batch_incomplete',
                    'batch_reason':batch.get('reason'),'requested_pages':pages,'pages':[],
                    'completed_pages':[p['page'] for b in batches for p in b['pages']]}
        if batches:
            current={k:v for k,v in batch['identity'].items() if k!='pages'}
            first={k:v for k,v in batches[0]['identity'].items() if k!='pages'}
            if current!=first:
                return {'status':'unresolved','reason':'batch_identity_changed','requested_pages':pages,'pages':[]}
        batches.append(batch)
    identity={**batches[0]['identity'],'pages':pages}
    result={'status':'collected','identity':identity,'pages':[p for b in batches for p in b['pages']],
            'batch_count':len(batches),'boundary':'internal evidence only; not standard limits or accepted audit verdicts'}
    if not _valid_local_table_evidence(result,identity):
        return {'status':'unresolved','reason':'invalid_merged_evidence','requested_pages':pages,'pages':[]}
    return result


def _paddle_crosscheck_pages(text: str) -> list[int]:
    """Paddle primary needs independent coordinates even when its HTML looks clean.

    Page selection is not measurement acceptance. Existing coordinate/source
    identity checks still decide whether any resulting observation is usable.
    """
    headers = list(re.finditer(
        r'^--- Page (\d+) \(PaddleOCR (?:whole-PDF )?original-page bound\) ---\s*$',
        text,
        re.M,
    ))
    numbers = [int(h[1]) for h in headers]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError('PaddleOCR原页标记重复，禁止推测来源')
    pages = []
    for i, header in enumerate(headers):
        body = text[header.end():headers[i+1].start() if i+1 < len(headers) else len(text)]
        if re.search(r'失重试验|高温压力|老化前抗张强度|老化后抗张强度|绝缘电阻', body):
            pages.append(int(header[1]))
    return pages


def _validated_auxiliary_page_hints(pdf_path: str, text: str, ocr_dir: str) -> list[int]:
    """Load source-bound page-selection hints from an independent parser.

    Hints may only request offline coordinate OCR. They never supply values,
    units, verdicts, sample ownership, or standard limits. Invalid or stale
    sidecars are ignored, preserving the existing MinerU-only behavior.
    """
    import hashlib
    import json

    sidecar = Path(ocr_dir) / "auxiliary_paddle_page_hints.json"
    if not sidecar.is_file():
        return []
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(value, dict) or value.get("schema") != 1:
        return []
    if value.get("provider") != "PaddleOCR" or value.get("model") != "PaddleOCR-VL-1.6":
        return []
    if value.get("source_sha256") != hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest():
        return []
    if value.get("mineru_text_sha256") != hashlib.sha256(text.encode()).hexdigest():
        return []
    if value.get("binding_policy") != "unique_exact_report_number_and_printed_page_pair":
        return []
    if value.get("all_flagged_pages_bound") is not True:
        return []
    if value.get("usage") != "page_selection_only" or value.get("conflict_policy") != "block":
        return []
    records = value.get("pages")
    if not isinstance(records, list) or not records or len(records) > 32:
        return []
    allowed_reasons = {
        "measurement_attached_to_section_heading",
        "measurement_unit_conflict",
        "measurement_row_without_cells",
        "multiple_measurement_labels_in_row",
        "invalid_table_structure",
        "source_table_shape_conflict",
    }
    pages: list[int] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"page", "reasons"}:
            return []
        page = record.get("page")
        reasons = record.get("reasons")
        if type(page) is not int or page <= 0 or page > 4096:
            return []
        if not isinstance(reasons, list) or not reasons or any(
            reason not in allowed_reasons for reason in reasons
        ):
            return []
        # PaddleOCR remains a page-discovery aid only.  Its broader warnings
        # stay in the bound sidecar for diagnostics, but only a structural
        # disagreement with the source table may request coordinate OCR from
        # the original PDF.
        if "source_table_shape_conflict" in reasons:
            pages.append(page)
    if len(set(pages)) != len(pages):
        return []
    return sorted(pages)


def _collect_local_table_batch(pdf_path: str, text: str, ocr_dir: str, pages: list[int]) -> dict:
    """Collect bounded offline evidence separately; never rewrite source text.

    No recovered row is a verdict. Consumers must bind samples and apply rules.
    Failed/partial attempts are not cached as completed evidence.
    """
    import hashlib
    import json
    import tempfile
    if not pages:
        return {'status':'not_needed','pages':[]}
    if len(pages)>8:
        return {'status':'unresolved','reason':'local_page_budget_exceeded','requested_pages':pages,'pages':[]}
    if get_settings().ocr_disabled:
        return {'status':'unresolved','reason':'local_ocr_disabled','requested_pages':pages,'pages':[]}
    source = Path(pdf_path)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    # Code and exact source bind this sidecar, independently of model caches.
    from backend.app import mineru_pages
    runtime = _local_ocr_runtime_identity()
    if runtime is None:
        return {'status':'unresolved','reason':'local_ocr_unavailable','requested_pages':pages,'pages':[]}
    identity_base = {'source_sha256':source_hash, 'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'extract_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'parser_sha256':hashlib.sha256(Path(mineru_pages.__file__).read_bytes()).hexdigest(),
                'local_runtime':runtime,'render_scale':2,'schema':4}
    import fitz
    try:
        with fitz.open(pdf_path) as document:
            if any(p>len(document) for p in pages):
                return {'status':'unresolved','reason':'invalid_page_reference','requested_pages':pages,'pages':[]}
            observations={}
            missing=[]
            page_caches={}
            for number in pages:
                page_identity={**identity_base,'pages':[number]}
                fingerprint=hashlib.sha256(json.dumps(page_identity,sort_keys=True).encode()).hexdigest()
                cache=Path(ocr_dir)/f'local_table_page_evidence_{fingerprint}.json'
                page_caches[number]=(page_identity,cache)
                if cache.is_file():
                    try:
                        receipt=json.loads(cache.read_text())
                        if _valid_local_table_evidence(receipt,page_identity):
                            observations[number]=receipt['pages'][0]
                            continue
                    except (OSError,ValueError,KeyError,TypeError):
                        pass
                missing.append(number)

            if missing:
                engine = _get_rapidocr()
                if engine is None:
                    return {'status':'unresolved','reason':'local_ocr_unavailable','requested_pages':pages,'pages':[]}
                with tempfile.TemporaryDirectory(prefix='audit-local-tables-') as temporary:
                    for number in missing:
                        image=document[number-1].get_pixmap(matrix=fitz.Matrix(2,2),alpha=False)
                        path=Path(temporary)/f'{number}.png'
                        image.save(path)
                        rows,_=engine(str(path))
                        if not rows:
                            return {'status':'unresolved','reason':'empty_local_ocr','requested_pages':pages,'pages':[],
                                    'completed_pages':[p for p in pages if p in observations]}
                        page={'page':number,'width':image.width,'height':image.height,
                            'rows':[{'box':box,'text':value,'confidence':float(score)} for box,value,score in rows]}
                        # Bounded offline resampling; preserve primary evidence.
                        # Consumers independently require same-row agreement before
                        # recovering any unit, measurement, or condition.
                        uncertain_units=any(.60<=r['confidence']<.95 and re.fullmatch(
                            r'N/mm(?:2|²)|mg/cm(?:2|²)|[%％]',re.sub(r'\s+','',r['text'])) for r in page['rows'])
                        from backend.app.mineru_pages import coordinate_row_observations, coordinate_pressure_conditions
                        target_rows=coordinate_row_observations(page,r'^(?:失重试验|高温压力)')['observations']
                        uncertain_measurements=any(r.get('report_verdict')!='N' and (
                            r['status']!='located' or coordinate_pressure_conditions(page,r,kind='loss' if r['label'].startswith('失重') else 'pressure')['status']!='located') for r in target_rows)
                        if uncertain_units or uncertain_measurements:
                            alternate_pages=[]
                            for scale in (3,4):
                                alternate=document[number-1].get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False)
                                alternate.save(path)
                                alternate_rows,_=engine(str(path))
                                if alternate_rows:
                                    alternate_pages.append({'page':number,'width':alternate.width,'height':alternate.height,
                                        'render_scale':scale,'rows':[{'box':b,'text':v,'confidence':float(s)} for b,v,s in alternate_rows]})
                            page['unit_corroboration_pages']=alternate_pages
                            if uncertain_measurements:
                                page['measurement_corroboration_pages']=alternate_pages

                        if hashlib.sha256(source.read_bytes()).hexdigest()!=source_hash:
                            raise ValueError('PDF changed during local evidence collection')
                        page_identity,cache=page_caches[number]
                        receipt={'status':'collected','identity':page_identity,'pages':[page],
                            'boundary':'internal evidence only; not standard limits or accepted audit verdicts'}
                        if not _valid_local_table_evidence(receipt,page_identity):
                            raise ValueError('Invalid local OCR evidence')
                        cache.parent.mkdir(parents=True,exist_ok=True)
                        with tempfile.NamedTemporaryFile(mode='w',dir=cache.parent,prefix='.local-page-evidence-',suffix='.tmp',delete=False) as handle:
                            temporary_path=Path(handle.name)
                            try:
                                json.dump(receipt,handle,ensure_ascii=False)
                                handle.flush()
                                os.fsync(handle.fileno())
                            except BaseException:
                                temporary_path.unlink(missing_ok=True)
                                raise
                        try:
                            os.replace(temporary_path,cache)
                        finally:
                            temporary_path.unlink(missing_ok=True)
                        observations[number]=page
        if hashlib.sha256(source.read_bytes()).hexdigest()!=source_hash:
            raise ValueError('PDF changed during local evidence collection')
        identity={**identity_base,'pages':pages}
        saved={'status':'collected','identity':identity,'pages':[observations[p] for p in pages],
               'cache_hits':len(pages)-len(missing),'ocr_pages':missing,
               'boundary':'internal evidence only; not standard limits or accepted audit verdicts'}
        if not _valid_local_table_evidence(saved,identity):
            raise ValueError('Invalid local OCR evidence')
        # Supplemental proofs are regenerated from the current PDF, not trusted
        # from persisted OCR receipts. The source identity above is mandatory.
        from backend.app.ocr_evidence_support import enrich_verified_pages
        saved = enrich_verified_pages(saved, str(source), _get_rapidocr)
        return saved
    except Exception as exc:
        # No exception text here: paths/configuration must not leak into UI.
        return {'status':'unresolved','reason':type(exc).__name__,'requested_pages':pages,'pages':[]}


def _mark_empty_local_ocr_pages(cached: str, ocr_dir: Path) -> str:
    """旧缓存中的空本地OCR页同样必须保留缺页标记；不改有正文的页。"""
    pattern = r'^--- Page (\d+) \(rapidocr(?: empty)?\) ---[^\S\r\n]*\r?\n(.*?)(?=^--- Page \d+|^--- MinerU|\Z)'
    def replace(match):
        if match[2].strip():
            return match[0]
        image = ocr_dir / f'page_{int(match[1]):03d}.png'
        return f'--- Page {match[1]} (rapidocr empty) ---\n[IMAGE:{image}]\n'
    return re.sub(pattern, replace, cached, flags=re.M | re.S)


def _upgrade_cached_mineru_pages(cached: str, ocr_path: Path) -> str:
    """在内存中恢复旧MinerU缓存页码；完整性由调用方核对原PDF。

    不覆盖原缓存：结果包可能缺页，验证失败后仍需保留原证据。
    """
    segments = re.split(r'^--- MinerU document parse[^\n]*---[^\S\r\n]*\n', cached, flags=re.M)
    # Local cross-check pages do not establish page metadata for the separate
    # authoritative MinerU section. Never merge ambiguous multiple sections.
    if len(segments) != 2 or re.search(r'^--- Page ', segments[-1], re.M):
        return cached
    archive_path = ocr_path.parent / "mineru_result.zip"
    if not archive_path.is_file():
        return cached
    try:
        from backend.app.mineru_pages import page_marked_markdown_from_zip
        page_text = page_marked_markdown_from_zip(archive_path)
    except Exception:
        # 旧结果包不完整时继续使用原缓存并进入证据不足路径，不猜页码。
        return cached
    upgraded = (
        EXTRACTION_CACHE_MARKER
        + '\n' + segments[0].removeprefix(EXTRACTION_CACHE_MARKER).lstrip('\r\n')
        + "\n--- MinerU document parse (page metadata cache upgrade) ---\n"
        + page_text
    )
    return upgraded


def _is_dense_table(text: str) -> bool:
    """判断文本是否属于密集表格页（含三栏结构）。"""
    keywords = ["标准要求", "检验结果", "单项评定", "报告结果", "检验要求"]
    return sum(1 for k in keywords if k in text) >= 2


def _text_layer_is_usable(text: str, structured_text: str = "") -> bool:
    """判断文字层能否可靠用于审核；密集表格还需具备基本行列与数值信息。"""
    clean = text.strip()
    if not clean:
        return False
    replacement_count = clean.count("�") + clean.count("□")
    if replacement_count / max(len(clean), 1) > 0.01:
        return False
    if not _is_dense_table(clean):
        return True

    candidate = (structured_text or clean).strip()
    lines = [line for line in candidate.splitlines() if line.strip()]
    numeric_values = re.findall(r"(?<!\w)[+-]?\d+(?:\.\d+)?(?:\s*[%A-Za-zΩ℃°/]+)?", candidate)
    return len(candidate) >= 80 and len(lines) >= 5 and len(numeric_values) >= 3


def _markdown_table(rows: list[list[object]], table_index: int) -> str:
    normalized = [[str(cell or "").replace("\n", " ").strip() for cell in row] for row in rows if row]
    normalized = [row for row in normalized if any(row)]
    if not normalized:
        return ""
    width = max(len(row) for row in normalized)
    normalized = [row + [""] * (width - len(row)) for row in normalized]
    header = normalized[0]
    lines = [f"[结构化表格 {table_index}]", "| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
    return "\n".join(lines)


def _extract_structured_tables(page) -> str:
    """使用 PDF 文字坐标恢复整页阅读顺序和表格列间距。"""
    try:
        words = page.get_text("words", sort=True)
    except Exception:
        words = []
    rows: list[list[tuple[float, float, str, float]]] = []
    for word in sorted(words, key=lambda item: (round(float(item[1]) / 3), float(item[0]))):
        x0, y0, x1, _, value = float(word[0]), float(word[1]), float(word[2]), word[3], str(word[4]).strip()
        if not value:
            continue
        if not rows or abs(rows[-1][0][1] - y0) > 3:
            rows.append([])
        rows[-1].append((x0, y0, value, x1))
    lines: list[str] = []
    for row in rows:
        row.sort(key=lambda item: item[0])
        parts: list[str] = []
        previous_right: float | None = None
        for x0, _, value, x1 in row:
            separator = "\t" if previous_right is not None and x0 - previous_right > 18 else " "
            parts.append((separator if parts else "") + value)
            previous_right = x1
        lines.append("".join(parts))
    coordinate_text = "\n".join(lines)
    if coordinate_text.strip():
        return coordinate_text

    # 极少数 PDF 无法返回 words，退回 PyMuPDF 的表格探测结果。
    table_parts: list[str] = []
    try:
        finder = page.find_tables()
        for index, table in enumerate(getattr(finder, "tables", []), start=1):
            rendered = _markdown_table(table.extract() or [], index)
            if rendered:
                table_parts.append(rendered)
    except Exception:
        pass
    return "\n\n".join(table_parts)


def _get_rapidocr():
    """懒加载本地OCR；缺失则返回None。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
    except Exception:
        return None


def _mineru_has_complete_pages(text: str, page_count: int) -> bool:
    """Only explicit, unique, nonempty structured pages can replace missing OCR.

    This establishes page coverage, not recognition accuracy. Unpaged Markdown,
    empty pages and image-only placeholders cannot prove that coverage.
    """
    headers = list(re.finditer(r'^--- Page (\d+) \(MinerU structured\) ---\s*$', text, re.M))
    if page_count < 1 or [int(m[1]) for m in headers] != list(range(1, page_count + 1)):
        return False
    if text[:headers[0].start()].strip():
        return False
    for index, header in enumerate(headers):
        body = text[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)]
        if re.search(r'\[IMAGE:|\[MINERU_ERROR:|^--- Page ', body, re.M):
            return False
        # An image reference, HTML/Markdown decoration or a cache comment alone
        # is not recognized report content.
        visible = re.sub(r'<!--.*?-->|!\[[^\]]*\]\([^)]*\)|<[^>]*>', '', body, flags=re.S)
        if not re.search(r'[\w\u4e00-\u9fff]', visible):
            return False
    return True


def _validate_cached_page_coverage(cached: str, page_count: int) -> str:
    """Validate against the source PDF before reuse; never silently re-upload.

    Old mixed caches may contain both local and authoritative MinerU pages.
    Only complete MinerU coverage may supersede old pending-image markers.
    """
    segments = re.split(r'^--- MinerU document parse[^\n]*---[^\S\r\n]*\n', cached, flags=re.M)
    if len(segments) > 1:
        mineru_text = segments[-1]
        notice = '[表格审核时以本段的行列对应关系为准；前面的PDF文字层仅用于基础信息交叉核对]'
        mineru_text = mineru_text.removeprefix(notice + '\n')
        if len(segments) != 2 or not _mineru_has_complete_pages(mineru_text, page_count):
            raise RuntimeError('OCR缓存页码覆盖不完整或存在空页，已停止审核；请核实原PDF并重新解析')
        # Preserve cross-check text and the authoritative section. Only obsolete
        # pending-image requests are removed, after complete coverage is proven.
        return re.sub(r'\[IMAGE:[^\]\n]+\]', '', cached)

    local = cached.removeprefix(EXTRACTION_CACHE_MARKER).strip()
    headers = list(re.finditer(r'^--- Page (\d+) \(([^\n)]+)\) ---[^\S\r\n]*\n', local, re.M))
    pages = {}
    invalid = not headers or bool(local[:headers[0].start()].strip())
    for index, header in enumerate(headers):
        body = local[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(local)]
        pages.setdefault(int(header[1]), []).append((header[2], body))
    ocr_labels = {'rapidocr', 'rapidocr empty', 'rapidocr missing', 'vision OCR'}
    normalized = []
    for number, entries in sorted(pages.items()):
        primary = [(label, body) for label, body in entries if label != 'text layer fallback']
        fallback = [body for label, body in entries if label == 'text layer fallback']
        # Extraction stores text layers before OCR pages, not in PDF order.
        # A weak text layer is allowed only alongside exactly one OCR entry
        # for that same page. Two primary sources still require investigation.
        if (len(primary) != 1 or len(fallback) > 1
                # Legacy table-aware text extraction used this primary label.
                # It carries no exemption from page/body/duplicate validation.
                or primary[0][0] not in ocr_labels | {'text layer', 'text layer structured'}
                or (fallback and primary[0][0] not in ocr_labels)):
            invalid = True
            continue
        normalized.append(f'--- Page {number} (MinerU structured) ---\n{primary[0][1]}')
    local = '\n'.join(normalized)
    # Pending images still go through review's fail-closed cache check. This is
    # solely a coverage check and must not convert a pending page into OCR text.
    coverage_only = re.sub(r'\[IMAGE:[^\]\n]+\]', 'pending OCR checked by review', local)
    if invalid or not _mineru_has_complete_pages(coverage_only, page_count):
        raise RuntimeError('OCR缓存页码覆盖不完整或存在空页，已停止审核；请核实原PDF并重新解析')
    return cached


def _textlayer_primary_preferred(pdf_path: str, ocr_settings: dict) -> bool:
    """全部页面文字层通过质量门时，优先文字层主解析（Paddle 仅作对照）。

    实验室软件生成的 PDF 内嵌文字层与打印内容同源，比图像 OCR 少一道
    有损转录；逐页质量门 (_text_layer_is_usable) 与本地 mixed 路径完全
    相同，任一页不达标即整份走 Paddle，不牺牲扫描件质量。可用 ocr 设置
    textlayer_primary=false 关闭。
    """
    if ocr_settings.get("textlayer_primary", True) is False:
        return False
    import fitz
    doc = fitz.open(pdf_path)
    try:
        total = len(doc)
        if total == 0:
            return False
        usable = 0
        for page_idx in range(total):
            text = doc.load_page(page_idx).get_text("text")
            if _text_layer_is_usable(text, text):
                usable += 1
            elif _is_dense_table(text):
                # 密集表格页承载实测数据，必须文字层可用；
                # 否则整份走 Paddle，不拿图像 OCR 补数据页。
                return False
        # 封面/盖章页等少数非表格页允许质量门不过（走本地 OCR 补齐），
        # 但文字层可用页需覆盖至少 80%。
        return usable / total >= 0.8
    finally:
        doc.close()


def extract_pdf(pdf_path: str, ocr_dir: str) -> Tuple[str, str]:
    """
    提取PDF全部文本，返回 (text, ocr_path)。
    ocr_path 为保存文本内容的文件路径。
    """
    from backend.app.settings_store import get_section
    ocr_settings = get_section("ocr")
    engine = str(ocr_settings.get("engine") or "rapidocr").lower()
    if engine in {'paddleocr','paddleocr_vl','paddleocr-vl','paddleocr-vl-1.6'}:
        from backend.app import paddle_runtime
        try:
            if not _textlayer_primary_preferred(pdf_path, ocr_settings):
                return paddle_runtime.extract(pdf_path, ocr_dir, ocr_settings)
            # 全部页面文字层通过质量门：跳过 Paddle 主解析，落到下方本地
            # mixed 路径，由文字层直接充当审核主文本（与 rapidocr 时代同
            # 一套已验证逻辑；证人仍保留做逐项对照）。
        except paddle_runtime.TransientSubmissionError:
            # 供应商队列已满等临时性拒绝：回退本地通用提取，保证报告仍可审核。
            pass
    # 只有文件完整写入后才会出现 extracted.txt，因此可安全作为
    # 重试和 Worker 重启时的提取缓存，避免重复渲染整份 PDF。
    ocr_path = Path(ocr_dir) / "extracted.txt"
    if ocr_path.is_file():
        try:
            cached = ocr_path.read_text(encoding="utf-8")
            cached = _upgrade_cached_mineru_pages(cached, ocr_path)
            cached = _mark_empty_local_ocr_pages(cached, ocr_path.parent)
            image_refs = re.findall(r"\[IMAGE:(.+?)\]", cached)
            # MinerU 段落会根据解析路径附加说明，例如
            # "(authoritative for dense tables)"。只要存在 MinerU 段落即可
            # 复用缓存，避免任务重试时重新渲染、OCR 和上传整份 PDF。
            cache_engine_ok = engine not in {"mineru", "mineru_hybrid", "hybrid"} or (
                "--- MinerU document parse" in cached
            )
            if cached.startswith(EXTRACTION_CACHE_MARKER) and cache_engine_ok and all(Path(ref).is_file() for ref in image_refs):
                import fitz
                source_doc = fitz.open(pdf_path)
                try:
                    validated = _validate_cached_page_coverage(cached, len(source_doc))
                finally:
                    source_doc.close()
                return validated, str(ocr_path)
        except OSError:
            pass

    import fitz  # PyMuPDF
    settings = get_settings()
    # MinerU 解析整份 PDF，适合需要保留版面和复杂表格结构的报告。
    # 结果写入同一份提取缓存目录，重试时不会重复上传。
    if engine == "mineru":
        from backend.app.mineru import MinerUClient
        mineru_cfg = {
            "base_url": ocr_settings.get("mineru_base_url"),
            "task_endpoint": ocr_settings.get("mineru_task_endpoint"),
            "model_version": ocr_settings.get("mineru_model_version"),
            "api_key": ocr_settings.get("mineru_api_key"),
            "timeout_seconds": ocr_settings.get("mineru_timeout_seconds"),
            "poll_interval_seconds": ocr_settings.get("mineru_poll_interval_seconds"),
        }
        mineru_path = MinerUClient(mineru_cfg).parse_pdf(pdf_path, ocr_dir)
        mineru_text = Path(mineru_path).read_text(encoding="utf-8")
        source_doc = fitz.open(pdf_path)
        try:
            complete = _mineru_has_complete_pages(mineru_text, len(source_doc))
        finally:
            source_doc.close()
        if not complete:
            raise ValueError("MinerU页码覆盖不完整或存在空页，已停止审核")
        full_text = EXTRACTION_CACHE_MARKER + "\n--- MinerU document parse ---\n" + mineru_text
        ocr_path.write_text(full_text, encoding="utf-8")
        return full_text, str(ocr_path)
    doc = fitz.open(pdf_path)
    page_count = len(doc)

    # 全扫描件在混合模式下直接交给 MinerU。旧流程会先对
    # 每页执行 RapidOCR，随后又用 MinerU 解析整份文档，同一份
    # 扫描报告因此被重复识别。MinerU 失败时仍落回原有本地 OCR。
    if engine in {"mineru_hybrid", "hybrid"}:
        has_usable_text_layer = False
        for page_idx in range(len(doc)):
            probe_text = doc.load_page(page_idx).get_text("text")
            if _text_layer_is_usable(probe_text, probe_text):
                has_usable_text_layer = True
                break
        if not has_usable_text_layer:
            try:
                from backend.app.mineru import MinerUClient
                mineru_cfg = {
                    "base_url": ocr_settings.get("mineru_base_url"),
                    "task_endpoint": ocr_settings.get("mineru_task_endpoint"),
                    "model_version": ocr_settings.get("mineru_model_version"),
                    "api_key": ocr_settings.get("mineru_api_key"),
                    "timeout_seconds": ocr_settings.get("mineru_timeout_seconds"),
                    "poll_interval_seconds": ocr_settings.get("mineru_poll_interval_seconds"),
                }
                mineru_path = MinerUClient(mineru_cfg).parse_pdf(pdf_path, ocr_dir)
                mineru_text = Path(mineru_path).read_text(encoding="utf-8")
                if not _mineru_has_complete_pages(mineru_text, page_count):
                    raise ValueError("MinerU页码覆盖不完整或存在空页，改用本地OCR")
                full_text = EXTRACTION_CACHE_MARKER + "\n--- MinerU document parse (scanned document) ---\n" + mineru_text
                ocr_path.write_text(full_text, encoding="utf-8")
                doc.close()
                return full_text, str(ocr_path)
            except Exception:
                # MinerU 不可用时继续下方 RapidOCR/视觉 OCR 降级路径。
                pass

    text_parts = []
    ocr_parts = []
    rendered_count = 0
    dense_table_count = 0

    for page_idx in range(len(doc)):
        page = doc.load_page(page_idx)
        text = page.get_text("text")
        has_text = bool(text.strip())
        if has_text and _is_dense_table(text):
            dense_table_count += 1

        structured_text = _extract_structured_tables(page) if has_text and _is_dense_table(text) else ""

        if has_text and _text_layer_is_usable(text, text):
            # 文字层优先。不要用简单的 x/y 坐标拼接结果替换原始文字层：
            # 多行表格单元格会被拆成相邻行，导致“标准要求”和“检验结果”错位。
            # 结构化坐标文本只作为调试信息保留，不进入审核主文本。
            page_text = text
            layer_label = "text layer"
            text_parts.append(f"\n--- Page {page_idx + 1} ({layer_label}) ---\n{page_text}")
            continue

        # 无文字层或文字层质量不足时才渲染图片。已有文字仍保留，供 OCR 结果交叉核对。
        if has_text:
            text_parts.append(f"\n--- Page {page_idx + 1} (text layer fallback) ---\n{text}")
        rendered_count += 1
        pix = page.get_pixmap(dpi=int(ocr_settings.get("render_dpi", 200)))
        img_path = Path(ocr_dir) / f"page_{page_idx + 1:03d}.png"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(img_path))

        # 密集表格页优先用视觉模型
        if (_is_dense_table(text) and ocr_settings.get("dense_table_vision", True)) or not ocr_settings.get("enabled", True):
            ocr_parts.append(f"\n--- Page {page_idx + 1} (vision OCR) ---\n")
            # 这里不直接调用LLM，避免extract层重；由上层 review.py 选择调用
            ocr_parts.append(f"[IMAGE:{img_path}]")
        else:
            # 普通扫描页用本地 rapidocr
            rapidocr = _get_rapidocr()
            if rapidocr and not settings.ocr_disabled and ocr_settings.get("enabled", True):
                result, _ = rapidocr(str(img_path))
                page_text = "\n".join(line[1] for line in result) if result else ""
                if page_text.strip():
                    ocr_parts.append(f"\n--- Page {page_idx + 1} (rapidocr) ---\n{page_text}")
                else:
                    # 空识别不能成为只有页标题的“成功页”，否则下游可能
                    # 对缺页报告给结论。保留缺页标记，交由完整性检查停止。
                    ocr_parts.append(f"\n--- Page {page_idx + 1} (rapidocr empty) ---\n[IMAGE:{img_path}]")
            else:
                ocr_parts.append(f"\n--- Page {page_idx + 1} (rapidocr missing) ---\n[IMAGE:{img_path}]")

    doc.close()

    # 混合模式只在文字层无法可靠覆盖全部页面时调用 MinerU，避免正常报告
    # 额外产生云端解析次数。MinerU 结果作为补充证据保留在提取文本末尾。
    if engine in {"mineru_hybrid", "hybrid"} and (rendered_count or dense_table_count):
        try:
            from backend.app.mineru import MinerUClient
            mineru_cfg = {
                "base_url": ocr_settings.get("mineru_base_url"),
                "task_endpoint": ocr_settings.get("mineru_task_endpoint"),
                "model_version": ocr_settings.get("mineru_model_version"),
                "api_key": ocr_settings.get("mineru_api_key"),
                "timeout_seconds": ocr_settings.get("mineru_timeout_seconds"),
                "poll_interval_seconds": ocr_settings.get("mineru_poll_interval_seconds"),
            }
            mineru_path = MinerUClient(mineru_cfg).parse_pdf(pdf_path, ocr_dir)
            mineru_text = Path(mineru_path).read_text(encoding="utf-8")
            if not _mineru_has_complete_pages(mineru_text, page_count):
                raise ValueError("MinerU页码覆盖不完整或存在空页，不能替代缺失OCR")
            # Full structured coverage supersedes earlier OCR placeholders. Keep
            # PDF text layers for cross-checking, but do not keep obsolete IMAGE
            # markers that would incorrectly stop a fully parsed document.
            ocr_parts = []
            ocr_parts.append(
                "\n--- MinerU document parse (authoritative for dense tables) ---\n"
                "[表格审核时以本段的行列对应关系为准；前面的PDF文字层仅用于基础信息交叉核对]\n"
                + mineru_text
            )
        except Exception as exc:
            ocr_parts.append(f"\n--- MinerU fallback unavailable ---\n[MINERU_ERROR:{exc}]\n")

    full_text = EXTRACTION_CACHE_MARKER + "\n" + "\n".join(text_parts) + "\n" + "\n".join(ocr_parts)

    # 保存ocr文本文件
    ocr_path.write_text(full_text, encoding="utf-8")

    return full_text, str(ocr_path)


def _first_field(text: str, labels: list[str], pattern: str = r"[^\n]+") -> str:
    for label in labels:
        match = re.search(rf"{label}\s*[:：]?\s*({pattern})", text, re.IGNORECASE)
        if match:
            value = re.sub(r"\s+", "", match.group(1)).strip("：:;,，")
            if value and value not in ("-", "/"):
                return value
    return ""


_COMPANY_LABELS = ["委托人", "委托单位", "申请人", "申请企业", "生产者", "制造商", "生产企业", "企业名称"]
_COMPANY_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "集团公司", "公司",
    "集团", "总厂", "分厂", "厂", "研究院", "研究所", "中心", "合作社", "商行",
    "个体工商户",
)
_COMPANY_STOP_LABELS = (
    "委托人地址", "委托单位地址", "申请人地址", "申请企业地址", "生产者地址",
    "生产企业地址", "制造商地址", "企业地址", "申请编号", "申请号", "报告编号",
    "样品名称", "产品名称", "产品单元", "型号", "规格", "商标", "数量", "生产日期",
    "生产序号", "样品生产序号", "试验依据", "检验依据", "收样日期", "完成日期",
)


def _clean_company_piece(value: str) -> str:
    """清理企业名称分行。PDF 常把“有限公司”拆到下一行。"""
    # MinerU 有时会把整个 HTML 表格行压成一行，例如：
    # “委托人:某有限公司委托人地址:...生产者:...”。企业名称
    # 必须在下一个字段标签前截断，否则会超过数据库 255 字符限制。
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\s+", "", value).strip("：:;,，。|/")
    stop_markers = tuple(_COMPANY_STOP_LABELS) + tuple(_COMPANY_LABELS)
    cut_positions = [position for marker in stop_markers if (position := value.find(marker)) > 0]
    if cut_positions:
        value = value[:min(cut_positions)].strip("：:;,，。|/")
    return value


def _valid_company_candidate(value: str) -> bool:
    """Reject OCR strings that contain interleaved fields or report boilerplate."""
    suffix_ok = value.endswith(_COMPANY_SUFFIXES) or bool(re.search(r"[（(]个体工商户[）)]$", value))
    if not (3 <= len(value) <= 100 and suffix_ok):
        return False
    forbidden = tuple(_COMPANY_STOP_LABELS) + (
        "不得", "检验结果", "TESTING", "CNAS", "抽样人员", "样品来源",
    )
    if any(marker.lower() in value.lower() for marker in forbidden):
        return False
    if re.search(r"[:：]|[A-Z]\d{3,}|\d{4,}.*(?:米|m)\b", value, re.I):
        return False
    return True


def _company_comparison_key(value: str) -> str:
    """Normalize presentation-only punctuation, not OCR characters or names."""
    return re.sub(r"\s+", "", value).translate(str.maketrans({"（": "(", "）": ")"}))


def _collapse_vertical_company_labels(text: str) -> str:
    """Restore field labels split vertically by PDF reading order.

    Some text-layer reports emit ``委 / 托 / 人：名称`` on separate lines.
    Only exact label partitions followed by a field colon are collapsed, so
    ordinary sentences containing words such as ``委托人不得`` stay intact.
    """
    for label in sorted(_COMPANY_LABELS, key=len, reverse=True):
        # Enumerate every non-empty partition of the label into 2+ lines.
        for mask in range(1, 1 << (len(label) - 1)):
            chunks = []
            start = 0
            for offset in range(len(label) - 1):
                if mask & (1 << offset):
                    chunks.append(label[start:offset + 1])
                    start = offset + 1
            chunks.append(label[start:])
            pattern = r"(?m)^[ \t]*" + r"[ \t]*\r?\n[ \t]*".join(
                re.escape(chunk) for chunk in chunks
            ) + r"[ \t]*([:：])"
            text = re.sub(pattern, lambda match, field=label: field + match.group(1), text)
    return text


def _company_candidate_sets(text: str) -> dict[int, set[str]]:
    """Collect source-labelled candidates by business-role priority."""
    lines = _collapse_vertical_company_labels(text).splitlines()
    candidates: dict[int, set[str]] = {0: set(), 1: set(), 2: set()}
    label_pattern = re.compile(
        rf"(?P<label>{'|'.join(map(re.escape, _COMPANY_LABELS))})(?!地址)\s*[:：]\s*(?P<value>.*)$"
    )
    stop_pattern = re.compile(rf"^(?:{'|'.join(map(re.escape, _COMPANY_STOP_LABELS))})\s*[:：]?")

    for index, original_line in enumerate(lines):
        line = original_line.strip()
        match = label_pattern.search(line)
        if not match:
            # 兼容“委托人”单独占一行、名称位于下一行；普通语句中的
            # “委托人不得……”不具备字段分隔符，绝不能作为企业名称。
            plain_line = re.sub(r"<[^>]+>", "", line).strip(" ：:")
            if plain_line not in _COMPANY_LABELS:
                continue
            label = plain_line
            inline_value = ""
        else:
            label = match.group("label")
            inline_value = match.group("value")

        pieces: list[str] = []
        first_piece = _clean_company_piece(inline_value)
        if first_piece:
            pieces.append(first_piece)

        # 最多向下拼三个非空分行；遇到新字段立即停止。
        for next_line in lines[index + 1:index + 7]:
            next_value = next_line.strip()
            if not next_value:
                continue
            if next_value.startswith("--- Page") or stop_pattern.match(next_value) or label_pattern.search(next_value):
                break
            cleaned = _clean_company_piece(next_value)
            if not cleaned:
                continue
            pieces.append(cleaned)
            if len(pieces) >= 4:
                break

        combined = ""
        for piece in pieces:
            combined += piece
            if (
                _valid_company_candidate(combined)
                and not re.search(r"(?:地址|申请编号|报告编号)", combined)
            ):
                priority = 0 if label in {"委托人", "委托单位", "申请人", "申请企业", "企业名称"} else (
                    1 if label in {"生产者", "制造商"} else 2
                )
                candidates[priority].add(combined)

    return candidates


def _extract_company(text: str) -> str:
    """先按委托/申请主体角色，再挑选同角色中最完整的名称。"""
    candidates = _company_candidate_sets(text)

    available = next((values for priority, values in sorted(candidates.items()) if values), None)
    if not available:
        fallback = _clean_company_piece(_first_field(text, _COMPANY_LABELS))
        # 无法可靠分离字段时宁可留空待复核，不把整行 HTML
        #、免责声明或检测机构标识误当成公司名存入数据库。
        if _valid_company_candidate(fallback):
            return fallback
        return ""
    # 同一主体角色的后续页往往有未换行全称。仅当短候选都是最长
    # 候选的前缀时，才可确定性地采用完整值；两个互不兼容的主体
    # 一律留空，避免以长度或出现次数猜公司。
    longest = max(
        available,
        key=lambda value: (len(_company_comparison_key(value)), value.count("（") + value.count("）"), value),
    )
    longest_key = _company_comparison_key(longest)
    if any(not longest_key.startswith(_company_comparison_key(value)) for value in available):
        return ""
    return longest


def extract_report_info(text: str) -> dict[str, str]:
    """从 OCR/文字层稳定提取用于列表展示的报告基本信息。"""
    report_no = _first_field(
        text,
        ["报告编号", "No\\.?"] ,
        r"[A-Z0-9][A-Z0-9()\-/]*",
    )
    application_no = _first_field(
        text,
        ["申请编号", "申请号"],
        r"[A-Z0-9][A-Z0-9\-/]*",
    )
    product_unit = _first_field(text, ["产品名称", "产品单元"])
    company = _extract_company(text)
    return {
        "report_no": report_no,
        "application_no": application_no,
        "company": company,
        "product_unit": product_unit,
    }


def _text_layer_usable(text: str) -> bool:
    """页文字层质量门：长度足够且含正常中文，排除乱码CMap与空层。"""
    if len(text or "") < 100:
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk >= 30


def collect_text_layer(pdf_path: str, ocr_path: str) -> dict:
    """抽取PDF内嵌文字层（按页），质量门通过的页存入 sidecar 缓存。

    缓存绑定源文件与代码版本；不进审核结论，仅作证人供规则采信。
    """
    import hashlib
    import json
    from pathlib import Path
    source = Path(pdf_path)
    try:
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        return {"status": "unavailable", "pages": {}}
    code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    cache = Path(ocr_path) / "text_layer_pages.json"
    if cache.is_file():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if payload.get("source_sha256") == source_hash and payload.get("code_sha256") == code_hash:
                return payload
        except (OSError, ValueError, KeyError, TypeError):
            pass
    pages: dict[str, str] = {}
    try:
        import fitz
        with fitz.open(pdf_path) as document:
            for index in range(document.page_count):
                try:
                    text = (document[index].get_text() or "").strip()
                except Exception:
                    continue
                if _text_layer_usable(text):
                    pages[str(index + 1)] = text
    except Exception:
        return {"status": "unavailable", "pages": {}}
    payload = {"status": "collected", "source_sha256": source_hash,
               "code_sha256": code_hash, "pages": pages}
    try:
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return payload
