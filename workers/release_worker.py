"""Release 功能工作线程：列表加载（QThread）+ 资产下载（QProcess/deno）"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QThread, Signal
from loguru import logger

from core.release_service import extract_github_owner_repo, fetch_releases


def _build_tools_candidates() -> list[Path]:
    """构建 tools 目录候选列表（顺序即优先级）。"""
    candidates = [
        Path(__file__).resolve().parent.parent / "tools",  # 开发模式 / 打包后 dist 根
        Path(sys.executable).resolve().parent / "tools",   # onefile payload 所在目录
    ]
    compiled = globals().get("__compiled__")
    if compiled is not None:
        try:
            candidates.append(Path(compiled.containing_dir) / "tools")
        except Exception:  # noqa: BLE001 - 属性缺失等极端情况
            pass
    candidates.append(Path(sys.argv[0]).resolve().parent / "tools")  # 便携布局
    return candidates


_TOOLS_CANDIDATES = _build_tools_candidates()


def _locate_tools_dir() -> Path:
    """定位 tools 目录（deno + 下载脚本），按候选顺序取首个 deno 存在的目录。

    - 开发模式: 本文件在 <仓库>/workers/，父目录即仓库根 → <仓库>/tools
    - Nuitka onefile: 数据文件解压到缓存目录，模块 __file__、payload 的
      sys.executable、__compiled__.containing_dir 均位于该目录树下
    - 便携布局: 手动把 tools/ 放在 exe 旁（sys.argv[0] 所在目录）
    """
    deno_name = "deno.exe" if sys.platform == "win32" else "deno"
    for cand in _TOOLS_CANDIDATES:
        if (cand / deno_name).is_file():
            return cand
    logger.debug(
        f"[Release] 未定位到 {deno_name}，候选路径: {[str(c) for c in _TOOLS_CANDIDATES]}"
    )
    return _TOOLS_CANDIDATES[0]

_TOOLS_DIR = _locate_tools_dir()
DENO_EXE = _TOOLS_DIR / ("deno.exe" if sys.platform == "win32" else "deno")
DOWNLOAD_TS = _TOOLS_DIR / "download.ts"


class ReleaseListWorker(QObject):
    """Release 列表加载线程"""

    finished = Signal(list, str)  # (releases, error)

    def __init__(self, remote_url: str, token: str | None, proxy: str | None):
        super().__init__()
        self.remote_url = remote_url
        self.token = token
        self.proxy = proxy

    def run(self):
        owner_repo = extract_github_owner_repo(self.remote_url)
        if not owner_repo:
            self.finished.emit([], "非 GitHub 仓库或不支持的远程地址")
            return
        owner, repo = owner_repo
        releases, error = fetch_releases(owner, repo, self.token, self.proxy)
        if releases is None:
            self.finished.emit([], error)
            return
        if not releases:
            self.finished.emit([], f"{owner}/{repo} 没有任何 Release")
            return
        logger.info(f"[Release] {owner}/{repo} 加载 {len(releases)} 个版本")
        self.finished.emit(releases, "")


class AssetDownloadManager(QObject):
    """资产下载管理器（QProcess 调用 deno，逐行解析 JSON 进度）"""

    progress = Signal(int, int, str)  # (received, total, asset_name)
    finished = Signal(bool, str)  # (成功, 消息)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._asset_name = ""

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def start(self, asset_url: str, asset_name: str, output_path: str, token: str | None, proxy: str | None):
        """启动下载。已在下载中时忽略。"""
        if self.is_running:
            self.finished.emit(False, "已有下载任务进行中")
            return

        if not DENO_EXE.exists():
            self.finished.emit(
                False,
                f"未找到 deno: {DENO_EXE}\n候选路径:\n"
                + "\n".join(f"  {c}" for c in _TOOLS_CANDIDATES),
            )
            return
        if not DOWNLOAD_TS.exists():
            self.finished.emit(False, f"未找到下载脚本: {DOWNLOAD_TS}")
            return

        # 参数写临时 JSON 文件（避免命令行传参的引号/转义问题）
        param_file = Path(output_path).parent / f".download_{os.getpid()}.json"
        param_file.write_text(
            json.dumps({"url": asset_url, "output": output_path, "token": token}),
            encoding="utf-8",
        )

        self._asset_name = asset_name
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.MergedChannels)

        env = self._process.processEnvironment()
        if proxy:
            env.insert("HTTP_PROXY", proxy)
            env.insert("HTTPS_PROXY", proxy)
        self._process.setProcessEnvironment(env)

        self._process.readyReadStandardOutput.connect(self._on_output)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(
            lambda err: logger.warning(f"[Release] QProcess 错误: {err}")
        )

        logger.info(f"[Release] 开始下载: {asset_name} → {output_path}")
        self._process.start(
            str(DENO_EXE),
            ["run", "--allow-net", "--allow-read", "--allow-write", str(DOWNLOAD_TS), str(param_file)],
        )
        self._param_file = param_file

    def cancel(self):
        """取消下载（终止 deno 进程，脚本会清理半成品文件）。"""
        if self.is_running:
            logger.info("[Release] 用户取消下载")
            self._process.kill()

    # ---------- 内部 ----------
    def _on_output(self):
        if not self._process:
            return
        raw = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "progress":
                self.progress.emit(
                    int(msg.get("received", 0)),
                    int(msg.get("total", 0)),
                    self._asset_name,
                )
            elif kind == "error":
                self._error_message = str(msg.get("message", "下载失败"))

    def _on_finished(self, exit_code: int, _status):
        # 清理参数临时文件
        try:
            if getattr(self, "_param_file", None) and self._param_file.exists():
                self._param_file.unlink()
        except OSError:
            pass

        error = getattr(self, "_error_message", "")
        self._error_message = ""

        if exit_code == 0 and not error:
            logger.success(f"[Release] 下载完成: {self._asset_name}")
            self.finished.emit(True, f"{self._asset_name} 下载完成")
        else:
            msg = error or ("已取消" if exit_code != 0 else "下载失败")
            logger.warning(f"[Release] 下载结束: {self._asset_name} → {msg}")
            self.finished.emit(False, f"{self._asset_name}: {msg}")

        self._process = None
