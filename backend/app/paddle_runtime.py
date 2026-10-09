"""Source-bound Paddle adapter with whole-PDF primary and single-page supplements."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import fitz
import httpx

from backend.app import paddle_tables

VERSION = 'paddle-document-v2'
MODEL = 'PaddleOCR-VL-1.6'
ENDPOINT = 'https://paddleocr.aistudio-app.com/api/v2/ocr/jobs'
OPTIONS = {'useDocOrientationClassify': False, 'useDocUnwarping': False, 'useChartRecognition': False}


class TransientSubmissionError(RuntimeError):
    """供应商侧临时性拒绝（队列已满/过载），保留记录后允许稍后重新提交。"""

MAX_TRANSIENT_RESUBMISSIONS = 8


def _submission_rejection(status, payload, key):
    """记录供应商拒绝原因，并判断是否为临时性拒绝（可安全重试）。"""
    code = None
    message = ''
    if isinstance(payload, dict):
        code = payload.get('code')
        message = str(payload.get('msg', payload.get('message', '')))
    if code is None and not message:
        message = 'non-json response'
    error = {'code': code, 'message': message.replace(key, '[REDACTED]')[:300]}
    transient = (status == 429 or status >= 500 or code == 10010
                 or '队列已满' in message or '请稍后重试' in message)
    return error, transient


def _reject_submission(state, statefile, status, payload, key,
                       permanent_message, transient_message):
    """临时拒绝保留记录并允许重试；超过次数上限或非临时拒绝则永久固化。"""
    provider_error, transient = _submission_rejection(status, payload, key)
    attempts = int(state.get('transient_resubmit_count') or 0) + 1
    if transient and attempts <= MAX_TRANSIENT_RESUBMISSIONS:
        state.update(state='submit_rejected_transient', http_status=status,
                     provider_error=provider_error, transient_resubmit_count=attempts)
        save(statefile, state)
        raise TransientSubmissionError(transient_message)
    state.update(state='rejected', http_status=status, provider_error=provider_error)
    save(statefile, state)
    raise RuntimeError(permanent_message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save(path, data):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, sort_keys=True)
    temporary.replace(path)


def result_url(url):
    parsed = urlsplit(url)
    # Provider-owned object storage only; never follow result redirects or send credentials here.
    host_ok = parsed.hostname == 'pplines-online.bj.bcebos.com' or bool(re.fullmatch(r'paddleocr-store(?:-[0-9]{1,2})?\.bj\.bcebos\.com', parsed.hostname or ''))
    if (parsed.scheme != 'https' or not host_ok
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise RuntimeError('PaddleOCR结果下载地址不在允许范围，已停止')
    return url


def _clean_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('empty page')
    if '[IMAGE:' in text or re.search(r'^--- Page \d+', text, re.M):
        raise ValueError('reserved source markers')
    text = re.sub(r'<t[dh]\b[^>]*>.*?</t[dh]>', lambda m: m[0].replace('\\r\\n', ' ').replace('\\n', ' '), text, flags=re.S | re.I)
    text = text.replace('\\r\\n', '\n').replace('\\n', '\n')
    if '[IMAGE:' in text or re.search(r'^--- Page \d+', text, re.M):
        raise ValueError('reserved source markers')
    def math_markup(match):
        value = match[1].replace(r'\times', '×')
        return value if re.fullmatch(r'[\d\s.×]+', value) else match[0]
    text = re.sub(r'\$([^$\n]+)\$', math_markup, text)

    # Unwrap remaining inline math (e.g. ``$ 2.5mm^{2} $``): superscript
    # braces become Unicode so downstream spec/identity parsing sees 2.5mm².
    sup_digits = {'0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
                  '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'}
    def unwrap_math(match):
        value = match[1].strip()
        value = re.sub(r'\^\{*(\d+)\}*',
                       lambda m: ''.join(sup_digits.get(ch, ch) for ch in m.group(1)),
                       value)
        return value.replace('$', '')
    text = re.sub(r'\$([^$\n]+)\$', unwrap_math, text)
    # Paddle sometimes emits bare LaTeX symbols without $...$ wrappers
    # (e.g. ``(70 \pm2)°C``); normalize so deterministic parsers and the
    # review model see plain ± and ×.
    text = re.sub(r'\\pm\s*', '±', text)
    text = re.sub(r'\\times\s*', '×', text)
    # Append a structure-preserving plain view of standard test tables so
    # condition rows carry explicit ownership; the original HTML stays
    # verbatim for bridges, identity comparisons and privacy preflight.
    return paddle_tables.annotate_html_tables(text)


def _layouts(raw):
    layouts = []
    try:
        for line in raw.decode('utf-8').splitlines():
            if line.strip():
                layouts.extend(json.loads(line)['result']['layoutParsingResults'])
        if not layouts:
            raise ValueError('page count')
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise RuntimeError('PaddleOCR结果缺失或格式异常，已停止') from None
    return layouts


def parse_result(raw):
    try:
        layouts = _layouts(raw)
        if len(layouts) != 1:
            raise ValueError('page count')
        return _clean_text(layouts[0]['markdown']['text'])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('PaddleOCR单页结果缺失、数量异常或含保留标记，已停止') from None


def parse_document_result(raw, expected_pages):
    """Validate provider chunk order and add the only trusted physical-page markers.

    Paddle splits a PDF into internal chunks.  ``input_img_N`` restarts at zero
    for each chunk, so a global 0..N sequence would reject valid documents.
    We accept only consecutive images inside a never-repeated chunk directory;
    the provider's validated layout array order is then the physical PDF order.
    """
    try:
        layouts = _layouts(raw)
        if len(layouts) != expected_pages:
            raise ValueError('page count')
        pages = []
        closed_chunks = set()
        current_chunk = None
        expected_local_index = 0
        for layout in layouts:
            image = urlsplit(str(layout.get('inputImage') or ''))
            if image.scheme != 'https' or image.hostname not in {
                    'pplines-online.bj.bcebos.com',
                    'paddleocr-store.bj.bcebos.com'}:
                raise ValueError('page source')
            marker = re.search(
                r'/([^/]+)/input_img_(\d+)\.(?:jpg|jpeg|png)$', image.path, re.I,
            )
            if not marker:
                raise ValueError('page order')
            chunk, local_index = marker.group(1), int(marker.group(2))
            if chunk != current_chunk:
                if current_chunk is not None:
                    closed_chunks.add(current_chunk)
                if chunk in closed_chunks or local_index != 0:
                    raise ValueError('chunk order')
                current_chunk = chunk
                expected_local_index = 0
            if local_index != expected_local_index:
                raise ValueError('page order')
            expected_local_index += 1
            pages.append(_clean_text(layout['markdown']['text']))
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('PaddleOCR整份PDF页数、顺序或页面内容异常，已停止') from None
    return '\n\n'.join(
        f'--- Page {number} (PaddleOCR whole-PDF original-page bound) ---\n{text}'
        for number, text in enumerate(pages, 1)
    )


def credential(settings):
    value = settings.get('paddleocr_api_key') or os.environ.get('PADDLEOCR_API_KEY')
    if not value:
        file = os.environ.get('PADDLEOCR_API_KEY_FILE')
        if file:
            value = Path(file).read_text().strip()
            if value.startswith('PADDLEOCR_API_KEY='):
                value = value.split('=', 1)[1]
    if not value or '\n' in value:
        raise RuntimeError('未配置PaddleOCR凭证')
    return value


def recognize(image, folder, identity, key, timeout=600):
    """Serialize both primary and supplementary requests for identical cache keys."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'request.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('同一OCR页面请求正在进行，禁止重复提交') from None
        return _recognize_locked(image, folder, identity, key, timeout)


