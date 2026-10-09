"""LLM客户端封装：审核+视觉表格OCR，均通过OpenAI兼容协议。"""
from __future__ import annotations

import base64
import json
import time
from typing import List, Dict, Any
import httpx
from backend.app.config import get_settings


# Mock结果（LLM_MOCK=true时返回），用于无API Key环境冒烟测试
MOCK_REVIEW_RESULT = {
    "application_no": "A2026CCC0105-MOCK001",
    "report_no": "MOCK-2026-001",
    "company": "示例电缆有限公司",
    "product_desc": "PVC绝缘电缆检测报告",
    "product_unit": "聚氯乙烯绝缘无护套电线电缆",
    "conclusion": "需修改2处",
    "samples": [
        {
            "model": "BV 450/750V",
            "voltage": "450/750V",
            "spec": "1×2.5",
            "checks": [
                {
                    "category": "电压",
                    "item": "成品电缆电压试验",
                    "reported": "2000V/5min，未击穿，P",
                    "required": "450/750V成品电缆应施加2500V/5min，不击穿",
                    "verdict": "fail",
                    "basis": "GB/T 5023.1 电压试验要求",
                    "note": "报告试验电压低于标准要求"
                },
                {
                    "category": "机械性能",
                    "item": "绝缘老化前后性能",
                    "reported": "80℃/168h，抗张强度15.2MPa，伸长率290%，P",
                    "required": "80±2℃/168h，抗张强度和伸长率均应满足材料限值",
                    "verdict": "pass",
                    "basis": "GB/T 5023.1 PVC/C绝缘机械性能",
                    "note": "报告条件、实测值和P判定一致"
                }
            ],
            "items": [
                {
                    "item": "高温压力试验温度",
                    "reported": "80℃",
                    "should_be": "70℃",
                    "standard": "GB/T 2951.31 PVC/D 70℃",
                    "severity": "must_fix"
                },
                {
                    "item": "电压试验",
                    "reported": "2000V",
                    "should_be": "2500V",
                    "standard": "450/750V成品电缆2500V",
                    "severity": "must_fix"
                }
            ]
        }
    ],
    "remarks": ["本结果为Mock数据，仅用于验证流程；配置真实API Key后重审。"],
    "detail": "## 审核明细\n\n| 样品 | 项目 | 报告原值 | 应改值 | 标准依据 | 结论 |\n|---|---|---|---|---|---|\n| BV 450/750V | 高温压力试验温度 | 80℃ | 70℃ | GB/T 2951.31 PVC/D 70℃ | 必须改 |\n| BV 450/750V | 电压试验 | 2000V | 2500V | 450/750V成品电缆2500V | 必须改 |\n"
}


class LLMClient:
    """OpenAI 兼容协议的统一封装。"""

    def __init__(
        self,
        timeout_seconds: float | None = None,
        ai_config: dict[str, Any] | None = None,
    ):
        self.settings = get_settings()
        if ai_config is None:
            from backend.app.settings_store import get_ai_runtime_config
            ai_config = get_ai_runtime_config()
        self.ai = dict(ai_config)
        timeout = float(timeout_seconds or self.ai.get("timeout_seconds", 120))
        self.client = None if self.ai.get("mock_enabled") else httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout))
        )

    def _headers(self):
        if self.ai.get("provider") == "intranet":
            return {
                "apikey": str(self.ai.get("api_key", "")),
                "Content-Type": "application/json",
                "Accept": "*/*",
                "User-Agent": "CQC-Review/1.0",
            }
        return {
            "Authorization": f"Bearer {self.ai.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def _chat_url(self) -> str:
        base_url = str(self.ai.get("base_url", "")).rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def _apply_provider_options(self, payload: dict[str, Any]) -> dict[str, Any]:
        """DeepSeek 官方 OpenAI 格式控制思考开关与强度。"""
        if self.ai.get("provider") == "deepseek":
            thinking_enabled = bool(self.ai.get("thinking_enabled"))
            payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
            if thinking_enabled:
                payload["reasoning_effort"] = self.ai.get("reasoning_effort", "max")
        elif self.ai.get("provider") == "intranet":
            # 单位内网 Ollama/OpenAI 兼容网关使用 enable_thinking。
            # 安全预审需要稳定输出短JSON，默认关闭思考，避免推理耗尽正文额度。
            payload["enable_thinking"] = bool(self.ai.get("thinking_enabled", False))
        return payload

    @staticmethod
    def _response_content(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ValueError("模型接口未返回 choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            finish_reason = choices[0].get("finish_reason", "unknown")
            completion_tokens = (data.get("usage") or {}).get("completion_tokens", "unknown")
            raise ValueError(
                f"模型返回空正文（finish_reason={finish_reason}, "
                f"completion_tokens={completion_tokens}）"
            )
        return content

    def chat(
        self,
        system: str,
        user: str,
        model: str = None,
        max_retries: int = 2,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """调用 chat/completions，返回文本内容。失败可重试。"""
        if self.ai.get("mock_enabled"):
            time.sleep(0.3)
            return json.dumps(MOCK_REVIEW_RESULT, ensure_ascii=False, indent=2)

        url = self._chat_url()
        payload = {
            "model": model or self.ai.get("review_model"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(self.ai.get("temperature", 0.2)),
            "max_tokens": int(max_tokens or self.ai.get("max_tokens", 8192)),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        self._apply_provider_options(payload)
        last_err = None
        json_mode_fallback_used = False
        for attempt in range(max_retries + 1):
            try:
                resp = self.client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                return self._response_content(resp.json())
            except Exception as e:
                last_err = e
                # 部分OpenAI兼容服务不支持response_format。仅在接口明确
                # 拒绝请求时去掉该参数原样重发，不屏蔽其他请求错误。
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if (
                    json_mode and not json_mode_fallback_used
                    and "response_format" in payload and status_code in (400, 404, 415, 422)
                ):
                    payload.pop("response_format", None)
                    json_mode_fallback_used = True
                    try:
                        resp = self.client.post(url, headers=self._headers(), json=payload)
                        resp.raise_for_status()
                        return self._response_content(resp.json())
                    except Exception as fallback_error:
                        last_err = fallback_error
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
        raise last_err

    def vision_ocr(self, image_path: str, prompt: str = None, max_retries: int = 2) -> str:
        """调用视觉模型识别图片中的表格。"""
        if self.ai.get("mock_enabled"):
            return "| 项目 | 标准要求 | 检验结果 | 单项评定 |\n|---|---|---|---|\n| 高温压力 | 70℃ | 80℃ | P |\n"

        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        url = self._chat_url()
        payload = {
            "model": self.ai.get("vision_model"),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or "识别图片中的表格内容，用Markdown表格输出。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        self._apply_provider_options(payload)
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                resp = self.client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                return self._response_content(resp.json())
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
        raise last_err

    def parse_json(self, text: str) -> Dict[str, Any]:
        """从模型输出中提取JSON。允许被Markdown代码围栏包裹。"""
        text = text.strip()
        # 去除可能的 ```json ... ``` 围栏
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("\n", 1)[0]
        # 截取第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
        return json.loads(text)
