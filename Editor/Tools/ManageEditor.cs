using System;
using System.IO;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;
using MCPForUnity.Editor.Services.Transport;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditorInternal; // Required for tag management
using UnityEngine;

namespace MCPForUnity.Editor.Tools
{
    internal static class PlayModeService
    {
        private sealed class Operation
        {
            public string id;
            public string action;
            public bool targetPlaying;
            public bool targetModeEntered;
            public string status;
            public string error;
            public double deadline;
            public int startingFrame;
            public int firstFrame = -1;
            public int settlingUpdates;
            public bool domainReloadObserved;
        }

        private const string SessionKey = "MCPForUnity.PlayModeOperation";
        private static Operation _active;

        static PlayModeService()
        {
            Restore();
            EditorApplication.update += Tick;
            EditorApplication.playModeStateChanged += OnPlayModeStateChanged;
            AssemblyReloadEvents.beforeAssemblyReload += () =>
            {
                if (_active != null) _active.domainReloadObserved = true;
                Save();
            };
            EditorApplication.delayCall += Tick;
        }

        public static object Request(JObject parameters, string action)
        {
            bool targetPlaying = action == "play";
            bool wait = parameters?["waitForCompletion"]?.ToObject<bool?>() ?? true;
            string id = parameters?["operationId"]?.ToString();
            if (!string.IsNullOrEmpty(id) && (id.Length > 64 || !Guid.TryParse(id, out _)))
                return new ErrorResponse("operation_id must be a valid GUID.");
            if (_active != null)
            {
                if (id == _active.id)
                    return _active.status == "running" ? Pending() : CompleteResult(_active, false);
                if (_active.targetPlaying == targetPlaying && _active.status == "running")
                    return Pending();
                if (_active.status == "running" && targetPlaying)
                    return new ErrorResponse("A Stop operation is pending. Query its operation_id before entering Play.",
                        new { operationId = _active.id, targetPlaying = _active.targetPlaying });
                if (_active.status == "running")
                {
                    Fail("The pending Play operation was superseded by an explicit Stop request.");
                    Clear();
                }
                else Clear();
            }
            if (Application.isPlaying == targetPlaying && !EditorApplication.isPlayingOrWillChangePlaymode)
                return CompleteResult(new Operation { id = id ?? Guid.NewGuid().ToString("N"), action = action, targetPlaying = targetPlaying, targetModeEntered = true, status = "completed" }, true);

            _active = new Operation
            {
                id = id ?? Guid.NewGuid().ToString("N"), action = action, targetPlaying = targetPlaying, status = "running",
                deadline = EditorApplication.timeSinceStartup + Math.Clamp(parameters?["timeoutSeconds"]?.ToObject<double?>() ?? 90.0, 5.0, 120.0),
                startingFrame = Time.frameCount,
            };
            Save();
            try { EditorApplication.isPlaying = targetPlaying; }
            catch (Exception ex)
            {
                Fail(ex.Message);
                return CompleteResult(_active, false);
            }
            if (!wait)
                return new SuccessResponse("Play mode transition dispatched without waiting.", new { operationId = _active.id, completed = false });
            return Pending();
        }

        public static object GetStatus(JObject parameters)
        {
            string id = parameters?["operationId"]?.ToString();
            if (_active != null && (string.IsNullOrEmpty(id) || id == _active.id))
                return _active.status == "running" ? Pending() : CompleteResult(_active, false);
            if (!string.IsNullOrEmpty(id))
            {
                if (!Guid.TryParse(id, out _)) return new ErrorResponse("operation_id must be a valid GUID.");
                try
                {
                    var saved = JObject.Parse(File.ReadAllText(ResultPath(id)));
                    return saved.Value<bool>("success")
                        ? new SuccessResponse("Play mode operation completed.", saved["data"])
                        : (object)new ErrorResponse(saved.Value<string>("error"), saved["data"]);
                }
                catch (Exception ex) { return new ErrorResponse($"Play mode operation '{id}' was not found: {ex.Message}"); }
            }
            return new SuccessResponse("No Play mode operation is active.", CurrentState(null));
        }

