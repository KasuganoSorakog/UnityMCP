using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using MCPForUnity.Editor.Constants;
using MCPForUnity.Editor.Clients.Configurators;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Models;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace MCPForUnity.Editor.Helpers
{
    public static class ConfigJsonBuilder
    {
        public static string BuildManualConfigJson(string uvPath, McpClient client)
        {
            var root = new JObject();
            bool isVSCode = client?.IsVsCodeLayout == true;
            JObject container = isVSCode ? EnsureObject(root, "servers") : EnsureObject(root, "mcpServers");

            var unity = new JObject();
            PopulateUnityNode(unity, uvPath, client, isVSCode);

            container["unityMCP"] = unity;

            return root.ToString(Formatting.Indented);
        }

        public static JObject ApplyUnityServerToExistingConfig(JObject root, string uvPath, McpClient client)
        {
            if (root == null) root = new JObject();
            bool isVSCode = client?.IsVsCodeLayout == true;
            JObject container = isVSCode ? EnsureObject(root, "servers") : EnsureObject(root, "mcpServers");
            string name = GetUnityServerName(root, client);
            JObject unity = container[name] as JObject ?? new JObject();
            PopulateUnityNode(unity, uvPath, client, isVSCode);
            container[name] = unity;
            return root;
        }

        public static bool TryGetUnityServer(JObject root, McpClient client, out JObject unity)
        {
            unity = null;
            if (root == null) return false;
            var container = client?.IsVsCodeLayout == true
                ? root["servers"] ?? root["mcp"]?["servers"]
                : root["mcpServers"];
            if (container is not JObject servers) return false;
            unity = servers[GetUnityServerName(root, client)] as JObject;
            return unity != null;
        }

        public static bool UrlsEqual(string a, string b)
        {
            if (string.IsNullOrWhiteSpace(a) || string.IsNullOrWhiteSpace(b)) return false;
            if (Uri.TryCreate(a.Trim(), UriKind.Absolute, out var uriA) &&
                Uri.TryCreate(b.Trim(), UriKind.Absolute, out var uriB))
            {
                if (uriA.IsLoopback && uriB.IsLoopback)
                {
                    uriA = new UriBuilder(uriA) { Host = "127.0.0.1" }.Uri;
                    uriB = new UriBuilder(uriB) { Host = "127.0.0.1" }.Uri;
                }
                return Uri.Compare(uriA, uriB, UriComponents.HttpRequestUrl,
                    UriFormat.SafeUnescaped, StringComparison.OrdinalIgnoreCase) == 0;
            }
            return string.Equals(a.Trim().TrimEnd('/'), b.Trim().TrimEnd('/'), StringComparison.OrdinalIgnoreCase);
        }

        private static string GetUnityServerName(JObject root, McpClient client)
        {
            var container = client?.IsVsCodeLayout == true
                ? root["servers"] ?? root["mcp"]?["servers"]
                : root["mcpServers"];
            if (container is not JObject servers || servers["unityMCP"] != null) return "unityMCP";
            if (servers["unity-mcp"] is JObject legacy && IsRecognizedUnityServer(legacy)) return "unity-mcp";
            if (servers["UnityMCP_8080"] is JObject qoderLegacy && IsRecognizedUnityServer(qoderLegacy)) return "UnityMCP_8080";
            return "unityMCP";
        }

        private static bool IsRecognizedUnityServer(JObject server)
        {
            string url = (server["url"] ?? server["serverUrl"])?.ToString();
            if (UrlsEqual(url, HttpEndpointUtility.GetMcpRpcUrl())) return true;
            return server["args"] is JArray args && args.Values<string>().Any(value =>
                string.Equals(value, "mcp-for-unity", StringComparison.OrdinalIgnoreCase));
        }

        /// <summary>
        /// Centralized builder that applies all caveats consistently.
        /// - Sets command/args with uvx and package version
        /// - Ensures env exists
        /// - Adds transport configuration (HTTP or stdio)
        /// - Adds disabled:false for Windsurf/Kiro only when missing
        /// </summary>
        private static void PopulateUnityNode(JObject unity, string uvPath, McpClient client, bool isVSCode)
        {
            // Get transport preference (default to HTTP)
            bool prefValue = McpProjectSettings.GetUseHttpTransport();
            bool clientSupportsHttp = client?.SupportsHttpTransport != false;
            bool useHttpTransport = clientSupportsHttp && prefValue;
            string httpProperty = string.IsNullOrEmpty(client?.HttpUrlProperty) ? "url" : client.HttpUrlProperty;
            var urlPropsToRemove = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "url", "serverUrl" };
            urlPropsToRemove.Remove(httpProperty);

            if (useHttpTransport)
            {
                // HTTP mode: Use URL, no command
                string httpUrl = HttpEndpointUtility.GetMcpRpcUrl();
                unity[httpProperty] = httpUrl;

                foreach (var prop in urlPropsToRemove)
                {
                    if (unity[prop] != null) unity.Remove(prop);
                }

                // Remove command/args if they exist from previous config
                if (unity["command"] != null) unity.Remove("command");
                if (unity["args"] != null) unity.Remove("args");

                if (isVSCode)
                {
                    unity["type"] = "http";
                }
            }
            else
            {
                // Stdio mode: Use uvx command
                var (uvxPath, fromUrl, packageName) = AssetPathUtility.GetUvxCommandParts();
                if (!AssetPathUtility.TryGetPrivateServerSource(out fromUrl, out var sourceError))
                {
                    throw new InvalidOperationException(sourceError);
                }

                var toolArgs = BuildUvxArgs(fromUrl, packageName);

                if (ShouldUseWindowsCmdShim(client))
                {
                    unity["command"] = ResolveCmdPath();

                    var cmdArgs = new List<string> { "/c", uvxPath };
                    cmdArgs.AddRange(toolArgs);

                    unity["args"] = JArray.FromObject(cmdArgs.ToArray());
                }
                else
                {
                    unity["command"] = uvxPath;
                    unity["args"] = JArray.FromObject(toolArgs.ToArray());
                }

                // Remove url/serverUrl if they exist from previous config
                if (unity["url"] != null) unity.Remove("url");
                if (unity["serverUrl"] != null) unity.Remove("serverUrl");

                if (isVSCode)
                {
                    unity["type"] = "stdio";
                }
            }

            if (client?.IncludeTransportType == true)
            {
                unity["type"] = useHttpTransport ? "http" : "stdio";
            }
            else if (!isVSCode && unity["type"] != null)
            {
                unity.Remove("type");
            }

            bool requiresEnv = client?.EnsureEnvObject == true;
            bool stripEnv = client?.StripEnvWhenNotRequired == true;

            if (requiresEnv)
            {
                if (unity["env"] == null)
                {
                    unity["env"] = new JObject();
                }
            }
            else if (stripEnv && unity["env"] != null)
            {
                unity.Remove("env");
            }

            if (client?.DefaultUnityFields != null)
            {
                foreach (var kvp in client.DefaultUnityFields)
                {
                    if (unity[kvp.Key] == null)
                    {
                        unity[kvp.Key] = kvp.Value != null ? JToken.FromObject(kvp.Value) : JValue.CreateNull();
                    }
                }
            }
        }

        private static JObject EnsureObject(JObject parent, string name)
        {
            if (parent[name] is JObject o) return o;
            var created = new JObject();
            parent[name] = created;
            return created;
        }

        private static IList<string> BuildUvxArgs(string fromUrl, string packageName)
        {
            var args = new List<string>();
            if (!string.IsNullOrEmpty(fromUrl))
            {
                args.Add("--from");
                args.Add(fromUrl);
            }
            args.Add(packageName);

            args.Add("--transport");
            args.Add("stdio");

            return args;
        }

        private static bool ShouldUseWindowsCmdShim(McpClient client)
        {
            if (client == null)
            {
                return false;
            }

            return Application.platform == RuntimePlatform.WindowsEditor &&
                   string.Equals(client.name, ClaudeDesktopConfigurator.ClientName, StringComparison.OrdinalIgnoreCase);
        }

        private static string ResolveCmdPath()
        {
            var comSpec = Environment.GetEnvironmentVariable("ComSpec");
            if (!string.IsNullOrEmpty(comSpec) && File.Exists(comSpec))
            {
                return comSpec;
            }

            string system32Cmd = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "cmd.exe");
            return File.Exists(system32Cmd) ? system32Cmd : "cmd.exe";
        }
    }
}
