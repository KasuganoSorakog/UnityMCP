using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using MCPForUnity.Editor.Helpers;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.Animations;
using UnityEngine;
using UnityEngine.LowLevel;

namespace MCPForUnity.Editor.Services
{
    internal sealed class RuntimeSampleRequest
    {
        public string SampleId;
        public string Target;
        public bool IncludeInactive;
        public float Duration;
        public bool IncludeSamples;
        public int MaxSamples;
        public int MaxSpikes;
        public List<string> Properties;
        public Dictionary<string, float> Thresholds;
    }

    [InitializeOnLoad]
    internal static class RuntimeSamplingService
    {
        private const int MaxFrames = 20000;
        private const int MaxAnimators = 4;
        private const int MaxEvents = 128;
        private static SampleSession _active;
        private static bool _playerLoopHooked;

        static RuntimeSamplingService()
        {
            EditorApplication.playModeStateChanged += OnPlayModeChanged;
            AssemblyReloadEvents.beforeAssemblyReload += CompleteForDomainReload;
        }

        internal static object StartSample(RuntimeSampleRequest request)
        {
            if (_active != null)
                return new ErrorResponse("A runtime sample is already active. Wait for it to finish or cancel it before starting another.", new { sampleId = _active.Id });
            if (!Application.isPlaying)
                return new ErrorResponse("sample_runtime requires an active Play session. Enter Play, wait for its first frames, then sample; this tool does not enter Play implicitly.");
            if (!RuntimeTargetResolver.TryResolve(request.Target, out var target, out string error, request.IncludeInactive))
                return new ErrorResponse(error);

            var session = new SampleSession(request, target);
            session.Initialize();
            _active = session;
            try
            {
                session.Start();
                return new PendingResponse("Runtime sample started; poll action=result with this sample_id.", 0.5, session.GetProgress());
            }
            catch
            {
                if (ReferenceEquals(_active, session)) _active = null;
                RemovePlayerLoopHook();
                throw;
            }
        }

        internal static object GetResult(string sampleId)
        {
            ValidateSampleId(sampleId);
            if (_active != null && _active.Id == sampleId)
                return new PendingResponse("Runtime sample is still running.", 0.5, _active.GetProgress());
            string path = ResultPath(sampleId);
            if (!File.Exists(path)) return new ErrorResponse($"No saved runtime sample '{sampleId}' was found.");
            try
            {
                var response = JObject.Parse(File.ReadAllText(path));
                return new SuccessResponse(response.Value<string>("message"), response["data"]);
            }
            catch (Exception ex)
            {
                return new ErrorResponse($"Saved runtime sample '{sampleId}' could not be read: {ex.Message}");
            }
        }

        internal static object Cancel(string sampleId)
        {
            ValidateSampleId(sampleId);
            if (_active == null || _active.Id != sampleId)
                return new ErrorResponse($"Runtime sample '{sampleId}' is not active.");
            _active.Complete("cancelled");
            return new SuccessResponse("Runtime sample cancelled; partial results were retained.", new { sampleId });
        }

        internal static void CompleteForDomainReload()
        {
            _active?.Complete("domain_reload");
            _playerLoopHooked = false;
        }

        private static void OnPlayModeChanged(PlayModeStateChange state)
        {
            if (state == PlayModeStateChange.ExitingPlayMode)
                _active?.Complete("play_mode_exit");
        }

        private static void Tick()
        {
            _active?.SampleFrame();
        }

        private static void AddPlayerLoopHook()
        {
            if (_playerLoopHooked) return;
            var loop = PlayerLoop.GetCurrentPlayerLoop();
            int index = Array.FindIndex(loop.subSystemList, system => system.type == typeof(UnityEngine.PlayerLoop.PostLateUpdate));
            if (index < 0) throw new InvalidOperationException("Unity's PostLateUpdate PlayerLoop phase was not found.");
            var phase = loop.subSystemList[index];
            var systems = phase.subSystemList ?? Array.Empty<PlayerLoopSystem>();
            if (systems.Any(system => system.updateDelegate == (PlayerLoopSystem.UpdateFunction)Tick)) return;
            Array.Resize(ref systems, systems.Length + 1);
            systems[^1] = new PlayerLoopSystem { type = typeof(RuntimeSamplingService), updateDelegate = Tick };
            phase.subSystemList = systems;
            loop.subSystemList[index] = phase;
            PlayerLoop.SetPlayerLoop(loop);
            _playerLoopHooked = true;
        }

