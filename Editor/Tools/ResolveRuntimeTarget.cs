using MCPForUnity.Editor.Helpers;
using Newtonsoft.Json.Linq;

namespace MCPForUnity.Editor.Tools
{
    [McpForUnityTool("resolve_runtime_target", AutoRegister = false)]
    internal static class ResolveRuntimeTarget
    {
        public static object HandleCommand(JObject @params)
        {
            string target = @params?["target"]?.ToString()?.Trim();
            bool includeInactive = @params?["includeInactive"]?.ToObject<bool?>() ?? false;
            if (!RuntimeTargetResolver.TryResolve(target, out var gameObject, out string error, includeInactive))
                return new ErrorResponse(error);
            return new SuccessResponse("Runtime target resolved.", new
            {
                target = RuntimeTargetResolver.GetHierarchyPath(gameObject.transform),
                targetInstanceId = gameObject.GetInstanceID(),
                active = gameObject.activeInHierarchy,
            });
        }
    }
}
