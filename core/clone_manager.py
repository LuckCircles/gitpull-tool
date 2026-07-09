"""克隆管理器 — URL解析、标准化、克隆请求解析。

职责：
    - 调用 normalize_github_url() 标准化各种 URL 格式
    - 从 URL 提取仓库名称
    - 构建克隆候选源列表（主源 + 镜像）
    - 完整解析克隆请求，返回所有克隆所需信息

不含 Qt 依赖，可被 Worker 和 UI 层共享调用。
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from github_url_utils import normalize_github_url


class CloneManager:
    """克隆仓库的核心业务逻辑（纯函数，无状态）。"""

    # ------------------------------------------------------------------
    # URL 标准化
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_url(raw_url: str) -> str:
        """标准化 URL，返回标准格式或原始输入。"""
        return normalize_github_url(raw_url) or raw_url

    # ------------------------------------------------------------------
    # 仓库名提取
    # ------------------------------------------------------------------
    @staticmethod
    def derive_repo_name(url: str) -> str:
        """从 URL 提取仓库名称。"""
        cleaned = url.strip().rstrip("/").rstrip("\\")
        if not cleaned:
            return ""
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        repo_name = os.path.basename(cleaned.replace(":", "/"))
        return repo_name.strip()

    # ------------------------------------------------------------------
    # 克隆候选源
    # ------------------------------------------------------------------
    @staticmethod
    def build_clone_candidates(url: str) -> list[str]:
        """构建克隆候选源列表（主源 + 镜像）。"""
        primary = url.strip()
        if not primary:
            return []
        candidates = [primary]
        parsed = urlparse(primary)

        if parsed.scheme in ("http", "https") and parsed.netloc.lower() == "github.com":
            candidates.append(f"https://ghproxy.com/{primary}")
        elif parsed.scheme in ("http", "https") and parsed.netloc.lower() == "gitee.com":
            candidates.insert(0, primary)
        elif parsed.scheme in ("http", "https") and parsed.netloc.lower() == "gitlab.com":
            candidates.append(primary)

        unique_candidates = []
        for candidate in candidates:
            if candidate not in unique_candidates:
                unique_candidates.append(candidate)
        return unique_candidates

    # ------------------------------------------------------------------
    # 完整克隆请求解析
    # ------------------------------------------------------------------
    @staticmethod
    def resolve_clone_request(raw_url: str, base_dir: str) -> dict:
        """完整解析克隆请求。

        Returns:
            {
                "normalized_url": str,   # 标准化后的 URL
                "repo_name": str,        # 仓库名称
                "target_path": str,      # 克隆目标绝对路径
                "error": str | None,     # 错误信息（None 表示通过）
            }
        """
        normalized = CloneManager.normalize_url(raw_url)
        repo_name = CloneManager.derive_repo_name(normalized)

        if not repo_name:
            return {
                "normalized_url": "",
                "repo_name": "",
                "target_path": "",
                "error": "无法从 URL 解析仓库名称",
            }

        base_abs = os.path.abspath(base_dir)
        target_path = os.path.join(base_abs, repo_name)

        if os.path.exists(target_path):
            return {
                "normalized_url": normalized,
                "repo_name": repo_name,
                "target_path": target_path,
                "error": f"目录已存在: {repo_name}",
            }

        return {
            "normalized_url": normalized,
            "repo_name": repo_name,
            "target_path": target_path,
            "error": None,
        }
