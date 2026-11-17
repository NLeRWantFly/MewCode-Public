// Diagnostic-only AppContainer/LPAC launcher prototype.
// Compile with .NET Framework csc.exe; this file intentionally has no package dependencies.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using Microsoft.Win32;
using System.Web.Script.Serialization;

namespace MewCode.Native.Windows
{
    public sealed class ProbeRequest
    {
        public string fixture_root { get; set; }
        public string executable { get; set; }
        public string[] arguments { get; set; }
        public string cwd { get; set; }
        public int? timeout_ms { get; set; }
        public Dictionary<string, string> environment { get; set; }
        public string[] read_paths { get; set; }
        public string[] write_paths { get; set; }
        public string[] execute_paths { get; set; }
        public bool? lpac { get; set; }
        public bool? runtime_registry_read { get; set; }
        public bool? runtime_instrumentation { get; set; }
        public bool? runtime_com { get; set; }
        public string cancel_event_name { get; set; }
    }

    public sealed class ProbeReceipt
    {
        public int? exit_code { get; set; }
        public string stdout { get; set; }
        public string stderr { get; set; }
        public string stdout_base64 { get; set; }
        public string stderr_base64 { get; set; }
        public string error { get; set; }
        public bool timed_out { get; set; }
        public bool cancelled { get; set; }
        public bool job_cleanup_ok { get; set; }
        public bool profile_cleanup_ok { get; set; }
        public bool acl_cleanup_ok { get; set; }
        public int? pid { get; set; }
        public string profile_name { get; set; }
        public bool appcontainer_verified { get; set; }
        public bool lpac_verified { get; set; }
        public string lpac_verification_method { get; set; }
        public int? lpac_access_mask { get; set; }
        public string[] capabilities { get; set; }
        public bool stdout_truncated { get; set; }
        public bool stderr_truncated { get; set; }
        public int output_code_page { get; set; }
        [ScriptIgnore]
        public NetworkProfileReceipt network { get; set; }
    }

    public sealed class NetworkProfileReceipt
    {
        public bool runtime_network { get; set; }
        public bool persistent_profile { get; set; }
        public string profile_name { get; set; }
        public string profile_sid { get; set; }
        public string user_sid { get; set; }
    }

    internal sealed class NetworkProfileDetails
    {
        public string ProfileName;
        public string ProfileSid;
        public string UserSid;
    }

    internal sealed class DerivedNetworkProfile
    {
        public string ProfileName;
        public string ProfileSid;
        public string UserSid;
        public IntPtr PackageSid;
    }

    internal sealed class ValidatedRequest
    {
        public string FixtureRoot;
        public string Executable;
        public string Cwd;
        public string[] Arguments;
        public int TimeoutMs;
        public string CancelEventName;
        public bool Lpac;
        public bool RuntimeRegistryRead;
        public bool RuntimeInstrumentation;
        public bool RuntimeCom;
        public bool RuntimeNetwork;
        public Dictionary<string, string> Environment;
        public Dictionary<string, PathGrant> Grants;
        public bool RuntimeRequest;
        public string StagedWorkspace;
        public string[] StagedRuntimeRoots;
    }

    internal sealed class DerivedCapabilitySids
    {
        public string Name;
        public IntPtr GroupSids;
        public uint GroupSidCount;
        public IntPtr CapabilitySids;
        public uint CapabilitySidCount;
    }

    internal sealed class PathGrant
    {
        public string Path;
        public bool IsDirectory;
        public bool Read;
        public bool Write;
        public bool Execute;
        public FileSystemAccessRule Rule;
        public bool StagedMutableTree;
        public bool StagedReadOnly;
    }

    internal sealed class CaptureBuffer
    {
        public IntPtr Handle;
        public readonly MemoryStream Bytes = new MemoryStream();
        public readonly int Limit;
        public Thread Thread;
        public uint NativeThreadId;
        public long TotalBytes;
        public bool Truncated;
        public bool Complete;
        public bool Joined;
        public bool ErrorReported;
        public string Error;
        public int CodePage = 65001;

        public CaptureBuffer(IntPtr handle, int limit)
        {
            Handle = handle;
            Limit = limit;
        }
    }

    internal sealed class LaunchState
    {
        public readonly List<IntPtr> RuntimePathPins = new List<IntPtr>();
        public readonly List<PathGrant> AppliedGrants = new List<PathGrant>();
        public string ProfileName;
        public string FixtureRoot;
        public bool ProfileCreated;
        public bool RuntimeNetwork;
        public string ProfileSid;
        public string UserSid;
        public bool RuntimeRequest;
        public IntPtr PackageSid;
        public readonly List<DerivedCapabilitySids> DerivedCapabilities = new List<DerivedCapabilitySids>();
        public IntPtr CapabilityAttributesMemory;
        public uint CapabilityCount;
        public IntPtr ProcessToken;
        public IntPtr CancelEvent;
        public IntPtr Job;
        public bool JobConfigured;
        public bool JobCreated;
        public bool JobTerminateAttempted;
        public bool JobTerminateSucceeded;
        public bool JobEmptyConfirmed;
        public bool JobEmptyErrorReported;
        public Stopwatch JobEmptyTimer;
        public bool JobClosed;
        public bool ProcessCreated;
        public bool ProcessAssigned;
        public bool ProcessResumed;
        public bool RootCompleted;
        public bool UnassignedProcessCleanupOk = true;
        public IntPtr Process;
        public IntPtr PrimaryThread;
        public IntPtr StdIn;
        public IntPtr StdOutRead;
        public IntPtr StdOutWrite;
        public IntPtr StdErrRead;
        public IntPtr StdErrWrite;
        public CaptureBuffer StdOutCapture;
        public CaptureBuffer StdErrCapture;
        public IntPtr AttributeList;
        public bool AttributeListInitialized;
        public IntPtr SecurityCapabilitiesMemory;
        public IntPtr LpacPolicyMemory;
        public IntPtr HandleListMemory;
        public IntPtr EnvironmentMemory;
        public readonly List<string> CleanupErrors = new List<string>();
    }

    public static partial class MewCodeAppContainerProbe
    {
        static MewCodeAppContainerProbe()
        {
            AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
            AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        }

        // Inputs still reject extended/device paths. Only already validated
        // local-drive paths receive this internal Win32 long-path spelling.
        private static string NativeFilesystemPath(string path)
        {
            if (path.Length < 248) return path;
            if (path.Length < 3 || !IsAsciiLetter(path[0]) || path[1] != ':' ||
                (path[2] != '\\' && path[2] != '/'))
                throw new InvalidDataException("Internal filesystem path is not a validated local-drive path.");
            return @"\\?\" + path.Replace('/', '\\');
        }

