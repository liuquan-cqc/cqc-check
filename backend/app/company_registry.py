"""天眼查企业信息查询与存续状态标准化。"""
from __future__ import annotations

import re
from typing import Any

import httpx

from backend.app.settings_store import get_section


class CompanyRegistryError(RuntimeError):
    pass


NAME_FIELDS = ("name", "companyName", "company_name", "enterpriseName", "entName")
STATUS_FIELDS = ("regStatus", "reg_status", "companyStatus", "company_status", "regState", "status")
CREDIT_FIELDS = (
    "creditCode", "credit_code", "creditNo", "socialCreditCode", "unifiedSocialCreditCode", "creditNum"
)
ADDRESS_FIELDS = ("regLocation", "registeredAddress", "address", "regAddress")


def _first_value(data: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = data.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_name(value: str) -> str:
    return re.sub(r"[\s　]+", "", value).replace("(", "（").replace(")", "）")


def map_operating_status(raw_status: str) -> str:
    value = re.sub(r"\s+", "", raw_status or "")
    if not value:
        return "unknown"
    if "吊销" in value:
        return "revoked"
    if any(word in value for word in ("注销", "已注销", "清算", "清算中")):
        return "cancelled"
    if "迁出" in value:
        return "moved"
    if any(word in value for word in ("存续", "在业", "开业", "正常", "营业", "迁入")):
        return "active"
    return "other"


def _record_candidates(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            if _first_value(value, NAME_FIELDS):
                found.append(value)
            for key, child in value.items():
                if key in {"data", "result", "items", "list", "records", "companies", "searchResult"}:
                    visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:100]:
                visit(child, depth + 1)

    visit(payload)
    return found


def parse_company_response(payload: Any, keyword: str, exact_match: bool = True) -> dict[str, str]:
    candidates = _record_candidates(payload)
    normalized_keyword = _normalize_name(keyword)
    exact = [
        item for item in candidates
        if _normalize_name(_first_value(item, NAME_FIELDS)) == normalized_keyword
    ]
    if not exact and not exact_match and candidates:
        exact = [candidates[0]]
    if not exact:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("reason") or payload.get("msg") or "")
        suffix = f"：{message}" if message else ""
        raise CompanyRegistryError(f"未找到与“{keyword}”全称精确匹配的企业{suffix}")

    item = exact[0]
    raw_status = _first_value(item, STATUS_FIELDS)
    if not raw_status:
        raise CompanyRegistryError("接口已找到企业，但返回数据中没有工商状态字段")
    return {
        "name": _first_value(item, NAME_FIELDS),
        "raw_status": raw_status,
        "operating_status": map_operating_status(raw_status),
        "unified_social_credit_code": _first_value(item, CREDIT_FIELDS),
        "address": _first_value(item, ADDRESS_FIELDS),
    }


class TianyanchaClient:
    def __init__(self, db=None):
        self.config = get_section("company_registry", db)

    def search_company(self, keyword: str, exact_match: bool = True) -> dict[str, str]:
        if not self.config.get("enabled"):
            raise CompanyRegistryError("请先在系统设置中启用天眼查企业核验")
        if self.config.get("mode", "browser") != "api":
            raise CompanyRegistryError("当前使用 Edge 网页辅助核验，不会调用付费 API")
        token = str(self.config.get("token") or "").strip()
        if not token:
            raise CompanyRegistryError("请先填写并保存天眼查 Token")

        auth_value = f"Bearer {token}" if self.config.get("auth_mode") == "bearer" else token
        try:
            response = httpx.post(
                str(self.config["base_url"]),
                headers={"Authorization": auth_value, "Content-Type": "application/json"},
                json={
                    "keyword": keyword,
                    "include": list(self.config.get("include_fields") or []),
                },
                timeout=httpx.Timeout(float(self.config.get("timeout_seconds", 20)), connect=10.0),
            )
        except httpx.TimeoutException as exc:
            raise CompanyRegistryError("天眼查请求超时，请检查单位网络或接口 IP 白名单") from exc
        except httpx.RequestError as exc:
            raise CompanyRegistryError(f"无法连接天眼查：{exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise CompanyRegistryError(f"天眼查返回了非 JSON 数据（HTTP {response.status_code}）") from exc
        if response.status_code >= 400:
            message = payload.get("message") or payload.get("reason") or payload.get("msg") if isinstance(payload, dict) else ""
            raise CompanyRegistryError(f"天眼查接口返回 HTTP {response.status_code}{f'：{message}' if message else ''}")
        return parse_company_response(payload, keyword, exact_match=exact_match)
