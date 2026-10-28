using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using MCPForUnity.Editor.Helpers; // For Response class
using MCPForUnity.Editor.Services;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditorInternal;
using UnityEngine;

namespace MCPForUnity.Editor.Tools
{
    /// <summary>
    /// Handles reading and clearing Unity Editor console log entries.
    /// Uses reflection to access internal LogEntry methods/properties.
    /// </summary>
    [McpForUnityTool("read_console", AutoRegister = false)]
    public static class ReadConsole
    {
        private const int DefaultTailScanEntries = 2000;
        private const int MaxTailScanEntries = 20000;
        private const int DefaultMaxMessageChars = 4000;
        private const int MaxMessageCharsLimit = 20000;

        // (Calibration removed)

        // Reflection members for accessing internal LogEntry data
        // private static MethodInfo _getEntriesMethod; // Removed as it's unused and fails reflection
        private static MethodInfo _startGettingEntriesMethod;
        private static MethodInfo _endGettingEntriesMethod; // Renamed from _stopGettingEntriesMethod, trying End...
        private static MethodInfo _clearMethod;
        private static MethodInfo _getCountMethod;
        private static MethodInfo _getEntryMethod;
        private static FieldInfo _modeField;
        private static FieldInfo _messageField;
        private static FieldInfo _fileField;
        private static FieldInfo _lineField;
        private static FieldInfo _instanceIdField;

        // Note: Timestamp is not directly available in LogEntry; need to parse message or find alternative?

        // Static constructor for reflection setup
        static ReadConsole()
        {
            try
            {
                Type logEntriesType = typeof(EditorApplication).Assembly.GetType(
                    "UnityEditor.LogEntries"
                );
                if (logEntriesType == null)
                    throw new Exception("Could not find internal type UnityEditor.LogEntries");



                // Include NonPublic binding flags as internal APIs might change accessibility
                BindingFlags staticFlags =
                    BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
                BindingFlags instanceFlags =
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;

                _startGettingEntriesMethod = logEntriesType.GetMethod(
                    "StartGettingEntries",
                    staticFlags
                );
                if (_startGettingEntriesMethod == null)
                    throw new Exception("Failed to reflect LogEntries.StartGettingEntries");

                // Try reflecting EndGettingEntries based on warning message
                _endGettingEntriesMethod = logEntriesType.GetMethod(
                    "EndGettingEntries",
                    staticFlags
                );
                if (_endGettingEntriesMethod == null)
                    throw new Exception("Failed to reflect LogEntries.EndGettingEntries");

                _clearMethod = logEntriesType.GetMethod("Clear", staticFlags);
                if (_clearMethod == null)
                    throw new Exception("Failed to reflect LogEntries.Clear");

                _getCountMethod = logEntriesType.GetMethod("GetCount", staticFlags);
                if (_getCountMethod == null)
                    throw new Exception("Failed to reflect LogEntries.GetCount");

                _getEntryMethod = logEntriesType.GetMethod("GetEntryInternal", staticFlags);
                if (_getEntryMethod == null)
                    throw new Exception("Failed to reflect LogEntries.GetEntryInternal");

                Type logEntryType = typeof(EditorApplication).Assembly.GetType(
                    "UnityEditor.LogEntry"
                );
                if (logEntryType == null)
                    throw new Exception("Could not find internal type UnityEditor.LogEntry");

                _modeField = logEntryType.GetField("mode", instanceFlags);
                if (_modeField == null)
                    throw new Exception("Failed to reflect LogEntry.mode");

                _messageField = logEntryType.GetField("message", instanceFlags);
                if (_messageField == null)
                    throw new Exception("Failed to reflect LogEntry.message");

                _fileField = logEntryType.GetField("file", instanceFlags);
                if (_fileField == null)
                    throw new Exception("Failed to reflect LogEntry.file");

                _lineField = logEntryType.GetField("line", instanceFlags);
                if (_lineField == null)
                    throw new Exception("Failed to reflect LogEntry.line");

                _instanceIdField = logEntryType.GetField("instanceID", instanceFlags);
                if (_instanceIdField == null)
                    throw new Exception("Failed to reflect LogEntry.instanceID");

                // (Calibration removed)

            }
            catch (Exception e)
            {
                McpLog.Error(
                    $"[ReadConsole] Static Initialization Failed: Could not setup reflection for LogEntries/LogEntry. Console reading/clearing will likely fail. Specific Error: {e.Message}"
                );
                // Set members to null to prevent NullReferenceExceptions later, HandleCommand should check this.
                _startGettingEntriesMethod =
                    _endGettingEntriesMethod =
                    _clearMethod =
                    _getCountMethod =
                    _getEntryMethod =
                        null;
                _modeField = _messageField = _fileField = _lineField = _instanceIdField = null;
            }
        }