        private static void OnPlayModeStateChanged(PlayModeStateChange change)
        {
            if (_active == null || _active.status != "running") return;
            if (change == PlayModeStateChange.EnteredPlayMode)
            {
                if (_active.targetPlaying) MarkTargetModeEntered();
                else Fail("The editor entered Play mode while a Stop operation was pending.");
            }
            else if (change == PlayModeStateChange.EnteredEditMode)
            {
                if (_active.targetPlaying) Fail("The editor returned to Edit mode before the Play operation completed.");
                else MarkTargetModeEntered();
            }
        }

        private static void MarkTargetModeEntered()
        {
            _active.targetModeEntered = true;
            _active.firstFrame = Time.frameCount;
            _active.settlingUpdates = 0;
            Save();
        }

        private static void Tick()
        {
            if (_active == null || _active.status != "running") return;
            if (!_active.targetModeEntered)
            {
                if (!_active.targetPlaying && !Application.isPlaying && !EditorApplication.isPlayingOrWillChangePlaymode)
                    MarkTargetModeEntered();
                else if (EditorApplication.timeSinceStartup > _active.deadline)
                    Fail("Play mode transition or bridge recovery did not finish before the deadline.");
                else return;
            }
            if (_active.status != "running") return;
            if (Application.isPlaying != _active.targetPlaying)
            {
                Fail("The editor left the requested Play state before the operation completed.");
                return;
            }
            if (EditorApplication.isCompiling || EditorApplication.isUpdating) return;
            if (!_active.targetPlaying && !MCPServiceLocator.TransportManager.IsRunning(TransportMode.Http)) return;
            if (EditorApplication.timeSinceStartup > _active.deadline)
            {
                Fail("Play mode transition or bridge recovery did not finish before the deadline.");
                return;
            }
            _active.settlingUpdates++;
            if (_active.settlingUpdates < 2) return;
            if (_active.targetPlaying && Time.frameCount <= _active.firstFrame) return;
            if (!_active.targetPlaying && !MCPServiceLocator.TransportManager.IsRunning(TransportMode.Http)) return;

            _active.status = "completed";
            Save();
            PersistResult(_active);
            Clear();
        }

        private static void Fail(string error)
        {
            _active.status = "failed";
            _active.error = error;
            Save();
            PersistResult(_active);
        }

        private static PendingResponse Pending()
        {
            var data = CurrentState(_active);
            data["targetPlaying"] = _active.targetPlaying;
            data["settlingUpdates"] = _active.settlingUpdates;
            data["firstFrame"] = _active.firstFrame;
            data["timeSinceStartup"] = EditorApplication.timeSinceStartup;
            data["deadline"] = _active.deadline;
            return new PendingResponse(
                "Play mode transition is in progress; poll manage_editor action=operation_status with operation_id.", 0.25, data);
        }

        private static object CompleteResult(Operation operation, bool alreadyInTargetState)
        {
            var data = CurrentState(operation);
            data["alreadyInTargetState"] = alreadyInTargetState;
            PersistResult(operation);
            Clear();
            return operation.status == "failed"
                ? new ErrorResponse(operation.error, data)
                : new SuccessResponse(alreadyInTargetState ? "Already in the requested Play state." : "Play mode transition completed.", data);
        }

        private static void PersistResult(Operation operation)
        {
            string path = ResultPath(operation.id);
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, JsonConvert.SerializeObject(new JObject
            {
                ["success"] = operation.status != "failed", ["data"] = CurrentState(operation), ["error"] = operation.error,
            }, Formatting.Indented));
        }

        private static JObject CurrentState(Operation operation) => new JObject
        {
            ["operationId"] = operation?.id,
            ["action"] = operation?.action,
            ["status"] = operation?.status,
            ["targetModeEntered"] = operation?.targetModeEntered ?? false,
            ["isPlaying"] = Application.isPlaying,
            ["isPaused"] = EditorApplication.isPaused,
            ["isCompiling"] = EditorApplication.isCompiling,
            ["isUpdating"] = EditorApplication.isUpdating,
            ["frame"] = Time.frameCount,
            ["completed"] = operation == null || operation.status != "running",
            ["domainReloadObserved"] = operation?.domainReloadObserved ?? false,
            ["reconnected"] = MCPServiceLocator.TransportManager.IsRunning(TransportMode.Http),
            ["error"] = operation?.error,
        };

