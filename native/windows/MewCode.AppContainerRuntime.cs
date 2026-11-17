// Host-only LPAC transport. Compiled with the shared launcher, with this entry
// point selected explicitly. There is no fixture-sentinel authorization here.
// Production activation remains disabled in the Python policy/runtime gates.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace MewCode.Native.Windows
{
    public sealed class RuntimeRequest
    {
        public string schema { get; set; }
        public string request_id { get; set; }
        public string workspace { get; set; }
        public string executable { get; set; }
        public string[] arguments { get; set; }
        public string cwd { get; set; }
        public int timeout_ms { get; set; }
        public string cancel_event_name { get; set; }
        public bool? runtime_network { get; set; }
        public Dictionary<string, string> environment { get; set; }
        // Exact, pre-enumerated paths, not recursive directory authorizations.
        public string[] read_paths { get; set; }
        public string[] write_paths { get; set; }
    }

    // Versioned host envelope for execution from a private staged workspace.
    // The nested request keeps the exact-runtime request contract; duplicated
    // transport identity and timeout are checked against it before launch.
    public sealed class StagedRuntimeRequestEnvelope
    {
        public string schema { get; set; }
        public string request_id { get; set; }
        public int timeout_ms { get; set; }
        public RuntimeRequest request { get; set; }
        public string[] readonly_paths { get; set; }
        public string[] pinned_containers { get; set; }
        public string original_workspace { get; set; }
        // Optional host-private destination for a final cleanup-only fact.
        public string cleanup_receipt_path { get; set; }
    }

    public sealed class NetworkTransportRequest
    {
        public string schema { get; set; }
        public string request_base64 { get; set; }
        public Dictionary<string, string> proxy_environment { get; set; }
    }

    public static class MewCodeAppContainerRuntime
    {
        public static int Main(string[] args) { return MewCodeAppContainerProbe.RuntimeMain(args); }
    }

    public static partial class MewCodeAppContainerProbe
    {
        private const int MaxCleanupEvidenceBytes = 16 * 1024;
        private const int MaxNetworkTransportBytes = 2 * 1024 * 1024;

        internal static int RuntimeMain(string[] args)
        {
            var receipt = new ProbeReceipt { stdout = "", stderr = "", error = "",
                capabilities = new string[0], job_cleanup_ok = true,
                profile_cleanup_ok = true, acl_cleanup_ok = true };
            string requestId = null;
            bool networkProtocol = false;
            Mutex networkLock = null;
            try
            {
                if (args.Length == 1 && args[0] == "--network-profile-status-v1")
                {
                    EnsureHostProcessToken();
                    NetworkProfileDetails profile = NetworkProfileStatus();
                    Console.OutputEncoding = new UTF8Encoding(false);
                    Console.WriteLine(NewSerializer().Serialize(new {
                        schema = "mewcode-lpac-network-profile-v1",
                        profile_name = profile.ProfileName,
                        profile_sid = profile.ProfileSid,
                        user_sid = profile.UserSid
                    }));
                    return 0;
                }
                if (args.Length == 1 && args[0] == "--provision-network-profile-v1")
                {
                    EnsureHostProcessToken();
                    networkLock = AcquireNetworkProfileLock();
                    NetworkProfileDetails profile = ProvisionNetworkProfile();
                    Console.OutputEncoding = new UTF8Encoding(false);
                    Console.WriteLine(NewSerializer().Serialize(new {
                        schema = "mewcode-lpac-network-profile-v1",
                        profile_name = profile.ProfileName,
                        profile_sid = profile.ProfileSid,
                        user_sid = profile.UserSid
                    }));
                    return 0;
                }
                if (args.Length != 1 ||
                    (args[0] != "--host-stdin-v1" && args[0] != "--host-network-stdin-v1"))
                    throw new InvalidDataException("Expected a supported host stdin protocol, never a workspace request file.");
                networkProtocol = args[0] == "--host-network-stdin-v1";
                if (networkProtocol) {
                    EnsureHostProcessToken();
                    networkLock = AcquireNetworkProfileLock();
                }
                int maxInputBytes = networkProtocol ? MaxNetworkTransportBytes : MaxRequestBytes;
                var inputBytes = new byte[maxInputBytes + 1];
                int inputCount = 0, read;
                using (Stream input = Console.OpenStandardInput())
                {
                    while (inputCount < inputBytes.Length &&
                        (read = input.Read(inputBytes, inputCount, inputBytes.Length - inputCount)) > 0)
                        inputCount += read;
                }
                if (inputCount == 0 || inputCount > maxInputBytes)
                    throw new InvalidDataException("Invalid request size.");
                byte[] requestBytes = inputBytes;
                int count = inputCount;
                Dictionary<string, string> proxyEnvironment = null;
                string payload = new UTF8Encoding(false, true).GetString(inputBytes, 0, inputCount);
                // Match StreamReader's former UTF-8 BOM handling while hashing
                // the original bytes below, including a possible BOM.
                if (payload.Length > 0 && payload[0] == '\uFEFF') payload = payload.Substring(1);
                if (networkProtocol)
                {
                    NetworkTransportRequest transport = NewSerializer().Deserialize<NetworkTransportRequest>(payload);
                    if (transport == null || transport.schema != "mewcode-lpac-network-transport-v1" ||
                        String.IsNullOrEmpty(transport.request_base64))
                        throw new InvalidDataException("Invalid network transport wrapper.");
                    try { requestBytes = Convert.FromBase64String(transport.request_base64); }
                    catch (FormatException) { throw new InvalidDataException("Network request bytes are not canonical base64."); }
                    if (requestBytes.Length == 0 || requestBytes.Length > MaxRequestBytes ||
                        !String.Equals(Convert.ToBase64String(requestBytes), transport.request_base64, StringComparison.Ordinal))
                        throw new InvalidDataException("Decoded network request exceeds its bound or is not canonical base64.");
                    count = requestBytes.Length;
                    payload = new UTF8Encoding(false, true).GetString(requestBytes, 0, count);
                    if (payload.Length > 0 && payload[0] == '\uFEFF') payload = payload.Substring(1);
                    proxyEnvironment = ValidateProxyEnvironment(transport.proxy_environment);
                }
                var staged = NewSerializer().Deserialize<StagedRuntimeRequestEnvelope>(payload);
                if (staged != null && staged.schema == StagedRuntimeSchema)
                {
                    var validated = ValidateStagedRuntimeRequest(staged);
                    if (validated.RuntimeNetwork != networkProtocol)
                        throw new InvalidDataException("The network request schema, runtime_network flag, and host protocol must match.");
                    if (networkProtocol) MergeNetworkEnvironment(validated, proxyEnvironment);
                    requestId = staged.request_id;
                    string requestSha256 = HashRequestBytes(requestBytes, count);
                    FileStream cleanupEvidence = OpenCleanupEvidence(staged, validated);
                    bool executeReturned = false;
                    try
                    {
                        Execute(null, receipt, validated);
                        executeReturned = true;
                    }
                    finally
                    {
                        if (cleanupEvidence != null)
                        {
                            if (!executeReturned)
                            {
                                // An escaped exception means cleanup completion is unknown.
                                receipt.job_cleanup_ok = false;
                                receipt.profile_cleanup_ok = false;
                                receipt.acl_cleanup_ok = false;
                            }
                            try
                            {
                                WriteCleanupEvidence(cleanupEvidence, requestId, requestSha256, receipt);
                            }
                            catch (Exception error)
                            {
                                receipt.job_cleanup_ok = false;
                                receipt.profile_cleanup_ok = false;
                                receipt.acl_cleanup_ok = false;
                                AddError(receipt, "Final cleanup evidence could not be persisted: " + Describe(error));
                            }
                            finally
                            {
                                try { cleanupEvidence.Dispose(); }
                                catch { /* Process teardown closes the non-inheritable handle as a last resort. */ }
                            }
                        }
                    }
                }
                else
                {
                    if (networkProtocol)
                        throw new InvalidDataException("The network protocol requires the existing staged-workspace envelope.");
                    var request = NewSerializer().Deserialize<RuntimeRequest>(payload);
                    var validated = ValidateRuntimeRequest(request);
                    if (validated.RuntimeNetwork)
                        throw new InvalidDataException("A network request requires --host-network-stdin-v1.");
                    requestId = request.request_id;
                    Execute(null, receipt, validated);
                }
            }
            catch (Exception error) { AddError(receipt, Describe(error)); }
            finally {
                if (networkLock != null) {
                    try { networkLock.ReleaseMutex(); } finally { networkLock.Dispose(); }
                }
            }
            Console.OutputEncoding = new UTF8Encoding(false);
            Console.WriteLine(NewSerializer().Serialize(new { schema = "mewcode-lpac-receipt-v1",
                request_id = requestId, backend = "mewcode-windows-lpac", receipt = ReceiptForSerialization(receipt) }));
            return String.IsNullOrEmpty(receipt.error) && !receipt.timed_out && !receipt.cancelled && receipt.exit_code.HasValue
                ? receipt.exit_code.Value : 1;
        }

        private static Mutex AcquireNetworkProfileLock()
        {
            var mutex = new Mutex(false, @"Global\MewCode.NetworkProfile." + NetworkProfileName(CurrentUserSid()));
            try {
                try {
                    if (!mutex.WaitOne(TimeSpan.FromSeconds(60)))
                        throw new InvalidDataException("Network AppContainer profile is still in use by another host invocation.");
                } catch (AbandonedMutexException) {
                    // A crashed holder can have left ACEs. The transaction recovery
                    // must certify cleanup before this SID can be reused.
                    mutex.ReleaseMutex();
                    throw new InvalidDataException("Network AppContainer lock was abandoned; host cleanup recovery is required.");
                }
                return mutex;
            } catch { mutex.Dispose(); throw; }
        }

        private static string HashRequestBytes(byte[] bytes, int count)
        {
            using (SHA256 sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(bytes, 0, count);
                return BitConverter.ToString(digest).Replace("-", "").ToLowerInvariant();
            }
        }

        private static FileStream OpenCleanupEvidence(StagedRuntimeRequestEnvelope envelope,
            ValidatedRequest request)
        {
            if (envelope.cleanup_receipt_path == null) return null;
            string path = ValidateAbsolutePath(envelope.cleanup_receipt_path, "cleanup_receipt_path");
            if (String.IsNullOrEmpty(request.StagedWorkspace))
                throw new InvalidDataException("Cleanup evidence requires a validated staged workspace.");
            string transaction = Path.GetDirectoryName(TrimSeparators(request.StagedWorkspace));
            if (String.IsNullOrEmpty(transaction))
                throw new InvalidDataException("Cleanup evidence has no private transaction parent.");
            string expected = Path.GetFullPath(Path.Combine(transaction, "cleanup", "receipt.json"));
            if (!String.Equals(TrimSeparators(path), TrimSeparators(expected), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Cleanup evidence must be transaction/cleanup/receipt.json.");

            string directory = ValidateExistingPath(Path.GetDirectoryName(path), "cleanup evidence directory", true);
            if (!String.Equals(TrimSeparators(directory), TrimSeparators(Path.GetDirectoryName(expected)),
                StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Cleanup evidence directory changed during validation.");
            if ((File.GetAttributes(directory) & FileAttributes.Device) != 0)
                throw new InvalidDataException("Cleanup evidence parent must be an ordinary directory.");

            string original = ValidateExistingPath(envelope.original_workspace, "original_workspace", true);
            if (PathsOverlap(path, original) || PathsOverlap(path, request.StagedWorkspace))
                throw new InvalidDataException("Cleanup evidence overlaps a protected workspace.");
            if (request.StagedRuntimeRoots != null)
                foreach (string root in request.StagedRuntimeRoots)
                    if (PathsOverlap(path, root))
                        throw new InvalidDataException("Cleanup evidence overlaps a private child runtime subtree.");
            foreach (PathGrant grant in request.Grants.Values)
                if (PathsOverlap(path, grant.Path))
                    throw new InvalidDataException("Cleanup evidence overlaps an authorized child path.");

            // This file is outside every child grant. FileStream creates a
            // non-inheritable handle by default; clear the flag explicitly too.
            var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
            if (!SetHandleInformation(stream.SafeFileHandle.DangerousGetHandle(), HandleFlagInherit, 0))
            {
                int error = Marshal.GetLastWin32Error();
                stream.Dispose();
                throw new IOException("Could not make cleanup evidence handle non-inheritable (Win32 " + error + ").");
            }
            return stream;
        }

        private static void WriteCleanupEvidence(FileStream stream, string requestId,
            string requestSha256, ProbeReceipt receipt)
        {
            string json = NewSerializer().Serialize(new {
                schema = "mewcode-native-cleanup-v1",
                request_id = requestId,
                request_sha256 = requestSha256,
                backend = "mewcode-windows-lpac",
                completed = true,
                job_cleanup_ok = receipt.job_cleanup_ok,
                profile_cleanup_ok = receipt.profile_cleanup_ok,
                acl_cleanup_ok = receipt.acl_cleanup_ok
            });
            byte[] bytes = new UTF8Encoding(false).GetBytes(json);
            if (bytes.Length == 0 || bytes.Length > MaxCleanupEvidenceBytes)
                throw new InvalidDataException("Final cleanup evidence exceeds its bounded format.");
            try
            {
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(true);
            }
            catch
            {
                // Best effort: a failed final write must not leave a readable
                // success-shaped record behind.
                try { stream.SetLength(0); stream.Flush(true); }
                catch { }
                throw;
            }
        }

        private static ValidatedRequest ValidateRuntimeRequest(RuntimeRequest request)
        {
            Guid requestGuid;
            bool networkRequest = request != null && request.schema == "mewcode-lpac-network-request-v1";
            if (request == null ||
                (networkRequest != (request.runtime_network == true)) ||
                (!networkRequest && request.runtime_network == true) ||
                (!networkRequest && request.schema != "mewcode-lpac-request-v1") ||
                !Guid.TryParseExact(request.request_id, "N", out requestGuid))
                throw new InvalidDataException("Invalid host request identity.");
            string workspace = ValidateExistingPath(request.workspace, "workspace", true);
            if (String.Equals(workspace, Path.GetPathRoot(workspace), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("A volume root is not a workspace.");
            string cwd = ValidateExistingPath(request.cwd, "cwd", true);
            if (!String.Equals(cwd, workspace, StringComparison.OrdinalIgnoreCase) && !IsStrictlyBeneath(cwd, workspace))
                throw new InvalidDataException("cwd is outside the frozen workspace.");
            string executable = ValidateExistingPath(request.executable, "executable", false);
            if (!IsTrustedSystemExecutable(executable))
                throw new InvalidDataException("The host entry point must be Windows PowerShell or CMD.");
            if (request.timeout_ms < 1000 || request.timeout_ms > 600000)
                throw new InvalidDataException("Invalid bounded runtime timeout.");
            if (request.arguments == null || request.arguments.Length > 32)
                throw new InvalidDataException("Invalid arguments.");
            foreach (string argument in request.arguments)
                if (argument == null || argument.IndexOf('\0') >= 0) throw new InvalidDataException("Invalid argument.");
            string cancel = ValidateCancelEventName(request.cancel_event_name);
            if (cancel == null) throw new InvalidDataException("A live host cancellation event is required.");
            var allowedEnvironment = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
                "SystemRoot", "WINDIR", "SystemDrive", "COMSPEC", "PATHEXT", "PATH",
                "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME",
                "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "PYTHONIOENCODING",
                "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT" };
            var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (request.environment == null) throw new InvalidDataException("Exact environment required.");
            foreach (var pair in request.environment)
            {
                if (!allowedEnvironment.Contains(pair.Key) || environment.ContainsKey(pair.Key) ||
                    pair.Value == null || pair.Value.IndexOf('\0') >= 0)
                    throw new InvalidDataException("Unexpected child environment entry.");
                environment.Add(pair.Key, pair.Value);
            }
            if (networkRequest) ValidateNetworkEnvironment(environment);
            var grants = new Dictionary<string, PathGrant>(StringComparer.OrdinalIgnoreCase);
            AddRuntimeGrants(request.read_paths, true, grants);
            AddRuntimeGrants(request.write_paths, false, grants);
            if (!grants.ContainsKey(cwd)) throw new InvalidDataException("cwd has no explicit grant.");
            return new ValidatedRequest { FixtureRoot = workspace, Cwd = cwd, Executable = executable,
                Arguments = request.arguments, TimeoutMs = request.timeout_ms, CancelEventName = cancel,
                Lpac = true, RuntimeRegistryRead = true, RuntimeInstrumentation = true, RuntimeCom = true,
                RuntimeNetwork = networkRequest,
                Environment = environment, Grants = grants, RuntimeRequest = true };
        }

        private static void ValidateNetworkEnvironment(Dictionary<string, string> environment)
        {
            ValidateNetworkProxyValues(environment);
            string gitNoSystem;
            if (!environment.TryGetValue("GIT_CONFIG_NOSYSTEM", out gitNoSystem) || gitNoSystem != "1")
                throw new InvalidDataException("GIT_CONFIG_NOSYSTEM must remain enabled for network execution.");
            string gitPrompt;
            if (!environment.TryGetValue("GIT_TERMINAL_PROMPT", out gitPrompt) || gitPrompt != "0")
                throw new InvalidDataException("GIT_TERMINAL_PROMPT must remain disabled for network execution.");
        }

        private static void ValidateNetworkProxyValues(Dictionary<string, string> environment)
        {
            ValidateProxyEnvironment(environment, "HTTP_PROXY", "http");
            ValidateProxyEnvironment(environment, "HTTPS_PROXY", "http");
            ValidateProxyEnvironment(environment, "GRPC_PROXY", "http");
            ValidateProxyEnvironment(environment, "ALL_PROXY", "http-or-socks");
            string noProxy;
            if (environment.TryGetValue("NO_PROXY", out noProxy) && noProxy.Length != 0)
                throw new InvalidDataException("NO_PROXY must be empty in the network protocol.");
            string gitParameters;
            if (environment.TryGetValue("GIT_CONFIG_PARAMETERS", out gitParameters) &&
                gitParameters != "'http.proxyAuthMethod=basic'")
                throw new InvalidDataException("GIT_CONFIG_PARAMETERS must use the fixed network proxy authentication setting.");
        }

        private static Dictionary<string, string> ValidateProxyEnvironment(Dictionary<string, string> source)
        {
            var allowed = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
                "HTTP_PROXY", "HTTPS_PROXY", "GRPC_PROXY", "ALL_PROXY", "NO_PROXY",
                "GIT_CONFIG_PARAMETERS" };
            var validated = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (source != null)
            {
                foreach (var pair in source)
                {
                    if (String.IsNullOrEmpty(pair.Key) || !allowed.Contains(pair.Key) ||
                        validated.ContainsKey(pair.Key) || pair.Value == null || pair.Value.IndexOf('\0') >= 0)
                        throw new InvalidDataException("Unexpected network proxy environment entry.");
                    validated.Add(pair.Key.ToUpperInvariant(), pair.Value);
                }
            }
            ValidateNetworkProxyValues(validated);
            return validated;
        }

        private static void MergeNetworkEnvironment(ValidatedRequest request,
            Dictionary<string, string> proxyEnvironment)
        {
            if (request == null || !request.RuntimeNetwork)
                throw new InvalidDataException("Network proxy environment requires a validated network request.");
            if (proxyEnvironment != null)
            {
                foreach (var pair in proxyEnvironment)
                {
                    if (request.Environment.ContainsKey(pair.Key))
                        throw new InvalidDataException("Network proxy environment duplicates a base environment key.");
                    request.Environment.Add(pair.Key, pair.Value);
                }
            }
            string gitParameters;
            if (!request.Environment.TryGetValue("GIT_CONFIG_PARAMETERS", out gitParameters))
                request.Environment.Add("GIT_CONFIG_PARAMETERS", "'http.proxyAuthMethod=basic'");
            ValidateNetworkEnvironment(request.Environment);
        }

        private static void ValidateProxyEnvironment(Dictionary<string, string> environment,
            string name, string proxyKind)
        {
            string value;
            if (!environment.TryGetValue(name, out value)) return;
            string userInfo = null;
            if (proxyKind == "http")
            {
                if (!TryParseLoopbackProxy(value, "http", out userInfo))
                    throw new InvalidDataException(name + " must use the approved loopback HTTP proxy.");
            }
            else if (!TryParseLoopbackProxy(value, "http", out userInfo) &&
                !TryParseLoopbackProxy(value, "socks5", out userInfo) &&
                !TryParseLoopbackProxy(value, "socks5h", out userInfo))
                throw new InvalidDataException(name + " must use the approved loopback HTTP or SOCKS proxy.");
        }

        private static bool TryParseLoopbackProxy(string value, string scheme, out string userInfo)
        {
            userInfo = null;
            string prefix = scheme + "://";
            if (String.IsNullOrEmpty(value) || value.Length > 5000 ||
                !value.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return false;
            const string suffix = "@127.0.0.1:60081";
            if (!value.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)) return false;
            string candidate = value.Substring(prefix.Length, value.Length - prefix.Length - suffix.Length);
            if (candidate.Length == 0 || candidate.Length > 4096) return false;
            for (int i = 0; i < candidate.Length; i++)
            {
                char c = candidate[i];
                bool allowed = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                    (c >= '0' && c <= '9') || "-._~!$&'()*+,;=:".IndexOf(c) >= 0;
                if (c == '%')
                {
                    if (i + 2 >= candidate.Length || !IsHex(candidate[i + 1]) || !IsHex(candidate[i + 2]))
                        return false;
                    i += 2;
                }
                else if (!allowed) return false;
            }
            userInfo = candidate;
            return true;
        }

        private static bool IsHex(char value)
        {
            return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f') ||
                (value >= 'A' && value <= 'F');
        }

        private static object ReceiptForSerialization(ProbeReceipt receipt)
        {
            if (receipt.network == null) return receipt;
            var fields = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (var property in typeof(ProbeReceipt).GetProperties())
            {
                if (String.Equals(property.Name, "network", StringComparison.Ordinal)) continue;
                fields[property.Name] = property.GetValue(receipt, null);
            }
            fields["network"] = receipt.network;
            return fields;
        }

        private static void AddRuntimeGrants(string[] paths, bool read, Dictionary<string, PathGrant> grants)
        {
            if (paths == null || paths.Length > 12000) throw new InvalidDataException("Invalid exact grants.");
            foreach (string raw in paths)
            {
                string path = ValidateExistingPath(raw, "exact grant", null);
                if (String.Equals(path, Path.GetPathRoot(path), StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("No volume-root grant.");
                bool directory = (File.GetAttributes(path) & FileAttributes.Directory) != 0;
                if (!directory) RejectHardLinkedFile(path, "exact grant");
                AddGrant(grants, path, directory, read, !read, read && !directory, "exact grant");
            }
        }

    }
}