        // --- Main Handler ---

        public static object HandleCommand(JObject @params)
        {
            // Check if ALL required reflection members were successfully initialized.
            if (
                _startGettingEntriesMethod == null
                || _endGettingEntriesMethod == null
                || _clearMethod == null
                || _getCountMethod == null
                || _getEntryMethod == null
                || _modeField == null
                || _messageField == null
                || _fileField == null
                || _lineField == null
                || _instanceIdField == null
            )
            {
                // Log the error here as well for easier debugging in Unity Console
                McpLog.Error(
                    "[ReadConsole] HandleCommand called but reflection members are not initialized. Static constructor might have failed silently or there's an issue."
                );
                return new ErrorResponse(
                    "ReadConsole handler failed to initialize due to reflection errors. Cannot access console logs."
                );
            }

            string action = @params["action"]?.ToString().ToLower() ?? "get";

            try
            {
                if (action == "clear")
                {
                    return ClearConsole();
                }
                else if (action == "get")
                {
                    // Extract parameters for 'get'
                    var types =
                        (@params["types"] as JArray)?.Select(t => t.ToString().ToLower()).ToList()
                        ?? new List<string> { "error", "warning" };
                    int? count = @params["count"]?.ToObject<int?>();
                    int? pageSize =
                        @params["pageSize"]?.ToObject<int?>()
                        ?? @params["page_size"]?.ToObject<int?>();
                    int? cursor = @params["cursor"]?.ToObject<int?>();
                    string filterText = @params["filterText"]?.ToString();
                    string filterRegex =
                        @params["regex"]?.ToString() ?? @params["filterRegex"]?.ToString();
                    string sinceTimestampStr = @params["sinceTimestamp"]?.ToString();
                    string format = (@params["format"]?.ToString() ?? "plain").ToLower();
                    bool includeStacktrace =
                        @params["includeStacktrace"]?.ToObject<bool?>() ?? false;
                    int maxEntriesToScan = Mathf.Clamp(
                        @params["maxEntriesToScan"]?.ToObject<int?>()
                            ?? @params["max_entries_to_scan"]?.ToObject<int?>()
                            ?? DefaultTailScanEntries,
                        1,
                        MaxTailScanEntries
                    );
                    int maxMessageChars = Mathf.Clamp(
                        @params["maxMessageChars"]?.ToObject<int?>()
                            ?? @params["max_message_chars"]?.ToObject<int?>()
                            ?? DefaultMaxMessageChars,
                        200,
                        MaxMessageCharsLimit
                    );

                    if (types.Contains("all"))
                    {
                        types = new List<string> { "error", "warning", "log" }; // Expand 'all'
                    }

                    TextFilter textFilter;
                    try
                    {
                        textFilter = new TextFilter(filterText, filterRegex);
                    }
                    catch (ArgumentException e)
                    {
                        return new ErrorResponse($"Invalid 'regex' '{filterRegex}': {e.Message}");
                    }
                    catch (RegexMatchTimeoutException)
                    {
                        return new ErrorResponse(
                            $"Invalid 'regex' '{filterRegex}': compiling it exceeded the match timeout."
                        );
                    }

                    DateTime? sinceUtc = null;
                    if (!string.IsNullOrEmpty(sinceTimestampStr))
                    {
                        if (!TryParseTimestamp(sinceTimestampStr, out DateTime parsed))
                        {
                            return new ErrorResponse(
                                $"Invalid 'since_timestamp' '{sinceTimestampStr}'. Use UTC ISO-8601 (2026-10-28T01:40:00Z), ISO without offset (read as UTC), or unix seconds/milliseconds."
                            );
                        }
                        sinceUtc = parsed;
                    }

                    if (sinceUtc.HasValue)
                    {
                        return GetBufferedEntries(
                            sinceUtc.Value,
                            types,
                            count,
                            pageSize,
                            cursor,
                            textFilter,
                            format,
                            includeStacktrace,
                            maxMessageChars
                        );
                    }

                    return GetConsoleEntries(
                        types,
                        count,
                        pageSize,
                        cursor,
                        textFilter,
                        format,
                        includeStacktrace,
                        maxEntriesToScan,
                        maxMessageChars
                    );
                }
                else
                {
                    return new ErrorResponse(
                        $"Unknown action: '{action}'. Valid actions are 'get' or 'clear'."
                    );
                }
            }
            catch (Exception e)
            {
                McpLog.Error($"[ReadConsole] Action '{action}' failed: {e}");
                return new ErrorResponse($"Internal error processing action '{action}': {e.Message}");
            }
        }

