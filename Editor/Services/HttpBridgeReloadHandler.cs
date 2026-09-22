using System;
using System.Threading;
using System.Threading.Tasks;
using MCPForUnity.Editor.Constants;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services.Transport;
using MCPForUnity.Editor.Windows;
using UnityEditor;

namespace MCPForUnity.Editor.Services
{
    /// <summary>
    /// Records HTTP transport reload state without automatically reconnecting after reload.
    /// </summary>
    [InitializeOnLoad]
    internal static class HttpBridgeReloadHandler
    {
        private static readonly SynchronizationContext EditorSynchronizationContext;

        static HttpBridgeReloadHandler()
        {
            EditorSynchronizationContext = SynchronizationContext.Current;
            AssemblyReloadEvents.beforeAssemblyReload += OnBeforeAssemblyReload;
            AssemblyReloadEvents.afterAssemblyReload += OnAfterAssemblyReload;

            // 主线程预热包版本缓存：CheckRunningServerVersion 会在线程池里调 GetPackageVersion，
            // 其内部 PackageInfo.FindForAssembly 仅主线程可用；此处（[InitializeOnLoad]，必在主线程、
            // 且早于一切版本判定）预热后，后续任意线程直接命中缓存。
            _ = AssetPathUtility.GetPackageVersion();
        }

        /// <summary>
        /// Asset Import Worker 等批处理进程不得持有 MCP 连接：
        /// 它们会用相同 project hash 注册并顶掉编辑器会话，导致命令路由错乱。
        /// 设置 UNITY_MCP_ALLOW_BATCH 可强制放行（与 StdioBridgeHost 的守卫保持一致）。
        /// </summary>
        internal static bool BatchModeBlocked()
        {
            return UnityEngine.Application.isBatchMode &&
                   string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("UNITY_MCP_ALLOW_BATCH"));
        }

        private static void OnBeforeAssemblyReload()
        {
            if (BatchModeBlocked())
            {
                return;
            }

            try
            {
                var transport = MCPServiceLocator.TransportManager;
                bool shouldResume = transport.IsRunning(TransportMode.Http);

                if (shouldResume)
                {
                    EditorPrefs.SetBool(EditorPrefKeys.ResumeHttpAfterReload, true);
                    if (McpProjectSettings.GetAutoReconnect())
                    {
                        EditorPrefs.DeleteKey(EditorPrefKeys.ManualReconnectRequired);
                    }
                    else
                    {
                        EditorPrefs.SetBool(EditorPrefKeys.ManualReconnectRequired, true);
                    }
                }
                else
                {
                    EditorPrefs.DeleteKey(EditorPrefKeys.ResumeHttpAfterReload);
                }

                if (shouldResume)
                {
                    // 域重载前取消所有 pending 命令（对齐 StdioBridgeHost.Stop 的模式），
                    // 避免旧命令在新域中继续执行或悬挂到超时
                    try { TransportCommandDispatcher.CancelAllPending("domain_reload"); } catch { }

                    var stopTask = transport.StopAsync(TransportMode.Http);
                    // 域重载前有限同步等待旧连接关闭，避免新域注册时旧会话仍存活造成瞬时双会话。
                    // StopAsync 内部全程 ConfigureAwait(false)，无主线程死锁风险；超时则放弃，由服务端顶替逻辑兜底。
                    try { stopTask.Wait(TimeSpan.FromMilliseconds(750)); } catch { }
                    stopTask.ContinueWith(t =>
                    {
                        if (t.IsFaulted && t.Exception != null)
                        {
                            McpLog.Warn($"Error stopping MCP bridge before reload: {t.Exception.GetBaseException().Message}");
                        }
                    }, TaskScheduler.Default);
                }
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to evaluate HTTP bridge reload state: {ex.Message}");
            }
        }

        /// <summary>
        /// 本工程 Session 是否已被同工程另一实例顶替（项目级标记，由 WebSocketTransportClient 写入）。
        /// </summary>
        internal static bool IsSuperseded()
        {
            try
            {
                return EditorPrefs.GetBool(EditorPrefKeys.SupersededPrefix + ProjectIdentityUtility.GetProjectHash(), false);
            }
            catch
            {
                return false;
            }
        }

