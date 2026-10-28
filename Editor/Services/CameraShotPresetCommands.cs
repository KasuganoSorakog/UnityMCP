using System;
using System.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Models;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace MCPForUnity.Editor.Services
{
    internal static class CameraShotPresetCommands
    {
        internal static object Save(JObject parameters)
        {
            string name = parameters?["presetName"]?.ToString()?.Trim();
            if (string.IsNullOrWhiteSpace(name) || name.Length > 80)
                return new ErrorResponse("preset_name must be a non-empty name of at most 80 characters.");
            if (CameraShotPresetStore.GetAll().Any(preset => string.Equals(preset.name, name, StringComparison.OrdinalIgnoreCase)))
                return new ErrorResponse($"Camera shot preset '{name}' already exists. Choose another name or delete it explicitly before replacing it.");

            Camera source = SceneView.lastActiveSceneView != null ? SceneView.lastActiveSceneView.camera : Camera.main;
            if (source == null) return new ErrorResponse("No Scene view or main camera is available for a camera preset.");
            var preset = new CameraShotPreset
            {
                name = name,
                position = source.transform.position,
                rotation = source.transform.rotation,
                fieldOfView = source.fieldOfView,
            };
            string targetPath = parameters?["followTarget"]?.ToString();
            if (!string.IsNullOrWhiteSpace(targetPath))
            {
                if (!RuntimeTargetResolver.TryResolve(targetPath, out GameObject target, out string error, includeInactive: true))
                    return new ErrorResponse(error);
                if (SceneView.lastActiveSceneView == null || source == null)
                    return new ErrorResponse("Open a Scene view to save a following shot; the relative pose must be well-defined.");
                preset = CameraShotPresetStore.CreateFromSceneView(SceneView.lastActiveSceneView, name, true, target);
            }
            CameraShotPresetStore.Save(preset, overwrite: false);
            return new SuccessResponse($"Saved camera shot preset '{name}'.", new
            {
                preset = name,
                assetPath = CameraShotPresetStore.AssetPath,
                followTarget = preset.followTarget,
                target = preset.targetPath,
                position = new[] { preset.position.x, preset.position.y, preset.position.z },
                rotationEuler = new[] { preset.rotation.eulerAngles.x, preset.rotation.eulerAngles.y, preset.rotation.eulerAngles.z },
                fieldOfView = preset.fieldOfView,
            });
        }
    }
}