        private static void RemovePlayerLoopHook()
        {
            if (!_playerLoopHooked) return;
            _playerLoopHooked = false;
            var loop = PlayerLoop.GetCurrentPlayerLoop();
            int index = Array.FindIndex(loop.subSystemList, system => system.type == typeof(UnityEngine.PlayerLoop.PostLateUpdate));
            if (index < 0) return;
            var phase = loop.subSystemList[index];
            var systems = phase.subSystemList ?? Array.Empty<PlayerLoopSystem>();
            // Remove only this sampler's callback; never restore an old PlayerLoop snapshot over other systems.
            var retained = systems.Where(system => system.updateDelegate != (PlayerLoopSystem.UpdateFunction)Tick).ToArray();
            if (retained.Length == systems.Length) return;
            phase.subSystemList = retained;
            loop.subSystemList[index] = phase;
            PlayerLoop.SetPlayerLoop(loop);
        }

        private static string ResultPath(string sampleId)
        {
            string projectRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
            return Path.Combine(projectRoot, "Library", "UnityMCP", "SampleRuntime", sampleId + ".json");
        }

        internal static void ValidateSampleId(string sampleId)
        {
            if (!Guid.TryParseExact(sampleId ?? string.Empty, "N", out _))
                throw new ArgumentException("sample_id must be a 32-character lowercase or uppercase GUID without dashes.");
        }

        private sealed class SampleSession
        {
            private readonly RuntimeSampleRequest _request;
            private readonly GameObject _target;
            private readonly List<RuntimeMetricProbe> _probes = new();
            private readonly List<AnimatorTrack> _animators = new();
            private readonly List<string> _warnings = new();
            private readonly HashSet<string> _warningKeys = new(StringComparer.Ordinal);
            private readonly string _targetPath;
            private string _stopReason;
            private string _error;
            private bool _completed;
            private int _lastFrame = -1;
            private int _sampledFrames;
            private int _eventCount;
            private int _droppedEvents;
            private float _startUnscaledTime;
            private float _startRealtime;
            private float _endRealtime;

            internal SampleSession(RuntimeSampleRequest request, GameObject target)
            {
                _request = request;
                _target = target;
                _targetPath = RuntimeTargetResolver.GetHierarchyPath(target.transform);
            }

            internal string Id => _request.SampleId;

