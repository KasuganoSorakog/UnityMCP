using System;
using System.Buffers;
using System.Collections.Generic;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using MCPForUnity.Editor.Constants;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;
using MCPForUnity.Editor.Services.Transport;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace MCPForUnity.Editor.Services.Transport.Transports
{
    /// <summary>
    /// Maintains a persistent WebSocket connection to the MCP server plugin hub.
    /// Handles registration, keep-alives, and command dispatch back into Unity via
    /// <see cref="TransportCommandDispatcher"/>.
    /// </summary>
    public class WebSocketTransportClient : IMcpTransportClient, IDisposable
    {
        private const string TransportDisplayName = "websocket";
        private static readonly TimeSpan[] ReconnectSchedule =
        {
            TimeSpan.FromSeconds(2),
            TimeSpan.FromSeconds(10),
            TimeSpan.FromSeconds(30)
        };

        private static readonly TimeSpan DefaultKeepAliveInterval = TimeSpan.FromSeconds(15);
        private static readonly TimeSpan DefaultCommandTimeout = TimeSpan.FromSeconds(30);
        private static readonly TimeSpan RegistrationTimeout = TimeSpan.FromSeconds(10);
        // 单条 WebSocket 消息的接收上限（对齐 legacy TCP 的 64MB）
        private const long MaxMessageBytes = 64L * 1024 * 1024;

        private readonly IToolDiscoveryService _toolDiscoveryService;
        private ClientWebSocket _socket;
        private CancellationTokenSource _lifecycleCts;
        private CancellationTokenSource _connectionCts;
        private Task _receiveTask;
        private Task _keepAliveTask;
        private TaskCompletionSource<string> _registrationCompletion;
        private readonly SemaphoreSlim _sendLock = new(1, 1);

        private Uri _endpointUri;
        private string _sessionId;
        private string _projectHash;
        private string _projectName;
        private string _projectPath;
        private string _unityVersion;
        private string _packageVersion;
        private TimeSpan _keepAliveInterval = DefaultKeepAliveInterval;
        private TimeSpan _socketKeepAliveInterval = DefaultKeepAliveInterval;
        private volatile bool _isConnected;
        // 被同工程另一实例顶替后停止自动重连，直至用户手动连接
        private volatile bool _superseded;
        private TransportState _state = TransportState.Disconnected(TransportDisplayName, "Transport not started");
        private int _isReconnectingFlag;
        private bool _disposed;

        public WebSocketTransportClient(IToolDiscoveryService toolDiscoveryService = null)
        {
            _toolDiscoveryService = toolDiscoveryService;
        }

        public bool IsConnected => _isConnected;
        public string TransportName => TransportDisplayName;
        public TransportState State => _state;

        private Task<List<ToolMetadata>> GetEnabledToolsOnMainThreadAsync(CancellationToken token)
        {
            return TransportCommandDispatcher.RunOnMainThreadAsync(
                () => _toolDiscoveryService?.GetEnabledTools() ?? new List<ToolMetadata>(),
                token);
        }

        public async Task<bool> StartAsync()
        {
            // 手动/自动连接入口：重置顶替标志，允许重新建立会话
            _superseded = false;

            // Capture identity values on the main thread before any async context switching
            _projectName = ProjectIdentityUtility.GetProjectName();
            _projectHash = ProjectIdentityUtility.GetProjectHash();
            _projectPath = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
            _unityVersion = Application.unityVersion;
            _packageVersion = AssetPathUtility.GetPackageVersion();

            await StopAsync();

            _lifecycleCts = new CancellationTokenSource();
            _endpointUri = BuildWebSocketUri(HttpEndpointUtility.GetBaseUrl());
            _sessionId = null;

            if (!await EstablishConnectionAsync(_lifecycleCts.Token))
            {
                await StopAsync();
                return false;
            }

            _state = TransportState.Connected(TransportDisplayName, sessionId: _sessionId, details: _endpointUri.ToString());
            _isConnected = true;
            return true;
        }

        public async Task StopAsync()
        {
            if (_lifecycleCts == null)
            {
                return;
            }

            try
            {
                _lifecycleCts.Cancel();
            }
            catch { }

            await StopConnectionLoopsAsync().ConfigureAwait(false);

            if (_socket != null)
            {
                try
                {
                    if (_socket.State == WebSocketState.Open || _socket.State == WebSocketState.CloseReceived)
                    {
                        await _socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "Shutdown", CancellationToken.None).ConfigureAwait(false);
                    }
                }
                catch { }
                finally
                {
                    _socket.Dispose();
                    _socket = null;
                }
            }

            _isConnected = false;
            _state = TransportState.Disconnected(TransportDisplayName);

            _lifecycleCts.Dispose();
            _lifecycleCts = null;
        }

        public async Task<bool> VerifyAsync()
        {
            if (_socket == null || _socket.State != WebSocketState.Open)
            {
                return false;
            }

            if (_lifecycleCts == null)
            {
                return false;
            }

            try
            {
                using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(_lifecycleCts.Token);
                timeoutCts.CancelAfter(TimeSpan.FromSeconds(5));
                await SendPongAsync(timeoutCts.Token).ConfigureAwait(false);
                return true;
            }
            catch (Exception ex)
            {
                McpLog.Warn($"[WebSocket] Verify ping failed: {ex.Message}");
                return false;
            }
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }

            try
            {
                // Ensure background loops are stopped before disposing shared resources
                StopAsync().GetAwaiter().GetResult();
            }
            catch (Exception ex)
            {
                McpLog.Warn($"[WebSocket] Dispose failed to stop cleanly: {ex.Message}");
            }

            _sendLock?.Dispose();
            _socket?.Dispose();
            _lifecycleCts?.Dispose();
            _disposed = true;
        }

        private async Task<bool> EstablishConnectionAsync(CancellationToken token)
        {
            await StopConnectionLoopsAsync().ConfigureAwait(false);

            _connectionCts?.Dispose();
            _connectionCts = CancellationTokenSource.CreateLinkedTokenSource(token);
            CancellationToken connectionToken = _connectionCts.Token;

            _socket?.Dispose();
            _socket = new ClientWebSocket();
            _socket.Options.KeepAliveInterval = _socketKeepAliveInterval;
            _sessionId = null;
            _registrationCompletion = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);

            try
            {
                await _socket.ConnectAsync(_endpointUri, connectionToken).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                McpLog.Error($"[WebSocket] Connection failed: {ex.Message}");
                return false;
            }

            StartBackgroundLoops(connectionToken);

            try
            {
                await SendRegisterAsync(connectionToken).ConfigureAwait(false);

                Task registrationTask = _registrationCompletion.Task;
                Task timeoutTask = Task.Delay(RegistrationTimeout, connectionToken);
                Task completedTask = await Task.WhenAny(registrationTask, timeoutTask).ConfigureAwait(false);
                if (completedTask != registrationTask)
                {
                    McpLog.Error($"[WebSocket] Registration acknowledgement timed out after {RegistrationTimeout.TotalSeconds:0} seconds");
                    return false;
                }

                await registrationTask.ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                McpLog.Error($"[WebSocket] Registration failed: {ex.Message}");
                return false;
            }

            return true;
        }

        /// <summary>
        /// Stops the connection loops and disposes of the connection CTS.
        /// Particularly useful when reconnecting, we want to ensure that background loops are cancelled correctly before starting new oens
        /// </summary>
        /// <param name="awaitTasks">Whether to await the receive and keep alive tasks before disposing.</param>
        private async Task StopConnectionLoopsAsync(bool awaitTasks = true)
        {
            if (_connectionCts != null && !_connectionCts.IsCancellationRequested)
            {
                try { _connectionCts.Cancel(); } catch { }
            }

            if (_receiveTask != null)
            {
                if (awaitTasks)
                {
                    try { await _receiveTask.ConfigureAwait(false); } catch { }
                    _receiveTask = null;
                }
                else if (_receiveTask.IsCompleted)
                {
                    _receiveTask = null;
                }
            }

            if (_keepAliveTask != null)
            {
                if (awaitTasks)
                {
                    try { await _keepAliveTask.ConfigureAwait(false); } catch { }
                    _keepAliveTask = null;
                }
                else if (_keepAliveTask.IsCompleted)
                {
                    _keepAliveTask = null;
                }
            }

            if (_connectionCts != null)
            {
                _connectionCts.Dispose();
                _connectionCts = null;
            }
        }

        private void StartBackgroundLoops(CancellationToken token)
        {
            if ((_receiveTask != null && !_receiveTask.IsCompleted) || (_keepAliveTask != null && !_keepAliveTask.IsCompleted))
            {
                return;
            }

            _receiveTask = Task.Run(() => ReceiveLoopAsync(token), CancellationToken.None);
            _keepAliveTask = Task.Run(() => KeepAliveLoopAsync(token), CancellationToken.None);
        }

        private async Task ReceiveLoopAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    string message = await ReceiveMessageAsync(token).ConfigureAwait(false);
                    if (message == null)
                    {
                        continue;
                    }
                    await HandleMessageAsync(message, token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (WebSocketException wse)
                {
                    McpLog.Warn($"[WebSocket] Receive loop error: {wse.Message}");
                    await HandleSocketClosureAsync(wse.Message).ConfigureAwait(false);
                    break;
                }
                catch (Exception ex)
                {
                    McpLog.Warn($"[WebSocket] Unexpected receive error: {ex.Message}");
                    await HandleSocketClosureAsync(ex.Message).ConfigureAwait(false);
                    break;
                }
            }
        }

        private async Task<string> ReceiveMessageAsync(CancellationToken token)
        {
            if (_socket == null)
            {
                return null;
            }

            byte[] rentedBuffer = System.Buffers.ArrayPool<byte>.Shared.Rent(8192);
            var buffer = new ArraySegment<byte>(rentedBuffer);
            using var ms = new MemoryStream(8192);

            try
            {
                while (!token.IsCancellationRequested)
                {
                    WebSocketReceiveResult result = await _socket.ReceiveAsync(buffer, token).ConfigureAwait(false);

                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        await HandleSocketClosureAsync(result.CloseStatusDescription ?? "Server closed connection").ConfigureAwait(false);
                        return null;
                    }

                    if (result.Count > 0)
                    {
                        ms.Write(buffer.Array!, buffer.Offset, result.Count);
                        if (ms.Length > MaxMessageBytes)
                        {
                            // 超过接收上限：抛出异常，由 ReceiveLoop 走关闭重连路径
                            throw new InvalidOperationException("MCP message exceeds 64MB limit");
                        }
                    }

                    if (result.EndOfMessage)
                    {
                        break;
                    }
                }

                if (ms.Length == 0)
                {
                    return null;
                }

                return Encoding.UTF8.GetString(ms.ToArray());
            }
            finally
            {
                System.Buffers.ArrayPool<byte>.Shared.Return(rentedBuffer);
            }
        }

        private async Task HandleMessageAsync(string message, CancellationToken token)
        {
            JObject payload;
            try
            {
                payload = JObject.Parse(message);
            }
            catch (Exception ex)
            {
                McpLog.Warn($"[WebSocket] Invalid JSON payload: {ex.Message}");
                return;
            }

            string messageType = payload.Value<string>("type") ?? string.Empty;

            switch (messageType)
            {
                case "welcome":
                    ApplyWelcome(payload);
                    break;
                case "registered":
                    await HandleRegisteredAsync(payload, token).ConfigureAwait(false);
                    break;
                case "execute":
                    // 命令在 Unity 主线程执行可能耗时数十秒：fire-and-forget 后台执行并自行回发结果，
                    // 避免串行 await 阻塞接收循环，导致 ping/cancel/session_superseded 等轻量消息积压在 socket 缓冲区读不到。
                    _ = Task.Run(() => ExecuteCommandInBackgroundAsync(payload, token), CancellationToken.None);
                    break;
                case "ping":
                    await SendPongAsync(token).ConfigureAwait(false);
                    break;
                case "session_superseded":
                    await HandleSessionSupersededAsync(payload).ConfigureAwait(false);
                    break;
                case "cancel":
                    HandleCancel(payload);
                    break;
                default:
                    // No-op for unrecognised types (keep-alives, telemetry, etc.)
                    break;
            }
        }

        /// <summary>
        /// 服务端通知本 Session 已被同一工程的另一个 Unity 实例顶替：
        /// 置标志停止自动重连，写项目级 EditorPrefs 标记，best-effort 关闭 socket。
        /// </summary>
        private async Task HandleSessionSupersededAsync(JObject payload)
        {
            _superseded = true;
            _isConnected = false;

            string reason = payload?.Value<string>("reason") ?? "duplicate_project_hash";
            _state = TransportState.Disconnected(TransportDisplayName, "已被同一工程的另一个 Unity 实例顶替（同项目多开？），已停止自动重连");
            McpLog.Warn($"[WebSocket] Session 已被同一工程的另一个 Unity 实例顶替（reason={reason}），已停止自动重连。");

            // 项目级标记：阻止域重载后/启动时的自动连接，避免两个编辑器互相顶替死循环
            try
            {
                if (!string.IsNullOrEmpty(_projectHash))
                {
                    EditorPrefs.SetBool(EditorPrefKeys.SupersededPrefix + _projectHash, true);
                }
            }
            catch { }

            // 不在持锁状态下等待后台任务，避免死锁
            await StopConnectionLoopsAsync(awaitTasks: false).ConfigureAwait(false);

            if (_socket != null && _socket.State == WebSocketState.Open)
            {
                try
                {
                    await _socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "Superseded", CancellationToken.None).ConfigureAwait(false);
                }
                catch { }
            }
        }

        /// <summary>
        /// 服务端下发 cancel（命令超时/主动取消）：转发给命令分发器按 id 取消，
        /// 命令结果由 HandleExecuteAsync 的等待方按正常 command_result 流程回发。
        /// </summary>
        private void HandleCancel(JObject payload)
        {
            string commandId = payload?.Value<string>("id");
            if (string.IsNullOrEmpty(commandId))
            {
                McpLog.Warn("[WebSocket] Invalid cancel payload (missing id)");
                return;
            }

            var result = TransportCommandDispatcher.TryCancelServerCommand(commandId);
            switch (result)
            {
                case TransportCommandDispatcher.ServerCommandCancelResult.CancelledQueued:
                    McpLog.Info($"[WebSocket] 命令 {commandId} 在排队中被取消（未执行）。");
                    break;
                case TransportCommandDispatcher.ServerCommandCancelResult.AlreadyRunning:
                    McpLog.Warn($"[WebSocket] 命令 {commandId} 已开始执行，无法中止，仅通知等待方。");
                    break;
                default:
                    McpLog.Info($"[WebSocket] 收到 cancel，但未找到命令 {commandId}（可能已完成或已超时）。");
                    break;
            }
        }

        private void ApplyWelcome(JObject payload)
        {
            int? keepAliveSeconds = payload.Value<int?>("keepAliveInterval");
            if (keepAliveSeconds.HasValue && keepAliveSeconds.Value > 0)
            {
                _keepAliveInterval = TimeSpan.FromSeconds(keepAliveSeconds.Value);
                _socketKeepAliveInterval = _keepAliveInterval;
            }

            int? serverTimeoutSeconds = payload.Value<int?>("serverTimeout");
            if (serverTimeoutSeconds.HasValue)
            {
                int sourceSeconds = keepAliveSeconds ?? serverTimeoutSeconds.Value;
                int safeSeconds = Math.Max(5, Math.Min(serverTimeoutSeconds.Value, sourceSeconds));
                _socketKeepAliveInterval = TimeSpan.FromSeconds(safeSeconds);
            }
        }

        private async Task HandleRegisteredAsync(JObject payload, CancellationToken token)
        {
            string newSessionId = payload.Value<string>("session_id");
            if (!string.IsNullOrEmpty(newSessionId))
            {
                _sessionId = newSessionId;
                ProjectIdentityUtility.SetSessionId(_sessionId);
                _state = TransportState.Connected(TransportDisplayName, sessionId: _sessionId, details: _endpointUri.ToString());
                McpLog.Info($"[WebSocket] Registered with session ID: {_sessionId}");

                await SendRegisterToolsAsync(token).ConfigureAwait(false);
                _registrationCompletion?.TrySetResult(_sessionId);
            }
        }

        private async Task SendRegisterToolsAsync(CancellationToken token)
        {
            if (_toolDiscoveryService == null) return;

            token.ThrowIfCancellationRequested();
            var tools = await GetEnabledToolsOnMainThreadAsync(token).ConfigureAwait(false);
            token.ThrowIfCancellationRequested();
            McpLog.Info($"[WebSocket] Preparing to register {tools.Count} tool(s) with the bridge.");
            var toolsArray = new JArray();

            foreach (var tool in tools)
            {
                var toolObj = new JObject
                {
                    ["name"] = tool.Name,
                    ["description"] = tool.Description,
                    ["structured_output"] = tool.StructuredOutput,
                    ["requires_polling"] = tool.RequiresPolling,
                    ["poll_action"] = tool.PollAction
                };

                var paramsArray = new JArray();
                if (tool.Parameters != null)
                {
                    foreach (var p in tool.Parameters)
                    {
                        paramsArray.Add(new JObject
                        {
                            ["name"] = p.Name,
                            ["description"] = p.Description,
                            ["type"] = p.Type,
                            ["required"] = p.Required,
                            ["default_value"] = p.DefaultValue
                        });
                    }
                }
                toolObj["parameters"] = paramsArray;
                toolsArray.Add(toolObj);
            }

            var payload = new JObject
            {
                ["type"] = "register_tools",
                ["tools"] = toolsArray
            };

            await SendJsonAsync(payload, token).ConfigureAwait(false);
            McpLog.Info($"[WebSocket] Sent {tools.Count} tools registration");
        }

        private async Task HandleExecuteAsync(JObject payload, CancellationToken token)
        {
            string commandId = payload.Value<string>("id");
            string commandName = payload.Value<string>("name");
            JObject parameters = payload.Value<JObject>("params") ?? new JObject();
            int timeoutSeconds = payload.Value<int?>("timeout") ?? (int)DefaultCommandTimeout.TotalSeconds;

            if (string.IsNullOrEmpty(commandId) || string.IsNullOrEmpty(commandName))
            {
                McpLog.Warn("[WebSocket] Invalid execute payload (missing id or name)");
                return;
            }

            var commandEnvelope = new JObject
            {
                ["type"] = commandName,
                ["params"] = parameters
            };

            string responseJson;
            try
            {
                using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(token);
                timeoutCts.CancelAfter(TimeSpan.FromSeconds(Math.Max(1, timeoutSeconds)));
                responseJson = await TransportCommandDispatcher.ExecuteCommandJsonAsync(commandEnvelope.ToString(Formatting.None), timeoutCts.Token, commandId).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                // 到达这里说明命令已在主线程开始执行（排队中取消会返回 cancelled_before_execution 结果而不抛异常）
                responseJson = JsonConvert.SerializeObject(new
                {
                    status = "error",
                    code = "execution_state_unknown",
                    error = $"命令 '{commandName}' 已在 Unity 主线程开始执行后超时/被取消（{timeoutSeconds}s），可能已部分生效，重试前请先验证场景状态"
                });
            }
            catch (Exception ex)
            {
                responseJson = JsonConvert.SerializeObject(new
                {
                    status = "error",
                    error = ex.Message
                });
            }

            JToken resultToken;
            try
            {
                resultToken = JToken.Parse(responseJson);
            }
            catch
            {
                resultToken = new JObject
                {
                    ["status"] = "error",
                    ["error"] = "Invalid response payload"
                };
            }

            var responsePayload = new JObject
            {
                ["type"] = "command_result",
                ["id"] = commandId,
                ["result"] = resultToken
            };

            await SendJsonAsync(responsePayload, token).ConfigureAwait(false);
        }

        /// <summary>
        /// 后台执行服务端命令并回发结果（接收循环 fire-and-forget 调度，并发语义不变：
        /// 命令仍由 <see cref="TransportCommandDispatcher"/> 排队后在 Unity 主线程串行执行）。
        /// HandleExecuteAsync 内部已把命令异常转成 error 结果；这里兜底捕获剩余异常
        /// （多为结果回发时连接已断开），尽量补发 error 结果，失败仅记日志，绝不静默吞掉。
        /// </summary>
        private async Task ExecuteCommandInBackgroundAsync(JObject payload, CancellationToken token)
        {
            string commandId = payload?.Value<string>("id");
            try
            {
                await HandleExecuteAsync(payload, token).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                McpLog.Warn($"[WebSocket] 命令 {commandId} 执行/回发异常：{ex.Message}");
                await TrySendErrorResultAsync(commandId, ex.Message).ConfigureAwait(false);
            }
        }

        /// <summary>
        /// 尽力回发 error 结果。用 CancellationToken.None：连接令牌可能已取消，
        /// 但 socket 仍开着时（如命令超时）补发仍可能成功；连接已断开则记日志放弃。
        /// </summary>
        private async Task TrySendErrorResultAsync(string commandId, string error)
        {
            if (string.IsNullOrEmpty(commandId))
            {
                return;
            }

            try
            {
                var payload = new JObject
                {
                    ["type"] = "command_result",
                    ["id"] = commandId,
                    ["result"] = new JObject
                    {
                        ["status"] = "error",
                        ["error"] = error
                    }
                };

                await SendJsonAsync(payload, CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                McpLog.Warn($"[WebSocket] 命令 {commandId} 的 error 结果回发失败（连接可能已断开）：{ex.Message}");
            }
        }

        private async Task KeepAliveLoopAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(_keepAliveInterval, token).ConfigureAwait(false);
                    if (_socket == null || _socket.State != WebSocketState.Open)
                    {
                        break;
                    }
                    await SendPongAsync(token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    McpLog.Warn($"[WebSocket] Keep-alive failed: {ex.Message}");
                    await HandleSocketClosureAsync(ex.Message).ConfigureAwait(false);
                    break;
                }
            }
        }

        private async Task SendRegisterAsync(CancellationToken token)
        {
            string currentScene = string.Empty;
            try
            {
                var scene = EditorSceneManager.GetActiveScene();
                currentScene = string.IsNullOrEmpty(scene.path) ? scene.name : scene.path;
            }
            catch { }

            var registerPayload = new JObject
            {
                ["type"] = "register",
                // session_id is now server-authoritative; omitted here or sent as null
                ["project_name"] = _projectName,
                ["project_hash"] = _projectHash,
                ["project_path"] = _projectPath,
                ["unity_version"] = _unityVersion,
                ["package_version"] = _packageVersion,
                ["current_scene"] = currentScene,
                ["capabilities_version"] = "1"
            };

            await SendJsonAsync(registerPayload, token).ConfigureAwait(false);
        }

        private Task SendPongAsync(CancellationToken token)
        {
            // 未注册到 session 前不发送 pong（服务端按 session_id 刷新活性）
            if (string.IsNullOrEmpty(_sessionId))
            {
                return Task.CompletedTask;
            }

            var payload = new JObject
            {
                ["type"] = "pong",
                ["session_id"] = _sessionId,
                ["project_hash"] = _projectHash,
            };
            return SendJsonAsync(payload, token);
        }

        private async Task SendJsonAsync(JObject payload, CancellationToken token)
        {
            if (_socket == null)
            {
                throw new InvalidOperationException("WebSocket is not initialised");
            }

            string json = payload.ToString(Formatting.None);
            byte[] bytes = Encoding.UTF8.GetBytes(json);
            var buffer = new ArraySegment<byte>(bytes);

            await _sendLock.WaitAsync(token).ConfigureAwait(false);
            try
            {
                if (_socket.State != WebSocketState.Open)
                {
                    throw new InvalidOperationException("WebSocket is not open");
                }

                await _socket.SendAsync(buffer, WebSocketMessageType.Text, true, token).ConfigureAwait(false);
            }
            finally
            {
                _sendLock.Release();
            }
        }

        private async Task HandleSocketClosureAsync(string reason)
        {
            if (_lifecycleCts == null || _lifecycleCts.IsCancellationRequested)
            {
                return;
            }

            // 已被顶替：不重连、不覆盖"已被顶替"的状态文案
            if (_superseded)
            {
                return;
            }

            _isConnected = false;
            _state = TransportState.Disconnected(TransportDisplayName, reason ?? "Connection closed");
            McpLog.Warn($"[WebSocket] Connection closed: {reason}");

            await StopConnectionLoopsAsync(awaitTasks: false).ConfigureAwait(false);
            if (!McpProjectSettings.GetAutoReconnect())
            {
                McpLog.Warn("[WebSocket] 自动重连未开启，请在 MCP 面板手动连接。");
                return;
            }

            if (Interlocked.CompareExchange(ref _isReconnectingFlag, 1, 0) != 0)
            {
                return;
            }

            _ = Task.Run(() => AttemptReconnectAsync(_lifecycleCts.Token), CancellationToken.None);
        }

        private async Task AttemptReconnectAsync(CancellationToken token)
        {
            try
            {
                await StopConnectionLoopsAsync().ConfigureAwait(false);

                int attempt = 0;
                while (!token.IsCancellationRequested && !_superseded && McpProjectSettings.GetAutoReconnect())
                {
                    TimeSpan delay = ReconnectSchedule[Math.Min(attempt, ReconnectSchedule.Length - 1)];
                    attempt++;

                    try { await Task.Delay(delay, token).ConfigureAwait(false); }
                    catch (OperationCanceledException) { return; }

                    if (await EstablishConnectionAsync(token).ConfigureAwait(false))
                    {
                        _state = TransportState.Connected(TransportDisplayName, sessionId: _sessionId, details: _endpointUri.ToString());
                        _isConnected = true;
                        McpLog.Info("[WebSocket] 已自动重连到 MCP server");
                        return;
                    }
                }

                if (!_superseded)
                {
                    _state = TransportState.Disconnected(TransportDisplayName, "Auto reconnect stopped");
                }
            }
            finally
            {
                Interlocked.Exchange(ref _isReconnectingFlag, 0);
            }
        }

        private static Uri BuildWebSocketUri(string baseUrl)
        {
            if (!Uri.TryCreate(baseUrl, UriKind.Absolute, out var httpUri))
            {
                throw new InvalidOperationException($"Invalid MCP base URL: {baseUrl}");
            }

            string scheme = httpUri.Scheme.Equals("https", StringComparison.OrdinalIgnoreCase) ? "wss" : "ws";
            string builder = $"{scheme}://{httpUri.Authority}";
            if (!string.IsNullOrEmpty(httpUri.AbsolutePath) && httpUri.AbsolutePath != "/")
            {
                builder += httpUri.AbsolutePath.TrimEnd('/');
            }

            builder += "/hub/plugin";

            return new Uri(builder);
        }
    }
}
