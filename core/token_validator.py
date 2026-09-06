"""令牌验证模块 - 验证 GitHub / Gitee 访问令牌的可用性"""

import json
import re
import subprocess
from typing import Tuple

# GitHub 令牌前缀（新版 PAT / OAuth / App 令牌）
_GITHUB_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")

_GITHUB_API = "https://api.github.com/rate_limit"
_GITEE_API = "https://gitee.com/api/v5/user"


def mask_token(token: str) -> str:
    """令牌脱敏（日志与界面显示用）。"""
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "*" * (len(token) - 8) + token[-4:]


def validate_token_format(token: str) -> Tuple[bool, str]:
    """
    验证令牌格式

    支持的格式:
    - GitHub 新版令牌: ghp_ / gho_ / ghu_ / ghs_ / ghr_ 前缀
    - Gitee 令牌 / GitHub 经典令牌: 40 位十六进制字符串

    返回: (是否有效, 错误信息)
    """
    token = (token or "").strip()
    if not token:
        return False, "令牌为空"

    if token.startswith(_GITHUB_PREFIXES):
        # 新版 GitHub 令牌：前缀 + 至少 36 位字母数字
        if re.fullmatch(r"gh[a-z]_[A-Za-z0-9]{36,}", token):
            return True, ""
        return False, "GitHub 令牌格式不完整（前缀后应至少 36 位字符）"

    if re.fullmatch(r"[0-9a-f]{40}", token):
        # Gitee 令牌与 GitHub 经典令牌同为 40 位十六进制
        return True, ""

    return False, "无法识别的令牌格式（支持 GitHub / Gitee 令牌）"


def _curl_json(
    url: str, headers: list[str], proxy: str | None, timeout: int
) -> Tuple[int, str]:
    """
    执行 curl 请求并返回 (HTTP 状态码, 响应体)

    失败时返回 (-1, 错误信息)。
    """
    cmd = [
        "curl",
        "-s",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(timeout + 5),
        "-w",
        "\n%{http_code}",  # 最后一行附加状态码
        *headers,
        url,
    ]
    if proxy and proxy.strip():
        cmd.extend(["-x", proxy.strip()])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 7, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        return -1, f"请求超时 (>{timeout}秒)"
    except FileNotFoundError:
        return -1, "curl 命令未找到"
    except Exception as e:
        return -1, f"请求出错: {str(e)[:100]}"

    if result.returncode == 7:
        return -1, "无法连接到服务器（检查网络或代理）"
    if result.returncode == 28:
        return -1, f"连接超时 (>{timeout}秒)"
    if result.returncode == 35:
        return -1, "SSL/TLS 握手失败"
    if result.returncode != 0:
        err = result.stderr.strip()
        return -1, f"请求失败: {err[:100] if err else f'错误代码 {result.returncode}'}"

    lines = result.stdout.strip().rsplit("\n", 1)
    if len(lines) != 2 or not lines[1].isdigit():
        return -1, "响应格式异常"
    return int(lines[1]), lines[0]


def verify_github_token(
    token: str, proxy: str | None = None, timeout: int = 10
) -> Tuple[bool, str]:
    """
    验证 GitHub 令牌（rate_limit 接口不消耗配额）

    返回: (验证是否通过, 结果信息)
    """
    status, body = _curl_json(
        _GITHUB_API, ["-H", f"Authorization: Bearer {token}"], proxy, timeout
    )
    if status == -1:
        return False, body

    if status == 200:
        # 解析配额信息作为附加反馈
        try:
            data = json.loads(body)
            remaining = data.get("rate", {}).get("remaining", "?")
            limit = data.get("rate", {}).get("limit", "?")
            return True, f"GitHub 令牌有效（API 配额 {remaining}/{limit}）"
        except (json.JSONDecodeError, AttributeError):
            return True, "GitHub 令牌有效"
    if status == 401:
        return False, "GitHub 令牌无效或已过期 (HTTP 401)"
    if status == 403:
        return False, "GitHub 拒绝访问 (HTTP 403)，令牌可能已失效"
    return False, f"GitHub 返回异常状态 (HTTP {status})"


def verify_gitee_token(
    token: str, proxy: str | None = None, timeout: int = 10
) -> Tuple[bool, str]:
    """
    验证 Gitee 令牌

    返回: (验证是否通过, 结果信息)
    """
    from urllib.parse import quote

    url = f"{_GITEE_API}?access_token={quote(token)}"
    status, body = _curl_json(url, [], proxy, timeout)
    if status == -1:
        return False, body

    if status == 200:
        try:
            data = json.loads(body)
            login = data.get("login")
            if login:
                return True, f"Gitee 令牌有效（账号: {login}）"
        except (json.JSONDecodeError, AttributeError):
            pass
        return True, "Gitee 令牌有效"
    if status == 401:
        return False, "Gitee 令牌无效或已过期 (HTTP 401)"
    return False, f"Gitee 返回异常状态 (HTTP {status})"


def verify_token(
    token: str | None, proxy: str | None = None, timeout: int = 10
) -> Tuple[bool, str]:
    """
    完整的令牌验证流程

    按前缀优先判定平台：GitHub 新版前缀只验证 GitHub；
    40 位十六进制先验证 Gitee，失败后回退验证 GitHub 经典令牌。

    返回: (验证是否通过, 结果信息)
    """
    token = (token or "").strip()
    if not token:
        return False, "未设置令牌"

    valid, msg = validate_token_format(token)
    if not valid:
        return False, msg

    masked = mask_token(token)

    if token.startswith(_GITHUB_PREFIXES):
        ok, message = verify_github_token(token, proxy, timeout)
        platform = "GitHub"
    else:
        # 40 位十六进制：Gitee 优先，失败回退 GitHub 经典令牌
        ok, message = verify_gitee_token(token, proxy, timeout)
        platform = "Gitee"
        if not ok and "无效或已过期" in message:
            ok, message = verify_github_token(token, proxy, timeout)
            platform = "GitHub"

    if ok:
        return True, f"{platform} · {message}（令牌 {masked}）"
    return False, f"{platform} · {message}（令牌 {masked}）"
