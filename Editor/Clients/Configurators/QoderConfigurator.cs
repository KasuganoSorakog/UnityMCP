using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Models;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace MCPForUnity.Editor.Clients.Configurators
{
    public sealed class QoderConfigurator : JsonFileMcpConfigurator
    {
        private static string ProjectPath =>
            Path.Combine(Path.GetDirectoryName(Application.dataPath) ?? string.Empty, ".qoder", "settings.local.json");

        public QoderConfigurator() : base(new McpClient
        {
            name = "Qoder",
            windowsConfigPath = ProjectPath,
            macConfigPath = ProjectPath,
            linuxConfigPath = ProjectPath,
        })
        { }

        public override void Configure()
        {
            base.Configure();
            string path = GetConfigPath();
            string json = File.Exists(path) ? File.ReadAllText(path) : string.Empty;
            var root = JObject.Parse(json);
            if (!ConfigJsonBuilder.TryGetUnityServer(root, client, out var server) || server.Parent is not JProperty property)
                throw new InvalidOperationException("UnityMCP: Qoder registration is missing after configuration.");
            string updated = DesktopClientConfiguration.AddQoderReadOnlyPermissions(json, property.Name);
            var allow = JObject.Parse(updated)["permissions"]?["allow"] as JArray;
            bool hasWildcard = allow?.Values<string>().Any(rule =>
                rule != null && rule.StartsWith("mcp__unity", StringComparison.OrdinalIgnoreCase) &&
                rule.EndsWith("__*", StringComparison.Ordinal)) == true;
            if (hasWildcard)
                McpLog.Warn("UnityMCP: Qoder has an existing wildcard MCP allow rule. It was preserved; remove it manually to enforce read-only approval bypass.");
            if (!string.Equals(json, updated, StringComparison.Ordinal))
                McpConfigurationHelper.WriteAtomicFile(path, updated);
        }

        public override IList<string> GetInstallationSteps() => new List<string>
        {
            "在当前 Unity 项目中点击“配置”。",
            "配置合并到 .qoder/settings.local.json，保留其他 Server 和权限。",
            "只读工具使用精确 allow 规则；重新打开 Qoder 项目后生效。",
        };
    }
}
