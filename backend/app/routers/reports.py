"""报告相关API。"""
import os
import shutil
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, aliased, selectinload
from backend.app import models, schemas
from backend.app.auth import get_current_user, require_permission
from backend.app.database import get_db, init_db
from backend.app.config import get_settings
from backend.app.review import build_markdown
from backend.app.extract import extract_report_info
from backend.app.audit import add_audit
from backend.app.progress import read_progress, write_progress

router = APIRouter(prefix="/api/reports", tags=["reports"])
_UPLOAD_LOCK = threading.RLock()


def _safe_remove_file(file_path: Optional[str], base_dir: str):
    """删除base_dir内的文件；路径不在该目录内则跳过，防止路径穿越误删。"""
    if not file_path:
        return
    try:
        base = Path(base_dir).resolve()
        target = Path(file_path).resolve()
        if target.is_file() and str(target).startswith(str(base) + os.sep):
            target.unlink()
    except OSError:
        pass


def _safe_remove_dir(dir_path: Optional[str], base_dir: str):
    """删除base_dir内的目录（含内容）；路径不在该目录内则跳过。"""
    if not dir_path:
        return
    try:
        base = Path(base_dir).resolve()
        target = Path(dir_path).resolve()
        if target.is_dir() and str(target).startswith(str(base) + os.sep):
            shutil.rmtree(target)
    except OSError:
        pass


def _save_upload(file: UploadFile, report_id: int) -> str:
    settings = get_settings()
    ext = Path(file.filename or "report.pdf").suffix
    upload_dir = Path(settings.uploads_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{report_id}{ext}"
    with target.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return str(target)


def _upload_sha256(file: UploadFile) -> str:
    """流式计算文件指纹并复位，避免把大 PDF 整体读入内存。"""
    digest = hashlib.sha256()
    while chunk := file.file.read(1024 * 1024):
        digest.update(chunk)
    file.file.seek(0)
    return digest.hexdigest()


def _original_filename(file: UploadFile) -> str:
    """只保存文件名，不保留客户端目录信息。"""
    return Path(file.filename or "report.pdf").name[:500]


def _extract_upload_metadata(file_path: str) -> dict[str, str]:
    """读取前3页文字层，尽早识别申请编号；失败时由 Worker 后续完整提取。"""
    import fitz
    doc = fitz.open(file_path)
    try:
        text_value = "\n".join(doc[index].get_text("text") for index in range(min(3, len(doc))))
    finally:
        doc.close()
    return extract_report_info(text_value)


def _duplicate_detail(report: models.Report) -> dict[str, object]:
    return {
        "code": "exact_duplicate",
        "message": "该PDF内容与已上传报告完全相同，请直接查看已有报告",
        "report_id": report.id,
        "application_no": report.application_no,
        "report_no": report.report_no,
        "revision_no": report.revision_no or 1,
    }


def _application_exists_detail(report: models.Report) -> dict[str, object]:
    return {
        "code": "application_exists",
        "message": "该申请编号已存在，建议作为原报告的更正版本上传",
        "report_id": report.id,
        "root_report_id": report.root_report_id or report.id,
        "application_no": report.application_no,
        "report_no": report.report_no,
        "revision_no": report.revision_no or 1,
        "next_revision_no": int(report.revision_no or 1) + 1,
    }


def _latest_application_report(db: Session, application_no: str) -> models.Report | None:
    return db.query(models.Report).filter(
        models.Report.application_no == application_no
    ).order_by(models.Report.revision_no.desc(), models.Report.id.desc()).first()


def _apply_metadata(db: Session, report: models.Report, metadata: dict[str, str]) -> None:
    for field in ("report_no", "application_no", "product_unit"):
        if metadata.get(field):
            setattr(report, field, metadata[field])
    company_name = metadata.get("company")
    if company_name:
        company = db.query(models.Company).filter(models.Company.name == company_name).first()
        if not company:
            # PDF 可能恰好在“有限公司”中间换行。若数据库已有唯一的
            # 长/短名前缀候选，优先复用或升级该企业，避免拆成多条。
            compatible = [
                item for item in db.query(models.Company).all()
                if min(len(item.name), len(company_name)) >= 6
                and abs(len(item.name) - len(company_name)) <= 8
                and (item.name.startswith(company_name) or company_name.startswith(item.name))
            ]
            if len(compatible) == 1:
                company = compatible[0]
                if company_name.startswith(company.name) and len(company_name) > len(company.name):
                    company.name = company_name
            else:
                company = models.Company(name=company_name)
                db.add(company)
                db.flush()
        report.company_id = company.id


def _validate_upload(file: UploadFile, db: Session) -> None:
    """按系统文件设置校验上传格式和大小，并把文件指针复位。"""
    from backend.app.settings_store import get_section
    file_settings = get_section("files", db)
    extension = Path(file.filename or "").suffix.lower().lstrip(".")
    allowed = [str(item).lower().lstrip(".") for item in file_settings.get("allowed_extensions", ["pdf"])]
    if extension not in allowed:
        raise HTTPException(status_code=400, detail=f"只接受以下文件格式：{', '.join(allowed)}")
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0)
    max_bytes = int(file_settings.get("max_upload_mb", 50)) * 1024 * 1024
    if file_size > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件不能超过 {file_settings.get('max_upload_mb', 50)} MB")


