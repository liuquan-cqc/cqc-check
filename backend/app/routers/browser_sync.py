"""Edge浏览器辅助程序：短时配对、设备管理和报告同步。"""
from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.app import models
from backend.app.audit import add_audit
from backend.app.auth import require_role
from backend.app.database import get_db
from backend.app.routers import reports
from backend.app import browser_intake


router = APIRouter(prefix="/api/browser-sync", tags=["browser-sync"])
EDGE_EXTENSION_VERSION = "0.6.0"
_PAIRING_CODES: dict[str, tuple[int, object]] = {}
_PAIRING_LOCK = threading.Lock()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _device_payload(device: models.BrowserSyncDevice) -> dict:
    return {
        "id": device.id,
        "name": device.name,
        "status": device.status,
        "last_seen_at": device.last_seen_at,
        "created_at": device.created_at,
    }


def _current_device(
    token: str | None = Header(None, alias="X-Browser-Sync-Token"),
    db: Session = Depends(get_db),
) -> models.BrowserSyncDevice:
    if not token:
        raise HTTPException(status_code=401, detail="浏览器辅助程序尚未配对")
    device = db.query(models.BrowserSyncDevice).filter(
        models.BrowserSyncDevice.token_hash == _token_hash(token),
        models.BrowserSyncDevice.status == "active",
    ).first()
    if not device:
        raise HTTPException(status_code=401, detail="配对已失效，请联系管理员重新配对")
    return device


def _shanghai_day_start_utc() -> datetime:
    now_utc = models._utc_now()
    shanghai_date = (now_utc + timedelta(hours=8)).date()
    return datetime.combine(shanghai_date, time.min) - timedelta(hours=8)


def _verification_task_payload(task: models.CompanyVerificationTask, interval_seconds: int) -> dict:
    return {
        "task_id": task.id,
        "company_id": task.company.id,
        "company_name": task.company.name,
        "unified_social_credit_code": task.company.unified_social_credit_code or "",
        "search_url": f"https://www.tianyancha.com/search?key={quote(task.company.name)}",
        "interval_seconds": interval_seconds,
    }