def _recognize_locked(image, folder, identity, key, timeout):
    """Preserve uncertain submissions and resume known jobs while holding the lock."""
    statefile = folder / 'state.json'
    rawfile = folder / 'result.jsonl'
    state = json.loads(statefile.read_text()) if statefile.exists() else {'identity': identity}
    if state.get('identity') != identity:
        raise RuntimeError('PaddleOCR缓存来源或参数指纹不一致')
    if state.get('state') == 'complete':
        if not rawfile.is_file() or digest(rawfile.read_bytes()) != state.get('result_sha256'):
            raise RuntimeError('PaddleOCR结果缓存损坏，已停止')
        return parse_result(rawfile.read_bytes())
    if state.get('state') in ('submitting', 'rejected', 'failed') and not state.get('job_id'):
        raise RuntimeError('PaddleOCR提交未确认或已拒绝，禁止自动重复上传')
    headers = {'Authorization': 'bearer ' + key}
    # No inherited proxies; auth applies only to the fixed provider endpoint.
    with httpx.Client(timeout=90, follow_redirects=False, trust_env=False,
                      transport=httpx.HTTPTransport(local_address='0.0.0.0', retries=2)) as client:
        if not state.get('job_id'):
            state['state'] = 'submitting'
            save(statefile, state)
            try:
                response = client.post(ENDPOINT, headers=headers,
                    data={'model': MODEL, 'optionalPayload': json.dumps(OPTIONS)},
                    files={'file': ('page.png', image, 'image/png')})
                if response.status_code != 200:
                    try:
                        payload = response.json()
                    except (ValueError, AttributeError):
                        payload = None
                    _reject_submission(state, statefile, response.status_code, payload, key,
                        'PaddleOCR提交被拒绝，请核对服务状态',
                        'PaddleOCR提交暂时被拒绝（供应商队列已满或繁忙），已保留记录，允许稍后重试')
                job = response.json()['data']['jobId']
                if not isinstance(job, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', job):
                    raise ValueError('job')
                state.update(job_id=job, state='submitted')
                save(statefile, state)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                state.update(state='connection_failed_before_send', submission_error_type=type(exc).__name__)
                save(statefile, state)
                raise RuntimeError('PaddleOCR连接未建立，尚未发送页面；可重试连接') from None
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                state['submission_error_type'] = type(exc).__name__
                save(statefile, state)
                raise RuntimeError('PaddleOCR提交响应未确认，已保留记录，禁止重复上传') from None
        deadline = time.monotonic() + timeout
        transport_failures = 0
        while time.monotonic() < deadline:
            try:
                response = client.get(ENDPOINT + '/' + state['job_id'], headers=headers)
                if response.status_code != 200:
                    if response.status_code >= 500 or response.status_code == 429:
                        response.raise_for_status()
                    raise RuntimeError('PaddleOCR任务查询失败，重试将续查原任务')
                data = response.json()['data']
                status = data['state']
                if status == 'failed':
                    state['state'] = 'failed'
                    save(statefile, state)
                    raise RuntimeError('PaddleOCR任务失败，未使用其他引擎替代')
                if status == 'done':
                    url = result_url(data['resultUrl']['jsonUrl'])
                    with client.stream('GET', url) as response:
                        if response.status_code != 200:
                            if response.status_code >= 500 or response.status_code == 429:
                                response.raise_for_status()
                            raise RuntimeError('PaddleOCR结果下载失败')
                        blocks = []; size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 20_000_000:
                                raise RuntimeError('PaddleOCR单页结果超出安全大小限制')
                            blocks.append(chunk)
                    raw = b''.join(blocks)
                    text = parse_result(raw)
                    fd = os.open(rawfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(raw)
                    state.update(state='complete', result_sha256=digest(raw))
                    save(statefile, state)
                    return text
                if status not in ('pending', 'running'):
                    raise RuntimeError('PaddleOCR任务状态异常')
            except httpx.HTTPError as exc:
                transport_failures += 1
                state['last_transport_error'] = {'type': type(exc).__name__, 'consecutive_attempt': transport_failures}
                save(statefile, state)
                if transport_failures < 3 and time.monotonic() < deadline:
                    time.sleep(2)
                    continue
                raise RuntimeError('PaddleOCR查询或下载连续异常，已保留原任务供续查') from None
            except (KeyError, ValueError, TypeError) as exc:
                state['last_response_error'] = {'type': type(exc).__name__}
                save(statefile, state)
                raise RuntimeError('PaddleOCR返回格式异常，已保留原任务供核查') from None
            transport_failures = 0
            time.sleep(3)
    raise RuntimeError('PaddleOCR等待超时，已保留已完成页面及原任务供续查')


def recognize_document(source, folder, identity, key, expected_pages, timeout=600):
    """Submit one PDF once; resume only its known job and bind returned page order."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'request.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('同一报告PaddleOCR整份解析正在进行，禁止重复提交') from None
        statefile = folder / 'state.json'
        rawfile = folder / 'result.jsonl'
        state = json.loads(statefile.read_text()) if statefile.exists() else {'identity': identity}
        if state.get('identity') != identity:
            raise RuntimeError('PaddleOCR整份缓存来源或参数指纹不一致')
        if state.get('state') == 'complete':
            if not rawfile.is_file() or digest(rawfile.read_bytes()) != state.get('result_sha256'):
                raise RuntimeError('PaddleOCR整份结果缓存损坏，已停止')
            return parse_document_result(rawfile.read_bytes(), expected_pages)
        if state.get('state') in ('submitting', 'rejected', 'failed') and not state.get('job_id'):
            raise RuntimeError('PaddleOCR整份提交未确认或已拒绝，禁止自动重复上传')
        headers = {'Authorization': 'bearer ' + key}
        with httpx.Client(timeout=90, follow_redirects=False, trust_env=False,
                          transport=httpx.HTTPTransport(local_address='0.0.0.0', retries=2)) as client:
            if not state.get('job_id'):
                state['state'] = 'submitting'
                save(statefile, state)
                try:
                    with Path(source).open('rb') as stream:
                        response = client.post(ENDPOINT, headers=headers,
                            data={'model': MODEL, 'optionalPayload': json.dumps(OPTIONS)},
                            files={'file': (Path(source).name, stream, 'application/pdf')})
                    if response.status_code != 200:
                        try:
                            payload = response.json()
                        except (ValueError, AttributeError):
                            payload = None
                        _reject_submission(state, statefile, response.status_code, payload, key,
                            'PaddleOCR整份PDF提交被拒绝，请核对服务状态',
                            'PaddleOCR整份PDF提交暂时被拒绝（供应商队列已满或繁忙），已保留记录，允许稍后重试')
                    job = response.json()['data']['jobId']
                    if not isinstance(job, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', job):
                        raise ValueError('job')
                    state.update(job_id=job, state='submitted')
                    save(statefile, state)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    state.update(state='connection_failed_before_send', submission_error_type=type(exc).__name__)
                    save(statefile, state)
                    raise RuntimeError('PaddleOCR整份连接未建立，尚未发送PDF；可重试连接') from None
                except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                    state['submission_error_type'] = type(exc).__name__
                    save(statefile, state)
                    raise RuntimeError('PaddleOCR整份提交响应未确认，已保留记录，禁止重复上传') from None
            deadline = time.monotonic() + timeout
            transport_failures = 0
            while time.monotonic() < deadline:
                try:
                    response = client.get(ENDPOINT + '/' + state['job_id'], headers=headers)
                    if response.status_code != 200:
                        if response.status_code >= 500 or response.status_code == 429:
                            response.raise_for_status()
                        raise RuntimeError('PaddleOCR整份任务查询失败，重试将续查原任务')
                    data = response.json()['data']
                    status = data['state']
                    if status == 'failed':
                        state['state'] = 'failed'
                        save(statefile, state)
                        raise RuntimeError('PaddleOCR整份任务失败，未使用其他引擎替代')
                    if status == 'done':
                        url = result_url(data['resultUrl']['jsonUrl'])
                        with client.stream('GET', url) as download:
                            if download.status_code != 200:
                                if download.status_code >= 500 or download.status_code == 429:
                                    download.raise_for_status()
                                raise RuntimeError('PaddleOCR整份结果下载失败')
                            blocks = []; size = 0
                            for chunk in download.iter_bytes():
                                size += len(chunk)
                                if size > 100_000_000:
                                    raise RuntimeError('PaddleOCR整份结果超出安全大小限制')
                                blocks.append(chunk)
                        raw = b''.join(blocks)
                        text = parse_document_result(raw, expected_pages)
                        fd = os.open(rawfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(raw)
                        state.update(state='complete', result_sha256=digest(raw), pages=expected_pages)
                        save(statefile, state)
                        return text
                    if status not in ('pending', 'running'):
                        raise RuntimeError('PaddleOCR整份任务状态异常')
                except httpx.HTTPError as exc:
                    transport_failures += 1
                    state['last_transport_error'] = {'type': type(exc).__name__, 'consecutive_attempt': transport_failures}
                    save(statefile, state)
                    if transport_failures < 3 and time.monotonic() < deadline:
                        time.sleep(2)
                        continue
                    raise RuntimeError('PaddleOCR整份查询或下载连续异常，已保留原任务供续查') from None
                except (KeyError, ValueError, TypeError) as exc:
                    state['last_response_error'] = {'type': type(exc).__name__}
                    save(statefile, state)
                    raise RuntimeError('PaddleOCR整份返回格式异常，已保留原任务供核查') from None
                transport_failures = 0
                time.sleep(3)
        raise RuntimeError('PaddleOCR整份等待超时，已保留原任务供续查')


def extract(pdf_path, ocr_dir, settings):
    source = Path(pdf_path)
    source_hash = digest(source.read_bytes())
    identity = {'version': VERSION, 'source_sha256': source_hash, 'model': MODEL,
                'options': OPTIONS, 'mode': 'whole_pdf', 'renderer': fitz.VersionBind}
    root = Path(ocr_dir) / 'paddle_primary' / digest(json.dumps(identity, sort_keys=True).encode())
    root.mkdir(parents=True, exist_ok=True)
    key = credential(settings)
    with (root / 'document.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('同一报告PaddleOCR解析正在进行，请勿重复启动') from None
        with fitz.open(source) as doc:
            if not 1 <= len(doc) <= 300:
                raise RuntimeError('报告页数超出本次PaddleOCR允许范围')
            page_count = len(doc)
        text = recognize_document(source, root / 'document', identity, key, page_count,
                                  timeout=600)
        if digest(source.read_bytes()) != source_hash:
            raise RuntimeError('原PDF在解析期间发生变化，结果未采信')
        target = root / 'extracted.txt'
        target.write_text(text, encoding='utf-8')
        save(root / 'manifest.json', {'identity': identity, 'pages': page_count,
                                     'text_sha256': digest(text.encode()), 'complete': True,
                                     'adapter_sha256': digest(Path(__file__).read_bytes())})
        return text, str(target)
