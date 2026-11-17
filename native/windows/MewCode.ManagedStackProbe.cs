// Read-only CLR stack snapshot of one fresh synthetic AppContainer process.
// Passive ClrMD snapshots are inherently inconsistent and are diagnostic only.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Diagnostics.Runtime;

namespace MewCode.Native.Windows
{
    internal static class ManagedStackProbe
    {
        private const int MaxThreads = 24;
        private const int MaxFrames = 32;
        private const int MaxMethodChars = 192;
        private const int WatchdogMs = 8000;
        private const string FixturePrefix = "mewcode-lpac-probe-";
        private const string PackageDirectory = "clrmd-probe-1.1.142101";
        private const string SentinelName = ".mewcode-synthetic-fixture";
        private const string SentinelContent = "mewcode-synthetic-only-v1\n";
        private const uint ProcessQueryLimitedInformation = 0x1000;
        private const uint TokenQuery = 0x0008;
        private const int TokenIsAppContainer = 29;

        private static readonly ManualResetEvent WatchdogStop = new ManualResetEvent(false);
        private static readonly object OutputGate = new object();
        private static int outputWritten;

        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            StartWatchdog();
            try
            {
                int pid;
                string fixture;
                if (!TryParseArguments(args, out pid, out fixture))
                {
                    WriteReport("invalid_input", "arguments_rejected", null, null);
                    return 2;
                }

                DateTime sentinelCreatedUtc;
                string packageRoot;
                try
                {
                    packageRoot = ValidateFixture(fixture, out sentinelCreatedUtc);
                }
                catch
                {
                    WriteReport("invalid_input", "fixture_rejected", null, null);
                    return 2;
                }

                try
                {
                    return Observe(pid, fixture, packageRoot, sentinelCreatedUtc);
                }
                catch
                {
                    WriteReport("diagnostic_unavailable", "probe_failed", pid, null);
                    return 3;
                }
            }
            finally
            {
                WatchdogStop.Set();
            }
        }

