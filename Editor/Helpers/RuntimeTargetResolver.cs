using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

namespace MCPForUnity.Editor.Helpers
{
    internal static class RuntimeTargetResolver
    {
        private static readonly Dictionary<string, HumanBodyBones> HumanBoneAliases =
            new(StringComparer.OrdinalIgnoreCase)
            {
                ["head"] = HumanBodyBones.Head,
                ["neck"] = HumanBodyBones.Neck,
                ["hips"] = HumanBodyBones.Hips,
                ["spine"] = HumanBodyBones.Spine,
                ["chest"] = HumanBodyBones.Chest,
                ["l_wrist"] = HumanBodyBones.LeftHand,
                ["left_wrist"] = HumanBodyBones.LeftHand,
                ["l_hand"] = HumanBodyBones.LeftHand,
                ["left_hand"] = HumanBodyBones.LeftHand,
                ["r_wrist"] = HumanBodyBones.RightHand,
                ["right_wrist"] = HumanBodyBones.RightHand,
                ["r_hand"] = HumanBodyBones.RightHand,
                ["right_hand"] = HumanBodyBones.RightHand,
                ["l_foot"] = HumanBodyBones.LeftFoot,
                ["left_foot"] = HumanBodyBones.LeftFoot,
                ["r_foot"] = HumanBodyBones.RightFoot,
                ["right_foot"] = HumanBodyBones.RightFoot,
            };

        public static bool TryResolve(
            string target,
            out GameObject gameObject,
            out string error,
            bool includeInactive = true)
        {
            gameObject = null;
            error = null;
            if (string.IsNullOrWhiteSpace(target))
            {
                error = "Target is required. Provide a hierarchy path, unique GameObject/bone name, or instance ID.";
                return false;
            }

            target = target.Trim();
            var all = EnumerateSceneObjects(includeInactive).ToList();
            List<GameObject> matches;
            if (int.TryParse(target, out int instanceId))
            {
                matches = all.Where(go => go.GetInstanceID() == instanceId).ToList();
            }
            else
            {
                matches = all
                    .Where(go => string.Equals(GetHierarchyPath(go.transform), target, StringComparison.OrdinalIgnoreCase))
                    .ToList();
                if (matches.Count == 0)
                {
                    matches = all
                        .Where(go => string.Equals(go.name, target, StringComparison.OrdinalIgnoreCase))
                        .ToList();
                }
                if (matches.Count == 0 && HumanBoneAliases.TryGetValue(target, out var humanBone))
                {
                    matches = all
                        .Select(go => go.GetComponent<Animator>())
                        .Where(animator => animator != null && animator.isHuman)
                        .Select(animator =>
                        {
                            try { return animator.GetBoneTransform(humanBone); }
                            catch { return null; }
                        })
                        .Where(transform => transform != null)
                        .Select(transform => transform.gameObject)
                        .Distinct()
                        .ToList();
                }
                if (matches.Count == 0)
                {
                    string normalized = NormalizeName(target);
                    matches = all
                        .Where(go => NormalizeName(go.name) == normalized)
                        .ToList();
                }
            }

            if (matches.Count == 1)
            {
                gameObject = matches[0];
                return true;
            }

            if (matches.Count == 0)
            {
                error = $"Target '{target}' was not found in loaded scenes.";
                return false;
            }

            string candidates = string.Join(", ", matches
                .Take(10)
                .Select(go => $"'{GetHierarchyPath(go.transform)}' ({go.GetInstanceID()})"));
            error = $"Target '{target}' is ambiguous. Use a full hierarchy path or instance ID. Candidates: {candidates}";
            return false;
        }

        public static IEnumerable<GameObject> EnumerateSceneObjects(bool includeInactive = true)
        {
            return UnityEngine.Resources.FindObjectsOfTypeAll<Transform>()
                .Where(transform => transform != null)
                .Select(transform => transform.gameObject)
                .Where(go => go.scene.IsValid() && go.scene.isLoaded)
                .Where(go => includeInactive || go.activeInHierarchy)
                .Distinct();
        }

        public static string GetHierarchyPath(Transform transform)
        {
            if (transform == null) return null;
            var names = new Stack<string>();
            var current = transform;
            while (current != null)
            {
                names.Push(current.name);
                current = current.parent;
            }
            return string.Join("/", names);
        }

        private static string NormalizeName(string value)
        {
            return new string((value ?? string.Empty)
                .Where(ch => ch != '_' && ch != '-' && !char.IsWhiteSpace(ch))
                .Select(char.ToLowerInvariant)
                .ToArray());
        }
    }
}
