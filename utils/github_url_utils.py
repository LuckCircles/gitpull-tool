"""
GitHub URL 标准化工具。

将任意形式的 GitHub 仓库地址统一转换为：
    https://github.com/<owner>/<repo>.git

核心思路：从输入中提取 owner/repo，然后重建标准 URL。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# 核心正则 —— 从任意文本中捕捉 GitHub 仓库的 owner/repo
# ---------------------------------------------------------------------------

# 匹配路径中出现的 "github.com/owner/repo" 形式（.git 可选，斜杠之后带 git 扩展的截断）
_GITHUB_COM_PATH = re.compile(
    r"github\.com/"  # github.com/
    r"([\w._-]+)"  # owner (group 1)
    r"/"
    r"([\w._-]+?)"  # repo  (group 2)
    r"(?:\.git)?"  # 可选 .git（不捕获）
    r"(?=/|$|\s|'|\"|\\|[?#])",  # 以 / $ \s 引号 # ? 终止
)

# 匹配 SSH 风格: git@github.com:owner/repo.git
_SSH_GITHUB = re.compile(
    r"git@github\.com:"
    r"([\w._-]+)"  # owner
    r"/"
    r"([\w._-]+?)"  # repo
    r"(?:\.git)?(?=$|\s|'|\"|\\|[?#])",
)

# 匹配 git clone 命令：找到 git clone 之后第一个像 Git URL 的参数
_GIT_CLONE_RE = re.compile(
    r"git\s+clone\s+"
    r"(?:--[-\w]+\s+(?:\S+\s+)?)*"  # 跳过 --flag [value] 选项
    r"['\"]?"  # 可选引号
    r"("  # group 1: 实际 URL/SSH 地址
    r"https?://\S+?"  # HTTPS URL
    r"|"
    r"git@\S+?"  # SSH 地址
    r")"
    r"['\"]?"  # 可选引号
    r"(?:\s|$)",  # 以空格或结尾终止
)

# owner/repo 合法性（GitHub 用户名规则 + 常见仓库名规则）
_VALID_OWNER = re.compile(r"^[\w](?:[\w.-]*[\w])?$")  # GitHub 用户名
_VALID_REPO = re.compile(r"^[\w](?:[\w._-]*[\w])?$")  # 仓库名（允许 . _ -）


def normalize_github_url(raw: str) -> str | None:
    """
    将任意 GitHub 仓库地址标准化为 https://github.com/<owner>/<repo>.git。

    无法识别时返回 None。
    """
    if not raw or not isinstance(raw, str):
        return None

    text = raw.strip()

    # 1. 如果是 git clone 命令，先提取参数
    m = _GIT_CLONE_RE.search(text)
    if m:
        text = m.group(1)

    # 2. URL 解码一层（处理中转代理编码的 URL）
    text = unquote(text)

    # 3. 解开嵌套 URL 包装（例如 ghfast.top/https://github.com/...）
    text = _unwrap_nested_url(text)

    # 4. 尝试各种提取方式
    owner, repo = _extract_owner_repo(text)
    if owner and repo and _VALID_OWNER.match(owner) and _VALID_REPO.match(repo):
        # 二次校验：排除非 GitHub 源
        if not _has_conflicting_domain(text):
            return f"https://github.com/{owner}/{repo}.git"

    return None


def _has_conflicting_domain(text: str) -> bool:
    """如果文本中明确出现了非 GitHub 的 Git 托管服务域名，返回 True。"""
    non_github = re.compile(
        r"(?:^|://|\.)"
        r"(gitlab|gitee|bitbucket|gitcode|coding\.net)"
        r"(?:\.(?:com|org|net|cn|io))"
        r"(?:/|$|:|\.)",
        re.IGNORECASE,
    )
    return bool(non_github.search(text))


def _unwrap_nested_url(text: str) -> str:
    """处理 ghfast.top/https://github.com/... 这类嵌套 URL。"""
    # 如果文本本身就是一个 URL，解析它
    parsed = urlparse(text if "://" in text else f"https://{text}")
    path = parsed.path.lstrip("/")

    # 情况：path 以 https:/ 或 http:/ 开头（嵌套 URL）
    if re.match(r"https?:/(?!($|/))", path):
        # 重建内层 URL
        inner = parsed.path.lstrip("/") + (f"?{parsed.query}" if parsed.query else "")
        inner = inner + (f"#{parsed.fragment}" if parsed.fragment else "")
        return inner

    # 情况：整个 text 就是一个带 query/fragment 的 GitHub URL
    # 先尝试直接提取 owner/repo
    return text


