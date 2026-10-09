"""数据库模型。"""
import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from backend.app.database import Base


def _utc_now() -> datetime.datetime:
    """返回无UTC偏移的当前UTC时间（兼容数据库存储）。"""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class Company(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    address = Column(String(500), nullable=True)
    unified_social_credit_code = Column(String(32), nullable=True, index=True)
    # 工商状态必须带核验来源；无依据时保持 unknown，不由 AI 猜测。
    operating_status = Column(String(30), nullable=False, default="unknown", index=True)
    status_source = Column(String(500), nullable=True)
    status_checked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now)

    reports = relationship("Report", back_populates="company")


class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True, index=True)
    report_no = Column(String(100), nullable=True, index=True)
    application_no = Column(String(100), nullable=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    uploaded_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    original_filename = Column(String(500), nullable=True)
    file_sha256 = Column(String(64), nullable=True, index=True)
    file_path = Column(String(500), nullable=False)
    ocr_path = Column(String(500), nullable=True)
    product_unit = Column(String(255), nullable=True)
    product_desc = Column(Text, nullable=True)
    status = Column(String(20), default="pending", index=True)
    samples_json = Column(JSON, nullable=True)
    result_json = Column(JSON, nullable=True)
    conclusion = Column(String(100), nullable=True)
    error_msg = Column(Text, nullable=True)
    # 实验室更正版本链。ReviewReport.version 仍只表示同一 PDF 的审核次数。
    root_report_id = Column(Integer, ForeignKey("reports.id"), nullable=True, index=True)
    parent_report_id = Column(Integer, ForeignKey("reports.id"), nullable=True, index=True)
    revision_no = Column(Integer, nullable=False, default=1, index=True)
    revision_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utc_now)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)

    company = relationship("Company", back_populates="reports")
    uploader = relationship("User", foreign_keys=[uploaded_by_id])
    tasks = relationship("Task", back_populates="report", order_by="Task.created_at")
    review_items = relationship("ReviewItem", back_populates="report")
    review_reports = relationship("ReviewReport", back_populates="report")
    trial_review = relationship(
        "TrialReviewRecord", back_populates="report", uselist=False,
        cascade="all, delete-orphan",
    )
    parent_report = relationship(
        "Report", remote_side=[id], foreign_keys=[parent_report_id], back_populates="correction_versions"
    )
    correction_versions = relationship(
        "Report", foreign_keys=[parent_report_id], back_populates="parent_report", order_by="Report.revision_no"
    )

    @property
    def review_completed_at(self):
        """最近一次成功审核的完成时间；处理中或失败时不冒充为完成时间。"""
        if self.status != "done":
            return None
        finished = [task.finished_at for task in self.tasks if task.status == "done" and task.finished_at]
        return max(finished) if finished else None


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False, index=True)
    status = Column(String(20), default="queued", index=True)
    attempt = Column(Integer, default=0)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now)

    report = relationship("Report", back_populates="tasks")


class ReviewItem(Base):
    __tablename__ = "review_items"
    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False, index=True)
    sample_idx = Column(Integer, default=0, nullable=True)
    item_name = Column(String(255), nullable=False)
    reported_value = Column(Text, nullable=True)
    standard_value = Column(Text, nullable=True)
    standard_ref = Column(String(255), nullable=True)
    status = Column(String(20), default="ok")
    remark = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utc_now)

    report = relationship("Report", back_populates="review_items")


class User(Base):
    """系统用户。permissions_override 为空时继承角色权限，否则使用用户专属权限。"""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="viewer")
    permissions_override = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime, default=_utc_now)


class ReviewReport(Base):
    __tablename__ = "review_reports"
    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False, index=True)
    version = Column(Integer, default=1)
    content_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=_utc_now)

    report = relationship("Report", back_populates="review_reports")