        // --- Action Implementations ---

        private static object ClearConsole()
        {
            try
            {
                _clearMethod.Invoke(null, null); // Static method, no instance, no parameters
                return new SuccessResponse("Console cleared successfully.");
            }
            catch (Exception e)
            {
                McpLog.Error($"[ReadConsole] Failed to clear console: {e}");
                return new ErrorResponse($"Failed to clear console: {e.Message}");
            }
        }

        /// <summary>
        /// Retrieves console log entries with optional filtering and paging.
        /// </summary>
        /// <param name="types">Log types to include (e.g., "error", "warning", "log").</param>
        /// <param name="count">Maximum entries to return in non-paging mode. Ignored when paging is active.</param>
        /// <param name="pageSize">Number of entries per page. Defaults to 50 when omitted.</param>
        /// <param name="cursor">Starting index for paging (0-based). Defaults to 0.</param>
        /// <param name="filter">Optional text/regex filter (both must match when supplied).</param>
        /// <param name="format">Output format: "plain", "detailed", or "json".</param>
        /// <param name="includeStacktrace">Whether to include stack traces in the output.</param>
        /// <returns>A success response with entries, or an error response.</returns>
        private static object GetConsoleEntries(
            List<string> types,
            int? count,
            int? pageSize,
            int? cursor,
            TextFilter filter,
            string format,
            bool includeStacktrace,
            int maxEntriesToScan,
            int maxMessageChars
        )
        {
            List<object> formattedEntries = new List<object>();
            int retrievedCount = 0;
            int totalMatches = 0;
            bool sawInfoEntry = false;
            bool usePaging = pageSize.HasValue || cursor.HasValue;
            bool useTailScan = !usePaging && count.HasValue && count.Value > 0;
            // pageSize defaults to 50 when omitted; count is the overall non-paging limit only
            int resolvedPageSize = Mathf.Clamp(pageSize ?? 50, 1, 500);
            int resolvedCursor = Mathf.Max(0, cursor ?? 0);
            int pageEndExclusive = resolvedCursor + resolvedPageSize;

            try
            {
                // LogEntries requires calling Start/Stop around GetEntries/GetEntryInternal
                _startGettingEntriesMethod.Invoke(null, null);

                int totalEntries = (int)_getCountMethod.Invoke(null, null);
                // Create instance to pass to GetEntryInternal - Ensure the type is correct
                Type logEntryType = typeof(EditorApplication).Assembly.GetType(
                    "UnityEditor.LogEntry"
                );
                if (logEntryType == null)
                    throw new Exception(
                        "Could not find internal type UnityEditor.LogEntry during GetConsoleEntries."
                    );
                object logEntryInstance = Activator.CreateInstance(logEntryType);

                int startIndex = 0;
                int endIndexExclusive = totalEntries;
                int step = 1;

                if (useTailScan)
                {
                    endIndexExclusive = Math.Max(-1, totalEntries - maxEntriesToScan - 1);
                    startIndex = totalEntries - 1;
                    step = -1;
                }

                for (
                    int i = startIndex;
                    useTailScan ? i > endIndexExclusive : i < endIndexExclusive;
                    i += step
                )
                {
                    // Get the entry data into our instance using reflection
                    _getEntryMethod.Invoke(null, new object[] { i, logEntryInstance });

                    // Extract data using reflection
                    int mode = (int)_modeField.GetValue(logEntryInstance);
                    string message = (string)_messageField.GetValue(logEntryInstance);
                    string file = (string)_fileField.GetValue(logEntryInstance);

                    int line = (int)_lineField.GetValue(logEntryInstance);
                    // int instanceId = (int)_instanceIdField.GetValue(logEntryInstance);

                    if (string.IsNullOrEmpty(message))
                    {
                        continue; // Skip empty messages
                    }

                    // (Calibration removed)

                    // --- Filtering ---
                    // Prefer classifying severity from message/stacktrace; fallback to mode bits if needed
                    LogType unityType = InferTypeFromMessage(message);
                    bool isExplicitDebug = IsExplicitDebugLog(message);
                    if (!isExplicitDebug && unityType == LogType.Log)
                    {
                        unityType = GetLogTypeFromMode(mode);
                    }

                    bool want;
                    // Treat Exception/Assert as errors for filtering convenience
                    if (unityType == LogType.Exception)
                    {
                        want = types.Contains("error") || types.Contains("exception");
                    }
                    else if (unityType == LogType.Assert)
                    {
                        want = types.Contains("error") || types.Contains("assert");
                    }
                    else
                    {
                        want = types.Contains(unityType.ToString().ToLowerInvariant());
                    }

                    if (!want) continue;

                    if (unityType == LogType.Log) sawInfoEntry = true;

                    // Filter by text (case-insensitive) and optional regex
                    if (!filter.Matches(message))
                    {
                        continue;
                    }

                    // (Timestamp filtering is served from ConsoleLogBuffer; LogEntry has no timestamp.)

                    object formattedEntry = FormatEntry(
                        unityType,
                        message,
                        file,
                        line,
                        format,
                        includeStacktrace,
                        maxMessageChars
                    );

                    totalMatches++;

                    if (usePaging)
                    {
                        if (totalMatches > resolvedCursor && totalMatches <= pageEndExclusive)
                        {
                            formattedEntries.Add(formattedEntry);
                            retrievedCount++;
                        }
                        // Early exit: we've filled the page and only need to check if more exist
                        else if (totalMatches > pageEndExclusive)
                        {
                            // We've passed the page; totalMatches now indicates truncation
                            break;
                        }
                    }
                    else
                    {
                        formattedEntries.Add(formattedEntry);
                        retrievedCount++;

                        // Apply count limit (after filtering)
                        if (count.HasValue && retrievedCount >= count.Value)
                        {
                            break;
                        }
                    }
                }

                if (useTailScan)
                {
                    formattedEntries.Reverse();
                }
            }
            catch (Exception e)
            {
                McpLog.Error($"[ReadConsole] Error while retrieving log entries: {e}");
                // EndGettingEntries will be called in the finally block
                return new ErrorResponse($"Error retrieving log entries: {e.Message}");
            }
            finally
            {
                // Ensure we always call EndGettingEntries
                try
                {
                    _endGettingEntriesMethod.Invoke(null, null);
                }
                catch (Exception e)
                {
                    McpLog.Error($"[ReadConsole] Failed to call EndGettingEntries: {e}");
                    // Don't return error here as we might have valid data, but log it.
                }
            }

            int supplementedFromBuffer = 0;
            if (
                types.Contains("log")
                && !sawInfoEntry
                && ConsoleLogBuffer.Count > 0
            )
            {
                // Unity's LogEntries view follows the Console window's severity toggles, so an
                // editor with "Info" switched off reports no Log entries at all. The timestamped
                // mirror is filter-independent and carries the authoritative LogType.
                int budget = usePaging
                    ? resolvedPageSize
                    : Math.Max(0, (count ?? DefaultTailScanEntries) - retrievedCount);
                foreach (
                    ConsoleLogBuffer.Entry entry in ConsoleLogBuffer.CollectSince(DateTime.MinValue)
                )
                {
                    if (supplementedFromBuffer >= budget) break;
                    if (entry.type != LogType.Log) continue;
                    string candidateMessage = string.IsNullOrEmpty(entry.stackTrace)
                        ? entry.condition
                        : $"{entry.condition}\n{entry.stackTrace}";
                    if (!filter.Matches(candidateMessage))
                    {
                        continue;
                    }
                    formattedEntries.Add(FormatEntry(
                        entry.type, candidateMessage, null, 0, format, includeStacktrace, maxMessageChars));
                    supplementedFromBuffer++;
                    totalMatches++;
                }
            }

            if (filter.TimedOut)
            {
                return new ErrorResponse(
                    "'regex' matching timed out after the fixed 1s per-match guard; the pattern is probably catastrophic backtracking. Narrow the pattern."
                );
            }

            if (usePaging)
            {
                bool truncated = totalMatches > pageEndExclusive;
                string nextCursor = truncated ? pageEndExclusive.ToString() : null;
                var payload = new
                {
                    cursor = resolvedCursor,
                    pageSize = resolvedPageSize,
                    nextCursor = nextCursor,
                    truncated = truncated,
                    total = totalMatches,
                    items = formattedEntries,
                    supplemented_from_console_mirror = supplementedFromBuffer == 0
                        ? (int?)null
                        : supplementedFromBuffer,
                };

                return new SuccessResponse(
                    $"Retrieved {formattedEntries.Count} log entries.",
                    payload
                );
            }

            // Return the filtered and formatted list (might be empty)
            return new SuccessResponse(
                supplementedFromBuffer > 0
                    ? $"Retrieved {formattedEntries.Count} log entries ({supplementedFromBuffer} came from the timestamped console mirror because Unity's console filter hid them)."
                    : $"Retrieved {formattedEntries.Count} log entries.",
                formattedEntries
            );
        }

