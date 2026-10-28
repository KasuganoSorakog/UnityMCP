using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;
using Newtonsoft.Json.Linq;

namespace MCPForUnity.Editor.Tools
{
    [McpForUnityTool("sample_runtime", AutoRegister = false)]
    public static class SampleRuntime
    {
        public static object HandleCommand(JObject @params)
        {
            @params ??= new JObject();
            string action = (@params["action"]?.ToString() ?? "sample").Trim().ToLowerInvariant();
            try
            {
                string sampleId = @params["sampleId"]?.ToString() ?? Guid.NewGuid().ToString("N");
                RuntimeSamplingService.ValidateSampleId(sampleId);
                if (action == "result") return RuntimeSamplingService.GetResult(sampleId);
                if (action == "cancel") return RuntimeSamplingService.Cancel(sampleId);
                if (action != "sample") return new ErrorResponse("Unknown action. Valid actions: sample, result, cancel.");
                var request = new RuntimeSampleRequest
                {
                    SampleId = sampleId,
                    Target = @params["target"]?.ToString()?.Trim(),
                    IncludeInactive = ParseBool(@params["includeInactive"], false),
                    Duration = ParseFloat(@params["duration"], 10f, 0.1f, 60f, "duration"),
                    IncludeSamples = ParseBool(@params["includeSamples"], false),
                    MaxSamples = ParseInt(@params["maxSamples"], 120, 2, 600, "maxSamples"),
                    MaxSpikes = ParseInt(@params["maxSpikes"], 20, 1, 100, "maxSpikes"),
                    Properties = ParseProperties(@params["properties"] ?? @params["fields"]),
                    Thresholds = ParseThresholds(@params["spikeThresholds"] ?? @params["spike_threshold"]),
                };
                if (string.IsNullOrWhiteSpace(request.Target)) return new ErrorResponse("target is required; provide a hierarchy path, unique GameObject/bone name, or instance ID.");
                return RuntimeSamplingService.StartSample(request);
            }
            catch (Exception ex)
            {
                return new ErrorResponse($"sample_runtime failed: {ex.Message}");
            }
        }

        private static List<string> ParseProperties(JToken token)
        {
            if (token == null || token.Type == JTokenType.Null)
                return new List<string> { "transform.position", "transform.rotation", "animator" };
            var properties = token is JArray array
                ? array.Select(item => item?.ToString()?.Trim()).ToList()
                : token.ToString().Split(',').Select(item => item.Trim()).ToList();
            properties = properties.Where(item => !string.IsNullOrWhiteSpace(item)).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
            if (properties.Count == 0 || properties.Count > 16)
                throw new ArgumentException("properties must contain 1-16 distinct paths; Transform and Animator entries count toward this limit.");
            if (properties.Any(item => item.Length > 256))
                throw new ArgumentException("A property path cannot exceed 256 characters.");
            return properties;
        }

        private static Dictionary<string, float> ParseThresholds(JToken token)
        {
            var result = new Dictionary<string, float>(StringComparer.OrdinalIgnoreCase);
            if (token == null || token.Type == JTokenType.Null) return result;
            if (token.Type == JTokenType.String && token.ToString().Trim().Equals("auto", StringComparison.OrdinalIgnoreCase)) return result;
            if (token is JObject map)
            {
                foreach (var property in map.Properties())
                    result[property.Name == "*" ? "*" : property.Name.Trim()] = ParseFloat(property.Value, 0f, 0.000001f, float.MaxValue, "spike threshold");
                return result;
            }
            result["*"] = ParseFloat(token, 0f, 0.000001f, float.MaxValue, "spike_threshold");
            return result;
        }

        private static float ParseFloat(JToken token, float defaultValue, float min, float max, string name)
        {
            if (token == null || token.Type == JTokenType.Null) return defaultValue;
            if (!float.TryParse(token.ToString(), NumberStyles.Float, CultureInfo.InvariantCulture, out float value) ||
                float.IsNaN(value) || float.IsInfinity(value) || value < min || value > max)
                throw new ArgumentException($"{name} must be between {min} and {max}.");
            return value;
        }

        private static int ParseInt(JToken token, int defaultValue, int min, int max, string name)
        {
            if (token == null || token.Type == JTokenType.Null) return defaultValue;
            if (!int.TryParse(token.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int value) || value < min || value > max)
                throw new ArgumentException($"{name} must be an integer between {min} and {max}.");
            return value;
        }

        private static bool ParseBool(JToken token, bool defaultValue)
        {
            if (token == null || token.Type == JTokenType.Null) return defaultValue;
            if (bool.TryParse(token.ToString(), out bool value)) return value;
            if (token.ToString() == "1") return true;
            if (token.ToString() == "0") return false;
            throw new ArgumentException($"Expected a boolean, got '{token}'.");
        }
    }
}
