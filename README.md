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

## 运行时检查与安全操作

先调用 `health`，确认目标项目、包/Server 版本、编译与 Play 状态、命令队列和写锁。每个实例的 `connection_history` 包含最近 20 条断线/重连事件（含原因和重连耗时）；日志写入本地滚动文件，不上传。

- **写锁**：跨会话修改同一项目前调用 `claim_instance(action="claim", owner="任务名", ttl_seconds=300)`。活跃调用会续租；完成后 `release`。其他会话的写操作会收到持有者信息，只读工具不受影响。锁只协调 MCP 会话，不能代替人工协调 Unity 编辑器操作。
- **原子 Play/Stop**：`manage_editor(action="play"或"stop", wait_for_completion=true)` 等待状态真正切换；若请求被客户端中断，用 `action="operation_status"` 和返回的 `operation_id` 查询，不要重复发送 Play/Stop。
- **限定模拟**：`play_simulation(entry="Namespace.Type::Method", duration=10)` 只执行明确指定的公开静态无参入口，并按请求截图/采样后自动 Stop、恢复编辑模式。没有合适的业务入口时，不会通过私有方法或修改业务对象来伪造模拟。
- **机位截图**：`capture_view(target="层级路径/骨骼名", angles=["front", "45"], grid=true)` 使用非 XR 临时相机。`angles` 也接受逗号分隔字符串。可用 `preset` 复用已保存的固定或跟随机位（位置和旋转）；Scene 视角可通过 **Tools → UnityMCP → Camera Shots** 保存。跟随目标未激活时须显式 `include_inactive=true`。`include_overlay=true` 可带字幕层，但与 Game View 的 UI/世界深度可能不同。
- **运行时采样**：`sample_runtime(target=..., properties=["transform.position", "animator"], duration=10)` 返回统计、帧间尖峰和 Animator 状态事件。可用 `ComponentType.field` 读取公开或 `[SerializeField]` 字段，`animator.layer1.normalized_time` 指定层；原始样本默认不返回，必要时设 `include_samples=true`。Animator 未初始化或无 controller 时会给出警告，其他可读字段仍可采样。
- **中断恢复**：采样中退出 Play 或域重载时，结果标记 `partial=true`，可用返回的 `sampleId` 调用 `action="result"` 取回；它不会自行重新进入 Play 或重启采样。临时队列 busy 只在已有样本的结果轮询中等待，不会把新采样误当成已启动。

截图统一保存在 `<项目>/Library/MCPScreenshots/`，返回绝对路径，不导入生成的 PNG。`manage_scene(action="screenshot")` 也使用同步相机渲染；无可用相机时会明确失败并建议 `capture_view`。

### Kimi Code、Qoder、WorkBuddy 配置

通过 **Tools → UnityMCP → Clients** 下的对应菜单自动配置 `http://localhost:8080/mcp`，保留已有 MCP Server、权限和其他配置。Kimi 使用 `~/.kimi-code/mcp.json` 与 `config.toml`；Qoder 使用其项目设置；WorkBuddy 使用 `~/.workbuddy/mcp.json`。

自动配置会复用已有 Server 名称，并为上述 7 个只读工具添加允许规则。它不会关闭写操作审批，也不会覆盖无效 JSON/TOML。若 Qoder 已存在 `mcp__UnityMCP_8080__*` 等通配允许规则，该规则仍会绕过写操作审批，需要手动收窄；自动配置不会擅自删除。WorkBuddy 的审批文件格式未被本工具假定为已支持，因此不修改其审批文件。

日志位置：Windows `%LOCALAPPDATA%/UnityMCP/Logs`；macOS `~/Library/Application Support/UnityMCP/Logs`；Linux `$XDG_STATE_HOME/UnityMCP/Logs`（未设置时为 `~/.local/state/UnityMCP/Logs`）。可通过 `UNITY_MCP_LOG_DIR` 覆盖。`unity_mcp_server.log` 是运行日志，`unity_connection_events.jsonl` 是连接事件日志。

排查探针日志可用 `read_console(filter_text="[Probe]", since_timestamp=<进入 Play 的时间>)`，配合 `types` 和分页缩小范围，避免一次拉取整个 Console。`since_timestamp` 接受 UTC ISO-8601（不带时量按 UTC 读）或 unix 秒/毫秒，由编辑器内的**时间戳控制台镜像**提供（Unity 自身的 LogEntry 没有时间戳），因此只覆盖本次域重载之后、且包已加载的日志；响应里的 `coverage_start_utc` 与 `possibly_incomplete` 会说明这一点。`read_console` 与 `manage_asset(action="search")` 还支持 `regex`（大小写不敏感、≤200 字符、单次匹配 1 秒超时，非法或灾难回溯表达式直接报错，不返回半截结果）；`regex` 与 `filter_text` 同时给出时两者都要命中。若 Unity Console 窗口关掉了 Info 开关，`read_console` 会自动用时间戳镜像补齐被隐藏的普通日志，并在消息里注明有多少条来自镜像。

## 服务端开发自测

```bash
cd <项目>/Tools/MCPForUnityServer   # 或本仓库 Server~/
uv run --extra dev pytest
```

## 许可

MIT。保留上游 CoplayDev 版权声明，见 [LICENSE](LICENSE)。
