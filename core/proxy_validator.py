"""代理验证模块 - 验证代理配置和GitHub连接性"""

import re
import subprocess
import urllib.parse
from typing import Tuple


def validate_proxy_format(proxy: str) -> Tuple[bool, str]:
    """
    验证代理格式

    支持的格式:
    - http://host:port
    - https://host:port
    - socks5://host:port
    - 空字符串（无代理）

    返回: (是否有效, 错误信息)
    """
    if not proxy or not proxy.strip():
        return True, ""

    proxy = proxy.strip()

    # 尝试解析为URL
    try:
        parsed = urllib.parse.urlparse(proxy)

        # 检查scheme
        if parsed.scheme not in ("http", "https", "socks5"):
            return False, f"不支持的协议: {parsed.scheme}"

        # 检查hostname和port
        if not parsed.hostname:
            return False, "代理地址缺少主机名"

        if not parsed.port:
            return False, "代理地址缺少端口号"

        # 验证端口号有效性
        try:
            port = int(parsed.port)
            if port < 1 or port > 65535:
                return False, f"端口号无效: {port}"
        except ValueError:
            return False, f"端口号不是有效的数字: {parsed.port}"

        return True, ""

    except Exception as e:
        return False, f"代理地址格式错误: {str(e)}"


def test_github_connectivity(
    proxy: str | None = None, timeout: int = 5
) -> Tuple[bool, str]:
    """
    测试 GitHub 连接性

    参数:
        proxy: 代理地址 (可选)
        timeout: 超时时间 (秒)

    返回: (连接是否成功, 结果信息)
    """
    try:
        # 构建 curl 命令
        cmd = ["curl", "-I", "--connect-timeout", str(timeout), "https://github.com"]

        if proxy and proxy.strip():
            cmd.extend(["-x", proxy])

        # 执行 curl 命令
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 2
        )

        # 检查响应
        if result.returncode == 0:
            # 查找HTTP状态码
            for line in result.stdout.split("\n"):
                if line.startswith("HTTP/"):
                    status_match = re.search(r"HTTP/[\d.]+ (\d+)", line)
                    if status_match:
                        status_code = int(status_match.group(1))
                        if 200 <= status_code < 400:
                            return True, f"连接成功 (HTTP {status_code})"
                        else:
                            return False, f"服务器返回错误 (HTTP {status_code})"
            return True, "连接成功"

        elif result.returncode == 7:
            return False, "连接失败: 无法连接到主机"
        elif result.returncode == 28:
            return False, f"连接超时 (>{timeout}秒)"
        elif result.returncode == 35:
            return False, "SSL/TLS 握手失败"
        elif result.returncode == 56:
            return False, "接收数据失败"
        else:
            error_msg = result.stderr.strip()
            if error_msg:
                return False, f"连接失败: {error_msg[:100]}"
            return False, f"连接失败 (错误代码: {result.returncode})"

    except subprocess.TimeoutExpired:
        return False, f"连接超时 (>{timeout}秒)"
    except FileNotFoundError:
        return False, "curl 命令未找到"
    except Exception as e:
        return False, f"连接测试出错: {str(e)[:100]}"


def verify_proxy(proxy: str | None = None, timeout: int = 5) -> Tuple[bool, str]:
    """
    完整的代理验证流程

    参数:
        proxy: 代理地址
        timeout: 超时时间 (秒)

    返回: (验证是否通过, 结果信息)
    """
    # 如果代理为空或空字符串，返回成功
    if not proxy or not proxy.strip():
        return True, "无代理配置"

    # 格式验证
    valid, msg = validate_proxy_format(proxy)
    if not valid:
        return False, msg

    # 连接性测试
    return test_github_connectivity(proxy, timeout)
