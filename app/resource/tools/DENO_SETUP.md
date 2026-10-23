# Deno 运行时获取说明

> 本文件随仓库入库；`app/resource/tools/deno.exe` 二进制（约 97 MB）**不入库**（见 `.gitignore` 的 `app/resource/tools/deno.exe` 与 `*.exe`），需在本地自行下载放置。

## 为什么需要 Deno

Release 资产下载功能依赖 Deno 运行时：
- `app/service/release_worker.py` 通过 `QProcess` 调用 `app/resource/tools/deno.exe` 执行 `app/resource/tools/download.ts` 下载脚本，逐行解析 JSON 进度。
- `build.py` 打包时用 `--include-data-files=app/resource/tools/deno.exe=app/resource/tools/deno.exe` 把 deno 内嵌进 onefile exe，发布包自带、无需用户另装。

因此**开发环境**必须本地备好 `app/resource/tools/deno.exe`，否则 Release 下载会报「未找到 deno」。

## 放置位置

| 平台 | 路径 |
|------|------|
| Windows | `app/resource/tools/deno.exe` |
| macOS / Linux | `app/resource/tools/deno` |

即与 `app/resource/tools/download.ts` 同目录。

## 下载方式（任选其一）

### 方式 A：直接下载最新版 zip（推荐，免安装）

Windows x64 直链：

```
https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip
```

步骤：
1. 下载上述 zip。
2. 解压，得到 `deno.exe`。
3. 复制为 `app/resource/tools/deno.exe`。

其他平台资产名（替换 URL 末尾的文件名）：
- macOS Apple Silicon：`deno-aarch64-apple-darwin.zip`
- macOS Intel：`deno-x86_64-apple-darwin.zip`
- Linux x64：`deno-x86_64-unknown-linux-gnu.zip`

固定版本：到 https://github.com/denoland/deno/releases 选具体版本，把 URL 中的 `latest` 换成 `tag/<版本号>`，例如 `releases/tag/v2.1.4/download/deno-x86_64-pc-windows-msvc.zip`。

### 方式 B：官方安装器（装到系统 PATH）

PowerShell：
```powershell
irm https://deno.land/install.ps1 | iex
```
或 winget：
```winget install DenoLand.Deno
```

装好后，仍需把 `deno.exe` 复制一份到 `app/resource/tools/deno.exe`（打包脚本只认 `app/resource/tools/` 下的文件，不读 PATH）。

## 校验

在项目根目录执行：
```bash
app/resource/tools/deno.exe --version
```
应输出类似 `deno 2.x.x` 的版本号即正常。

## 备注

- `app/resource/tools/download.ts` 是入库的下载脚本（TypeScript），不要删除。
- 升级 deno：重复「方式 A」覆盖 `app/resource/tools/deno.exe` 即可；本项目不锁定 deno 版本，用最新稳定版即可。
- 打包发布（`uv run python build.py`）前务必确认 `app/resource/tools/deno.exe` 已就位，否则 Nuitka 会因找不到该数据文件报错。
