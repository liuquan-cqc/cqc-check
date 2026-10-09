"""全局配置：环境变量统一入口。"""
import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """从环境变量读取所有配置，默认值适配SQLite开发环境。"""
    database_url: str = "sqlite:///./data/ccc_review.db"
    # 上传/OCR文件目录
    uploads_dir: str = "./data/uploads"
    ocr_dir: str = "./data/ocr"
    knowledge_dir: str = "./data/knowledge"

    # LLM
    siliconflow_api_key: str = ""
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V3.2"
    vision_model: str = "Qwen/Qwen3-VL-30B-A3B-Instruct"

    # 调试/降级开关
    llm_mock: bool = False
    ocr_disabled: bool = False

    # 简单认证（旧 /api/login 兼容用）
    admin_password: str = "admin123"

    # JWT认证
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_hours: int = 12

    # 服务端口
    port: int = 8080

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