@router.post("/upload", response_model=schemas.ReportOut)
def upload_report(
    request: Request,
    file: UploadFile = File(...),
    application_no_hint: str = Form(""),
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("reports.upload")),
):
    """上传PDF，创建报告和任务（admin/reviewer）。"""
    _validate_upload(file, db)
    file_hash = _upload_sha256(file)
    with _UPLOAD_LOCK:
        duplicate = db.query(models.Report).filter(models.Report.file_sha256 == file_hash).first()
        if duplicate:
            raise HTTPException(status_code=409, detail=_duplicate_detail(duplicate))

        report = models.Report(
            status="pending", file_path="", ocr_path="", report_no=None,
            original_filename=_original_filename(file), file_sha256=file_hash,
            uploaded_by_id=current.id,
        )
        db.add(report)
        db.flush()
        report.root_report_id = report.id
        report.file_path = _save_upload(file, report.id)

        metadata: dict[str, str] = {}
        try:
            metadata = _extract_upload_metadata(report.file_path)
        except Exception:
            pass
        if not metadata.get("application_no") and application_no_hint.strip():
            metadata["application_no"] = application_no_hint.strip()[:100]
        if metadata.get("application_no"):
            existing = _latest_application_report(db, metadata["application_no"])
            if existing and existing.id != report.id:
                _safe_remove_file(report.file_path, get_settings().uploads_dir)
                db.rollback()
                raise HTTPException(status_code=409, detail=_application_exists_detail(existing))
        _apply_metadata(db, report, metadata)

        ocr_dir = Path(get_settings().ocr_dir) / f"report_{report.id}"
        ocr_dir.mkdir(parents=True, exist_ok=True)
        report.ocr_path = str(ocr_dir)
        write_progress(ocr_dir, "queued", 0, 0, "已进入审核队列")

        from backend.app.settings_store import get_section
        if get_section("workflow", db).get("auto_review", True):
            db.add(models.Task(report_id=report.id, status="queued", attempt=0))
        add_audit(
            db, current, "报告", "上传报告",
            f"文件：{report.original_filename or '-'}，SHA256：{file_hash[:12]}…，报告 ID：{report.id}", request,
        )
        db.commit()
        db.refresh(report)
        return report


