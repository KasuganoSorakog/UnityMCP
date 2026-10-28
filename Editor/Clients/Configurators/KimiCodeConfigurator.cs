using System;
using System.Collections.Generic;
using System.IO;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Models;

namespace MCPForUnity.Editor.Clients.Configurators
{
    public sealed class KimiCodeConfigurator : JsonFileMcpConfigurator
    {
        private static string Home =>
            Environment.GetEnvironmentVariable("KIMI_CODE_HOME") is string home && !string.IsNullOrWhiteSpace(home)
                ? home
                : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".kimi-code");

        public KimiCodeConfigurator() : base(new McpClient
        {
            name = "Kimi Code",
            windowsConfigPath = Path.Combine(Home, "mcp.json"),
            macConfigPath = Path.Combine(Home, "mcp.json"),
            linuxConfigPath = Path.Combine(Home, "mcp.json"),
            DefaultUnityFields =
            {
                { "enabled", true },
                { "toolTimeoutMs", 180000 },
            },
        })
        { }

        public override void Configure()
        {
            string path = Path.Combine(Home, "config.toml");
            string toml = File.Exists(path) ? File.ReadAllText(path) : string.Empty;
            string updated = DesktopClientConfiguration.AddKimiReadOnlyPermissions(toml);
            base.Configure();
            if (!string.Equals(toml, updated, StringComparison.Ordinal))
            {
                McpConfigurationHelper.EnsureConfigDirectoryExists(path);
                McpConfigurationHelper.WriteAtomicFile(path, updated);
            }
        }

        public override IList<string> GetInstallationSteps() => new List<string>
        {
            "点击“配置”合并 ~/.kimi-code/mcp.json 的 HTTP 注册。",
            "同时在 config.toml 中添加精确的只读工具 allow 规则；写工具不会免审批。",
            "尊重 KIMI_CODE_HOME；已有 deny 规则会保留。重启 Kimi Code 会话后生效。",
        };
    }
}
