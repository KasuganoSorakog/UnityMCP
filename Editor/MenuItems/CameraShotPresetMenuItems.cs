using System;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;
using UnityEditor;
using UnityEngine;

namespace MCPForUnity.Editor.MenuItems
{
    internal static class CameraShotPresetMenuItems
    {
        [MenuItem("Tools/UnityMCP/Camera Shots/Save Current Scene View...", priority = 100)]
        private static void SaveCurrentSceneView()
        {
            CameraShotNameWindow.Open();
        }

        [MenuItem("Tools/UnityMCP/Camera Shots/Save Current Scene View...", true)]
        private static bool CanSaveCurrentSceneView()
        {
            return SceneView.lastActiveSceneView != null;
        }

        private sealed class CameraShotNameWindow : EditorWindow
        {
            private string _name;
            private bool _followTarget;

            public static void Open()
            {
                var window = CreateInstance<CameraShotNameWindow>();
                window.titleContent = new GUIContent("Save MCP Camera Shot");
                window._name = "Shot " + DateTime.Now.ToString("yyyyMMdd-HHmmss");
                window._followTarget = Selection.activeGameObject != null;
                window.minSize = new Vector2(400, 120);
                window.maxSize = new Vector2(500, 120);
                window.ShowModal();
            }

            private void OnGUI()
            {
                EditorGUILayout.LabelField("Save the active Scene view camera as a reusable MCP shot.", EditorStyles.wordWrappedLabel);
                _name = EditorGUILayout.TextField("Preset name", _name);
                using (new EditorGUI.DisabledScope(Selection.activeGameObject == null))
                {
                    _followTarget = EditorGUILayout.Toggle("Follow selected position and rotation", _followTarget);
                }
                if (Selection.activeGameObject == null)
                    EditorGUILayout.HelpBox("Select a scene target to enable target following.", MessageType.None);

                EditorGUILayout.Space();
                using (new EditorGUI.DisabledScope(string.IsNullOrWhiteSpace(_name)))
                {
                    if (!GUILayout.Button("Save Preset")) return;
                    try
                    {
                        bool overwrite = CameraShotPresetStore.Exists(_name.Trim());
                        if (overwrite && !EditorUtility.DisplayDialog(
                                "Replace camera shot preset",
                                $"Preset '{_name.Trim()}' already exists. Replace it?",
                                "Replace",
                                "Cancel"))
                            return;

                        var preset = CameraShotPresetStore.CreateFromSceneView(
                            SceneView.lastActiveSceneView,
                            _name,
                            _followTarget,
                            Selection.activeGameObject);
                        CameraShotPresetStore.Save(preset, overwrite);
                        McpLog.Info($"Saved MCP camera shot preset '{preset.name}' to '{CameraShotPresetStore.AssetPath}'.");
                        Close();
                    }
                    catch (Exception ex)
                    {
                        EditorUtility.DisplayDialog("Could not save camera shot", ex.Message, "OK");
                    }
                }
            }
        }
    }
}
