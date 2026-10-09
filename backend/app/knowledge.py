"""知识库读取：把 data/knowledge 下所有 Markdown 文件拼接。"""
from __future__ import annotations
import os
import re
from pathlib import Path
from backend.app.config import get_settings


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _visible_markdown(content: str) -> str:
    """移除勘误库中已注释废弃的旧规则，避免它们仍进入模型提示词。"""
    return _HTML_COMMENT_RE.sub("", content)


def _include_for_family(relative_path: Path, standard_family: str | None) -> bool:
    """按已锁定的标准体系隔离高风险标准文件和案例，防止PVC/橡皮参数串用。"""
    if not standard_family:
        return True
    value = relative_path.as_posix()
    if standard_family == "pvc":
        if value in {"standards/5013-rubber.md", "standards/5013-tables.md", "standards/8735-tables.md"}:
            return False
        # 该案例文件以5013/8735橡皮报告为主，含0.75mm²→6A等高风险数值。
        if value == "cases/report-review-cases-202608i.md":
            return False
    elif standard_family == "rubber":
        if value in {"standards/5023-tables.md", "standards/8734-tables.md", "standards/8734-2-table8-lowtemp.md"}:
            return False
        # PVC案例不参与橡皮电缆判定；保留专门的橡皮案例文件。
        if value.startswith("cases/") and value != "cases/report-review-cases-202608i.md":
            return False
    return True


def load_knowledge(standard_family: str | None = None) -> str:
    """递归读取知识库；已识别标准体系时只加载相容内容。"""
    settings = get_settings()
    knowledge_dir = Path(settings.knowledge_dir)
    if not knowledge_dir.exists():
        return "（知识库目录不存在）"

    parts = []
    for path in sorted(knowledge_dir.rglob("*.md")):
        relative = path.relative_to(knowledge_dir)
        if not _include_for_family(relative, standard_family):
            continue
        try:
            content = _visible_markdown(path.read_text(encoding="utf-8"))
        except Exception as e:
            content = f"（读取失败: {e}）"
        parts.append(f"--- {relative} ---\n{content}\n")
    return "\n".join(parts) if parts else "（知识库为空）"


_REVIEW_RULE_FILES = {
    "rules/01-voltage-rules.md",
    "rules/02-aging-pressure.md",
    "rules/03-bending-impact.md",
    "rules/04-lowtemp-combustion.md",
    "standards/voltage-rules-by-standard.md",
    "standards/5013-rubber.md",
    "standards/5013-tables.md",
    "standards/8735-tables.md",
    "standards/5023-tables.md",
    "standards/8734-tables.md",
    "standards/8734-2-table8-lowtemp.md",
}


def load_review_knowledge(standard_family: str | None = None) -> str:
    """加载审核判定所需的紧凑知识上下文。

    浏览说明、数据库说明、验证报告和长篇案例不重复塞入每个样品请求；
    它们仍保留在知识库浏览页。审核只携带数值规则和对应标准表，降低
    长报告请求超时概率。
    """
    settings = get_settings()
    knowledge_dir = Path(settings.knowledge_dir)
    if not knowledge_dir.exists():
        return "（知识库目录不存在）"
    parts = []
    for relative_value in sorted(_REVIEW_RULE_FILES):
        relative = Path(relative_value)
        if not _include_for_family(relative, standard_family):
            continue
        path = knowledge_dir / relative
        if not path.is_file():
            continue
        try:
            content = _visible_markdown(path.read_text(encoding="utf-8"))
        except Exception as exc:
            content = f"（读取失败: {exc}）"
        parts.append(f"--- {relative} ---\n{content}\n")
    return "\n".join(parts) if parts else "（审核知识库为空）"
