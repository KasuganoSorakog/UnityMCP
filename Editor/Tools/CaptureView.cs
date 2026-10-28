using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace MCPForUnity.Editor.Tools
{
    [McpForUnityTool("capture_view", AutoRegister = false)]
    public static class CaptureView
    {
        public static async Task<object> HandleCommand(JObject @params)
        {
            string action = (@params?["action"]?.ToString() ?? "capture").Trim().ToLowerInvariant();
            try
            {
                switch (action)
                {
                    case "list_presets":
                        return ListPresets();
                    case "delete_preset":
                        return DeletePreset(@params);
                    case "capture":
                        var request = ParseRequest(@params);
                        return await ViewCaptureService.CaptureAsync(request);
                    default:
                        return new ErrorResponse($"Unknown action '{action}'. Valid actions: capture, list_presets, delete_preset.");
                }
            }
            catch (Exception ex)
            {
                McpLog.Error($"[capture_view] {action} failed: {ex}");
                return new ErrorResponse($"capture_view failed: {ex.Message}");
            }
        }

        private static object ListPresets()
        {
            var presets = CameraShotPresetStore.GetAll()
                .Select(preset => new
                {
                    name = preset.name,
                    fieldOfView = preset.fieldOfView,
                    followTarget = preset.followTarget,
                    target = preset.targetPath,
                    targetScene = preset.targetScene,
                    position = ToArray(preset.position),
                    rotationEuler = ToArray(preset.rotation.eulerAngles),
                })
                .ToArray();
            return new SuccessResponse(
                $"Found {presets.Length} camera shot preset(s).",
                new { presets, count = presets.Length, assetPath = CameraShotPresetStore.AssetPath });
        }

        private static object DeletePreset(JObject parameters)
        {
            string name = parameters?["preset"]?.ToString();
            if (string.IsNullOrWhiteSpace(name))
                return new ErrorResponse("'preset' is required for delete_preset.");
            if (parameters?["confirm"]?.ToObject<bool?>() != true)
                return new ErrorResponse("Deleting a camera shot preset requires confirm=true.");

            return CameraShotPresetStore.Delete(name.Trim())
                ? new SuccessResponse($"Deleted camera shot preset '{name.Trim()}'.")
                : new ErrorResponse($"Camera shot preset '{name.Trim()}' was not found.");
        }

        private static ViewCaptureRequest ParseRequest(JObject parameters)
        {
            parameters ??= new JObject();
            string preset = NullIfWhiteSpace(parameters["preset"]?.ToString());
            string target = NullIfWhiteSpace(parameters["target"]?.ToString());
            bool hasAngles = parameters["angles"] != null || parameters["angle"] != null;
            var angles = ParseAngles(parameters["angles"] ?? parameters["angle"]);
            var request = new ViewCaptureRequest
            {
                Target = target,
                PresetName = preset,
                Angles = angles,
                Distance = ParseDistance(parameters["distance"]),
                Frames = ParseInt(parameters["frames"], 1, 1, 16, "frames"),
                Interval = ParseFloat(parameters["interval"], 0.1f, 0f, 5f, "interval"),
                Grid = ParseBool(parameters["grid"], false),
                IncludeOverlay = ParseBool(parameters["includeOverlay"] ?? parameters["include_overlay"], true),
                IncludeInactive = ParseBool(parameters["includeInactive"] ?? parameters["include_inactive"], false),
                FileName = NullIfWhiteSpace((parameters["fileName"] ?? parameters["file_name"])?.ToString()),
                Width = ParseInt(parameters["width"], 1280, 64, 4096, "width"),
                Height = ParseInt(parameters["height"], 720, 64, 4096, "height"),
                FieldOfView = ParseNullableFloat(parameters["fov"], 5f, 120f, "fov"),
            };

            if (preset != null && (target != null || hasAngles || request.Distance.HasValue))
                throw new ArgumentException("preset cannot be combined with target, angle/angles, or distance. Save a new preset for a different composition.");
            if (target == null && preset == null && hasAngles)
                throw new ArgumentException("angle/angles requires target, unless a preset is used.");
            if (request.Angles.Count == 0)
                throw new ArgumentException("angles must contain at least one shot name.");
            if (request.Angles.Count * request.Frames > 16)
                throw new ArgumentException("angles * frames cannot exceed 16 images per call.");
            if ((request.Angles.Count * request.Frames - 1) * request.Interval > 60f)
                throw new ArgumentException("Total sequence waiting time cannot exceed 60 seconds. Reduce angles, frames, or interval.");
            return request;
        }

        private static List<string> ParseAngles(JToken token)
        {
            if (token == null || token.Type == JTokenType.Null) return new List<string> { "front" };
            if (token is JArray array)
                return array.Select(item => item?.ToString()?.Trim())
                    .Where(value => !string.IsNullOrWhiteSpace(value))
                    .ToList();
            return token.ToString()
                .Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(value => value.Trim())
                .Where(value => !string.IsNullOrWhiteSpace(value))
                .ToList();
        }

        private static float? ParseDistance(JToken token)
        {
            if (token == null || token.Type == JTokenType.Null) return null;
            string text = token.ToString().Trim();
            if (string.IsNullOrWhiteSpace(text) || text.Equals("auto", StringComparison.OrdinalIgnoreCase)) return null;
            if (!float.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out float value) ||
                float.IsNaN(value) || float.IsInfinity(value) || value < 0.01f || value > 10000f)
                throw new ArgumentException("distance must be 'auto' or a number between 0.01 and 10000.");
            return value;
        }

        private static float? ParseNullableFloat(JToken token, float min, float max, string name)
        {
            if (token == null || token.Type == JTokenType.Null || string.IsNullOrWhiteSpace(token.ToString())) return null;
            return ParseFloat(token, 0f, min, max, name);
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
            if (!int.TryParse(token.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int value) ||
                value < min || value > max)
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

        private static string NullIfWhiteSpace(string value)
        {
            return string.IsNullOrWhiteSpace(value) ? null : value.Trim();
        }

        private static float[] ToArray(Vector3 value) => new[] { value.x, value.y, value.z };
    }
}