class TrialReviewRecord(Base):
    """8089试运行期的强制人工终审及30份晋级证据。"""
    __tablename__ = "trial_review_records"
    __table_args__ = (
        UniqueConstraint("sequence_no", name="uq_trial_review_sequence_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False, unique=True, index=True)
    sequence_no = Column(Integer, nullable=False, index=True)
    human_status = Column(String(20), nullable=False, default="pending", index=True)
    final_decision = Column(String(20), nullable=True)
    severe_miss_detected = Column(Boolean, nullable=False, default=False)
    sensitive_info_externalized = Column(Boolean, nullable=False, default=False)
    incorrect_auto_release = Column(Boolean, nullable=False, default=False)
    safety_evidence_json = Column(JSON, nullable=False, default=dict)
    review_note = Column(Text, nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now, index=True)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)

    report = relationship("Report", back_populates="trial_review")
    reviewer = relationship("User", foreign_keys=[reviewed_by])


class SystemSetting(Base):
    """按功能分组保存系统设置。敏感字段只通过专用接口写入，不向前端回传明文。"""
    __tablename__ = "system_settings"
    section = Column(String(50), primary_key=True)
    value_json = Column(JSON, nullable=False, default=dict)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)


class AuditLog(Base):
    """管理员操作审计日志。保留用户名快照，避免用户删除后日志失去主体。"""
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    username = Column(String(100), nullable=False, default="system")
    module = Column(String(50), nullable=False, index=True)
    action = Column(String(100), nullable=False)
    detail = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    success = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_utc_now, index=True)


class BrowserSyncDevice(Base):
    """Edge辅助程序设备；只保存同步令牌哈希，不保存单位系统登录信息。"""
    __tablename__ = "browser_sync_devices"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="active", index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now, index=True)

    creator = relationship("User")


class CompanyVerificationTask(Base):
    """由已登录 Edge 页面执行的企业状态核验队列；不保存网站 Cookie。"""
    __tablename__ = "company_verification_tasks"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    device_id = Column(Integer, ForeignKey("browser_sync_devices.id"), nullable=True, index=True)
    status = Column(String(20), nullable=False, default="queued", index=True)
    raw_status = Column(String(100), nullable=True)
    source_url = Column(String(1000), nullable=True)
    error_msg = Column(Text, nullable=True)
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_utc_now, index=True)
    started_at = Column(DateTime, nullable=True, index=True)
    finished_at = Column(DateTime, nullable=True)

    company = relationship("Company")
    device = relationship("BrowserSyncDevice")
    requester = relationship("User")


class ReviewCorrection(Base):
    """审核员针对 AI 结果提交的人工纠错，保留原值与正确值快照。"""
    __tablename__ = "review_corrections"
    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False, index=True)
    correction_type = Column(String(40), nullable=False, index=True)
    original_json = Column(JSON, nullable=True)
    corrected_json = Column(JSON, nullable=False, default=dict)
    description = Column(Text, nullable=False)
    standard_ref = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    review_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utc_now, index=True)
    reviewed_at = Column(DateTime, nullable=True)

    report = relationship("Report")
    submitter = relationship("User", foreign_keys=[submitted_by])
    reviewer = relationship("User", foreign_keys=[reviewed_by])


class ReviewRule(Base):
    """由已确认纠错沉淀出的结构化审核规则。"""
    __tablename__ = "review_rules"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    category = Column(String(60), nullable=False, default="其他")
    product_unit = Column(String(255), nullable=True, index=True)
    applicable_conditions = Column(Text, nullable=True)
    rule_text = Column(Text, nullable=False)
    standard_ref = Column(Text, nullable=True)
    source_correction_id = Column(Integer, ForeignKey("review_corrections.id"), nullable=True)
    status = Column(String(20), nullable=False, default="draft", index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=_utc_now, index=True)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)


class ReviewVersion(Base):
    """一次发布的规则快照；切换活动版本即可安全回退。"""
    __tablename__ = "review_versions"
    id = Column(Integer, primary_key=True, index=True)
    version_no = Column(Integer, nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="draft", index=True)
    rules_json = Column(JSON, nullable=False, default=list)
    metrics_json = Column(JSON, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    published_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=_utc_now, index=True)
    published_at = Column(DateTime, nullable=True)


class MatrixConditionRule(Base):
    """型号矩阵 conditional 行的可执行三态条件；按版本维护，不覆盖原始证据文本。"""
    __tablename__ = "matrix_condition_rules"
    __table_args__ = (
        UniqueConstraint("matrix_id", "version_no", name="uq_matrix_condition_rule_version"),
    )

    id = Column(Integer, primary_key=True, index=True)
    matrix_id = Column(Integer, nullable=False, index=True)
    condition_json = Column(JSON, nullable=False, default=dict)
    evidence_ref = Column(Text, nullable=True)
    verification_status = Column(String(20), nullable=False, default="unverified", index=True)
    enabled_for_review = Column(Boolean, nullable=False, default=False, index=True)
    version_no = Column(Integer, nullable=False, default=1)
    source_review_rule_id = Column(Integer, ForeignKey("review_rules.id"), nullable=True, index=True)
    created_by = Column(String(100), nullable=False, default="system")
    created_at = Column(DateTime, default=_utc_now, index=True)


