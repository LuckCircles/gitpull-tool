"""Release 服务 — 查询 GitHub 仓库的 Release 与资产列表"""

from __future__ import annotations

import json
import re
from typing import Tuple

from core.token_validator import _curl_json
from utils.github_url_utils import _extract_owner_repo, _VALID_OWNER, _VALID_REPO


def extract_github_owner_repo(remote_url: str) -> Tuple[str, str] | None:
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


def fetch_releases(
    owner: str,
    repo: str,
    token: str | None = None,
    proxy: str | None = None,
    timeout: int = 15,
    per_page: int = 20,
) -> Tuple[list[dict] | None, str]:
    """
    查询仓库的 Release 列表。

    返回: (release 列表, 错误信息)。成功时错误信息为空串。

    每个 release 字典包含:
        tag, name, published_at, prerelease, body, assets
    每个 asset 字典包含: name, size, download_count, url
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page={per_page}"
    headers = []
    if token:
        headers = ["-H", f"Authorization: Bearer {token}"]

    status, body = _curl_json(url, headers, proxy, timeout)
    if status == -1:
        return None, body
    if status == 301 or status == 302:
        return None, "仓库已转移或改名，且新地址无法访问 (HTTP 301/302)"
    if status == 404:
        return None, f"仓库 {owner}/{repo} 不存在、为私有或无 Release"
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

    releases = []
    for rel in data:
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
        releases.append(
            {
                "tag": rel.get("tag_name", ""),
                "name": rel.get("name") or rel.get("tag_name", ""),
                "published_at": (rel.get("published_at") or "")[:10],
                "prerelease": bool(rel.get("prerelease")),
                "body": rel.get("body") or "",
                "assets": assets,
            }
        )

    # 日期从新到旧：release 按发布日期、资产按上传/更新日期倒序
    releases.sort(key=lambda r: r["published_at"], reverse=True)
    for rel in releases:
        rel["assets"].sort(key=lambda a: a["updated_at"], reverse=True)
    return releases, ""