            internal void Initialize()
            {
                var animatorRequests = new List<(string Path, int Layer)>();
                foreach (string property in _request.Properties)
                {
                    string normalized = Regex.Replace(property.Trim().ToLowerInvariant(), "[_\\- ]", string.Empty);
                    var match = Regex.Match(normalized, "^animator(?:\\.layer(?<layer>\\d+))?(?:\\.(?<metric>normalizedtime|state))?$");
                    if (match.Success)
                    {
                        animatorRequests.Add((property, match.Groups["layer"].Success ? int.Parse(match.Groups["layer"].Value) : 0));
                    }
                    else
                    {
                        _probes.Add(RuntimeMetricProbe.Create(_target, property, _request.IncludeSamples));
                    }
                }

                var animators = _target.GetComponentsInChildren<Animator>(_request.IncludeInactive)
                    .Take(MaxAnimators + 1).ToArray();
                if (animators.Length > MaxAnimators)
                {
                    Warn("animator_limit", $"Only the first {MaxAnimators} Animators below the target are sampled.");
                    animators = animators.Take(MaxAnimators).ToArray();
                }
                if (animatorRequests.Count > 0 && animators.Length == 0)
                    Warn("animator_missing", "No Animator was found on or below the target; Transform/field sampling still runs.");

                var requestedLayers = animatorRequests.Select(item => item.Layer).Distinct().ToArray();
                for (int index = 0; index < animators.Length; index++)
                {
                    var animator = animators[index];
                    var track = new AnimatorTrack(animator, index, requestedLayers);
                    _animators.Add(track);
                    if (animator.runtimeAnimatorController == null || !animator.isActiveAndEnabled || !animator.isInitialized)
                    {
                        Warn($"animator_unavailable_{index}", $"'{animator.name}' is inactive, uninitialized, or has no controller; normalizedTime is unavailable, so only Transform/field metrics are sampled.");
                        continue;
                    }
                    foreach (var request in animatorRequests)
                    {
                        if (_probes.Any(probe => probe.Path == $"animator[{index}].layer{request.Layer}.normalized_time")) continue;
                        _probes.Add(RuntimeMetricProbe.CreateAnimator(
                            animator, request.Layer, $"animator[{index}].layer{request.Layer}.normalized_time", request.Path, _request.IncludeSamples));
                    }
                }
                if (_probes.Count > 16) throw new ArgumentException("A maximum of 16 numeric metric probes is allowed per sample.");
                foreach (var probe in _probes)
                {
                    probe.MaxSamples = _request.MaxSamples;
                    probe.Validate();
                }
            }

            internal void Start()
            {
                _startUnscaledTime = Time.unscaledTime;
                _startRealtime = Time.realtimeSinceStartup;
                AddPlayerLoopHook();
                SampleFrame();
            }

            internal void SampleFrame()
            {
                if (_completed) return;
                try
                {
                    if (!Application.isPlaying)
                    {
                        Complete("play_mode_exit");
                        return;
                    }
                    if (_target == null)
                    {
                        Complete("target_destroyed");
                        return;
                    }
                    if (_animators.Any(track => track.Animator == null))
                    {
                        Complete("animator_destroyed");
                        return;
                    }
                    float realtime = Time.realtimeSinceStartup;
                    if (realtime - _startRealtime >= _request.Duration)
                    {
                        Complete("completed");
                        return;
                    }
                    if (Time.frameCount == _lastFrame) return;
                    _lastFrame = Time.frameCount;
                    float elapsed = Mathf.Max(0f, Time.unscaledTime - _startUnscaledTime);
                    float delta = Mathf.Max(0f, Time.unscaledDeltaTime);
                    foreach (var probe in _probes) probe.Sample(Time.frameCount, elapsed, delta);
                    foreach (var track in _animators) track.Sample(this, elapsed);
                    _sampledFrames++;
                    if (_sampledFrames >= MaxFrames) Complete("frame_limit");
                }
                catch (Exception ex)
                {
                    _error = ex.Message;
                    Complete("read_error");
                }
            }

            internal void Complete(string reason)
            {
                if (_completed) return;
                _completed = true;
                _stopReason = reason;
                _endRealtime = Time.realtimeSinceStartup;
                if (ReferenceEquals(_active, this)) _active = null;
                try
                {
                    RemovePlayerLoopHook();
                }
                catch (Exception ex)
                {
                    Warn("player_loop_cleanup", $"The sampler callback could not be removed cleanly: {ex.Message}");
                }
                Persist(BuildResponse());
            }

            internal object GetProgress()
            {
                return new
                {
                    sampleId = Id,
                    sampledFrames = _sampledFrames,
                    lastFrame = _lastFrame,
                    playerLoopHooked = _playerLoopHooked,
                    elapsedSeconds = Mathf.Max(0f, Time.realtimeSinceStartup - _startRealtime),
                };
            }

