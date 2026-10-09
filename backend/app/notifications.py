"""审核结果通知。由 Worker 调用，通知失败只记录日志，不影响审核结果。"""
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage

import httpx

from backend.app.settings_store import get_section


def send_review_notification(report, success: bool, error: str = "") -> None:
    config = get_section("notifications")
    event_enabled = config.get("notify_on_done", True) if success else config.get("notify_on_failed", True)
    if not event_enabled:
        return

    status_text = "审核完成" if success else "审核失败"
    identifier = report.application_no or report.report_no or report.id
    subject = f"[CCC审核] {status_text} - {identifier}"
    body = f"申请：{identifier}\n状态：{status_text}\n结论：{report.conclusion or '-'}"
    if error:
        body += f"\n错误：{error}"

    if config.get("email_enabled") and config.get("smtp_host") and config.get("recipient_email"):
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = config.get("sender_email") or config.get("smtp_username")
        message["To"] = config["recipient_email"]
        message.set_content(body)
        smtp_cls = smtplib.SMTP_SSL if config.get("smtp_ssl", True) else smtplib.SMTP
        with smtp_cls(config["smtp_host"], int(config.get("smtp_port", 465)), timeout=15) as server:
            if config.get("smtp_username"):
                server.login(config["smtp_username"], config.get("smtp_password", ""))
            server.send_message(message)

    if config.get("webhook_enabled") and config.get("webhook_url"):
        payload = {
            "event": "review.completed" if success else "review.failed",
            "report_id": report.id,
            "report_no": report.report_no,
            "application_no": report.application_no,
            "status": report.status,
            "conclusion": report.conclusion,
            "error": error,
        }
        response = httpx.post(config["webhook_url"], json=payload, timeout=15)
        response.raise_for_status()
