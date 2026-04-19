import os
import subprocess
import sys

os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))


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
        "--output-dir=releases",
        "--mingw64",
        "--jobs=8",
        # "--disable-ccache",
        "--show-progress",
        "--windows-console-mode=disable",
        f"--main={main_script}",
        "--enable-plugins=pyside6",
        "--windows-icon-from-ico=icon.ico",
        "--onefile",
         f"--output-filename=GitPull-tool",
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
        print("编译成功完成!")
    except subprocess.CalledProcessError as e:
        print(f"编译过程中出现错误: {e}")
    except FileNotFoundError:
        print("未找到python命令，请确保Python已正确安装并添加到PATH环境变量中")


if __name__ == "__main__":
    compile_with_nuitka()