        // --- Internal Helpers ---

        /// <summary>
        /// Combines the legacy substring filter with the optional regex filter. Both must match.
        /// Patterns are capped in length and bounded by a per-match timeout so a pathological
        /// expression cannot freeze the editor thread.
        /// </summary>
        private sealed class TextFilter
        {
            private const int MaxPatternChars = 200;
            private static readonly TimeSpan MatchTimeout = TimeSpan.FromSeconds(1);

            private readonly string _text;
            private readonly Regex _regex;

            internal bool TimedOut { get; private set; }

            internal TextFilter(string text, string pattern)
            {
                _text = text;
                if (string.IsNullOrEmpty(pattern)) return;
                if (pattern.Length > MaxPatternChars)
                {
                    throw new ArgumentException(
                        $"must be at most {MaxPatternChars} characters (got {pattern.Length})."
                    );
                }
                _regex = new Regex(
                    pattern,
                    RegexOptions.IgnoreCase | RegexOptions.CultureInvariant,
                    MatchTimeout
                );
            }

            internal bool Matches(string message)
            {
                if (string.IsNullOrEmpty(message)) return false;
                if (
                    !string.IsNullOrEmpty(_text)
                    && message.IndexOf(_text, StringComparison.OrdinalIgnoreCase) < 0
                )
                {
                    return false;
                }
                if (_regex == null) return true;
                try
                {
                    return _regex.IsMatch(message);
                }
                catch (RegexMatchTimeoutException)
                {
                    TimedOut = true;
                    return false;
                }
            }
        }

        /// <summary>
        /// Accepts ISO-8601 (offsets honoured, naive values read as UTC) or unix seconds/milliseconds.
        /// </summary>
        private static bool TryParseTimestamp(string raw, out DateTime utc)
        {
            utc = default;
            if (string.IsNullOrWhiteSpace(raw)) return false;
            raw = raw.Trim();

            if (long.TryParse(raw, out long numeric))
            {
                // Values above ~2.5e10 can only be milliseconds at realistic dates.
                if (numeric > 25000000000L)
                {
                    if (numeric > DateTime.MaxValue.Ticks / TimeSpan.TicksPerMillisecond) return false;
                    utc = new DateTime(numeric * TimeSpan.TicksPerMillisecond, DateTimeKind.Utc);
                    return true;
                }
                if (numeric > DateTime.MaxValue.Ticks / TimeSpan.TicksPerSecond) return false;
                utc = new DateTime(numeric * TimeSpan.TicksPerSecond, DateTimeKind.Utc);
                return true;
            }

            if (DateTimeOffset.TryParse(
                    raw,
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.AssumeUniversal
                        | System.Globalization.DateTimeStyles.AdjustToUniversal,
                    out DateTimeOffset offset))
            {
                utc = offset.UtcDateTime;
                return true;
            }
            if (DateTime.TryParse(
                    raw,
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.AssumeUniversal
                        | System.Globalization.DateTimeStyles.AdjustToUniversal,
                    out DateTime parsed))
            {
                utc = DateTime.SpecifyKind(parsed, DateTimeKind.Utc);
                return true;
            }
            return false;
        }