        private static string PlainFilesystemPath(string path)
        {
            return path.StartsWith(@"\\?\", StringComparison.Ordinal) ? path.Substring(4) : path;
        }
        private const string FixturePrefix = "mewcode-lpac-probe-";
        private const string FixtureMarkerName = ".mewcode-synthetic-fixture";
        private const string FixtureMarkerContent = "mewcode-synthetic-only-v1\n";
        private const int MaxRequestBytes = 1024 * 1024;
        private const int MaxCapturedBytesPerStream = 1024 * 1024;
        private const int MaximumTimeoutMs = 60000;
        private const int DrainWaitMs = 5000;
        private const int CleanupWaitMs = 5000;
        private const string CancelEventPrefix = "MewCodeProbeCancel_";

        private const uint CreateSuspended = 0x00000004;
        private const uint CreateUnicodeEnvironment = 0x00000400;
        private const uint CreateNoWindow = 0x08000000;
        private const uint ExtendedStartupInfoPresent = 0x00080000;
        private const uint StartfUseStdHandles = 0x00000100;
        private const uint JobObjectExtendedLimitInformation = 9;
        private const uint JobObjectBasicAccountingInformation = 1;
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private const uint ProcThreadAttributeHandleList = 0x00020002;
        private const uint ProcThreadAttributeSecurityCapabilities = 0x00020009;
        private const uint ProcThreadAttributeAllApplicationPackagesPolicy = 0x0002000F;
        private const uint ProcessCreationAllApplicationPackagesOptOut = 0x00000001;
        private const uint GenericRead = 0x80000000;
        private const uint FileReadAttributes = 0x00000080;
        private const uint FileAddFile = 0x00000002;
        private const uint FileAddSubdirectory = 0x00000004;
        private const uint FileDeleteChild = 0x00000040;
        private const uint Delete = 0x00010000;
        private const uint WriteDac = 0x00040000;
        private const uint WriteOwner = 0x00080000;
        private const uint FileShareRead = 0x00000001;
        private const uint FileShareWrite = 0x00000002;
        private const uint FileShareDelete = 0x00000004;
        private const uint OpenExisting = 3;
        private const uint FileAttributeNormal = 0x00000080;
        private const uint FileFlagOpenReparsePoint = 0x00200000;
        private const uint FileFlagBackupSemantics = 0x02000000;
        private const uint HandleFlagInherit = 0x00000001;
        private const uint WaitObject0 = 0x00000000;
        private const uint WaitTimeout = 0x00000102;
        private const uint WaitFailed = 0xFFFFFFFF;
        private const uint Synchronize = 0x00100000;
        private const int ErrorInsufficientBuffer = 122;
        private const int ErrorAccessDenied = 5;
        private const int ErrorBrokenPipe = 109;
        private const int ErrorOperationAborted = 995;
        private const int ErrorInvalidHandle = 6;
        private const int ErrorNotSupported = 50;
        private const int ErrorInvalidParameter = 87;
        private const int ErrorInsufficientBufferNative = 122;
        private const uint SeGroupEnabled = 0x00000004;
        private const uint MaximumDerivedSidCount = 16;
        private const ulong MaximumDerivedSidArrayBytes = 1024 * 1024;
        private const uint ThreadTerminate = 0x0001;
        private static readonly IntPtr InvalidHandleValue = new IntPtr(-1);

        public static int Main(string[] args)
        {
            var receipt = new ProbeReceipt
            {
                stdout = String.Empty,
                stderr = String.Empty,
                error = String.Empty,
                job_cleanup_ok = true,
                profile_cleanup_ok = true,
                acl_cleanup_ok = true,
                capabilities = new string[0],
                timed_out = false,
                cancelled = false
            };

            try
            {
                if (args == null || args.Length != 1)
                    throw new ArgumentException("Expected exactly one absolute JSON request path.");
                string requestPath = ValidateAbsolutePath(args[0], "request path");
                CheckNoReparseAncestors(requestPath, "request path");
                var requestInfo = new FileInfo(requestPath);
                if (!requestInfo.Exists || requestInfo.Length > MaxRequestBytes)
                    throw new InvalidDataException("Request file is missing or exceeds 1 MiB.");
                string json = File.ReadAllText(requestPath, new UTF8Encoding(false, true));
                var serializer = NewSerializer();
                ProbeRequest request = serializer.Deserialize<ProbeRequest>(json);
                if (request == null)
                    throw new InvalidDataException("Request JSON must contain an object.");
                Execute(request, receipt);
            }
            catch (Exception ex)
            {
                AddError(receipt, Describe(ex));
            }

            try
            {
                Console.OutputEncoding = new UTF8Encoding(false);
                Console.Out.WriteLine(NewSerializer().Serialize(receipt));
                Console.Out.Flush();
            }
            catch
            {
                // The receipt is best-effort only if the host output stream itself has failed.
            }
            return String.IsNullOrEmpty(receipt.error) && !receipt.timed_out && !receipt.cancelled && receipt.exit_code.HasValue
                ? receipt.exit_code.Value
                : 1;
        }

        private static void Execute(ProbeRequest request, ProbeReceipt receipt, ValidatedRequest runtime = null)
        {
            var state = new LaunchState();
            ValidatedRequest validated = null;
            try
            {
                validated = runtime ?? ValidateRequest(request);
                if (validated.RuntimeNetwork)
                {
                    InitializeNetworkProfileIdentity(state, receipt);
                    receipt.capabilities = RuntimeCapabilityNames(validated).ToArray();
                }
                if (validated.RuntimeRequest) PinRuntimePaths(validated, state);
                if (validated.CancelEventName != null)
                {
                    // The trusted host creates this local event; the helper only waits on it.
                    state.CancelEvent = OpenEventW(Synchronize, false, validated.CancelEventName);
                    if (state.CancelEvent == IntPtr.Zero)
                        throw LastWin32("OpenEvent(cancel_event_name) failed.");
                }
                if (CancellationRequested(state.CancelEvent))
                {
                    receipt.cancelled = true;
                    return;
                }
                state.FixtureRoot = validated.FixtureRoot;
                state.RuntimeRequest = validated.RuntimeRequest;
                receipt.capabilities = RuntimeCapabilityNames(validated).ToArray();
                PrepareRuntimeCapabilities(validated, state);
                if (validated.RuntimeNetwork)
                {
                    CreateNetworkExecutionProfile(validated, state, receipt);
                }
                else
                {
                    state.ProfileName = "MewCodeProbe_" + Guid.NewGuid().ToString("N");
                    receipt.profile_name = state.ProfileName;
                    int profileHr = CreateAppContainerProfile(
                        state.ProfileName,
                        "MewCode diagnostic probe",
                        "Disposable synthetic AppContainer/LPAC diagnostic profile with explicitly selected capabilities",
                        state.CapabilityAttributesMemory,
                        state.CapabilityCount,
                        out state.PackageSid);
                    if (profileHr < 0)
                        throw new Win32Exception(profileHr, "CreateAppContainerProfile failed.");
                    state.ProfileCreated = true;
                }

                SecurityIdentifier packageSid = new SecurityIdentifier(state.PackageSid);
                WithAclLock(delegate() {
                    foreach (PathGrant grant in validated.Grants.Values)
                        ApplyGrant(grant, packageSid, state);
                });

                CreateAndConfigureJob(state);
                CreateStandardHandles(state);
                BuildProcessAttributes(validated, state);
                state.EnvironmentMemory = Marshal.StringToHGlobalUni(BuildEnvironmentBlock(validated.Environment));

                var startup = new STARTUPINFOEX();
                startup.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
                startup.StartupInfo.dwFlags = StartfUseStdHandles;
                startup.StartupInfo.hStdInput = state.StdIn;
                startup.StartupInfo.hStdOutput = state.StdOutWrite;
                startup.StartupInfo.hStdError = state.StdErrWrite;
                startup.lpAttributeList = state.AttributeList;

                string commandLine = BuildCommandLine(validated.Executable, validated.Arguments);
                if (commandLine.Length > 32760)
                    throw new InvalidDataException("Command line exceeds the Windows process creation limit.");

                PROCESS_INFORMATION processInfo;
                bool created = CreateProcessW(
                    validated.Executable,
                    new StringBuilder(commandLine),
                    IntPtr.Zero,
                    IntPtr.Zero,
                    true,
                    CreateSuspended | CreateUnicodeEnvironment | ExtendedStartupInfoPresent | CreateNoWindow,
                    state.EnvironmentMemory,
                    validated.Cwd,
                    ref startup,
                    out processInfo);
                if (!created)
                    throw LastWin32("CreateProcessW failed.");

                state.ProcessCreated = true;
                state.Process = processInfo.hProcess;
                state.PrimaryThread = processInfo.hThread;
                receipt.pid = unchecked((int)processInfo.dwProcessId);

                CloseChildSideHandlesInParent(state);

                if (!AssignProcessToJobObject(state.Job, state.Process))
                    throw LastWin32("AssignProcessToJobObject failed; suspended child was not resumed.");
                state.ProcessAssigned = true;

                VerifyChildAppContainerToken(state, receipt, validated.Lpac);
                if (CancellationRequested(state.CancelEvent))
                {
                    // The child is still suspended and assigned to the job; finally terminates it.
                    receipt.cancelled = true;
                    return;
                }

                state.StdOutCapture = new CaptureBuffer(state.StdOutRead, MaxCapturedBytesPerStream);
                state.StdErrCapture = new CaptureBuffer(state.StdErrRead, MaxCapturedBytesPerStream);
                if (validated.RuntimeRequest)
                {
                    // One code page end to end, and it is the system OEM page:
                    // Windows PowerShell 5 decodes and emits redirected text in
                    // it (its OutputEncoding setter is blocked in
                    // ConstrainedLanguage), Python is pinned to it via
                    // PYTHONIOENCODING, and the request bootstrap routes Git's
                    // UTF-8 through raw-byte capture files. Raw bytes stay
                    // available in *_base64 regardless.
                    receipt.output_code_page = unchecked((int)GetOEMCP());
                    state.StdOutCapture.CodePage = receipt.output_code_page;
                    state.StdErrCapture.CodePage = receipt.output_code_page;
                }
                StartCapture(state.StdOutCapture);
                StartCapture(state.StdErrCapture);

                uint resumeResult = ResumeThread(state.PrimaryThread);
                if (resumeResult == 0xFFFFFFFF)
                    throw LastWin32("ResumeThread failed.");
                state.ProcessResumed = true;
                // Host-only diagnostic stream; child stderr uses a separate pipe.
                Console.Error.WriteLine("MEWCODE_CHILD_PID=" + receipt.pid.Value.ToString());
                Console.Error.Flush();

                bool cancellable = state.CancelEvent != IntPtr.Zero;
                uint wait = cancellable
                    ? WaitForMultipleObjects(2, new IntPtr[] { state.CancelEvent, state.Process }, false,
                        (uint)validated.TimeoutMs)
                    : WaitForSingleObject(state.Process, (uint)validated.TimeoutMs);
                if (cancellable && wait == WaitObject0)
                {
                    receipt.cancelled = true;
                    TerminateJob(state, receipt);
                    WaitForRootCleanup(state, receipt);
                }
                else if (wait == WaitTimeout)
                {
                    receipt.timed_out = true;
                    AddError(receipt, "child process exceeded timeout_ms");
                    TerminateJob(state, receipt);
                    WaitForRootCleanup(state, receipt);
                }
                else if (wait == (cancellable ? WaitObject0 + 1 : WaitObject0))
                {
                    state.RootCompleted = true;
                    uint exitCode;
                    if (GetExitCodeProcess(state.Process, out exitCode))
                        receipt.exit_code = unchecked((int)exitCode);
                    else
                        AddError(receipt, Describe(LastWin32("GetExitCodeProcess failed.")));

                    // The root may have left descendants behind; kill the whole job before capture joins.
                    TerminateJob(state, receipt);
                }
                else if (wait == WaitFailed)
                {
                    throw LastWin32(cancellable
                        ? "WaitForMultipleObjects(cancel_event_name, process) failed."
                        : "WaitForSingleObject(process) failed.");
                }
                else
                {
                    throw new InvalidOperationException("Unexpected process wait result: " + wait.ToString());
                }

                JoinCapture(state.StdOutCapture, receipt, "stdout");
                JoinCapture(state.StdErrCapture, receipt, "stderr");
                CopyCapture(state.StdOutCapture, receipt, true);
                CopyCapture(state.StdErrCapture, receipt, false);
            }
            catch (Exception ex)
            {
                AddError(receipt, Describe(ex));
            }
            finally
            {
                CleanupChildAndJob(state, receipt);
                JoinCapture(state.StdOutCapture, receipt, "stdout");
                JoinCapture(state.StdErrCapture, receipt, "stderr");
                CopyCapture(state.StdOutCapture, receipt, true);
                CopyCapture(state.StdErrCapture, receipt, false);
                try { WithAclLock(delegate() { CleanupAclGrants(state, validated, receipt); }); }
                catch (Exception error) { receipt.acl_cleanup_ok = false; AddError(receipt, Describe(error)); }
                CleanupNativeResources(state, receipt);
                CleanupProfile(state, receipt);
                foreach (IntPtr pin in state.RuntimePathPins) CloseHandle(pin);
            }
        }

        private static void PinRuntimePaths(ValidatedRequest request, LaunchState state)
        {
            // No DELETE sharing: exact grants and ancestors cannot be swapped
            // while the host applies/removes ACEs. Never inherited by children.
            var paths = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
            var leaves = new List<string>(request.Grants.Keys) { request.Executable };
            foreach (string leaf in leaves)
            {
                string current = leaf;
                while (!String.IsNullOrEmpty(current))
                {
                    paths.Add(current);
                    current = Path.GetDirectoryName(TrimSeparators(current));
                }
            }
            foreach (string path in paths)
            {
                uint pinShare = FileShareRead | FileShareWrite;
                uint pinAccess = FileReadAttributes;
                PathGrant protectedGrant;
                if (request.Grants.TryGetValue(path, out protectedGrant) && protectedGrant.StagedReadOnly)
                {
                    pinShare = FileShareRead;
                    // Attribute-only handles are not sufficient to enforce
                    // the data read/write sharing interlock on Windows.
                    pinAccess = GenericRead;
                }
                IntPtr handle = OpenFileForLinkCount(path, pinAccess,
                    pinShare, IntPtr.Zero, OpenExisting,
                    FileFlagOpenReparsePoint | FileFlagBackupSemantics, IntPtr.Zero);
                if (handle == IntPtr.Zero || handle == InvalidHandleValue)
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error == ErrorAccessDenied && IsUnmodifiableHostProfileAncestor(path, request))
                    {
                        // This is an ungranted host-profile boundary the current helper token
                        // cannot modify. In restricted hosts the helper may traverse it
                        // (GetFileAttributes succeeds) but cannot open it for attributes. Omit
                        // only this unmodifiable host-profile handle; the
                        // full no-reparse walk still runs, and every authorized path below it stays
                        // pinned without FILE_SHARE_DELETE.
                        CheckNoReparseAncestors(path, "unlocked host-profile boundary");
                        continue;
                    }
                    throw new Win32Exception(error, "Cannot pin a runtime path; refusing execution.");
                }
                state.RuntimePathPins.Add(handle);
                BY_HANDLE_FILE_INFORMATION information;
                if (!GetFileInformationByHandle(handle, out information) ||
                    (information.FileAttributes & (uint)FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException("Runtime path identity is unavailable or reparse-backed.");
                CheckNoReparseAncestors(path, "pinned runtime path");
                if ((information.FileAttributes & (uint)FileAttributes.Directory) == 0 &&
                    request.Grants.ContainsKey(path) && information.NumberOfLinks != 1)
                    throw new InvalidDataException("A granted runtime file is hard-linked.");
            }
        }

        private static bool IsUnmodifiableHostProfileAncestor(string path, ValidatedRequest request)
        {
            string configuredProfile = Environment.GetEnvironmentVariable("USERPROFILE");
            if (String.IsNullOrEmpty(configuredProfile) || request.Grants.ContainsKey(path) ||
                String.Equals(path, request.Executable, StringComparison.OrdinalIgnoreCase))
                return false;

            string profile;
            try { profile = TrimSeparators(Path.GetFullPath(configuredProfile)); }
            catch { return false; }
            if (!String.Equals(TrimSeparators(path), profile, StringComparison.OrdinalIgnoreCase))
                return false;

            bool ancestorOfAuthorizedPath = IsStrictlyBeneath(request.Executable, profile);
            if (!ancestorOfAuthorizedPath)
                foreach (PathGrant grant in request.Grants.Values)
                    if (IsStrictlyBeneath(grant.Path, profile))
                    {
                        ancestorOfAuthorizedPath = true;
                        break;
                    }
            if (!ancestorOfAuthorizedPath) return false;

            string parent = Path.GetDirectoryName(profile);
            if (String.IsNullOrEmpty(parent)) return false;
            uint[] profileMutationRights = { FileAddFile, FileAddSubdirectory, FileDeleteChild,
                Delete, WriteDac, WriteOwner };
            uint[] parentMutationRights = { FileAddFile, FileAddSubdirectory, FileDeleteChild,
                Delete, WriteDac, WriteOwner };
            return AllRightsDenied(profile, profileMutationRights) && AllRightsDenied(parent, parentMutationRights);
        }

        private static bool AllRightsDenied(string path, uint[] rights)
        {
            foreach (uint right in rights)
            {
                IntPtr handle = OpenFileForLinkCount(path, right,
                    FileShareRead | FileShareWrite | FileShareDelete, IntPtr.Zero, OpenExisting,
                    FileFlagOpenReparsePoint | FileFlagBackupSemantics, IntPtr.Zero);
                if (handle != IntPtr.Zero && handle != InvalidHandleValue)
                {
                    CloseHandle(handle);
                    return false;
                }
                if (Marshal.GetLastWin32Error() != ErrorAccessDenied) return false;
            }
            return true;
        }

        private static ValidatedRequest ValidateRequest(ProbeRequest request)
        {
            if (request == null) throw new InvalidDataException("Request is null.");
            string cancelEventName = ValidateCancelEventName(request.cancel_event_name);
            string executable = ValidateExistingPath(request.executable, "executable", false);
            CheckNoReparseAncestors(executable, "executable");
            if (!String.Equals(Path.GetExtension(executable), ".exe", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("executable must be an .exe file.");
            bool trustedSystemExecutable = IsTrustedSystemExecutable(executable);

            string fixtureRoot = ValidateExistingPath(request.fixture_root, "fixture_root", true);
            CheckNoReparseAncestors(fixtureRoot, "fixture_root");
            string leaf = Path.GetFileName(fixtureRoot.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
            if (leaf == null || !leaf.StartsWith(FixturePrefix, StringComparison.OrdinalIgnoreCase) ||
                leaf.Length < FixturePrefix.Length + 8 || !IsSafeRandomSuffix(leaf.Substring(FixturePrefix.Length)))
                throw new InvalidDataException("fixture_root must be a uniquely named mewcode-lpac-probe-* directory.");
            string parent = Path.GetDirectoryName(fixtureRoot.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
            if (String.IsNullOrEmpty(parent) || !String.Equals(Path.GetFileName(parent), ".tmp", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("fixture_root must be a direct child of a .tmp directory.");
            string marker = Path.Combine(fixtureRoot, FixtureMarkerName);
            marker = ValidateExistingPath(marker, "fixture marker", false);
            CheckNoReparseAncestors(marker, "fixture marker");
            RejectHardLinkedFile(marker, "fixture marker");
            if (new FileInfo(marker).Length != Encoding.ASCII.GetByteCount(FixtureMarkerContent))
                throw new InvalidDataException("fixture marker has an unexpected length.");
            string markerContent = File.ReadAllText(marker, Encoding.ASCII);
            if (!String.Equals(markerContent, FixtureMarkerContent, StringComparison.Ordinal))
                throw new InvalidDataException("fixture marker content does not match the synthetic-only sentinel.");

            string cwd = ValidateExistingPath(request.cwd, "cwd", true);
            EnsureStrictlyBeneath(cwd, fixtureRoot, "cwd");
            CheckNoReparseAncestors(cwd, "cwd");

            if (request.arguments == null) throw new InvalidDataException("arguments must be a string array.");
            var arguments = new string[request.arguments.Length];
            for (int i = 0; i < request.arguments.Length; i++)
            {
                string arg = request.arguments[i];
                if (arg == null || arg.IndexOf('\0') >= 0)
                    throw new InvalidDataException("arguments must contain only non-null strings without NUL.");
                arguments[i] = arg;
            }

            int timeout = request.timeout_ms.HasValue ? request.timeout_ms.Value : 20000;
            if (timeout < 1000 || timeout > MaximumTimeoutMs)
                throw new InvalidDataException("timeout_ms must be between 1000 and 60000.");

            if (request.environment == null)
                throw new InvalidDataException("environment must be a dictionary; it is passed as the exact child environment.");
            var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (KeyValuePair<string, string> entry in request.environment)
            {
                if (String.IsNullOrEmpty(entry.Key) || entry.Key.IndexOf('=') >= 0 || entry.Key.IndexOf('\0') >= 0 ||
                    entry.Value == null || entry.Value.IndexOf('\0') >= 0)
                    throw new InvalidDataException("environment names and values must be non-null, contain no NUL, and names may not contain '='.");
                if (environment.ContainsKey(entry.Key))
                    throw new InvalidDataException("environment contains duplicate names ignoring case.");
                environment.Add(entry.Key, entry.Value);
            }

            var grants = new Dictionary<string, PathGrant>(StringComparer.OrdinalIgnoreCase);
            AddRequestedGrants(request.read_paths, "read_paths", true, fixtureRoot, grants);
            AddRequestedGrants(request.write_paths, "write_paths", false, fixtureRoot, grants);
            // Older/hand-authored requests may omit this optional field. Missing means
            // no additional ExecuteFile authority; execute grants are exact files only.
            if (request.execute_paths != null)
                AddRequestedExecuteGrants(request.execute_paths, "execute_paths", fixtureRoot, grants);

            if (!grants.ContainsKey(cwd))
                throw new InvalidDataException("cwd must also appear in read_paths or write_paths to make its access explicit.");

            bool executableInFixture = IsStrictlyBeneath(executable, fixtureRoot);
            if (!executableInFixture && !trustedSystemExecutable)
                throw new InvalidDataException("executable must be inside fixture_root or the exact System32 cmd.exe/Windows PowerShell binary.");
            if (executableInFixture)
            {
                RejectHardLinkedFile(executable, "fixture executable");
                AddGrant(grants, executable, false, true, false, true, "executable");
            }

            bool lpac = request.lpac.HasValue ? request.lpac.Value : true;
            bool runtimeRegistryRead = request.runtime_registry_read.HasValue && request.runtime_registry_read.Value;
            if (runtimeRegistryRead && !lpac)
                throw new InvalidDataException("runtime_registry_read registry capability is allowed only when LPAC is enabled.");
            bool runtimeInstrumentation = request.runtime_instrumentation.HasValue && request.runtime_instrumentation.Value;
            if (runtimeInstrumentation && (!lpac || !runtimeRegistryRead))
                throw new InvalidDataException("runtime_instrumentation requires LPAC and runtime_registry_read=true.");
            bool runtimeCom = request.runtime_com.HasValue && request.runtime_com.Value;
            if (runtimeCom && (!lpac || !runtimeRegistryRead))
                throw new InvalidDataException("runtime_com requires LPAC and runtime_registry_read=true.");

            return new ValidatedRequest
            {
                FixtureRoot = fixtureRoot,
                Executable = executable,
                Cwd = cwd,
                Arguments = arguments,
                TimeoutMs = timeout,
                CancelEventName = cancelEventName,
                Lpac = lpac,
                RuntimeRegistryRead = runtimeRegistryRead,
                RuntimeInstrumentation = runtimeInstrumentation,
                RuntimeCom = runtimeCom,
                Environment = environment,
                Grants = grants
            };
        }

        private static string ValidateCancelEventName(string name)
        {
            if (name == null) return null;
            if (name.Length != CancelEventPrefix.Length + 32 ||
                !name.StartsWith(CancelEventPrefix, StringComparison.Ordinal))
                throw new InvalidDataException("cancel_event_name must be MewCodeProbeCancel_ followed by 32 hexadecimal characters.");
            for (int i = CancelEventPrefix.Length; i < name.Length; i++)
            {
                char c = name[i];
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')))
                    throw new InvalidDataException("cancel_event_name must be MewCodeProbeCancel_ followed by 32 hexadecimal characters.");
            }
            return name;
        }

        private static bool CancellationRequested(IntPtr cancelEvent)
        {
            if (cancelEvent == IntPtr.Zero) return false;
            uint wait = WaitForSingleObject(cancelEvent, 0);
            if (wait == WaitObject0) return true;
            if (wait == WaitTimeout) return false;
            if (wait == WaitFailed)
                throw LastWin32("WaitForSingleObject(cancel_event_name) failed.");
            throw new InvalidOperationException("Unexpected cancel event wait result: " + wait.ToString());
        }

        private static List<string> RuntimeCapabilityNames(ValidatedRequest request)
        {
            var capabilityNames = new List<string>();
            if (request.RuntimeRegistryRead) capabilityNames.Add("registryRead");
            if (request.RuntimeInstrumentation) capabilityNames.Add("lpacInstrumentation");
            // The protocol selects a fixed capability set; callers cannot supply
            // arbitrary capability names or relax the existing LPAC policy.
            if (request.RuntimeCom) capabilityNames.Add("lpacCom");
            if (request.RuntimeNetwork) capabilityNames.Add("internetClient");
            return capabilityNames;
        }

        private static void PrepareRuntimeCapabilities(ValidatedRequest request, LaunchState state)
        {
            var capabilityNames = RuntimeCapabilityNames(request);
            if (capabilityNames.Count == 0) return;

            int entrySize = Marshal.SizeOf(typeof(SID_AND_ATTRIBUTES));
            state.CapabilityAttributesMemory = Marshal.AllocHGlobal(checked(entrySize * capabilityNames.Count));
            for (int i = 0; i < capabilityNames.Count; i++)
            {
                string capabilityName = capabilityNames[i];
                var derivedSids = new DerivedCapabilitySids { Name = capabilityName };
                // Register the outputs before the native call so partial outputs are reclaimed on every failure path.
                state.DerivedCapabilities.Add(derivedSids);
                bool derived = DeriveCapabilitySidsFromName(
                    capabilityName,
                    out derivedSids.GroupSids,
                    out derivedSids.GroupSidCount,
                    out derivedSids.CapabilitySids,
                    out derivedSids.CapabilitySidCount);
                if (!derived)
                    throw LastWin32("DeriveCapabilitySidsFromName(" + capabilityName + ") failed.");

                ValidateDerivedSidArray(derivedSids.GroupSids, derivedSids.GroupSidCount,
                    capabilityName + " group SID", false);
                ValidateDerivedSidArray(derivedSids.CapabilitySids, derivedSids.CapabilitySidCount,
                    capabilityName + " capability SID", true);

                IntPtr capabilitySid = Marshal.ReadIntPtr(derivedSids.CapabilitySids);
                if (capabilitySid == IntPtr.Zero)
                    throw new InvalidDataException("DeriveCapabilitySidsFromName returned a null " + capabilityName + " SID.");

                var capability = new SID_AND_ATTRIBUTES
                {
                    Sid = capabilitySid,
                    Attributes = SeGroupEnabled
                };
                IntPtr entry = IntPtr.Add(state.CapabilityAttributesMemory, checked(entrySize * i));
                Marshal.StructureToPtr(capability, entry, false);
            }
            state.CapabilityCount = (uint)capabilityNames.Count;
        }

        private static void ValidateDerivedSidArray(IntPtr sidArray, uint sidCount, string label,
            bool requireSingle)
        {
            if (sidArray == IntPtr.Zero || sidCount == 0 || sidCount > MaximumDerivedSidCount ||
                (requireSingle && sidCount != 1))
                throw new InvalidDataException("DeriveCapabilitySidsFromName returned an unexpected " + label + " array.");

            UIntPtr allocationSize = LocalSize(sidArray);
            ulong allocationBytes = allocationSize.ToUInt64();
            ulong requiredBytes = (ulong)sidCount * (ulong)IntPtr.Size;
            if (allocationBytes == 0 || allocationBytes > MaximumDerivedSidArrayBytes || requiredBytes > allocationBytes)
                throw new InvalidDataException("DeriveCapabilitySidsFromName returned an invalid " + label + " array size.");
        }

        private static bool IsSafeRandomSuffix(string suffix)
        {
            if (suffix == null) return false;
            for (int i = 0; i < suffix.Length; i++)
            {
                char c = suffix[i];
                if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                      (c >= '0' && c <= '9') || c == '-' || c == '_')) return false;
            }
            return true;
        }

        private static void AddRequestedGrants(string[] paths, string label, bool read, string fixtureRoot,
            Dictionary<string, PathGrant> grants)
        {
            if (paths == null) throw new InvalidDataException(label + " must be a string array.");
            for (int i = 0; i < paths.Length; i++)
            {
                string path = ValidateExistingPath(paths[i], label + "[" + i.ToString() + "]", null);
                CheckNoReparseAncestors(path, label);
                EnsureStrictlyBeneath(path, fixtureRoot, label + "[" + i.ToString() + "]");
                bool isDirectory = (File.GetAttributes(NativeFilesystemPath(path)) & FileAttributes.Directory) != 0;
                if (!isDirectory) RejectHardLinkedFile(path, label + "[" + i.ToString() + "]");
                AddGrant(grants, path, isDirectory, read, !read, false, label);
            }
        }

        private static void AddRequestedExecuteGrants(string[] paths, string label, string fixtureRoot,
            Dictionary<string, PathGrant> grants)
        {
            for (int i = 0; i < paths.Length; i++)
            {
                string path = ValidateExistingPath(paths[i], label + "[" + i.ToString() + "]", false);
                CheckNoReparseAncestors(path, label);
                EnsureStrictlyBeneath(path, fixtureRoot, label + "[" + i.ToString() + "]");
                RejectHardLinkedFile(path, label + "[" + i.ToString() + "]");
                AddGrant(grants, path, false, true, false, true, label);
            }
        }

        private static void AddGrant(Dictionary<string, PathGrant> grants, string path, bool isDirectory,
            bool read, bool write, bool execute, string label)
        {
            PathGrant grant;
            if (!grants.TryGetValue(path, out grant))
            {
                grant = new PathGrant { Path = path, IsDirectory = isDirectory };
                grants.Add(path, grant);
            }
            else if (grant.IsDirectory != isDirectory)
            {
                throw new InvalidDataException("Path type changed during validation for " + label + ".");
            }
            if (read) grant.Read = true;
            if (write) grant.Write = true;
            if (execute) grant.Execute = true;
        }

        private static string ValidateExistingPath(string raw, string label, bool? directory)
        {
            string path = ValidateAbsolutePath(raw, label);
            CheckNoReparseAncestors(path, label);
            FileAttributes attributes;
            try { attributes = File.GetAttributes(NativeFilesystemPath(path)); }
            catch (Exception ex) { throw new InvalidDataException(label + " must exist.", ex); }
            bool isDirectory = (attributes & FileAttributes.Directory) != 0;
            if (directory.HasValue && directory.Value != isDirectory)
                throw new InvalidDataException(label + (directory.Value ? " must be a directory." : " must be a file."));
            return path;
        }

        private static string ValidateAbsolutePath(string raw, string label)
        {
            if (String.IsNullOrEmpty(raw) || raw.IndexOf('\0') >= 0)
                throw new InvalidDataException(label + " must be an absolute path without NUL.");
            if (raw.Length < 3 || !IsAsciiLetter(raw[0]) || raw[1] != ':' ||
                (raw[2] != '\\' && raw[2] != '/'))
                throw new InvalidDataException(label + " must be an absolute local-drive path; UNC and drive-relative paths are rejected.");
            if (raw.StartsWith("\\\\?\\", StringComparison.Ordinal) || raw.StartsWith("\\\\.\\", StringComparison.Ordinal))
                throw new InvalidDataException(label + " may not use a device or extended path prefix.");
            string normalizedParts = raw.Replace('/', '\\');
            string[] parts = normalizedParts.Split('\\');
            for (int i = 1; i < parts.Length; i++)
            {
                if (parts[i] == "..") throw new InvalidDataException(label + " may not contain parent traversal.");
            }
            if (raw.IndexOf(':', 2) >= 0)
                throw new InvalidDataException(label + " may not contain an alternate data stream.");
            try { return Path.GetFullPath(raw); }
            catch (Exception ex) { throw new InvalidDataException(label + " is not a valid Windows path.", ex); }
        }

        private static bool IsAsciiLetter(char value)
        {
            return (value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z');
        }

        private static void CheckNoReparseAncestors(string path, string label)
        {
            string current = path;
            while (!String.IsNullOrEmpty(current))
            {
                FileAttributes attributes;
                try { attributes = File.GetAttributes(NativeFilesystemPath(current)); }
                catch (Exception ex) { throw new InvalidDataException(label + " and every ancestor must exist.", ex); }
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException(label + " or an ancestor is a reparse point.");
                string root = Path.GetPathRoot(current);
                if (String.Equals(TrimSeparators(current), TrimSeparators(root), StringComparison.OrdinalIgnoreCase))
                    break;
                string parent = Path.GetDirectoryName(TrimSeparators(current));
                if (String.IsNullOrEmpty(parent) || String.Equals(parent, current, StringComparison.OrdinalIgnoreCase))
                    break;
                current = parent;
            }
        }

        private static string TrimSeparators(string path)
        {
            string root = Path.GetPathRoot(path);
            string value = path;
            while (value.Length > (root == null ? 0 : root.Length) &&
                   (value[value.Length - 1] == '\\' || value[value.Length - 1] == '/'))
                value = value.Substring(0, value.Length - 1);
            return value;
        }

        private static bool IsStrictlyBeneath(string path, string root)
        {
            string normalizedRoot = TrimSeparators(Path.GetFullPath(root));
            string normalizedPath = TrimSeparators(Path.GetFullPath(path));
            if (String.Equals(normalizedRoot, normalizedPath, StringComparison.OrdinalIgnoreCase)) return false;
            string prefix = normalizedRoot + Path.DirectorySeparatorChar;
            return normalizedPath.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
        }

        private static void EnsureStrictlyBeneath(string path, string root, string label)
        {
            if (!IsStrictlyBeneath(path, root))
                throw new InvalidDataException(label + " must exist strictly beneath fixture_root; fixture_root itself is never granted.");
        }

        private static bool IsTrustedSystemExecutable(string path)
        {
            var buffer = new StringBuilder(32768);
            uint length = GetWindowsDirectoryW(buffer, (uint)buffer.Capacity);
            if (length == 0 || length >= buffer.Capacity) return false;
            string root = ValidateAbsolutePath(buffer.ToString(), "host Windows directory");
            string system32 = Path.Combine(root, "System32");
            string cmd = Path.Combine(system32, "cmd.exe");
            string powershell = Path.Combine(system32, "WindowsPowerShell", "v1.0", "powershell.exe");
            return String.Equals(path, Path.GetFullPath(cmd), StringComparison.OrdinalIgnoreCase) ||
                   String.Equals(path, Path.GetFullPath(powershell), StringComparison.OrdinalIgnoreCase);
        }

        private static void RejectHardLinkedFile(string path, string label)
        {
            IntPtr handle = OpenFileForLinkCount(NativeFilesystemPath(path), FileReadAttributes,
                FileShareRead | FileShareWrite | FileShareDelete, IntPtr.Zero,
                OpenExisting, FileFlagOpenReparsePoint, IntPtr.Zero);
            if (handle == IntPtr.Zero || handle == InvalidHandleValue)
                throw LastWin32("Could not inspect " + label + " hard-link count.");
            try
            {
                BY_HANDLE_FILE_INFORMATION information;
                if (!GetFileInformationByHandle(handle, out information))
                    throw LastWin32("GetFileInformationByHandle failed for " + label + ".");
                if ((information.FileAttributes & (uint)FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException(label + " is a reparse point.");
                if (information.NumberOfLinks > 1)
                    throw new InvalidDataException(label + " must not be hard-linked (NumberOfLinks > 1).");
                if (information.NumberOfLinks != 1)
                    throw new InvalidDataException(label + " has an invalid or unavailable hard-link count.");
            }
            finally
            {
                CloseHandle(handle);
            }
        }

        private static void VerifyChildAppContainerToken(LaunchState state, ProbeReceipt receipt, bool requestedLpac)
        {
            if (!OpenProcessToken(state.Process, 0x000A, out state.ProcessToken))
                throw LastWin32("OpenProcessToken(TOKEN_QUERY | TOKEN_DUPLICATE) failed while the child was suspended.");

            int isAppContainer = 0;
            uint returned;
            if (!GetTokenInformation(state.ProcessToken, 29, out isAppContainer, sizeof(int), out returned))
                throw LastWin32("GetTokenInformation(TokenIsAppContainer) failed while the child was suspended.");
            if (isAppContainer != 1)
                throw new InvalidOperationException("Created child token is not an AppContainer token; refusing to resume it.");

            uint required = 0;
            bool firstCall = GetTokenInformation(state.ProcessToken, 31, IntPtr.Zero, 0, out required);
            int sizingError = Marshal.GetLastWin32Error();
            if (firstCall || sizingError != ErrorInsufficientBuffer || required < (uint)IntPtr.Size || required > 4096)
                throw new Win32Exception(sizingError, "Could not size TokenAppContainerSid information while the child was suspended.");

            IntPtr infoMemory = Marshal.AllocHGlobal((int)required);
            try
            {
                if (!GetTokenInformation(state.ProcessToken, 31, infoMemory, required, out returned))
                    throw LastWin32("GetTokenInformation(TokenAppContainerSid) failed while the child was suspended.");
                IntPtr actualSid = Marshal.ReadIntPtr(infoMemory);
                if (actualSid == IntPtr.Zero)
                    throw new InvalidOperationException("Created child token has no AppContainer SID; refusing to resume it.");
                long start = infoMemory.ToInt64();
                long end = start + required;
                long sidAddress = actualSid.ToInt64();
                if (sidAddress < start || sidAddress > end - 8)
                    throw new InvalidOperationException("TokenAppContainerSid returned a pointer outside its buffer.");
                uint sidLength = GetLengthSid(actualSid);
                if (sidAddress < start || sidAddress > end || sidLength == 0 || sidAddress + sidLength > end)
                    throw new InvalidOperationException("TokenAppContainerSid returned an invalid buffer pointer; refusing to resume the child.");
                var actual = new SecurityIdentifier(actualSid);
                var expected = new SecurityIdentifier(state.PackageSid);
                if (!actual.Equals(expected))
                    throw new InvalidOperationException("Created child AppContainer SID does not match the fresh profile SID; refusing to resume it.");
                receipt.appcontainer_verified = true;
            }
            finally
            {
                Marshal.FreeHGlobal(infoMemory);
            }

            int isLpac = 0;
            if (!GetTokenInformation(state.ProcessToken, 46, out isLpac, sizeof(int), out returned))
            {
                int queryError = Marshal.GetLastWin32Error();
                if (queryError != ErrorInvalidParameter && queryError != ErrorNotSupported)
                    throw new Win32Exception(queryError,
                        "GetTokenInformation(TokenIsLessPrivilegedAppContainer) failed; refusing to resume.");

                receipt.lpac_verification_method = "access_check";
                uint grantedAccess = CheckLpacAccessMask(state.ProcessToken);
                receipt.lpac_access_mask = unchecked((int)grantedAccess);
                uint expectedAccess = requestedLpac ? 0x2u : 0x3u;
                if (grantedAccess != expectedAccess)
                    throw new InvalidOperationException("LPAC AccessCheck returned " + grantedAccess.ToString() +
                        "; expected " + expectedAccess.ToString() + ". Refusing to resume the child.");
                receipt.lpac_verified = true;
                return;
            }
            receipt.lpac_verification_method = "token_query";
            if ((isLpac != 0) != requestedLpac)
                throw new InvalidOperationException("Created child LPAC state does not match the request; refusing to resume it.");
            receipt.lpac_verified = true;
        }

        private static uint CheckLpacAccessMask(IntPtr processToken)
        {
            // Kernel behavior check used by Chromium's CheckLpacToken:
            // https://chromium.googlesource.com/chromium/src/+/4fc987ed69c8333f860e2ab455bbdd731cdf682c/sandbox/win/src/app_container_test.cc
            IntPtr impersonationToken = IntPtr.Zero;
            IntPtr securityDescriptor = IntPtr.Zero;
            IntPtr privilegeBuffer = IntPtr.Zero;
            try
            {
                if (!DuplicateToken(processToken, 2, out impersonationToken))
                    throw LastWin32("DuplicateToken(SecurityImpersonation) failed for LPAC verification.");

                uint descriptorBytes;
                const string descriptorSddl = "O:SYG:SYD:(A;;0x3;;;WD)(A;;0x1;;;AC)(A;;0x2;;;S-1-15-2-2)";
                if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    descriptorSddl, 1, out securityDescriptor, out descriptorBytes))
                    throw LastWin32("Converting the in-memory LPAC verification descriptor failed.");

                const int maxPrivilegeBufferBytes = 4096;
                privilegeBuffer = Marshal.AllocHGlobal(maxPrivilegeBufferBytes);
                var zeroes = new byte[maxPrivilegeBufferBytes];
                Marshal.Copy(zeroes, 0, privilegeBuffer, zeroes.Length);

                var mapping = new GENERIC_MAPPING();
                uint privilegeBytes = (uint)Marshal.SizeOf(typeof(PRIVILEGE_SET));
                for (int attempt = 0; attempt < 2; attempt++)
                {
                    uint grantedAccess;
                    bool accessStatus;
                    if (AccessCheck(securityDescriptor, impersonationToken, 0x02000000, ref mapping,
                        privilegeBuffer, ref privilegeBytes, out grantedAccess, out accessStatus))
                    {
                        if (!accessStatus)
                            throw new UnauthorizedAccessException("LPAC verification AccessCheck denied the synthetic descriptor.");
                        return grantedAccess;
                    }

                    int error = Marshal.GetLastWin32Error();
                    if (error != ErrorInsufficientBufferNative ||
                        privilegeBytes <= (uint)Marshal.SizeOf(typeof(PRIVILEGE_SET)) ||
                        privilegeBytes > maxPrivilegeBufferBytes || attempt != 0)
                        throw new Win32Exception(error, "AccessCheck failed for LPAC verification.");
                    Marshal.Copy(zeroes, 0, privilegeBuffer, zeroes.Length);
                }
                throw new InvalidOperationException("LPAC verification AccessCheck exceeded its bounded retry.");
            }
            finally
            {
                if (privilegeBuffer != IntPtr.Zero) Marshal.FreeHGlobal(privilegeBuffer);
                if (securityDescriptor != IntPtr.Zero) LocalFree(securityDescriptor);
                if (impersonationToken != IntPtr.Zero) CloseHandle(impersonationToken);
            }
        }

        private static void ApplyGrant(PathGrant grant, SecurityIdentifier packageSid, LaunchState state)
        {
            CheckNoReparseAncestors(grant.Path, "ACL target");
            if (grant.StagedMutableTree || grant.StagedReadOnly)
            {
                ApplyStagedGrant(grant, packageSid, state);
                return;
            }
            if (!state.RuntimeRequest && !IsStrictlyBeneath(grant.Path, state.FixtureRoot))
                throw new InvalidDataException("ACL target moved outside fixture_root before grant.");
            if (!grant.IsDirectory) RejectHardLinkedFile(grant.Path, "ACL target");
            int rights = 0;
            if (grant.IsDirectory)
            {
                if (grant.Read) rights |= 0x00000001 | 0x00000020 | 0x00000008 | 0x00000080 | 0x00020000 | 0x00100000;
                if (grant.Write) rights |= 0x00000002 | 0x00000004 | 0x00000020 | 0x00000010 | 0x00000100 | 0x00100000;
            }
            else
            {
                if (grant.Read) rights |= 0x00000001 | 0x00000008 | 0x00000080 | 0x00020000 | 0x00100000;
                if (grant.Write) rights |= 0x00000002 | 0x00000004 | 0x00000010 | 0x00000100 | 0x00100000;
                if (grant.Execute) rights |= 0x00000020;
            }
            grant.Rule = new FileSystemAccessRule(
                packageSid,
                (FileSystemRights)rights,
                InheritanceFlags.None,
                PropagationFlags.None,
                AccessControlType.Allow);
            // Record before the path-based update so finally attempts exact-ACE removal even if the setter throws late.
            state.AppliedGrants.Add(grant);
            if (grant.IsDirectory)
            {
                DirectorySecurity security = Directory.GetAccessControl(NativeFilesystemPath(grant.Path));
                security.AddAccessRule(grant.Rule);
                Directory.SetAccessControl(NativeFilesystemPath(grant.Path), security);
            }
            else
            {
                FileSecurity security = File.GetAccessControl(NativeFilesystemPath(grant.Path));
                security.AddAccessRule(grant.Rule);
                File.SetAccessControl(NativeFilesystemPath(grant.Path), security);
            }
        }

        private static void WithAclLock(Action action)
        {
            // AddAccessRule + SetAccessControl is a read/modify/write pair.
            // Serialize our launchers so another call's SID cannot be lost.
            using (var mutex = new Mutex(false, "Local\\MewCodeLpacAcl_v1"))
            {
                bool owned = false;
                try
                {
                    try { owned = mutex.WaitOne(10000); }
                    catch (AbandonedMutexException) { owned = true; }
                    if (!owned) throw new TimeoutException("Bounded LPAC ACL transaction wait expired.");
                    action();
                }
                finally { if (owned) mutex.ReleaseMutex(); }
            }
        }

        private static void CreateAndConfigureJob(LaunchState state)
        {
            state.Job = CreateJobObject(IntPtr.Zero, null);
            if (state.Job == IntPtr.Zero) throw LastWin32("CreateJobObject failed.");
            state.JobCreated = true;
            var limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            if (!SetInformationJobObject(state.Job, JobObjectExtendedLimitInformation, ref limits,
                (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION))))
                throw LastWin32("SetInformationJobObject(KILL_ON_JOB_CLOSE) failed.");
            state.JobConfigured = true;
        }

        private static void CreateStandardHandles(LaunchState state)
        {
            var attributes = new SECURITY_ATTRIBUTES();
            attributes.nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
            attributes.lpSecurityDescriptor = IntPtr.Zero;
            attributes.bInheritHandle = true;

            if (!CreatePipe(out state.StdOutRead, out state.StdOutWrite, ref attributes, 0))
                throw LastWin32("CreatePipe(stdout) failed.");
            if (!SetHandleInformation(state.StdOutRead, HandleFlagInherit, 0))
                throw LastWin32("SetHandleInformation(stdout read) failed.");
            if (!CreatePipe(out state.StdErrRead, out state.StdErrWrite, ref attributes, 0))
                throw LastWin32("CreatePipe(stderr) failed.");
            if (!SetHandleInformation(state.StdErrRead, HandleFlagInherit, 0))
                throw LastWin32("SetHandleInformation(stderr read) failed.");
            state.StdIn = CreateFileW("NUL", GenericRead, FileShareRead | FileShareWrite,
                ref attributes, OpenExisting, FileAttributeNormal, IntPtr.Zero);
            if (state.StdIn == InvalidHandleValue || state.StdIn == IntPtr.Zero)
            {
                state.StdIn = IntPtr.Zero;
                throw LastWin32("Opening NUL for child stdin failed.");
            }
        }

        private static void BuildProcessAttributes(ValidatedRequest request, LaunchState state)
        {
            int count = request.Lpac ? 3 : 2;
            IntPtr size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, count, 0, ref size);
            int sizingError = Marshal.GetLastWin32Error();
            if (size == IntPtr.Zero || sizingError != ErrorInsufficientBuffer)
                throw new Win32Exception(sizingError, "InitializeProcThreadAttributeList sizing failed.");
            state.AttributeList = Marshal.AllocHGlobal(size);
            if (!InitializeProcThreadAttributeList(state.AttributeList, count, 0, ref size))
                throw LastWin32("InitializeProcThreadAttributeList failed.");
            state.AttributeListInitialized = true;

            var capabilities = new SECURITY_CAPABILITIES();
            capabilities.AppContainerSid = state.PackageSid;
            capabilities.Capabilities = state.CapabilityAttributesMemory;
            capabilities.CapabilityCount = state.CapabilityCount;
            capabilities.Reserved = 0;
            state.SecurityCapabilitiesMemory = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(SECURITY_CAPABILITIES)));
            Marshal.StructureToPtr(capabilities, state.SecurityCapabilitiesMemory, false);
            if (!UpdateProcThreadAttribute(state.AttributeList, 0,
                new UIntPtr(ProcThreadAttributeSecurityCapabilities), state.SecurityCapabilitiesMemory,
                (IntPtr)Marshal.SizeOf(typeof(SECURITY_CAPABILITIES)), IntPtr.Zero, IntPtr.Zero))
                throw LastWin32("Setting SECURITY_CAPABILITIES failed.");

            IntPtr[] handles = new IntPtr[] { state.StdIn, state.StdOutWrite, state.StdErrWrite };
            state.HandleListMemory = Marshal.AllocHGlobal(IntPtr.Size * handles.Length);
            Marshal.Copy(handles, 0, state.HandleListMemory, handles.Length);
            if (!UpdateProcThreadAttribute(state.AttributeList, 0,
                new UIntPtr(ProcThreadAttributeHandleList), state.HandleListMemory,
                (IntPtr)(IntPtr.Size * handles.Length), IntPtr.Zero, IntPtr.Zero))
                throw LastWin32("Setting explicit child handle allowlist failed.");

            if (request.Lpac)
            {
                state.LpacPolicyMemory = Marshal.AllocHGlobal(sizeof(uint));
                Marshal.WriteInt32(state.LpacPolicyMemory, unchecked((int)ProcessCreationAllApplicationPackagesOptOut));
                if (!UpdateProcThreadAttribute(state.AttributeList, 0,
                    new UIntPtr(ProcThreadAttributeAllApplicationPackagesPolicy), state.LpacPolicyMemory,
                    (IntPtr)sizeof(uint), IntPtr.Zero, IntPtr.Zero))
                    throw LastWin32("Setting LPAC ALL_APPLICATION_PACKAGES opt-out policy failed.");
            }
        }

        private static string BuildEnvironmentBlock(Dictionary<string, string> environment)
        {
            var keys = new List<string>(environment.Keys);
            keys.Sort(StringComparer.OrdinalIgnoreCase);
            var block = new StringBuilder();
            foreach (string key in keys)
                block.Append(key).Append('=').Append(environment[key]).Append('\0');
            block.Append('\0');
            if (keys.Count == 0) block.Append('\0');
            if (block.Length > 32767)
                throw new InvalidDataException("Exact child environment exceeds the Windows environment block limit.");
            return block.ToString();
        }

        private static string BuildCommandLine(string executable, string[] arguments)
        {
            var command = new StringBuilder();
            command.Append(QuoteWindowsArgument(executable));
            foreach (string argument in arguments)
            {
                command.Append(' ').Append(QuoteWindowsArgument(argument));
            }
            return command.ToString();
        }

        private static string QuoteWindowsArgument(string value)
        {
            bool needsQuotes = value.Length == 0;
            for (int i = 0; i < value.Length && !needsQuotes; i++)
                needsQuotes = value[i] == ' ' || value[i] == '\t' || value[i] == '"';
            if (!needsQuotes) return value;

            var result = new StringBuilder();
            result.Append('"');
            int slashes = 0;
            for (int i = 0; i < value.Length; i++)
            {
                char current = value[i];
                if (current == '\\')
                {
                    slashes++;
                    continue;
                }
                if (current == '"')
                {
                    result.Append('\\', slashes * 2 + 1).Append('"');
                    slashes = 0;
                    continue;
                }
                if (slashes != 0)
                {
                    result.Append('\\', slashes);
                    slashes = 0;
                }
                result.Append(current);
            }
            if (slashes != 0) result.Append('\\', slashes * 2);
            result.Append('"');
            return result.ToString();
        }

        private static void CloseChildSideHandlesInParent(LaunchState state)
        {
            CloseIfOpen(ref state.StdOutWrite);
            CloseIfOpen(ref state.StdErrWrite);
            CloseIfOpen(ref state.StdIn);
        }

        private static void StartCapture(CaptureBuffer capture)
        {
            capture.Thread = new Thread(delegate() { CaptureLoop(capture); });
            capture.Thread.IsBackground = true;
            capture.Thread.Name = "MewCode appcontainer pipe reader";
            capture.Thread.Start();
        }

        private static void CaptureLoop(CaptureBuffer capture)
        {
            capture.NativeThreadId = GetCurrentThreadId();
            byte[] buffer = new byte[8192];
            try
            {
                while (true)
                {
                    uint bytesRead;
                    bool ok = ReadFile(capture.Handle, buffer, (uint)buffer.Length, out bytesRead, IntPtr.Zero);
                    if (!ok)
                    {
                        int error = Marshal.GetLastWin32Error();
                        if (error != ErrorBrokenPipe && error != ErrorOperationAborted && error != ErrorInvalidHandle)
                            capture.Error = new Win32Exception(error).Message;
                        break;
                    }
                    if (bytesRead == 0) break;
                    capture.TotalBytes += bytesRead;
                    int remaining = capture.Limit - (int)capture.Bytes.Length;
                    int keep = (int)Math.Min((long)remaining, (long)bytesRead);
                    if (keep > 0) capture.Bytes.Write(buffer, 0, keep);
                    if (keep < bytesRead) capture.Truncated = true;
                }
            }
            catch (Exception ex)
            {
                capture.Error = ex.Message;
            }
            finally
            {
                capture.Complete = true;
            }
        }

        private static void JoinCapture(CaptureBuffer capture, ProbeReceipt receipt, string label)
        {
            if (capture == null || capture.Thread == null) return;
            if (capture.Thread.Join(DrainWaitMs))
            {
                capture.Joined = true;
                if (!String.IsNullOrEmpty(capture.Error) && !capture.ErrorReported)
                {
                    AddError(receipt, label + " capture error: " + capture.Error);
                    capture.ErrorReported = true;
                }
                return;
            }
            if (capture.NativeThreadId != 0)
            {
                IntPtr threadHandle = OpenThread(ThreadTerminate, false, capture.NativeThreadId);
                if (threadHandle != IntPtr.Zero)
                {
                    CancelSynchronousIo(threadHandle);
                    CloseHandle(threadHandle);
                }
            }
            capture.Joined = capture.Thread.Join(1000);
            if (!capture.Joined && !capture.ErrorReported)
            {
                AddError(receipt, label + " capture did not finish after job termination");
                capture.ErrorReported = true;
            }
            if (capture.Joined && !String.IsNullOrEmpty(capture.Error) && !capture.ErrorReported)
            {
                AddError(receipt, label + " capture error: " + capture.Error);
                capture.ErrorReported = true;
            }
        }

        private static void CopyCapture(CaptureBuffer capture, ProbeReceipt receipt, bool stdout)
        {
            if (capture == null || capture.Thread == null || !capture.Joined) return;
            byte[] bytes = capture.Bytes.ToArray();
            string value = Encoding.GetEncoding(capture.CodePage).GetString(bytes);
            // Windows tools can emit OEM/ANSI bytes. Keep the bounded raw
            // capture so the UTF-8 display view never destroys that evidence.
            string raw = Convert.ToBase64String(bytes);
            if (stdout)
            {
                receipt.stdout = value;
                receipt.stdout_base64 = raw;
                receipt.stdout_truncated = capture.Truncated;
            }
            else
            {
                receipt.stderr = value;
                receipt.stderr_base64 = raw;
                receipt.stderr_truncated = capture.Truncated;
            }
        }

        private static void TerminateJob(LaunchState state, ProbeReceipt receipt)
        {
            if (state.Job == IntPtr.Zero) return;
            bool success = TerminateJobObject(state.Job, 0xE0000001);
            int error = success ? 0 : Marshal.GetLastWin32Error();
            state.JobTerminateAttempted = true;
            state.JobTerminateSucceeded = state.JobTerminateSucceeded || success;
            if (!success)
                AddError(receipt, "TerminateJobObject failed: " + new Win32Exception(error).Message);
            ConfirmJobEmpty(state, receipt);
        }

        private static bool ConfirmJobEmpty(LaunchState state, ProbeReceipt receipt)
        {
            if (state.JobEmptyConfirmed) return true;
            if (state.Job == IntPtr.Zero) return false;
            if (state.JobEmptyTimer == null)
                state.JobEmptyTimer = Stopwatch.StartNew();
            int bytes = Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
            while (state.JobEmptyTimer.ElapsedMilliseconds <= CleanupWaitMs)
            {
                JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting;
                uint returned;
                if (!QueryInformationJobObject(state.Job, JobObjectBasicAccountingInformation,
                    out accounting, (uint)bytes, out returned))
                {
                    if (!state.JobEmptyErrorReported)
                    {
                        AddError(receipt, "QueryInformationJobObject(accounting) failed: " +
                            new Win32Exception(Marshal.GetLastWin32Error()).Message);
                        state.JobEmptyErrorReported = true;
                    }
                    return false;
                }
                if (accounting.ActiveProcesses == 0)
                {
                    state.JobEmptyConfirmed = true;
                    return true;
                }
                Thread.Sleep(50);
            }
            if (!state.JobEmptyErrorReported)
            {
                AddError(receipt, "Job still had active processes after the bounded cleanup wait.");
                state.JobEmptyErrorReported = true;
            }
            return false;
        }

        private static void WaitForRootCleanup(LaunchState state, ProbeReceipt receipt)
        {
            if (state.Process == IntPtr.Zero) return;
            uint wait = WaitForSingleObject(state.Process, CleanupWaitMs);
            if (wait == WaitObject0)
            {
                state.RootCompleted = true;
                uint exitCode;
                if (GetExitCodeProcess(state.Process, out exitCode)) receipt.exit_code = unchecked((int)exitCode);
            }
            else
            {
                AddError(receipt, wait == WaitTimeout ? "root process did not exit after job termination" : "waiting for root cleanup failed");
            }
        }

        private static void CleanupChildAndJob(LaunchState state, ProbeReceipt receipt)
        {
            if (state.ProcessCreated && !state.RootCompleted)
            {
                if (state.ProcessAssigned && state.Job != IntPtr.Zero)
                {
                    if (!state.JobTerminateSucceeded || !state.JobEmptyConfirmed) TerminateJob(state, receipt);
                    WaitForRootCleanup(state, receipt);
                }
                else if (state.Process != IntPtr.Zero)
                {
                    if (!TerminateProcess(state.Process, 0xE0000002))
                    {
                        int error = Marshal.GetLastWin32Error();
                        // It may already have exited between checks; the bounded wait decides cleanup status.
                        AddError(receipt, "TerminateProcess for unassigned suspended child failed: " + new Win32Exception(error).Message);
                    }
                    uint wait = WaitForSingleObject(state.Process, CleanupWaitMs);
                    state.UnassignedProcessCleanupOk = wait == WaitObject0;
                    state.RootCompleted = state.UnassignedProcessCleanupOk;
                    if (!state.UnassignedProcessCleanupOk)
                        AddError(receipt, "unassigned child process cleanup could not be confirmed");
                }
            }

            if (state.Job != IntPtr.Zero)
            {
                if (!state.JobTerminateSucceeded) TerminateJob(state, receipt);
                else if (!state.JobEmptyConfirmed) ConfirmJobEmpty(state, receipt);
            }

            if (state.Job != IntPtr.Zero)
            {
                bool closed = CloseHandle(state.Job);
                state.JobClosed = closed;
                state.Job = IntPtr.Zero;
                if (!closed) AddError(receipt, "CloseHandle(job) failed: " + new Win32Exception(Marshal.GetLastWin32Error()).Message);
            }
            receipt.job_cleanup_ok = (!state.JobCreated && !state.ProcessAssigned) ||
                (state.JobClosed && state.JobTerminateSucceeded && state.JobEmptyConfirmed && state.UnassignedProcessCleanupOk);

            if (state.Process != IntPtr.Zero && !state.RootCompleted)
            {
                uint wait = WaitForSingleObject(state.Process, CleanupWaitMs);
                if (wait == WaitObject0)
                {
                    state.RootCompleted = true;
                    uint exitCode;
                    if (!receipt.exit_code.HasValue && GetExitCodeProcess(state.Process, out exitCode))
                        receipt.exit_code = unchecked((int)exitCode);
                }
                else
                {
                    receipt.job_cleanup_ok = false;
                    AddError(receipt, "root process exit was not confirmed after closing its job");
                }
            }
        }

        private static void CleanupAclGrants(LaunchState state, ValidatedRequest request, ProbeReceipt receipt)
        {
            // Recursive ACL propagation is never attempted across an
            // unverified child-created link. Retain the private scratch
            // evidence and report cleanup unknown instead of following it.
            if (request != null && request.StagedWorkspace != null)
            {
                try
                {
                    if (state.ProcessCreated && !state.JobEmptyConfirmed)
                        throw new InvalidDataException("Staged job cleanup is not confirmed.");
                    ValidateStagedTree(request.StagedWorkspace);
                    if (request.StagedRuntimeRoots != null)
                        foreach (string root in request.StagedRuntimeRoots) ValidateStagedTree(root);
                }
                catch (Exception error)
                {
                    receipt.acl_cleanup_ok = false;
                    AddError(receipt, "Staged ACL cleanup withheld: " + Describe(error));
                    return;
                }
            }
            bool ok = true;
            for (int i = state.AppliedGrants.Count - 1; i >= 0; i--)
            {
                PathGrant grant = state.AppliedGrants[i];
                try
                {
                    // Recheck every component immediately before path-based ACL access; never follow a replacement reparse point.
                    CheckNoReparseAncestors(grant.Path, "ACL cleanup target");
                    if (request == null || (!request.RuntimeRequest && !IsStrictlyBeneath(grant.Path, request.FixtureRoot)))
                        throw new InvalidDataException("ACL cleanup target is no longer beneath fixture_root.");
                    FileAttributes attributes = File.GetAttributes(NativeFilesystemPath(grant.Path));
                    bool isDirectory = (attributes & FileAttributes.Directory) != 0;
                    if (isDirectory != grant.IsDirectory)
                        throw new InvalidDataException("ACL cleanup target type changed.");
                    if (grant.IsDirectory)
                    {
                        DirectorySecurity security = Directory.GetAccessControl(NativeFilesystemPath(grant.Path));
                        security.RemoveAccessRuleSpecific(grant.Rule);
                        Directory.SetAccessControl(NativeFilesystemPath(grant.Path), security);
                    }
                    else
                    {
                        FileSecurity security = File.GetAccessControl(NativeFilesystemPath(grant.Path));
                        security.RemoveAccessRuleSpecific(grant.Rule);
                        File.SetAccessControl(NativeFilesystemPath(grant.Path), security);
                    }
                }
                catch (Exception ex)
                {
                    ok = false;
                    AddError(receipt, "ACL cleanup failed for an exact fixture path: " + Describe(ex));
                }
            }
            receipt.acl_cleanup_ok = ok;
        }

        private static void CleanupProfile(LaunchState state, ProbeReceipt receipt)
        {
            if (state.ProfileCreated && state.ProcessCreated &&
                (!state.RootCompleted || !state.JobClosed || !state.JobEmptyConfirmed))
            {
                receipt.profile_cleanup_ok = false;
                AddError(receipt, "AppContainer profile retained because the child job was not confirmed empty and closed.");
                return;
            }
            if (state.PackageSid != IntPtr.Zero)
            {
                FreeSid(state.PackageSid);
                state.PackageSid = IntPtr.Zero;
            }
            if (!state.ProfileCreated) return;
            int hr = unchecked((int)0x80004005);
            for (int attempt = 0; attempt < 3; attempt++)
            {
                hr = DeleteAppContainerProfile(state.ProfileName);
                if (hr >= 0)
                {
                    state.ProfileCreated = false;
                    receipt.profile_cleanup_ok = true;
                    return;
                }
                if (attempt < 2) Thread.Sleep(100);
            }
            receipt.profile_cleanup_ok = false;
            AddError(receipt, "DeleteAppContainerProfile failed after bounded retries: " + new Win32Exception(hr).Message);
        }

        private static void CleanupNativeResources(LaunchState state, ProbeReceipt receipt)
        {
            if (state.AttributeListInitialized && state.AttributeList != IntPtr.Zero)
                DeleteProcThreadAttributeList(state.AttributeList);
            if (state.AttributeList != IntPtr.Zero) Marshal.FreeHGlobal(state.AttributeList);
            if (state.SecurityCapabilitiesMemory != IntPtr.Zero) Marshal.FreeHGlobal(state.SecurityCapabilitiesMemory);
            if (state.CapabilityAttributesMemory != IntPtr.Zero) Marshal.FreeHGlobal(state.CapabilityAttributesMemory);
            for (int i = state.DerivedCapabilities.Count - 1; i >= 0; i--)
            {
                DerivedCapabilitySids derived = state.DerivedCapabilities[i];
                FreeDerivedSidArray(ref derived.GroupSids, ref derived.GroupSidCount,
                    derived.Name + " group SID", receipt);
                FreeDerivedSidArray(ref derived.CapabilitySids, ref derived.CapabilitySidCount,
                    derived.Name + " capability SID", receipt);
            }
            if (state.LpacPolicyMemory != IntPtr.Zero) Marshal.FreeHGlobal(state.LpacPolicyMemory);
            if (state.HandleListMemory != IntPtr.Zero) Marshal.FreeHGlobal(state.HandleListMemory);
            if (state.EnvironmentMemory != IntPtr.Zero) Marshal.FreeHGlobal(state.EnvironmentMemory);

            CloseIfOpen(ref state.StdIn);
            CloseIfOpen(ref state.StdOutWrite);
            CloseIfOpen(ref state.StdErrWrite);
            CloseIfOpen(ref state.StdOutRead);
            CloseIfOpen(ref state.StdErrRead);
            CloseIfOpen(ref state.PrimaryThread);
            CloseIfOpen(ref state.Process);
            CloseIfOpen(ref state.CancelEvent);
        }

        private static void FreeDerivedSidArray(ref IntPtr sidArray, ref uint sidCount, string label,
            ProbeReceipt receipt)
        {
            if (sidArray == IntPtr.Zero)
            {
                sidCount = 0;
                return;
            }

            UIntPtr allocationSize = LocalSize(sidArray);
            ulong allocationBytes = allocationSize.ToUInt64();
            ulong requiredBytes = (ulong)sidCount * (ulong)IntPtr.Size;
            if (allocationBytes == 0 || allocationBytes > MaximumDerivedSidArrayBytes ||
                requiredBytes > allocationBytes)
            {
                AddError(receipt, "Cleanup could not safely enumerate the " + label + " array returned by Windows.");
            }
            else
            {
                for (uint i = 0; i < sidCount; i++)
                {
                    IntPtr sid = Marshal.ReadIntPtr(sidArray, checked((int)(i * (uint)IntPtr.Size)));
                    if (sid == IntPtr.Zero) continue;
                    if (LocalFree(sid) != IntPtr.Zero)
                        AddError(receipt, "LocalFree failed for a derived " + label + ".");
                }
            }

            if (LocalFree(sidArray) != IntPtr.Zero)
                AddError(receipt, "LocalFree failed for the derived " + label + " pointer array.");
            sidArray = IntPtr.Zero;
            sidCount = 0;
        }

        [DllImport("kernel32.dll")]
        private static extern uint GetOEMCP();

        private static void CloseIfOpen(ref IntPtr handle)
        {
            if (handle != IntPtr.Zero && handle != InvalidHandleValue)
                CloseHandle(handle);
            handle = IntPtr.Zero;
        }

        private static void AddError(ProbeReceipt receipt, string error)
        {
            if (String.IsNullOrEmpty(error)) return;
            receipt.error = String.IsNullOrEmpty(receipt.error) ? error : receipt.error + "; " + error;
        }

        private static string Describe(Exception exception)
        {
            var win32 = exception as Win32Exception;
            if (win32 != null) return win32.Message + " (Win32 " + win32.NativeErrorCode.ToString() + ": " + new Win32Exception(win32.NativeErrorCode).Message + ")";
            return exception.GetType().Name + ": " + exception.Message;
        }

        private static Win32Exception LastWin32(string message)
        {
            int error = Marshal.GetLastWin32Error();
            return new Win32Exception(error, message);
        }

        private static JavaScriptSerializer NewSerializer()
        {
            // Two capped 1 MiB streams can expand to 12 MiB after JSON escaping,
            // plus their base64 originals. Input remains separately capped.
            return new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 };
        }

        private const string NetworkProfilePrefix = "mewcode-network-v1-";
        private const int HResultAlreadyExists = unchecked((int)0x800700B7);

        internal static void EnsureHostProcessToken()
        {
            IntPtr token = OpenHostProcessToken();
            try
            {
                int isAppContainer = 0;
                uint returned;
                if (!GetTokenInformation(token, 29, out isAppContainer, sizeof(int), out returned))
                    throw LastWin32("GetTokenInformation(TokenIsAppContainer) failed for the host caller.");
                if (isAppContainer != 0)
                    throw new InvalidOperationException("An AppContainer caller cannot invoke the network host protocol.");
            }
            finally { CloseHandle(token); }
        }

        private static string CurrentUserSid()
        {
            IntPtr token = OpenHostProcessToken();
            try
            {
                using (WindowsIdentity identity = new WindowsIdentity(token))
                {
                    if (identity.User == null)
                        throw new InvalidOperationException("The host user SID is unavailable.");
                    // Use the process primary token, not a potentially impersonated thread token.
                    return new SecurityIdentifier(identity.User.Value).Value;
                }
            }
            finally { CloseHandle(token); }
        }

        private static IntPtr OpenHostProcessToken()
        {
            IntPtr token = IntPtr.Zero;
            using (Process current = Process.GetCurrentProcess())
            {
                if (!OpenProcessToken(current.Handle, 0x0008, out token))
                    throw LastWin32("OpenProcessToken(TOKEN_QUERY) failed for the host caller.");
            }
            return token;
        }

        private static string NetworkProfileName(string userSid)
        {
            string normalizedSid = new SecurityIdentifier(userSid).Value;
            using (SHA256 sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(Encoding.UTF8.GetBytes(normalizedSid));
                return NetworkProfilePrefix + BitConverter.ToString(digest).Replace("-", "")
                    .ToLowerInvariant().Substring(0, 16);
            }
        }

        internal static NetworkProfileDetails ProvisionNetworkProfile()
        {
            string userSid = CurrentUserSid();
            string profileName = NetworkProfileName(userSid);
            IntPtr returnedSid = IntPtr.Zero;
            int hr = CreateAppContainerProfile(profileName, "MewCode network runtime",
                "Per-user AppContainer profile for network-enabled LPAC runtime invocations",
                IntPtr.Zero, 0, out returnedSid);
            string createdSid = null;
            try { if (returnedSid != IntPtr.Zero) createdSid = new SecurityIdentifier(returnedSid).Value; }
            finally { if (returnedSid != IntPtr.Zero) FreeSid(returnedSid); }
            if (hr < 0 && hr != HResultAlreadyExists)
                throw new Win32Exception(hr, "CreateAppContainerProfile(network profile) failed.");

            DerivedNetworkProfile verified = VerifyExistingNetworkProfile(profileName, userSid);
            try
            {
                if (createdSid != null && !String.Equals(createdSid, verified.ProfileSid, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Provisioned profile SID does not match its derived identity.");
                return new NetworkProfileDetails { ProfileName = verified.ProfileName,
                    ProfileSid = verified.ProfileSid, UserSid = verified.UserSid };
            }
            finally { if (verified.PackageSid != IntPtr.Zero) FreeSid(verified.PackageSid); }
        }

        internal static NetworkProfileDetails NetworkProfileStatus()
        {
            string userSid = CurrentUserSid();
            string profileName = NetworkProfileName(userSid);
            // DeriveAppContainerSidFromAppContainerName is a deterministic
            // identity conversion; this status operation never registers a profile.
            return new NetworkProfileDetails { ProfileName = profileName,
                ProfileSid = DeriveNetworkProfileSid(profileName), UserSid = userSid };
        }

        private static DerivedNetworkProfile VerifyExistingNetworkProfile(string profileName, string userSid)
        {
            if (!String.Equals(profileName, NetworkProfileName(userSid), StringComparison.Ordinal))
                throw new InvalidDataException("Network profile name is not the fixed current-user identity.");
            IntPtr packageSid = IntPtr.Zero;
            int hr = DeriveAppContainerSidFromAppContainerName(profileName, out packageSid);
            if (hr < 0 || packageSid == IntPtr.Zero)
            {
                if (packageSid != IntPtr.Zero) FreeSid(packageSid);
                throw new Win32Exception(hr, "DeriveAppContainerSidFromAppContainerName failed.");
            }
            try
            {
                string profileSid = new SecurityIdentifier(packageSid).Value;
                string mappingPath = @"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings\" + profileSid;
                using (RegistryKey mapping = Registry.CurrentUser.OpenSubKey(mappingPath, false))
                {
                    if (mapping == null)
                        throw new InvalidDataException("The fixed network AppContainer profile mapping does not exist.");
                    object monikerValue = mapping.GetValue("Moniker", null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                    if (!String.Equals(monikerValue as string, profileName, StringComparison.Ordinal))
                        throw new InvalidDataException("The existing AppContainer mapping is not owned by the fixed MewCode network profile name.");
                }

                IntPtr folderPointer = IntPtr.Zero;
                int folderHr = GetAppContainerFolderPath(profileSid, out folderPointer);
                if (folderHr < 0)
                {
                    if (folderPointer != IntPtr.Zero) CoTaskMemFree(folderPointer);
                    throw new Win32Exception(folderHr, "GetAppContainerFolderPath failed for the existing network profile.");
                }
                if (folderPointer == IntPtr.Zero)
                    throw new InvalidDataException("GetAppContainerFolderPath returned no path for the existing network profile.");
                string folderPath;
                try { folderPath = Marshal.PtrToStringUni(folderPointer); }
                finally { CoTaskMemFree(folderPointer); }
                if (String.IsNullOrEmpty(folderPath) || !Path.IsPathRooted(folderPath) || !Directory.Exists(folderPath))
                    throw new InvalidDataException("The existing network AppContainer profile folder is unavailable.");

                return new DerivedNetworkProfile { ProfileName = profileName, ProfileSid = profileSid,
                    UserSid = userSid, PackageSid = packageSid };
            }
            catch
            {
                FreeSid(packageSid);
                throw;
            }
        }

        private static void InitializeNetworkProfileIdentity(LaunchState state, ProbeReceipt receipt)
        {
            EnsureHostProcessToken();
            string userSid = CurrentUserSid();
            string profileName = NetworkProfileName(userSid);
            string profileSid = DeriveNetworkProfileSid(profileName);
            state.RuntimeNetwork = true;
            state.ProfileName = profileName;
            state.UserSid = userSid;
            state.ProfileSid = profileSid;
            receipt.profile_name = profileName;
            receipt.network = new NetworkProfileReceipt { runtime_network = true,
                persistent_profile = false, profile_name = profileName,
                profile_sid = profileSid, user_sid = userSid };
        }

        private static void CreateNetworkExecutionProfile(ValidatedRequest request,
            LaunchState state, ProbeReceipt receipt)
        {
            if (!request.RuntimeNetwork || !state.RuntimeNetwork || receipt.network == null)
                throw new InvalidDataException("Network profile creation requires a validated network request identity.");
            string profileName = state.ProfileName;
            string userSid = state.UserSid;
            string expectedProfileSid = state.ProfileSid;

            int profileHr = CreateAppContainerProfile(profileName, "MewCode network runtime",
                "Per-user AppContainer profile for network-enabled LPAC runtime invocations",
                state.CapabilityAttributesMemory, state.CapabilityCount, out state.PackageSid);
            if (profileHr == HResultAlreadyExists)
            {
                // Until the prior fixed-name profile is removed and the fresh
                // invocation profile is later deleted, cleanup is not proven.
                receipt.profile_cleanup_ok = false;
                if (state.PackageSid != IntPtr.Zero)
                {
                    FreeSid(state.PackageSid);
                    state.PackageSid = IntPtr.Zero;
                }
                DerivedNetworkProfile existing = VerifyExistingNetworkProfile(profileName, userSid);
                try
                {
                    if (!String.Equals(existing.ProfileSid, expectedProfileSid, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("Existing network profile SID differs from the fixed derived identity.");
                }
                finally { if (existing.PackageSid != IntPtr.Zero) FreeSid(existing.PackageSid); }

                int deleteHr = DeleteAppContainerProfile(profileName);
                if (deleteHr < 0)
                    throw new Win32Exception(deleteHr, "Could not remove the prior MewCode network profile before recreation.");
                profileHr = CreateAppContainerProfile(profileName, "MewCode network runtime",
                    "Per-user AppContainer profile for network-enabled LPAC runtime invocations",
                    state.CapabilityAttributesMemory, state.CapabilityCount, out state.PackageSid);
            }
            if (profileHr < 0)
                throw new Win32Exception(profileHr, "CreateAppContainerProfile(network execution profile) failed.");

            state.ProfileCreated = true;
            if (state.PackageSid == IntPtr.Zero)
                throw new InvalidDataException("CreateAppContainerProfile returned no network profile SID.");
            string actualProfileSid = new SecurityIdentifier(state.PackageSid).Value;
            state.ProfileSid = actualProfileSid;
            receipt.network.profile_sid = actualProfileSid;
            DerivedNetworkProfile active = VerifyExistingNetworkProfile(profileName, userSid);
            try
            {
                if (!String.Equals(actualProfileSid, active.ProfileSid, StringComparison.OrdinalIgnoreCase) ||
                    (expectedProfileSid != null && !String.Equals(active.ProfileSid, expectedProfileSid,
                        StringComparison.OrdinalIgnoreCase)))
                    throw new InvalidDataException("Created network profile SID does not match the fixed derived identity.");
                state.ProfileSid = active.ProfileSid;
                receipt.network.profile_sid = active.ProfileSid;
            }
            finally { if (active.PackageSid != IntPtr.Zero) FreeSid(active.PackageSid); }
        }

        private static string DeriveNetworkProfileSid(string profileName)
        {
            IntPtr packageSid = IntPtr.Zero;
            int hr = DeriveAppContainerSidFromAppContainerName(profileName, out packageSid);
            if (hr < 0 || packageSid == IntPtr.Zero)
            {
                if (packageSid != IntPtr.Zero) FreeSid(packageSid);
                throw new Win32Exception(hr, "Could not derive the fixed network AppContainer SID.");
            }
            try { return new SecurityIdentifier(packageSid).Value; }
            finally { FreeSid(packageSid); }
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public uint dwX;
            public uint dwY;
            public uint dwXSize;
            public uint dwYSize;
            public uint dwXCountChars;
            public uint dwYCountChars;
            public uint dwFillAttribute;
            public uint dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct STARTUPINFOEX
        {
            public STARTUPINFO StartupInfo;
            public IntPtr lpAttributeList;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public uint dwProcessId;
            public uint dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SECURITY_ATTRIBUTES
        {
            public int nLength;
            public IntPtr lpSecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool bInheritHandle;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SECURITY_CAPABILITIES
        {
            public IntPtr AppContainerSid;
            public IntPtr Capabilities;
            public uint CapabilityCount;
            public uint Reserved;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SID_AND_ATTRIBUTES
        {
            public IntPtr Sid;
            public uint Attributes;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        {
            public long TotalUserTime;
            public long TotalKernelTime;
            public long ThisPeriodTotalUserTime;
            public long ThisPeriodTotalKernelTime;
            public uint TotalPageFaults;
            public uint TotalProcesses;
            public uint ActiveProcesses;
            public uint TotalTerminatedProcesses;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct NATIVE_FILETIME
        {
            public uint LowDateTime;
            public uint HighDateTime;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct BY_HANDLE_FILE_INFORMATION
        {
            public uint FileAttributes;
            public NATIVE_FILETIME CreationTime;
            public NATIVE_FILETIME LastAccessTime;
            public NATIVE_FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct GENERIC_MAPPING
        {
            public uint GenericRead;
            public uint GenericWrite;
            public uint GenericExecute;
            public uint GenericAll;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LUID
        {
            public uint LowPart;
            public int HighPart;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LUID_AND_ATTRIBUTES
        {
            public LUID Luid;
            public uint Attributes;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PRIVILEGE_SET
        {
            public uint PrivilegeCount;
            public uint Control;
            public LUID_AND_ATTRIBUTES Privilege;
        }

        [DllImport("userenv.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern int CreateAppContainerProfile(string pszAppContainerName, string pszDisplayName,
            string pszDescription, IntPtr pCapabilities, uint dwCapabilityCount, out IntPtr ppSidAppContainerSid);

        [DllImport("userenv.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern int DeleteAppContainerProfile(string pszAppContainerName);

        [DllImport("userenv.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern int DeriveAppContainerSidFromAppContainerName(string appContainerName,
            out IntPtr appContainerSid);

        [DllImport("userenv.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern int GetAppContainerFolderPath(string appContainerSid, out IntPtr path);

        [DllImport("ole32.dll")]
        private static extern void CoTaskMemFree(IntPtr pv);

        [DllImport("KernelBase.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool DeriveCapabilitySidsFromName(string capabilityName,
            out IntPtr capabilityGroupSids, out uint capabilityGroupSidCount,
            out IntPtr capabilitySids, out uint capabilitySidCount);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern IntPtr FreeSid(IntPtr pSid);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint GetLengthSid(IntPtr pSid);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(IntPtr ProcessHandle, uint DesiredAccess, out IntPtr TokenHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool DuplicateToken(IntPtr ExistingTokenHandle, int ImpersonationLevel,
            out IntPtr DuplicateTokenHandle);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, EntryPoint = "ConvertStringSecurityDescriptorToSecurityDescriptorW", SetLastError = true)]
        private static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(string StringSecurityDescriptor,
            uint StringSDRevision, out IntPtr SecurityDescriptor, out uint SecurityDescriptorSize);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool AccessCheck(IntPtr pSecurityDescriptor, IntPtr ClientToken, uint DesiredAccess,
            ref GENERIC_MAPPING GenericMapping, IntPtr PrivilegeSet, ref uint PrivilegeSetLength,
            out uint GrantedAccess, [MarshalAs(UnmanagedType.Bool)] out bool AccessStatus);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetTokenInformation(IntPtr TokenHandle, int TokenInformationClass,
            out int TokenInformation, int TokenInformationLength, out uint ReturnLength);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetTokenInformation(IntPtr TokenHandle, int TokenInformationClass,
            IntPtr TokenInformation, uint TokenInformationLength, out uint ReturnLength);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool InitializeProcThreadAttributeList(IntPtr lpAttributeList, int dwAttributeCount,
            uint dwFlags, ref IntPtr lpSize);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern void DeleteProcThreadAttributeList(IntPtr lpAttributeList);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool UpdateProcThreadAttribute(IntPtr lpAttributeList, uint dwFlags, UIntPtr attribute,
            IntPtr lpValue, IntPtr cbSize, IntPtr lpPreviousValue, IntPtr lpReturnSize);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcessW(string lpApplicationName, StringBuilder lpCommandLine,
            IntPtr lpProcessAttributes, IntPtr lpThreadAttributes, bool bInheritHandles, uint dwCreationFlags,
            IntPtr lpEnvironment, string lpCurrentDirectory, ref STARTUPINFOEX lpStartupInfo,
            out PROCESS_INFORMATION lpProcessInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr hJob, uint JobObjectInformationClass,
            ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION lpJobObjectInformation, uint cbJobObjectInformationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool QueryInformationJobObject(IntPtr hJob, uint JobObjectInformationClass,
            out JOBOBJECT_BASIC_ACCOUNTING_INFORMATION lpJobObjectInformation, uint cbJobObjectInformationLength,
            out uint lpReturnLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr hThread);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForMultipleObjects(uint nCount, [In] IntPtr[] lpHandles,
            [MarshalAs(UnmanagedType.Bool)] bool bWaitAll, uint dwMilliseconds);

        [DllImport("kernel32.dll", EntryPoint = "OpenEventW", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr OpenEventW(uint dwDesiredAccess, [MarshalAs(UnmanagedType.Bool)] bool bInheritHandle,
            string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CreatePipe(out IntPtr hReadPipe, out IntPtr hWritePipe,
            ref SECURITY_ATTRIBUTES lpPipeAttributes, uint nSize);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetHandleInformation(IntPtr hObject, uint dwMask, uint dwFlags);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateFileW(string lpFileName, uint dwDesiredAccess, uint dwShareMode,
            ref SECURITY_ATTRIBUTES lpSecurityAttributes, uint dwCreationDisposition, uint dwFlagsAndAttributes,
            IntPtr hTemplateFile);

        [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr OpenFileForLinkCount(string lpFileName, uint dwDesiredAccess, uint dwShareMode,
            IntPtr lpSecurityAttributes, uint dwCreationDisposition, uint dwFlagsAndAttributes, IntPtr hTemplateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(IntPtr hFile, out BY_HANDLE_FILE_INFORMATION lpFileInformation);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetWindowsDirectoryW(StringBuilder lpBuffer, uint uSize);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool ReadFile(IntPtr hFile, byte[] lpBuffer, uint nNumberOfBytesToRead,
            out uint lpNumberOfBytesRead, IntPtr lpOverlapped);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr LocalFree(IntPtr hMem);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern UIntPtr LocalSize(IntPtr hMem);

        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentThreadId();

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenThread(uint dwDesiredAccess, bool bInheritHandle, uint dwThreadId);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CancelSynchronousIo(IntPtr hThread);
    }
}
