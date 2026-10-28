using System;
using System.IO;
using System.Linq;
using System.Reflection;
using UnityEngine;

namespace MCPForUnity.Runtime.Helpers
//The reason for having another Runtime Utilities in additional to Editor Utilities is to avoid Editor-only dependencies in this runtime code.
{
    public readonly struct ScreenshotCaptureResult
    {
        public ScreenshotCaptureResult(string fullPath, string projectRelativePath, int superSize)
        {
            FullPath = fullPath;
            ProjectRelativePath = projectRelativePath;
            SuperSize = superSize;
        }

        public string FullPath { get; }
        public string ProjectRelativePath { get; }
        public string AssetsRelativePath => ProjectRelativePath;
        public int SuperSize { get; }
    }

    public static class ScreenshotUtility
    {
        public static ScreenshotCaptureResult CaptureToAssetsFolder(string fileName = null, int superSize = 1, bool ensureUniqueFileName = true)
        {
            return CaptureFromCamera(Camera.main, fileName, superSize, ensureUniqueFileName);
        }

        public static ScreenshotCaptureResult CaptureFromCameraToAssetsFolder(Camera camera, string fileName = null, int superSize = 1, bool ensureUniqueFileName = true)
        {
            return CaptureFromCamera(camera, fileName, superSize, ensureUniqueFileName);
        }

        public static ScreenshotCaptureResult CaptureFromCamera(Camera camera, string fileName = null, int superSize = 1, bool ensureUniqueFileName = true)
        {
            if (camera == null)
            {
                throw new InvalidOperationException("No camera is available. Use capture_view with a target or an explicit camera preset.");
            }

            int size = Mathf.Max(1, superSize);
            string fullPath = Path.Combine(
                GetProjectRootPath(), "Library", "MCPScreenshots", BuildFileName(fileName));
            if (ensureUniqueFileName)
            {
                fullPath = EnsureUnique(fullPath);
            }
            string normalizedFullPath = Path.GetFullPath(fullPath).Replace('\\', '/');
            Directory.CreateDirectory(Path.GetDirectoryName(normalizedFullPath));

            int width = Mathf.Max(1, camera.pixelWidth > 0 ? camera.pixelWidth : Screen.width) * size;
            int height = Mathf.Max(1, camera.pixelHeight > 0 ? camera.pixelHeight : Screen.height) * size;
            RenderTexture previousTarget = camera.targetTexture;
            RenderTexture previousActive = RenderTexture.active;
            var renderTexture = RenderTexture.GetTemporary(width, height, 24, RenderTextureFormat.ARGB32);
            Texture2D texture = null;
            try
            {
                camera.targetTexture = renderTexture;
                camera.Render();
                RenderTexture.active = renderTexture;
                texture = new Texture2D(width, height, TextureFormat.RGBA32, false);
                texture.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                texture.Apply();
                File.WriteAllBytes(normalizedFullPath, EncodeTextureToPng(texture));
            }
            finally
            {
                camera.targetTexture = previousTarget;
                RenderTexture.active = previousActive;
                RenderTexture.ReleaseTemporary(renderTexture);
                if (texture != null) UnityEngine.Object.DestroyImmediate(texture);
            }

            string projectRoot = GetProjectRootPath();
            string relativePath = normalizedFullPath.StartsWith(projectRoot, StringComparison.OrdinalIgnoreCase)
                ? normalizedFullPath.Substring(projectRoot.Length)
                : normalizedFullPath;
            return new ScreenshotCaptureResult(normalizedFullPath, relativePath, size);
        }

        private static byte[] EncodeTextureToPng(Texture2D texture)
        {
            Type imageConversionType = Type.GetType("UnityEngine.ImageConversion, UnityEngine.ImageConversionModule");
            MethodInfo encodeMethod = imageConversionType?.GetMethod(
                "EncodeToPNG",
                BindingFlags.Public | BindingFlags.Static,
                null,
                new[] { typeof(Texture2D) },
                null);

            if (encodeMethod == null)
            {
                throw new InvalidOperationException("ImageConversionModule is unavailable. Enable com.unity.modules.imageconversion for screenshot encoding.");
            }

            return (byte[])encodeMethod.Invoke(null, new object[] { texture });
        }

        private static string BuildFileName(string fileName)
        {
            string name = string.IsNullOrWhiteSpace(fileName)
                ? $"screenshot-{DateTime.Now:yyyyMMdd-HHmmss}"
                : fileName.Trim();

            name = SanitizeFileName(name);

            if (!name.EndsWith(".png", StringComparison.OrdinalIgnoreCase) &&
                !name.EndsWith(".jpg", StringComparison.OrdinalIgnoreCase) &&
                !name.EndsWith(".jpeg", StringComparison.OrdinalIgnoreCase))
            {
                name += ".png";
            }

            return name;
        }

        private static string SanitizeFileName(string fileName)
        {
            var invalidChars = Path.GetInvalidFileNameChars();
            string cleaned = new string(fileName.Select(ch => invalidChars.Contains(ch) ? '_' : ch).ToArray());

            return string.IsNullOrWhiteSpace(cleaned) ? "screenshot" : cleaned;
        }

        private static string EnsureUnique(string path)
        {
            if (!File.Exists(path))
            {
                return path;
            }

            string directory = Path.GetDirectoryName(path) ?? string.Empty;
            string baseName = Path.GetFileNameWithoutExtension(path);
            string extension = Path.GetExtension(path);
            int counter = 1;

            string candidate;
            do
            {
                candidate = Path.Combine(directory, $"{baseName}-{counter}{extension}");
                counter++;
            } while (File.Exists(candidate));

            return candidate;
        }

        private static string GetProjectRootPath()
        {
            string root = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
            root = root.Replace('\\', '/');
            if (!root.EndsWith("/", StringComparison.Ordinal))
            {
                root += "/";
            }
            return root;
        }
    }
}
