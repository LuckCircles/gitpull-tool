"""Token 安全存储模块 — 系统密钥环（keyring 可选依赖）优先，本地 secrets 文件回退。

设计要点：
- 无 UI 依赖，可被 main.py 与测试脚本独立调用；
- 首次 get_token() 时自动迁移：config.json 中的明文 token → 安全存储，
  迁移成功后从 config.json 删除 "token" 键（幂等，可安全重复调用）；
- 所有日志只记录事件，绝不输出 token 明文。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from loguru import logger

from app.config import CONFIG_FILE, get_app_data_dir, load_config, save_config

# keyring 为可选依赖：未安装或后端不可用时回退本地 secrets 文件
try:
    import keyring

    _KEYRING_OK: bool = True
except Exception:  # ImportError 及后端初始化异常均视为不可用
    keyring = None
    _KEYRING_OK = False

# 串行化读写与迁移，避免并发调用交错导致 token 丢失
_lock = threading.RLock()


def _secrets_path() -> Path:
    """本地回退存储文件路径（与应用数据同目录）。"""
    return get_app_data_dir() / "secrets.json"


def _keyring_read() -> str:
    """从系统密钥环读取 token，失败/不存在返回空串。"""
    if not _KEYRING_OK:
        return ""
    try:
        value = keyring.get_password(TokenStore.SERVICE_NAME, TokenStore.ACCOUNT)
        return str(value or "").strip()
    except Exception:
        # 密钥环后端不可用时静默回退本地文件
        return ""


def _keyring_write(token: str) -> bool:
    """写入系统密钥环，返回是否成功。"""
    if not _KEYRING_OK:
        return False
    try:
        keyring.set_password(TokenStore.SERVICE_NAME, TokenStore.ACCOUNT, token)
        return True
    except Exception as e:
        logger.warning(f"密钥环写入失败，回退本地令牌文件: {str(e)}")
        return False


def _keyring_delete() -> None:
    """从系统密钥环删除 token（不存在或失败均静默）。"""
    if not _KEYRING_OK:
        return
    try:
        keyring.delete_password(TokenStore.SERVICE_NAME, TokenStore.ACCOUNT)
    except Exception:
        pass


def _file_read() -> str:
    """从本地 secrets 文件读取 token，失败/不存在返回空串。"""
    path = _secrets_path()
    try:
        if not path.exists():
            return ""
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return ""
        return str(data.get("token") or "").strip()
    except Exception as e:
        logger.warning(f"读取本地令牌文件失败: {str(e)}")
        return ""


def _file_write(token: str) -> bool:
    """写入本地 secrets 文件（尽量 0o600 权限），返回是否成功。"""
    path = _secrets_path()
    try:
        payload = json.dumps({"token": token}, ensure_ascii=False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(path), flags, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        # Windows 上 chmod 仅影响只读位，POSIX 上收紧为属主可读写
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        return True
    except Exception as e:
        logger.error(f"写入本地令牌文件失败: {str(e)}")
        return False


def _file_delete() -> None:
    """删除本地 secrets 文件（不存在/失败均静默）。"""
    path = _secrets_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"删除本地令牌文件失败: {str(e)}")


def _read_stored() -> str:
    """按优先级读取已存储的 token：keyring → 本地文件。"""
    token = _keyring_read()
    if token:
        return token
    return _file_read()


def _write_stored(token: str) -> bool:
    """按优先级写入 token：keyring 成功则清掉本地文件副本，否则写本地文件。"""
    if _keyring_write(token):
        # 避免双份存储
        _file_delete()
        return True
    return _file_write(token)


def _migrate_from_config() -> None:
    """将 config.json 中的明文 token 迁入安全存储（幂等）。

    - 迁移源存在（config.json 含 "token" 键）且安全存储为空 → 写入存储；
    - 写入校验通过后从 config.json 删除 "token" 键（保留其他键）；
    - 写入失败则保留 "token" 键，下次调用重试，保证 token 不丢失。
    """
    try:
        if not CONFIG_FILE.exists():
            return
        config = load_config()
        if "token" not in config:
            return

        legacy_token = str(config.get("token") or "").strip()
        if legacy_token and not _read_stored():
            # 写入后回读校验，确保迁移成功才允许删除明文
            if _write_stored(legacy_token) and _read_stored() == legacy_token:
                logger.info("检测到 config.json 中的明文令牌，已自动迁移至安全存储")
            else:
                logger.error("令牌迁移写入失败，保留 config.json 中的明文令牌待重试")
                return

        # 无论值是否为空，安全存储就绪后都应移除明文键
        config.pop("token", None)
        save_config(config)
        logger.info("已从 config.json 移除明文令牌键")
    except Exception as e:
        logger.warning(f"令牌迁移失败: {str(e)}")


class TokenStore:
    """访问令牌安全存储（keyring 优先，本地 secrets 文件回退）。

    使用方式：
        TokenStore.get_token()            # 读取（必要时自动迁移 config.json 明文）
        TokenStore.set_token(token)       # 保存（空串等价于清除）
        TokenStore.clear_token()          # 清除
    """

    SERVICE_NAME = "git-tool"
    ACCOUNT = "access_token"

    @classmethod
    def get_token(cls) -> str:
        """读取访问令牌；不存在返回空串。首次调用自动迁移 config.json 明文。"""
        with _lock:
            _migrate_from_config()
            return _read_stored()

    @classmethod
    def set_token(cls, token: str) -> None:
        """保存访问令牌（空串/None 视为清除）。"""
        token = str(token or "").strip()
        if not token:
            cls.clear_token()
            return
        with _lock:
            if not _write_stored(token):
                raise OSError("令牌写入失败（密钥环与本地文件均不可用）")

    @classmethod
    def clear_token(cls) -> None:
        """清除已存储的访问令牌（keyring 与本地文件一并清理）。"""
        with _lock:
            _keyring_delete()
            _file_delete()
