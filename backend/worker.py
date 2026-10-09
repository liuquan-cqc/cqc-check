"""独立审核worker：数据库轮询 + 线程池并发 + 失败重试。"""
import os
import sys
import time
import argparse
import logging
import shutil
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 把项目根目录加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app import models, schemas
from backend.app.database import SessionLocal, init_db
from backend.app.config import get_settings
from backend.app.review import review_report, build_markdown
from backend.app.extract import extract_report_info
from backend.app.progress import write_progress

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("worker")

settings = get_settings()
MAX_WORKERS = int(os.environ.get("WORKER_CONCURRENCY", 3))
MAX_RETRY = 2
POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", 5))
_last_cleanup_at = 0.0


def _workflow_settings():
    from backend.app.settings_store import get_section
    return get_section("workflow")


def _save_review_report(db, report: models.Report, result: dict):
    """保存JSON结果并生成Markdown版本记录。"""
    report.result_json = result
    report.conclusion = result.get("conclusion", "")
    report.samples_json = result.get("samples")

    # 版本号递增
    last = db.query(models.ReviewReport).filter(
        models.ReviewReport.report_id == report.id).order_by(
        models.ReviewReport.id.desc()).first()
    version = (last.version + 1) if last else 1
    review_report_record = models.ReviewReport(
        report_id=report.id,
        version=version,
        content_json=result,
    )
    db.add(review_report_record)

    # 清除旧的审核明细，保留版本历史记录
    db.query(models.ReviewItem).filter(models.ReviewItem.report_id == report.id).delete(
        synchronize_session=False
    )

    # 保存review_items明细
    for sample_idx, sample in enumerate(result.get("samples", [])):
        for item in sample.get("items", []):
            ri = models.ReviewItem(
                report_id=report.id,
                sample_idx=sample_idx,
                item_name=item.get("item", ""),
                reported_value=str(item.get("reported", "")),
                standard_value=str(item.get("should_be", "")),
                standard_ref=item.get("standard", ""),
                status=item.get("severity", "ok"),
                remark="",
            )
            db.add(ri)

    # 同步回填申请编号、企业、报告编号和产品单元
    if result.get("company"):
        company_name = result.get("company")
        company = db.query(models.Company).filter(models.Company.name == company_name).first()
        if not company:
            company = models.Company(name=company_name)
            db.add(company)
            db.flush()
        report.company_id = company.id
    if result.get("application_no"):
        report.application_no = result.get("application_no")
    if result.get("report_no"):
        report.report_no = result.get("report_no")
    if result.get("product_unit"):
        report.product_unit = result.get("product_unit")
    if result.get("product_desc"):
        report.product_desc = result.get("product_desc")

    report.status = "done"
    db.commit()


def _process_task(task_id: int) -> bool:
    """处理单个任务，返回是否成功。"""
    db = SessionLocal()
    try:
        task = db.query(models.Task).filter(models.Task.id == task_id).with_for_update().first()
        if not task or task.status != "queued":
            return False
        task.status = "running"
        task.started_at = models._utc_now()
        task.attempt += 1
        db.commit()

        report = db.query(models.Report).filter(models.Report.id == task.report_id).first()
        if not report:
            task.status = "failed"
            task.finished_at = models._utc_now()
            db.commit()
            return False

        def update_progress(stage: str, current: int, total: int, message: str) -> None:
            write_progress(report.ocr_path, stage, current, total, message)
            report.status = "reviewing" if stage in ("reviewing", "finalizing") else "extracting"
            report.error_msg = None
            db.commit()

        def update_metadata(metadata: dict[str, str]) -> None:
            if metadata.get("application_no"):
                report.application_no = metadata["application_no"]
            if metadata.get("report_no"):
                report.report_no = metadata["report_no"]
            if metadata.get("product_unit"):
                report.product_unit = metadata["product_unit"]
            company_name = metadata.get("company")
            if company_name:
                company = db.query(models.Company).filter(models.Company.name == company_name).first()
                if not company:
                    company = models.Company(name=company_name)
                    db.add(company)
                    db.flush()
                report.company_id = company.id
            db.commit()

        # 执行审核（extracting -> reviewing -> done）
        parent_review = None
        if report.parent_report_id:
            parent = db.query(models.Report).filter(models.Report.id == report.parent_report_id).first()
            if parent:
                parent_review = parent.result_json
        result = review_report(
            report.id,
            report.file_path,
            report.ocr_path,
            progress=update_progress,
            metadata_callback=update_metadata,
            task_id=task.id,
            parent_report_id=report.parent_report_id,
            parent_review=parent_review,
        )
        _save_review_report(db, report, result)
        write_progress(report.ocr_path, "done", 1, 1, "审核完成")

        task.status = "done"
        task.finished_at = models._utc_now()
        db.commit()
        logger.info(f"报告 {report.id} 审核完成：{report.conclusion}")
        try:
            from backend.app.notifications import send_review_notification
            send_review_notification(report, success=True)
        except Exception:
            logger.exception("发送审核完成通知失败")
        return True
    except Exception as e:
        logger.exception(f"任务 {task_id} 处理失败")
        try:
            task = db.query(models.Task).filter(models.Task.id == task_id).first()
            if task:
                task.status = "failed"
                task.finished_at = models._utc_now()
            report = db.query(models.Report).filter(models.Report.id == task.report_id).first()
            if report:
                report.status = "failed"
                report.error_msg = str(e)
                write_progress(report.ocr_path, "failed", 0, 0, f"审核失败：{e}")
            db.commit()
            try:
                from backend.app.notifications import send_review_notification
                if report:
                    send_review_notification(report, success=False, error=str(e))
            except Exception:
                logger.exception("发送审核失败通知失败")
        except Exception:
            pass
        return False
    finally:
        db.close()


