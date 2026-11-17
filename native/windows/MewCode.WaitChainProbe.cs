// Read-only, bounded Wait Chain Traversal probe for one explicitly named PID.
// Compile with .NET Framework csc.exe; requires no third-party assemblies.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace MewCode.Native.Windows
{
    internal static class WaitChainProbe
    {
        private const int MaxThreads = 8;
        private const int MaxNodes = 16;
        private const int PerThreadWaitMs = 2000;
        private const int TotalBudgetMs = 18500;
        private const int WatchdogMs = 19500;
        private const uint AsyncOpenFlag = 0x1;
        private const uint ErrorIoPending = 997;
        private const uint ErrorMoreData = 234;
        private const uint ErrorTooManyThreads = 115;

        private static readonly WaitChainCallback CallbackRoot = OnWaitChain;
        private static readonly ManualResetEvent WatchdogStop = new ManualResetEvent(false);
        private static readonly Query[] Queries = new Query[MaxThreads];
        private static long startedAt;
        private static int outputWritten;
        private static int targetPid;
        private static string expectedProcessName;
        private static string actualProcessName;
        private static int closeState; // 0 not returned, 1 returned, -1 threw
        private static string closeError;
        private static IntPtr session;

        private sealed class Query
        {
            internal readonly int ThreadId;
            internal readonly ManualResetEvent Completed = new ManualResetEvent(false);
            internal readonly IntPtr CountBuffer;
            internal readonly IntPtr NodesBuffer;
            internal readonly IntPtr CycleBuffer;
            internal GCHandle ContextHandle;
            internal ThreadReport Result;
            internal int Submitted;

            internal Query(int threadId) : this(threadId, true)
            {
            }

            internal Query(int threadId, bool allocateBuffers)
            {
                ThreadId = threadId;
                if (!allocateBuffers) return;
                CountBuffer = Marshal.AllocHGlobal(sizeof(int));
                NodesBuffer = Marshal.AllocHGlobal(WaitChainNodeInfo.NativeSize * MaxNodes);
                CycleBuffer = Marshal.AllocHGlobal(sizeof(int));
                Marshal.WriteInt32(CountBuffer, MaxNodes);
                Marshal.WriteInt32(CycleBuffer, 0);
                ContextHandle = GCHandle.Alloc(this, GCHandleType.Normal);
            }
        }

        private sealed class ThreadReport
        {
            internal int ThreadId;
            internal string Status;
            internal bool TimedOut;
            internal long ElapsedMs;
            internal int? CallbackStatus;
            internal int? ErrorCode;
            internal string Error;
            internal bool? Deadlocked;
            internal List<NodeReport> Nodes;
        }

        private sealed class NodeReport
        {
            internal uint ObjectType;
            internal string ObjectTypeName;
            internal uint ObjectStatus;
            internal string ObjectStatusName;
            internal string ObjectName;
            internal uint? ProcessId;
            internal uint? ThreadId;
            internal uint? WaitTime;
            internal uint? ContextSwitches;
        }

        [StructLayout(LayoutKind.Explicit, Size = 280)]
        private struct WaitChainNodeInfo
        {
            internal const int NativeSize = 280;

            [FieldOffset(0)] internal uint ObjectType;
            [FieldOffset(4)] internal uint ObjectStatus;
            [FieldOffset(8)] internal uint ThreadProcessId;
            [FieldOffset(12)] internal uint ThreadId;
            [FieldOffset(16)] internal uint WaitTime;
            [FieldOffset(20)] internal uint ContextSwitches;
        }

        [UnmanagedFunctionPointer(CallingConvention.Winapi)]
        private delegate void WaitChainCallback(
            IntPtr wctHandle,
            UIntPtr context,
            uint callbackStatus,
            IntPtr nodeCount,
            IntPtr nodeInfoArray,
            IntPtr isCycle);

        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            int pid;
            string expected;
            string parseError;
            if (!TryParseArguments(args, out pid, out expected, out parseError))
            {
                WriteError("invalid_arguments", parseError, null, null, 2);
                return 2;
            }

            targetPid = pid;
            expectedProcessName = expected;
            startedAt = Stopwatch.GetTimestamp();
            StartWatchdog();

            try
            {
                return RunProbe();
            }
            catch (Exception ex)
            {
                bool sessionClosed = session == IntPtr.Zero;
                if (!sessionClosed)
                {
                    try { sessionClosed = CloseSessionWithinBudget(); }
                    catch { sessionClosed = false; }
                }
                WriteError("probe_error", ex.GetType().Name + ": " + ex.Message,
                    targetPid, expectedProcessName, 3);
                if (!sessionClosed)
                {
                    Console.Out.Flush();
                    Native.ExitProcess(3);
                }
                return 3;
            }
            finally
            {
                WatchdogStop.Set();
            }
        }

        private static bool TryParseArguments(string[] args, out int pid, out string expected, out string error)
        {
            pid = 0;
            expected = "powershell";
            error = null;
            bool sawPid = false;
            bool sawName = false;

            if (args == null || args.Length == 0)
            {
                error = "Usage: MewCode.WaitChainProbe.exe --pid <positive-integer> [--expected-process-name <name>]";
                return false;
            }

            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i];
                if (String.Equals(arg, "--pid", StringComparison.Ordinal))
                {
                    if (sawPid || i + 1 >= args.Length ||
                        !Int32.TryParse(args[++i], NumberStyles.None, CultureInfo.InvariantCulture, out pid) || pid <= 0)
                    {
                        error = "--pid must appear once and be followed by a positive decimal process ID.";
                        return false;
                    }
                    sawPid = true;
                }
                else if (String.Equals(arg, "--expected-process-name", StringComparison.Ordinal))
                {
                    if (sawName || i + 1 >= args.Length)
                    {
                        error = "--expected-process-name must appear at most once and have a value.";
                        return false;
                    }
                    expected = NormalizeProcessName(args[++i]);
                    if (!IsSafeProcessName(expected))
                    {
                        error = "--expected-process-name must be a simple executable name.";
                        return false;
                    }
                    sawName = true;
                }
                else
                {
                    error = "Unknown argument: " + arg;
                    return false;
                }
            }

            if (!sawPid)
            {
                error = "--pid is required.";
                return false;
            }
            return true;
        }

        private static string NormalizeProcessName(string value)
        {
            if (value == null) return String.Empty;
            string name = value.Trim();
            if (name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
                name = name.Substring(0, name.Length - 4);
            return name;
        }

        private static bool IsSafeProcessName(string value)
        {
            if (String.IsNullOrWhiteSpace(value) || value.Length > 64) return false;
            for (int i = 0; i < value.Length; i++)
            {
                char c = value[i];
                if (!(Char.IsLetterOrDigit(c) || c == '-' || c == '_' || c == '.')) return false;
            }
            return true;
        }

        private static int RunProbe()
        {
            int[] threadIds;
            try
            {
                using (Process process = Process.GetProcessById(targetPid))
                {
                    actualProcessName = process.ProcessName;
                    if (!String.Equals(actualProcessName, expectedProcessName, StringComparison.OrdinalIgnoreCase))
                    {
                        WriteError("process_name_mismatch",
                            "The PID does not identify the expected process name.",
                            targetPid, expectedProcessName, 3, actualProcessName);
                        return 3;
                    }

                    List<int> ids = new List<int>();
                    ProcessThreadCollection processThreads = process.Threads;
                    foreach (ProcessThread processThread in processThreads)
                    {
                        using (processThread)
                        {
                            ids.Add(processThread.Id);
                        }
                    }
                    ids.Sort();
                    if (ids.Count > MaxThreads) ids.RemoveRange(MaxThreads, ids.Count - MaxThreads);
                    threadIds = ids.ToArray();
                }
            }
            catch (ArgumentException)
            {
                WriteError("process_not_found", "No process exists for the requested PID.",
                    targetPid, expectedProcessName, 3);
                return 3;
            }
            catch (Exception ex)
            {
                WriteError("process_query_failed", ex.GetType().Name + ": " + ex.Message,
                    targetPid, expectedProcessName, 3);
                return 3;
            }

            try
            {
                session = Native.OpenThreadWaitChainSession(AsyncOpenFlag, CallbackRoot);
            }
            catch (Exception ex)
            {
                WriteWctOpenFailure(threadIds, null, ex.GetType().Name + ": " + ex.Message);
                return 4;
            }
            if (session == IntPtr.Zero)
            {
                int code = Marshal.GetLastWin32Error();
                WriteWctOpenFailure(threadIds, code, Win32Message(code));
                return 4;
            }

            for (int i = 0; i < threadIds.Length; i++)
            {
                Query query;
                try
                {
                    query = new Query(threadIds[i]);
                    Queries[i] = query;
                }
                catch (Exception ex)
                {
                    Query failed = new Query(threadIds[i], false);
                    Queries[i] = failed;
                    SetResult(failed, ErrorReport(threadIds[i], "buffer_allocation_failed", ex.Message, null, 0));
                    continue;
                }

                SubmitAndWait(query);
            }

            bool closeReturned = CloseSessionWithinBudget();
            WriteSuccess(threadIds.Length, closeReturned);
            if (!closeReturned)
            {
                // The session and all callback buffers remain live. Process teardown
                // ends only this diagnostic process; the requested PID is untouched.
                Console.Out.Flush();
                Native.ExitProcess(0);
                return 0;
            }
            return 0;
        }

        private static void SubmitAndWait(Query query)
        {
            int remaining = RemainingBudget(TotalBudgetMs);
            if (remaining <= 0)
            {
                SetResult(query, TimeoutReport(query.ThreadId, 0));
                return;
            }

            long queryStarted = Stopwatch.GetTimestamp();
            try
            {
                query.Submitted = 1;
                bool succeeded = Native.GetThreadWaitChain(
                    session,
                    PointerAsUIntPtr(GCHandle.ToIntPtr(query.ContextHandle)),
                    0, // Do not follow or enumerate threads in other processes.
                    unchecked((uint)query.ThreadId),
                    query.CountBuffer,
                    query.NodesBuffer,
                    query.CycleBuffer);

                int error = succeeded ? 0 : Marshal.GetLastWin32Error();
                if (succeeded)
                {
                    SetResult(query, BuildCallbackReport(query.ThreadId, 0,
                        query.CountBuffer, query.NodesBuffer, query.CycleBuffer,
                        ElapsedMilliseconds(queryStarted)));
                    query.Completed.Set();
                    return;
                }
                if (error != ErrorIoPending)
                {
                    SetResult(query, ErrorReport(query.ThreadId, "wct_query_failed",
                        Win32Message(error), error, ElapsedMilliseconds(queryStarted)));
                    query.Completed.Set();
                    return;
                }

                int waitMs = Math.Min(PerThreadWaitMs - (int)ElapsedMilliseconds(queryStarted),
                    RemainingBudget(TotalBudgetMs));
                if (waitMs > 0 && query.Completed.WaitOne(waitMs))
                {
                    return;
                }
                SetResult(query, TimeoutReport(query.ThreadId, ElapsedMilliseconds(queryStarted)));
            }
            catch (Exception ex)
            {
                SetResult(query, ErrorReport(query.ThreadId, "wct_query_exception",
                    ex.GetType().Name + ": " + ex.Message, null, ElapsedMilliseconds(queryStarted)));
            }
        }

        private static void OnWaitChain(IntPtr wctHandle, UIntPtr context, uint callbackStatus,
            IntPtr nodeCount, IntPtr nodeInfoArray, IntPtr isCycle)
        {
            Query query = null;
            try
            {
                IntPtr contextPointer = UIntPtrAsPointer(context);
                GCHandle handle = GCHandle.FromIntPtr(contextPointer);
                query = handle.Target as Query;
                if (query != null)
                {
                    ThreadReport report = BuildCallbackReport(query.ThreadId, callbackStatus,
                        nodeCount, nodeInfoArray, isCycle, 0);
                    SetResult(query, report);
                }
            }
            catch (Exception ex)
            {
                if (query != null)
                {
                    SetResult(query, ErrorReport(query.ThreadId, "callback_error",
                        ex.GetType().Name + ": " + ex.Message, unchecked((int)callbackStatus), 0));
                }
            }
            finally
            {
                if (query != null)
                {
                    try { query.Completed.Set(); }
                    catch { }
                }
            }
        }

        private static ThreadReport BuildCallbackReport(int threadId, uint callbackStatus,
            IntPtr countPointer, IntPtr nodesPointer, IntPtr cyclePointer, long elapsedMs)
        {
            ThreadReport report = new ThreadReport();
            report.ThreadId = threadId;
            report.CallbackStatus = unchecked((int)callbackStatus);
            report.ElapsedMs = elapsedMs;

            bool chainValid = callbackStatus == 0 || callbackStatus == ErrorMoreData ||
                callbackStatus == ErrorTooManyThreads;
            if (!chainValid)
            {
                report.Status = "error";
                report.ErrorCode = unchecked((int)callbackStatus);
                report.Error = Win32Message(unchecked((int)callbackStatus));
                report.Deadlocked = null;
                report.Nodes = null;
                return report;
            }

            if (countPointer == IntPtr.Zero || nodesPointer == IntPtr.Zero || cyclePointer == IntPtr.Zero)
            {
                report.Status = "error";
                report.Error = "WCT callback returned a null result pointer.";
                report.Deadlocked = null;
                report.Nodes = null;
                return report;
            }

            uint count = unchecked((uint)Marshal.ReadInt32(countPointer));
            int readable = (int)Math.Min((ulong)count, (ulong)MaxNodes);
            report.Nodes = new List<NodeReport>(readable);
            for (int i = 0; i < readable; i++)
            {
                IntPtr nodePointer = new IntPtr(nodesPointer.ToInt64() + ((long)i * WaitChainNodeInfo.NativeSize));
                WaitChainNodeInfo node = (WaitChainNodeInfo)Marshal.PtrToStructure(
                    nodePointer, typeof(WaitChainNodeInfo));
                NodeReport item = new NodeReport();
                item.ObjectType = node.ObjectType;
                item.ObjectTypeName = ObjectTypeName(node.ObjectType);
                item.ObjectStatus = node.ObjectStatus;
                item.ObjectStatusName = ObjectStatusName(node.ObjectStatus);

                if (node.ObjectType == 8) // WctThreadType
                {
                    item.ProcessId = node.ThreadProcessId;
                    item.ThreadId = node.ThreadId;
                    item.WaitTime = node.WaitTime;
                    item.ContextSwitches = node.ContextSwitches;
                }
                else
                {
                    item.ObjectName = ReadObjectName(nodePointer);
                }
                report.Nodes.Add(item);
            }

            report.Status = callbackStatus == 0 ? "completed" : "partial";
            report.Deadlocked = Marshal.ReadInt32(cyclePointer) != 0;
            if (count > MaxNodes)
                report.Error = "The wait chain exceeded WCT_MAX_NODE_COUNT; only the supported node limit was read.";
            return report;
        }

        private static bool CloseSessionWithinBudget()
        {
            if (session == IntPtr.Zero) return true;
            try
            {
                ManualResetEvent returned = new ManualResetEvent(false);
                Thread closer = new Thread(delegate()
                {
                    try
                    {
                        Native.CloseThreadWaitChainSession(session);
                        Interlocked.Exchange(ref closeState, 1);
                    }
                    catch (Exception ex)
                    {
                        closeError = ex.GetType().Name + ": " + ex.Message;
                        Interlocked.Exchange(ref closeState, -1);
                    }
                    finally
                    {
                        returned.Set();
                    }
                });
                closer.IsBackground = true;
                closer.Name = "WCT session close";
                closer.Start();

                int remaining = RemainingBudget(TotalBudgetMs);
                int closeWait = Math.Min(1000, Math.Max(0, remaining));
                return closeWait > 0 && returned.WaitOne(closeWait) &&
                    Interlocked.CompareExchange(ref closeState, 0, 0) == 1;
            }
            catch (Exception ex)
            {
                closeError = ex.GetType().Name + ": " + ex.Message;
                return false;
            }
        }

        private static ThreadReport TimeoutReport(int threadId, long elapsedMs)
        {
            ThreadReport report = new ThreadReport();
            report.ThreadId = threadId;
            report.Status = "timeout";
            report.TimedOut = true;
            report.ElapsedMs = elapsedMs;
            report.Deadlocked = null;
            report.Nodes = null;
            report.Error = "No callback arrived within the bounded wait; the wait chain is unknown.";
            return report;
        }

        private static ThreadReport ErrorReport(int threadId, string status, string error,
            int? errorCode, long elapsedMs)
        {
            ThreadReport report = new ThreadReport();
            report.ThreadId = threadId;
            report.Status = "error";
            report.ElapsedMs = elapsedMs;
            report.ErrorCode = errorCode;
            report.Error = status + ": " + error;
            report.Deadlocked = null;
            report.Nodes = null;
            return report;
        }

        private static void SetResult(Query query, ThreadReport report)
        {
            Interlocked.CompareExchange(ref query.Result, report, null);
        }

        private static void StartWatchdog()
        {
            Thread watchdog = new Thread(delegate()
            {
                if (!WatchdogStop.WaitOne(WatchdogMs))
                {
                    WriteSuccess(0, false, true);
                    Console.Out.Flush();
                    Native.ExitProcess(124);
                }
            });
            watchdog.IsBackground = true;
            watchdog.Name = "WCT probe deadline";
            watchdog.Start();
        }

        private static int RemainingBudget(int limitMs)
        {
            long elapsed = ElapsedMilliseconds(startedAt);
            return (int)Math.Max(0, limitMs - elapsed);
        }

        private static long ElapsedMilliseconds(long start)
        {
            if (start == 0) return 0;
            return (long)((Stopwatch.GetTimestamp() - start) * 1000.0 / Stopwatch.Frequency);
        }

        private static UIntPtr PointerAsUIntPtr(IntPtr pointer)
        {
            if (IntPtr.Size == 4) return new UIntPtr(unchecked((uint)pointer.ToInt32()));
            return new UIntPtr(unchecked((ulong)pointer.ToInt64()));
        }

        private static IntPtr UIntPtrAsPointer(UIntPtr pointer)
        {
            if (IntPtr.Size == 4) return new IntPtr(unchecked((int)pointer.ToUInt32()));
            return new IntPtr(unchecked((long)pointer.ToUInt64()));
        }

        private static void WriteSuccess(int selectedCount, bool closeReturned, bool watchdog = false)
        {
            if (Interlocked.CompareExchange(ref outputWritten, 1, 0) != 0) return;
            StringBuilder json = new StringBuilder(4096);
            json.Append("{\"ok\":true,\"pid\":").Append(targetPid.ToString(CultureInfo.InvariantCulture));
            json.Append(",\"processName\":").Append(JsonString(actualProcessName));
            json.Append(",\"expectedProcessName\":").Append(JsonString(expectedProcessName));
            json.Append(",\"threadLimit\":").Append(MaxThreads);
            json.Append(",\"selectedThreadCount\":").Append(selectedCount);
            json.Append(",\"durationMs\":").Append(ElapsedMilliseconds(startedAt));
            json.Append(",\"sessionCloseCompleted\":").Append(closeReturned ? "true" : "false");
            json.Append(",\"globalTimeout\":").Append(watchdog ? "true" : "false");
            if (closeError != null) json.Append(",\"sessionCloseError\":").Append(JsonString(closeError));
            json.Append(",\"threads\":[");
            bool first = true;
            for (int i = 0; i < Queries.Length; i++)
            {
                Query query = Queries[i];
                if (query == null) continue;
                if (!first) json.Append(',');
                first = false;
                ThreadReport report = Interlocked.CompareExchange(ref query.Result, null, null);
                if (report == null)
                    report = TimeoutReport(query.ThreadId, PerThreadWaitMs);
                AppendThreadReport(json, report);
            }
            json.Append("]}");
            Console.WriteLine(json.ToString());
        }

        private static void AppendThreadReport(StringBuilder json, ThreadReport report)
        {
            json.Append("{\"threadId\":").Append(report.ThreadId.ToString(CultureInfo.InvariantCulture));
            json.Append(",\"status\":").Append(JsonString(report.Status));
            json.Append(",\"timedOut\":").Append(report.TimedOut ? "true" : "false");
            json.Append(",\"elapsedMs\":").Append(report.ElapsedMs.ToString(CultureInfo.InvariantCulture));
            json.Append(",\"callbackStatus\":").Append(NullableInt(report.CallbackStatus));
            json.Append(",\"errorCode\":").Append(NullableInt(report.ErrorCode));
            json.Append(",\"error\":").Append(JsonString(report.Error));
            json.Append(",\"deadlocked\":").Append(NullableBool(report.Deadlocked));
            json.Append(",\"nodes\":");
            if (report.Nodes == null)
            {
                json.Append("null");
            }
            else
            {
                json.Append('[');
                for (int i = 0; i < report.Nodes.Count; i++)
                {
                    if (i != 0) json.Append(',');
                    NodeReport node = report.Nodes[i];
                    json.Append("{\"objectType\":").Append(node.ObjectType);
                    json.Append(",\"objectTypeName\":").Append(JsonString(node.ObjectTypeName));
                    json.Append(",\"objectStatus\":").Append(node.ObjectStatus);
                    json.Append(",\"objectStatusName\":").Append(JsonString(node.ObjectStatusName));
                    json.Append(",\"objectName\":").Append(JsonString(node.ObjectName));
                    json.Append(",\"processId\":").Append(NullableUInt(node.ProcessId));
                    json.Append(",\"threadId\":").Append(NullableUInt(node.ThreadId));
                    json.Append(",\"waitTime\":").Append(NullableUInt(node.WaitTime));
                    json.Append(",\"contextSwitches\":").Append(NullableUInt(node.ContextSwitches));
                    json.Append('}');
                }
                json.Append(']');
            }
            json.Append('}');
        }

        private static void WriteError(string code, string message, int? pid, string expected,
            int exitCode, string actual = null, int? win32Code = null)
        {
            if (Interlocked.CompareExchange(ref outputWritten, 1, 0) != 0) return;
            StringBuilder json = new StringBuilder();
            json.Append("{\"ok\":false,\"error\":{\"code\":").Append(JsonString(code));
            json.Append(",\"message\":").Append(JsonString(message));
            json.Append(",\"win32Code\":").Append(NullableInt(win32Code)).Append("},\"pid\":");
            json.Append(NullableInt(pid));
            json.Append(",\"expectedProcessName\":").Append(JsonString(expected));
            json.Append(",\"processName\":").Append(JsonString(actual));
            json.Append('}');
            Console.WriteLine(json.ToString());
        }

        private static string NullableInt(int? value)
        {
            return value.HasValue ? value.Value.ToString(CultureInfo.InvariantCulture) : "null";
        }

        private static string NullableUInt(uint? value)
        {
            return value.HasValue ? value.Value.ToString(CultureInfo.InvariantCulture) : "null";
        }

        private static string NullableBool(bool? value)
        {
            if (!value.HasValue) return "null";
            return value.Value ? "true" : "false";
        }

        private static string JsonString(string value)
        {
            if (value == null) return "null";
            StringBuilder escaped = new StringBuilder(value.Length + 2);
            escaped.Append('"');
            for (int i = 0; i < value.Length; i++)
            {
                char c = value[i];
                switch (c)
                {
                    case '"': escaped.Append("\\\""); break;
                    case '\\': escaped.Append("\\\\"); break;
                    case '\b': escaped.Append("\\b"); break;
                    case '\f': escaped.Append("\\f"); break;
                    case '\n': escaped.Append("\\n"); break;
                    case '\r': escaped.Append("\\r"); break;
                    case '\t': escaped.Append("\\t"); break;
                    default:
                        if (c < 0x20) escaped.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else escaped.Append(c);
                        break;
                }
            }
            escaped.Append('"');
            return escaped.ToString();
        }

        private static string EmptyToNull(string value)
        {
            if (String.IsNullOrEmpty(value)) return null;
            int terminator = value.IndexOf('\0');
            if (terminator >= 0) value = value.Substring(0, terminator);
            return value.Length == 0 ? null : value;
        }

        private static string ReadObjectName(IntPtr nodePointer)
        {
            // WAITCHAIN_NODE_INFO starts its union at byte 8; ObjectName is the
            // first 128 UTF-16 characters in the lock-object union member.
            return EmptyToNull(Marshal.PtrToStringUni(new IntPtr(nodePointer.ToInt64() + 8), 128));
        }

        private static string Win32Message(int code)
        {
            try { return new Win32Exception(code).Message; }
            catch { return "Win32 error " + code.ToString(CultureInfo.InvariantCulture); }
        }

        private static string ObjectTypeName(uint value)
        {
            switch (value)
            {
                case 1: return "CriticalSection";
                case 2: return "SendMessage";
                case 3: return "Mutex";
                case 4: return "Alpc";
                case 5: return "Com";
                case 6: return "ThreadWait";
                case 7: return "ProcessWait";
                case 8: return "Thread";
                case 9: return "ComActivation";
                case 10: return "Unknown";
                case 11: return "SocketIo";
                case 12: return "SmbIo";
                default: return "Unknown(" + value.ToString(CultureInfo.InvariantCulture) + ")";
            }
        }

        private static string ObjectStatusName(uint value)
        {
            switch (value)
            {
                case 1: return "NoAccess";
                case 2: return "Running";
                case 3: return "Blocked";
                case 4: return "PidOnly";
                case 5: return "PidOnlyRpcss";
                case 6: return "Owned";
                case 7: return "NotOwned";
                case 8: return "Abandoned";
                case 9: return "Unknown";
                case 10: return "Error";
                default: return "Unknown(" + value.ToString(CultureInfo.InvariantCulture) + ")";
            }
        }

        private static void WriteError(string code, string message, int? pid,
            string expected, int exitCode)
        {
            WriteError(code, message, pid, expected, exitCode, null, null);
        }

        private static void WriteWctOpenFailure(int[] threadIds, int? code, string message)
        {
            for (int i = 0; i < threadIds.Length; i++)
            {
                Query query = new Query(threadIds[i], false);
                Queries[i] = query;
                SetResult(query, ErrorReport(threadIds[i], "wct_open_failed", message, code, 0));
            }

            if (Interlocked.CompareExchange(ref outputWritten, 1, 0) != 0) return;
            StringBuilder json = new StringBuilder();
            json.Append("{\"ok\":false,\"pid\":").Append(targetPid.ToString(CultureInfo.InvariantCulture));
            json.Append(",\"processName\":").Append(JsonString(actualProcessName));
            json.Append(",\"expectedProcessName\":").Append(JsonString(expectedProcessName));
            json.Append(",\"durationMs\":").Append(ElapsedMilliseconds(startedAt));
            json.Append(",\"error\":{\"code\":\"wct_open_failed\",\"message\":").Append(JsonString(message));
            json.Append(",\"win32Code\":").Append(NullableInt(code)).Append("},\"threads\":[");
            for (int i = 0; i < threadIds.Length; i++)
            {
                if (i != 0) json.Append(',');
                AppendThreadReport(json, Queries[i].Result);
            }
            json.Append("]}");
            Console.WriteLine(json.ToString());
        }

        private static class Native
        {
            [DllImport("Advapi32.dll", SetLastError = true)]
            internal static extern IntPtr OpenThreadWaitChainSession(
                uint flags, WaitChainCallback callback);

            [DllImport("Advapi32.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool GetThreadWaitChain(
                IntPtr wctHandle,
                UIntPtr context,
                uint flags,
                uint threadId,
                IntPtr nodeCount,
                IntPtr nodeInfoArray,
                IntPtr isCycle);

            [DllImport("Advapi32.dll")]
            internal static extern void CloseThreadWaitChainSession(IntPtr wctHandle);

            [DllImport("kernel32.dll", SetLastError = false)]
            internal static extern void ExitProcess(uint exitCode);
        }
    }
}