        private static void OnAfterAssemblyReload()
        {
            if (BatchModeBlocked())
            {
                return;
            }

            if (IsSuperseded())
            {
                McpLog.Warn("本工程 Session 已被同一工程的另一个 Unity 实例顶替（同项目多开？），跳过域重载后的自动重连，请在 MCP 面板手动连接。");
                return;
            }

            bool resume = false;
            try
            {
                // Only resume HTTP if it is still the selected transport.
                bool useHttp = McpProjectSettings.GetUseHttpTransport();
                resume = useHttp && EditorPrefs.GetBool(EditorPrefKeys.ResumeHttpAfterReload, false);
                if (resume)
                {
                    EditorPrefs.DeleteKey(EditorPrefKeys.ResumeHttpAfterReload);
                }
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to read HTTP bridge reload flag: {ex.Message}");
                resume = false;
            }

            if (!resume)
            {
                return;
            }

            if (!McpProjectSettings.GetAutoReconnect())
            {
                McpLog.Warn("HTTP MCP bridge stopped for domain reload; 自动重连未开启，请在 MCP 面板手动连接。");
                return;
            }

            EditorPrefs.DeleteKey(EditorPrefKeys.ManualReconnectRequired);
            _ = StartHttpAfterReloadAsync();
        }

        private static async Task StartHttpAfterReloadAsync()
        {
            try
            {
                // 包升级触发的是域重载而非冷启动：恢复路径必须先做版本判定。
                // 否则旧服务端会被立即重连，CentralServerAutoConnect 的版本判定会被"会话已连接"
                // 早返回永久挡死（Editor.log 实证：三次升级版本日志零出现，服务端停在旧版数小时）。
                // allowLaunch=false：Server 未运行时不主动拉起（尊重 CentralServerAutoStart 设置），
                // 但"运行中 + 本项目启动 + 版本明确过旧"的停旧起新仍会执行——那是升级自愈，不是冷启动。
                var settings = McpProjectSettings.Load();
                if (settings.CentralServerEnabled && settings.CentralServerAutoStart)
                {
                    await CentralServerAutoConnect.EnsureServerReadyWithVersionCheckAsync(allowLaunch: false);
                }

                bool started = await MCPServiceLocator.TransportManager.StartAsync(TransportMode.Http);
                if (!started)
                {
                    McpLog.Warn("HTTP MCP bridge 自动重连失败，请在 MCP 面板手动连接。");
                    return;
                }

                RunOnEditorThread(MCPForUnityEditorWindow.RequestHealthVerification);
            }
            catch (Exception ex)
            {
                McpLog.Error($"HTTP MCP bridge 自动重连异常：{ex.Message}");
            }
        }

