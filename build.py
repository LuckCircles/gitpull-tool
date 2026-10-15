import os
import shutil
import subprocess
import sys
import time

os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))

OUTPUT_DIR = "releases"
OUTPUT_NAME = "GitPull-tool"


def _rmtree_with_retry(path: str, max_retries: int = 5, interval: float = 1.0) -> bool:
    """Windows 下带重试的目录删除（处理文件被占用导致的 WinError 32）。"""
    if not os.path.exists(path):
        return True
    for attempt in range(1, max_retries + 1):
        try:
            shutil.rmtree(path)
            return True
        except PermissionError as e:
            if attempt == max_retries:
                print(f"无法删除 {path}（已重试 {max_retries} 次）: {e}")
                return False
            print(f"删除失败（第 {attempt} 次，{e}），{interval}s 后重试...")
            time.sleep(interval)
    return False


def _cleanup_stale_artifacts() -> bool:
    """打包前清理上一次残留的 build/dist/exe，避免 WinError 32 导致 Nuitka 崩溃。"""
    stale_dirs = [
        os.path.join(OUTPUT_DIR, "main.build"),
        os.path.join(OUTPUT_DIR, "main.dist"),
        os.path.join(OUTPUT_DIR, f"{OUTPUT_NAME}.build"),
        os.path.join(OUTPUT_DIR, f"{OUTPUT_NAME}.dist"),
    ]
    stale_file = os.path.join(OUTPUT_DIR, f"{OUTPUT_NAME}.exe")

    ok = True
    for d in stale_dirs:
        if os.path.exists(d) and not _rmtree_with_retry(d):
            ok = False

    if os.path.exists(stale_file):
        for attempt in range(1, 6):
            try:
                os.unlink(stale_file)
                break
            except PermissionError as e:
                if attempt == 5:
                    print(f"无法删除旧 exe: {e}")
                    ok = False
                else:
                    time.sleep(1.0)

    return ok


def compile_with_nuitka():
    # 打包前清理残留：Windows 下文件句柄释放有延迟，必须重试
    if not _cleanup_stale_artifacts():
        print("警告：部分残留文件无法清理，打包可能失败。建议关闭资源管理器预览后重试。")

    main_script = "main.py"

    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--python-flag=-S",
        "--follow-imports",
        "--remove-output",
        f"--output-dir={OUTPUT_DIR}",
        # "--mingw64",
        "--msvc=latest",
        "--jobs=8",
        # "--disable-ccache",
        "--show-progress",
        "--windows-console-mode=disable",
        "--include-windows-runtime-dlls=yes",
        f"--main={main_script}",
        "--enable-plugins=pyside6",
        "--windows-icon-from-ico=icon.ico",
        "--onefile",
        # 单文件缓存解压目录：首次启动解压一次，后续启动复用
        # （否则每次启动都重新解压内嵌的 deno.exe，约 97MB）
        "--onefile-tempdir-spec={CACHE_DIR}/GitPull-tool",
        # 内嵌 tools/（deno + 下载脚本）：exe 单文件即可分发，
        # 运行时经 __compiled__.containing_dir/tools 定位（见 workers/release_worker.py）
        "--include-data-files=tools/deno.exe=tools/deno.exe",
        "--include-data-files=tools/download.ts=tools/download.ts",
        # markdown 库的扩展经 entry points 动态导入，
        # 静态 follow-imports 收集不到，需整包包含（Release 说明渲染）
        "--include-package=markdown",
        f"--output-filename={OUTPUT_NAME}",
        # f"--file-version={VERSION}",
        "--warn-implicit-exceptions",
        "--assume-yes-for-downloads",
        # "--include-data-dir=ImageView/resource=ImageView/resource",
        # "--include-data-dir=ispc=ispc",
    ]

    print("执行命令:")
    print(" ".join(command))

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        print(f"编译过程中出现错误: {e}")
        return False
    except FileNotFoundError:
        print("未找到python命令，请确保Python已正确安装并添加到PATH环境变量中")
        return False

    exe_path = os.path.join(OUTPUT_DIR, f"{OUTPUT_NAME}.exe")
    if not os.path.exists(exe_path):
        print(f"未找到编译产物: {exe_path}")
        return False

    print("编译成功完成!")
    print(f"产物（单文件，含内嵌 deno）: {exe_path}")
    return True


if __name__ == "__main__":
    sys.exit(0 if compile_with_nuitka() else 1)
