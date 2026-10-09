"""MinerU云端文档解析适配器。"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx

from backend.app.mineru_pages import page_marked_markdown_from_zip


class MinerUError(RuntimeError):
    pass


class MinerUClient:
    def __init__(self, config: dict[str, Any]):
        self.base_url = str(config.get("base_url") or "https://mineru.net/api/v4").rstrip("/")
        self.task_endpoint = str(config.get("task_endpoint") or "/extract/task")
        if not self.task_endpoint.startswith("/"):
            self.task_endpoint = "/" + self.task_endpoint
        self.model_version = str(config.get("model_version") or "vlm")
        self.parse_options = {key: config.get(key, default) for key, default in
                              (("is_ocr", False), ("enable_table", True), ("enable_formula", True))}
        if any(type(value) is not bool for value in self.parse_options.values()):
            raise ValueError("MinerU解析开关必须是布尔值")
        self.language = str(config.get("language") or "ch")
        self.token = str(config.get("api_key") or config.get("token") or "").strip()
        self.timeout = float(config.get("timeout_seconds") or 300)
        self.poll_seconds = max(2.0, float(config.get("poll_interval_seconds") or 3))
        self.client = httpx.Client(timeout=httpx.Timeout(30, read=min(self.timeout, 60)))

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise MinerUError("未配置 MinerU API Token")
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code") not in (None, 0, 200):
            raise MinerUError(str(data.get("msg") or data.get("message") or data))
        return data if isinstance(data, dict) else {}

    def parse_pdf(self, pdf_path: str, output_dir: str) -> str:
        """解析PDF并返回带真实页码标记的本地Markdown路径。"""
        source = Path(pdf_path)
        # Non-default parsing parameters cannot reuse a legacy OCR cache.
        # Default callers retain their historical cache behavior unchanged.
        if self.parse_options != dict(is_ocr=False,enable_table=True,enable_formula=True) or self.language != 'ch':
            import hashlib
            import json
            from backend.app import mineru_pages
            identity = dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                model_version=self.model_version,base_url=self.base_url,language=self.language,**self.parse_options,
                adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                page_parser_sha256=hashlib.sha256(Path(mineru_pages.__file__).read_bytes()).hexdigest())
            fingerprint = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
            output_dir = str(Path(output_dir)/f'parse_{fingerprint}')
        target = Path(output_dir) / "mineru.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size > 20:
            return str(target)

        headers = self._headers()
        response = self.client.post(
            f"{self.base_url}/file-urls/batch",
            headers=headers,
            json={
                "files": [{"name": source.name, "data_id": source.stem, "is_ocr": self.parse_options['is_ocr']}],
                "model_version": self.model_version,
                "enable_table": self.parse_options['enable_table'],
                "enable_formula": self.parse_options['enable_formula'],
                "language": self.language,
            },
        )
        data = self._json(response)
        result = data.get("data") or data
        batch_id = result.get("batch_id") or result.get("batchId")
        upload_urls = result.get("file_urls") or result.get("fileUrls") or []
        if not batch_id or not upload_urls:
            raise MinerUError(f"MinerU 未返回批次ID或上传地址：{data}")
        upload_item = upload_urls[0]
        upload_url = upload_item if isinstance(upload_item, str) else (
            upload_item.get("url") or upload_item.get("file_url") or upload_item.get("upload_url")
        )
        if not upload_url:
            raise MinerUError(f"MinerU 未返回有效上传地址：{data}")
        with source.open("rb") as stream:
            upload_response = self.client.put(upload_url, content=stream)
            upload_response.raise_for_status()

        deadline = time.monotonic() + self.timeout
        download_url = ""
        completed = False
        while time.monotonic() < deadline:
            status_response = self.client.get(f"{self.base_url}/extract-results/batch/{batch_id}", headers=headers)
            status_data = self._json(status_response)
            status = status_data.get("data") or status_data
            items = (
                status.get("extract_result") or status.get("extract_result_list")
                or status.get("files") or status.get("results") or []
            )
            if isinstance(items, dict):
                items = [items]
            if items:
                # This request uploads exactly one file. Do not silently take
                # the first result from an ambiguous or mismatched response.
                if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                    raise MinerUError("MinerU单文件任务返回了异常或多条结果，已停止解析")
                current = items[0]
                for field, expected in (("data_id", source.stem), ("file_name", source.name)):
                    actual = current.get(field)
                    if actual not in (None, "") and str(actual) != expected:
                        raise MinerUError("MinerU结果文件标识与本次上传不一致，已停止解析")
                state = str(current.get("state") or current.get("status") or "").lower()
                download_url = (
                    current.get("full_zip_url") or current.get("zip_url")
                    or current.get("full_zip_file") or current.get("result_url") or current.get("url") or ""
                )
                download_url = (
                    download_url or status.get("full_zip_url") or status.get("zip_url")
                    or status.get("result_url") or ""
                )
                if state in {"done", "success", "succeeded", "completed"} and download_url:
                    completed = True
                    break
                if state in {"failed", "error", "cancelled"}:
                    raise MinerUError(str(current.get("err_msg") or current.get("message") or "MinerU 解析失败"))
            time.sleep(self.poll_seconds)
        if not completed:
            raise MinerUError(f"MinerU 解析超时（{self.timeout:.0f} 秒）")

        result_response = self.client.get(download_url)
        result_response.raise_for_status()
        content_type = result_response.headers.get("content-type", "")
        if "zip" in content_type or download_url.lower().split("?")[0].endswith(".zip"):
            import io
            import zipfile

            archive_bytes = result_response.content
            archive_path = target.parent / "mineru_result.zip"
            archive_path.write_bytes(archive_bytes)
            try:
                text = page_marked_markdown_from_zip(archive_path)
            except (ValueError, OSError, zipfile.BadZipFile, UnicodeDecodeError, re.error):
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                    candidates = [
                        name for name in archive.namelist()
                        if name.lower().endswith((".md", ".markdown", ".txt"))
                    ]
                    if not candidates:
                        raise MinerUError("MinerU 结果包中没有结构化内容或Markdown文本")
                    candidates.sort(key=lambda name: (0 if Path(name).name.lower() == "full.md" else 1, len(name), name))
                    text = archive.read(candidates[0]).decode("utf-8", errors="replace")
        else:
            text = result_response.text
        text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
        if not text:
            raise MinerUError("MinerU 返回了空解析结果")
        target.write_text(text, encoding="utf-8")
        return str(target)
