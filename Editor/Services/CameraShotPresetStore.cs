using System;
using System.Collections.Generic;
using System.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Models;
using UnityEditor;
using UnityEngine;

namespace MCPForUnity.Editor.Services
{
    internal static class CameraShotPresetStore
    {
        public const string AssetPath = "Assets/Settings/UnityMCP/CameraShotPresets.asset";

        public static IReadOnlyList<CameraShotPreset> GetAll()
        {
            return Load()?.shots ?? new List<CameraShotPreset>();
        }

        public static CameraShotPreset Find(string name)
        {
            return GetAll().FirstOrDefault(shot =>
                string.Equals(shot.name, name, StringComparison.OrdinalIgnoreCase));
        }

        public static bool Exists(string name) => Find(name) != null;

        public static CameraShotPreset CreateFromSceneView(
            SceneView sceneView,
            string name,
            bool followTarget,
            GameObject target)
        {
            if (sceneView == null || sceneView.camera == null)
                throw new InvalidOperationException("Open a Scene view before saving a camera shot preset.");
            if (string.IsNullOrWhiteSpace(name))
                throw new ArgumentException("Preset name is required.", nameof(name));
            if (followTarget && target == null)
                throw new InvalidOperationException("Select a target GameObject before enabling target following.");

            var camera = sceneView.camera;
            var preset = new CameraShotPreset
            {
                name = name.Trim(),
                position = camera.transform.position,
                rotation = camera.transform.rotation,
                fieldOfView = camera.fieldOfView,
                followTarget = followTarget,
            };
            if (!followTarget) return preset;

            preset.targetPath = RuntimeTargetResolver.GetHierarchyPath(target.transform);
            preset.targetScene = target.scene.path;
            preset.targetGlobalObjectId = GlobalObjectId.GetGlobalObjectIdSlow(target).ToString();
            preset.targetInstanceId = target.GetInstanceID();
            preset.targetLocalPosition = target.transform.InverseTransformPoint(camera.transform.position);
            preset.targetLocalRotation = Quaternion.Inverse(target.transform.rotation) * camera.transform.rotation;
            return preset;
        }

        public static void Save(CameraShotPreset preset, bool overwrite)
        {
            if (preset == null || string.IsNullOrWhiteSpace(preset.name))
                throw new ArgumentException("Preset name is required.", nameof(preset));

            var presets = Load();
            if (presets == null)
            {
                EnsureFolder();
                presets = ScriptableObject.CreateInstance<CameraShotPresets>();
                presets.shots = new List<CameraShotPreset>();
                AssetDatabase.CreateAsset(presets, AssetPath);
            }

            int existingIndex = presets.shots.FindIndex(shot =>
                string.Equals(shot.name, preset.name, StringComparison.OrdinalIgnoreCase));
            if (existingIndex >= 0 && !overwrite)
                throw new InvalidOperationException($"Camera shot preset '{preset.name}' already exists.");

            Undo.RecordObject(presets, "Save MCP Camera Shot Preset");
            if (existingIndex >= 0) presets.shots[existingIndex] = preset;
            else presets.shots.Add(preset);
            EditorUtility.SetDirty(presets);
            AssetDatabase.SaveAssets();
        }

        public static bool Delete(string name)
        {
            var presets = Load();
            if (presets == null) return false;
            var shot = presets.shots.FirstOrDefault(item =>
                string.Equals(item.name, name, StringComparison.OrdinalIgnoreCase));
            if (shot == null) return false;

            Undo.RecordObject(presets, "Delete MCP Camera Shot Preset");
            presets.shots.Remove(shot);
            EditorUtility.SetDirty(presets);
            AssetDatabase.SaveAssets();
            return true;
        }

        public static bool TryGetCameraPose(
            CameraShotPreset preset,
            out Vector3 position,
            out Quaternion rotation,
            out GameObject target,
            out string error)
        {
            position = preset.position;
            rotation = preset.rotation;
            target = null;
            error = null;
            if (!preset.followTarget) return true;

            if (!string.IsNullOrWhiteSpace(preset.targetGlobalObjectId) &&
                GlobalObjectId.TryParse(preset.targetGlobalObjectId, out var globalId))
            {
                try { target = GlobalObjectId.GlobalObjectIdentifierToObjectSlow(globalId) as GameObject; }
                catch (Exception ex) { error = ex.Message; }
            }
            if (target == null)
            {
                target = RuntimeTargetResolver.EnumerateSceneObjects(includeInactive: true)
                    .FirstOrDefault(go => string.Equals(
                        RuntimeTargetResolver.GetHierarchyPath(go.transform),
                        preset.targetPath,
                        StringComparison.OrdinalIgnoreCase));
            }
            if (target == null && string.IsNullOrWhiteSpace(preset.targetGlobalObjectId))
                RuntimeTargetResolver.TryResolve(preset.targetInstanceId.ToString(), out target, out _, includeInactive: true);
            if (target == null)
            {
                error = $"Follow target for preset '{preset.name}' could not be resolved by persistent object ID or exact hierarchy path '{preset.targetPath}'. Ensure its scene is loaded; a same-named object is not substituted.";
                return false;
            }
            if (!string.IsNullOrEmpty(preset.targetScene) && target.scene.path != preset.targetScene)
            {
                error = $"Follow target for preset '{preset.name}' is now in scene '{target.scene.path}', expected '{preset.targetScene}'.";
                target = null;
                return false;
            }

            position = target.transform.TransformPoint(preset.targetLocalPosition);
            rotation = target.transform.rotation * preset.targetLocalRotation;
            return true;
        }

        private static CameraShotPresets Load()
        {
            return AssetDatabase.LoadAssetAtPath<CameraShotPresets>(AssetPath);
        }

        private static void EnsureFolder()
        {
            string[] parts = AssetPath.Split('/');
            string current = parts[0];
            for (int i = 1; i < parts.Length - 1; i++)
            {
                string next = current + "/" + parts[i];
                if (!AssetDatabase.IsValidFolder(next))
                    AssetDatabase.CreateFolder(current, parts[i]);
                current = next;
            }
        }
    }
}
