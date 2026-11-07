using System;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;
using UnityEditor;
using UnityEngine.UIElements;

namespace MCPForUnity.Editor.Windows.Components.CameraShots
{
    /// <summary>
    /// Controller for the camera shot section: saves the active Scene view camera
    /// as a reusable preset. Replaces the former Tools/UnityMCP/Camera Shots menu.
    /// </summary>
    public class McpCameraShotsSection
    {
        private TextField nameField;
        private Toggle followTargetToggle;
        private Button saveButton;
        private Label statusLabel;

        public McpCameraShotsSection(VisualElement root)
        {
            nameField = root.Q<TextField>("shot-name-field");
            followTargetToggle = root.Q<Toggle>("follow-target-toggle");
            saveButton = root.Q<Button>("save-shot-button");
            statusLabel = root.Q<Label>("shot-status");

            if (nameField != null)
            {
                nameField.value = NewDefaultName();
                nameField.RegisterValueChangedCallback(_ => UpdateInteractivity());
            }

            if (saveButton != null)
            {
                saveButton.clicked += OnSaveClicked;
            }

            if (followTargetToggle != null)
            {
                followTargetToggle.value = Selection.activeGameObject != null;
            }

            UpdateInteractivity();
        }

        private static string NewDefaultName() => "Shot " + DateTime.Now.ToString("yyyyMMdd-HHmmss");

        /// <summary>
        /// Refreshes button/toggle availability, called from the window's periodic update.
        /// </summary>
        public void UpdateInteractivity()
        {
            bool hasSceneView = SceneView.lastActiveSceneView != null;
            bool hasSelection = Selection.activeGameObject != null;

            if (saveButton != null)
            {
                saveButton.SetEnabled(hasSceneView && !string.IsNullOrWhiteSpace(nameField?.value));
                saveButton.tooltip = hasSceneView ? null : "打开一个 Scene 视图后可用。";
            }

            if (followTargetToggle != null)
            {
                followTargetToggle.SetEnabled(hasSceneView && hasSelection);
                followTargetToggle.tooltip = hasSceneView && !hasSelection ? "选中场景物体后可开启跟随。" : null;
            }
        }

        private void OnSaveClicked()
        {
            SceneView sceneView = SceneView.lastActiveSceneView;
            string presetName = nameField?.value?.Trim();
            if (sceneView == null || string.IsNullOrWhiteSpace(presetName))
            {
                return;
            }

            try
            {
                if (followTargetToggle != null && followTargetToggle.value && Selection.activeGameObject == null)
                {
                    if (statusLabel != null)
                    {
                        statusLabel.text = "保存失败：请先选中一个场景物体再开启跟随。";
                    }
                    return;
                }

                bool overwrite = CameraShotPresetStore.Exists(presetName);
                if (overwrite && !EditorUtility.DisplayDialog(
                        "替换相机快照预设",
                        $"预设 '{presetName}' 已存在，是否覆盖？",
                        "覆盖",
                        "取消"))
                    return;

                var preset = CameraShotPresetStore.CreateFromSceneView(
                    sceneView,
                    presetName,
                    followTargetToggle != null && followTargetToggle.value,
                    Selection.activeGameObject);
                CameraShotPresetStore.Save(preset, overwrite);
                McpLog.Info($"Saved MCP camera shot preset '{preset.name}' to '{CameraShotPresetStore.AssetPath}'.");

                if (statusLabel != null)
                {
                    statusLabel.text = $"已保存 '{preset.name}'，共 {CameraShotPresetStore.GetAll().Count} 个预设。";
                }

                if (nameField != null)
                {
                    nameField.value = NewDefaultName();
                }
            }
            catch (Exception ex)
            {
                if (statusLabel != null)
                {
                    statusLabel.text = $"保存失败：{ex.Message}";
                }
                McpLog.Error($"Failed to save MCP camera shot preset: {ex.Message}");
            }
        }
    }
}
