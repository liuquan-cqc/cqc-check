"""Pydantic 请求/响应模型。"""
from typing import Optional, List
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_serializer


class UtcResponseModel(BaseModel):
    """数据库使用无偏移 UTC；API 输出时补上明确的 UTC 标记。"""

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_datetime_as_utc(self, value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value


class CompanyOut(UtcResponseModel):
    id: int
    name: str
    address: Optional[str] = None
    unified_social_credit_code: Optional[str] = None
    operating_status: str = "unknown"
    status_source: Optional[str] = None
    status_checked_at: Optional[datetime] = None
    created_at: datetime
    report_count: int = 0
    last_review_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CompanyIn(BaseModel):
    """企业创建/更新入参。"""
    name: Optional[str] = None
    address: Optional[str] = None
    unified_social_credit_code: Optional[str] = None
    operating_status: Optional[str] = None
    status_source: Optional[str] = None


class UserOut(UtcResponseModel):
    id: int
    username: str
    role: str
    permissions_override: Optional[list[str]] = None
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    user: UserOut


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1)
    role: str = "viewer"
    permissions_override: Optional[list[str]] = None


class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    permissions_override: Optional[list[str]] = None
    status: Optional[str] = None


class ReportCreate(BaseModel):
    report_no: Optional[str] = None
    application_no: Optional[str] = None
    company_id: Optional[int] = None
    product_desc: Optional[str] = None


class ReportSimple(UtcResponseModel):
    id: int
    report_no: Optional[str] = None
    application_no: Optional[str] = None
    original_filename: Optional[str] = None
    file_sha256: Optional[str] = None
    company_id: Optional[int] = None
    uploaded_by_id: Optional[int] = None
    product_unit: Optional[str] = None
    product_desc: Optional[str] = None
    status: str
    conclusion: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    review_completed_at: Optional[datetime] = None
    root_report_id: Optional[int] = None
    parent_report_id: Optional[int] = None
    revision_no: int = 1
    revision_note: Optional[str] = None

    class Config:
        from_attributes = True


class ReportOut(ReportSimple):
    company: Optional[CompanyOut] = None
    uploader: Optional[UserOut] = None
    result_json: Optional[dict] = None
    error_msg: Optional[str] = None
    samples_json: Optional[List[dict]] = None


class ReviewItemOut(BaseModel):
    id: int
    sample_idx: int
    item_name: str
    reported_value: Optional[str] = None
    standard_value: Optional[str] = None
    standard_ref: Optional[str] = None
    status: str
    remark: Optional[str] = None

    class Config:
        from_attributes = True


class ReviewReportOut(UtcResponseModel):
    id: int
    version: int
    content_json: dict
    created_at: datetime

    class Config:
        from_attributes = True


class ReportStatus(UtcResponseModel):
    report_id: int
    status: str
    conclusion: Optional[str] = None
    error_msg: Optional[str] = None
    updated_at: datetime
    task_status: Optional[str] = None
    attempt: Optional[int] = None
    stage: Optional[str] = None
    progress_current: int = 0
    progress_total: int = 0
    progress_percent: int = 0
    progress_message: str = ""
    application_no: Optional[str] = None
    report_no: Optional[str] = None
    company_name: Optional[str] = None
    product_unit: Optional[str] = None


class ReviewJsonOut(BaseModel):
    report_id: int
    version: int
    conclusion: Optional[str] = None
    content: dict
    markdown: str


class ReportList(BaseModel):
    total: int
    items: List[ReportOut]


class ReportUploaderOption(BaseModel):
    id: int
    username: str


class ReportVersionOut(ReportSimple):
    """版本时间线中的精简报告信息。"""
    company: Optional[CompanyOut] = None
