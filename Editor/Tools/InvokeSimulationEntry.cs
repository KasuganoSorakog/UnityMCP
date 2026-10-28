using System;
using System.Linq;
using System.Reflection;
using MCPForUnity.Editor.Helpers;
using Newtonsoft.Json.Linq;

namespace MCPForUnity.Editor.Tools
{
    [McpForUnityTool("invoke_simulation_entry", AutoRegister = false)]
    internal static class InvokeSimulationEntry
    {
        public static object HandleCommand(JObject parameters)
        {
            string entry = parameters?["entry"]?.ToString();
            if (string.IsNullOrWhiteSpace(entry) || entry.Length > 512)
                return new ErrorResponse("entry must be a non-empty Namespace.Type::Method string of at most 512 characters.");
            string[] parts = entry.Split(new[] { "::" }, StringSplitOptions.None);
            if (parts.Length != 2 || parts.Any(string.IsNullOrWhiteSpace))
                return new ErrorResponse("entry must use the Namespace.Type::Method form; assembly-qualified names are not accepted.");
            string typeName = parts[0].Trim();
            string methodName = parts[1].Trim();
            if (typeName.Any(c => !(char.IsLetterOrDigit(c) || c == '_' || c == '.' || c == '+')) ||
                !methodName.All(c => char.IsLetterOrDigit(c) || c == '_'))
                return new ErrorResponse("entry contains unsupported type or method characters.");

            try
            {
                var types = AppDomain.CurrentDomain.GetAssemblies()
                    .Where(assembly => !assembly.IsDynamic)
                    .Select(assembly => assembly.GetType(typeName, false, false))
                    .Where(type => type != null)
                    .Distinct()
                    .Take(2)
                    .ToArray();
                if (types.Length == 0) return new ErrorResponse($"Simulation entry type '{typeName}' was not found.");
                if (types.Length > 1) return new ErrorResponse($"Simulation entry type '{typeName}' is ambiguous across loaded assemblies.");
                var type = types[0];
                if (type.ContainsGenericParameters) return new ErrorResponse("Generic simulation entry types are not supported.");
                var method = type.GetMethod(methodName,
                    BindingFlags.Public | BindingFlags.Static, null, Type.EmptyTypes, null);
                if (method == null)
                    return new ErrorResponse($"Simulation entry '{entry}' must be a public static method without parameters.");
                if (method.ContainsGenericParameters) return new ErrorResponse("Generic simulation entry methods are not supported.");
                method.Invoke(null, null);
                return new SuccessResponse($"Simulation entry '{entry}' completed.", new { entry, completed = true });
            }
            catch (TargetInvocationException ex)
            {
                var cause = ex.InnerException ?? ex;
                return new ErrorResponse($"Simulation entry '{entry}' threw {cause.GetType().FullName}: {cause.Message}",
                    new { entry, exceptionType = cause.GetType().FullName, stackTrace = cause.StackTrace });
            }
            catch (Exception ex)
            {
                return new ErrorResponse($"Could not invoke simulation entry '{entry}': {ex.Message}");
            }
        }
    }
}
