"""克隆管理器 — URL解析、标准化、克隆请求解析。

职责：
    - 调用 normalize_github_url() 标准化各种 GitHub URL 格式
    - 从 URL 提取仓库名称
    - 构建克隆候选源列表（主源 + 镜像/回退）
    - 完整解析克隆请求，返回所有克隆所需信息

不含 Qt 依赖，可被 Worker 和 UI 层共享调用。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from app.common.github_url_utils import extract_clone_url, normalize_github_url

# 开头是否带 scheme://（注意：嵌套镜像 URL 如 ghfast.top/https://github.com/...
# 中途含 "://"，不能据此判定已有 scheme，git 会将其当作本地路径）
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

# ---------------------------------------------------------------------------
# GitHub 镜像前缀（2026-10-15 本机实测可用）：
#   - ghfast.top / gh-proxy.com 可用
#   - ghproxy.com 域名已失效，勿加回
#   - gitclone.com 超时不可用，勿加
# ---------------------------------------------------------------------------
GITHUB_MIRROR_PREFIXES = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
)


def _complete_scheme(url: str) -> str:
    """补全 scheme：开头无 scheme:// 且无 "@"（SSH）时前缀 https://，其余原样返回。"""
    if url and not _SCHEME_RE.match(url) and "@" not in url:
        return f"https://{url}"
    return url


def _is_github_ssh(url: str) -> bool:
    """判断是否为 git@github.com:owner/repo 形式的 SSH 地址。"""
    at = url.rfind("@")
    if at == -1:
        return False
    colon = url.find(":", at)
    if colon == -1:
        return False
    return url[at + 1 : colon].strip().lower() == "github.com"


def _effective_canonical(raw: str, url: str) -> str | None:
    """计算 canonical 的有效值，排除非 GitHub 地址的误报。

    normalize_github_url 的通用路径提取会把 git@gitee.com:o/r.git、
    ssh://git@gitee.com/o/r.git、https://user@gitee.com/o/r.git 等误判为
    github.com/owner/repo.git（其冲突域检测不识别 "@" 前缀的主机），
    此处修正：地址带 @（SSH/userinfo）且主机不是 github.com 时，
    视为非 GitHub 源。
    """
    canonical = normalize_github_url(raw)
    if canonical is None:
        return None
    if _is_github_ssh(url):
        return canonical
    if "@" in url:
        if "://" in url:
            host = (urlparse(url).hostname or "").lower()
            if host == "github.com":
                return canonical
        return None
    return canonical


def _is_github_input(url: str) -> bool:
    """判断输入是否直接指向 GitHub（而非镜像站包装）。

    - 含 "://"：netloc 为 github.com → GitHub 直连
    - SSH：git@github.com:owner/repo → GitHub 直连
    - 无 scheme：补 scheme 后 netloc 为 github.com（如 github.com/o/r）→ 直连；
      或 netloc 不含 "."（如裸 owner/repo）→ 交给 canonical 处理
    其余（netloc 是别的域名，如 ghfast.top、gitclone.com）→ 镜像包装，返回 False
    """
    if "://" in url:
        return urlparse(url).netloc.lower() == "github.com"
    if _is_github_ssh(url):
        return True
    netloc = urlparse(f"https://{url}").netloc.lower()
    return netloc == "github.com" or "." not in netloc


def _looks_like_git_address(url: str) -> bool:
    """轻校验：判断非 GitHub 输入是否像 Git 地址。

    SSH scp 形式（含 @ 无 scheme）视为通过；带 scheme 的要求 netloc 或
    path 至少其一非空（排除 "http://" 这类退化输入）；否则补 scheme 后
    要求 netloc 含 "."（是域名）且 path 非空（有仓库路径）。
    """
    if "@" in url and "://" not in url:
        return True
    if "://" in url:
        parsed = urlparse(url)
        return bool(parsed.netloc) or bool(parsed.path.strip("/"))
    parsed = urlparse(f"https://{url}")
    return "." in parsed.netloc and bool(parsed.path.strip("/"))


