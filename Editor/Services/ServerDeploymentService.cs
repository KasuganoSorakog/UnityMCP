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

        // 部署完成标记：拷贝全部完成后才写入（内容为版本号）。中途崩溃的同步
        // 不会留下它，因此"版本号一致但半拷贝"的树能被指纹检出并自愈。
        private const string DeployStampFileName = ".mcp-deploy-stamp";

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
                if (string.Equals(projectVersion, bundledVersion, StringComparison.Ordinal)
                    && IsDeploymentComplete(projectPath))
                {
                    // 已部署且版本一致，直接使用项目内服务端。
                    return true;
                }
            }

            try
            {
                SyncServerSources(bundledPath, projectPath, bundledVersion);
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
        /// Minimal integrity fingerprint: a deploy interrupted mid-copy (crash, disk
        /// full, killed editor) can leave a version-matching but incomplete tree,
        /// which would otherwise never self-heal because the versions compare equal.
        /// The stamp file is written only after the full copy completes, and the
        /// delete phase removes it first — so its presence proves completeness.
        /// </summary>
        private static bool IsDeploymentComplete(string projectPath)
        {
            // 目录指纹与包内 Server~/src 的实际目录（core/models/services/transport/utils）及 tests 一一对齐
            return File.Exists(Path.Combine(projectPath, "uv.lock"))
                && File.Exists(Path.Combine(projectPath, "src", "main.py"))
                && Directory.Exists(Path.Combine(projectPath, "src", "core"))
                && Directory.Exists(Path.Combine(projectPath, "src", "models"))
                && Directory.Exists(Path.Combine(projectPath, "src", "services"))
                && Directory.Exists(Path.Combine(projectPath, "src", "transport"))
                && Directory.Exists(Path.Combine(projectPath, "src", "utils"))
                && Directory.Exists(Path.Combine(projectPath, "tests"))
                && File.Exists(Path.Combine(projectPath, DeployStampFileName));
        }

        /// <summary>
        /// True when the project-local server deployment is complete enough to run.
        /// Fallback for when a re-sync attempt fails (transient IO errors etc.):
        /// an existing good deployment should still be usable instead of hard-failing START.
        /// </summary>
        public static bool HasRunnableProjectServer()
        {
            return IsDeploymentComplete(GetProjectServerPath());
        }

        /// <summary>
        /// Mirrors bundled sources into the project directory, preserving .venv.
        /// Everything else under the target is deleted first so removed files cannot
        /// linger as ghost tools/resources (the server auto-discovers .py files).
        /// </summary>
        private static void SyncServerSources(string sourceRoot, string targetRoot, string version)
        {
            Directory.CreateDirectory(targetRoot);

            // Perforce 等版本管控下检出的文件带只读属性：同步前递归清除，
            // 否则删除/覆盖目标文件会抛 IOException
            ClearReadOnlyAttributes(targetRoot);

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
                // .gitignore 由部署器按项目需要写入（且可能已被项目自定义），不属于同步源，保留
                if (string.Equals(Path.GetFileName(file), ".gitignore", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                File.Delete(file);
            }

            CopyDirectoryFiltered(sourceRoot, targetRoot);
            WriteGitignoreIfMissing(targetRoot);

            // 完成标记最后写入：同步中途崩溃时标记缺失，下次启动指纹检出半拷贝并自愈
            File.WriteAllText(Path.Combine(targetRoot, DeployStampFileName), version + "\n");
        }

        /// <summary>
        /// Recursively clears the ReadOnly attribute on the target tree so synced-out
        /// files (e.g. Perforce checkouts) can be deleted/overwritten without IOException.
        /// </summary>
        private static void ClearReadOnlyAttributes(string rootDir)
        {
            if (!Directory.Exists(rootDir))
            {
                return;
            }

            ClearReadOnlyAttributesRecursive(rootDir);
        }

        private static void ClearReadOnlyAttributesRecursive(string dir)
        {
            foreach (string file in Directory.GetFiles(dir))
            {
                var attributes = File.GetAttributes(file);
                if ((attributes & FileAttributes.ReadOnly) != 0)
                {
                    File.SetAttributes(file, attributes & ~FileAttributes.ReadOnly);
                }
            }

            foreach (string subDir in Directory.GetDirectories(dir))
            {
                // 跳过 .venv 等保留目录：同步不会删除/覆盖它们，
                // 遍历 venv 内上万文件只会徒增编辑器卡顿
                if (IsExcluded(Path.GetFileName(subDir)))
                {
                    continue;
                }

                var attributes = File.GetAttributes(subDir);
                if ((attributes & FileAttributes.ReadOnly) != 0)
                {
                    File.SetAttributes(subDir, attributes & ~FileAttributes.ReadOnly);
                }

                ClearReadOnlyAttributesRecursive(subDir);
            }
        }

        /// <summary>
        /// The deployed server directory contains generated artifacts (.venv, __pycache__,
        /// egg-info, build) that must not be committed: drop a .gitignore on first deploy.
        /// An existing .gitignore is left untouched to respect project-specific rules.
        /// </summary>
        private static void WriteGitignoreIfMissing(string targetRoot)
        {
            string gitignorePath = Path.Combine(targetRoot, ".gitignore");
            if (File.Exists(gitignorePath))
            {
                return;
            }

            try
            {
                File.WriteAllText(gitignorePath, ".venv/\n__pycache__/\n*.egg-info\nbuild/\n" + DeployStampFileName + "\n");
            }
            catch (Exception ex)
            {
                McpLog.Warn($"写入 {gitignorePath} 失败：{ex.Message}");
            }
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