        /// <summary>
        /// Serves time-filtered entries from <see cref="ConsoleLogBuffer"/> because Unity's
        /// LogEntry struct has no timestamp. Types come from the log callback itself, so no
        /// message-content guessing is involved on this path.
        /// </summary>
        private static object GetBufferedEntries(
            DateTime sinceUtc,
            List<string> types,
            int? count,
            int? pageSize,
            int? cursor,
            TextFilter filter,
            string format,
            bool includeStacktrace,
            int maxMessageChars
        )
        {
            List<ConsoleLogBuffer.Entry> buffered = ConsoleLogBuffer.CollectSince(sinceUtc);
            bool usePaging = pageSize.HasValue && pageSize.Value > 0;
            int resolvedPageSize = usePaging ? Math.Clamp(pageSize.Value, 1, 500) : 0;
            int resolvedCursor = usePaging ? Math.Max(0, cursor ?? 0) : 0;
            int limit = usePaging
                ? resolvedCursor + resolvedPageSize + 1
                : Math.Min(count ?? DefaultTailScanEntries, DefaultTailScanEntries);

            var items = new List<object>();
            int totalMatches = 0;
            foreach (ConsoleLogBuffer.Entry entry in buffered)
            {
                string token = entry.type.ToString().ToLowerInvariant();
                bool want;
                if (entry.type == LogType.Exception)
                    want = types.Contains("error") || types.Contains("exception");
                else if (entry.type == LogType.Assert)
                    want = types.Contains("error") || types.Contains("assert");
                else
                    want = types.Contains(token);
                if (!want) continue;

                string message = string.IsNullOrEmpty(entry.stackTrace)
                    ? entry.condition
                    : $"{entry.condition}\n{entry.stackTrace}";
                if (!filter.Matches(message)) continue;

                totalMatches++;
                if (totalMatches > limit) break;
                if (usePaging && totalMatches <= resolvedCursor) continue;

                items.Add(FormatEntry(
                    entry.type, message, null, 0, format, includeStacktrace, maxMessageChars));
            }

            if (filter.TimedOut)
            {
                return new ErrorResponse(
                    "'regex' matching timed out after the fixed 1s per-match guard; the pattern is probably catastrophic backtracking. Narrow the pattern."
                );
            }

            DateTime? coverageStart = ConsoleLogBuffer.CoverageStartUtc;
            var payload = new
            {
                items,
                cursor = usePaging ? resolvedCursor : (int?)null,
                pageSize = usePaging ? (int?)resolvedPageSize : null,
                nextCursor = usePaging && totalMatches > resolvedCursor + resolvedPageSize
                    ? (resolvedCursor + resolvedPageSize).ToString()
                    : null,
                truncated = usePaging
                    ? totalMatches > resolvedCursor + resolvedPageSize
                    : totalMatches >= limit,
                total = totalMatches,
                source = "console_log_buffer",
                since_utc = sinceUtc.ToString("O"),
                coverage_start_utc = coverageStart?.ToString("O"),
                possibly_incomplete = ConsoleLogBuffer.IsFull
                    && coverageStart.HasValue
                    && coverageStart.Value > sinceUtc,
                warning = ConsoleLogBuffer.Count == 0
                    ? "No entries recorded yet: the timestamped console mirror only sees logs emitted after the editor loaded the MCP package. Restart-independent history is unavailable."
                    : null,
            };
            return new SuccessResponse(
                $"Retrieved {items.Count} log entries (since {sinceUtc:O}).",
                payload
            );
        }

        private static object FormatEntry(
            LogType unityType,
            string message,
            string file,
            int line,
            string format,
            bool includeStacktrace,
            int maxMessageChars
        )
        {
            string stackTrace = includeStacktrace ? ExtractStackTrace(message) : null;
            string[] messageLines = message.Split(
                new[] { '\n', '\r' },
                StringSplitOptions.RemoveEmptyEntries
            );
            string messageOnly = messageLines.Length > 0 ? messageLines[0] : message;
            messageOnly = Truncate(messageOnly, maxMessageChars);

            if (!includeStacktrace)
            {
                stackTrace = null;
            }
            else
            {
                stackTrace = Truncate(stackTrace, maxMessageChars);
            }

            switch (format)
            {
                case "plain":
                    return messageOnly;
                case "json":
                case "detailed":
                default:
                    return new
                    {
                        type = unityType.ToString(),
                        message = messageOnly,
                        file = file,
                        line = line,
                        stackTrace = stackTrace,
                    };
            }
        }

        private static string Truncate(string value, int maxChars)
        {
            if (string.IsNullOrEmpty(value) || maxChars <= 0 || value.Length <= maxChars)
            {
                return value;
            }

            return value.Substring(0, maxChars) + $"... [truncated {value.Length - maxChars} chars]";
        }

