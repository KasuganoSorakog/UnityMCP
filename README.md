# Sora Unity MCP

Unity 编辑器与 AI 助手之间的 MCP (Model Context Protocol) 桥接工具。

Fork 自 [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp)（MIT），在原版基础上做了多项目并行场景的稳定性强化：

- 中心 Server 多项目路由：僵尸会话清扫（45s 无心跳自动回收）、心跳 pong 携带 session_id + 按连接兜底保活
- 同 hash 顶替显式通知 + 客户端停止无效重连、hash 前缀匹配统一
- 工具调用超时优雅期重分类（`cancelled_before_execution` 可安全重试 / `execution_state_unknown` 先验证再重试 / 迟到成功结果原样返回）
- middleware LRU 上限、stdio 队列清理、64MB 接收上限、busy 可重试、非 loopback 远程访问警告
- **首次启动自动部署**：包内 `Server~/` 自带 Python 服务端，点"启动 Server"时自动部署到项目 `Tools/MCPForUnityServer`，无需手动拷贝

## 安装（Unity Package Manager）

在项目的 `Packages/manifest.json` 中添加（用 tag 锁定版本）：

```json
"com.sora.unitymcp": "https://github.com/KasuganoSorakog/UnityMCP.git#v20260921.0.0"
```

或在 Package Manager 窗口 `+` → `Add package from git URL...` 粘贴上面的 URL。

升级：把 `#` 后的 tag 换成新版本，删 `Packages/packages-lock.json` 中对应条目后让 Unity 重新解析（或直接改 lock 里的 hash）。

## 使用

1. 项目需要本机安装 [uv](https://docs.astral.sh/uv/)（包内会自动探测，也可在设置里指定路径）。
2. 打开 `Window → MCP for Unity`（或菜单内对应入口），点 **启动 Server**：
   - 首次启动：自动把包内 `Server~/` 部署到 `<项目>/Tools/MCPForUnityServer`（排除 `.venv` 等运行产物），随后正常拉起。
   - 后续启动：直接使用项目内的服务端目录。
   - 包升级后（服务端 `version` 变化）：下次启动自动覆盖同步源码，保留 `.venv`，`uv run --frozen` 会按新 `uv.lock` 自动补齐依赖。
3. 中心 Server 以项目为单位路由会话；多个 Unity 项目可并行开启，各自连接自己的中心 Server，互不顶替（同项目重复连接才触发顶替通知）。

## 目录结构

```
UnityMCP/
├── package.json      # UPM 包清单（com.sora.unitymcp）
├── Editor/           # 编辑器端 C#（桥接、UI、服务定位）
├── Runtime/          # 运行时 C#
└── Server~/          # Python MCP 服务端（~ 目录 Unity 不导入，随包分发）
    ├── pyproject.toml
    ├── src/
    ├── tests/
    └── vendor/
```

## 版本约定

- 包版本与服务端版本对齐，格式 `yyyyMMdd.0.0`（如 `20260921.0.0`）。
- 每次发布打 git tag `v<版本号>`，各项目通过 tag 锁定。

## 服务端开发自测

```bash
cd <项目>/Tools/MCPForUnityServer   # 或本仓库 Server~/
uv run pytest
```

## 许可

MIT。保留上游 CoplayDev 版权声明，见 [LICENSE](LICENSE)。
