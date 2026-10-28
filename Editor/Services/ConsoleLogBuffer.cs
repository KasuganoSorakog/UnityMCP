using System;
using System.Collections.Generic;
using UnityEngine;

namespace MCPForUnity.Editor.Services
{
    /// <summary>
    /// Bounded, timestamped mirror of the Unity console. Unity's own LogEntry struct carries no
    /// timestamp, so time-based filtering is served from this buffer instead of from LogEntries.
    /// </summary>
    [UnityEditor.InitializeOnLoad]
    internal static class ConsoleLogBuffer
    {
        internal struct Entry
        {
            internal long utcTicks;
            internal LogType type;
            internal string condition;
            internal string stackTrace;
        }

        private const int Capacity = 5000;
        private const int MaxConditionChars = 4000;
        private const int MaxStackTraceChars = 2000;

        private static readonly object Gate = new object();
        private static readonly LinkedList<Entry> Entries = new LinkedList<Entry>();

        static ConsoleLogBuffer()
        {
            Application.logMessageReceivedThreaded -= OnLogMessage;
            Application.logMessageReceivedThreaded += OnLogMessage;
        }

        private static void OnLogMessage(string condition, string stackTrace, LogType type)
        {
            if (condition == null) return;
            Entry entry = new Entry
            {
                utcTicks = DateTime.UtcNow.Ticks,
                type = type,
                condition = Truncate(condition, MaxConditionChars),
                stackTrace = Truncate(stackTrace, MaxStackTraceChars),
            };
            lock (Gate)
            {
                Entries.AddLast(entry);
                while (Entries.Count > Capacity) Entries.RemoveFirst();
            }
        }

        /// <summary>Newest-first snapshot of entries recorded at or after <paramref name="sinceUtc"/>.</summary>
        internal static List<Entry> CollectSince(DateTime sinceUtc)
        {
            long ticks = sinceUtc.ToUniversalTime().Ticks;
            var results = new List<Entry>();
            lock (Gate)
            {
                LinkedListNode<Entry> node = Entries.Last;
                while (node != null)
                {
                    if (node.Value.utcTicks < ticks) break;
                    results.Add(node.Value);
                    node = node.Previous;
                }
            }
            return results;
        }

        internal static int Count
        {
            get { lock (Gate) return Entries.Count; }
        }

        internal static DateTime? CoverageStartUtc
        {
            get
            {
                lock (Gate)
                {
                    return Entries.Count == 0 ? (DateTime?)null : new DateTime(Entries.First.Value.utcTicks, DateTimeKind.Utc);
                }
            }
        }

        internal static bool IsFull
        {
            get { lock (Gate) return Entries.Count >= Capacity; }
        }

        private static string Truncate(string value, int maxChars)
        {
            if (string.IsNullOrEmpty(value) || value.Length <= maxChars) return value;
            return value.Substring(0, maxChars) + "...[truncated]";
        }
    }
}
