# Git 多仓库管理器

基于 PySide6 + QFluentWidgets 的 Windows 桌面工具，用于批量管理本地 Git 仓库：扫描、更新、克隆、分支与版本切换一站式完成。采用 Fluent 暗色主题。

## 功能特性

### 仓库管理
- **目录扫描**：一键扫描指定目录下的所有 Git 仓库，展示分支、本地/远端版本、同步状态（↑领先 ↓落后）
- **批量/单个更新**：`pull --rebase` 策略，本地有已跟踪改动时自动 `--autostash` 暂存并在更新后恢复
- **克隆仓库**：输入 URL（自动识别 GitHub 短链等格式）选择目录克隆，实时日志输出
- **重命名项目文件夹**：右键菜单重命名，同步更新仓库列表与缓存
- **删除仓库**：两种模式可选 —— 完全删除（整个文件夹）或仅删除 `.git`（保留源码）

### 安全更新算法
更新前进行远端健康预检验，防止远端删库/归档损毁本地代码：
- 远端 404 / Repository not found → 判定删库，阻断并提示
- 远端分支仅剩 README/ARCHIVE 等说明文档（1~3 个）或空分支 → 判定归档，阻断更新
- 游离 HEAD、远端分支被删除 → 明确提示
- 网络类错误（超时/断连/SSL/代理故障）退避后自动重试
- rebase 冲突失败后自动 `rebase --abort` 恢复到更新前状态；上次中断残留的 rebase 目录自动清理
- pull 成功后完整性兜底检查，异常时自动回滚
- per-repo 互斥锁串行化 fetch/pull，避免 `index.lock` 冲突

### 分支与版本
- **分支管理**：查看/切换本地与远端分支
- **版本历史**：独立窗口查看本地/远端提交历史（SegmentedWidget 切换），差异提交橙色高亮，支持硬重置切换版本

### 设置
- 仓库目录、Git 代理（开关 + 地址 + 后台线程验证）、访问 Token 管理
- 忽略指定仓库的更新（右键菜单设置）

## 项目结构

```
├── main.py               # 主窗口（MSFluentWindow）与页面逻辑
├── app/                  # 配置读写、仓库缓存、日志初始化
├── core/                 # 业务核心（无 UI 依赖）
│   ├── git_runner.py     #   git 进程执行与生命周期管理
│   ├── git_service.py    #   Git 操作封装（状态检查、分支、重置）
│   ├── update_service.py #   更新算法（预检验/重试/自愈/完整性检查）
│   ├── scan_service.py   #   目录扫描
│   ├── repo_service.py   #   仓库增删改名
│   ├── clone_manager.py  #   克隆流程
│   └── proxy_validator.py#   代理可用性验证
├── ui/
│   ├── widgets/          # 仓库表格、日志、设置页、版本历史等组件
│   └── dialogs/          # 克隆/分支/删除/重命名等 Fluent 弹窗
├── workers/              # 后台线程（克隆、代理验证）
├── models/               # 数据模型
└── utils/                # subprocess 工具（隐藏窗口执行等）
```

## 环境与运行

- Python 3.12（uv 管理依赖）
- 依赖：`pyside6-fluent-widgets`、`loguru`

```bash
uv sync
uv run python main.py
```

## 打包

使用 Nuitka 打包为 Windows 可执行文件：

```bash
uv run python build.py
```

## 日志

运行日志写入程序目录下的 `git_manager.log`。
