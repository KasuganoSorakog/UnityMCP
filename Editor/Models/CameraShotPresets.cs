using System;
using System.Collections.Generic;
using UnityEngine;

namespace MCPForUnity.Editor.Models
{
    [Serializable]
    public sealed class CameraShotPreset
    {
        public string name;
        public Vector3 position;
        public Quaternion rotation = Quaternion.identity;
        public float fieldOfView = 60f;
        public bool followTarget;
        public string targetPath;
        public string targetScene;
        public string targetGlobalObjectId;
        public int targetInstanceId;
        public Vector3 targetLocalPosition;
        public Quaternion targetLocalRotation = Quaternion.identity;
    }

    public sealed class CameraShotPresets : ScriptableObject
    {
        public List<CameraShotPreset> shots = new();
    }
}
