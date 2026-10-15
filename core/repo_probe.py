"""仓库下架 / 只剩 README 的快速探针。

设计目标：**快**。本地 git 判定为主（毫秒级，只依赖已 fetch 的远端引用），
在线判定每仓库最多 2 次 GitHub API 请求。刻意不做 DMCA 公告检索、
历史遍历、Fork 检查等重活。

判定顺序（先命中先返回，取最严重者）：
1. fetch 报 404 / not found            → remote_missing   远端已删除或私有化
2. 远端分支 origin/<branch> 不存在     → remote_missing   远端分支不存在
3. 本地与远端无共同祖先（merge-base）  → history_rewritten 远端历史被重写
4. 远端分支提交数为 0                  → empty_remote     远端空分支
5. 远端分支仅 1 个提交且本地更多        → 记为显著提示（不单独下结论，见下）
6. 本地领先远端提交数 >= 阈值           → local_far_ahead  本地领先远端过多
7. 远端树仅剩 README                    → readme_only      远端仅剩 README   ← 用户最关心
8. 远端树仅剩文档（无源码）             → docs_only        远端仅剩文档
9. 远端树为空                           → empty_remote     远端为空仓库
10. 在线 404                             → remote_missing
11. 在线 451                             → 不定性，仅记录状态码
12. 在线 archived=true                   → archived         远端已归档
13. 其余且远端有源码                    → normal           正常
14. 任何环节无法判定                     → unknown          未知

关于第 5 条：远端只剩 1 个提交时，无法区分"远端被清空重建"与"本地提交尚未推送"
——两者在 git 层面表现完全相同。因此不把它单独判成一种状态，只写入 details 提示，
最终结论仍由领先数阈值与远端文件树决定，避免把"用户还没 push"误报成删库。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from core.git_service import (
    AHEAD_DELETION_THRESHOLD,
    GitService,
    is_remote_deleted,
)
from core.release_service import extract_github_owner_repo
from core.token_validator import _curl_json

STATUS_NORMAL = "normal"
STATUS_README_ONLY = "readme_only"
STATUS_DOCS_ONLY = "docs_only"
STATUS_EMPTY_REMOTE = "empty_remote"
STATUS_HISTORY_REWRITTEN = "history_rewritten"
STATUS_LOCAL_FAR_AHEAD = "local_far_ahead"
STATUS_REMOTE_MISSING = "remote_missing"
STATUS_ARCHIVED = "archived"
STATUS_UNKNOWN = "unknown"

STATUS_LABELS: dict[str, str] = {
    STATUS_NORMAL: "正常",
    STATUS_README_ONLY: "仅剩 README",
    STATUS_DOCS_ONLY: "仅剩文档",
    STATUS_EMPTY_REMOTE: "空仓库",
    STATUS_HISTORY_REWRITTEN: "历史被重写",
    STATUS_LOCAL_FAR_AHEAD: "本地领先过多",
    STATUS_REMOTE_MISSING: "远端已删除",
    STATUS_ARCHIVED: "已归档",
    STATUS_UNKNOWN: "未知",
}

# 严重度排序（列表越靠前越严重）
SEVERITY_ORDER: list[str] = [
    STATUS_REMOTE_MISSING,
    STATUS_HISTORY_REWRITTEN,
    STATUS_LOCAL_FAR_AHEAD,
    STATUS_README_ONLY,
    STATUS_DOCS_ONLY,
    STATUS_EMPTY_REMOTE,
    STATUS_ARCHIVED,
    STATUS_UNKNOWN,
    STATUS_NORMAL,
]

AHEAD_THRESHOLD = AHEAD_DELETION_THRESHOLD

SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java", ".cs",
    ".cpp", ".c", ".h", ".hpp", ".cc", ".dart", ".swift", ".rb", ".php",
    ".sh", ".bash", ".ps1", ".bat", ".html", ".css", ".vue", ".svelte",
    ".kt", ".scala", ".m", ".mm", ".pl", ".lua", ".r", ".jl", ".ex",
    ".exs", ".sql", ".gradle", ".mjs", ".cjs", ".mts", ".cts", ".fs",
    ".fsx", ".vb", ".clj", ".cljs", ".erl", ".hs", ".ml", ".zig", ".v",
    ".sol", ".asm", ".s",
})

DOC_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".markdown", ".rst", ".txt", ".adoc",
})

README_PREFIX = "readme"

# 本地传输（file:// 与裸路径）远端消失时的 git 报错特征：
#   fatal: '<path>' does not appear to be a git repository
# 本地传输没有「网络不通」这一说，路径不存在即远端已被删除；
# 故仅对本地传输启用该特征，http(s)/ssh 仍只认 is_remote_deleted 的 404 特征
# （避免服务端故障被误判成删库）。
_LOCAL_TRANSPORT_RE = re.compile(r"^file://", re.IGNORECASE)
_LOCAL_REPO_GONE_RE = re.compile(
    r"does not appear to be a git repository|does not exist",
    re.IGNORECASE,
)

# 无扩展名但属于构建/源码入口的文件
_SOURCE_BASENAMES: frozenset[str] = frozenset({
    "dockerfile", "makefile", "gnumakefile", "cmakelists.txt",
    "rakefile", "gemfile",
})

_MAX_DETAIL_FILES = 5


@dataclass
class ProbeResult:
    repo_path: str = ""
    repo_name: str = ""
    owner_repo: str = ""  # "owner/repo"，非 GitHub 则空
    remote_url: str = ""
    status: str = STATUS_UNKNOWN
    details: list[str] = field(default_factory=list)  # 中文说明，逐条
    evidence_url: str = ""  # 可点击证据链接
    checked_at: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "repo_name": self.repo_name,
            "owner_repo": self.owner_repo,
            "remote_url": self.remote_url,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "details": list(self.details),
            "evidence_url": self.evidence_url,
            "checked_at": self.checked_at,
            "duration_seconds": self.duration_seconds,
        }


def _is_readme(path: str) -> bool:
    """README 判定：文件名（转小写）以 readme 开头即算。"""
    return os.path.basename(path).lower().startswith(README_PREFIX)


def _is_source(path: str) -> bool:
    """源码判定：扩展名命中或属于构建入口文件（不使用「非 README 即源码」）。"""
    base = os.path.basename(path).lower()
    if base in _SOURCE_BASENAMES:
        return True
    return os.path.splitext(base)[1] in SOURCE_EXTENSIONS


def _is_doc(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in DOC_EXTENSIONS


def _is_bare_path(url: str) -> bool:
    """远端是否为本地裸路径（非 file:// 前缀的绝对/相对路径）。"""
    if not url:
        return False
    lowered = url.lower()
    if "://" in lowered or "@" in url.split("/")[0]:
        return False
    return os.path.exists(url) or os.sep in url or "/" in url


