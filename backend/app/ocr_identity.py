"""Evidence-bound OCR identity recovery; never relax the audit identity gate."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

VERSION = 'ocr-identity-recovery-v1'
MARKER = r'^--- MinerU document parse[^\n]*---\s*$'
PAGE = r'^--- Page (\d+)[^\n]*---[^\S\r\n]*\r?\n'
NUMBER = r'报告编号\s*[:：]\s*([A-Za-z0-9]+(?:[./_-][ \t\r\n]*[A-Za-z0-9]+)*)'

def compact(value):
    value = value.translate(str.maketrans({c: '-' for c in '‐‑‒–—−－'}))
    return re.sub(r'\s+', '', value)

def report_numbers(value):
    return {compact(m) for m in re.findall(NUMBER, value)}

def pages(value):
    result = {}
    for part in re.split(r'(?=^--- Page \d+)', value, flags=re.M):
        m = re.match(PAGE, part)
        if m:
            number = int(m[1])
            if number in result:
                raise ValueError('Duplicate OCR page')
            result[number] = part[m.end():]
    return result

def normalize_wrapped_inspections(value):
    """Join an observed suffix only with a unique complete same-page anchor.

    Not a general whitespace cleanup. HTML boundaries and page boundaries
    cannot be crossed. Missing/incorrect suffixes remain unresolved.
    """
    marker = re.search(MARKER, value, re.M)
    end = marker.start() if marker else len(value)
    parts = re.split(r'(?=^--- Page \d+)', value[:end], flags=re.M)
    changes = []
    for index, part in enumerate(parts):
        page = re.match(PAGE, part)
        if not page or '(text layer)' not in page[0]:
            continue
        anchors = report_numbers(part)
        if len(anchors) != 1:
            continue
        pattern = (r'(?P<label>检验编号[ \t:：]*)'
                   r'(?P<head>[A-Za-z0-9][A-Za-z0-9./_-]{2,}[./_-])'
                   r'[ \t]*\r?\n[ \t]*(?P<tail>[A-Za-z0-9]+(?:[./_-][A-Za-z0-9]+)*)'
                   r'[ \t]*(?=\r?$)')
        def replace(m):
            complete = m['head'] + m['tail']
            if complete not in anchors:
                return m[0]
            changes.append(dict(page=int(page[1]), kind='wrapped_inspection',
                                before=m[0], after=m['label']+complete))
            return m['label'] + complete
        parts[index] = re.sub(pattern, replace, part, flags=re.M)
    return ''.join(parts)+value[end:], changes

def digest(value):
    return hashlib.sha256(value).hexdigest()

def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
        name = stream.name
    os.replace(name, path)

def _legend_plans(text, pdf_path, archive_path):
    """Require original PDF geometry and a unique matching content-list block."""
    import fitz
    marker = re.search(MARKER, text, re.M)
    if not marker:
        return []
    native, supplements = pages(text[:marker.start()]), pages(text[marker.end():])
    if set(native) != set(supplements):
        return []
    with zipfile.ZipFile(archive_path) as archive:
        names = [n for n in archive.namelist() if re.search(r'(?:^|/)[^/]+_content_list\.json$', n)]
        if len(names) != 1:
            return []
        blocks = json.loads(archive.read(names[0]))
    if not isinstance(blocks, list):
        return []
    plans = []
    with fitz.open(pdf_path) as doc:
        if set(native) != set(range(1,len(doc)+1)):
            return []
        for number, body in native.items():
            own, other = report_numbers(body), report_numbers(supplements[number])
            if not own or not other or own == other:
                continue
            # Only a non-test composition page. Never dismiss conflicting
            # identities on a measured-results page, even if headers agree.
            if len(own)!=1 or not own < other or '报告组成' not in body:
                return []
            if re.search(r'检验编号|检验结果|检测项目|单项评定',compact(body)):
                return []
            raw_native = doc[number-1].get_text('text')
            if report_numbers(raw_native)!=own:
                return []
            extra = other-own
            if len(extra)!=1 or next(iter(extra)) not in compact(raw_native):
                return []
            candidate_blocks = []
            for block in blocks:
                if not isinstance(block,dict) or block.get('page_idx')!=number-1 or block.get('type')!='text':
                    continue
                original = block.get('text','')
                if not isinstance(original,str) or not original.startswith('判定'):
                    continue
                matches = list(re.finditer(NUMBER, original))
                if len(matches)!=1:
                    continue
                match = matches[0]
                prefix = original[:match.start()].rstrip()
                if original[match.end():].strip() or {compact(match[1])}!=extra:
                    continue
                if not all(v in compact(prefix) for v in ('P试验结果符合要求','F试验结果不符合要求','N表示该项目不要求判定')):
                    continue
                if compact(raw_native).count(compact(prefix))!=1 or supplements[number].count(original)!=1:
                    continue
                bbox = block.get('bbox')
                if not isinstance(bbox,list) or len(bbox)!=4 or not all(type(v) in (int,float) and 0<=v<=1000 for v in bbox):
                    continue
                x0,y0,x1,y1=bbox
                if x0>=x1 or y0>=y1:
                    continue
                rect=doc[number-1].rect
                clip=fitz.Rect(x0*rect.width/1000-3,y0*rect.height/1000-3,
                               x1*rect.width/1000+3,y1*rect.height/1000+3)
                region=doc[number-1].get_text('text',clip=clip)
                if compact(prefix) not in compact(region) or '报告编号' in region:
                    continue
                candidate_blocks.append(dict(page=number,kind='composition_legend_ocr_contamination',
                    before=original,after=prefix,bbox=bbox,expected_report=sorted(own)))
            if len(candidate_blocks)!=1:
                return []
            plans.extend(candidate_blocks)
    return plans

def apply_corroborated_legend(text, alternate, plans):
    """Keep all VLM tables/measurements; replace only independently proven text."""
    marker=re.search(MARKER,text,re.M)
    if not marker or not plans:
        raise ValueError('Missing corroboration plan')
    native=pages(text[:marker.start()])
    alt=pages(alternate)
    if set(native)!=set(alt):
        raise ValueError('Alternate OCR page coverage differs')
    for n,part in native.items():
        own=report_numbers(part)
        if own and report_numbers(alt[n])!=own:
            raise ValueError('Alternate OCR report identities differ')
    parts=re.split(r'(?=^--- Page \d+)',text[marker.end():],flags=re.M)
    for plan in plans:
        n=plan['page']
        if compact(plan['after']) not in compact(alt[n]):
            raise ValueError('Independent OCR does not confirm original legend')
        matches=[i for i,p in enumerate(parts) if (m:=re.match(PAGE,p)) and int(m[1])==n]
        if len(matches)!=1 or parts[matches[0]].count(plan['before'])!=1:
            raise ValueError('Ambiguous source block')
        parts[matches[0]]=parts[matches[0]].replace(plan['before'],plan['after'],1)
    candidate=text[:marker.end()]+''.join(parts)
    if re.findall(r'<table\b.*?</table>',text,re.S|re.I)!=re.findall(r'<table\b.*?</table>',candidate,re.S|re.I):
        raise ValueError('Tables changed during identity recovery')
    return candidate

def prepare_review_source(text, pdf_path, ocr_dir, settings, splitter, progress=None):
    """Bounded fallback, content-addressed sidecars, unchanged original caches."""
    original=text
    text, changes=normalize_wrapped_inspections(text)
    try:
        splitter(text)
    except RuntimeError as exc:
        if '同页文字层报告编号冲突' not in str(exc):
            raise
        if str(settings.get('engine')) not in {'mineru','mineru_hybrid','hybrid'}:
            raise
        plans=_legend_plans(text,pdf_path,Path(ocr_dir)/'mineru_result.zip')
        if not plans:
            raise
        params=dict(model_version='pipeline',is_ocr=True,enable_table=True,enable_formula=True,language='ch')
        identity=dict(version=VERSION,source_sha256=digest(Path(pdf_path).read_bytes()),
            original_text_sha256=digest(original.encode()),parameters=params,
            base_url=settings.get('mineru_base_url'),parser_sha256=digest(Path(__file__).read_bytes()))
        from backend.app import mineru, mineru_pages
        identity['adapter_sha256']=digest(Path(mineru.__file__).read_bytes())
        identity['page_parser_sha256']=digest(Path(mineru_pages.__file__).read_bytes())
        root=Path(ocr_dir)/'identity_recovery'/digest(json.dumps(identity,sort_keys=True).encode())
        root.mkdir(parents=True,exist_ok=True)
        receipt=root/'attempt.json'
        completed=root/'completed.json'
        if receipt.exists() and not completed.exists():
            raise RuntimeError('备用OCR已尝试但未完成核验，请查看独立解析记录，禁止自动重复上传。')
        if completed.exists():
            cached=json.loads(completed.read_text())
            alternate=(root/'alternate.md').read_text()
            if cached.get('identity')!=identity or cached.get('sha256')!=digest(alternate.encode()):
                raise RuntimeError('备用OCR缓存指纹不匹配，请核对缓存后重试。')
        else:
            if progress:
                progress('extracting',0,1,'正在用备用OCR核验报告组成页编号；保留原始试验表格')
            save_json(receipt,dict(identity=identity,status='started'))
            client=mineru.MinerUClient(dict(params,base_url=settings.get('mineru_base_url'),
                api_key=settings.get('mineru_api_key'),timeout_seconds=settings.get('mineru_timeout_seconds') or 300,
                poll_interval_seconds=settings.get('mineru_poll_interval_seconds') or 3))
            try:
                alternate=Path(client.parse_pdf(pdf_path,str(root/'pipeline'))).read_text()
                apply_corroborated_legend(text,alternate,plans)
                (root/'alternate.md').write_text(alternate)
                save_json(completed,dict(identity=identity,sha256=digest(alternate.encode())))
            finally:
                client.client.close()
        candidate=apply_corroborated_legend(text,alternate,plans)
        splitter(candidate)  # Original strict gate remains authoritative.
        text=candidate
        changes.extend(plans)
    if changes:
        identity=dict(version=VERSION,source_sha256=digest(Path(pdf_path).read_bytes()),
                      original_text_sha256=digest(original.encode()),resolved_text_sha256=digest(text.encode()))
        root=Path(ocr_dir)/'identity_recovery'/digest(json.dumps(identity,sort_keys=True).encode())
        root.mkdir(parents=True,exist_ok=True)
        (root/'resolved.txt').write_text(text)
        save_json(root/'evidence.json',dict(identity=identity,changes=changes))
    return text,dict(version=VERSION,changes=[dict(page=c['page'],kind=c['kind']) for c in changes],
                     original_cache_preserved=True)
