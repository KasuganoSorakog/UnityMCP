using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.External.Tommy;
using Newtonsoft.Json.Linq;

namespace MCPForUnity.Editor.Clients
{
    public static class DesktopClientConfiguration
    {
        public static readonly string[] ReadOnlyTools =
        {
            "find_gameobjects",
            "find_in_file",
            "get_sha",
            "get_test_job",
            "health",
            "manage_script_capabilities",
            "validate_script",
        };

        public static string AddQoderReadOnlyPermissions(string json, string serverName = "unityMCP")
        {
            if (string.IsNullOrWhiteSpace(serverName)) throw new ArgumentException("A Qoder MCP server name is required.", nameof(serverName));
            var root = string.IsNullOrWhiteSpace(json) ? new JObject() : JObject.Parse(json);
            if (root["permissions"] is not JObject permissions)
            {
                permissions = new JObject();
                root["permissions"] = permissions;
            }
            if (permissions["allow"] is not JArray allow)
            {
                allow = new JArray();
                permissions["allow"] = allow;
            }

            var existing = new HashSet<string>(
                allow.Values<string>().Where(value => value != null),
                StringComparer.Ordinal);
            foreach (string tool in ReadOnlyTools)
            {
                string rule = $"mcp__{serverName}__{tool}";
                if (existing.Add(rule)) allow.Add(rule);
            }
            return root.ToString(Newtonsoft.Json.Formatting.Indented);
        }

        public static string AddKimiReadOnlyPermissions(string toml)
        {
            if (!string.IsNullOrWhiteSpace(toml))
            {
                try
                {
                    using var reader = new StringReader(toml);
                    TOML.Parse(reader);
                }
                catch (Exception ex) when (ex is TomlParseException || ex is TomlSyntaxException || ex is FormatException)
                {
                    throw new InvalidDataException("Kimi Code config.toml is invalid; the original file was left unchanged.", ex);
                }
            }

            var rules = new List<string>();
            foreach (string tool in ReadOnlyTools)
            {
                string pattern = $"mcp__unityMCP__{tool}";
                if (HasRule(toml, pattern)) continue;
                rules.Add($"[[permission.rules]]\ndecision = \"allow\"\npattern = \"{pattern}\"");
            }
            if (rules.Count == 0) return toml ?? string.Empty;

            string lineEnding = toml?.Contains("\r\n") == true ? "\r\n" : "\n";
            string insertion = string.Join(lineEnding + lineEnding, rules).Replace("\n", lineEnding);
            var firstRule = Regex.Match(
                toml ?? string.Empty,
                @"(?m)^[ \t]*\[\[\s*permission\s*\.\s*rules\s*\]\][ \t]*(?:#.*)?\r?$");
            string result;
            if (firstRule.Success)
            {
                result = toml.Insert(firstRule.Index, insertion + lineEnding + lineEnding);
            }
            else
            {
                string prefix = toml ?? string.Empty;
                if (prefix.Length > 0 && !prefix.EndsWith("\n", StringComparison.Ordinal))
                    prefix += lineEnding;
                if (!HasPermissionTable(toml))
                    insertion = "[permission]" + lineEnding + insertion;
                result = prefix + lineEnding + insertion + lineEnding;
            }
            using (var reader = new StringReader(result))
                TOML.Parse(reader);
            return result;
        }

        private static bool HasPermissionTable(string toml)
        {
            if (string.IsNullOrWhiteSpace(toml)) return false;
            using var reader = new StringReader(toml);
            return TOML.Parse(reader).TryGetNode("permission", out var node) && node is TomlTable;
        }

        private static bool HasRule(string toml, string pattern)
        {
            if (string.IsNullOrWhiteSpace(toml)) return false;
            using var reader = new StringReader(toml);
            if (!TOML.Parse(reader).TryGetNode("permission", out var permission) ||
                permission is not TomlTable permissionTable ||
                !permissionTable.TryGetNode("rules", out var rulesNode) ||
                rulesNode is not TomlArray rules)
                return false;

            foreach (var rule in rules.Children.OfType<TomlTable>())
            {
                if (rule.TryGetNode("pattern", out var value) &&
                    value is TomlString text &&
                    string.Equals(text.Value, pattern, StringComparison.Ordinal))
                {
                    if (rule.TryGetNode("decision", out var decision) &&
                        decision is TomlString decisionText &&
                        !string.Equals(decisionText.Value, "allow", StringComparison.Ordinal))
                        McpLog.Warn($"UnityMCP: Kimi Code has an explicit '{decisionText.Value}' rule for {pattern}; it was preserved.");
                    return true;
                }
            }
            return false;
        }
    }
}