@router.post("/{report_id}/correction-version", response_model=schemas.ReportOut)
def upload_correction_version(
    report_id: int,
    request: Request,
    file: UploadFile = File(...),
    revision_note: str = Form(""),
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("reports.upload")),
):
    """上传实验室更正后的 PDF，作为原报告版本链中的下一版并自动审核。"""
    parent = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not parent:
        raise HTTPException(status_code=404, detail="原报告不存在")
    from backend.app.browser_intake import guard_existing_scope
    guard_existing_scope(db, parent, file)
    if parent.status != "done" or not parent.result_json:
        raise HTTPException(status_code=409, detail="原报告尚未完成审核，不能上传更正版本")
    root_id = parent.root_report_id or parent.id
    latest = db.query(models.Report).filter(
        models.Report.root_report_id == root_id
    ).order_by(models.Report.revision_no.desc(), models.Report.id.desc()).first()
    if latest and latest.id != parent.id:
        raise HTTPException(status_code=409, detail=f"当前不是最新版本，请在 V{latest.revision_no or 1} 上继续上传")
    _validate_upload(file, db)
    file_hash = _upload_sha256(file)
    with _UPLOAD_LOCK:
        duplicate = db.query(models.Report).filter(models.Report.file_sha256 == file_hash).first()
        if duplicate:
            raise HTTPException(status_code=409, detail=_duplicate_detail(duplicate))

        revision_no = int(parent.revision_no or 1) + 1
        report = models.Report(
            status="pending", file_path="", ocr_path="",
            report_no=parent.report_no, application_no=parent.application_no,
            company_id=parent.company_id, product_unit=parent.product_unit,
            product_desc=parent.product_desc, root_report_id=root_id,
            parent_report_id=parent.id, revision_no=revision_no,
            revision_note=(revision_note or "").strip() or None,
            original_filename=_original_filename(file), file_sha256=file_hash,
            uploaded_by_id=current.id,
        )
        db.add(report)
        db.flush()
        report.file_path = _save_upload(file, report.id)

        metadata: dict[str, str] = {}
        try:
            metadata = _extract_upload_metadata(report.file_path)
        except Exception:
            pass
        if (
            metadata.get("application_no") and parent.application_no
            and metadata["application_no"] != parent.application_no
        ):
            _safe_remove_file(report.file_path, get_settings().uploads_dir)
            db.rollback()
            raise HTTPException(status_code=409, detail={
                "code": "application_mismatch",
                "message": (
                    f"新版申请编号 {metadata['application_no']} 与原报告 {parent.application_no} 不一致，"
                    "不能加入同一版本链"
                ),
                "report_id": parent.id,
            })
        _apply_metadata(db, report, metadata)

        ocr_dir = Path(get_settings().ocr_dir) / f"report_{report.id}"
        ocr_dir.mkdir(parents=True, exist_ok=True)
        report.ocr_path = str(ocr_dir)
        write_progress(ocr_dir, "queued", 0, 0, f"更正版本 V{revision_no} 已进入审核队列")
        from backend.app.settings_store import get_section
        if get_section("workflow", db).get("auto_review", True):
            db.add(models.Task(report_id=report.id, status="queued", attempt=0))
        add_audit(
            db, current, "报告", "上传更正版本",
            f"原报告 ID：{parent.id}，新报告 ID：{report.id}，版本：V{revision_no}，"
            f"文件：{report.original_filename or '-'}，说明：{report.revision_note or '-'}",
            request,
        )
        db.commit()
        db.refresh(report)
        return report


@router.post("/{report_id}/independent-category", response_model=schemas.ReportOut)
def upload_independent_category(
    report_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("reports.upload")),
):
    """将类别不一致的文件按原申请和任务创建为独立类别报告链。"""
    source_report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not source_report:
        raise HTTPException(status_code=404, detail="来源报告不存在")
    from backend.app.browser_intake import create_independent_category
    return create_independent_category(request, file, source_report, db, current)


@router.get("/{report_id}/versions", response_model=list[schemas.ReportVersionOut])
def list_report_versions(
    report_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.view")),
):
    """返回报告所属版本链的完整时间线。"""
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    root_id = report.root_report_id or report.id
    return db.query(models.Report).options(
        selectinload(models.Report.company), selectinload(models.Report.tasks)
    ).filter(models.Report.root_report_id == root_id).order_by(
        models.Report.revision_no.asc(), models.Report.id.asc()
    ).all()


