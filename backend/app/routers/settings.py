"""管理员系统设置、连通性测试、客户端配置和操作日志。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from backend.app import models
from backend.app.auth import get_current_user, require_role
from backend.app.database import get_db
from backend.app.settings_store import (
    DEFAULT_SETTINGS, effective_permissions, get_ai_runtime_config, get_intranet_runtime_config,
    get_section, public_section, save_section,
)
from backend.app.audit import add_audit

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/client")
def client_settings(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """所有登录用户可读的品牌、显示偏好及当前实际权限。"""
    basic = get_section("basic", db)
    return {
        "basic": basic,
        "permissions": effective_permissions(user, db),
    }


@router.get("")
def all_settings(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_role("admin")),
):
    return {section: public_section(section, db) for section in DEFAULT_SETTINGS}


@router.put("/{section}")
def update_settings(
    section: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    try:
        result = save_section(section, body, user.id, db)
    except KeyError:
        raise HTTPException(status_code=404, detail="设置分组不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    changed = ", ".join(sorted(body.keys()))
    add_audit(db, user, "系统设置", f"更新{section}设置", f"变更字段：{changed}", request)
    db.commit()
    return result


@router.post("/ai/test")
def test_ai_connection(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    ai = get_ai_runtime_config(db)
    if not ai.get("mock_enabled") and not ai.get("api_key"):
        raise HTTPException(status_code=400, detail="请先填写并保存 API Key")
    try:
        from backend.app.llm import LLMClient
        content = LLMClient().chat(
            "你是连接测试助手，只回复 OK。",
            "请回复 OK。",
            model=ai.get("review_model"),
            max_retries=0,
        )
        add_audit(db, user, "AI审核", "测试模型连接", f"模型：{ai.get('review_model')}", request)
        db.commit()
        return {
            "ok": True,
            "message": "Mock 模式连接正常" if ai.get("mock_enabled") else "模型连接成功",
            "provider": ai.get("provider"),
            "model": ai.get("review_model"),
            "response": content[:120],
        }
    except Exception as exc:
        add_audit(db, user, "AI审核", "测试模型连接", str(exc), request, success=False)
        db.commit()
        raise HTTPException(status_code=502, detail=f"连接失败：{exc}")


@router.post("/ocr/paddle/test")
def test_paddle_connection(
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    """Admin-confirmed synthetic-image submission; no report or raw key returned."""
    import re
    import httpx
    import pymupdf
    from backend.app.paddle_runtime import credential, ENDPOINT, MODEL, OPTIONS
    if body.get("confirm_test_image") is not True:
        raise HTTPException(status_code=400, detail="请确认发送不含业务资料的测试图片；此操作可能消耗OCR额度")
    config = get_section("ocr", db)
    if config.get("engine") != "paddleocr" or not config.get("enabled"):
        raise HTTPException(status_code=400, detail="请先保存并启用PaddleOCR引擎")
    try:
        key = credential(config)
    except Exception:
        raise HTTPException(status_code=400, detail="PaddleOCR凭证未配置或服务器凭证文件不可读") from None
    with pymupdf.open() as document:
        page = document.new_page(width=360, height=100)
        page.insert_text((20, 50), "OCR CONNECTION TEST", fontsize=20)
        image = page.get_pixmap(alpha=False).tobytes("png")
    provider_status = None
    try:
        # Only connection establishment is retried. A read/write timeout after
        # submission must not result in a second POST.
        with httpx.Client(timeout=httpx.Timeout(15, connect=3), follow_redirects=False, trust_env=False,
                          transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=2)) as client:
            response = client.post(ENDPOINT, headers={"Authorization": "bearer " + key},
                data={"model": MODEL, "optionalPayload": json.dumps(OPTIONS)},
                files={"file": ("connection-test.png", image, "image/png")})
        provider_status = response.status_code
        if response.status_code != 200:
            raise ValueError("provider_rejected")
        job = response.json()["data"]["jobId"]
        if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", job):
            raise ValueError("invalid_job_id")
    except Exception as exc:
        # Provider response text can echo a credential: never expose or log it.
        add_audit(db, user, "OCR设置", "Paddle连接测试", f"{type(exc).__name__}; provider_http_status={provider_status}", request, success=False)
        db.commit()
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            message = "无法建立PaddleOCR连接，请检查服务器网络、DNS或证书后重试"
        elif provider_status in (401, 403):
            message = f"PaddleOCR拒绝访问（HTTP {provider_status}），请检查已保存的凭证及服务权限"
        elif provider_status == 429:
            message = "PaddleOCR额度或请求频率受限（HTTP 429），请稍后检查服务额度"
        else:
            message = "PaddleOCR测试提交未确认；请勿连续点击重试，以免重复消耗额度"
        raise HTTPException(status_code=502, detail=message) from None
    add_audit(db, user, "OCR设置", "Paddle连接测试", "不含业务资料的测试图片已提交；任务：" + job, request)
    db.commit()
    return {"ok": True, "message": "连接及鉴权成功，测试图片已提交。未上传业务报告；这不代表整份报告识别已完成验收。",
            "job_id": job, "recognition_completed": False}


@router.post("/ai/preflight/test")
def test_preflight_ai_connection(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    """独立测试内网内容复核模型，不切换当前主审核供应商。"""
    ai = get_intranet_runtime_config(db)
    if not ai.get("mock_enabled") and not ai.get("api_key"):
        raise HTTPException(status_code=400, detail="请先填写并保存单位内网 API Key")
    try:
        from backend.app.llm import LLMClient
        content = LLMClient(ai_config=ai).chat(
            "你是连接测试助手，只回复 OK。",
            "请回复 OK。",
            model=ai.get("review_model"),
            max_retries=0,
            # 部分内网网关仍会为关闭思考的模型计算生成 token。
            # 32 可能在返回正文前耗尽，导致连接正常却被误报为空响应。
            max_tokens=512,
        )
        add_audit(db, user, "AI内容复核", "测试内网内容复核模型连接", f"模型：{ai.get('review_model')}", request)
        db.commit()
        return {
            "ok": True,
            "message": "Mock 模式连接正常" if ai.get("mock_enabled") else "内网内容复核模型连接成功",
            "provider": "intranet",
            "model": ai.get("review_model"),
            "response": content[:120],
        }
    except Exception as exc:
        add_audit(db, user, "AI内容复核", "测试内网内容复核模型连接", str(exc), request, success=False)
        db.commit()
        raise HTTPException(status_code=502, detail=f"连接失败：{exc}")


@router.post("/ai/table-vision/test")
def test_table_vision_connection(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    """Verify the intranet VLM with a synthetic image, never a report page."""
    import re
    import tempfile
    from pathlib import Path
    from PIL import Image, ImageDraw

    ai = get_intranet_runtime_config(db)
    if ai.get("mock_enabled"):
        raise HTTPException(status_code=400, detail="请关闭内网模型演示模式后再测试视觉能力")
    if not ai.get("api_key"):
        raise HTTPException(status_code=400, detail="请先填写并保存单位内网 API Key")
    image_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="table-vision-test-", suffix=".png", delete=False) as handle:
            image_path = Path(handle.name)
        image = Image.new("RGB", (640, 220), "white")
        ImageDraw.Draw(image).text((80, 80), "TABLE 27", fill="black", stroke_width=1)
        image.save(image_path)
        from backend.app.llm import LLMClient
        content = LLMClient(timeout_seconds=90, ai_config=ai).vision_ocr(
            str(image_path),
            prompt="只读取图片中央的英文和数字，只回复读取结果。",
            max_retries=0,
        )
        normalized = re.sub(r"\s+", "", content).upper()
        if "TABLE" not in normalized or "27" not in normalized:
            raise ValueError("visual_content_mismatch")
        add_audit(db, user, "表格预处理", "测试内网视觉模型", f"模型：{ai.get('vision_model')}", request)
        db.commit()
        return {
            "ok": True,
            "message": "内网视觉模型可读取图片；正式流程仅在表格行列关系无法确定时调用",
            "provider": "intranet",
            "model": ai.get("vision_model"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        add_audit(db, user, "表格预处理", "测试内网视觉模型", type(exc).__name__, request, success=False)
        db.commit()
        raise HTTPException(status_code=502, detail="内网模型连接正常性或视觉识别能力未通过测试") from None
    finally:
        if image_path is not None:
            image_path.unlink(missing_ok=True)


@router.post("/company-registry/test")
def test_company_registry_connection(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    """使用公开测试关键词验证天眼查鉴权、网络和字段映射。"""
    try:
        from backend.app.company_registry import TianyanchaClient
        result = TianyanchaClient(db).search_company("天眼查", exact_match=False)
        add_audit(db, user, "企业核验", "测试天眼查连接", f"状态：{result['raw_status']}", request)
        db.commit()
        return {
            "ok": True,
            "message": "天眼查连接成功",
            "company": result["name"],
            "status": result["raw_status"],
        }
    except Exception as exc:
        add_audit(db, user, "企业核验", "测试天眼查连接", str(exc), request, success=False)
        db.commit()
        raise HTTPException(status_code=502, detail=f"连接失败：{exc}")


@router.get("/logs")
def list_audit_logs(
    module: str = Query(""),
    q: str = Query(""),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: models.User = Depends(require_role("admin")),
):
    query = db.query(models.AuditLog)
    if module:
        query = query.filter(models.AuditLog.module == module)
    if q:
        query = query.filter(
            models.AuditLog.username.ilike(f"%{q}%") |
            models.AuditLog.action.ilike(f"%{q}%") |
            models.AuditLog.detail.ilike(f"%{q}%")
        )
    total = query.count()
    rows = query.order_by(models.AuditLog.id.desc()).offset((page - 1) * size).limit(size).all()
    return {
        "total": total,
        "items": [
            {
                "id": row.id,
                "username": row.username,
                "module": row.module,
                "action": row.action,
                "detail": row.detail or "",
                "ip_address": row.ip_address or "",
                "success": row.success,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }
