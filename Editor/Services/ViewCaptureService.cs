using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Models;
using UnityEditor;
using UnityEngine;

namespace MCPForUnity.Editor.Services
{
    internal sealed class ViewCaptureRequest
    {
        public string Target { get; set; }
        public string PresetName { get; set; }
        public List<string> Angles { get; set; }
        public float? Distance { get; set; }
        public int Frames { get; set; }
        public float Interval { get; set; }
        public bool Grid { get; set; }
        public bool IncludeOverlay { get; set; }
        public bool IncludeInactive { get; set; }
        public string FileName { get; set; }
        public int Width { get; set; }
        public int Height { get; set; }
        public float? FieldOfView { get; set; }
    }

    internal static class ViewCaptureService
    {
        private const long MaxGridPixels = 33_554_432;

        private sealed class CapturedImage
        {
            public string FullPath;
            public string ProjectRelativePath;
            public string Angle;
            public int Frame;
            public int Width;
            public int Height;
            public string Target;
            public string BoundsSource;
            public Vector3 CameraPosition;
            public Vector3 CameraRotation;
            public float FieldOfView;
            public float UnscaledTime;
            public int UnityFrame;
            public int OverlayCanvasCount;
        }

        private sealed class ShotDefinition
        {
            public Vector3 Position;
            public Quaternion Rotation;
            public float FieldOfView;
            public float Distance;
            public string Target;
            public string BoundsSource;
            public string Angle;
        }

        public static async Task<object> CaptureAsync(ViewCaptureRequest request)
        {
            if (request == null) return new ErrorResponse("Capture request is required.");
            string projectRoot = GetProjectRoot();
            string batchId = DateTime.UtcNow.ToString("yyyyMMdd-HHmmssfff") + "-" + Guid.NewGuid().ToString("N").Substring(0, 8);
            string outputDirectory = Path.Combine(projectRoot, "Library", "MCPScreenshots", "capture_view", batchId);
            Directory.CreateDirectory(outputDirectory);
            string baseName = Path.GetFileNameWithoutExtension(SanitizeFileName(request.FileName ?? "view"));
            if (string.IsNullOrWhiteSpace(baseName)) baseName = "view";
            var images = new List<CapturedImage>();
            var warnings = new List<string>();
            var referenceCamera = ResolveReferenceCamera();
            int overlayCanvasCount = 0;

            try
            {
                for (int angleIndex = 0; angleIndex < request.Angles.Count; angleIndex++)
                {
                    for (int frameIndex = 0; frameIndex < request.Frames; frameIndex++)
                    {
                        string angle = request.Angles[angleIndex];
                        var shot = ResolveShot(request, angle, referenceCamera, warnings);
                        string fileName = request.Angles.Count == 1 && request.Frames == 1
                            ? baseName + ".png"
                            : $"{baseName}-{SanitizeFileName(shot.Angle)}-f{frameIndex + 1:000}.png";
                        string fullPath = Path.Combine(outputDirectory, fileName);
                        var image = CaptureOne(shot, request, fullPath, warnings);
                        image.ProjectRelativePath = ToProjectRelativePath(projectRoot, fullPath);
                        image.Angle = shot.Angle;
                        image.Frame = frameIndex + 1;
                        image.Target = shot.Target;
                        image.BoundsSource = shot.BoundsSource;
                        images.Add(image);
                        overlayCanvasCount = Math.Max(overlayCanvasCount, image.OverlayCanvasCount);

                        if (angleIndex < request.Angles.Count - 1 || frameIndex < request.Frames - 1)
                            await WaitForNextFrame(request.Interval);
                    }
                }

                string gridFullPath = null;
                if (request.Grid)
                {
                    gridFullPath = Path.Combine(outputDirectory, baseName + "-grid.png");
                    WriteGrid(images, request.Width, request.Height, gridFullPath);
                }

                var payload = images.Select(image => new
                {
                    fullPath = image.FullPath,
                    path = image.ProjectRelativePath,
                    angle = image.Angle,
                    frame = image.Frame,
                    width = image.Width,
                    height = image.Height,
                    target = image.Target,
                    boundsSource = image.BoundsSource,
                    cameraPosition = ToArray(image.CameraPosition),
                    cameraRotationEuler = ToArray(image.CameraRotation),
                    fieldOfView = image.FieldOfView,
                    unscaledTime = image.UnscaledTime,
                    unityFrame = image.UnityFrame,
                    overlayCanvasCount = image.OverlayCanvasCount,
                }).ToArray();
                return new SuccessResponse($"Captured {images.Count} view(s).", new
                {
                    captures = payload,
                    count = images.Count,
                    fullPath = images[0].FullPath,
                    path = images[0].ProjectRelativePath,
                    gridFullPath,
                    gridPath = gridFullPath == null ? null : ToProjectRelativePath(projectRoot, gridFullPath),
                    outputDirectory = outputDirectory.Replace('\\', '/'),
                    overlayCanvasCount,
                    warnings = warnings.Distinct().ToArray(),
                    truncated = false,
                });
            }
            catch (Exception ex)
            {
                return new ErrorResponse($"capture_view failed: {ex.Message}", new
                {
                    captures = images.Select(image => new
                    {
                        fullPath = image.FullPath,
                        path = image.ProjectRelativePath,
                        angle = image.Angle,
                        frame = image.Frame,
                    }).ToArray(),
                    count = images.Count,
                    outputDirectory = outputDirectory.Replace('\\', '/'),
                });
            }
        }

        private static Task WaitForNextFrame(float interval)
        {
            var completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            double started = EditorApplication.timeSinceStartup;
            double deadline = started + Math.Max(10.0, interval * 4.0 + 5.0);
            int editorUpdates = 0;
            EditorApplication.CallbackFunction update = null;
            update = () =>
            {
                editorUpdates++;
                EditorApplication.QueuePlayerLoopUpdate();
                if (!Application.isPlaying) SceneView.RepaintAll();
                double now = EditorApplication.timeSinceStartup;
                if (now < deadline && (editorUpdates < 2 || now - started < interval)) return;
                EditorApplication.update -= update;
                if (now >= deadline)
                    completion.TrySetException(new TimeoutException("Timed out waiting for a rendered frame. The Editor may be paused, compiling, or reloading; no further capture was scheduled."));
                else
                    completion.TrySetResult(true);
            };
            EditorApplication.update += update;
            update();
            return completion.Task;
        }

        private static ShotDefinition ResolveShot(
            ViewCaptureRequest request,
            string angle,
            Camera referenceCamera,
            List<string> warnings)
        {
            if (!string.IsNullOrEmpty(request.PresetName))
            {
                var preset = CameraShotPresetStore.Find(request.PresetName);
                if (preset == null)
                    throw new ArgumentException($"Camera shot preset '{request.PresetName}' was not found. Use action=list_presets.");
                if (!CameraShotPresetStore.TryGetCameraPose(preset, out var position, out var rotation, out var target, out string error))
                    throw new InvalidOperationException(error);
                return new ShotDefinition
                {
                    Position = position,
                    Rotation = rotation,
                    FieldOfView = request.FieldOfView ?? preset.fieldOfView,
                    Distance = preset.followTarget && target != null
                        ? Vector3.Distance(position, target.transform.position)
                        : 0f,
                    Target = preset.followTarget && target != null ? RuntimeTargetResolver.GetHierarchyPath(target.transform) : null,
                    BoundsSource = preset.followTarget ? "preset_follow" : "preset_fixed",
                    Angle = "preset",
                };
            }

            if (!string.IsNullOrEmpty(request.Target))
            {
                if (!RuntimeTargetResolver.TryResolve(request.Target, out var targetObject, out string error, request.IncludeInactive))
                    throw new InvalidOperationException(error);
                string normalizedAngle = NormalizeAngle(angle);
                float fov = request.FieldOfView ?? 60f;
                var framingTransform = targetObject.transform;
                bool hasBounds = TryGetRendererBounds(targetObject, out var bounds);
                string boundsSource = "renderer";
                if (!hasBounds && TryFindSkinRenderer(targetObject.transform, out var skin))
                {
                    framingTransform = skin.transform;
                    hasBounds = TryEstimateBoneBounds(targetObject.transform, skin, out bounds, out bool usedMeshBounds);
                    boundsSource = usedMeshBounds ? "bone_renderer" : "bone_estimate";
                    if (hasBounds && !usedMeshBounds)
                        warnings.Add($"'{request.Target}' uses a bone-local bounds estimate; specify distance for exact framing.");
                }

                Vector3 focus;
                float radius;
                if (hasBounds)
                {
                    focus = bounds.center;
                    radius = Mathf.Max(0.01f, bounds.extents.magnitude);
                }
                else if (request.Distance.HasValue)
                {
                    focus = targetObject.transform.position;
                    radius = 1f;
                    boundsSource = "transform_fallback";
                    warnings.Add($"'{request.Target}' has no usable Renderer or matching skinned bones; using its Transform position with explicit distance.");
                }
                else
                {
                    throw new InvalidOperationException($"'{request.Target}' has no Renderer bounds or matching SkinnedMeshRenderer bones. Specify distance or use a preset.");
                }

                if (!IsFinite(focus) || !IsFinite(bounds.extents) || !IsFinite(targetObject.transform.position))
                    throw new InvalidOperationException($"Target '{request.Target}' produced non-finite bounds or position.");
                float aspect = (float)request.Width / request.Height;
                float verticalTangent = Mathf.Tan(fov * 0.5f * Mathf.Deg2Rad);
                float autoDistance = Mathf.Max(radius / verticalTangent, radius / (verticalTangent * aspect)) * 1.2f;
                if (normalizedAngle == "closeup") autoDistance *= 0.65f;
                float distance = request.Distance ?? autoDistance;
                Vector3 direction = GetShotDirection(normalizedAngle, framingTransform);
                Vector3 position = focus + direction * distance;
                Vector3 up = normalizedAngle == "top" ? framingTransform.forward : Vector3.up;
                up = SafeDirection(up, Vector3.up);
                if (Mathf.Abs(Vector3.Dot(direction, up)) > 0.98f) up = Vector3.forward;
                if (Mathf.Abs(Vector3.Dot(direction, up)) > 0.98f) up = Vector3.right;
                return new ShotDefinition
                {
                    Position = position,
                    Rotation = Quaternion.LookRotation(focus - position, up),
                    FieldOfView = fov,
                    Distance = distance,
                    Target = RuntimeTargetResolver.GetHierarchyPath(targetObject.transform),
                    BoundsSource = boundsSource,
                    Angle = normalizedAngle,
                };
            }

            if (referenceCamera == null)
                throw new InvalidOperationException("No target or preset was supplied, and no Game or Scene view camera is available.");
            return new ShotDefinition
            {
                Position = referenceCamera.transform.position,
                Rotation = referenceCamera.transform.rotation,
                FieldOfView = request.FieldOfView ?? referenceCamera.fieldOfView,
                BoundsSource = "source_camera",
                Angle = "source",
            };
        }

