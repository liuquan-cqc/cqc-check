"""Bounded optional context crops; no source-text rewrite and no unit overrides."""
import copy
import json
from pathlib import Path
import re
import fitz
from backend.app.paddle_runtime import credential, recognize, digest, MODEL, OPTIONS, VERSION
from backend.app.ocr_oven_bridge import table_groups
from backend.app.mineru_pages import coordinate_oven_aging_observations


def supplement_oven_pages(pdf_path, source_text, evidence, ocr_dir, settings):
    if str(settings.get('engine', '')).lower() not in {'paddleocr', 'paddleocr_vl', 'paddleocr-vl', 'paddleocr-vl-1.6'}:
        return evidence
    source = Path(pdf_path); source_hash = digest(source.read_bytes())
    if evidence.get('identity', {}).get('source_sha256') != source_hash:
        return evidence
    result = copy.deepcopy(evidence)
    matches = list(re.finditer(r'^--- Page (\d+)[^\n]*---\s*$', source_text, re.M))
    texts = {int(m[1]): source_text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(source_text)] for i, m in enumerate(matches)}
    attempts = 0
    with fitz.open(source) as doc:
        for page in result.get('pages', []):
            number = page['page']
            if len(table_groups(texts.get(number, ''))) == 1: continue
            oven = coordinate_oven_aging_observations(page)
            rows = oven.get('observations', []) + oven.get('before_observations', [])
            if len(rows) != 6 or not any(r.get('reason') == 'low_confidence_measurement' for r in rows): continue
            if any(r.get('reason') not in (None, 'low_confidence_measurement') or not r.get('label_box') for r in rows): continue
            if attempts >= 6:
                page['context_supplement_status'] = 'budget_exhausted_original_evidence_retained'; continue
            attempts += 1
            actual = doc[number - 1]
            bottom = max(p[1] for r in rows for p in r['label_box']) / page['height'] * actual.rect.height
            clip = fitz.Rect(0, 0, actual.rect.width, min(actual.rect.height, bottom + 15))
            image = actual.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False).tobytes('png')
            identity = {'version': VERSION, 'source_sha256': source_hash, 'page': number, 'clip': list(clip),
                        'scale': 2, 'renderer': fitz.VersionBind, 'model': MODEL, 'options': OPTIONS,
                        'image_sha256': digest(image), 'kind': 'oven_context'}
            folder = Path(ocr_dir) / 'paddle_context' / digest(json.dumps(identity, sort_keys=True).encode())
            try:
                text = recognize(image, folder, identity, credential(settings))
                if len(table_groups(text)) != 1:
                    page['context_supplement_status'] = 'ambiguous_table_original_evidence_retained'; continue
                page['paddle_oven_context'] = {'identity': identity, 'text': text, 'text_sha256': digest(text.encode())}
                page['context_supplement_status'] = 'source_bound_requires_row_agreement'
            except RuntimeError:
                page['context_supplement_status'] = 'unavailable_original_evidence_retained'
    if digest(source.read_bytes()) != source_hash:
        raise RuntimeError('原PDF在局部补强期间变化，结果未采信')
    return result