class ExecutableReviewRule(Base):
    """跨型号通用审核规则登记；候选默认关闭，验证后才允许进入运行链。"""
    __tablename__ = "executable_review_rules"
    __table_args__ = (
        UniqueConstraint("rule_code", "version_no", name="uq_executable_review_rule_version"),
    )

    id = Column(Integer, primary_key=True, index=True)
    rule_code = Column(String(120), nullable=False, index=True)
    rule_type = Column(String(60), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    scope_json = Column(JSON, nullable=False, default=dict)
    config_json = Column(JSON, nullable=False, default=dict)
    evidence_ref = Column(Text, nullable=True)
    verification_status = Column(String(20), nullable=False, default="unverified", index=True)
    enabled_for_review = Column(Boolean, nullable=False, default=False, index=True)
    source_review_rule_id = Column(Integer, ForeignKey("review_rules.id"), nullable=True, index=True)
    version_no = Column(Integer, nullable=False, default=1)
    created_by = Column(String(100), nullable=False, default="system")
    created_at = Column(DateTime, default=_utc_now, index=True)


class RegressionCase(Base):
    """人工确认的回归测试样本及期望结论。"""
    __tablename__ = "regression_cases"
    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    expected_conclusion = Column(String(100), nullable=False)
    expected_issue_count = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True)
    notes = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_utc_now, index=True)

    report = relationship("Report")


class RegressionRun(Base):
    """一次回归校验的结果快照。"""
    __tablename__ = "regression_runs"
    id = Column(Integer, primary_key=True, index=True)
    version_id = Column(Integer, ForeignKey("review_versions.id"), nullable=True)
    status = Column(String(20), nullable=False, default="done")
    total = Column(Integer, nullable=False, default=0)
    passed = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    results_json = Column(JSON, nullable=False, default=list)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_utc_now, index=True)
    finished_at = Column(DateTime, nullable=True)


class SamplingTask(Base):
    """一次下样任务。与报告审核规则完全分表保存。"""
    __tablename__ = "sampling_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_no = Column(String(80), nullable=False, unique=True, index=True)
    enterprise_name = Column(String(255), nullable=False, index=True)
    mode = Column(String(30), nullable=False, default="fast", index=True)
    application_nos_json = Column(JSON, nullable=False, default=list)
    input_json = Column(JSON, nullable=False, default=dict)
    status = Column(String(30), nullable=False, default="draft", index=True)
    current_version_id = Column(Integer, nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=_utc_now, index=True)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)

    creator = relationship("User", foreign_keys=[created_by])
    versions = relationship(
        "SamplingPlanVersion",
        back_populates="task",
        foreign_keys="SamplingPlanVersion.task_id",
        order_by="SamplingPlanVersion.version_no",
        cascade="all, delete-orphan",
    )


class SamplingPlanVersion(Base):
    """不可变下样方案快照。人工调整总是新增子版本。"""
    __tablename__ = "sampling_plan_versions"
    __table_args__ = (
        UniqueConstraint("task_id", "version_no", name="uq_sampling_task_version"),
    )

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("sampling_tasks.id"), nullable=False, index=True)
    version_no = Column(Integer, nullable=False, default=1)
    parent_version_id = Column(Integer, ForeignKey("sampling_plan_versions.id"), nullable=True, index=True)
    source = Column(String(30), nullable=False, default="generated")
    change_type = Column(String(40), nullable=False, default="generate")
    change_reason = Column(Text, nullable=True)
    plan_json = Column(JSON, nullable=False, default=dict)
    coverage_json = Column(JSON, nullable=False, default=dict)
    copy_text = Column(Text, nullable=False, default="")
    validation_status = Column(String(30), nullable=False, default="draft_only", index=True)
    hard_gap_count = Column(Integer, nullable=False, default=0)
    rule_package_version = Column(String(80), nullable=False, default="unknown")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=_utc_now, index=True)

    task = relationship("SamplingTask", back_populates="versions", foreign_keys=[task_id])
    parent = relationship("SamplingPlanVersion", remote_side=[id], foreign_keys=[parent_version_id])
    creator = relationship("User", foreign_keys=[created_by])
