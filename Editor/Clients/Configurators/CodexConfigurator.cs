using System;
using System.Collections.Generic;
using System.IO;
using MCPForUnity.Editor.Models;

namespace MCPForUnity.Editor.Clients.Configurators
{
    public class CodexConfigurator : CodexMcpConfigurator
    {
        public CodexConfigurator() : base(new McpClient
        {
            name = "Codex",
            windowsConfigPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".codex", "config.toml"),
            macConfigPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".codex", "config.toml"),
            linuxConfigPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".codex", "config.toml")
        })
        { }

        public override IList<string> GetInstallationSteps() => new List<string>
        {
            "在 Codex 设置中打开 MCP servers，然后选择 Add server。",
            $"选择 Streamable HTTP，URL 填写 {MCPForUnity.Editor.Helpers.HttpEndpointUtility.GetMcpRpcUrl()}。",
            "保存并重启 Codex，然后使用 /mcp 确认服务已连接。"
        };
    }
}