        private static CapturedImage CaptureOne(
            ShotDefinition shot,
            ViewCaptureRequest request,
            string fullPath,
            List<string> warnings)
        {
            var previousActive = RenderTexture.active;
            var renderTexture = RenderTexture.GetTemporary(request.Width, request.Height, 24, RenderTextureFormat.ARGB32);
            Texture2D texture = null;
            GameObject cameraObject = null;
            OverlayCanvasScope overlayScope = null;
            var referenceCamera = ResolveReferenceCamera();
            try
            {
                cameraObject = new GameObject("MCP_CaptureViewCamera")
                {
                    hideFlags = HideFlags.HideAndDontSave,
                };
                // Internal capture-only camera: it is destroyed in finally after this shot and is never added to a business object.
                var camera = cameraObject.AddComponent<Camera>();
                camera.enabled = false;
                camera.stereoTargetEye = StereoTargetEyeMask.None;
                camera.cameraType = CameraType.Game;
                camera.allowHDR = referenceCamera == null || referenceCamera.allowHDR;
                camera.allowMSAA = true;
                camera.useOcclusionCulling = false;
                camera.clearFlags = referenceCamera != null ? referenceCamera.clearFlags : CameraClearFlags.Skybox;
                camera.backgroundColor = referenceCamera != null ? referenceCamera.backgroundColor : new Color(0.08f, 0.08f, 0.1f, 1f);
                camera.cullingMask = referenceCamera != null ? referenceCamera.cullingMask : ~0;
                float referenceNearClip = referenceCamera != null ? referenceCamera.nearClipPlane : 0.03f;
                camera.nearClipPlane = shot.Distance > 0f
                    ? Mathf.Clamp(Mathf.Min(referenceNearClip, shot.Distance * 0.05f), 0.001f, 0.3f)
                    : referenceNearClip;
                camera.farClipPlane = referenceCamera != null ? referenceCamera.farClipPlane : 1000f;
                camera.transform.SetPositionAndRotation(shot.Position, shot.Rotation);
                camera.fieldOfView = shot.FieldOfView;
                camera.targetTexture = renderTexture;
                camera.aspect = (float)request.Width / request.Height;

                if (request.IncludeOverlay)
                {
                    overlayScope = new OverlayCanvasScope(camera, warnings);
                    camera.cullingMask = overlayScope.CameraCullingMask;
                }
                Canvas.ForceUpdateCanvases();
                camera.Render();

                RenderTexture.active = renderTexture;
                texture = new Texture2D(request.Width, request.Height, TextureFormat.RGBA32, false);
                texture.ReadPixels(new Rect(0, 0, request.Width, request.Height), 0, 0);
                texture.Apply();
                byte[] png = ImageConversion.EncodeToPNG(texture);
                if (png == null || png.Length == 0) throw new InvalidOperationException("Unity returned an empty PNG payload.");
                File.WriteAllBytes(fullPath, png);
                if (!File.Exists(fullPath) || new FileInfo(fullPath).Length == 0)
                    throw new IOException("PNG capture did not produce a readable file.");

                return new CapturedImage
                {
                    FullPath = fullPath.Replace('\\', '/'),
                    Width = request.Width,
                    Height = request.Height,
                    CameraPosition = shot.Position,
                    CameraRotation = shot.Rotation.eulerAngles,
                    FieldOfView = shot.FieldOfView,
                    UnscaledTime = Time.unscaledTime,
                    UnityFrame = Time.frameCount,
                    OverlayCanvasCount = overlayScope?.Count ?? 0,
                };
            }
            finally
            {
                overlayScope?.Dispose();
                RenderTexture.active = previousActive;
                if (texture != null) UnityEngine.Object.DestroyImmediate(texture);
                if (cameraObject != null) UnityEngine.Object.DestroyImmediate(cameraObject);
                RenderTexture.ReleaseTemporary(renderTexture);
            }
        }