        private static void Save()
        {
            if (_active != null) SessionState.SetString(SessionKey, JsonConvert.SerializeObject(_active));
        }

        private static void Clear()
        {
            _active = null;
            SessionState.SetString(SessionKey, string.Empty);
        }

        private static void Restore()
        {
            try
            {
                string json = SessionState.GetString(SessionKey, null);
                if (string.IsNullOrEmpty(json)) return;
                _active = JsonConvert.DeserializeObject<Operation>(json);
                if (_active?.status == "running" && Application.isPlaying == _active.targetPlaying)
                {
                    _active.targetModeEntered = true;
                    _active.firstFrame = Time.frameCount;
                    _active.settlingUpdates = 0;
                    Save();
                }
            }
            catch (Exception ex) { McpLog.Warn($"Could not restore Play/Stop operation: {ex.Message}"); }
        }

        private static string ResultPath(string id) => Path.Combine(Path.GetFullPath(Path.Combine(Application.dataPath, "..")),
            "Library", "MCPForUnity", "RunState", "PlayMode-" + id + ".json");
    }

    /// <summary>
    /// Handles editor control actions including play mode control, tool selection,
    /// and tag/layer management. For reading editor state, use MCP resources instead.
    /// </summary>
    [McpForUnityTool("manage_editor", AutoRegister = false)]
    public static class ManageEditor
    {
        // Constant for starting user layer index
        private const int FirstUserLayerIndex = 8;

        // Constant for total layer count
        private const int TotalLayerCount = 32;

        /// <summary>
        /// Main handler for editor management actions.
        /// </summary>
        public static object HandleCommand(JObject @params)
        {
            string action = @params?["action"]?.ToString().ToLower();
            // Parameters for specific actions
            string tagName = @params?["tagName"]?.ToString();
            string layerName = @params?["layerName"]?.ToString();

            if (string.IsNullOrEmpty(action))
            {
                return new ErrorResponse("Action parameter is required.");
            }

            if (action == "play" || action == "stop")
                return PlayModeService.Request(@params, action);
            if (action == "operation_status")
                return PlayModeService.GetStatus(@params);

            switch (action)
            {
                case "pause":
                    try
                    {
                        if (EditorApplication.isPlaying)
                        {
                            EditorApplication.isPaused = !EditorApplication.isPaused;
                            return new SuccessResponse(
                                EditorApplication.isPaused ? "Game paused." : "Game resumed."
                            );
                        }
                        return new ErrorResponse("Cannot pause/resume: Not in play mode.");
                    }
                    catch (Exception e)
                    {
                        return new ErrorResponse($"Error pausing/resuming game: {e.Message}");
                    }

                // Tool Control
                case "set_active_tool":
                    string toolName = @params["toolName"]?.ToString();
                    if (string.IsNullOrEmpty(toolName))
                        return new ErrorResponse("'toolName' parameter required for set_active_tool.");
                    return SetActiveTool(toolName);

                // Tag Management
                case "add_tag":
                    if (string.IsNullOrEmpty(tagName))
                        return new ErrorResponse("'tagName' parameter required for add_tag.");
                    return AddTag(tagName);
                case "remove_tag":
                    if (string.IsNullOrEmpty(tagName))
                        return new ErrorResponse("'tagName' parameter required for remove_tag.");
                    return RemoveTag(tagName);
                // Layer Management
                case "add_layer":
                    if (string.IsNullOrEmpty(layerName))
                        return new ErrorResponse("'layerName' parameter required for add_layer.");
                    return AddLayer(layerName);
                case "remove_layer":
                    if (string.IsNullOrEmpty(layerName))
                        return new ErrorResponse("'layerName' parameter required for remove_layer.");
                    return RemoveLayer(layerName);
                // --- Settings (Example) ---
                // case "set_resolution":
                //     int? width = @params["width"]?.ToObject<int?>();
                //     int? height = @params["height"]?.ToObject<int?>();
                //     if (!width.HasValue || !height.HasValue) return new ErrorResponse("'width' and 'height' parameters required.");
                //     return SetGameViewResolution(width.Value, height.Value);
                // case "set_quality":
                //     // Handle string name or int index
                //     return SetQualityLevel(@params["qualityLevel"]);

                default:
                    return new ErrorResponse(
                        $"Unknown action: '{action}'. Supported actions: play, pause, stop, set_active_tool, add_tag, remove_tag, add_layer, remove_layer. Use MCP resources for reading editor state, project info, tags, layers, selection, windows, prefab stage, and active tool."
                    );
            }
        }

        // --- Tool Control Methods ---

        private static object SetActiveTool(string toolName)
        {
            try
            {
                Tool targetTool;
                if (Enum.TryParse<Tool>(toolName, true, out targetTool)) // Case-insensitive parse
                {
                    // Check if it's a valid built-in tool
                    if (targetTool != Tool.None && targetTool <= Tool.Custom) // Tool.Custom is the last standard tool
                    {
                        UnityEditor.Tools.current = targetTool;
                        return new SuccessResponse($"Set active tool to '{targetTool}'.");
                    }
                    else
                    {
                        return new ErrorResponse(
                            $"Cannot directly set tool to '{toolName}'. It might be None, Custom, or invalid."
                        );
                    }
                }
                else
                {
                    // Potentially try activating a custom tool by name here if needed
                    // This often requires specific editor scripting knowledge for that tool.
                    return new ErrorResponse(
                        $"Could not parse '{toolName}' as a standard Unity Tool (View, Move, Rotate, Scale, Rect, Transform, Custom)."
                    );
                }
            }
            catch (Exception e)
            {
                return new ErrorResponse($"Error setting active tool: {e.Message}");
            }
        }

        // --- Tag Management Methods ---

        private static object AddTag(string tagName)
        {
            if (string.IsNullOrWhiteSpace(tagName))
                return new ErrorResponse("Tag name cannot be empty or whitespace.");

            // Check if tag already exists
            if (System.Linq.Enumerable.Contains(InternalEditorUtility.tags, tagName))
            {
                return new ErrorResponse($"Tag '{tagName}' already exists.");
            }

            try
            {
                // Add the tag using the internal utility
                InternalEditorUtility.AddTag(tagName);
                // Force save assets to ensure the change persists in the TagManager asset
                AssetDatabase.SaveAssets();
                return new SuccessResponse($"Tag '{tagName}' added successfully.");
            }
            catch (Exception e)
            {
                return new ErrorResponse($"Failed to add tag '{tagName}': {e.Message}");
            }
        }

        private static object RemoveTag(string tagName)
        {
            if (string.IsNullOrWhiteSpace(tagName))
                return new ErrorResponse("Tag name cannot be empty or whitespace.");
            if (tagName.Equals("Untagged", StringComparison.OrdinalIgnoreCase))
                return new ErrorResponse("Cannot remove the built-in 'Untagged' tag.");

            // Check if tag exists before attempting removal
            if (!System.Linq.Enumerable.Contains(InternalEditorUtility.tags, tagName))
            {
                return new ErrorResponse($"Tag '{tagName}' does not exist.");
            }

            try
            {
                // Remove the tag using the internal utility
                InternalEditorUtility.RemoveTag(tagName);
                // Force save assets
                AssetDatabase.SaveAssets();
                return new SuccessResponse($"Tag '{tagName}' removed successfully.");
            }
            catch (Exception e)
            {
                // Catch potential issues if the tag is somehow in use or removal fails
                return new ErrorResponse($"Failed to remove tag '{tagName}': {e.Message}");
            }
        }

        // --- Layer Management Methods ---

        private static object AddLayer(string layerName)
        {
            if (string.IsNullOrWhiteSpace(layerName))
                return new ErrorResponse("Layer name cannot be empty or whitespace.");

            // Access the TagManager asset
            SerializedObject tagManager = GetTagManager();
            if (tagManager == null)
                return new ErrorResponse("Could not access TagManager asset.");

            SerializedProperty layersProp = tagManager.FindProperty("layers");
            if (layersProp == null || !layersProp.isArray)
                return new ErrorResponse("Could not find 'layers' property in TagManager.");

            // Check if layer name already exists (case-insensitive check recommended)
            for (int i = 0; i < TotalLayerCount; i++)
            {
                SerializedProperty layerSP = layersProp.GetArrayElementAtIndex(i);
                if (
                    layerSP != null
                    && layerName.Equals(layerSP.stringValue, StringComparison.OrdinalIgnoreCase)
                )
                {
                    return new ErrorResponse($"Layer '{layerName}' already exists at index {i}.");
                }
            }

            // Find the first empty user layer slot (indices 8 to 31)
            int firstEmptyUserLayer = -1;
            for (int i = FirstUserLayerIndex; i < TotalLayerCount; i++)
            {
                SerializedProperty layerSP = layersProp.GetArrayElementAtIndex(i);
                if (layerSP != null && string.IsNullOrEmpty(layerSP.stringValue))
                {
                    firstEmptyUserLayer = i;
                    break;
                }
            }

            if (firstEmptyUserLayer == -1)
            {
                return new ErrorResponse("No empty User Layer slots available (8-31 are full).");
            }

            // Assign the name to the found slot
            try
            {
                SerializedProperty targetLayerSP = layersProp.GetArrayElementAtIndex(
                    firstEmptyUserLayer
                );
                targetLayerSP.stringValue = layerName;
                // Apply the changes to the TagManager asset
                tagManager.ApplyModifiedProperties();
                // Save assets to make sure it's written to disk
                AssetDatabase.SaveAssets();
                return new SuccessResponse(
                    $"Layer '{layerName}' added successfully to slot {firstEmptyUserLayer}."
                );
            }
            catch (Exception e)
            {
                return new ErrorResponse($"Failed to add layer '{layerName}': {e.Message}");
            }
        }

        private static object RemoveLayer(string layerName)
        {
            if (string.IsNullOrWhiteSpace(layerName))
                return new ErrorResponse("Layer name cannot be empty or whitespace.");

            // Access the TagManager asset
            SerializedObject tagManager = GetTagManager();
            if (tagManager == null)
                return new ErrorResponse("Could not access TagManager asset.");

            SerializedProperty layersProp = tagManager.FindProperty("layers");
            if (layersProp == null || !layersProp.isArray)
                return new ErrorResponse("Could not find 'layers' property in TagManager.");

            // Find the layer by name (must be user layer)
            int layerIndexToRemove = -1;
            for (int i = FirstUserLayerIndex; i < TotalLayerCount; i++) // Start from user layers
            {
                SerializedProperty layerSP = layersProp.GetArrayElementAtIndex(i);
                // Case-insensitive comparison is safer
                if (
                    layerSP != null
                    && layerName.Equals(layerSP.stringValue, StringComparison.OrdinalIgnoreCase)
                )
                {
                    layerIndexToRemove = i;
                    break;
                }
            }

            if (layerIndexToRemove == -1)
            {
                return new ErrorResponse($"User layer '{layerName}' not found.");
            }

            // Clear the name for that index
            try
            {
                SerializedProperty targetLayerSP = layersProp.GetArrayElementAtIndex(
                    layerIndexToRemove
                );
                targetLayerSP.stringValue = string.Empty; // Set to empty string to remove
                // Apply the changes
                tagManager.ApplyModifiedProperties();
                // Save assets
                AssetDatabase.SaveAssets();
                return new SuccessResponse(
                    $"Layer '{layerName}' (slot {layerIndexToRemove}) removed successfully."
                );
            }
            catch (Exception e)
            {
                return new ErrorResponse($"Failed to remove layer '{layerName}': {e.Message}");
            }
        }

        // --- Helper Methods ---

        /// <summary>
        /// Gets the SerializedObject for the TagManager asset.
        /// </summary>
        private static SerializedObject GetTagManager()
        {
            try
            {
                // Load the TagManager asset from the ProjectSettings folder
                UnityEngine.Object[] tagManagerAssets = AssetDatabase.LoadAllAssetsAtPath(
                    "ProjectSettings/TagManager.asset"
                );
                if (tagManagerAssets == null || tagManagerAssets.Length == 0)
                {
                    McpLog.Error("[ManageEditor] TagManager.asset not found in ProjectSettings.");
                    return null;
                }
                // The first object in the asset file should be the TagManager
                return new SerializedObject(tagManagerAssets[0]);
            }
            catch (Exception e)
            {
                McpLog.Error($"[ManageEditor] Error accessing TagManager.asset: {e.Message}");
                return null;
            }
        }

        // --- Example Implementations for Settings ---
        /*
        private static object SetGameViewResolution(int width, int height) { ... }
        private static object SetQualityLevel(JToken qualityLevelToken) { ... }
        */
    }
}
