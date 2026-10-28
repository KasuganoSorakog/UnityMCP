using System;
using System.Collections.Generic;
using System.IO;
using MCPForUnity.Editor.Models;

namespace MCPForUnity.Editor.Clients.Configurators
{
    public sealed class WorkBuddyConfigurator : JsonFileMcpConfigurator
    {
        private static string ConfigPath => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".workbuddy", "mcp.json");

        public WorkBuddyConfigurator() : base(new McpClient
        {
            name = "WorkBuddy",
            windowsConfigPath = ConfigPath,
            macConfigPath = ConfigPath,
            linuxConfigPath = ConfigPath,
            IncludeTransportType = true,
        })
        { }

        public override IList<string> GetInstallationSteps() => new List<string>
        {
            "点击“配置”合并 ~/.workbuddy/mcp.json 的 HTTP 注册，已有 unity-mcp 项会被复用。",
            "重启 WorkBuddy 桌面版后连接 UnityMCP。",
            "桌面版的只读免审批文件格式尚未得到官方确认，本配置器不会修改 mcp-approvals.json。",
        };
    }
}
