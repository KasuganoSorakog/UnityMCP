using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using MCPForUnity.Editor.Constants;
using Newtonsoft.Json;
using UnityEditor;
using UnityEngine;

namespace MCPForUnity.Editor.Helpers
{
    /// <summary>
    /// Manages dynamic port allocation and persistent storage for MCP for Unity
    /// </summary>
    public static class PortManager
    {
        private static bool IsDebugEnabled()
        {
            try { return EditorPrefs.GetBool(EditorPrefKeys.DebugLogs, false); }
            catch { return false; }
        }

        private const int DefaultPort = 6400;
        private const string RegistryFileName = "unity-mcp-port.json";

        [Serializable]
        public class PortConfig
        {
            public int unity_port;
            public string created_date;
            public string project_path;
        }

        /// <summary>
        /// Get the port to use from storage, or return the default if none has been saved yet.
        /// </summary>
        /// <returns>Port number to use</returns>
        public static int GetPortWithFallback()
        {
            var storedConfig = GetStoredPortConfig();
            if (storedConfig != null &&
                storedConfig.unity_port > 0 &&
                string.Equals(storedConfig.project_path ?? string.Empty, Application.dataPath ?? string.Empty, StringComparison.OrdinalIgnoreCase))
            {
                return storedConfig.unity_port;
            }

            return DefaultPort;
        }

        /// <summary>
        /// Persist a user-selected port and return the value actually stored.
        /// If <paramref name="port"/> is unavailable, the next available port is chosen instead.
        /// </summary>
        public static int SetPreferredPort(int port)
        {
            if (port <= 0)
            {
                throw new ArgumentOutOfRangeException(nameof(port), "Port must be positive.");
            }

            if (!IsPortAvailable(port))
            {
                throw new InvalidOperationException($"Port {port} is already in use.");
            }

            SavePort(port);
            return port;
        }

        /// <summary>
        /// Check if a specific port is available for binding
        /// </summary>
        /// <param name="port">Port to check</param>
        /// <returns>True if port is available</returns>
        public static bool IsPortAvailable(int port)
        {
            // Start with quick loopback check
            try
            {
                var testListener = new TcpListener(IPAddress.Loopback, port);
                testListener.Start();
                testListener.Stop();
            }
            catch (SocketException)
            {
                return false;
            }

#if UNITY_EDITOR_OSX
            // On macOS, the OS might report the port as available (SO_REUSEADDR) even if another process
            // is using it, unless we also check active connections or try a stricter bind.
            // Double check by trying to Connect to it. If we CAN connect, it's NOT available.
            try
            {
                using var client = new TcpClient();
                var connectTask = client.ConnectAsync(IPAddress.Loopback, port);
                // If we connect successfully, someone is listening -> Not available
                if (connectTask.Wait(50) && client.Connected)
                {
                    if (IsDebugEnabled()) McpLog.Info($"[PortManager] Port {port} bind succeeded but connection also succeeded -> Not available (Conflict).");
                    return false;
                }
            }
            catch
            {
                // Connection failed -> likely available (or firewall blocked, but we assume available)
                if (IsDebugEnabled()) McpLog.Info($"[PortManager] Port {port} connection failed -> likely available.");
            }
#endif

            return true;
        }

        /// <summary>
        /// Check if a port is currently being used by MCP for Unity
        /// This helps avoid unnecessary port changes when Unity itself is using the port
        /// </summary>
        /// <param name="port">Port to check</param>
        /// <returns>True if port appears to be used by MCP for Unity</returns>
        public static bool IsPortUsedByMCPForUnity(int port)
        {
            try
            {
                // Try to make a quick connection to see if it's an MCP for Unity server
                using var client = new TcpClient();
                var connectTask = client.ConnectAsync(IPAddress.Loopback, port);
                if (connectTask.Wait(100)) // 100ms timeout
                {
                    // If connection succeeded, it's likely the MCP for Unity server
                    return client.Connected;
                }
                return false;
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// Save port to persistent storage
        /// </summary>
        /// <param name="port">Port to save</param>
        private static void SavePort(int port)
        {
            try
            {
                var portConfig = new PortConfig
                {
                    unity_port = port,
                    created_date = DateTime.UtcNow.ToString("O"),
                    project_path = Application.dataPath
                };

                string registryDir = GetRegistryDirectory();
                Directory.CreateDirectory(registryDir);

                string registryFile = GetRegistryFilePath();
                string json = JsonConvert.SerializeObject(portConfig, Formatting.Indented);
                // Write to hashed, project-scoped file
                File.WriteAllText(registryFile, json, new System.Text.UTF8Encoding(false));
                // Also write to legacy stable filename to avoid hash/case drift across reloads
                string legacy = Path.Combine(GetRegistryDirectory(), RegistryFileName);
                File.WriteAllText(legacy, json, new System.Text.UTF8Encoding(false));

                if (IsDebugEnabled()) McpLog.Info($"Saved port {port} to storage");
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Could not save port to storage: {ex.Message}");
            }
        }

        /// <summary>
        /// Load port from persistent storage
        /// </summary>
        /// <returns>Stored port number, or 0 if not found</returns>
        private static int LoadStoredPort()
        {
            try
            {
                string registryFile = GetRegistryFilePath();

                if (!File.Exists(registryFile))
                {
                    // Backwards compatibility: try the legacy file name
                    string legacy = Path.Combine(GetRegistryDirectory(), RegistryFileName);
                    if (!File.Exists(legacy))
                    {
                        return 0;
                    }
                    registryFile = legacy;
                }

                string json = File.ReadAllText(registryFile);
                var portConfig = JsonConvert.DeserializeObject<PortConfig>(json);

                return portConfig?.unity_port ?? 0;
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Could not load port from storage: {ex.Message}");
                return 0;
            }
        }

        /// <summary>
        /// Get the current stored port configuration
        /// </summary>
        /// <returns>Port configuration if exists, null otherwise</returns>
        public static PortConfig GetStoredPortConfig()
        {
            try
            {
                string registryFile = GetRegistryFilePath();

                if (!File.Exists(registryFile))
                {
                    // Backwards compatibility: try the legacy file
                    string legacy = Path.Combine(GetRegistryDirectory(), RegistryFileName);
                    if (!File.Exists(legacy))
                    {
                        return null;
                    }
                    registryFile = legacy;
                }

                string json = File.ReadAllText(registryFile);
                return JsonConvert.DeserializeObject<PortConfig>(json);
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Could not load port config: {ex.Message}");
                return null;
            }
        }

        private static string GetRegistryDirectory()
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".unity-mcp");
        }

        private static string GetRegistryFilePath()
        {
            string dir = GetRegistryDirectory();
            string hash = ComputeProjectHash(Application.dataPath);
            string fileName = $"unity-mcp-port-{hash}.json";
            return Path.Combine(dir, fileName);
        }

        private static string ComputeProjectHash(string input)
        {
            try
            {
                using SHA1 sha1 = SHA1.Create();
                byte[] bytes = Encoding.UTF8.GetBytes(input ?? string.Empty);
                byte[] hashBytes = sha1.ComputeHash(bytes);
                var sb = new StringBuilder();
                foreach (byte b in hashBytes)
                {
                    sb.Append(b.ToString("x2"));
                }
                return sb.ToString()[..8]; // short, sufficient for filenames
            }
            catch
            {
                return "default";
            }
        }
    }
}
