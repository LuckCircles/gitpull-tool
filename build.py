import os
import subprocess
import sys

os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))

OUTPUT_DIR = "releases"
OUTPUT_NAME = "GitPull-tool"


def compile_with_nuitka():
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
        #"--mingw64",
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
        #（否则每次启动都重新解压内嵌的 deno.exe，约 97MB）
        "--onefile-tempdir-spec={CACHE_DIR}/GitPull-tool",
        # 内嵌 tools/（deno + 下载脚本）：exe 单文件即可分发，
        # 运行时经 __compiled__.containing_dir/tools 定位（见 workers/release_worker.py）
        "--include-data-files=tools/deno.exe=tools/deno.exe",
        "--include-data-files=tools/download.ts=tools/download.ts",
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