class CloneManager:
    """克隆仓库的核心业务逻辑（纯函数，无状态）。"""

    # ------------------------------------------------------------------
    # URL 标准化
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_url(raw_url: str) -> str:
        """返回主克隆源 URL。

        - GitHub 直连 / 裸 owner/repo：返回 canonical（github.com 标准地址）
        - 镜像包装（ghfast.top 等）：保留原始输入（尊重用户用镜像的意图），
          canonical 作为回退候选
        - 非 GitHub 源（gitee/gitlab 等）：返回补 scheme 后的原始输入
        """
        raw = (raw_url or "").strip()
        if not raw:
            return ""
        url = extract_clone_url(raw) or raw
        canonical = _effective_canonical(raw, url)
        if canonical is None:
            # 非 GitHub 源：原样保留（仅补 scheme）
            return _complete_scheme(url)
        if _is_github_input(url):
            # GitHub 直连或裸 owner/repo
            return canonical
        # 镜像包装：保留原始输入，canonical 由候选列表负责回退
        return _complete_scheme(url)

    # ------------------------------------------------------------------
    # Git 地址判定（供 UI 层格式化提示使用）
    # ------------------------------------------------------------------
    @staticmethod
    def is_git_address(raw_url: str) -> bool:
        """判断输入是否为可用的 Git 仓库地址。

        GitHub 直连 / 镜像包装（canonical 可解析）→ True；
        其他源（gitee/gitlab/SSH/自建）按轻校验判定。
        """
        raw = (raw_url or "").strip()
        if not raw:
            return False
        url = extract_clone_url(raw) or raw
        if _effective_canonical(raw, url) is not None:
            return True
        return _looks_like_git_address(url)

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
        """构建克隆候选源列表（主源在前，镜像/回退在后）。

        - GitHub 直连：[canonical] + 各镜像前缀包装（共 3 个）
        - 镜像包装：[原始输入, canonical]（用户镜像优先，GitHub 直连回退）
        - 非 GitHub 源：[补 scheme 后的原始输入]（单候选）
        去重保序。
        """
        raw = (url or "").strip()
        if not raw:
            return []
        primary = CloneManager.normalize_url(raw)
        canonical = _effective_canonical(raw, extract_clone_url(raw) or raw)

        if canonical is None:
            # 非 GitHub 源：单候选
            candidates = [primary]
        elif primary == canonical:
            # GitHub 直连：主源 + 已验证可用的镜像
            candidates = [canonical] + [
                prefix + canonical for prefix in GITHUB_MIRROR_PREFIXES
            ]
        else:
            # 镜像包装：用户指定的镜像优先，GitHub 直连回退
            candidates = [primary, canonical]

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
                "normalized_url": str,   # 主克隆源 URL（向后兼容字段）
                "repo_name": str,        # 仓库名称
                "target_path": str,      # 克隆目标绝对路径
                "candidates": list[str], # 克隆候选源列表（依次尝试）
                "error": str | None,     # 错误信息（None 表示通过）
            }
        """
        raw = (raw_url or "").strip()
        normalized = CloneManager.normalize_url(raw)
        url = extract_clone_url(raw) or raw
        canonical = _effective_canonical(raw, url)

        # 轻校验：非 GitHub 源至少要像 Git 地址
        # （gitee.com/owner/repo 通过；"not a url"、"abc" 拒绝）
        if canonical is None and not _looks_like_git_address(url):
            return {
                "normalized_url": normalized,
                "repo_name": "",
                "target_path": "",
                "candidates": [],
                "error": "无法识别为 Git 仓库地址，请检查输入",
            }

        repo_name = CloneManager.derive_repo_name(normalized)

        if not repo_name:
            return {
                "normalized_url": normalized,
                "repo_name": "",
                "target_path": "",
                "candidates": [],
                "error": "无法从 URL 解析仓库名称",
            }

        base_abs = os.path.abspath(base_dir)
        target_path = os.path.join(base_abs, repo_name)

        if os.path.exists(target_path):
            return {
                "normalized_url": normalized,
                "repo_name": repo_name,
                "target_path": target_path,
                "candidates": [],
                "error": f"目录已存在: {repo_name}",
            }

        return {
            "normalized_url": normalized,
            "repo_name": repo_name,
            "target_path": target_path,
            "candidates": CloneManager.build_clone_candidates(raw),
            "error": None,
        }