            private object BuildResponse()
            {
                var metrics = _probes.Select(probe => probe.ToJson(ResolveThreshold(probe), _request.MaxSpikes)).ToArray();
                var events = new JArray();
                foreach (var track in _animators)
                {
                    foreach (var item in track.Events)
                    {
                        if (events.Count >= MaxEvents) continue;
                        events.Add(item);
                    }
                }
                if (_droppedEvents > 0) Warn("event_limit", $"Only the first {MaxEvents} Animator events are returned; {_droppedEvents} later events were omitted.");
                return new SuccessResponse($"Runtime sample {Id} finished: {_stopReason}.", new
                {
                    sampleId = Id,
                    target = _targetPath,
                    targetInstanceId = _target != null ? _target.GetInstanceID() : (int?)null,
                    properties = _request.Properties.ToArray(),
                    duration = _request.Duration,
                    elapsedSeconds = Mathf.Max(0f, _endRealtime - _startRealtime),
                    sampledFrames = _sampledFrames,
                    complete = _stopReason == "completed",
                    partial = _stopReason != "completed",
                    stopReason = _stopReason,
                    error = _error,
                    metrics,
                    animatorEvents = events,
                    animatorEventCount = _eventCount,
                    animators = _animators.Select(track => track.ToJson()).ToArray(),
                    samplesReturned = _request.IncludeSamples,
                    maxSamplesPerMetric = _request.IncludeSamples ? _request.MaxSamples : 0,
                    warnings = _warnings.ToArray(),
                });
            }

            private float? ResolveThreshold(RuntimeMetricProbe probe)
            {
                if (_request.Thresholds.TryGetValue(probe.Path, out float exact)) return exact;
                if (_request.Thresholds.TryGetValue(probe.ThresholdKey, out float alias)) return alias;
                if (_request.Thresholds.TryGetValue("*", out float all)) return all;
                return null;
            }

            internal void AddAnimatorEvent(AnimatorTrack track, int layer, string eventType, int fromHash, int toHash, bool inTransition, float transitionDuration, float elapsed)
            {
                _eventCount++;
                if (_eventCount > MaxEvents)
                {
                    _droppedEvents++;
                    return;
                }
                track.Events.Add(new JObject
                {
                    ["frame"] = Time.frameCount,
                    ["time"] = elapsed,
                    ["unityTime"] = Time.time,
                    ["unscaledTime"] = Time.unscaledTime,
                    ["deltaTime"] = Time.deltaTime,
                    ["unscaledDeltaTime"] = Time.unscaledDeltaTime,
                    ["type"] = eventType,
                    ["animator"] = RuntimeTargetResolver.GetHierarchyPath(track.Animator.transform),
                    ["layer"] = layer,
                    ["fromHash"] = fromHash,
                    ["toHash"] = toHash,
                    ["from"] = track.ResolveState(fromHash),
                    ["to"] = track.ResolveState(toHash),
                    ["inTransition"] = inTransition,
                    ["transitionDuration"] = transitionDuration,
                });
            }

            internal void Warn(string key, string message)
            {
                if (_warningKeys.Add(key)) _warnings.Add(message);
            }

            private void Persist(object response)
            {
                try
                {
                    string path = ResultPath(Id);
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    File.WriteAllText(path, JObject.FromObject(response).ToString());
                }
                catch (Exception ex)
                {
                    Debug.LogWarning($"[sample_runtime] Could not persist partial result '{Id}': {ex.Message}");
                }
            }
        }

        private sealed class AnimatorTrack
        {
            private readonly int _index;
            private readonly Dictionary<int, string> _stateNames = new();
            private readonly Dictionary<int, Snapshot> _previous = new();
            private readonly Dictionary<int, float> _initialNormalizedTime = new();
            private bool _warnedNoController;

            private sealed class Snapshot
            {
                internal int Hash;
                internal int NextHash;
                internal bool InTransition;
                internal float TransitionDuration;
            }

            internal AnimatorTrack(Animator animator, int index, IEnumerable<int> layers)
            {
                Animator = animator;
                _index = index;
                Layers = layers.ToArray();
                BuildStateNames(animator.runtimeAnimatorController);
            }

