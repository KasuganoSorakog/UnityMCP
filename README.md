# Sora Unity MCP

Unity 编辑器与 AI 助手之间的 MCP (Model Context Protocol) 桥接工具。

Fork 自 [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp)（MIT），在原版基础上做了多项目并行场景的稳定性强化：

- 中心 Server 多项目路由：僵尸会话清扫（45s 无心跳自动回收）、心跳 pong 携带 session_id + 按连接兜底保活
- 同 hash 顶替显式通知 + 客户端停止无效重连、hash 前缀匹配统一
- 工具调用超时优雅期重分类（`cancelled_before_execution` 可安全重试 / `execution_state_unknown` 先验证再重试 / 迟到成功结果原样返回）
- middleware LRU 上限、stdio 队列清理、64MB 接收上限、busy 可重试、非 loopback 远程访问警告
- **首次启动自动部署**：包内 `Server~/` 自带 Python 服务端，点"启动 Server"时自动部署到项目 `Tools/MCPForUnityServer`，无需手动拷贝
- **隐私**：上游的 telemetry 上报（指向 CoplayDev 服务器）在本 fork 中默认关闭，不会向任何第三方发送数据

## 前置要求

- Unity ≥ 2021.3
- 系统已安装 **git**（UPM 通过 git 拉取本包，且需能访问 github.com）
- 系统已安装 **[uv](https://docs.astral.sh/uv/)**（服务端运行时；未安装时面板会提示，按提示安装即可）

## 安装（Unity Package Manager）

在项目的 `Packages/manifest.json` 中添加（`#release` 跟踪正式发版分支）：

```json
"com.sora.unitymcp": "https://github.com/KasuganoSorakog/UnityMCP.git#release"
```

或在 Package Manager 窗口 `+` → `Add package from git URL...` 粘贴上面的 URL。

升级：在 Package Manager 选中本包点 **Update** 即可拿到最新正式版；服务端源码会在升级后自动同步并重启（保留 `.venv`）。需要锁定/回滚到历史版本时，把 `#release` 改成 `#v<版本号>` 即可。

## 使用

1. 打开 **Window → Unity MCP Window**（快捷键 Ctrl+Shift+M）。
2. 点 **启动 Server**：
   - 首次启动：自动把包内 `Server~/` 部署到 `<项目>/Tools/MCPForUnityServer`（排除 `.venv` 等运行产物），随后正常拉起。
   - 后续启动：直接使用项目内的服务端目录。
   - 包升级后（服务端 `version` 变化）：下次启动自动覆盖同步源码，`uv run --frozen` 会按新 `uv.lock` 自动补齐依赖。
3. 中心 Server 全机只有一个（占用 8080）；多个 Unity 项目可并行开启，都连接同一个中心 Server，按项目路由会话、互不顶替（同项目重复连接才触发顶替通知）。

### 首次启动的网络说明

首次启动时 `uv` 需要联网：自动下载 Python 3.11+ 运行时并按 `uv.lock` 安装依赖（PyPI）。网络受限时可用环境变量切换镜像：

```bash
# PyPI 镜像（清华）
setx UV_DEFAULT_INDEX "https://pypi.tuna.tsinghua.edu.cn/simple"
# uv 下载 Python 的镜像（南京大学）
setx UV_PYTHON_INSTALL_MIRROR "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
```

## 目录结构

```
UnityMCP/
├── package.json      # UPM 包清单（com.sora.unitymcp）
├── Editor/           # 编辑器端 C#（桥接、UI、服务定位）
├── Runtime/          # 运行时 C#
└── Server~/          # Python MCP 服务端（~ 目录 Unity 不导入，随包分发）
    ├── pyproject.toml
    ├── src/
    └── tests/
```

## 版本约定

- 包版本与服务端版本对齐，格式 `yyyyMMdd.N.0`（如 `20260921.3.0`）。
- 每次发布打 git tag `v<版本号>`，并把 `release` 分支推进到该 tag；各项目通过 `#release` 跟踪（点 Update 升级），需要固定版本时改用 `#v<版本号>`。
- `main` 为开发分支，不保证随时可分发；请勿跟踪 `#main`。

## 服务端开发自测

```bash
cd <项目>/Tools/MCPForUnityServer   # 或本仓库 Server~/
uv run --extra dev pytest
```

## 许可

MIT。保留上游 CoplayDev 版权声明，见 [LICENSE](LICENSE)。
