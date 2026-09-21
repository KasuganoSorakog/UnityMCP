using System;
using System.IO;
using System.Text.RegularExpressions;
using MCPForUnity.Editor.Helpers;
using UnityEngine;
using PackageInfo = UnityEditor.PackageManager.PackageInfo;

namespace MCPForUnity.Editor.Services
{
    /// <summary>
    /// Ensures the project-local MCP server sources (Tools/MCPForUnityServer) exist and
    /// match the server version bundled inside this package (Server~ folder).
    /// First launch deploys automatically; package upgrades re-sync sources while
    /// preserving the project's .venv so uv can incrementally refresh dependencies.
    /// </summary>
    public static class ServerDeploymentService
    {
        private const string PackageServerFolderName = "Server~";

        // Directory names never copied from the package and never deleted from the project
        // (.venv is preserved on resync so dependencies do not need a full reinstall).
        private static readonly string[] ExcludedDirectoryNames = { ".venv", "__pycache__", ".pytest_cache", "build" };

        public static string GetProjectServerPath()
        {
            string projectRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
            return Path.Combine(projectRoot, "Tools", "MCPForUnityServer");
        }

        /// <summary>
        /// Locates the bundled Server~ folder via the package manager resolved path,
        /// which works for both embedded (Packages/...) and git-installed (PackageCache) packages.
        /// </summary>
        public static string GetBundledServerPath()
        {
            var packageInfo = PackageInfo.FindForAssembly(typeof(ServerDeploymentService).Assembly);
            if (packageInfo == null || string.IsNullOrEmpty(packageInfo.resolvedPath))
            {
                return null;
            }

            string candidate = Path.Combine(packageInfo.resolvedPath, PackageServerFolderName);
            return Directory.Exists(candidate) ? candidate : null;
        }

        /// <summary>
        /// Makes sure Tools/MCPForUnityServer exists and is in sync with the bundled Server~ sources.
        /// Returns true when the project-local server sources are ready to run.
        /// </summary>
        public static bool EnsureProjectLocalServer(out string message)
        {
            message = null;

            string bundledPath = GetBundledServerPath();
            if (string.IsNullOrEmpty(bundledPath))
            {
                message = "包内未找到 Server~ 服务端目录，无法自动部署。";
                return false;
            }

            string bundledVersion = ReadServerVersion(Path.Combine(bundledPath, "pyproject.toml"));
            if (string.IsNullOrEmpty(bundledVersion))
            {
                message = "包内 Server~/pyproject.toml 缺少 version 字段，无法自动部署。";
                return false;
            }

            string projectPath = GetProjectServerPath();
            string projectPyproject = Path.Combine(projectPath, "pyproject.toml");

            if (File.Exists(projectPyproject))
            {
                string projectVersion = ReadServerVersion(projectPyproject);
                if (string.Equals(projectVersion, bundledVersion, StringComparison.Ordinal))
                {
                    // 已部署且版本一致，直接使用项目内服务端。
                    return true;
                }
            }

            try
            {
                SyncServerSources(bundledPath, projectPath);
                message = $"已自动部署/同步 MCP Server 源码到 {projectPath}（版本 {bundledVersion}）。";
                McpLog.Info(message);
                return true;
            }
            catch (Exception ex)
            {
                message = $"自动部署 MCP Server 失败: {ex.Message}";
                McpLog.Error(message);
                return false;
            }
        }

        /// <summary>
        /// Mirrors bundled sources into the project directory, preserving .venv.
        /// Everything else under the target is deleted first so removed files cannot
        /// linger as ghost tools/resources (the server auto-discovers .py files).
        /// </summary>
        private static void SyncServerSources(string sourceRoot, string targetRoot)
        {
            Directory.CreateDirectory(targetRoot);

            foreach (string dir in Directory.GetDirectories(targetRoot))
            {
                if (IsExcluded(Path.GetFileName(dir)))
                {
                    continue;
                }

                Directory.Delete(dir, recursive: true);
            }

            foreach (string file in Directory.GetFiles(targetRoot))
            {
                File.Delete(file);
            }

            CopyDirectoryFiltered(sourceRoot, targetRoot);
        }

        private static void CopyDirectoryFiltered(string sourceDir, string targetDir)
        {
            Directory.CreateDirectory(targetDir);

            foreach (string file in Directory.GetFiles(sourceDir))
            {
                if (file.EndsWith(".pyc", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                File.Copy(file, Path.Combine(targetDir, Path.GetFileName(file)), overwrite: true);
            }

            foreach (string dir in Directory.GetDirectories(sourceDir))
            {
                string name = Path.GetFileName(dir);
                if (IsExcluded(name) || name.EndsWith(".egg-info", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                CopyDirectoryFiltered(dir, Path.Combine(targetDir, name));
            }
        }

        private static bool IsExcluded(string directoryName)
        {
            foreach (string excluded in ExcludedDirectoryNames)
            {
                if (string.Equals(directoryName, excluded, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static string ReadServerVersion(string pyprojectPath)
        {
            try
            {
                if (!File.Exists(pyprojectPath))
                {
                    return null;
                }

                string text = File.ReadAllText(pyprojectPath);
                var match = Regex.Match(text, "(?m)^\\s*version\\s*=\\s*\"(?<v>[^\"]+)\"");
                return match.Success ? match.Groups["v"].Value : null;
            }
            catch
            {
                return null;
            }
        }
    }
}