        // Mapping bits from LogEntry.mode. These may vary by Unity version.
        private const int ModeBitError = 1 << 0;
        private const int ModeBitAssert = 1 << 1;
        private const int ModeBitWarning = 1 << 2;
        private const int ModeBitLog = 1 << 3;
        private const int ModeBitException = 1 << 4; // often combined with Error bits
        private const int ModeBitScriptingError = 1 << 9;
        private const int ModeBitScriptingWarning = 1 << 10;
        private const int ModeBitScriptingLog = 1 << 11;
        private const int ModeBitScriptingException = 1 << 18;
        private const int ModeBitScriptingAssertion = 1 << 22;

        private static LogType GetLogTypeFromMode(int mode)
        {
            // Preserve Unity's real type (no remapping); bits may vary by version
            if ((mode & (ModeBitException | ModeBitScriptingException)) != 0) return LogType.Exception;
            if ((mode & (ModeBitError | ModeBitScriptingError)) != 0) return LogType.Error;
            if ((mode & (ModeBitAssert | ModeBitScriptingAssertion)) != 0) return LogType.Assert;
            if ((mode & (ModeBitWarning | ModeBitScriptingWarning)) != 0) return LogType.Warning;
            return LogType.Log;
        }

        // (Calibration helpers removed)

        /// <summary>
        /// Classifies severity using message/stacktrace content. Works across Unity versions.
        /// </summary>
        private static LogType InferTypeFromMessage(string fullMessage)
        {
            if (string.IsNullOrEmpty(fullMessage)) return LogType.Log;

            // Fast path: look for explicit Debug API names in the appended stack trace
            // e.g., "UnityEngine.Debug:LogError (object)" or "LogWarning"
            if (fullMessage.IndexOf("LogError", StringComparison.OrdinalIgnoreCase) >= 0)
                return LogType.Error;
            if (fullMessage.IndexOf("LogWarning", StringComparison.OrdinalIgnoreCase) >= 0)
                return LogType.Warning;

            // Compiler diagnostics (C#): "warning CSxxxx" / "error CSxxxx"
            if (fullMessage.IndexOf(" warning CS", StringComparison.OrdinalIgnoreCase) >= 0
                || fullMessage.IndexOf(": warning CS", StringComparison.OrdinalIgnoreCase) >= 0)
                return LogType.Warning;
            if (fullMessage.IndexOf(" error CS", StringComparison.OrdinalIgnoreCase) >= 0
                || fullMessage.IndexOf(": error CS", StringComparison.OrdinalIgnoreCase) >= 0)
                return LogType.Error;

            // Loose keywords must be matched on the message body only. Stack traces routinely
            // contain "Exception" (e.g. ExceptionDispatchInfo.Throw), which used to reclassify
            // plain Debug.Log entries as exceptions and hide them from types=["log"].
            string body = GetMessageBody(fullMessage);

            // Exceptions (avoid misclassifying compiler diagnostics)
            if (body.IndexOf("Exception", StringComparison.OrdinalIgnoreCase) >= 0)
                return LogType.Exception;

            // Unity assertions
            if (body.IndexOf("Assertion", StringComparison.OrdinalIgnoreCase) >= 0)
                return LogType.Assert;

            return LogType.Log;
        }

        /// <summary>
        /// Returns the message without its appended stack trace, so that keyword-based
        /// severity guessing is not driven by frames that merely mention Exception/Assert.
        /// </summary>
        private static string GetMessageBody(string fullMessage)
        {
            if (string.IsNullOrEmpty(fullMessage)) return fullMessage;
            string stack = ExtractStackTrace(fullMessage);
            if (string.IsNullOrEmpty(stack)) return fullMessage;
            int index = fullMessage.IndexOf(stack, StringComparison.Ordinal);
            return index > 0 ? fullMessage.Substring(0, index) : fullMessage;
        }

        private static bool IsExplicitDebugLog(string fullMessage)
        {
            if (string.IsNullOrEmpty(fullMessage)) return false;
            if (fullMessage.IndexOf("Debug:Log (", StringComparison.OrdinalIgnoreCase) >= 0) return true;
            if (fullMessage.IndexOf("UnityEngine.Debug:Log (", StringComparison.OrdinalIgnoreCase) >= 0) return true;
            return false;
        }

        /// <summary>
        /// Applies the "one level lower" remapping for filtering, like the old version.
        /// This ensures compatibility with the filtering logic that expects remapped types.
        /// </summary>
        private static LogType GetRemappedTypeForFiltering(LogType unityType)
        {
            switch (unityType)
            {
                case LogType.Error:
                    return LogType.Warning; // Error becomes Warning
                case LogType.Warning:
                    return LogType.Log; // Warning becomes Log
                case LogType.Assert:
                    return LogType.Assert; // Assert remains Assert
                case LogType.Log:
                    return LogType.Log; // Log remains Log
                case LogType.Exception:
                    return LogType.Warning; // Exception becomes Warning
                default:
                    return LogType.Log; // Default fallback
            }
        }

