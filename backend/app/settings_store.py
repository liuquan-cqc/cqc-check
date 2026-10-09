"""系统设置存取、默认值、字段校验与敏感字段脱敏。"""
from __future__ import annotations

import copy
import base64
import hashlib
from typing import Any


DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "basic": {
        "system_name": "电线电缆检测报告审核系统",
        "system_version": "1.0.0",
        "copyright": "中国质量认证中心",
        "timezone": "Asia/Shanghai",
        "date_format": "YYYY-MM-DD",
        "time_format": "24h",
        "language": "zh-CN",
        "page_size": 20,
        "theme_mode": "system",
        "primary_color": "#2563eb",
        "table_density": "comfortable",
        "logo_data_url": "",
    },
    "notifications": {
        "browser_enabled": True,
        "email_enabled": False,
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_username": "",
        "smtp_password": "",
        "sender_email": "",
        "recipient_email": "",
        "notify_on_done": True,
        "notify_on_failed": True,
        "webhook_enabled": False,
        "webhook_url": "",
    },
    "ocr": {
        "enabled": True,
        "engine": "rapidocr",
        "render_dpi": 200,
        "dense_table_vision": True,
        # Only pages whose row/column ownership cannot be resolved by the
        # original-PDF coordinate gate are sent to the intranet vision model.
        # The model proposes structure only; source tokens remain authoritative.
        "table_visual_repair_enabled": False,
        "vision_concurrency": 3,
        "language": "zh-CN",
        "paddleocr_api_key": "",
        "mineru_base_url": "https://mineru.net/api/v4",
        "mineru_task_endpoint": "/extract/task",
        "mineru_model_version": "vlm",
        "mineru_api_key": "",
        "mineru_timeout_seconds": 300,
        "mineru_poll_interval_seconds": 3,
    },
    "ai": {
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "review_model": "deepseek-ai/DeepSeek-V3.2",
        "vision_model": "Qwen/Qwen3-VL-30B-A3B-Instruct",
        "deepseek_base_url": "https://api.deepseek.com",
        "deepseek_api_key": "",
        "deepseek_review_model": "deepseek-v4-flash-vision-exp",
        "deepseek_vision_model": "deepseek-v4-flash-vision-exp",
        "deepseek_temperature": 0.2,
        "deepseek_max_tokens": 16384,
        "deepseek_timeout_seconds": 240,
        "deepseek_thinking_enabled": False,
        "deepseek_reasoning_effort": "max",
        "intranet_base_url": "http://intranet-llm.example/ollama/v1",
        "intranet_api_key": "",
        "intranet_review_model": "qwen3.8:27b",
        "intranet_vision_model": "qwen3.8:27b",
        "intranet_temperature": 0.7,
        "intranet_max_tokens": 262144,
        "intranet_timeout_seconds": 600,
        "intranet_thinking_enabled": False,
        # 在外网主审核前，仅用内网模型检查报告原文内部的
        # 文字、编号、日期、计算和 P/N 逻辑矛盾。它不判断标准
        # 适用性、试验项目或限值，也不直接改变最终结论。
        "intranet_content_review_enabled": False,
        # 使用当前外网主审核供应商做脱敏后的第一阶段
        # 内容一致性复核；第二阶段再根据规则库审核问题。
        "external_content_review_enabled": True,
        # 单位内网模型可作为外网审核前的独立安全预审层，不要求把
        # 当前主审核供应商切换为 intranet。
        # 旧数据库没有该新字段时，必须默认先脱敏再外发。
        # 否则独立最终安全网会在原始文本上正常阻断所有请求。
        "privacy_preflight_mode": "enforce",
        "privacy_preflight_mock_enabled": False,
        # 保留旧字段用于兼容已保存的设置；新的语义复核由
        # intranet_content_review_enabled 独立控制。
        "privacy_preflight_logic_enabled": False,
        "privacy_company_blacklist_enabled": True,
        "privacy_application_blacklist_enabled": True,
        "privacy_outbound_block_enabled": True,
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 240,
        "mock_enabled": True,
    },
    "company_registry": {
        "enabled": True,
        "provider": "tianyancha",
        "mode": "browser",
        "base_url": "https://api.tianyancha.com/v2/company/search",
        "token": "",
        "auth_mode": "bearer",
        "timeout_seconds": 20,
        "include_fields": [],
        "web_daily_limit": 10,
        "web_interval_seconds": 20,
    },
    "files": {
        "max_upload_mb": 50,
        "allowed_extensions": ["pdf"],
        "retention_days": 0,
    },
    "workflow": {
        "auto_review": True,
        "allow_re_review": True,
        "worker_concurrency": 3,
        "poll_interval_seconds": 5,
        "max_retries": 2,
    },
    "security": {
        "public_access_enabled": True,
        "jwt_expire_hours": 12,
        "password_min_length": 6,
        "login_max_failures": 5,
        "lock_minutes": 15,
    },
    "roles": {
        "admin": [
            "dashboard.view", "reports.view", "reports.upload", "reports.review",
            "reports.download", "reports.delete", "companies.manage", "users.manage",
            "settings.manage", "iteration.manage", "sampling.manage",
        ],
        "reviewer": [
            "dashboard.view", "reports.view", "reports.upload", "reports.review",
            "reports.download", "sampling.manage",
        ],
        "viewer": ["dashboard.view", "reports.view", "reports.download"],
    },
}

