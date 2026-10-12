"""Release 功能工作线程：列表加载（TaskExecutor 线程池）+ 资产下载（QProcess/deno）"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QObject, QProcess, Signal

from core.release_service import extract_github_owner_repo, fetch_releases


def fetch_release_list(
    remote_url: str, token: str | None, proxy: str | None
) -> tuple[list, str]:
    """获取 Release 列表（线程安全，经 TaskExecutor 在后台执行）。

    返回 (releases, error)：error 非空表示失败或无结果。
    """
    owner_repo = extract_github_owner_repo(remote_url)
    if not owner_repo:
        return [], "非 GitHub 仓库或不支持的远程地址"
    owner, repo = owner_repo
    releases, error = fetch_releases(owner, repo, token, proxy)
    if releases is None:
        return [], error
    if not releases:
        return [], f"{owner}/{repo} 没有任何 Release"
    logger.info(f"[Release] {owner}/{repo} 加载 {len(releases)} 个版本")
    return releases, ""


def _build_tools_candidates() -> list[Path]:
    """构建 tools 目录候选列表（顺序即优先级）。"""
    candidates = [
        Path(__file__).resolve().parent.parent / "tools",  # 开发模式 / 打包后 dist 根
        Path(sys.executable).resolve().parent / "tools",  # onefile payload 所在目录
    ]
    compiled = globals().get("__compiled__")
    if compiled is not None:
        try:
            candidates.append(Path(compiled.containing_dir) / "tools")
        except Exception:  # noqa: BLE001, S110 - 属性缺失等极端情况，跳过该候选即可
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


class AssetDownloadManager(QObject):
    """资产下载管理器（QProcess 调用 deno，逐行解析 JSON 进度）"""

    progress = Signal(int, int, str)  # (received, total, asset_name)
    finished = Signal(bool, str)  # (成功, 消息)
    paused = Signal(int, int)  # (received, total) 暂停成功，UI 切到"恢复"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._asset_name = ""
        self._ctx: dict | None = None  # 下载上下文（暂停/恢复/取消复用）
        self._last_received = 0
        self._total = 0
        self._paused = False
        self._cancelled = False

    @property
    def is_running(self) -> bool:
        return (
            self._process is not None and self._process.state() != QProcess.NotRunning
        )

    def start(
        self,
        asset_url: str,
        asset_name: str,
        output_path: str,
        token: str | None,
        proxy: str | None,
        start_offset: int = 0,
        total: int | None = None,
    ):
        """启动下载。已在下载中时忽略。start_offset>0 走断点续传（Range+append）。"""
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
            json.dumps(
                {
                    "url": asset_url,
                    "output": output_path,
                    "token": token,
                    "start": start_offset,
                    "total": total,
                }
            ),
            encoding="utf-8",
        )

        self._asset_name = asset_name
        self._ctx = {
            "url": asset_url,
            "name": asset_name,
            "output": output_path,
            "token": token,
            "proxy": proxy,
            "total": total,
        }
        self._last_received = start_offset
        self._total = total or 0
        self._paused = False
        self._cancelled = False
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.MergedChannels)

        env = self._process.processEnvironment()
        if proxy:
            env.insert("HTTP_PROXY", proxy)
            env.insert("HTTPS_PROXY", proxy)
        self._process.setProcessEnvironment(env)

        self._process.readyReadStandardOutput.connect(self._on_output)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_process_error)

        logger.info(f"[Release] 开始下载: {asset_name} → {output_path}")
        self._process.start(
            str(DENO_EXE),
            [
                "run",
                "--allow-net",
                "--allow-read",
                "--allow-write",
                str(DOWNLOAD_TS),
                str(param_file),
            ],
        )
        self._param_file = param_file

    def pause(self):
        """暂停下载：终止 deno 进程但保留半成品文件，供 resume() 续传。"""
        if self.is_running:
            logger.info("[Release] 用户暂停下载（保留半成品）")
            self._paused = True
            self._process.kill()
        # _on_finished 检测 _paused 后发 paused 信号

    def resume(self):
        """从半成品文件断点续传。仅在已暂停时有效。"""
        if not self._paused or not self._ctx:
            return
        ctx = self._ctx
        start = 0
        try:
            start = os.path.getsize(ctx["output"])
        except OSError:
            start = 0
        logger.info(f"[Release] 恢复下载: {ctx['name']} @ offset {start}")
        self._paused = False
        self.start(
            ctx["url"],
            ctx["name"],
            ctx["output"],
            ctx["token"],
            ctx["proxy"],
            start_offset=start,
            total=ctx.get("total"),
        )

    def cancel(self):
        """取消下载：终止进程并删除半成品文件。"""
        if not (self.is_running or self._paused):
            return  # 无进行中/已暂停的任务（如取消后关窗重复触发），静默忽略
        logger.info("[Release] 用户取消下载")
        self._cancelled = True
        if self.is_running:
            self._process.kill()
            # _on_finished 检测 _cancelled 后清理半成品并发 finished
        elif self._paused:
            # 已暂停（进程未运行）→ 直接清理并通知
            self._delete_partial()
            name = self._asset_name
            self._reset_state()
            self.finished.emit(False, f"{name}: 已取消")

    def _delete_partial(self):
        out = self._ctx.get("output") if self._ctx else None
        if out:
            try:
                os.remove(out)
            except OSError:
                pass

    def _reset_state(self):
        self._ctx = None
        self._last_received = 0
        self._total = 0
        self._paused = False
        self._cancelled = False

    # ---------- 内部 ----------
    def _on_process_error(self, err):
        """QProcess 错误回调。

        kill() 主动终止进程时 Qt 会发 Crashed —— 暂停/取消属预期流程，
        降级为 debug 不打扰用户；其余错误（启动失败等）才记 WARNING。
        """
        if self._paused or self._cancelled:
            logger.debug(f"[Release] QProcess 主动终止: {err}")
        else:
            logger.warning(f"[Release] QProcess 错误: {err}")

    def _on_output(self):
        if not self._process:
            return
        raw = bytes(self._process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
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
                received = int(msg.get("received", 0))
                total = int(msg.get("total", 0))
                self._last_received = received
                if total > 0:
                    self._total = total
                self.progress.emit(received, total, self._asset_name)
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

        # 暂停：保留半成品与上下文，通知 UI 切到"恢复"
        if self._paused:
            self._process = None
            logger.info(
                f"[Release] 已暂停: {self._asset_name} @ {self._last_received}"
            )
            self.paused.emit(self._last_received, self._total)
            return

        # 取消：删除半成品并通知
        if self._cancelled:
            self._delete_partial()
            name = self._asset_name
            self._reset_state()
            self._process = None
            logger.info(f"[Release] 已取消: {name}")
            self.finished.emit(False, f"{name}: 已取消")
            return

        if exit_code == 0 and not error:
            logger.success(f"[Release] 下载完成: {self._asset_name}")
            msg = f"{self._asset_name} 下载完成"
            self._reset_state()
            self._process = None
            self.finished.emit(True, msg)
        else:
            msg = error or "下载失败"
            logger.warning(f"[Release] 下载结束: {self._asset_name} → {msg}")
            self._reset_state()
            self._process = None
            self.finished.emit(False, f"{self._asset_name}: {msg}")