@router.post("/pairing-code")
def create_pairing_code(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    """管理员生成10分钟有效的一次性配对码。"""
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = models._utc_now() + timedelta(minutes=10)
    with _PAIRING_LOCK:
        now = models._utc_now()
        for old_code, (_, expiry) in list(_PAIRING_CODES.items()):
            if expiry <= now:
                _PAIRING_CODES.pop(old_code, None)
        _PAIRING_CODES[code] = (user.id, expires_at)
    add_audit(db, user, "浏览器同步", "生成一次性配对码", "有效期10分钟", request)
    db.commit()
    return {"code": code, "expires_at": expires_at}


@router.post("/pair")
def pair_device(
    code: str = Form(...),
    device_name: str = Form("Win11 Edge"),
    db: Session = Depends(get_db),
):
    """用一次性配对码换取设备令牌；令牌明文只返回这一次。"""
    with _PAIRING_LOCK:
        pairing = _PAIRING_CODES.pop(code.strip(), None)
    if not pairing or pairing[1] <= models._utc_now():
        raise HTTPException(status_code=400, detail="配对码无效或已过期")
    user_id = pairing[0]
    token = secrets.token_urlsafe(32)
    device = models.BrowserSyncDevice(
        name=(device_name or "Win11 Edge").strip()[:120],
        token_hash=_token_hash(token),
        status="active",
        created_by=user_id,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return {"token": token, "device": _device_payload(device)}


@router.get("/devices")
def list_devices(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_role("admin")),
):
    rows = db.query(models.BrowserSyncDevice).order_by(models.BrowserSyncDevice.id.desc()).all()
    return [_device_payload(row) for row in rows]


@router.get("/extension-package")
def download_extension_package(
    _: models.User = Depends(require_role("admin")),
):
    filename = f"edge-report-sync-{EDGE_EXTENSION_VERSION}.zip"
    package = Path(__file__).resolve().parents[3] / "browser-extension" / filename
    if not package.is_file():
        raise HTTPException(status_code=404, detail="Edge扩展安装包尚未生成")
    return FileResponse(
        str(package), media_type="application/zip",
        filename=filename,
    )


@router.get("/company-verification/next")
def next_company_verification(
    db: Session = Depends(get_db),
    device: models.BrowserSyncDevice = Depends(_current_device),
):
    """向已配对 Edge 下发一家待核验企业；不返回任何 Cookie 或账号信息。"""
    from backend.app.settings_store import get_section
    config = get_section("company_registry", db)
    if not config.get("enabled") or config.get("mode") != "browser":
        raise HTTPException(status_code=400, detail={"code": "disabled", "message": "Edge 网页辅助核验尚未启用"})
    limit = int(config.get("web_daily_limit", 10))
    task = db.query(models.CompanyVerificationTask).filter(
        models.CompanyVerificationTask.device_id == device.id,
        models.CompanyVerificationTask.status == "processing",
    ).order_by(models.CompanyVerificationTask.id).first()
    if task:
        return _verification_task_payload(task, int(config.get("web_interval_seconds", 20)))

    used = db.query(models.CompanyVerificationTask).filter(
        models.CompanyVerificationTask.started_at >= _shanghai_day_start_utc(),
        models.CompanyVerificationTask.status.in_(["processing", "done", "failed"]),
    ).count()
    if used >= limit:
        raise HTTPException(status_code=429, detail={"code": "daily_limit", "message": f"今日核验上限 {limit} 家已用完"})

    task = db.query(models.CompanyVerificationTask).filter(
        models.CompanyVerificationTask.status == "queued"
    ).order_by(models.CompanyVerificationTask.id).first()
    if not task:
        raise HTTPException(status_code=404, detail={"code": "no_tasks", "message": "核验队列中没有待处理企业"})
    task.status = "processing"
    task.device_id = device.id
    task.started_at = models._utc_now()
    task.error_msg = None
    device.last_seen_at = models._utc_now()
    db.commit()
    db.refresh(task)
    return _verification_task_payload(task, int(config.get("web_interval_seconds", 20)))


@router.post("/company-verification/{task_id}/result")
def submit_company_verification(
    task_id: int,
    request: Request,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    device: models.BrowserSyncDevice = Depends(_current_device),
):
    from backend.app.company_registry import _normalize_name, map_operating_status
    task = db.query(models.CompanyVerificationTask).filter(
        models.CompanyVerificationTask.id == task_id,
        models.CompanyVerificationTask.device_id == device.id,
        models.CompanyVerificationTask.status == "processing",
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="核验任务不存在或已结束")
    reported_name = str(body.get("company_name") or "").strip()
    raw_status = str(body.get("raw_status") or "").strip()
    source_url = str(body.get("source_url") or "").strip()
    host = (urlparse(source_url).hostname or "").lower()
    error = ""
    if _normalize_name(reported_name) != _normalize_name(task.company.name):
        error = "页面企业全称与待核验企业不一致"
    elif not raw_status:
        error = "页面中未读取到登记状态"
    elif host != "tianyancha.com" and not host.endswith(".tianyancha.com"):
        error = "核验来源不是天眼查页面"
    if error:
        task.status = "failed"
        task.error_msg = error
        task.finished_at = models._utc_now()
        db.commit()
        raise HTTPException(status_code=400, detail=error)

    company = task.company
    company.operating_status = map_operating_status(raw_status)
    company.status_source = f"天眼查网页辅助核验（原始状态：{raw_status}）"
    company.status_checked_at = models._utc_now()
    credit_code = str(body.get("unified_social_credit_code") or "").strip().upper()
    if credit_code:
        company.unified_social_credit_code = credit_code[:32]
    task.raw_status = raw_status[:100]
    task.source_url = source_url[:1000]
    task.status = "done"
    task.finished_at = models._utc_now()
    device.last_seen_at = models._utc_now()
    creator = db.query(models.User).filter(models.User.id == device.created_by).first()
    if creator:
        add_audit(
            db, creator, "企业核验", "Edge 回传天眼查核验结果",
            f"企业：{company.name}；状态：{raw_status}；任务：{task.id}", request,
        )
    db.commit()
    return {"ok": True, "company_id": company.id, "operating_status": company.operating_status, "raw_status": raw_status}


@router.post("/company-verification/{task_id}/pause")
def pause_company_verification(
    task_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    device: models.BrowserSyncDevice = Depends(_current_device),
):
    task = db.query(models.CompanyVerificationTask).filter(
        models.CompanyVerificationTask.id == task_id,
        models.CompanyVerificationTask.device_id == device.id,
        models.CompanyVerificationTask.status == "processing",
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="核验任务不存在或已结束")
    task.status = "queued"
    task.device_id = None
    task.started_at = None
    task.error_msg = str(body.get("reason") or "网页核验已暂停")[:1000]
    device.last_seen_at = models._utc_now()
    db.commit()
    return {"ok": True}


@router.delete("/devices/{device_id}")
def revoke_device(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    device = db.query(models.BrowserSyncDevice).filter(models.BrowserSyncDevice.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="配对设备不存在")
    device.status = "revoked"
    add_audit(db, user, "浏览器同步", "撤销设备配对", f"设备：{device.name}，ID：{device.id}", request)
    db.commit()
    return {"ok": True}


@router.get("/batch-capabilities")
def batch_capabilities(device: models.BrowserSyncDevice = Depends(_current_device)):
    return {"prevent_automatic_revision": True, "version": 2, "typed_intake": True, "identity": ["application_no", "task_no", "category"]}


@router.post("/upload")
def sync_report(
    request: Request,
    file: UploadFile = File(...),
    source_task_no: str = Form(""),
    source_application_no: str = Form(""),
    source_url: str = Form(""),
    batch_mode: bool = Form(False),
    intake_mode: str = Form(""),
    db: Session = Depends(get_db),
    device: models.BrowserSyncDevice = Depends(_current_device),
):
    """接收单位系统PDF；重复时跳过，同申请编号时自动进入更正版本链。"""
    if intake_mode == "typed_v1":
        return browser_intake.intake(request, file, source_application_no.strip(), source_task_no.strip(), source_url, db, device)
    if intake_mode:
        raise HTTPException(400, "不支持的报告接收模式")
    creator = db.query(models.User).filter(models.User.id == device.created_by).first()
    if not creator or creator.status != "active":
        raise HTTPException(status_code=403, detail="配对管理员账号已停用")
    try:
        report = reports.upload_report(
            request=request, file=file, application_no_hint=source_application_no,
            db=db, current=creator,
        )
        action = "created"
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if exc.status_code != 409 or detail.get("code") not in {"exact_duplicate", "application_exists"}:
            raise
        if detail.get("code") == "exact_duplicate":
            existing = db.query(models.Report).filter(models.Report.id == detail.get("report_id")).first()
            if not existing:
                raise
            report = existing
            action = "duplicate"
        else:
            if batch_mode:
                raise HTTPException(status_code=409, detail={
                    "code": "batch_application_conflict",
                    "message": "同申请编号已有不同文件；批量同步不自动创建更正版本，请按任务核对后单独处理",
                    "report_id": detail.get("report_id"),
                })
            parent = db.query(models.Report).filter(models.Report.id == detail.get("report_id")).first()
            if not parent:
                raise HTTPException(status_code=409, detail="未找到同申请编号的原报告")
            if parent.status != "done" or not parent.result_json:
                raise HTTPException(status_code=409, detail={
                    "code": "existing_report_in_progress",
                    "message": "同申请编号的报告仍在审核中，本次同步已暂停",
                    "report_id": parent.id,
                })
            file.file.seek(0)
            report = reports.upload_correction_version(
                report_id=parent.id,
                request=request,
                file=file,
                revision_note=f"浏览器同步更正版本；来源任务：{source_task_no or '-'}",
                db=db,
                current=creator,
            )
            action = "revision"

    device.last_seen_at = models._utc_now()
    add_audit(
        db, creator, "浏览器同步", "同步单位系统报告",
        f"设备：{device.name}，任务：{source_task_no or '-'}，申请：{source_application_no or '-'}，"
        f"结果：{action}，报告ID：{report.id}，来源：{source_url[:300] or '-'}",
        request,
    )
    db.commit()
    return {
        "action": action,
        "report_id": report.id,
        "application_no": report.application_no or source_application_no,
        "revision_no": report.revision_no or 1,
        "status": report.status,
    }
