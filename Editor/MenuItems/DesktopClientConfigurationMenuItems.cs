using MCPForUnity.Editor.Clients.Configurators;
using UnityEditor;

namespace MCPForUnity.Editor.MenuItems
{
    public static class DesktopClientConfigurationMenuItems
    {
        [MenuItem("Tools/UnityMCP/Clients/Configure Kimi Code")]
        public static void ConfigureKimiCode() => new KimiCodeConfigurator().Configure();

        [MenuItem("Tools/UnityMCP/Clients/Configure Qoder")]
        public static void ConfigureQoder() => new QoderConfigurator().Configure();

        [MenuItem("Tools/UnityMCP/Clients/Configure WorkBuddy")]
        public static void ConfigureWorkBuddy() => new WorkBuddyConfigurator().Configure();
    }
}