        /// <summary>
        /// Attempts to extract the stack trace part from a log message.
        /// Unity log messages often have the stack trace appended after the main message,
        /// starting on a new line and typically indented or beginning with "at ".
        /// </summary>
        /// <param name="fullMessage">The complete log message including potential stack trace.</param>
        /// <returns>The extracted stack trace string, or null if none is found.</returns>
        private static string ExtractStackTrace(string fullMessage)
        {
            if (string.IsNullOrEmpty(fullMessage))
                return null;

            // Split into lines, removing empty ones to handle different line endings gracefully.
            // Using StringSplitOptions.None might be better if empty lines matter within stack trace, but RemoveEmptyEntries is usually safer here.
            string[] lines = fullMessage.Split(
                new[] { '\r', '\n' },
                StringSplitOptions.RemoveEmptyEntries
            );

            // If there's only one line or less, there's no separate stack trace.
            if (lines.Length <= 1)
                return null;

            int stackStartIndex = -1;

            // Start checking from the second line onwards.
            for (int i = 1; i < lines.Length; ++i)
            {
                // Performance: TrimStart creates a new string. Consider using IsWhiteSpace check if performance critical.
                string trimmedLine = lines[i].TrimStart();

                // Check for common stack trace patterns.
                if (
                    trimmedLine.StartsWith("at ")
                    || trimmedLine.StartsWith("UnityEngine.")
                    || trimmedLine.StartsWith("UnityEditor.")
                    || trimmedLine.Contains("(at ")
                    || // Covers "(at Assets/..." pattern
                       // Heuristic: Check if line starts with likely namespace/class pattern (Uppercase.Something)
                    (
                        trimmedLine.Length > 0
                        && char.IsUpper(trimmedLine[0])
                        && trimmedLine.Contains('.')
                    )
                )
                {
                    stackStartIndex = i;
                    break; // Found the likely start of the stack trace
                }
            }

            // If a potential start index was found...
            if (stackStartIndex > 0)
            {
                // Join the lines from the stack start index onwards using standard newline characters.
                // This reconstructs the stack trace part of the message.
                return string.Join("\n", lines.Skip(stackStartIndex));
            }

            // No clear stack trace found based on the patterns.
            return null;
        }

        /* LogEntry.mode bits exploration (based on Unity decompilation/observation):
           May change between versions.

           Basic Types:
           kError = 1 << 0 (1)
           kAssert = 1 << 1 (2)
           kWarning = 1 << 2 (4)
           kLog = 1 << 3 (8)
           kFatal = 1 << 4 (16) - Often treated as Exception/Error

           Modifiers/Context:
           kAssetImportError = 1 << 7 (128)
           kAssetImportWarning = 1 << 8 (256)
           kScriptingError = 1 << 9 (512)
           kScriptingWarning = 1 << 10 (1024)
           kScriptingLog = 1 << 11 (2048)
           kScriptCompileError = 1 << 12 (4096)
           kScriptCompileWarning = 1 << 13 (8192)
           kStickyError = 1 << 14 (16384) - Stays visible even after Clear On Play
           kMayIgnoreLineNumber = 1 << 15 (32768)
           kReportBug = 1 << 16 (65536) - Shows the "Report Bug" button
           kDisplayPreviousErrorInStatusBar = 1 << 17 (131072)
           kScriptingException = 1 << 18 (262144)
           kDontExtractStacktrace = 1 << 19 (524288) - Hint to the console UI
           kShouldClearOnPlay = 1 << 20 (1048576) - Default behavior
           kGraphCompileError = 1 << 21 (2097152)
           kScriptingAssertion = 1 << 22 (4194304)
           kVisualScriptingError = 1 << 23 (8388608)

           Example observed values:
           Log: 2048 (ScriptingLog) or 8 (Log)
           Warning: 1028 (ScriptingWarning | Warning) or 4 (Warning)
           Error: 513 (ScriptingError | Error) or 1 (Error)
           Exception: 262161 (ScriptingException | Error | kFatal?) - Complex combination
           Assertion: 4194306 (ScriptingAssertion | Assert) or 2 (Assert)
        */
    }
}
