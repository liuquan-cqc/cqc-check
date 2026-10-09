"""SQLAlchemy 引擎与会话。兼容 SQLite 和 PostgreSQL。"""
import re
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker
from backend.app.config import get_settings

settings = get_settings()

# SQLite 需要关闭检查点同线程限制
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# 给 SQLite 加外键支持
def _sqlite_set_fk(dbapi_conn, _):
    from sqlite3 import Connection
    if isinstance(dbapi_conn, Connection):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


if settings.database_url.startswith("sqlite"):
    event.listen(engine, "connect", _sqlite_set_fk)


def get_db():
    """FastAPI 依赖。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """启动时建表。"""
    from backend.app import models
    Base.metadata.create_all(bind=engine)
    # create_all 不会为旧表补充新列；为已部署实例做轻量兼容迁移。
    try:
        with engine.begin() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("reports")}
            if "application_no" not in columns:
                connection.execute(text("ALTER TABLE reports ADD COLUMN application_no VARCHAR(100)"))
            if "original_filename" not in columns:
                connection.execute(text("ALTER TABLE reports ADD COLUMN original_filename VARCHAR(500)"))
            if "file_sha256" not in columns:
                connection.execute(text("ALTER TABLE reports ADD COLUMN file_sha256 VARCHAR(64)"))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_reports_file_sha256 ON reports (file_sha256)"
            ))
            if "root_report_id" not in columns:
                connection.execute(text("ALTER TABLE reports ADD COLUMN root_report_id INTEGER"))
            if "parent_report_id" not in columns:
                connection.execute(text("ALTER TABLE reports ADD COLUMN parent_report_id INTEGER"))
            if "revision_no" not in columns:
                connection.execute(text("ALTER TABLE reports ADD COLUMN revision_no INTEGER DEFAULT 1 NOT NULL"))
            if "revision_note" not in columns:
                connection.execute(text("ALTER TABLE reports ADD COLUMN revision_note TEXT"))
            if "uploaded_by_id" not in columns:
                connection.execute(text("ALTER TABLE reports ADD COLUMN uploaded_by_id INTEGER"))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_reports_uploaded_by_id ON reports (uploaded_by_id)"
            ))
            # 旧数据全部是版本链的首版；分两步回填兼容 SQLite/PostgreSQL。
            connection.execute(text("UPDATE reports SET revision_no = 1 WHERE revision_no IS NULL"))
            connection.execute(text("UPDATE reports SET root_report_id = id WHERE root_report_id IS NULL"))

            # 旧报告没有上传人列，但上传审计日志保存了当时的用户名和报告 ID。
            # 仅用明确的“上传报告/上传更正版本”日志回填，无法追溯的继续保持空值。
            audit_rows = connection.execute(text(
                "SELECT user_id, action, detail FROM audit_logs "
                "WHERE user_id IS NOT NULL AND action IN ('上传报告', '上传更正版本')"
            )).mappings().all()
            for audit_row in audit_rows:
                detail = str(audit_row.get("detail") or "")
                pattern = r"新报告\s*ID\s*[：:]\s*(\d+)" if audit_row["action"] == "上传更正版本" else r"报告\s*ID\s*[：:]\s*(\d+)"
                matched = re.search(pattern, detail)
                if matched:
                    connection.execute(text(
                        "UPDATE reports SET uploaded_by_id = :user_id "
                        "WHERE id = :report_id AND uploaded_by_id IS NULL"
                    ), {"user_id": audit_row["user_id"], "report_id": int(matched.group(1))})

            user_columns = {column["name"] for column in inspect(connection).get_columns("users")}
            if "permissions_override" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN permissions_override JSON"))

            company_columns = {column["name"] for column in inspect(connection).get_columns("companies")}
            if "unified_social_credit_code" not in company_columns:
                connection.execute(text("ALTER TABLE companies ADD COLUMN unified_social_credit_code VARCHAR(32)"))
            if "operating_status" not in company_columns:
                connection.execute(text("ALTER TABLE companies ADD COLUMN operating_status VARCHAR(30) DEFAULT 'unknown' NOT NULL"))
            if "status_source" not in company_columns:
                connection.execute(text("ALTER TABLE companies ADD COLUMN status_source VARCHAR(500)"))
            if "status_checked_at" not in company_columns:
                timestamp_type = "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
                connection.execute(text(f"ALTER TABLE companies ADD COLUMN status_checked_at {timestamp_type}"))
            connection.execute(text("UPDATE companies SET operating_status = 'unknown' WHERE operating_status IS NULL"))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_companies_unified_social_credit_code "
                "ON companies (unified_social_credit_code)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_companies_operating_status ON companies (operating_status)"
            ))
    except SQLAlchemyError:
        # API 与 Worker 可能同时启动，另一进程已完成迁移时安全忽略。
        pass