        private static Camera ResolveReferenceCamera()
        {
            var main = Camera.main;
            if (main != null) return main;
            var gameCamera = UnityEngine.Object.FindObjectsOfType<Camera>()
                .OrderByDescending(camera => camera.depth)
                .FirstOrDefault();
            if (gameCamera != null) return gameCamera;
            var sceneView = SceneView.lastActiveSceneView;
            return sceneView != null ? sceneView.camera : null;
        }

        private static bool TryGetRendererBounds(GameObject target, out Bounds bounds)
        {
            var renderers = target.GetComponentsInChildren<Renderer>(false)
                .Where(renderer => renderer != null && renderer.enabled && renderer.gameObject.activeInHierarchy)
                .ToList();
            if (renderers.Count == 0)
            {
                bounds = default;
                return false;
            }
            bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Count; i++) bounds.Encapsulate(renderers[i].bounds);
            return true;
        }

        private static bool TryFindSkinRenderer(Transform target, out SkinnedMeshRenderer skin)
        {
            skin = RuntimeTargetResolver.EnumerateSceneObjects(includeInactive: false)
                .SelectMany(go => go.GetComponents<SkinnedMeshRenderer>())
                .Where(renderer => renderer != null && renderer.enabled && renderer.gameObject.activeInHierarchy &&
                    renderer.bones != null && renderer.bones.Contains(target))
                .OrderBy(renderer => renderer.bones.Count(bone => bone == target || bone.IsChildOf(target)))
                .ThenBy(renderer => renderer.bones.Length)
                .FirstOrDefault();
            return skin != null;
        }

