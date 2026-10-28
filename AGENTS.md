# AGENTS.md

本文件约束在 `com.sora.unitymcp` 仓库内工作的 AI 助手，也包含**通过本包 MCP 工具操作 Unity** 时适用的规则。改动本包行为时，README 与 `Server~/src/main.py` 的 `instructions` 要同步更新。

## 目录与改动边界

- `Editor/` — 编辑器侧命令实现（工具的真正执行处）。
- `Runtime/` — 运行时程序集，尽量少改，避免影响打包。
- `Server~/` — 中心 Python MCP 服务端（多项目路由、写锁、日志、工具包装）。`~` 使 Unity 不导入该目录。
- `Server~/tests/` — 唯一的自动化测试入口：`cd Server~ && uv run --extra dev pytest`。仓库内没有 C# 测试程序集，不要新建测试脚手架。
- 新增/改名 Unity 侧资源要一并处理 `.meta`；不要为了跑通测试而修改业务项目的 `manifest.json`。

## 工具名到实现的映射

一个 MCP 工具由两层组成，两层名字相同但注册处不同：

1. 服务端包装：`Server~/src/services/tools/<tool>.py` 上的 `@mcp_for_unity_tool(...)`，负责参数校验、轮询与对客户端的返回格式。
2. Unity 侧执行：`Editor/Tools/<Class>.cs` 上的 `[McpForUnityTool("<tool>", AutoRegister = false)]`，负责真正调用 UnityEditor API。

`AutoRegister = false` 是有意为之：Unity 侧类型不参与自动暴露，只有服务端包装过的命令才对客户端可见。因此 `resolve_runtime_target`、`invoke_simulation_entry` 这类内部命令出现在实例的 `tools_available` 里，却不会作为 MCP 工具暴露——它们由 `play_simulation` 编排。

只读/写入的分类在服务端 `Server~/src/services/state/instance_locks.py` 的 `is_mutating_tool()`，按工具名 + `action` 判定，未知调用一律按写入处理。新增工具或新 `action` 时必须在这里补分类，否则默认要写锁。

## 操作 Unity 的固定流程

1. **会话开始先调一次 `health`**（只读）：确认已连接实例、项目路径、包版本与服务端版本是否一致、编译/Play 状态、命令队列、当前写锁持有者，以及每个实例最近 20 条断线/重连事件。别用 "hello"、"MCP 通吗" 之类的探活。
2. 多实例并存时先用 `mcpforunity://instances` 列出 `Name@hash`，再 `set_active_instance`，或在单次调用里显式传 `unity_instance`。服务端不会替你猜。
3. 要修改同一个项目（场景、资产、Play 状态、组件、Tag/Layer）之前，先 `claim_instance(action="claim", owner="任务名", ttl_seconds=300)`；完成后 `release`。锁的 owner 是服务端识别的真实会话身份，`owner` 参数只是显示标签；其他会话的写调用会收到 `unity_instance_busy` 和持有者标签，只读工具不受影响。活跃调用期间自动续租，TTL 只约束空闲租约。
4. 只读状态优先用资源而非工具：`mcpforunity://editor/state`（含 `compilation.is_compiling`、`compilation.is_domain_reload_pending`、`play_mode.is_playing`）、`mcpforunity://project/info`、`mcpforunity://scene/gameobject/{id}/components`、`mcpforunity://tests`。
5. 改完脚本用 `refresh_unity(compile="request", wait_for_ready=true)`，再用 `read_console(types=["error"])` 确认没有新增编译错误，之后才使用新类型或调用新组件。

## 长任务与中断恢复

- `manage_editor(action="play"|"stop", wait_for_completion=true)` 会等待状态真正切换、编译/导入结束与连接恢复；`timeout_seconds` 上限 120。
- 请求被客户端超时或域重载打断时，**用返回的 `operation_id` 调 `manage_editor(action="operation_status")` 查询，绝不重发 Play/Stop**。重复发送会造成二次切换。
- `play_simulation(entry, duration, capture, sample)` 只在停止状态下启动，只调用明确指定的公开入口（MenuItem 路径或 `Namespace.Type::Method` 形式的无参静态 public 方法），完成后自动退出并恢复编辑模式。不要用反射调私有方法、不要在运行时 `AddComponent` 业务管理器来"伪造模拟"。
- `sample_runtime` 只在已经处于 Play 时采样，不会自己进入 Play。Play 退出或域重载会中断采样并返回 `partial=true`，之后可用 `action="result"` + `sample_id` 取回已保存的部分结果。
- 客户端自身的 MCP 超时必须大于任务时长；做不到就按 `job_id` / `operation_id` 恢复查询，不要声称服务端能无限等待。

## 截图、采样与日志

- 所有截图输出到 `<项目>/Library/MCPScreenshots/`，返回绝对路径 `fullPath`，不写入 `Assets/`、不触发导入、不生成 `.meta`。`capture_view` 支持 `angles`（`front/side/back/top/45/closeup`）、`frames`+`interval` 连拍、`grid` 拼图、`include_overlay`（默认 true，临时改 Canvas 后在 `finally` 精确恢复）以及 `preset` 复用机位。机位预设资产在 `Assets/Settings/UnityMCP/CameraShotPresets.asset`。
- 声称截图/采样成功之前，必须实际读取返回路径的文件确认存在且内容符合预期，只看 `success: true` 不算验证。
- 服务端日志：Windows `%LOCALAPPDATA%/UnityMCP/Logs`，macOS `~/Library/Application Support/UnityMCP/Logs`，Linux `$XDG_STATE_HOME/UnityMCP/Logs`（未设置时 `~/.local/state/UnityMCP/Logs`），可用 `UNITY_MCP_LOG_DIR` 覆盖。`unity_mcp_server.log` 为运行日志（有界滚动），`unity_connection_events.jsonl` 为连接事件（断开/重连原因与恢复耗时），由 `health` 汇总，不上传。
- 读运行期探针日志用 `read_console(filter_text="[Probe]", since_timestamp=<进入 Play 的时间>)`，配合 `types` 与分页，不要一次拉取整个 Console。`since_timestamp` 由编辑器内的时间戳镜像支撑（Unity 的 LogEntry 没有时间戳），只覆盖本次域重载之后的日志，看响应的 `coverage_start_utc` / `possibly_incomplete` 判断可信范围。`read_console` 与 `manage_asset(action="search")` 的 `regex` 上限 200 字符、单次匹配 1 秒超时，非法表达式直接报错；`regex` 与 `filter_text` 同时使用时都要命中。Console 窗口的 Info 开关关闭时，`read_console` 会用镜像补齐普通日志并在消息里注明。

## 验证与交付

- 服务端改动必须跑完整套件，不能只跑新增文件：`cd Server~ && uv run --extra dev pytest`。
- C# 改动必须在真实 Unity 里编译并做一次实际 MCP 调用；Unity Console 中不应出现本次新增的编译错误。
- 临时验收脚本（例如放在试点项目 `Assets/Editor/` 下的一次性菜单）在 Edit 模式用 `manage_asset(action="delete")` 连同 `.meta` 一起删除，交付前不留残余。
- 未经用户明确要求，不 commit、不 push、不打 tag、不推进 `release`、不改版本号。发版通过 `release` 分支分发，tag 仅用于锁定/回滚。
- 环境或网络阻塞时，明确列出未验证的部分和证据，不要写"应该没问题"，也不要用发版绕过验证。