def _file_summary(paths: list[str]) -> str:
    """把文件列表拼成简短证据：最多列 5 个，超出补「等 N 个」。"""
    if not paths:
        return ""
    shown = "、".join(paths[:_MAX_DETAIL_FILES])
    if len(paths) > _MAX_DETAIL_FILES:
        shown += f" 等 {len(paths)} 个"
    return shown


class RepoProbeService:
    """快速探针：本地 git 判定为主，在线最多 2 次请求。"""

    def __init__(
        self,
        token: str | None = None,
        proxy: str | None = None,
        git_service: GitService | None = None,
    ):
        self._token = token
        self._proxy = proxy
        self._git = git_service or GitService()

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------
    def probe_many(self, repo_paths: list[str]) -> list[ProbeResult]:
        """批量检测：单个失败不影响其余。"""
        results: list[ProbeResult] = []
        for path in repo_paths or []:
            try:
                results.append(self.probe(path))
            except Exception as e:  # noqa: BLE001 单个仓库异常兜底
                logger.opt(exception=True).error(f"[Probe] 检测异常: {path}")
                results.append(
                    ProbeResult(
                        repo_path=path,
                        repo_name=os.path.basename((path or "").rstrip("\\/")),
                        status=STATUS_UNKNOWN,
                        details=[f"检测失败：{str(e)[:120]}"],
                        checked_at=datetime.now().isoformat(timespec="seconds"),
                    )
                )
        return results

    def probe(self, repo_path: str, *, remote_url: str = "") -> ProbeResult:
        """检测单个仓库；不抛异常，任何失败都按 unknown 返回。"""
        started = time.monotonic()
        repo_abs = os.path.abspath(repo_path or "")
        result = ProbeResult(
            repo_path=repo_abs,
            repo_name=os.path.basename(repo_abs.rstrip("\\/")) or repo_abs,
            remote_url=remote_url,
            checked_at=datetime.now().isoformat(timespec="seconds"),
        )
        try:
            self._probe_inner(result, repo_abs, remote_url)
        except Exception as e:  # noqa: BLE001 probe 顶层兜底
            logger.opt(exception=True).error(f"[Probe] 检测异常: {repo_abs}")
            result.status = STATUS_UNKNOWN
            result.details.append(f"检测失败：{str(e)[:120]}")
        result.duration_seconds = round(time.monotonic() - started, 3)
        return result

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def _probe_inner(
        self, result: ProbeResult, repo_abs: str, remote_url: str
    ) -> None:
        if not repo_abs or not os.path.isdir(repo_abs):
            result.status = STATUS_UNKNOWN
            result.details.append("仓库路径不存在")
            return

        if not remote_url:
            url, _, _ = self._git.run_git(repo_abs, ["remote", "get-url", "origin"])
            remote_url = (url or "").strip()
            result.remote_url = remote_url
        if not remote_url:
            result.status = STATUS_UNKNOWN
            result.details.append("未配置远端地址，无法判定")
            return

        owner_repo = self._github_owner_repo(remote_url)
        result.owner_repo = f"{owner_repo[0]}/{owner_repo[1]}" if owner_repo else ""

        # ---- 本地判定（持仓库锁，避免与扫描/更新并发冲突）----
        with self._git.repo_lock(repo_abs):
            local = self._judge_local(result, repo_abs, remote_url)

        # ---- 在线判定（最多 2 次请求，仅在本地未给出结论或需确认时）----
        if owner_repo:
            self._judge_online(result, owner_repo, local)
        else:
            result.details.append("非 GitHub 远端，已跳过在线判定")

    # ------------------------------------------------------------------
    # 本地 git 判定
    # ------------------------------------------------------------------
    @staticmethod
    def _github_owner_repo(remote_url: str) -> tuple[str, str] | None:
        """仅 GitHub 远端返回 (owner, repo)，其余（本地 file://、Gitee 等）返回 None。

        extract_github_owner_repo 只按路径片段猜测 host，`file:///C:/Users/x/r`
        会被误判成 `Users/x`。此处先要求 URL 显式指向 GitHub 域名再解析。
        """
        url = (remote_url or "").strip()
        if "@" not in url.split("/")[0] and "://" in url:
            # http(s)://github.com/... 或 file:///... —— 仅前者继续
            host = url.split("://", 1)[1].split("/", 1)[0].lower()
            if host not in ("github.com", "www.github.com"):
                return None
        elif not _LOCAL_TRANSPORT_RE.match(url) and not _is_bare_path(url):
            return None
        return extract_github_owner_repo(url)

    # ------------------------------------------------------------------
    # 本地 git 判定
    # ------------------------------------------------------------------
    @staticmethod
    def _local_remote_gone(remote_url: str, stderr: str) -> bool:
        """本地传输（file:// / 裸路径）远端目录消失的判定。

        本地传输不存在「网络故障」歧义，git 报「不是 git 仓库」即目录已删。
        """
        url = (remote_url or "").strip()
        if not (_LOCAL_TRANSPORT_RE.match(url) or _is_bare_path(url)):
            return False
        return bool(_LOCAL_REPO_GONE_RE.search(stderr or ""))

    def _judge_local(
        self, result: ProbeResult, repo_abs: str, remote_url: str
    ) -> str:
        _, fetch_err, fetch_rc = self._git.run_git(
            repo_abs, ["fetch", "--quiet", "--prune"], timeout=30
        )
        if fetch_rc != 0:
            if is_remote_deleted(fetch_err) or self._local_remote_gone(
                remote_url, fetch_err
            ):
                result.status = STATUS_REMOTE_MISSING
                result.details.append("远端仓库已删除或私有化（fetch 404 / not found）")
                return result.status
            # 网络/超时等：不下结论，继续用旧远端引用做本地判定
            reason = (fetch_err or "").strip().splitlines()
            reason = reason[-1][:120] if reason else "未知错误"
            result.details.append(
                f"fetch 失败：{reason}，本地判定基于上次同步的远端引用"
            )

        branch, _, _ = self._git.run_git(repo_abs, ["branch", "--show-current"])
        branch = (branch or "").strip()
        if not branch:
            result.status = STATUS_UNKNOWN
            result.details.append("游离 HEAD，无法确定远端分支")
            return result.status
        remote_ref = f"origin/{branch}"

        # 远端分支是否存在；不存在时区分「远端为空」与「仅该分支被删」
        _, _, ref_rc = self._git.run_git(
            repo_abs, ["rev-parse", "--verify", "--quiet", remote_ref]
        )
        if ref_rc != 0:
            heads, _, ls_rc = self._git.run_git(
                repo_abs, ["ls-remote", "--heads", "origin"], timeout=30
            )
            if ls_rc != 0:
                result.status = STATUS_REMOTE_MISSING
                result.details.append(f"远端不可访问，{remote_ref} 不存在")
                return result.status
            if not (heads or "").strip():
                result.status = STATUS_EMPTY_REMOTE
                result.details.append("远端为空仓库，没有任何分支与文件")
                return result.status
            result.status = STATUS_REMOTE_MISSING
            result.details.append(f"远端分支 {remote_ref} 不存在（远端已删除该分支）")
            return result.status

        # 共同祖先
        head, _, head_rc = self._git.run_git(
            repo_abs, ["rev-parse", "--verify", "--quiet", "HEAD"]
        )
        head = (head or "").strip()
        if head_rc == 0 and head:
            base, _, base_rc = self._git.run_git(
                repo_abs, ["merge-base", head, remote_ref]
            )
            if base_rc != 0 or not (base or "").strip():
                result.status = STATUS_HISTORY_REWRITTEN
                result.details.append(
                    "本地与远端无共同祖先，远端历史疑似被整体重写或删库重建"
                )
                return result.status

            # 远端提交数：0 = 空分支（无歧义，可直接下结论）。
            # 仅 1 个提交时**不能**直接判定为"远端被重建"——本地有未推送的提交
            # 同样是"远端 1 个 commit + 本地多个"，两种情况在 git 层面无法区分。
            # 因此这里只把它作为显著提示写进 details，最终结论仍由领先数与
            # 文件树判定，避免把"用户还没 push"误报成删库重建。
            remote_count_txt, _, _ = self._git.run_git(
                repo_abs, ["rev-list", "--count", remote_ref]
            )
            remote_count = (
                int(remote_count_txt.strip())
                if (remote_count_txt or "").strip().isdigit()
                else -1
            )
            if remote_count == 0:
                result.status = STATUS_EMPTY_REMOTE
                result.details.append("远端分支没有任何提交（空分支）")
                return result.status
            if remote_count == 1:
                local_count_txt, _, _ = self._git.run_git(
                    repo_abs, ["rev-list", "--count", "HEAD"]
                )
                local_count = (
                    int(local_count_txt.strip())
                    if (local_count_txt or "").strip().isdigit()
                    else 0
                )
                if local_count > 1:
                    result.details.append(
                        f"⚠ 远端分支仅剩 1 个提交（本地 {local_count} 个）："
                        "若该远端本应保留完整历史，说明远端已被清空后重新初始化"
                    )
            ahead_txt, _, _ = self._git.run_git(
                repo_abs, ["rev-list", "--count", "HEAD", f"^{remote_ref}"]
            )
            if (ahead_txt or "").strip().isdigit():
                ahead = int(ahead_txt.strip())
                if ahead >= AHEAD_THRESHOLD:
                    result.status = STATUS_LOCAL_FAR_AHEAD
                    result.details.append(
                        f"本地领先远端 {ahead} 个提交，远端历史疑似被截断"
                    )
                    return result.status
                result.details.append(f"本地领先远端 {ahead} 个提交（未达阈值）")

        # 远端文件树分类
        tree, _, tree_rc = self._git.run_git(
            repo_abs, ["ls-tree", "-r", "--name-only", remote_ref]
        )
        if tree_rc != 0:
            result.status = STATUS_UNKNOWN
            result.details.append("无法读取远端文件树")
            return result.status

        paths = [line.strip() for line in (tree or "").splitlines() if line.strip()]
        status = self._classify_tree(result, paths)
        if status is not None:
            result.status = status
            return result.status

        result.details.append(f"远端共 {len(paths)} 个文件，含源码，判定为正常")
        result.status = STATUS_NORMAL
        return result.status

    def _classify_tree(
        self, result: ProbeResult, paths: list[str]
    ) -> str | None:
        """按空 → 仅 README → 仅文档 → 有源码 分类；命中返回状态，否则 None。"""
        if not paths:
            result.details.append("远端为空仓库，没有任何文件")
            return STATUS_EMPTY_REMOTE

        non_readme = [p for p in paths if not _is_readme(p)]
        if not non_readme:
            result.details.append(
                f"远端仅剩 README：{_file_summary(paths)}"
            )
            return STATUS_README_ONLY

        # 有源码 → 正常（.txt 依赖清单不算源码，不能压过源码存在这一事实）
        if any(_is_source(p) for p in non_readme):
            return None

        # 无源码但有文档（README 之外的 .md 等）→ 仅剩文档
        if any(_is_doc(p) for p in non_readme):
            result.details.append(f"远端仅剩文档，无源码：{_file_summary(paths)}")
            return STATUS_DOCS_ONLY

        # 无源码无文档（README + LICENSE / .gitignore 等）→ 仍视为仅剩 README
        result.details.append(
            f"远端除 README 外只剩说明文件：{_file_summary(paths)}"
        )
        return STATUS_README_ONLY

    # ------------------------------------------------------------------
    # 在线判定（最多 2 次请求）
    # ------------------------------------------------------------------
    def _judge_online(
        self, result: ProbeResult, owner_repo: tuple[str, str], local_status: str
    ) -> None:
        owner, name = owner_repo
        full = f"{owner}/{name}"
        result.evidence_url = result.evidence_url or f"https://github.com/{full}"

        headers = ["-H", "Accept: application/vnd.github+json"]
        if self._token:
            headers += ["-H", f"Authorization: Bearer {self._token}"]

        # 请求 1：仓库元信息
        url = f"https://api.github.com/repos/{full}"
        code, body = self._safe_curl(url, headers)
        if code == 404:
            result.status = STATUS_REMOTE_MISSING
            result.details.append("GitHub 侧返回 404：仓库已删除、转私有或改名")
            return
        if code == 451:
            # 451 是 GitHub 的法律限制响应，不做进一步定性：
            # 只如实记录状态码，沿用本地判定结果。
            result.details.append(
                "GitHub 侧返回 451（法律限制），无法进一步判定，沿用本地判定结果"
            )
            return
        if code in (401, 403, 429):
            reason = {
                401: "令牌无效",
                403: "权限不足或已限流",
                429: "请求限流",
            }[code]
            result.details.append(
                f"GitHub 在线状态未知（HTTP {code}：{reason}），沿用本地判定结果"
            )
        default_branch = ""
        if code == 200:
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                data = {}
            if isinstance(data, dict):
                default_branch = data.get("default_branch") or ""
                if data.get("archived"):
                    result.status = STATUS_ARCHIVED
                    result.details.append("远端已归档（archived=true）")
                    return
                if data.get("disabled"):
                    result.details.append("远端已禁用（disabled=true）")
        elif code == -1:
            result.details.append("GitHub 在线请求失败，沿用本地判定结果")
        else:
            result.details.append(f"GitHub 返回异常状态 HTTP {code}")

        # 请求 2：仅当本地判定为「内容疑似丢失」时才在线确认远端树
        if local_status in (
            STATUS_README_ONLY,
            STATUS_DOCS_ONLY,
            STATUS_EMPTY_REMOTE,
        ):
            self._confirm_remote_tree(result, owner_repo, headers, default_branch)

    def _confirm_remote_tree(
        self,
        result: ProbeResult,
        owner_repo: tuple[str, str],
        headers: list[str],
        default_branch: str = "",
    ) -> None:
        owner, name = owner_repo
        url = (
            "https://api.github.com/repos/"
            f"{owner}/{name}/git/trees/{default_branch or 'HEAD'}?recursive=1"
        )
        code, body = self._safe_curl(url, headers)
        if code == -1:
            result.details.append("GitHub 侧文件树查询失败")
            return
        if code != 200:
            result.details.append(f"GitHub 侧文件树查询返回 HTTP {code}")
            return
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            result.details.append("GitHub 侧文件树响应解析失败")
            return

        entries = data.get("tree") or []
        paths = [
            e.get("path", "")
            for e in entries
            if isinstance(e, dict) and e.get("type") == "blob" and e.get("path")
        ]
        if data.get("truncated"):
            # 文件树被截断，结果不完整，不得据此下结论
            result.details.append(
                "GitHub 侧文件树返回 truncated=true（列表不完整），不据此下结论"
            )
            return

        non_readme = [p for p in paths if not _is_readme(p)]
        if paths and not non_readme:
            result.details.append(f"本地与 GitHub 侧一致：远端当前仅剩 README（{_file_summary(paths)}）")
        elif not paths:
            result.details.append("本地与 GitHub 侧一致：远端当前为空仓库")

    def _safe_curl(self, url: str, headers: list[str]) -> tuple[int, str]:
        """在线请求兜底：网络异常不得影响结论。"""
        try:
            return _curl_json(url, headers, self._proxy, 10)
        except Exception as e:  # noqa: BLE001 网络层异常不外抛
            logger.opt(exception=True).warning(f"[Probe] 在线请求异常: {url}")
            return -1, str(e)[:120]