        private static bool TryParseArguments(string[] args, out int pid, out string fixture)
        {
            pid = 0;
            fixture = null;
            if (args == null || args.Length != 4 ||
                !String.Equals(args[0], "--pid", StringComparison.Ordinal) ||
                !String.Equals(args[2], "--fixture", StringComparison.Ordinal) ||
                !Int32.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out pid) ||
                pid <= 0 || String.IsNullOrEmpty(args[3]))
                return false;
            fixture = args[3];
            return true;
        }

        private static string ValidateFixture(string fixture, out DateTime sentinelCreatedUtc)
        {
            sentinelCreatedUtc = DateTime.MinValue;
            string package = NormalizeAbsoluteLocalPath(AppDomain.CurrentDomain.BaseDirectory);
            package = TrimSeparators(package);
            if (!String.Equals(Path.GetFileName(package), PackageDirectory, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException();
            string scratch = Path.GetDirectoryName(package);
            if (!String.Equals(Path.GetFileName(scratch), ".tmp", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException();

            string root = TrimSeparators(NormalizeAbsoluteLocalPath(fixture));
            if (!String.Equals(root, fixture.TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(Path.GetDirectoryName(root), scratch, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException();
            string leaf = Path.GetFileName(root);
            if (!leaf.StartsWith(FixturePrefix, StringComparison.OrdinalIgnoreCase) ||
                !IsSafeSuffix(leaf.Substring(FixturePrefix.Length)))
                throw new InvalidDataException();
            CheckNoReparseAncestors(root);
            if ((File.GetAttributes(root) & FileAttributes.Directory) == 0)
                throw new InvalidDataException();

            string workspace = Path.Combine(root, "workspace");
            CheckNoReparseAncestors(workspace);
            if ((File.GetAttributes(workspace) & FileAttributes.Directory) == 0)
                throw new InvalidDataException();

            string marker = Path.Combine(root, SentinelName);
            CheckNoReparseAncestors(marker);
            FileInfo info = new FileInfo(marker);
            if (info.Length != Encoding.ASCII.GetByteCount(SentinelContent))
                throw new InvalidDataException();
            using (FileStream stream = new FileStream(marker, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                byte[] expected = Encoding.ASCII.GetBytes(SentinelContent);
                byte[] found = new byte[expected.Length + 1];
                int count = stream.Read(found, 0, found.Length);
                if (count != expected.Length)
                    throw new InvalidDataException();
                for (int i = 0; i < expected.Length; i++)
                    if (found[i] != expected[i]) throw new InvalidDataException();
            }
            sentinelCreatedUtc = info.CreationTimeUtc;
            if (sentinelCreatedUtc == DateTime.MinValue ||
                DateTime.UtcNow - sentinelCreatedUtc > TimeSpan.FromMinutes(10) ||
                sentinelCreatedUtc > DateTime.UtcNow.AddSeconds(5))
                throw new InvalidDataException();
            return package;
        }

        private static string NormalizeAbsoluteLocalPath(string raw)
        {
            if (String.IsNullOrEmpty(raw) || raw.IndexOf('\0') >= 0 || raw.Length < 3 ||
                !IsAsciiLetter(raw[0]) || raw[1] != ':' || (raw[2] != '\\' && raw[2] != '/') ||
                raw.StartsWith("\\\\?\\", StringComparison.Ordinal) ||
                raw.StartsWith("\\\\.\\", StringComparison.Ordinal) || raw.IndexOf(':', 2) >= 0)
                throw new InvalidDataException();
            string[] parts = raw.Replace('/', '\\').Split('\\');
            foreach (string part in parts)
                if (part == "..") throw new InvalidDataException();
            return Path.GetFullPath(raw);
        }

        private static void CheckNoReparseAncestors(string path)
        {
            string current = path;
            while (!String.IsNullOrEmpty(current))
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException();
                string root = Path.GetPathRoot(current);
                if (String.Equals(TrimSeparators(current), TrimSeparators(root), StringComparison.OrdinalIgnoreCase))
                    break;
                string parent = Path.GetDirectoryName(TrimSeparators(current));
                if (String.IsNullOrEmpty(parent) || String.Equals(parent, current, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException();
                current = parent;
            }
        }

        private static string TrimSeparators(string path)
        {
            string root = Path.GetPathRoot(path);
            while (path.Length > root.Length && (path[path.Length - 1] == '\\' || path[path.Length - 1] == '/'))
                path = path.Substring(0, path.Length - 1);
            return path;
        }

        private static bool IsAsciiLetter(char value)
        {
            return (value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z');
        }

        private static bool IsSafeSuffix(string suffix)
        {
            if (suffix.Length < 8 || suffix.Length > 64) return false;
            foreach (char value in suffix)
                if (!IsAsciiLetter(value) && (value < '0' || value > '9') && value != '_' && value != '-')
                    return false;
            return true;
        }

        private static int Observe(int pid, string fixture, string packageRoot, DateTime sentinelCreatedUtc)
        {
            string expectedPayload = Path.Combine(fixture, "workspace", "synthetic-payload.exe");
            string expectedPowerShell = Path.Combine(GetWindowsDirectory(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
            DateTime startedUtc;
            List<Dictionary<string, object>> observedThreads = null;
            using (Process process = Process.GetProcessById(pid))
            {
                if (process.HasExited) return Unavailable("target_exited", pid);
                string mainModule = NormalizeAbsoluteLocalPath(process.MainModule.FileName);
                if (!String.Equals(mainModule, expectedPayload, StringComparison.OrdinalIgnoreCase) &&
                    !String.Equals(mainModule, expectedPowerShell, StringComparison.OrdinalIgnoreCase))
                    return Unavailable("target_path_rejected", pid);
                if (String.Equals(mainModule, expectedPayload, StringComparison.OrdinalIgnoreCase))
                    CheckNoReparseAncestors(expectedPayload);

                startedUtc = process.StartTime.ToUniversalTime();
                DateTime now = DateTime.UtcNow;
                if (startedUtc < sentinelCreatedUtc.AddSeconds(-5) ||
                    startedUtc > now.AddSeconds(5) || now - startedUtc > TimeSpan.FromMinutes(10))
                    return Unavailable("target_not_fresh", pid);
                if (!IsAppContainer(pid)) return Unavailable("target_not_appcontainer", pid);
                if (process.HasExited) return Unavailable("target_exited", pid);

                string dacPath = Path.Combine(GetWindowsDirectory(), "Microsoft.NET", "Framework64", "v4.0.30319", "mscordacwks.dll");
                if (!File.Exists(dacPath)) return Unavailable("local_dac_unavailable", pid);
                using (DataTarget target = DataTarget.AttachToProcess(pid, 3000, AttachFlag.Passive))
                {
                    // Keep symbol lookup local. Explicit DAC path below forbids symbol-server fallback.
                    target.SymbolLocator.SymbolPath = packageRoot;
                    if (target.PointerSize != IntPtr.Size || target.ClrVersions.Count != 1 ||
                        target.ClrVersions[0].Flavor != ClrFlavor.Desktop)
                        return Unavailable("unsupported_runtime", pid);
                    ClrRuntime runtime = target.ClrVersions[0].CreateRuntime(dacPath, false);
                    if (runtime == null) return Unavailable("runtime_unavailable", pid);

                    List<Dictionary<string, object>> threads = new List<Dictionary<string, object>>();
                    bool sawMethod = false;
                    foreach (ClrThread thread in runtime.Threads)
                    {
                        if (threads.Count >= MaxThreads) break;
                        List<string> methods = new List<string>();
                        int framesSeen = 0;
                        foreach (ClrStackFrame frame in thread.EnumerateStackTrace())
                        {
                            if (framesSeen++ >= MaxFrames) break;
                            ClrMethod method = frame.Method;
                            if (method == null || String.IsNullOrEmpty(method.Name)) continue;
                            string typeName = method.Type == null ? String.Empty : method.Type.Name;
                            methods.Add(CleanMethodName(typeName + "." + method.Name));
                            sawMethod = true;
                        }
                        threads.Add(new Dictionary<string, object> {
                            { "thread_id", thread.OSThreadId },
                            { "is_alive", thread.IsAlive },
                            { "is_background", thread.IsBackground },
                            { "is_finalizer", thread.IsFinalizer },
                            { "methods", methods },
                        });
                    }
                    if (process.HasExited || process.StartTime.ToUniversalTime() != startedUtc)
                        return Unavailable("target_changed", pid);
                    if (threads.Count == 0 || !sawMethod)
                        return Unavailable("managed_stacks_unavailable", pid);
                    observedThreads = threads;
                }
                if (process.HasExited || process.StartTime.ToUniversalTime() != startedUtc)
                    return Unavailable("target_changed", pid);
            }
            WriteReport("observed", "passive_snapshot_diagnostic_only", pid, observedThreads);
            return 0;
        }

        private static string CleanMethodName(string method)
        {
            StringBuilder clean = new StringBuilder(Math.Min(method.Length, MaxMethodChars));
            for (int i = 0; i < method.Length && clean.Length < MaxMethodChars; i++)
            {
                char value = method[i];
                clean.Append(Char.IsControl(value) ? '_' : value);
            }
            return clean.ToString();
        }

        private static bool IsAppContainer(int pid)
        {
            IntPtr process = OpenProcess(ProcessQueryLimitedInformation, false, pid);
            if (process == IntPtr.Zero) return false;
            try
            {
                IntPtr token;
                if (!OpenProcessToken(process, TokenQuery, out token)) return false;
                try
                {
                    int value;
                    int returned;
                    return GetTokenInformation(token, TokenIsAppContainer, out value, sizeof(int), out returned) &&
                        returned == sizeof(int) && value == 1;
                }
                finally { CloseHandle(token); }
            }
            finally { CloseHandle(process); }
        }

        private static string GetWindowsDirectory()
        {
            StringBuilder buffer = new StringBuilder(32768);
            uint length = GetWindowsDirectoryW(buffer, (uint)buffer.Capacity);
            if (length == 0 || length >= buffer.Capacity) throw new InvalidDataException();
            return NormalizeAbsoluteLocalPath(buffer.ToString());
        }

        private static int Unavailable(string reason, int pid)
        {
            WriteReport("diagnostic_unavailable", reason, pid, null);
            return 3;
        }

        private static void WriteReport(string status, string reason, int? pid, List<Dictionary<string, object>> threads)
        {
            lock (OutputGate)
            {
                if (Interlocked.Exchange(ref outputWritten, 1) != 0) return;
                Dictionary<string, object> report = new Dictionary<string, object> {
                    { "schema", "mewcode-managed-stack-diagnostic-v1" },
                    { "status", status },
                    { "reason", reason },
                    { "diagnostic_only", true },
                    { "pid", pid },
                    { "threads", threads ?? new List<Dictionary<string, object>>() },
                };
                Console.Out.WriteLine(new JavaScriptSerializer().Serialize(report));
                Console.Out.Flush();
            }
        }

        private static void StartWatchdog()
        {
            Thread watchdog = new Thread(delegate()
            {
                if (WatchdogStop.WaitOne(WatchdogMs)) return;
                if (Monitor.TryEnter(OutputGate, 100))
                {
                    try
                    {
                        if (outputWritten == 0)
                            WriteReport("diagnostic_unavailable", "watchdog_timeout", null, null);
                    }
                    catch { }
                    finally { Monitor.Exit(OutputGate); }
                }
                // This ends only the observer, never the target process.
                ExitProcess(4);
            });
            watchdog.IsBackground = true;
            watchdog.Start();
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, int processId);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(IntPtr processHandle, uint desiredAccess, out IntPtr tokenHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetTokenInformation(IntPtr tokenHandle, int infoClass, out int value, int valueLength, out int returnedLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern uint GetWindowsDirectoryW(StringBuilder buffer, uint size);

        [DllImport("kernel32.dll")]
        private static extern void ExitProcess(uint exitCode);
    }
}