        private static bool TryEstimateBoneBounds(Transform target, SkinnedMeshRenderer skin, out Bounds bounds, out bool usedMeshBounds)
        {
            var relatedBones = skin.bones
                .Where(bone => bone != null && (bone == target || bone.IsChildOf(target)))
                .ToList();
            usedMeshBounds = false;
            if (relatedBones.Count == 0)
            {
                bounds = default;
                return false;
            }

            float scale = Mathf.Max(0.01f, Mathf.Abs(target.lossyScale.x));
            float padding = Mathf.Max(0.025f * scale, skin.bounds.size.magnitude * 0.01f);
            if (relatedBones.Count <= 8 && skin.bounds.size.sqrMagnitude > 0.000001f)
            {
                bounds = skin.bounds;
                bounds.Expand(padding * 2f);
                usedMeshBounds = true;
                return true;
            }

            bounds = new Bounds(relatedBones[0].position, Vector3.zero);
            foreach (var bone in relatedBones.Skip(1)) bounds.Encapsulate(bone.position);
            bounds.Expand(padding * 2f);
            return true;
        }

        private static string NormalizeAngle(string angle)
        {
            string normalized = (angle ?? "front").Trim().ToLowerInvariant()
                .Replace("°", string.Empty)
                .Replace("_", string.Empty)
                .Replace("-", string.Empty)
                .Replace(" ", string.Empty);
            return normalized switch
            {
                "front" => "front",
                "side" or "right" => "side",
                "back" => "back",
                "top" => "top",
                "45" or "fortyfive" => "45",
                "closeup" or "close" => "closeup",
                _ => throw new ArgumentException($"Unknown shot angle '{angle}'. Valid values: front, side, back, top, 45, closeup."),
            };
        }

        private static Vector3 GetShotDirection(string angle, Transform target)
        {
            Vector3 forward = SafeDirection(target.forward, Vector3.forward);
            Vector3 right = SafeDirection(target.right, Vector3.right);
            switch (angle)
            {
                case "front": return forward;
                case "side": return right;
                case "back": return -forward;
                case "top": return Vector3.up;
                case "closeup": return forward;
                case "45":
                    Vector3 horizontal = SafeDirection(forward + right, forward);
                    return (horizontal * Mathf.Cos(30f * Mathf.Deg2Rad) + Vector3.up * Mathf.Sin(30f * Mathf.Deg2Rad)).normalized;
                default: throw new ArgumentOutOfRangeException(nameof(angle));
            }
        }

        private static Vector3 SafeDirection(Vector3 value, Vector3 fallback)
        {
            return IsFinite(value) && value.sqrMagnitude > 0.00000001f ? value.normalized : fallback;
        }

        private static void WriteGrid(List<CapturedImage> images, int cellWidth, int cellHeight, string fullPath)
        {
            int columns = Mathf.CeilToInt(Mathf.Sqrt(images.Count));
            int rows = Mathf.CeilToInt(images.Count / (float)columns);
            long pixelCount = (long)columns * rows * cellWidth * cellHeight;
            if (columns * cellWidth > SystemInfo.maxTextureSize || rows * cellHeight > SystemInfo.maxTextureSize || pixelCount > MaxGridPixels)
                throw new InvalidOperationException("Grid exceeds texture or memory limits. Reduce resolution/frames or disable grid.");

            int width = columns * cellWidth;
            int height = rows * cellHeight;
            var previousActive = RenderTexture.active;
            var renderTexture = RenderTexture.GetTemporary(width, height, 0, RenderTextureFormat.ARGB32);
            Texture2D gridTexture = null;
            bool matrixPushed = false;
            try
            {
                RenderTexture.active = renderTexture;
                GL.PushMatrix();
                matrixPushed = true;
                GL.LoadPixelMatrix(0, width, 0, height);
                GL.Clear(true, true, Color.clear);
                for (int index = 0; index < images.Count; index++)
                {
                    Texture2D image = new Texture2D(2, 2, TextureFormat.RGBA32, false);
                    try
                    {
                        if (!ImageConversion.LoadImage(image, File.ReadAllBytes(images[index].FullPath), false))
                            throw new InvalidOperationException($"Could not load captured PNG '{images[index].FullPath}' for grid composition.");
                        int row = index / columns;
                        int column = index % columns;
                        float y = (rows - 1 - row) * cellHeight;
                        Graphics.DrawTexture(new Rect(column * cellWidth, y, cellWidth, cellHeight), image, new Rect(0f, 1f, 1f, -1f), 0, 0, 0, 0);
                    }
                    finally
                    {
                        UnityEngine.Object.DestroyImmediate(image);
                    }
                }

                gridTexture = new Texture2D(width, height, TextureFormat.RGBA32, false);
                gridTexture.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                gridTexture.Apply();
                File.WriteAllBytes(fullPath, ImageConversion.EncodeToPNG(gridTexture));
            }
            finally
            {
                if (matrixPushed) GL.PopMatrix();
                RenderTexture.active = previousActive;
                if (gridTexture != null) UnityEngine.Object.DestroyImmediate(gridTexture);
                RenderTexture.ReleaseTemporary(renderTexture);
            }
        }