        private static void RunOnEditorThread(Action action)
        {
            if (action == null)
            {
                return;
            }

            if (EditorSynchronizationContext != null)
            {
                EditorSynchronizationContext.Post(_ => action(), null);
                return;
            }

            action();
        }
    }

    [InitializeOnLoad]
    internal static class CentralServerAutoConnect
    {
        private const int MaxAttempts = 30;
        private const int InitialDelayMs = 1000;
        private const int RetryDelayMs = 2000;
        private const int ServerReadyPollMs = 500;
        private const int ServerReadyTimeoutMs = 30000;

        private static bool running;

        static CentralServerAutoConnect()
        {
            EditorApplication.delayCall += () => _ = TryAutoConnectAsync();
        }

        private static async Task TryAutoConnectAsync()
        {
            if (HttpBridgeReloadHandler.BatchModeBlocked())
            {
                return;
            }

            // 在第一个 await 之前（主线程）检查顶替标记：被同工程另一实例顶替后不再自动连接，避免互相顶替死循环
            if (HttpBridgeReloadHandler.IsSuperseded())
            {
                McpLog.Warn("UnityMCP 自动连接已跳过：本工程 Session 已被同工程另一实例顶替（同项目多开？），需在 MCP 面板手动连接。");
                return;
            }

            if (running)
            {
                return;
            }
            running = true;

            try
            {
                await Task.Delay(InitialDelayMs);

                var settings = McpProjectSettings.Load();
                if (!settings.CentralServerEnabled || !settings.CentralServerAutoStart || !settings.AutoConnect)
                {
                    return;
                }

                if (!settings.AutoReconnect && EditorPrefs.GetBool(EditorPrefKeys.ManualReconnectRequired, false))
                {
                    McpLog.Warn("UnityMCP 自动连接已跳过：上次连接因 reload/断线停止，请在 MCP 面板手动连接。");
                    return;
                }

                if (!settings.UseHttpTransport || !string.Equals(settings.HttpTransportScope, "local", StringComparison.OrdinalIgnoreCase))
                {
                    return;
                }

                for (int attempt = 1; attempt <= MaxAttempts; attempt++)
                {
                    if (!IsEditorReadyForAutoConnect())
                    {
                        await DelayBeforeRetryAsync(attempt);
                        continue;
                    }

                    if (MCPServiceLocator.TransportManager.IsRunning(TransportMode.Http))
                    {
                        McpLog.Info("UnityMCP Session 已连接，跳过自动连接。");
                        return;
                    }

                    // 三态版本判定 + 必要时停旧起新 + 等待就绪（与域重载恢复路径共用同一套逻辑）。
                    bool serverReady = await EnsureServerReadyWithVersionCheckAsync(allowLaunch: true);
                    if (!serverReady)
                    {
                        await DelayBeforeRetryAsync(attempt);
                        continue;
                    }

                    bool connected = await MCPServiceLocator.TransportManager.StartAsync(TransportMode.Http);
                    if (connected)
                    {
                        McpLog.Info("UnityMCP 已在项目加载完成后自动连接 Session。");
                        MCPForUnityEditorWindow.RequestHealthVerification();
                        return;
                    }

                    await DelayBeforeRetryAsync(attempt);
                }

                McpLog.Warn("UnityMCP 自动连接超时：中心 Server 未就绪或 Session 建立失败。");
            }
            catch (Exception ex)
            {
                McpLog.Warn($"UnityMCP 自动连接失败：{ex.Message}");
            }
            finally
            {
                running = false;
            }
        }

        /// <summary>
        /// 三态版本判定 + 必要时停旧起新 + 等待就绪，供自动连接循环与域重载恢复路径共用。
        /// allowLaunch=false 时不在"Server 未运行"的情况下主动拉起（尊重手动管理模式），
        /// 但"运行中 + 本项目启动 + 版本明确过旧"的停旧起新仍执行（升级自愈，非冷启动）。
        /// 返回 true 表示 Server 可用（含"非本项目启动但复用"与"Unknown 等待就绪"的情形）。
        /// </summary>
        internal static async Task<bool> EnsureServerReadyWithVersionCheckAsync(bool allowLaunch)
        {
            bool serverReady = MCPServiceLocator.Server.IsLocalHttpServerRunning();
            if (serverReady)
            {
                var versionCheck = await Task.Run(() => MCPServiceLocator.Server.CheckRunningServerVersion());
                if (versionCheck == ServerVersionCheck.Mismatch
                    && await Task.Run(() => MCPServiceLocator.Server.IsRunningServerOwnedByThisProject()))
                {
                    // 本项目启动的 Server 版本明确过旧：置为未就绪，进入下方 launch 分支停旧起新
                    serverReady = false;
                }
                else if (versionCheck == ServerVersionCheck.Mismatch)
                {
                    // 非本项目启动：不杀（中心 Server 多项目共享，滚动升级期版本偏斜属常态）
                    McpLog.Warn("中心 MCP HTTP Server 由其他项目启动且版本不一致，保留现有 Server（服务端会继续服务并自报版本偏斜）；如需统一请从启动它的项目升级或手动重启。");
                }
                // Unknown（探针失败/未就绪）：保持 serverReady=true，让后续连接/重试等它就绪，不进停杀路径
            }
            else if (!allowLaunch)
            {
                return false;
            }

            if (!serverReady)
            {
                // 未运行（或本项目启动的版本过旧 Server）时拉起；StartLocalHttpServerQuiet 内部
                // 会先停掉过旧 Server 再启动，防重入由其自身的端口检查承担。
                // 探针/停杀/部署/uv 全为阻塞调用，放线程池执行避免卡主线程。
                bool launchRequested = await Task.Run(() => MCPServiceLocator.Server.StartLocalHttpServerQuiet());
                if (!launchRequested)
                {
                    return false;
                }

                serverReady = await WaitForServerReadyAsync();
            }

            return serverReady;
        }

        private static bool IsEditorReadyForAutoConnect()
        {
            if (EditorApplication.isCompiling || EditorApplication.isUpdating)
            {
                return false;
            }

            try
            {
                var pipeline = Type.GetType("UnityEditor.Compilation.CompilationPipeline, UnityEditor");
                var prop = pipeline?.GetProperty("isCompiling", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static);
                if (prop != null && (bool)prop.GetValue(null))
                {
                    return false;
                }
            }
            catch { }

            return true;
        }

        private static Task DelayBeforeRetryAsync(int attempt)
        {
            // 项目启动和 Domain Reload 可能持续数十秒，最后一次失败后无需继续等待。
            return attempt < MaxAttempts
                ? Task.Delay(RetryDelayMs)
                : Task.CompletedTask;
        }

        private static async Task<bool> WaitForServerReadyAsync()
        {
            // 进程拉起后端口绑定需要时间：轮询等待（每 500ms 一次、总计约 30s 上限）。
            // await Task.Delay 为异步等待（与 DelayBeforeRetryAsync 一致），不阻塞主线程。
            var stopwatch = System.Diagnostics.Stopwatch.StartNew();
            while (stopwatch.ElapsedMilliseconds < ServerReadyTimeoutMs)
            {
                if (MCPServiceLocator.Server.IsLocalHttpServerRunning())
                {
                    return true;
                }

                await Task.Delay(ServerReadyPollMs);
            }

            return MCPServiceLocator.Server.IsLocalHttpServerRunning();
        }
    }
}