            internal Animator Animator { get; }
            internal int[] Layers { get; }
            internal List<JObject> Events { get; } = new();

            internal void Sample(SampleSession session, float elapsed)
            {
                if (Animator.runtimeAnimatorController == null)
                {
                    if (!_warnedNoController)
                    {
                        _warnedNoController = true;
                        session.Warn("animator_controller_" + _index, $"'{Animator.name}' has no runtime AnimatorController; no state samples are available.");
                    }
                    return;
                }
                if (!Animator.isActiveAndEnabled || !Animator.isInitialized) return;
                foreach (int layer in Layers)
                {
                    var state = Animator.GetCurrentAnimatorStateInfo(layer);
                    bool inTransition = Animator.IsInTransition(layer);
                    var transition = inTransition ? Animator.GetAnimatorTransitionInfo(layer) : default;
                    int nextHash = inTransition ? Animator.GetNextAnimatorStateInfo(layer).fullPathHash : 0;
                    if (_previous.TryGetValue(layer, out var previous))
                    {
                        if (previous.Hash != state.fullPathHash)
                            session.AddAnimatorEvent(this, layer, "state_change", previous.Hash, state.fullPathHash, inTransition, transition.duration, elapsed);
                        if (previous.InTransition != inTransition)
                            session.AddAnimatorEvent(this, layer, inTransition ? "transition_start" : "transition_end", previous.Hash,
                                inTransition ? nextHash : state.fullPathHash, inTransition, transition.duration, elapsed);
                    }
                    else
                    {
                        _initialNormalizedTime[layer] = state.normalizedTime;
                    }
                    _previous[layer] = new Snapshot
                    {
                        Hash = state.fullPathHash,
                        NextHash = nextHash,
                        InTransition = inTransition,
                        TransitionDuration = transition.duration,
                    };
                }
            }

            internal string ResolveState(int hash)
            {
                if (hash == 0) return null;
                return _stateNames.TryGetValue(hash, out string name) ? name : $"unknown_state_hash:{hash}";
            }

            internal JObject ToJson()
            {
                return new JObject
                {
                    ["target"] = Animator != null ? RuntimeTargetResolver.GetHierarchyPath(Animator.transform) : null,
                    ["controller"] = Animator != null && Animator.runtimeAnimatorController != null ? Animator.runtimeAnimatorController.name : null,
                    ["layers"] = new JArray(Layers),
                    ["states"] = new JObject(Layers.Select(layer =>
                    {
                        _previous.TryGetValue(layer, out var state);
                        return new JProperty(layer.ToString(), state == null ? null : new JObject
                        {
                            ["fullPathHash"] = state.Hash,
                            ["name"] = ResolveState(state.Hash),
                            ["nextStateHash"] = state.NextHash,
                            ["nextState"] = ResolveState(state.NextHash),
                            ["inTransition"] = state.InTransition,
                            ["transitionDuration"] = state.TransitionDuration,
                            ["initialNormalizedTime"] = _initialNormalizedTime.TryGetValue(layer, out float time) ? time : (float?)null,
                        });
                    })),
                };
            }

            private void BuildStateNames(RuntimeAnimatorController runtimeController)
            {
                var overrideController = runtimeController as AnimatorOverrideController;
                if (overrideController != null) runtimeController = overrideController.runtimeAnimatorController;
                if (runtimeController is not AnimatorController controller) return;
                foreach (var layer in controller.layers)
                    AddStateMachine(layer.stateMachine, layer.name);
            }

            private void AddStateMachine(AnimatorStateMachine stateMachine, string prefix)
            {
                if (stateMachine == null) return;
                foreach (var child in stateMachine.states)
                {
                    string path = prefix + "." + child.state.name;
                    _stateNames[Animator.StringToHash(path)] = path;
                    _stateNames[child.state.nameHash] = path;
                }
                foreach (var child in stateMachine.stateMachines)
                    AddStateMachine(child.stateMachine, prefix + "." + child.stateMachine.name);
            }
        }
    }
}