def _extract_owner_repo(text: str) -> tuple[str | None, str | None]:
    """
    从文本中提取 owner 和 repo。
    按优先级尝试：github.com 路径 → SSH 格式 → URL 路径通用提取。
    """

    # A. github.com/owner/repo 标准路径
    m = _GITHUB_COM_PATH.search(text)
    if m:
        owner = m.group(1)
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return owner, repo

    # B. SSH: git@github.com:owner/repo
    m = _SSH_GITHUB.search(text)
    if m:
        owner = m.group(1)
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return owner, repo

    # C. 通用路径提取
    owner, repo = _extract_from_generic_path(text)
    if owner and repo and repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def _extract_from_generic_path(text: str) -> tuple[str | None, str | None]:
    """从通用路径中提取 owner/repo。"""
    # 先去掉 scheme://host 前缀和查询/片段
    # 提取所有可能包含 owner/repo 的路径段
    cleaned = text

    # 如果有 :// 的 scheme，去掉
    if "://" in cleaned:
        parsed = urlparse(cleaned)
        cleaned = parsed.path.lstrip("/") + parsed.params

    # 如果有 @ 前缀（SSH user@host），取 : 之后的部分；否则取整个
    if "@" in cleaned:
        idx = cleaned.rfind("@")
        cleaned = cleaned[idx + 1 :]

    # 在剩余文本中搜索 owner/repo 模式
    # 格式: segment/segment 其中 segment 是合法 GitHub 用户名/仓库名
    pattern = re.compile(
        r"(?:^|/|:\s*)"  # 起始边界
        r"([\w](?:[\w.-]*[\w])?)"  # owner
        r"/"
        r"([\w](?:[\w._-]*[\w])?)"  # repo
        r"(?:\.git)?"
        r"(?=/|$|\s|'|\"|\\|[?#])",  # 结束边界
    )

    # 找所有匹配，取最佳候选
    for m in pattern.finditer(cleaned):
        owner = m.group(1)
        repo = m.group(2)
        # 排除常见非仓库路径
        if owner.lower() in (
            "api",
            "v1",
            "v2",
            "v3",
            "repos",
            "users",
            "orgs",
            "search",
            "notifications",
            "settings",
            "login",
            "logout",
        ):
            continue
        if repo.lower() in (
            "tree",
            "blob",
            "commits",
            "releases",
            "tags",
            "issues",
            "pulls",
            "wiki",
            "actions",
        ):
            continue
        if _VALID_OWNER.match(owner) and _VALID_REPO.match(repo):
            return owner, repo

    return None, None


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    TEST_CASES = [
        # ---- 标准 GitHub URL ----
        ("https://github.com/owner/repo", "https://github.com/owner/repo.git"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        ("http://github.com/owner/repo", "https://github.com/owner/repo.git"),
        ("github.com/owner/repo", "https://github.com/owner/repo.git"),
        ("github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        # ---- SSH ----
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo.git"),
        ("git@github.com:owner/repo", "https://github.com/owner/repo.git"),
        # ---- git clone 命令 ----
        (
            "git clone https://github.com/owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        (
            "git clone git@github.com:owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        (
            "git clone --depth 1 https://github.com/a/b.git",
            "https://github.com/a/b.git",
        ),
        # ---- 常见镜像/代理 ----
        ("https://githubfast.com/owner/repo.git", "https://github.com/owner/repo.git"),
        (
            "https://ghfast.top/https://github.com/owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        (
            "https://gitclone.com/github.com/owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        (
            "https://wget.la/https://github.com/owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        # ---- 带 query / fragment ----
        (
            "https://github.com/owner/repo?tab=readme",
            "https://github.com/owner/repo.git",
        ),
        (
            "https://github.com/owner/repo.git#readme",
            "https://github.com/owner/repo.git",
        ),
        # ---- 特殊仓库名 ----
        (
            "https://github.com/user123/my-repo",
            "https://github.com/user123/my-repo.git",
        ),
        (
            "https://github.com/user/repo_with_underscore",
            "https://github.com/user/repo_with_underscore.git",
        ),
        ("https://github.com/user/repo.v2.git", "https://github.com/user/repo.v2.git"),
        # ---- sub-group / 多级路径（非 GitHub 标准，但可处理）----
        (
            "https://github.com/owner/repo/tree/main",
            "https://github.com/owner/repo.git",
        ),
        # ---- 无法识别 ----
        ("https://gitlab.com/owner/repo.git", None),
        ("https://gitee.com/owner/repo.git", None),
        ("not a url", None),
        ("", None),
        (None, None),
        ("   ", None),
    ]

    passed = 0
    failed = 0

    print("=" * 80)
    print("GitHub URL Normalizer — Test Suite")
    print("=" * 80)

    for raw, expected in TEST_CASES:
        result = normalize_github_url(raw)
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1

        # 截断显示
        display_input = str(raw)[:70] if raw else repr(raw)
        print(f"[{status}] {display_input}")
        if status == "FAIL":
            print(f"       expected: {expected}")
            print(f"       got:      {result}")

    print("=" * 80)
    print(f"Total: {passed + failed}  |  Passed: {passed}  |  Failed: {failed}")
    print("=" * 80)