def _reschedule_failed():
    """将失败且未超过重试次数的任务重新置为queued。"""
    db = SessionLocal()
    try:
        max_retry = int(_workflow_settings().get("max_retries", MAX_RETRY))
        failed_tasks = db.query(models.Task).filter(
            models.Task.status == "failed",
            models.Task.attempt < max_retry + 1,
        ).all()
        for task in failed_tasks:
            if task.attempt <= max_retry:
                task.status = "queued"
                task.started_at = None
                task.finished_at = None
                report = db.query(models.Report).filter(models.Report.id == task.report_id).first()
                if report:
                    report.status = "pending"
                    report.error_msg = None
        db.commit()
        return len(failed_tasks)
    finally:
        db.close()


def _fetch_queued(limit: int, excluded: set[int] | None = None) -> list[int]:
    """获取待处理任务ID。"""
    db = SessionLocal()
    try:
        query = db.query(models.Task).filter(models.Task.status == "queued")
        if excluded:
            query = query.filter(~models.Task.id.in_(excluded))
        tasks = query.order_by(models.Task.id.asc()).limit(limit).all()
        return [t.id for t in tasks]
    finally:
        db.close()


def _recover_interrupted_tasks() -> int:
    """Worker 重启时恢复上次被中断的任务，不消耗失败重试次数。"""
    db = SessionLocal()
    try:
        tasks = db.query(models.Task).filter(models.Task.status == "running").all()
        for task in tasks:
            task.status = "queued"
            task.started_at = None
            task.finished_at = None
            task.attempt = max(0, int(task.attempt or 0) - 1)
            report = db.query(models.Report).filter(models.Report.id == task.report_id).first()
            if report:
                report.status = "pending"
                report.error_msg = None
                write_progress(report.ocr_path, "queued", 0, 0, "Worker 重启，已恢复中断的任务")
        db.commit()
        return len(tasks)
    finally:
        db.close()


def _safe_delete_path(path_value: str, base_value: str, directory: bool = False) -> None:
    if not path_value:
        return
    target = Path(path_value).resolve()
    base = Path(base_value).resolve()
    if not str(target).startswith(str(base) + os.sep):
        return
    if directory and target.is_dir():
        shutil.rmtree(target)
    elif not directory and target.is_file():
        target.unlink()


def _cleanup_expired_reports() -> int:
    """按文件设置清理过期报告。默认 retention_days=0，绝不会自动删除。"""
    global _last_cleanup_at
    now_mono = time.monotonic()
    if now_mono - _last_cleanup_at < 3600:
        return 0
    _last_cleanup_at = now_mono
    from backend.app.settings_store import get_section
    file_config = get_section("files")
    retention_days = int(file_config.get("retention_days", 0))
    if retention_days <= 0:
        return 0

    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    db = SessionLocal()
    deleted = 0
    try:
        expired = db.query(models.Report).filter(models.Report.created_at < cutoff).limit(200).all()
        for report in expired:
            file_path, ocr_path = report.file_path, report.ocr_path
            db.query(models.ReviewReport).filter(models.ReviewReport.report_id == report.id).delete()
            db.query(models.ReviewItem).filter(models.ReviewItem.report_id == report.id).delete()
            db.query(models.Task).filter(models.Task.report_id == report.id).delete()
            db.delete(report)
            db.flush()
            _safe_delete_path(file_path, settings.uploads_dir)
            _safe_delete_path(ocr_path, settings.ocr_dir, directory=True)
            deleted += 1
        if deleted:
            db.add(models.AuditLog(username="system", module="文件", action="清理过期报告", detail=f"删除 {deleted} 条超过 {retention_days} 天的报告"))
        db.commit()
        return deleted
    finally:
        db.close()


def run_once():
    """执行一轮：重试失败 + 处理新任务。"""
    init_db()
    _cleanup_expired_reports()
    _reschedule_failed()
    workflow = _workflow_settings()
    max_workers = int(workflow.get("worker_concurrency", MAX_WORKERS))
    task_ids = _fetch_queued(max_workers * 2)
    if not task_ids:
        return 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_task, tid): tid for tid in task_ids}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                future.result()
            except Exception:
                logger.exception(f"任务 {tid} 异常")
    return len(task_ids)


def run_loop():
    """持续轮询：长驻线程池可在旧任务运行时继续接收新任务。"""
    init_db()
    recovered = _recover_interrupted_tasks()
    logger.info(f"Worker启动，运行参数由系统设置动态加载，恢复任务 {recovered} 个")
    active: dict = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        while True:
            try:
                _cleanup_expired_reports()
                _reschedule_failed()

                for future in list(active):
                    if future.done():
                        task_id = active.pop(future)
                        try:
                            future.result()
                        except Exception:
                            logger.exception(f"任务 {task_id} 异常")

                workflow = _workflow_settings()
                desired = max(1, min(16, int(workflow.get("worker_concurrency", MAX_WORKERS))))
                capacity = max(0, desired - len(active))
                if capacity:
                    active_ids = set(active.values())
                    for task_id in _fetch_queued(capacity, active_ids):
                        active[executor.submit(_process_task, task_id)] = task_id
            except Exception:
                logger.exception("Worker轮询异常")
            poll_interval = int(_workflow_settings().get("poll_interval_seconds", POLL_INTERVAL))
            time.sleep(poll_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CCC审核Worker")
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    args = parser.parse_args()
    if args.once:
        count = run_once()
        print(f"处理任务数: {count}")
    else:
        run_loop()