        private static string GetProjectRoot()
        {
            return Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
        }

        private static string ToProjectRelativePath(string projectRoot, string fullPath)
        {
            string normalizedRoot = projectRoot.Replace('\\', '/').TrimEnd('/') + "/";
            string normalizedPath = fullPath.Replace('\\', '/');
            return normalizedPath.StartsWith(normalizedRoot, StringComparison.OrdinalIgnoreCase)
                ? normalizedPath.Substring(normalizedRoot.Length)
                : normalizedPath;
        }

        private static string SanitizeFileName(string value)
        {
            var invalid = Path.GetInvalidFileNameChars().Concat(new[] { '/', '\\' }).ToHashSet();
            string cleaned = new string((value ?? "view").Select(character => invalid.Contains(character) ? '_' : character).ToArray()).Trim();
            if (string.IsNullOrWhiteSpace(cleaned)) cleaned = "view";
            return cleaned.Length <= 80 ? cleaned : cleaned.Substring(0, 80);
        }

        private static bool IsFinite(Vector3 value)
        {
            return !float.IsNaN(value.x) && !float.IsNaN(value.y) && !float.IsNaN(value.z) &&
                   !float.IsInfinity(value.x) && !float.IsInfinity(value.y) && !float.IsInfinity(value.z);
        }

        private static float[] ToArray(Vector3 value) => new[] { value.x, value.y, value.z };

        private sealed class OverlayCanvasScope : IDisposable
        {
            private sealed class CanvasState
            {
                public Canvas Canvas;
                public RenderMode RenderMode;
                public Camera WorldCamera;
                public float PlaneDistance;
            }

            private readonly List<CanvasState> _states = new();
            private bool _disposed;

            public int Count => _states.Count;
            public int CameraCullingMask { get; }

            public OverlayCanvasScope(Camera camera, List<string> warnings)
            {
                CameraCullingMask = camera.cullingMask;
                var canvases = UnityEngine.Resources.FindObjectsOfTypeAll<Canvas>()
                    .Where(canvas => canvas != null && canvas.gameObject.scene.IsValid() && canvas.gameObject.scene.isLoaded)
                    .Where(canvas => canvas.isActiveAndEnabled && canvas.rootCanvas == canvas && canvas.renderMode == RenderMode.ScreenSpaceOverlay)
                    .ToList();
                foreach (var canvas in canvases)
                {
                    _states.Add(new CanvasState
                    {
                        Canvas = canvas,
                        RenderMode = canvas.renderMode,
                        WorldCamera = canvas.worldCamera,
                        PlaneDistance = canvas.planeDistance,
                    });
                    foreach (var transform in canvas.GetComponentsInChildren<Transform>(true))
                        CameraCullingMask |= 1 << transform.gameObject.layer;
                }

                foreach (var state in _states)
                {
                    state.Canvas.renderMode = RenderMode.ScreenSpaceCamera;
                    state.Canvas.worldCamera = camera;
                    state.Canvas.planeDistance = Mathf.Max(camera.nearClipPlane + 0.01f, 0.02f);
                }
                if (_states.Count > 0)
                {
                    Canvas.ForceUpdateCanvases();
                    warnings.Add($"Captured {_states.Count} Screen Space - Overlay canvas(es) through the shot camera; UI/world depth interactions can differ from Game View.");
                }
            }

            public void Dispose()
            {
                if (_disposed) return;
                _disposed = true;
                foreach (var state in _states)
                {
                    if (state.Canvas == null) continue;
                    state.Canvas.worldCamera = state.WorldCamera;
                    state.Canvas.planeDistance = state.PlaneDistance;
                    state.Canvas.renderMode = state.RenderMode;
                }
                if (_states.Count > 0) Canvas.ForceUpdateCanvases();
            }
        }
    }
}
