"""Release 服务 — 查询 GitHub 仓库的 Release 与资产列表"""

from __future__ import annotations

import json
import re

from core.token_validator import _curl_json
from utils.github_url_utils import _VALID_OWNER, _VALID_REPO, _extract_owner_repo


def fetch_repo_info(
    owner: str,
    repo: str,
    token: str | None = None,
    proxy: str | None = None,
    timeout: int = 10,
) -> tuple[int, str]:
    """查询仓库基本信息，返回 (stargazers_count, 错误信息)。

    非 GitHub 仓库或 API 失败时 stars=0，错误信息说明原因。
    """
    headers = ["-H", f"Authorization: Bearer {token}"] if token else []
    url = f"https://api.github.com/repos/{owner}/{repo}"
    status, body = _curl_json(url, headers, proxy, timeout)
    if status == 200:
        try:
            data = json.loads(body)
            return int(data.get("stargazers_count", 0)), ""
        except (json.JSONDecodeError, ValueError, TypeError):
            return 0, "响应解析失败"
    if status == 404:
        return 0, "仓库不存在或为私有"
    if status in (301, 302):
        return 0, "仓库已转移或改名"
    return 0, f"HTTP {status}" if status > 0 else body or "网络错误"


def extract_github_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """
    从仓库远程地址提取 (owner, repo)。

    仅支持 GitHub 远程；Gitee/GitLab 等返回 None。
    """
    if not remote_url:
        return None
    owner, repo = _extract_owner_repo(remote_url)
    if owner and repo and _VALID_OWNER.match(owner) and _VALID_REPO.match(repo):
        # 排除非 GitHub 域名（gitee/gitlab 等）
        non_github = re.search(
            r"(?:gitee|gitlab|bitbucket|gitcode)\.",
            remote_url,
            re.IGNORECASE,
        )
        if not non_github:
            return owner, repo
    return None


def _parse_release(rel: dict) -> dict:
    """把单个 GitHub Release API 对象转为本程序内部结构。"""
    assets = [
        {
            "name": a.get("name", ""),
            "size": a.get("size", 0),
            "download_count": a.get("download_count", 0),
            "url": a.get("url", ""),  # API 地址（下载需带 Accept 头）
            # 资产上传时间（晚于等于 release 发布时间，重新上传会更新）
            "updated_at": (a.get("updated_at") or "")[:10],
        }
        for a in rel.get("assets", [])
    ]
    return {
        "tag": rel.get("tag_name", ""),
        "name": rel.get("name") or rel.get("tag_name", ""),
        "published_at": (rel.get("published_at") or "")[:10],
        "prerelease": bool(rel.get("prerelease")),
        "body": rel.get("body") or "",
        "assets": assets,
    }


def fetch_releases(
    owner: str,
    repo: str,
    token: str | None = None,
    proxy: str | None = None,
    timeout: int = 15,
    per_page: int = 100,
) -> tuple[list[dict] | None, str]:
    """
    查询仓库的 Release 列表（自动分页，加载全部 Release）。

    GitHub API 单页最多 100 条；本函数按 `per_page=100` 逐页拉取，
    直到某页返回不足 `per_page` 条（末页）即停止，安全上限 50 页（5000 条）。

    返回: (release 列表, 错误信息)。成功时错误信息为空串。

    每个 release 字典包含:
        tag, name, published_at, prerelease, body, assets
    每个 asset 字典包含: name, size, download_count, url
    """
    headers = ["-H", f"Authorization: Bearer {token}"] if token else []
    releases: list[dict] = []
    max_pages = 50  # 安全上限，防止异常仓库导致无限翻页
    page = 1
    while page <= max_pages:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/releases"
            f"?per_page={per_page}&page={page}"
        )
        status, body = _curl_json(url, headers, proxy, timeout)
        if status == -1:
            # 网络层错误：已加载的部分不返回，避免误显示"全部"
            return None, body
        if status in (301, 302):
            return None, "仓库已转移或改名，且新地址无法访问 (HTTP 301/302)"
        if status == 404:
            # 首页 404 = 仓库不存在/私有/无 Release；翻页中 404 视为已到末页
            if page == 1:
                return None, f"仓库 {owner}/{repo} 不存在、为私有或无 Release"
            break
        if status == 401:
            return None, "令牌无效或已过期 (HTTP 401)"
        if status == 403:
            return None, "API 配额已用尽或无权访问（HTTP 403）"
        if status != 200:
            return None, f"GitHub 返回异常状态 (HTTP {status})"

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None, "响应解析失败"
        if not isinstance(data, list):
            return None, "响应格式异常"

        for rel in data:
            releases.append(_parse_release(rel))

        # 不足一页 = 已到末页；否则继续翻下一页
        if len(data) < per_page:
            break
        page += 1

    # 日期从新到旧：release 按发布日期、资产按上传/更新日期倒序
    releases.sort(key=lambda r: r["published_at"], reverse=True)
    for rel in releases:
        rel["assets"].sort(key=lambda a: a["updated_at"], reverse=True)
    return releases, ""
