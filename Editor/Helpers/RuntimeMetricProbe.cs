using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace MCPForUnity.Editor.Helpers
{
    internal sealed class RuntimeMetricProbe
    {
        private const int MaxChanges = 32;
        private readonly UnityEngine.Object _source;
        private readonly Func<object> _reader;
        private readonly bool _keepSamples;
        private readonly NumericSeries[] _series;
        private readonly NumericSeries _deltaSeries = new();
        private readonly NumericSeries _rateSeries = new();
        private readonly List<StepValue> _steps = new();
        private readonly List<JObject> _samples;
        private readonly List<JObject> _changes = new();
        private double[] _previous;
        private JToken _previousEvent;
        private JToken _firstValue;
        private long _sampleCount;
        private long _changeCount;
        private int _lastFrame = -1;

        private sealed class StepValue
        {
            public int Frame;
            public float Elapsed;
            public double Delta;
            public double Rate;
        }

        internal sealed class NumericSeries
        {
            private long _count;
            private double _mean;
            private double _m2;
            private double _min;
            private double _max;

            internal double Mean => _count > 0 ? _mean : 0.0;
            internal double StandardDeviation => _count > 1 ? Math.Sqrt(_m2 / (_count - 1)) : 0.0;

            internal void Add(double value)
            {
                if (!IsFinite(value)) throw new InvalidOperationException("A sampled value was NaN or infinity.");
                _count++;
                if (_count == 1) _min = _max = value;
                else
                {
                    _min = Math.Min(_min, value);
                    _max = Math.Max(_max, value);
                }
                double difference = value - _mean;
                _mean += difference / _count;
                _m2 += difference * (value - _mean);
            }

            internal JObject ToJson()
            {
                return new JObject
                {
                    ["count"] = _count,
                    ["mean"] = _count > 0 ? _mean : null,
                    ["stddev"] = _count > 1 ? StandardDeviation : 0.0,
                    ["min"] = _count > 0 ? _min : null,
                    ["max"] = _count > 0 ? _max : null,
                };
            }
        }

        private RuntimeMetricProbe(
            string path,
            string thresholdKey,
            string kind,
            string unit,
            string[] components,
            UnityEngine.Object source,
            Func<object> reader,
            bool keepSamples)
        {
            Path = path;
            ThresholdKey = thresholdKey;
            Kind = kind;
            Unit = unit;
            Components = components;
            _source = source;
            _reader = reader;
            _keepSamples = keepSamples;
            _series = components.Select(_ => new NumericSeries()).ToArray();
            _samples = keepSamples ? new List<JObject>() : null;
        }

        internal string Path { get; }
        internal string ThresholdKey { get; }
        internal string Kind { get; }
        internal string Unit { get; }
        internal string[] Components { get; }
        internal bool HasSamples { get; private set; }
        internal int MaxSamples { get; set; } = 120;

        internal static RuntimeMetricProbe Create(GameObject target, string path, bool keepSamples)
        {
            string normalized = path.Trim().ToLowerInvariant().Replace("_", string.Empty).Replace("-", string.Empty);
            switch (normalized)
            {
                case "transform.position":
                    return Vector(target, path, "position", "m", new[] { "x", "y", "z" }, () => target.transform.position, keepSamples);
                case "transform.localposition":
                    return Vector(target, path, "position", "m", new[] { "x", "y", "z" }, () => target.transform.localPosition, keepSamples);
                case "transform.rotation":
                case "transform.localrotation":
                    return new RuntimeMetricProbe(path, path, "quaternion", "degrees", new[] { "x", "y", "z", "w" }, target.transform,
                        () => normalized == "transform.rotation" ? target.transform.rotation : target.transform.localRotation, keepSamples);
                case "transform.eulerangles":
                    return Vector(target, path, "rotation_euler", "degrees", new[] { "x", "y", "z" }, () => target.transform.eulerAngles, keepSamples);
                case "transform.localeulerangles":
                    return Vector(target, path, "rotation_euler", "degrees", new[] { "x", "y", "z" }, () => target.transform.localEulerAngles, keepSamples);
                case "transform.localscale":
                    return Vector(target, path, "scale", "scale", new[] { "x", "y", "z" }, () => target.transform.localScale, keepSamples);
                case "transform.lossyscale":
                    return Vector(target, path, "scale", "scale", new[] { "x", "y", "z" }, () => target.transform.lossyScale, keepSamples);
                default:
                    return CreateFieldProbe(target, path, keepSamples);
            }
        }

        internal static RuntimeMetricProbe CreateAnimator(Animator animator, int layer, string path, string thresholdKey, bool keepSamples)
        {
            if (layer < 0 || layer >= animator.layerCount)
                throw new ArgumentException($"Animator '{animator.name}' has {animator.layerCount} layer(s); layer {layer} is invalid.");
            return new RuntimeMetricProbe(path, thresholdKey, "scalar", "normalized_time", new[] { "value" }, animator,
                () => animator.GetCurrentAnimatorStateInfo(layer).normalizedTime, keepSamples);
        }

        private static RuntimeMetricProbe Vector(
            GameObject target,
            string path,
            string kind,
            string unit,
            string[] components,
            Func<Vector3> reader,
            bool keepSamples)
        {
            return new RuntimeMetricProbe(path, path, kind, unit, components, target.transform, () => reader(), keepSamples);
        }

        private static RuntimeMetricProbe CreateFieldProbe(GameObject target, string path, bool keepSamples)
        {
            if (path.Length > 256) throw new ArgumentException("A field path cannot exceed 256 characters.");
            string[] segments = path.Split('.');
            if (segments.Length < 2 || segments.Length > 16 || segments.Any(string.IsNullOrWhiteSpace))
                throw new ArgumentException($"Field '{path}' must use ComponentType.field[.nestedField] dot-separated paths.");

            var components = target.GetComponents<Component>().Where(component => component != null).ToArray();
            Component matchedComponent = null;
            int fieldStart = -1;
            for (int length = 1; length < segments.Length && matchedComponent == null; length++)
            {
                string typeName = string.Join(".", segments.Take(length));
                var matches = components.Where(component =>
                    string.Equals(component.GetType().Name, typeName, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(component.GetType().FullName, typeName, StringComparison.OrdinalIgnoreCase)).ToArray();
                if (matches.Length > 1)
                    throw new ArgumentException($"Component type '{typeName}' occurs {matches.Length} times on '{target.name}'. Sampling does not guess between duplicate components.");
                if (matches.Length == 1)
                {
                    matchedComponent = matches[0];
                    fieldStart = length;
                }
            }
            if (matchedComponent == null)
            {
                string available = string.Join(", ", components.Select(component => component.GetType().FullName).Distinct());
                throw new ArgumentException($"No component matching '{path}' exists directly on '{target.name}'. Components: {available}");
            }

            var fields = new List<FieldInfo>();
            Type owner = matchedComponent.GetType();
            for (int index = fieldStart; index < segments.Length; index++)
            {
                FieldInfo field = owner.GetField(segments[index], BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (field == null || (!field.IsPublic && field.GetCustomAttribute<SerializeField>() == null))
                    throw new ArgumentException($"Field '{segments[index]}' in '{path}' was not found or is not public/[SerializeField]. Properties, methods, and indexers are not sampled.");
                if (field.IsStatic || field.IsLiteral)
                    throw new ArgumentException($"Field '{path}' must be a non-static instance field.");
                fields.Add(field);
                owner = field.FieldType;
            }

            string kind;
            string unit = "value";
            string[] names;
            object Read()
            {
                object value = matchedComponent;
                foreach (var field in fields) value = value == null ? null : field.GetValue(value);
                return value;
            }
            Type leaf = fields[^1].FieldType;
            if (IsNumeric(leaf)) { kind = "scalar"; names = new[] { "value" }; }
            else if (leaf == typeof(Vector2)) { kind = "vector2"; names = new[] { "x", "y" }; }
            else if (leaf == typeof(Vector3)) { kind = "vector3"; names = new[] { "x", "y", "z" }; }
            else if (leaf == typeof(Vector4) || leaf == typeof(Color)) { kind = "vector4"; names = new[] { "x", "y", "z", "w" }; }
            else if (leaf == typeof(Quaternion)) { kind = "quaternion"; names = new[] { "x", "y", "z", "w" }; unit = "degrees"; }
            else if (leaf == typeof(bool) || leaf == typeof(string) || leaf == typeof(char) || leaf.IsEnum)
            { kind = "event"; names = new[] { "value" }; }
            else
                throw new ArgumentException($"Field '{path}' has unsupported type '{leaf.FullName}'. Use a numeric, Vector2/3/4, Quaternion, Color, bool, string, char, or enum leaf; arrays, Unity object references, and object graphs are not sampled.");

            if (leaf == typeof(Color)) names = new[] { "r", "g", "b", "a" };
            return new RuntimeMetricProbe(path, path, kind, unit, names, matchedComponent, Read, keepSamples);
        }

        internal void Validate()
        {
            Read();
        }

        internal void Sample(int frame, float elapsed, float frameDelta)
        {
            if (frame == _lastFrame) return;
            _lastFrame = frame;
            object value = Read();
            if (value == null) throw new InvalidOperationException($"'{Path}' became null while sampling.");
            _sampleCount++;
            if (Kind == "event")
            {
                string text = Convert.ToString(value, CultureInfo.InvariantCulture) ?? string.Empty;
                bool truncated = text.Length > 128;
                if (truncated) text = text.Substring(0, 128);
                var token = new JValue(text);
                _firstValue ??= token;
                if (_previousEvent != null && !JToken.DeepEquals(_previousEvent, token))
                {
                    _changeCount++;
                    if (_changes.Count < MaxChanges)
                        _changes.Add(new JObject { ["frame"] = frame, ["time"] = elapsed, ["from"] = _previousEvent, ["to"] = token });
                }
                _previousEvent = token;
                HasSamples = true;
                if (_keepSamples) _samples.Add(FrameJson(frame, elapsed, token));
                return;
            }

            double[] values = ReadNumbers(value);
            if (values.Length != _series.Length) throw new InvalidOperationException($"'{Path}' returned an unexpected value shape.");
            for (int i = 0; i < values.Length; i++) _series[i].Add(values[i]);
            if (_previous != null)
            {
                double squared = values.Select((number, index) => (number - _previous[index]) * (number - _previous[index])).Sum();
                double delta = Kind == "quaternion" ? Quaternion.Angle(ToQuaternion(_previous), ToQuaternion(values)) : Math.Sqrt(squared);
                double rate = frameDelta > 0.000001f ? delta / frameDelta : 0.0;
                _deltaSeries.Add(delta);
                _rateSeries.Add(rate);
                _steps.Add(new StepValue { Frame = frame, Elapsed = elapsed, Delta = delta, Rate = rate });
            }
            _previous = values;
            HasSamples = true;
            JToken json = values.Length == 1 ? new JValue(values[0]) : new JArray(values.Select(number => new JValue(number)));
            _firstValue ??= json;
            if (_keepSamples) _samples.Add(FrameJson(frame, elapsed, json));
        }

        internal JObject ToJson(float? explicitThreshold, int maxSpikes)
        {
            double scale = Kind == "quaternion" ? 0.0 : _series.Select(series => series.StandardDeviation).DefaultIfEmpty(0.0).Max();
            double threshold = explicitThreshold ?? Math.Max(Math.Max(_deltaSeries.Mean + 6.0 * _deltaSeries.StandardDeviation, 0.05 * scale), 0.000001);
            var spikes = _steps.Where(step => step.Delta > threshold)
                .OrderByDescending(step => step.Delta).Take(maxSpikes)
                .OrderBy(step => step.Elapsed)
                .Select(step => new JObject
                {
                    ["frame"] = step.Frame,
                    ["time"] = step.Elapsed,
                    ["delta"] = step.Delta,
                    ["deltaPerSecond"] = step.Rate,
                }).ToArray();
            var result = new JObject
            {
                ["path"] = Path,
                ["kind"] = Kind,
                ["unit"] = Unit,
                ["count"] = _sampleCount,
                ["first"] = _firstValue,
                ["threshold"] = threshold,
                ["thresholdSource"] = explicitThreshold.HasValue ? "explicit" : "mean_frame_delta_plus_6_stddev_with_5_percent_signal_stddev_floor",
                ["spikes"] = new JArray(spikes),
                ["truncated"] = _steps.Count(step => step.Delta > threshold) > maxSpikes || _changeCount > _changes.Count,
            };
            if (Kind == "event")
            {
                result["last"] = _previousEvent;
                result["changeCount"] = _changeCount;
                result["changes"] = new JArray(_changes);
                result["threshold"] = null;
                result["thresholdSource"] = "event_changes";
            }
            else
            {
                result["last"] = _previous == null ? null : _previous.Length == 1 ? new JValue(_previous[0]) : new JArray(_previous.Select(number => new JValue(number)));
                result["delta"] = _deltaSeries.ToJson();
                result["deltaPerSecond"] = _rateSeries.ToJson();
                result["maxFrameDelta"] = _steps.Count == 0 ? null : _steps.Max(step => step.Delta);
                result["maxDeltaPerSecond"] = _steps.Count == 0 ? null : _steps.Max(step => step.Rate);
                result["spikeThresholdUnit"] = Unit + "/frame";
                result["rateUnit"] = Unit + "/unscaled_second";
                if (Kind != "quaternion")
                {
                    var components = new JObject();
                    for (int i = 0; i < _series.Length; i++) components[Components[i]] = _series[i].ToJson();
                    result["components"] = components;
                }
            }
            if (_keepSamples) result["samples"] = new JArray(Downsample(_samples, MaxSamples));
            return result;
        }

        private object Read()
        {
            if (_source == null) throw new InvalidOperationException($"The component supplying '{Path}' was destroyed.");
            try { return _reader(); }
            catch (Exception ex) { throw new InvalidOperationException($"Could not read '{Path}': {ex.Message}", ex); }
        }

        private double[] ReadNumbers(object value)
        {
            return value switch
            {
                Vector2 vector => new double[] { vector.x, vector.y },
                Vector3 vector => new double[] { vector.x, vector.y, vector.z },
                Vector4 vector => new double[] { vector.x, vector.y, vector.z, vector.w },
                Quaternion quaternion => new double[] { quaternion.x, quaternion.y, quaternion.z, quaternion.w },
                Color color => new double[] { color.r, color.g, color.b, color.a },
                _ => new[] { Convert.ToDouble(value, CultureInfo.InvariantCulture) },
            };
        }

        private static Quaternion ToQuaternion(double[] values)
        {
            return new Quaternion((float)values[0], (float)values[1], (float)values[2], (float)values[3]).normalized;
        }

        private static JObject FrameJson(int frame, float elapsed, JToken value)
        {
            return new JObject
            {
                ["frame"] = frame,
                ["time"] = elapsed,
                ["unityTime"] = Time.time,
                ["unscaledTime"] = Time.unscaledTime,
                ["deltaTime"] = Time.deltaTime,
                ["unscaledDeltaTime"] = Time.unscaledDeltaTime,
                ["value"] = value,
            };
        }

        private static IEnumerable<JObject> Downsample(IReadOnlyList<JObject> samples, int maximum)
        {
            if (samples.Count <= maximum) return samples;
            var result = new List<JObject>(maximum);
            for (int index = 0; index < maximum; index++)
                result.Add(samples[(int)Math.Round(index * (samples.Count - 1) / (double)(maximum - 1))]);
            return result;
        }

        private static bool IsNumeric(Type type)
        {
            return type == typeof(byte) || type == typeof(sbyte) || type == typeof(short) || type == typeof(ushort) ||
                   type == typeof(int) || type == typeof(uint) || type == typeof(long) || type == typeof(ulong) ||
                   type == typeof(float) || type == typeof(double) || type == typeof(decimal);
        }

        private static bool IsFinite(double value) => !double.IsNaN(value) && !double.IsInfinity(value);
    }
}