SECRET_FIELDS = {
    "ai": {"api_key", "deepseek_api_key", "intranet_api_key"},
    "ocr": {"mineru_api_key", "paddleocr_api_key"},
    "notifications": {"smtp_password", "webhook_url"},
    "company_registry": {"token"},
}
MASK = "********"
ENCRYPTED_PREFIX = "enc:v1:"
ADMIN_ONLY_PERMISSIONS = {"users.manage", "settings.manage"}


def _merge(section: str, stored: Any) -> dict[str, Any]:
    default = copy.deepcopy(DEFAULT_SETTINGS[section])
    if isinstance(stored, dict):
        default.update(stored)
    if section == "roles":
        # 管理员始终获得新增管理功能；旧部署保存过的角色配置也能自动补齐。
        default["admin"] = copy.deepcopy(DEFAULT_SETTINGS["roles"]["admin"])
    return default


def _secret_cipher():
    """使用服务器 JWT 密钥派生数据加密密钥，不新增另一份明文密钥。"""
    from cryptography.fernet import Fernet
    from backend.app.config import get_settings
    digest = hashlib.sha256(get_settings().jwt_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt_secret(value: str) -> str:
    if not value or value.startswith(ENCRYPTED_PREFIX):
        return value
    encrypted = _secret_cipher().encrypt(value.encode("utf-8")).decode("ascii")
    return ENCRYPTED_PREFIX + encrypted


def _decrypt_secret(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if not value.startswith(ENCRYPTED_PREFIX):
        return value
    try:
        return _secret_cipher().decrypt(value[len(ENCRYPTED_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        # 密钥变更或密文损坏时安全失败，不把密文当作可用 Token 发出。
        return ""


def get_section(section: str, db=None) -> dict[str, Any]:
    """读取分组设置；数据库不可用时安全回退到默认值和环境变量。"""
    if section not in DEFAULT_SETTINGS:
        raise KeyError(section)
    close_db = False
    try:
        if db is None:
            from backend.app.database import SessionLocal
            db = SessionLocal()
            close_db = True
        from backend.app.models import SystemSetting
        row = db.query(SystemSetting).filter(SystemSetting.section == section).first()
        has_stored_row = row is not None
        values = _merge(section, row.value_json if row else None)
    except Exception:
        has_stored_row = False
        values = copy.deepcopy(DEFAULT_SETTINGS[section])
    finally:
        if close_db and db is not None:
            db.close()

    for field in SECRET_FIELDS.get(section, set()):
        values[field] = _decrypt_secret(values.get(field))

    # 部署环境变量仅作为尚未在界面保存时的初始值。
    if section == "ai":
        from backend.app.config import get_settings
        env = get_settings()
        if not values.get("api_key"):
            values["api_key"] = env.siliconflow_api_key
        if not has_stored_row:
            values["base_url"] = env.llm_base_url
            values["review_model"] = env.llm_model
            values["vision_model"] = env.vision_model
            values["mock_enabled"] = env.llm_mock
    return values


def public_section(section: str, db=None) -> dict[str, Any]:
    values = get_section(section, db)
    paddle_status = None
    if section == "ocr":
        import os
        from backend.app.paddle_runtime import credential, MODEL, ENDPOINT, OPTIONS
        source = ("database" if values.get("paddleocr_api_key") else
                  "environment" if os.getenv("PADDLEOCR_API_KEY") else
                  "server_file" if os.getenv("PADDLEOCR_API_KEY_FILE") else "none")
        try:
            configured = bool(credential(values))
        except Exception:
            configured = False
        paddle_status = {"configured": configured, "credential_source": source,
                         "model": MODEL, "endpoint": ENDPOINT, "options": dict(OPTIONS)}
    for field in SECRET_FIELDS.get(section, set()):
        values[field] = MASK if values.get(field) else ""
        values[f"{field}_configured"] = bool(values[field])
    if paddle_status is not None:
        values["paddleocr_status"] = paddle_status
    return values


def get_ai_runtime_config(db=None) -> dict[str, Any]:
    """返回当前启用供应商的运行配置，同时保留各供应商独立存储值。"""
    values = get_section("ai", db)
    if values.get("provider") == "deepseek":
        values["base_url"] = values.get("deepseek_base_url", "https://api.deepseek.com")
        values["api_key"] = values.get("deepseek_api_key", "")
        values["review_model"] = values.get("deepseek_review_model", "deepseek-v4-flash-vision-exp")
        values["vision_model"] = values.get("deepseek_vision_model", "deepseek-v4-flash-vision-exp")
        values["temperature"] = values.get("deepseek_temperature", 0.2)
        values["max_tokens"] = values.get("deepseek_max_tokens", 16384)
        values["timeout_seconds"] = values.get("deepseek_timeout_seconds", 240)
        values["thinking_enabled"] = bool(values.get("deepseek_thinking_enabled", False))
        values["reasoning_effort"] = values.get("deepseek_reasoning_effort", "max")
    elif values.get("provider") == "intranet":
        values["base_url"] = values.get("intranet_base_url", "http://intranet-llm.example/ollama/v1")
        values["api_key"] = values.get("intranet_api_key", "")
        values["review_model"] = values.get("intranet_review_model", "qwen3.8:27b")
        values["vision_model"] = values.get("intranet_vision_model", "qwen3.8:27b")
        values["temperature"] = values.get("intranet_temperature", 0.7)
        values["max_tokens"] = values.get("intranet_max_tokens", 262144)
        values["timeout_seconds"] = values.get("intranet_timeout_seconds", 600)
        values["thinking_enabled"] = bool(values.get("intranet_thinking_enabled", False))
    return values


def get_intranet_runtime_config(db=None) -> dict[str, Any]:
    """返回独立内网内容复核/表格预处理配置，不改变主审核供应商。"""
    values = get_section("ai", db)
    return {
        **values,
        "provider": "intranet",
        "base_url": values.get("intranet_base_url", "http://intranet-llm.example/ollama/v1"),
        "api_key": values.get("intranet_api_key", ""),
        "review_model": values.get("intranet_review_model", "qwen3.8:27b"),
        "vision_model": values.get("intranet_vision_model", "qwen3.8:27b"),
        # 预审是结构化提取任务，低温度比主审核参数更稳定。
        "temperature": 0.1,
        "max_tokens": min(262144, int(values.get("intranet_max_tokens", 262144))),
        "timeout_seconds": values.get("intranet_timeout_seconds", 600),
        "thinking_enabled": bool(values.get("intranet_thinking_enabled", False)),
        # 与主审核演示模式解耦，允许“内网真实预审 + 外网Mock审核”
        # 的安全试运行组合。
        "mock_enabled": bool(values.get("privacy_preflight_mock_enabled", False)),
    }


def save_section(section: str, incoming: dict[str, Any], user_id: int, db) -> dict[str, Any]:
    if section not in DEFAULT_SETTINGS:
        raise KeyError(section)
    if not isinstance(incoming, dict):
        raise ValueError("设置内容格式错误")

    # public_section 会附加 *_configured 供界面显示密钥是否已配置。
    # 这些是只读状态，客户端即使原样回传也不应当作未知设置拒绝。
    read_only_fields = {
        f"{field}_configured" for field in SECRET_FIELDS.get(section, set())
    }
    incoming = {
        key: value for key, value in incoming.items() if key not in read_only_fields
    }
    if section == "ocr":
        incoming.pop("paddleocr_status", None)

    allowed = set(DEFAULT_SETTINGS[section])
    unknown = set(incoming) - allowed
    if unknown:
        raise ValueError(f"存在不支持的设置项：{', '.join(sorted(unknown))}")

    current = get_section(section, db)
    for key, value in incoming.items():
        if key in SECRET_FIELDS.get(section, set()) and value in ("", MASK, None):
            continue
        current[key] = value
    _validate(section, current)

    from backend.app.models import SystemSetting
    stored_values = copy.deepcopy(current)
    for field in SECRET_FIELDS.get(section, set()):
        if stored_values.get(field):
            stored_values[field] = _encrypt_secret(str(stored_values[field]))

    row = db.query(SystemSetting).filter(SystemSetting.section == section).first()
    if row:
        row.value_json = stored_values
        row.updated_by = user_id
    else:
        row = SystemSetting(section=section, value_json=stored_values, updated_by=user_id)
        db.add(row)
    db.flush()
    return public_section(section, db)


def _validate(section: str, data: dict[str, Any]) -> None:
    if section == "basic":
        if not str(data["system_name"]).strip():
            raise ValueError("系统名称不能为空")
        if data["page_size"] not in (10, 20, 50, 100):
            raise ValueError("每页显示数不合法")
        if data["theme_mode"] not in ("system", "light", "dark"):
            raise ValueError("主题模式不合法")
        if data["table_density"] not in ("compact", "comfortable", "spacious"):
            raise ValueError("表格密度不合法")
        if data.get("logo_data_url") and not str(data["logo_data_url"]).startswith("data:image/"):
            raise ValueError("Logo 格式不合法")
    elif section == "ai":
        if data.get("provider") not in {"siliconflow", "deepseek", "intranet", "openai-compatible"}:
            raise ValueError("不支持的 AI 服务商")
        if not str(data["base_url"]).startswith(("http://", "https://")):
            raise ValueError("API 地址必须以 http:// 或 https:// 开头")
        if not str(data.get("deepseek_base_url", "")).startswith(("http://", "https://")):
            raise ValueError("DeepSeek API 地址必须以 http:// 或 https:// 开头")
        if not str(data.get("intranet_base_url", "")).startswith(("http://", "https://")):
            raise ValueError("单位内网 API 地址必须以 http:// 或 https:// 开头")
        if not str(data.get("review_model", "")).strip() or not str(data.get("vision_model", "")).strip():
            raise ValueError("硅基流动审核模型和视觉模型不能为空")
        if not str(data.get("deepseek_review_model", "")).strip() or not str(data.get("deepseek_vision_model", "")).strip():
            raise ValueError("DeepSeek 审核模型和视觉模型不能为空")
        if not str(data.get("intranet_review_model", "")).strip() or not str(data.get("intranet_vision_model", "")).strip():
            raise ValueError("单位内网审核模型和视觉模型不能为空")
        if not 0 <= float(data["temperature"]) <= 2:
            raise ValueError("温度必须在 0 到 2 之间")
        if not 256 <= int(data["max_tokens"]) <= 32768:
            raise ValueError("最大输出 Token 必须在 256 到 32768 之间")
        if not 10 <= int(data["timeout_seconds"]) <= 600:
            raise ValueError("请求超时必须在 10 到 600 秒之间")
        if not 0 <= float(data.get("deepseek_temperature", 0.2)) <= 2:
            raise ValueError("DeepSeek 温度必须在 0 到 2 之间")
        if not 256 <= int(data.get("deepseek_max_tokens", 16384)) <= 32768:
            raise ValueError("DeepSeek 最大输出 Token 必须在 256 到 32768 之间")
        if not 10 <= int(data.get("deepseek_timeout_seconds", 240)) <= 600:
            raise ValueError("DeepSeek 请求超时必须在 10 到 600 秒之间")
        if not isinstance(data.get("deepseek_thinking_enabled", False), bool):
            raise ValueError("DeepSeek 思考模式开关格式错误")
        if data.get("deepseek_reasoning_effort", "max") not in {"low", "high", "max"}:
            raise ValueError("DeepSeek 思考强度仅支持 low、high 或 max")
        if not 0 <= float(data.get("intranet_temperature", 0.7)) <= 2:
            raise ValueError("单位内网模型温度必须在 0 到 2 之间")
        if not 256 <= int(data.get("intranet_max_tokens", 262144)) <= 262144:
            raise ValueError("单位内网最大输出 Token 必须在 256 到 262144 之间")
        if not 10 <= int(data.get("intranet_timeout_seconds", 600)) <= 1800:
            raise ValueError("单位内网请求超时必须在 10 到 1800 秒之间")
        if not isinstance(data.get("intranet_thinking_enabled", False), bool):
            raise ValueError("单位内网思考模式开关格式错误")
        if data.get("privacy_preflight_mode", "enforce") not in {"disabled", "shadow", "enforce"}:
            raise ValueError("内网安全预审模式仅支持 disabled、shadow 或 enforce")
        for field in (
            "intranet_content_review_enabled",
            "external_content_review_enabled",
            "privacy_preflight_logic_enabled",
            "privacy_preflight_mock_enabled",
            "privacy_company_blacklist_enabled",
            "privacy_application_blacklist_enabled",
            "privacy_outbound_block_enabled",
        ):
            if not isinstance(data.get(field, True), bool):
                raise ValueError(f"{field} 开关格式错误")
    elif section == "ocr":
        if data.get("engine") == "paddleocr" and data.get("enabled") is not True:
            raise ValueError("PaddleOCR主流程需要保持OCR开启；如需停用请先切换引擎")
        if not isinstance(data.get("paddleocr_api_key", ""), str) or any(c in data.get("paddleocr_api_key", "") for c in ('\n', '\r')):
            raise ValueError("PaddleOCR密钥格式错误")
        if data.get("engine") not in {"rapidocr", "mineru", "mineru_hybrid", "hybrid", "paddleocr"}:
            raise ValueError("不支持的 OCR 引擎")
        if not 100 <= int(data["render_dpi"]) <= 400:
            raise ValueError("OCR 渲染精度必须在 100 到 400 DPI 之间")
        if not 1 <= int(data["vision_concurrency"]) <= 8:
            raise ValueError("视觉识别并发数必须在 1 到 8 之间")
        if not isinstance(data.get("table_visual_repair_enabled", False), bool):
            raise ValueError("内网表格视觉修复开关格式错误")
        if not str(data.get("mineru_base_url", "")).startswith(("http://", "https://")):
            raise ValueError("MinerU API 地址必须以 http:// 或 https:// 开头")
        if str(data.get("mineru_task_endpoint", "")) not in {"/extract/task", "extract/task"}:
            raise ValueError("MinerU 任务接口必须为 /extract/task")
        if str(data.get("mineru_model_version", "")) not in {"pipeline", "vlm"}:
            raise ValueError("MinerU 模型版本必须为 pipeline 或 vlm")
        if not 60 <= int(data.get("mineru_timeout_seconds", 300)) <= 1800:
            raise ValueError("MinerU 超时时间必须在 60 到 1800 秒之间")
        if not 2 <= int(data.get("mineru_poll_interval_seconds", 3)) <= 30:
            raise ValueError("MinerU 轮询间隔必须在 2 到 30 秒之间")
    elif section == "company_registry":
        if data.get("provider") != "tianyancha":
            raise ValueError("目前仅支持天眼查企业数据接口")
        if data.get("mode") not in {"browser", "api"}:
            raise ValueError("不支持的企业核验方式")
        if not str(data.get("base_url", "")).startswith("https://"):
            raise ValueError("企业数据接口必须使用 HTTPS")
        if data.get("auth_mode") not in {"bearer", "raw"}:
            raise ValueError("不支持的 Token 鉴权方式")
        if not 5 <= int(data.get("timeout_seconds", 20)) <= 120:
            raise ValueError("企业接口超时时间必须在 5 到 120 秒之间")
        include_fields = data.get("include_fields", [])
        if not isinstance(include_fields, list) or not set(include_fields).issubset({"shareholders", "risk"}):
            raise ValueError("企业接口附加数据项不合法")
        if not 1 <= int(data.get("web_daily_limit", 10)) <= 50:
            raise ValueError("每日网页核验上限必须在 1 到 50 家之间")
        if not 10 <= int(data.get("web_interval_seconds", 20)) <= 300:
            raise ValueError("网页查询间隔必须在 10 到 300 秒之间")
    elif section == "files":
        if not 1 <= int(data["max_upload_mb"]) <= 500:
            raise ValueError("单文件大小必须在 1 到 500 MB 之间")
        if "pdf" not in [str(x).lower().lstrip(".") for x in data["allowed_extensions"]]:
            raise ValueError("当前审核流程必须允许 PDF 文件")
    elif section == "workflow":
        if not 1 <= int(data["worker_concurrency"]) <= 16:
            raise ValueError("并发数必须在 1 到 16 之间")
        if not 1 <= int(data["poll_interval_seconds"]) <= 60:
            raise ValueError("轮询间隔必须在 1 到 60 秒之间")
        if not 0 <= int(data["max_retries"]) <= 10:
            raise ValueError("重试次数必须在 0 到 10 之间")
    elif section == "security":
        if not isinstance(data.get("public_access_enabled"), bool):
            raise ValueError("公网访问开关必须为布尔值")
        if not 1 <= int(data["jwt_expire_hours"]) <= 168:
            raise ValueError("登录有效期必须在 1 到 168 小时之间")
        if not 6 <= int(data["password_min_length"]) <= 64:
            raise ValueError("密码最小长度必须在 6 到 64 位之间")
    elif section == "roles":
        valid = set(DEFAULT_SETTINGS["roles"]["admin"])
        for role in ("admin", "reviewer", "viewer"):
            if role not in data or not isinstance(data[role], list):
                raise ValueError("角色权限格式错误")
            if not set(data[role]).issubset(valid):
                raise ValueError("包含未知权限")
        # 管理员权限固定完整，避免误操作锁死系统。
        data["admin"] = copy.deepcopy(DEFAULT_SETTINGS["roles"]["admin"])
        data["reviewer"] = [item for item in data["reviewer"] if item not in ADMIN_ONLY_PERMISSIONS]
        data["viewer"] = [item for item in data["viewer"] if item not in ADMIN_ONLY_PERMISSIONS]


def effective_permissions(user, db=None) -> list[str]:
    """返回用户当前实际权限。管理员固定完整，其他用户可覆盖角色默认值。"""
    roles = get_section("roles", db)
    if user.role == "admin":
        return list(roles.get("admin", DEFAULT_SETTINGS["roles"]["admin"]))
    if user.permissions_override is not None:
        valid = set(DEFAULT_SETTINGS["roles"]["admin"])
        permissions = [item for item in user.permissions_override if item in valid and item not in ADMIN_ONLY_PERMISSIONS]
    else:
        permissions = [item for item in roles.get(user.role, []) if item not in ADMIN_ONLY_PERMISSIONS]
    # 下样由管理员和审核员共同操作；历史任务删除仍由接口单独限定管理员。
    if user.role == "reviewer" and "sampling.manage" not in permissions:
        permissions.append("sampling.manage")
    return permissions


def has_permission(user, permission: str, db=None) -> bool:
    return permission in effective_permissions(user, db)