@router.get("", response_model=schemas.ReportList)
def list_reports(
    q: Optional[str] = Query(None),
    product_unit: Optional[str] = Query(None),
    company: Optional[str] = Query(None),
    uploader_id: Optional[int] = Query(None, ge=0),
    status: Optional[str] = Query(None),
    conclusion: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    include_versions: bool = Query(False),
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.view")),
):
    """报告列表，支持分页与筛选。q模糊匹配申请号、报告号或企业名。"""
    query = db.query(models.Report).options(
        selectinload(models.Report.company),
        selectinload(models.Report.uploader),
        selectinload(models.Report.tasks),
    )
    if not include_versions:
        child = aliased(models.Report)
        query = query.filter(~db.query(child.id).filter(child.parent_report_id == models.Report.id).exists())
    # 兼容历史上重复上传的完全相同文件：保留数据，但列表只展示最新一条。
    same_file = aliased(models.Report)
    query = query.filter(or_(
        models.Report.file_sha256.is_(None),
        ~db.query(same_file.id).filter(
            same_file.file_sha256 == models.Report.file_sha256,
            same_file.id > models.Report.id,
        ).exists(),
    ))
    if status:
        query = query.filter(models.Report.status == status)
    if conclusion:
        query = query.filter(models.Report.conclusion.ilike(f"%{conclusion}%"))
    if start:
        query = query.filter(models.Report.created_at >= start)
    if end:
        query = query.filter(models.Report.created_at < end)
    if product_unit:
        query = query.filter(models.Report.product_unit == product_unit)
    if uploader_id is not None:
        query = query.filter(
            models.Report.uploaded_by_id.is_(None) if uploader_id == 0
            else models.Report.uploaded_by_id == uploader_id
        )
    if company or q:
        query = query.outerjoin(models.Company)
    if company:
        query = query.filter(models.Company.name.ilike(f"%{company}%"))
    if q:
        query = query.filter(or_(
            models.Report.application_no.ilike(f"%{q}%"),
            models.Report.report_no.ilike(f"%{q}%"),
            models.Company.name.ilike(f"%{q}%"),
        ))
    total = query.count()
    items = query.order_by(models.Report.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": items}


@router.get("/uploaders", response_model=list[schemas.ReportUploaderOption])
def list_report_uploaders(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.view")),
):
    """返回报告列表可用的上传人选项，不要求用户管理权限。"""
    rows = db.query(models.User.id, models.User.username).join(
        models.Report, models.Report.uploaded_by_id == models.User.id
    ).distinct().order_by(models.User.username).all()
    return [{"id": row.id, "username": row.username} for row in rows]


@router.get("/{report_id}", response_model=schemas.ReportOut)
def get_report(
    report_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.view")),
):
    report = db.query(models.Report).options(
        selectinload(models.Report.company),
        selectinload(models.Report.uploader),
        selectinload(models.Report.tasks),
    ).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    return report


@router.get("/{report_id}/status", response_model=schemas.ReportStatus)
def report_status(
    report_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.view")),
):
    """查询审核进度。"""
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    task = db.query(models.Task).filter(models.Task.report_id == report_id).order_by(models.Task.id.desc()).first()
    progress = read_progress(report.ocr_path)
    return {
        "report_id": report.id,
        "status": report.status,
        "conclusion": report.conclusion,
        "error_msg": report.error_msg,
        "updated_at": report.updated_at,
        "task_status": task.status if task else None,
        "attempt": task.attempt if task else None,
        "stage": progress.get("stage"),
        "progress_current": progress.get("current", 0),
        "progress_total": progress.get("total", 0),
        "progress_percent": progress.get("percent", 0),
        "progress_message": progress.get("message", ""),
        "application_no": report.application_no,
        "report_no": report.report_no,
        "company_name": report.company.name if report.company else None,
        "product_unit": report.product_unit,
    }


@router.get("/{report_id}/review", response_model=schemas.ReviewJsonOut)
def get_review_json(
    report_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.download")),
):
    """获取最新审核意见书JSON。"""
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    latest = db.query(models.ReviewReport).filter(models.ReviewReport.report_id == report_id).order_by(
        models.ReviewReport.id.desc()).first()
    if not latest:
        raise HTTPException(status_code=404, detail="尚未生成审核意见书")
    content = latest.content_json
    markdown = build_markdown(content)
    return {
        "report_id": report.id,
        "version": latest.version,
        "conclusion": report.conclusion,
        "content": content,
        "markdown": markdown,
    }


@router.get("/{report_id}/download")
def download_review(
    report_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.download")),
):
    """下载审核意见书Markdown。"""
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    latest = db.query(models.ReviewReport).filter(models.ReviewReport.report_id == report_id).order_by(
        models.ReviewReport.id.desc()).first()
    if not latest:
        raise HTTPException(status_code=404, detail="尚未生成审核意见书")
    markdown = build_markdown(latest.content_json)
    # 使用ASCII文件名避免部分客户端/测试库编码问题；内容仍保持中文Markdown
    filename = f"review_report_{report.id}.md"
    return PlainTextResponse(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


def _pdf_navigation_page_count(file_path: str) -> int | None:
    """Count the actual PDF pages; unknown must not enable page navigation."""
    try:
        import fitz
        with fitz.open(file_path) as document:
            if not document.is_pdf or document.needs_pass:
                return None
            count = document.page_count
            return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else None
    except Exception:
        # Preserve the existing raw-file preview even when metadata cannot be read.
        return None


@router.get("/{report_id}/pdf")
def get_report_pdf(
    report_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.view")),
):
    """返回上传的原始PDF文件，文件不存在返回404。"""
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    if not report.file_path or not Path(report.file_path).is_file():
        raise HTTPException(status_code=404, detail="PDF文件不存在")
    page_count = _pdf_navigation_page_count(report.file_path)
    headers = {"X-PDF-Page-Count": str(page_count)} if page_count is not None else {}
    return FileResponse(report.file_path, media_type="application/pdf", headers=headers)


@router.post("/{report_id}/re-review")
def re_review(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("reports.review")),
):
    """重新审核（admin/reviewer）。"""
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    from backend.app.browser_intake import guard_existing_scope
    guard_existing_scope(db, report)
    from backend.app.settings_store import get_section
    if not get_section("workflow", db).get("allow_re_review", True):
        raise HTTPException(status_code=403, detail="系统已关闭重新审核功能")
    # 重置状态
    report.status = "pending"
    report.conclusion = None
    report.error_msg = None
    report.result_json = None
    write_progress(report.ocr_path, "queued", 0, 0, "已提交重新审核，将复用已完成的 OCR")
    # 新建任务
    task = models.Task(report_id=report.id, status="queued", attempt=0)
    db.add(task)
    add_audit(db, current, "报告", "重新审核", f"报告 ID：{report.id}", request)
    db.commit()
    return {"report_id": report.id, "task_id": task.id}


@router.delete("/{report_id}")
def delete_report(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("reports.delete")),
):
    """删除报告及其关联数据、磁盘文件（仅admin）。"""
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    child = db.query(models.Report).filter(models.Report.parent_report_id == report_id).first()
    if child:
        raise HTTPException(
            status_code=409,
            detail=f"该报告已有更正版本 V{child.revision_no or 1}，为保留版本链，请先删除最新版本",
        )
    file_path, ocr_path = report.file_path, report.ocr_path
    # 删除关联表数据
    correction_ids = [row[0] for row in db.query(models.ReviewCorrection.id).filter(models.ReviewCorrection.report_id == report_id).all()]
    if correction_ids:
        db.query(models.ReviewRule).filter(models.ReviewRule.source_correction_id.in_(correction_ids)).update(
            {"source_correction_id": None}, synchronize_session=False
        )
    db.query(models.RegressionCase).filter(models.RegressionCase.report_id == report_id).delete()
    db.query(models.ReviewCorrection).filter(models.ReviewCorrection.report_id == report_id).delete()
    db.query(models.ReviewReport).filter(models.ReviewReport.report_id == report_id).delete()
    db.query(models.ReviewItem).filter(models.ReviewItem.report_id == report_id).delete()
    db.query(models.Task).filter(models.Task.report_id == report_id).delete()
    db.delete(report)
    add_audit(db, current, "报告", "删除报告", f"报告 ID：{report_id}", request)
    db.commit()
    # 删除磁盘上的上传文件和OCR目录（带路径校验，只删配置目录内的）
    settings = get_settings()
    _safe_remove_file(file_path, settings.uploads_dir)
    _safe_remove_dir(ocr_path, settings.ocr_dir)
    return {"ok": True}
