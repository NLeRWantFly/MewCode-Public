// Handle-only, short-lived SACL broker. This executable has no shell or Python dependency.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.AccessControl;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;
using System.Web.Script.Serialization;

namespace MewCode.Native.Windows
{
    internal static class MewCodeMetadataBroker
    {
        private const int MaximumFrame = 256 * 1024;
        private const int MaximumSecurityDescriptor = 64 * 1024;
        private const int MaximumPlanBytes = 8 * 1024 * 1024;
        private const int IoTimeoutMilliseconds = 15000;
        private const int LifetimeMilliseconds = 1200000;
        private const uint ProcessDuplicateHandle = 0x0040;
        private const uint ProcessQueryLimitedInformation = 0x1000;
        private const uint Synchronize = 0x00100000;
        private const uint DuplicateSameAccess = 0x0002;
        private const uint FileReadData = 0x0001;
        private const uint FileReadAttributes = 0x0080;
        private const uint ReadControl = 0x00020000;
        private const uint WriteDac = 0x00040000;
        private const uint AccessSystemSecurity = 0x01000000;
        private const uint FileAttributeDirectory = 0x0010;
        private const uint FileAttributeDevice = 0x0040;
        private const uint FileAttributeReparsePoint = 0x0400;
        private const uint FileFlagOpenReparsePoint = 0x00200000;
        private const uint FileFlagBackupSemantics = 0x02000000;
        private const ushort SelfRelative = 0x8000;
        private const ushort SaclPresent = 0x0010;
        private const ushort SaclProtected = 0x2000;
        private const uint SaclSecurityInformation = 0x00000008;
        private const uint ProtectedSaclSecurityInformation = 0x40000000;
        private const uint UnprotectedSaclSecurityInformation = 0x10000000;
        private const uint TokenQuery = 0x0008;
        private const uint TokenAdjustPrivileges = 0x0020;
        private const uint SePrivilegeEnabled = 0x0002;
        private const int ErrorInsufficientBuffer = 122;
        private const int ErrorNotAllAssigned = 1300;
        private const int ErrorInvalidHandle = 6;
        private const int WaitObject0 = 0;
        private const int WaitTimeout = 258;
        private const int WaitFailed = unchecked((int)0xffffffff);

        private static readonly JavaScriptSerializer Json = CreateJson();
        private static readonly JavaScriptSerializer PlanJson = CreatePlanJson();
        private static string expectedNonce;
        private static long expectedSequence = 1;
        private static IntPtr parentProcess = IntPtr.Zero;
        private static NamedPipeClientStream pipe;
        private static Scope activeScope;
        private static Timer lifetimeTimer;

        private sealed class Scope
        {
            public NativeObject Workspace;
            public NativeObject Transaction;
            public string WorkspacePath;
            public string TransactionPath;
            public uint Volume;
            public Dictionary<string, NativeObject> Registered = new Dictionary<string, NativeObject>(StringComparer.Ordinal);
            public Dictionary<string, byte[]> RecoveryDescriptors = new Dictionary<string, byte[]>(StringComparer.Ordinal);
        }

        private sealed class NativeObject : IDisposable
        {
            public IntPtr Handle;
            public uint Volume;
            public ulong Index;
            public ulong Length;
            public uint Attributes;
            public string Path;

            public string Identity { get { return Volume.ToString("X8") + ":" + Index.ToString("X16"); } }
            public bool IsDirectory { get { return (Attributes & FileAttributeDirectory) != 0; } }

            public void Dispose()
            {
                if (Handle != IntPtr.Zero)
                {
                    IntPtr h = Handle;
                    Handle = IntPtr.Zero;
                    Native.CloseHandle(h);
                }
            }
        }

        private sealed class FileInformation
        {
            public uint Volume;
            public ulong Index;
            public uint Attributes;
            public uint Links;
            public ulong Length;
        }

        private sealed class DescriptorParts
        {
            public byte[] Bytes;
            public ushort Control;
            public bool SaclPresent;
            public bool SaclNull;
            public int SaclOffset;
            public int SaclLength;
        }

        private static JavaScriptSerializer CreateJson()
        {
            JavaScriptSerializer json = new JavaScriptSerializer();
            json.MaxJsonLength = MaximumFrame;
            json.RecursionLimit = 32;
            return json;
        }

        private static JavaScriptSerializer CreatePlanJson()
        {
            JavaScriptSerializer json = new JavaScriptSerializer();
            json.MaxJsonLength = MaximumPlanBytes;
            json.RecursionLimit = 32;
            return json;
        }

        private static int Main(string[] args)
        {
            try
            {
                string pipeName;
                uint parentPid;
                ParseArguments(args, out pipeName, out parentPid, out expectedNonce);
                parentProcess = Native.OpenProcess(ProcessDuplicateHandle | ProcessQueryLimitedInformation | Synchronize, false, parentPid);
                if (parentProcess == IntPtr.Zero)
                    throw Native.LastError("Cannot pin the metadata broker parent");
                lifetimeTimer = new Timer(delegate { Environment.Exit(124); }, null, LifetimeMilliseconds, Timeout.Infinite);
                StartParentMonitor(parentProcess);

                pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.None);
                RunBounded(delegate { pipe.Connect(10000); }, "pipe connection", IoTimeoutMilliseconds);
                uint serverPid;
                if (!Native.GetNamedPipeServerProcessId(pipe.SafePipeHandle.DangerousGetHandle(), out serverPid)
                    || serverPid != parentPid)
                    throw new InvalidOperationException("Named pipe server process identity mismatch");

                EnableOnlyRequestedPrivilege();
                while (true)
                {
                    byte[] raw;
                    try { raw = ReadFrame(); }
                    catch (EndOfStreamException) { break; }
                    catch (IOException) { break; }

                    bool closeAfterReply;
                    Dictionary<string, object> response = HandleRequest(raw, out closeAfterReply);
                    WriteFrame(Json.Serialize(response));
                    if (closeAfterReply)
                        break;
                }
            }
            catch (Exception)
            {
                return 1;
            }
            finally
            {
                ClearScope();
                if (pipe != null) pipe.Dispose();
                if (parentProcess != IntPtr.Zero) Native.CloseHandle(parentProcess);
                if (lifetimeTimer != null) lifetimeTimer.Dispose();
            }
            return 0;
        }

        private static void ParseArguments(string[] args, out string name, out uint pid, out string nonce)
        {
            name = null;
            pid = 0;
            nonce = null;
            if (args == null || args.Length != 6 || args[0] != "--pipe" || args[2] != "--parent" || args[4] != "--nonce")
                throw new ArgumentException("Expected --pipe <name> --parent <pid> --nonce <64hex>");
            if (String.IsNullOrWhiteSpace(args[1]) || args[1].Length > 240)
                throw new ArgumentException("Invalid pipe name");
            for (int i = 0; i < args[1].Length; i++)
            {
                char c = args[1][i];
                if (!(Char.IsLetterOrDigit(c) || c == '.' || c == '_' || c == '-'))
                    throw new ArgumentException("Invalid pipe name");
            }
            uint parsedPid;
            if (!UInt32.TryParse(args[3], out parsedPid) || parsedPid == 0)
                throw new ArgumentException("Invalid parent process ID");
            if (args[5].Length != 64)
                throw new ArgumentException("Invalid broker nonce");
            for (int i = 0; i < args[5].Length; i++)
            {
                char c = args[5][i];
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')))
                    throw new ArgumentException("Invalid broker nonce");
            }
            name = args[1];
            pid = parsedPid;
            nonce = args[5];
        }

        private static void StartParentMonitor(IntPtr handle)
        {
            Thread monitor = new Thread(delegate()
            {
                int result = Native.WaitForSingleObject(handle, 0xffffffff);
                if (result == WaitObject0 || result == WaitFailed)
                    Environment.Exit(0);
            });
            monitor.IsBackground = true;
            monitor.Start();
        }

        private static void EnableOnlyRequestedPrivilege()
        {
            IntPtr token;
            if (!Native.OpenProcessToken(Native.GetCurrentProcess(), TokenAdjustPrivileges | TokenQuery, out token))
                throw Native.LastError("Cannot open broker token");
            try
            {
                Native.Luid luid;
                if (!Native.LookupPrivilegeValue(null, "SeSecurityPrivilege", out luid))
                    throw Native.LastError("SeSecurityPrivilege is unavailable");
                Native.TokenPrivileges privileges = new Native.TokenPrivileges();
                privileges.PrivilegeCount = 1;
                privileges.Privilege = new Native.LuidAndAttributes();
                privileges.Privilege.Luid = luid;
                privileges.Privilege.Attributes = SePrivilegeEnabled;
                if (!Native.AdjustTokenPrivileges(token, false, ref privileges, 0, IntPtr.Zero, IntPtr.Zero))
                    throw Native.LastError("Cannot enable SeSecurityPrivilege");
                int error = Marshal.GetLastWin32Error();
                if (error == ErrorNotAllAssigned)
                    throw Native.WithWin32Error(error, "SeSecurityPrivilege was not assigned to the broker token");
            }
            finally { Native.CloseHandle(token); }
        }

        private static void RunBounded(Action action, string operation, int timeoutMilliseconds)
        {
            Exception failure = null;
            ManualResetEvent done = new ManualResetEvent(false);
            Thread worker = new Thread(delegate()
            {
                try { action(); }
                catch (Exception ex) { failure = ex; }
                finally { done.Set(); }
            });
            worker.IsBackground = true;
            worker.Start();
            if (!done.WaitOne(timeoutMilliseconds))
                Environment.Exit(124);
            done.Dispose();
            if (failure != null)
                throw new IOException("Bounded " + operation + " failed", failure);
        }

        private static byte[] ReadFrame()
        {
            byte[] prefix = new byte[4];
            RunBounded(delegate { ReadExact(prefix, 0, prefix.Length); }, "frame header read", LifetimeMilliseconds);
            int length = prefix[0] | (prefix[1] << 8) | (prefix[2] << 16) | (prefix[3] << 24);
            if (length <= 0 || length > MaximumFrame)
                throw new InvalidDataException("Broker frame length is outside the allowed range");
            byte[] payload = new byte[length];
            RunBounded(delegate { ReadExact(payload, 0, payload.Length); }, "frame body read", IoTimeoutMilliseconds);
            return payload;
        }

        private static void ReadExact(byte[] target, int offset, int length)
        {
            int read = 0;
            while (read < length)
            {
                int count = pipe.Read(target, offset + read, length - read);
                if (count <= 0) throw new EndOfStreamException("Metadata broker pipe closed");
                read += count;
            }
        }

        private static void WriteFrame(string json)
        {
            byte[] body = new UTF8Encoding(false, true).GetBytes(json);
            if (body.Length <= 0 || body.Length > MaximumFrame)
                throw new InvalidDataException("Broker response exceeds the allowed frame length");
            byte[] frame = new byte[body.Length + 4];
            frame[0] = (byte)body.Length;
            frame[1] = (byte)(body.Length >> 8);
            frame[2] = (byte)(body.Length >> 16);
            frame[3] = (byte)(body.Length >> 24);
            Buffer.BlockCopy(body, 0, frame, 4, body.Length);
            RunBounded(delegate
            {
                int written = 0;
                while (written < frame.Length)
                {
                    pipe.Write(frame, written, frame.Length - written);
                    written = frame.Length;
                }
                pipe.Flush();
            }, "frame write", IoTimeoutMilliseconds);
        }

        private static Dictionary<string, object> HandleRequest(byte[] raw, out bool closeAfterReply)
        {
            closeAfterReply = false;
            long replySequence = expectedSequence;
            try
            {
                string text = new UTF8Encoding(false, true).GetString(raw);
                RejectDuplicateObjectKeys(text);
                object value = Json.DeserializeObject(text);
                Dictionary<string, object> request = value as Dictionary<string, object>;
                if (request == null) throw new InvalidDataException("Broker request must be a JSON object");
                object seqValue;
                if (request.TryGetValue("seq", out seqValue))
                {
                    long requestedSequence;
                    if (TryInt64(seqValue, out requestedSequence) && requestedSequence > 0)
                        replySequence = requestedSequence;
                }
                object nonceValue;
                if (!request.TryGetValue("nonce", out nonceValue) || !(nonceValue is string)
                    || !String.Equals((string)nonceValue, expectedNonce, StringComparison.Ordinal))
                    throw new InvalidDataException("Broker nonce mismatch");
                long sequence;
                if (!request.TryGetValue("seq", out seqValue) || !TryInt64(seqValue, out sequence)
                    || sequence != expectedSequence)
                    throw new InvalidDataException("Broker sequence mismatch");
                replySequence = sequence;
                if (expectedSequence == Int64.MaxValue)
                    throw new InvalidDataException("Broker sequence exhausted");
                expectedSequence++;
                object opValue;
                if (!request.TryGetValue("op", out opValue) || !(opValue is string))
                    throw new InvalidDataException("Broker operation is missing or invalid");
                string op = (string)opValue;
                Dictionary<string, object> result = new Dictionary<string, object>();
                if (op == "init")
                {
                    RequireFields(request, "nonce", "seq", "op", "workspace_handle", "workspace_id", "transaction_handle", "transaction_id");
                    Init(request, result);
                }
                else if (op == "snapshot")
                {
                    RequireFields(request, "nonce", "seq", "op", "handle", "file_id");
                    Snapshot(request, result);
                }
                else if (op == "register")
                {
                    RequireFields(request, "nonce", "seq", "op", "handle", "file_id");
                    Register(request, result);
                }
                else if (op == "register_recovery")
                {
                    RequireFields(request, "nonce", "seq", "op", "handle", "file_id", "plan_handle", "plan_id", "plan_sha256");
                    RegisterRecovery(request, result);
                }
                else if (op == "apply")
                {
                    RequireFields(request, "nonce", "seq", "op", "handle", "file_id", "descriptor", "final");
                    Apply(request, result);
                }
                else if (op == "end")
                {
                    RequireFields(request, "nonce", "seq", "op");
                    if (activeScope == null) throw new InvalidOperationException("No metadata transaction scope is active");
                    ClearScope();
                }
                else if (op == "close")
                {
                    RequireFields(request, "nonce", "seq", "op");
                    ClearScope();
                    closeAfterReply = true;
                }
                else throw new InvalidDataException("Unknown broker operation");
                result["nonce"] = expectedNonce;
                result["seq"] = replySequence;
                result["ok"] = true;
                return result;
            }
            catch (Exception ex)
            {
                Dictionary<string, object> failure = new Dictionary<string, object>();
                failure["nonce"] = expectedNonce;
                failure["seq"] = replySequence;
                failure["ok"] = false;
                failure["error"] = SafeError(ex);
                return failure;
            }
        }

        private static string SafeError(Exception ex)
        {
            string message = ex == null ? "Metadata broker request failed" : ex.Message;
            if (String.IsNullOrEmpty(message)) message = "Metadata broker request failed";
            if (message.Length > 300) message = message.Substring(0, 300);
            return message;
        }

        private static void RequireObjectFields(Dictionary<string, object> value, params string[] expected)
        {
            HashSet<string> fields = new HashSet<string>(expected, StringComparer.Ordinal);
            if (value.Count != fields.Count)
                throw new InvalidDataException("Recovery plan has missing or extra fields");
            foreach (string key in value.Keys)
                if (!fields.Contains(key)) throw new InvalidDataException("Recovery plan has missing or extra fields");
        }

        private static string RequiredString(Dictionary<string, object> value, string key)
        {
            object field;
            if (!value.TryGetValue(key, out field) || !(field is string))
                throw new InvalidDataException("Recovery plan field " + key + " must be text");
            return (string)field;
        }

        private static string CanonicalSourcePath(string source)
        {
            if (String.IsNullOrWhiteSpace(source) || !Path.IsPathRooted(source))
                throw new InvalidDataException("Recovery plan source must be an absolute path");
            string full = Path.GetFullPath(source).Replace('/', '\\');
            if (full.StartsWith("\\\\?\\", StringComparison.Ordinal))
                return CanonicalPath(full);
            if (full.StartsWith("\\\\", StringComparison.Ordinal))
                return CanonicalPath("\\\\?\\UNC\\" + full.Substring(2));
            return CanonicalPath("\\\\?\\" + full);
        }

        private static void ValidateRecoveryRelative(string relative)
        {
            if (String.IsNullOrEmpty(relative) || relative.IndexOf('\\') >= 0 || relative.IndexOf(':') >= 0
                || Path.IsPathRooted(relative) || relative[0] == '/')
                throw new InvalidDataException("Recovery plan contains an unsafe relative path");
            string[] pieces = relative.Split('/');
            for (int i = 0; i < pieces.Length; i++)
                if (pieces[i].Length == 0 || pieces[i] == "." || pieces[i] == "..")
                    throw new InvalidDataException("Recovery plan contains an unsafe relative path");
        }

        private static void RequireHex(string value, int length, string field)
        {
            if (value == null || value.Length != length)
                throw new InvalidDataException(field + " has an invalid hexadecimal length");
            for (int i = 0; i < value.Length; i++)
                if (!IsHex(value[i])) throw new InvalidDataException(field + " is not hexadecimal");
        }

        private static bool IsHex(char value)
        {
            return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f') || (value >= 'A' && value <= 'F');
        }

        private static byte[] DecodeHex(string value, int maximumCharacters, string field)
        {
            if (value == null || value.Length == 0 || value.Length > maximumCharacters || (value.Length & 1) != 0)
                throw new InvalidDataException(field + " has an invalid hexadecimal size");
            byte[] result = new byte[value.Length / 2];
            for (int i = 0; i < result.Length; i++)
            {
                int high = HexValue(value[i * 2]);
                int low = HexValue(value[i * 2 + 1]);
                if (high < 0 || low < 0) throw new InvalidDataException(field + " is not hexadecimal");
                result[i] = (byte)((high << 4) | low);
            }
            return result;
        }

        private static int HexValue(char value)
        {
            if (value >= '0' && value <= '9') return value - '0';
            if (value >= 'a' && value <= 'f') return value - 'a' + 10;
            if (value >= 'A' && value <= 'F') return value - 'A' + 10;
            return -1;
        }

        private static string Hex(byte[] value)
        {
            StringBuilder result = new StringBuilder(value.Length * 2);
            for (int i = 0; i < value.Length; i++) result.Append(value[i].ToString("x2"));
            return result.ToString();
        }

        private static bool BytesEqual(byte[] left, byte[] right)
        {
            if (left == null || right == null || left.Length != right.Length) return false;
            int difference = 0;
            for (int i = 0; i < left.Length; i++) difference |= left[i] ^ right[i];
            return difference == 0;
        }

        private static void RequireFields(Dictionary<string, object> request, params string[] expected)
        {
            HashSet<string> fields = new HashSet<string>(expected, StringComparer.Ordinal);
            if (request.Count != fields.Count)
                throw new InvalidDataException("Broker request has missing or extra fields");
            foreach (string key in request.Keys)
                if (!fields.Contains(key)) throw new InvalidDataException("Broker request has missing or extra fields");
        }

        private static void Init(Dictionary<string, object> request, Dictionary<string, object> response)
        {
            if (activeScope != null) throw new InvalidOperationException("A metadata transaction scope is already active");
            NativeObject workspace = null;
            NativeObject transaction = null;
            try
            {
                workspace = DuplicateRequestObject(request["workspace_handle"], request["workspace_id"]);
                transaction = DuplicateRequestObject(request["transaction_handle"], request["transaction_id"]);
                if (!workspace.IsDirectory || !transaction.IsDirectory)
                    throw new InvalidDataException("Workspace and transaction must be ordinary directories");
                if (workspace.Volume != transaction.Volume)
                    throw new InvalidDataException("Workspace and transaction must be on the same volume");
                string workspacePath = CanonicalPath(GetFinalPath(workspace.Handle));
                string transactionPath = CanonicalPath(GetFinalPath(transaction.Handle));
                if (workspacePath == transactionPath || IsWithin(workspacePath, transactionPath) || IsWithin(transactionPath, workspacePath))
                    throw new InvalidDataException("Workspace and transaction must be distinct and non-nested");
                RejectVolumeRoot(workspacePath);
                RejectVolumeRoot(transactionPath);
                workspace.Path = workspacePath;
                transaction.Path = transactionPath;
                Scope scope = new Scope();
                scope.Workspace = workspace;
                scope.Transaction = transaction;
                scope.WorkspacePath = workspacePath;
                scope.TransactionPath = transactionPath;
                scope.Volume = workspace.Volume;
                activeScope = scope;
                workspace = null;
                transaction = null;
                response["workspace_id"] = FileId(scope.Workspace.Volume, scope.Workspace.Index);
                response["transaction_id"] = FileId(scope.Transaction.Volume, scope.Transaction.Index);
            }
            finally
            {
                if (workspace != null) workspace.Dispose();
                if (transaction != null) transaction.Dispose();
            }
        }

        private static void Snapshot(Dictionary<string, object> request, Dictionary<string, object> response)
        {
            RequireActiveScope();
            NativeObject file = null;
            NativeObject securityFile = null;
            try
            {
                file = DuplicateRequestObject(request["handle"], request["file_id"]);
                RequireScopeObject(file);
                uint granted = GetGrantedAccess(file.Handle);
                if ((granted & ReadControl) == 0)
                    throw new UnauthorizedAccessException("Snapshot handle lacks READ_CONTROL");
                securityFile = ReopenForSecurity(file, granted);
                RequireScopeObject(securityFile);
                DescriptorParts descriptor = ReadDescriptor(securityFile.Handle);
                response["file_id"] = FileId(file.Volume, file.Index);
                response["descriptor"] = Convert.ToBase64String(descriptor.Bytes);
                response["control"] = (int)descriptor.Control;
                response["sacl"] = new Dictionary<string, object>
                {
                    { "state", "captured" },
                    { "present", descriptor.SaclPresent },
                    { "null", descriptor.SaclPresent && descriptor.SaclNull },
                    { "sddl", !descriptor.SaclPresent || descriptor.SaclNull ? null : (object)ToSddl(descriptor.Bytes, 8) }
                };
            }
            finally
            {
                if (securityFile != null) securityFile.Dispose();
                if (file != null) file.Dispose();
            }
        }

        private static void Register(Dictionary<string, object> request, Dictionary<string, object> response)
        {
            RequireActiveScope();
            NativeObject file = null;
            try
            {
                file = DuplicateRequestObject(request["handle"], request["file_id"]);
                uint granted = GetGrantedAccess(file.Handle);
                if ((granted & WriteDac) == 0)
                    throw new UnauthorizedAccessException("Registered handle lacks WRITE_DAC");
                string key = file.Identity;
                NativeObject existing;
                if (activeScope.Registered.TryGetValue(key, out existing))
                {
                    if (existing.Volume != file.Volume || existing.Index != file.Index)
                        throw new InvalidDataException("Registered object identity changed");
                    RequireScopeObject(file);
                    response["file_id"] = FileId(file.Volume, file.Index);
                    return;
                }
                RequirePublicationObject(file);
                NativeObject persistent = ReopenForSecurity(file, granted);
                try
                {
                    RequirePublicationObject(persistent);
                    if (persistent.Volume != file.Volume || persistent.Index != file.Index)
                        throw new InvalidDataException("Registered object changed during pinning");
                    activeScope.Registered.Add(key, persistent);
                    persistent = null;
                }
                finally { if (persistent != null) persistent.Dispose(); }
                response["file_id"] = FileId(file.Volume, file.Index);
            }
            finally { if (file != null) file.Dispose(); }
        }

        private static void RegisterRecovery(Dictionary<string, object> request, Dictionary<string, object> response)
        {
            RequireActiveScope();
            NativeObject target = null;
            NativeObject plan = null;
            NativeObject persistent = null;
            try
            {
                target = DuplicateRequestObject(request["handle"], request["file_id"]);
                if (!target.IsDirectory || !IsWithin(activeScope.WorkspacePath, target.Path))
                    throw new UnauthorizedAccessException("Recovery target must be a workspace directory");
                uint targetAccess = GetGrantedAccess(target.Handle);
                uint targetRequired = WriteDac | ReadControl | FileReadAttributes;
                if ((targetAccess & targetRequired) != targetRequired)
                    throw new UnauthorizedAccessException("Recovery target handle lacks WRITE_DAC and ordinary read rights");

                plan = DuplicateRequestObject(request["plan_handle"], request["plan_id"]);
                string expectedPlanPath = CanonicalPath(activeScope.TransactionPath + "\\publication\\plan.json");
                if (plan.IsDirectory || !String.Equals(plan.Path, expectedPlanPath, StringComparison.Ordinal))
                    throw new UnauthorizedAccessException("Recovery plan handle must identify transaction/publication/plan.json");
                uint planAccess = GetGrantedAccess(plan.Handle);
                if ((planAccess & (FileReadData | ReadControl)) != (FileReadData | ReadControl))
                    throw new UnauthorizedAccessException("Recovery plan handle lacks FILE_READ_DATA and READ_CONTROL");
                byte[] planBytes = ReadBoundedPlan(plan);
                object hashValue = request["plan_sha256"];
                if (!(hashValue is string)) throw new InvalidDataException("plan_sha256 must be hexadecimal text");
                string expectedHash = (string)hashValue;
                RequireHex(expectedHash, 64, "plan_sha256");
                string actualHash;
                using (SHA256 sha = SHA256.Create())
                    actualHash = Hex(sha.ComputeHash(planBytes));
                if (!String.Equals(expectedHash, actualHash, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Recovery plan digest does not match the supplied handle");

                byte[] originalSecurity = ValidateRecoveryPlan(planBytes, target);
                string key = target.Identity;
                NativeObject existing;
                if (activeScope.Registered.TryGetValue(key, out existing))
                {
                    byte[] bound;
                    if (!activeScope.RecoveryDescriptors.TryGetValue(key, out bound)
                        || !BytesEqual(bound, originalSecurity))
                        throw new UnauthorizedAccessException("Object is registered without this recovery descriptor");
                    if (existing.Volume != target.Volume || existing.Index != target.Index)
                        throw new InvalidDataException("Recovery registration identity changed");
                    response["file_id"] = FileId(target.Volume, target.Index);
                    return;
                }

                persistent = ReopenForSecurity(target, targetAccess);
                if (!IsWithin(activeScope.WorkspacePath, persistent.Path)
                    || persistent.Volume != target.Volume || persistent.Index != target.Index)
                    throw new InvalidDataException("Recovery target changed during registration");
                activeScope.Registered.Add(key, persistent);
                try { activeScope.RecoveryDescriptors.Add(key, originalSecurity); }
                catch
                {
                    activeScope.Registered.Remove(key);
                    throw;
                }
                persistent = null;
                response["file_id"] = FileId(target.Volume, target.Index);
                response["recovery_registered"] = true;
            }
            finally
            {
                if (persistent != null) persistent.Dispose();
                if (plan != null) plan.Dispose();
                if (target != null) target.Dispose();
            }
        }

        private static byte[] ReadBoundedPlan(NativeObject plan)
        {
            if (plan.Length > MaximumPlanBytes)
                throw new InvalidDataException("Recovery plan exceeds the 8 MiB bound");
            long position;
            if (!Native.SetFilePointerEx(plan.Handle, 0, out position, 0))
                throw Native.LastError("Cannot seek the duplicated recovery plan handle");
            MemoryStream bytes = new MemoryStream();
            byte[] buffer = new byte[64 * 1024];
            while (true)
            {
                uint count;
                if (!Native.ReadFile(plan.Handle, buffer, (uint)buffer.Length, out count, IntPtr.Zero))
                    throw Native.LastError("Cannot read the duplicated recovery plan handle");
                if (count == 0) break;
                if (bytes.Length + count > MaximumPlanBytes)
                    throw new InvalidDataException("Recovery plan exceeds the 8 MiB bound");
                bytes.Write(buffer, 0, checked((int)count));
            }
            if ((ulong)bytes.Length != plan.Length)
                throw new InvalidDataException("Recovery plan changed while it was being read");
            return bytes.ToArray();
        }

        private static byte[] ValidateRecoveryPlan(byte[] raw, NativeObject target)
        {
            string text = new UTF8Encoding(false, true).GetString(raw);
            RejectDuplicateObjectKeys(text, PlanJson);
            object parsed = PlanJson.DeserializeObject(text);
            Dictionary<string, object> document = parsed as Dictionary<string, object>;
            if (document == null)
                throw new InvalidDataException("Recovery plan must be a JSON object");
            RequireObjectFields(document, "schema", "manifest_sha256", "policy_revision", "source", "staged_digest",
                "holder", "lease_id", "entries", "metadata_broker_required");
            if (!String.Equals(RequiredString(document, "schema"), "mewcode-staged-publication-v2", StringComparison.Ordinal))
                throw new InvalidDataException("Unsupported recovery plan schema");
            string source = RequiredString(document, "source");
            if (!String.Equals(CanonicalSourcePath(source), activeScope.WorkspacePath, StringComparison.Ordinal))
                throw new UnauthorizedAccessException("Recovery plan source differs from the active workspace");
            if (!(document["metadata_broker_required"] is bool) || !(bool)document["metadata_broker_required"])
                throw new UnauthorizedAccessException("Recovery plan does not require metadata preservation");
            RequiredString(document, "manifest_sha256");
            RequiredString(document, "policy_revision");
            RequiredString(document, "staged_digest");
            RequiredString(document, "lease_id");
            if (!(document["holder"] is Dictionary<string, object>))
                throw new InvalidDataException("Recovery plan holder is invalid");
            object[] entries = document["entries"] as object[];
            if (entries == null || entries.Length == 0 || entries.Length > 12000)
                throw new InvalidDataException("Recovery plan entries are invalid");

            int targetMatches = 0;
            byte[] originalSecurity = null;
            string targetRelative = null;
            foreach (object value in entries)
            {
                Dictionary<string, object> entry = value as Dictionary<string, object>;
                if (entry == null) throw new InvalidDataException("Recovery plan entry must be an object");
                string relative = RequiredString(entry, "relative");
                ValidateRecoveryRelative(relative);
                string kind = RequiredString(entry, "kind");
                string operation = RequiredString(entry, "operation");
                if ((kind != "file" && kind != "directory")
                    || (operation != "create" && operation != "modify" && operation != "delete"))
                    throw new InvalidDataException("Recovery plan entry kind or operation is invalid");

                object oldValue;
                if (entry.TryGetValue("old_id", out oldValue) && oldValue != null)
                {
                    uint oldVolume;
                    ulong oldIndex;
                    ParseFileId(oldValue, out oldVolume, out oldIndex);
                    if (oldVolume == target.Volume && oldIndex == target.Index)
                        throw new InvalidDataException("Recovery target must not match any old_id");
                }
                if (kind == "directory" && operation == "delete")
                {
                    object restoreValue;
                    object securityValue;
                    if (!entry.TryGetValue("restore_id", out restoreValue) || restoreValue == null
                        || oldValue == null
                        || !entry.TryGetValue("original_security", out securityValue) || !(securityValue is string))
                        throw new InvalidDataException("Deleted directory recovery fields are incomplete");
                    uint restoreVolume;
                    ulong restoreIndex;
                    ParseFileId(restoreValue, out restoreVolume, out restoreIndex);
                    byte[] descriptor = DecodeHex((string)securityValue, MaximumSecurityDescriptor * 2, "original_security");
                    ValidateDescriptor(descriptor);
                    if (restoreVolume == target.Volume && restoreIndex == target.Index)
                    {
                        targetMatches++;
                        targetRelative = relative;
                        originalSecurity = descriptor;
                    }
                }
                else
                {
                    object restoreValue;
                    if (entry.TryGetValue("restore_id", out restoreValue) && restoreValue != null)
                    {
                        uint restoreVolume;
                        ulong restoreIndex;
                        ParseFileId(restoreValue, out restoreVolume, out restoreIndex);
                        if (restoreVolume == target.Volume && restoreIndex == target.Index)
                            throw new InvalidDataException("Recovery target restore_id is not a deleted directory");
                    }
                }
            }
            if (targetMatches != 1 || originalSecurity == null || targetRelative == null)
                throw new UnauthorizedAccessException("Recovery plan must contain exactly one matching directory restore entry");
            string expectedTarget = CanonicalPath(activeScope.WorkspacePath + "\\" + targetRelative.Replace('/', '\\'));
            if (!String.Equals(expectedTarget, target.Path, StringComparison.Ordinal))
                throw new UnauthorizedAccessException("Recovery plan relative path does not identify the target handle");
            return originalSecurity;
        }

        private static void Apply(Dictionary<string, object> request, Dictionary<string, object> response)
        {
            RequireActiveScope();
            NativeObject file = null;
            NativeObject securityFile = null;
            try
            {
                file = DuplicateRequestObject(request["handle"], request["file_id"]);
                uint granted = GetGrantedAccess(file.Handle);
                if ((granted & WriteDac) == 0)
                    throw new UnauthorizedAccessException("Apply handle lacks WRITE_DAC");
                NativeObject registered;
                if (!activeScope.Registered.TryGetValue(file.Identity, out registered))
                    throw new UnauthorizedAccessException("Object was not registered for metadata apply");
                if (registered.Volume != file.Volume || registered.Index != file.Index)
                    throw new InvalidDataException("Registered object identity changed");
                RequireScopeObject(file);
                object descriptorValue = request["descriptor"];
                if (!(descriptorValue is string)) throw new InvalidDataException("Descriptor must be base64 text");
                string encoded = (string)descriptorValue;
                byte[] descriptor;
                try { descriptor = Convert.FromBase64String(encoded); }
                catch (FormatException) { throw new InvalidDataException("Descriptor is not valid base64"); }
                if (!String.Equals(Convert.ToBase64String(descriptor), encoded, StringComparison.Ordinal))
                    throw new InvalidDataException("Descriptor base64 must use canonical encoding");
                DescriptorParts parts = ValidateDescriptor(descriptor);
                byte[] recoveryDescriptor;
                if (activeScope.RecoveryDescriptors.TryGetValue(file.Identity, out recoveryDescriptor)
                    && !BytesEqual(recoveryDescriptor, descriptor))
                    throw new UnauthorizedAccessException("Recovery apply descriptor differs from the registered original security");
                object finalValue = request["final"];
                if (!(finalValue is bool)) throw new InvalidDataException("final must be a boolean");
                securityFile = ReopenForSecurity(file, granted);
                RequireScopeObject(securityFile);
                ApplySacl(securityFile.Handle, parts, (bool)finalValue);
                response["file_id"] = FileId(file.Volume, file.Index);
                response["applied"] = true;
            }
            finally
            {
                if (securityFile != null) securityFile.Dispose();
                if (file != null) file.Dispose();
            }
        }

        private static NativeObject ReopenForSecurity(NativeObject original, uint originalAccess)
        {
            uint ordinaryAccess = ReadControl | FileReadAttributes;
            if ((originalAccess & ordinaryAccess) != ordinaryAccess)
                throw new UnauthorizedAccessException("Parent handle lacks the ordinary rights needed for SACL access");
            uint desiredAccess = ordinaryAccess | AccessSystemSecurity;
            uint flags = FileFlagOpenReparsePoint | (original.IsDirectory ? FileFlagBackupSemantics : 0);
            IntPtr handle;
            if (original.IsDirectory)
            {
                Native.FileIdDescriptor fileId = new Native.FileIdDescriptor();
                fileId.Size = 24;
                fileId.Type = 0; // FileIdType: the 64-bit identifier validated from this handle.
                fileId.FileId = unchecked((long)original.Index);
                if (Marshal.SizeOf(typeof(Native.FileIdDescriptor)) != 24)
                    throw new InvalidOperationException("FILE_ID_DESCRIPTOR has an unexpected native size");
                handle = Native.OpenFileById(original.Handle, ref fileId, desiredAccess, 7, IntPtr.Zero, flags);
            }
            else
                handle = Native.ReOpenFile(original.Handle, desiredAccess, 7, flags);
            if (handle == IntPtr.Zero || handle == new IntPtr(-1))
                throw Native.LastError("Cannot add SACL access to the duplicated object handle using "
                    + (original.IsDirectory ? "OpenFileById" : "ReOpenFile")
                    + " (desiredAccess=0x" + desiredAccess.ToString("X8") + ", flags=0x" + flags.ToString("X8") + ")");
            try
            {
                FileInformation information = GetFileInformation(handle);
                if (information.Volume != original.Volume || information.Index != original.Index
                    || (information.Attributes & (FileAttributeReparsePoint | FileAttributeDevice)) != 0
                    || (((information.Attributes & FileAttributeDirectory) != 0) != original.IsDirectory))
                    throw new InvalidDataException("Reopened security handle changed object identity");
                uint reopenedAccess = GetGrantedAccess(handle);
                if ((reopenedAccess & AccessSystemSecurity) == 0
                    || (reopenedAccess & ordinaryAccess) != ordinaryAccess
                    || (reopenedAccess & ~(ordinaryAccess | AccessSystemSecurity | Synchronize)) != 0)
                    throw new UnauthorizedAccessException("Reopened security handle changed ordinary access rights");
                NativeObject result = new NativeObject();
                result.Handle = handle;
                result.Volume = information.Volume;
                result.Index = information.Index;
                result.Attributes = information.Attributes;
                result.Path = CanonicalPath(GetFinalPath(handle));
                handle = IntPtr.Zero;
                return result;
            }
            finally { if (handle != IntPtr.Zero && handle != new IntPtr(-1)) Native.CloseHandle(handle); }
        }

        private static NativeObject DuplicateRequestObject(object handleValue, object requestedId)
        {
            long rawHandle;
            if (!TryInt64(handleValue, out rawHandle) || rawHandle == 0)
                throw new InvalidDataException("Handle must be a nonzero integer");
            uint requestedVolume;
            ulong requestedIndex;
            ParseFileId(requestedId, out requestedVolume, out requestedIndex);
            IntPtr duplicate;
            if (!Native.DuplicateHandle(parentProcess, new IntPtr(rawHandle), Native.GetCurrentProcess(), out duplicate,
                0, false, DuplicateSameAccess))
                throw Native.LastError("Cannot duplicate parent-provided object handle");
            try
            {
                FileInformation info = GetFileInformation(duplicate);
                if ((info.Attributes & FileAttributeReparsePoint) != 0 || (info.Attributes & FileAttributeDevice) != 0)
                    throw new InvalidDataException("Reparse points and device objects are not supported");
                if ((info.Attributes & FileAttributeDirectory) == 0 && info.Links != 1)
                    throw new InvalidDataException("Ordinary files must have exactly one hard link");
                if (info.Volume != requestedVolume || info.Index != requestedIndex)
                    throw new InvalidDataException("File identity does not match the request");
                NativeObject result = new NativeObject();
                result.Handle = duplicate;
                result.Volume = info.Volume;
                result.Index = info.Index;
                result.Length = info.Length;
                result.Attributes = info.Attributes;
                result.Path = CanonicalPath(GetFinalPath(duplicate));
                if (activeScope != null && result.Volume != activeScope.Volume)
                    throw new InvalidDataException("Object is on a different volume from the active scope");
                duplicate = IntPtr.Zero;
                return result;
            }
            finally { if (duplicate != IntPtr.Zero) Native.CloseHandle(duplicate); }
        }

        private static FileInformation GetFileInformation(IntPtr handle)
        {
            Native.ByHandleFileInformation raw;
            if (!Native.GetFileInformationByHandle(handle, out raw))
                throw Native.LastError("Cannot inspect the duplicated file handle");
            FileInformation info = new FileInformation();
            info.Volume = raw.VolumeSerialNumber;
            info.Index = ((ulong)raw.FileIndexHigh << 32) | raw.FileIndexLow;
            info.Attributes = raw.FileAttributes;
            info.Links = raw.NumberOfLinks;
            info.Length = ((ulong)raw.FileSizeHigh << 32) | raw.FileSizeLow;
            return info;
        }

        private static string GetFinalPath(IntPtr handle)
        {
            int capacity = 512;
            while (capacity <= 32768)
            {
                StringBuilder buffer = new StringBuilder(capacity);
                uint length = Native.GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0);
                if (length == 0) throw Native.LastError("Cannot resolve handle path");
                if (length < buffer.Capacity)
                    return buffer.ToString().Replace('/', '\\');
                capacity = checked((int)length + 1);
            }
            throw new InvalidDataException("Resolved object path exceeds the Windows path limit");
        }

        private static string CanonicalPath(string path)
        {
            if (String.IsNullOrWhiteSpace(path)) throw new InvalidDataException("Handle has no canonical path");
            string normalized = path.Replace('/', '\\');
            while (normalized.Length > 1 && normalized.EndsWith("\\", StringComparison.Ordinal))
                normalized = normalized.Substring(0, normalized.Length - 1);
            return normalized.ToUpperInvariant();
        }

        private static void RejectVolumeRoot(string path)
        {
            StringBuilder buffer = new StringBuilder(32768);
            if (!Native.GetVolumePathName(path, buffer, (uint)buffer.Capacity))
                throw Native.LastError("Cannot resolve the scope volume root");
            if (String.Equals(CanonicalPath(buffer.ToString()), path, StringComparison.Ordinal))
                throw new InvalidDataException("A volume root cannot be a metadata scope");
        }

        private static bool IsWithin(string root, string path)
        {
            if (String.Equals(root, path, StringComparison.Ordinal)) return true;
            return path.StartsWith(root + "\\", StringComparison.Ordinal);
        }

        private static void RequireActiveScope()
        {
            if (activeScope == null) throw new InvalidOperationException("No metadata transaction scope is active");
        }

        private static void RequireScopeObject(NativeObject file)
        {
            if (file.Volume != activeScope.Volume
                || (!IsWithin(activeScope.WorkspacePath, file.Path) && !IsWithin(activeScope.TransactionPath, file.Path)))
                throw new UnauthorizedAccessException("Object handle is outside the active workspace and transaction");
        }

        private static void RequirePublicationObject(NativeObject file)
        {
            string incoming = CanonicalPath(activeScope.TransactionPath + "\\publication\\incoming");
            string old = CanonicalPath(activeScope.TransactionPath + "\\publication\\old");
            if (file.Volume != activeScope.Volume || (!IsWithin(incoming, file.Path) && !IsWithin(old, file.Path)))
                throw new UnauthorizedAccessException("First registration is limited to publication incoming and old objects");
        }

        private static object[] FileId(uint volume, ulong index)
        {
            return new object[] { (long)volume, index };
        }

        private static void ParseFileId(object value, out uint volume, out ulong index)
        {
            object[] fields = value as object[];
            if (fields == null || fields.Length != 2)
                throw new InvalidDataException("file_id must be [volume,index64]");
            long v;
            if (!TryInt64(fields[0], out v) || v < 0 || v > UInt32.MaxValue)
                throw new InvalidDataException("file_id volume is invalid");
            volume = (uint)v;
            if (!TryUInt64(fields[1], out index))
                throw new InvalidDataException("file_id index is invalid");
        }

        private static bool TryInt64(object value, out long result)
        {
            if (value is int) { result = (int)value; return true; }
            if (value is long) { result = (long)value; return true; }
            if (value is uint) { result = (uint)value; return true; }
            if (value is decimal)
            {
                decimal number = (decimal)value;
                if (number >= Int64.MinValue && number <= Int64.MaxValue && Decimal.Truncate(number) == number)
                { result = (long)number; return true; }
            }
            if (value is ulong)
            {
                ulong number = (ulong)value;
                if (number <= Int64.MaxValue) { result = (long)number; return true; }
            }
            result = 0;
            return false;
        }

        private static bool TryUInt64(object value, out ulong result)
        {
            if (value is ulong) { result = (ulong)value; return true; }
            if (value is long) { long number = (long)value; if (number >= 0) { result = (ulong)number; return true; } }
            if (value is uint) { result = (uint)value; return true; }
            if (value is int) { int number = (int)value; if (number >= 0) { result = (uint)number; return true; } }
            if (value is decimal)
            {
                decimal number = (decimal)value;
                if (number >= 0 && number <= UInt64.MaxValue && Decimal.Truncate(number) == number)
                { result = (ulong)number; return true; }
            }
            result = 0;
            return false;
        }

        private static uint GetGrantedAccess(IntPtr handle)
        {
            Native.PublicObjectBasicInformation information = new Native.PublicObjectBasicInformation();
            uint capacity = (uint)Marshal.SizeOf(typeof(Native.PublicObjectBasicInformation));
            uint returned;
            int status = Native.NtQueryObject(handle, 0, out information, capacity, out returned);
            if (status < 0)
                throw new InvalidOperationException("NtQueryObject failed: status=0x" + status.ToString("X8")
                    + ", returned=" + returned + ", capacity=" + capacity);
            if (capacity != 56 || returned < 8 || returned > capacity)
                throw new InvalidDataException("NtQueryObject returned invalid bounds: returned=" + returned + ", capacity=" + capacity);
            return information.GrantedAccess;
        }

        private static DescriptorParts ReadDescriptor(IntPtr handle)
        {
            uint needed;
            Native.GetKernelObjectSecurity(handle, 15, IntPtr.Zero, 0, out needed);
            int error = Marshal.GetLastWin32Error();
            if (needed < 20 || needed > MaximumSecurityDescriptor || error != ErrorInsufficientBuffer)
                throw Native.WithWin32Error(error, "Cannot size the complete security descriptor");
            byte[] bytes = new byte[checked((int)needed)];
            GCHandle pin = GCHandle.Alloc(bytes, GCHandleType.Pinned);
            try
            {
                if (!Native.GetKernelObjectSecurity(handle, 15, pin.AddrOfPinnedObject(), needed, out needed))
                    throw Native.LastError("Cannot capture the complete security descriptor");
            }
            finally { pin.Free(); }
            if (needed != bytes.Length) Array.Resize(ref bytes, checked((int)needed));
            return ValidateDescriptor(bytes);
        }

        private static DescriptorParts ValidateDescriptor(byte[] bytes)
        {
            if (bytes == null || bytes.Length < 20 || bytes.Length > MaximumSecurityDescriptor)
                throw new InvalidDataException("Security descriptor size is outside the allowed range");
            ushort control = BitConverter.ToUInt16(bytes, 2);
            if ((control & SelfRelative) == 0)
                throw new InvalidDataException("Security descriptor is not self-relative");
            for (int index = 0; index < 4; index++)
            {
                uint offset = BitConverter.ToUInt32(bytes, 4 + index * 4);
                if (offset == 0) continue;
                if (offset < 20 || (offset & 3) != 0 || offset >= (uint)bytes.Length)
                    throw new InvalidDataException("Security descriptor contains an invalid offset");
                int partOffset = checked((int)offset);
                if (index < 2)
                {
                    if (partOffset + 8 > bytes.Length || bytes[partOffset] != 1)
                        throw new InvalidDataException("Security descriptor contains an invalid SID");
                    int subAuthorities = bytes[partOffset + 1];
                    if (subAuthorities > 15 || partOffset + 8 + subAuthorities * 4 > bytes.Length)
                        throw new InvalidDataException("Security descriptor contains a truncated SID");
                }
                else
                {
                    if (partOffset + 8 > bytes.Length)
                        throw new InvalidDataException("Security descriptor contains a truncated ACL");
                    int aclSize = BitConverter.ToUInt16(bytes, partOffset + 2);
                    int aceCount = BitConverter.ToUInt16(bytes, partOffset + 4);
                    if (aclSize < 8 || partOffset + aclSize > bytes.Length)
                        throw new InvalidDataException("Security descriptor contains an invalid ACL size");
                    ValidateAcl(bytes, partOffset, aclSize, aceCount);
                }
            }

            DescriptorParts parts = new DescriptorParts();
            parts.Bytes = bytes;
            parts.Control = control;
            parts.SaclPresent = (control & SaclPresent) != 0;
            uint saclOffset = BitConverter.ToUInt32(bytes, 12);
            parts.SaclOffset = saclOffset == 0 ? 0 : checked((int)saclOffset);
            parts.SaclNull = parts.SaclPresent && saclOffset == 0;
            if (saclOffset != 0) parts.SaclLength = BitConverter.ToUInt16(bytes, parts.SaclOffset + 2);
            GCHandle pin = GCHandle.Alloc(bytes, GCHandleType.Pinned);
            try
            {
                if (!Native.IsValidSecurityDescriptor(pin.AddrOfPinnedObject()))
                    throw new InvalidDataException("Security descriptor is not valid");
            }
            finally { pin.Free(); }
            return parts;
        }

        private static void ValidateAcl(byte[] bytes, int offset, int size, int aceCount)
        {
            int position = offset + 8;
            int end = offset + size;
            for (int i = 0; i < aceCount; i++)
            {
                if (position + 4 > end) throw new InvalidDataException("Security descriptor contains a truncated ACE");
                int aceSize = BitConverter.ToUInt16(bytes, position + 2);
                if (aceSize < 4 || position + aceSize > end)
                    throw new InvalidDataException("Security descriptor contains an invalid ACE size");
                position += aceSize;
            }
        }

        private static string ToSddl(byte[] descriptor, uint information)
        {
            GCHandle pin = GCHandle.Alloc(descriptor, GCHandleType.Pinned);
            IntPtr text = IntPtr.Zero;
            try
            {
                if (!Native.ConvertSecurityDescriptorToStringSecurityDescriptor(pin.AddrOfPinnedObject(), 1, information, out text, IntPtr.Zero))
                    throw Native.LastError("Cannot render captured SACL");
                return Marshal.PtrToStringUni(text) ?? String.Empty;
            }
            finally
            {
                if (text != IntPtr.Zero) Native.LocalFree(text);
                pin.Free();
            }
        }

        private static void ApplySacl(IntPtr handle, DescriptorParts descriptor, bool final)
        {
            byte[] bytes = (byte[])descriptor.Bytes.Clone();
            ushort control = BitConverter.ToUInt16(bytes, 2);
            if (!descriptor.SaclPresent)
            {
                DescriptorParts current = ReadDescriptor(handle);
                bool originalProtection = (descriptor.Control & SaclProtected) != 0;
                bool currentProtection = (current.Control & SaclProtected) != 0;
                if (!current.SaclPresent && currentProtection == originalProtection)
                    return;
            }
            bool protectedSacl = final ? (control & SaclProtected) != 0 : true;
            if (protectedSacl) control = (ushort)(control | SaclProtected);
            else control = (ushort)(control & ~SaclProtected);
            BitConverter.GetBytes(control).CopyTo(bytes, 2);
            GCHandle pin = GCHandle.Alloc(bytes, GCHandleType.Pinned);
            try
            {
                uint information = SaclSecurityInformation |
                    (protectedSacl ? ProtectedSaclSecurityInformation : UnprotectedSaclSecurityInformation);
                int status = Native.NtSetSecurityObject(handle, information, pin.AddrOfPinnedObject());
                if (status < 0)
                    throw new InvalidOperationException("SACL update failed with NTSTATUS 0x" + status.ToString("X8"));
            }
            finally { pin.Free(); }
        }

        private static void ClearScope()
        {
            Scope scope = activeScope;
            activeScope = null;
            if (scope == null) return;
            foreach (NativeObject item in scope.Registered.Values) item.Dispose();
            scope.Registered.Clear();
            scope.RecoveryDescriptors.Clear();
            if (scope.Transaction != null) scope.Transaction.Dispose();
            if (scope.Workspace != null) scope.Workspace.Dispose();
        }

        private static void RejectDuplicateObjectKeys(string json)
        {
            RejectDuplicateObjectKeys(json, Json);
        }

        private static void RejectDuplicateObjectKeys(string json, JavaScriptSerializer serializer)
        {
            int position = 0;
            SkipWhitespace(json, ref position);
            ScanJsonValue(json, ref position, 0, serializer);
            SkipWhitespace(json, ref position);
            if (position != json.Length) throw new InvalidDataException("Invalid JSON trailing data");
        }

        private static void ScanJsonValue(string json, ref int position, int depth, JavaScriptSerializer serializer)
        {
            if (depth > 32 || position >= json.Length) throw new InvalidDataException("Invalid or excessively nested JSON");
            char c = json[position];
            if (c == '{')
            {
                position++;
                SkipWhitespace(json, ref position);
                HashSet<string> keys = new HashSet<string>(StringComparer.Ordinal);
                if (position < json.Length && json[position] == '}') { position++; return; }
                while (true)
                {
                    SkipWhitespace(json, ref position);
                    string key = ScanJsonString(json, ref position, serializer);
                    if (!keys.Add(key)) throw new InvalidDataException("Duplicate broker request field");
                    SkipWhitespace(json, ref position);
                    if (position >= json.Length || json[position++] != ':') throw new InvalidDataException("Invalid JSON object");
                    SkipWhitespace(json, ref position);
                    ScanJsonValue(json, ref position, depth + 1, serializer);
                    SkipWhitespace(json, ref position);
                    if (position >= json.Length) throw new InvalidDataException("Invalid JSON object");
                    char delimiter = json[position++];
                    if (delimiter == '}') return;
                    if (delimiter != ',') throw new InvalidDataException("Invalid JSON object");
                }
            }
            if (c == '[')
            {
                position++;
                SkipWhitespace(json, ref position);
                if (position < json.Length && json[position] == ']') { position++; return; }
                while (true)
                {
                    SkipWhitespace(json, ref position);
                    ScanJsonValue(json, ref position, depth + 1, serializer);
                    SkipWhitespace(json, ref position);
                    if (position >= json.Length) throw new InvalidDataException("Invalid JSON array");
                    char delimiter = json[position++];
                    if (delimiter == ']') return;
                    if (delimiter != ',') throw new InvalidDataException("Invalid JSON array");
                }
            }
            if (c == '"') { ScanJsonString(json, ref position, serializer); return; }
            int start = position;
            while (position < json.Length && !Char.IsWhiteSpace(json[position]) && json[position] != ',' && json[position] != ']' && json[position] != '}')
                position++;
            if (position == start) throw new InvalidDataException("Invalid JSON value");
        }

        private static string ScanJsonString(string json, ref int position, JavaScriptSerializer serializer)
        {
            int start = position;
            if (position >= json.Length || json[position++] != '"') throw new InvalidDataException("Expected a JSON string");
            bool escaped = false;
            while (position < json.Length)
            {
                char c = json[position++];
                if (escaped) { escaped = false; continue; }
                if (c == '\\') { escaped = true; continue; }
                if (c == '"')
                {
                    string encoded = json.Substring(start, position - start);
                    return serializer.Deserialize<string>(encoded);
                }
                if (c < 0x20) throw new InvalidDataException("Invalid JSON string");
            }
            throw new InvalidDataException("Unterminated JSON string");
        }

        private static void SkipWhitespace(string json, ref int position)
        {
            while (position < json.Length && (json[position] == ' ' || json[position] == '\t' || json[position] == '\r' || json[position] == '\n'))
                position++;
        }

        private static class Native
        {
            [StructLayout(LayoutKind.Sequential, Pack = 4)]
            internal struct PublicObjectBasicInformation
            {
                public uint Attributes;
                public uint GrantedAccess;
                public uint HandleCount;
                public uint PointerCount;
                public uint Reserved0;
                public uint Reserved1;
                public uint Reserved2;
                public uint Reserved3;
                public uint Reserved4;
                public uint Reserved5;
                public uint Reserved6;
                public uint Reserved7;
                public uint Reserved8;
                public uint Reserved9;
            }

            [StructLayout(LayoutKind.Explicit, Size = 24)]
            internal struct FileIdDescriptor
            {
                [FieldOffset(0)] public uint Size;
                [FieldOffset(4)] public uint Type;
                [FieldOffset(8)] public long FileId;
            }

            [StructLayout(LayoutKind.Sequential, Pack = 4)]
            internal struct ByHandleFileInformation
            {
                public uint FileAttributes;
                public long CreationTime;
                public long LastAccessTime;
                public long LastWriteTime;
                public uint VolumeSerialNumber;
                public uint FileSizeHigh;
                public uint FileSizeLow;
                public uint NumberOfLinks;
                public uint FileIndexHigh;
                public uint FileIndexLow;
            }

            [StructLayout(LayoutKind.Sequential)]
            internal struct Luid
            {
                public uint LowPart;
                public int HighPart;
            }

            [StructLayout(LayoutKind.Sequential)]
            internal struct LuidAndAttributes
            {
                public Luid Luid;
                public uint Attributes;
            }

            [StructLayout(LayoutKind.Sequential)]
            internal struct TokenPrivileges
            {
                public uint PrivilegeCount;
                public LuidAndAttributes Privilege;
            }

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern IntPtr OpenProcess(uint access, bool inherit, uint processId);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern bool CloseHandle(IntPtr handle);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern IntPtr GetCurrentProcess();

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern bool DuplicateHandle(IntPtr sourceProcess, IntPtr sourceHandle, IntPtr targetProcess,
                out IntPtr targetHandle, uint desiredAccess, bool inherit, uint options);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern IntPtr ReOpenFile(IntPtr originalFile, uint desiredAccess, uint shareMode, uint flags);

            [DllImport("kernel32.dll", EntryPoint = "OpenFileById", SetLastError = true)]
            internal static extern IntPtr OpenFileById(IntPtr volumeHint, ref FileIdDescriptor fileId,
                uint desiredAccess, uint shareMode, IntPtr securityAttributes, uint flags);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern bool GetFileInformationByHandle(IntPtr handle, out ByHandleFileInformation information);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern bool ReadFile(IntPtr handle, byte[] buffer, uint bytesToRead, out uint bytesRead, IntPtr overlapped);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern bool SetFilePointerEx(IntPtr handle, long distance, out long newPosition, uint moveMethod);

            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            internal static extern uint GetFinalPathNameByHandle(IntPtr handle, StringBuilder path, uint length, uint flags);

            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            internal static extern bool GetVolumePathName(string fileName, StringBuilder volumePathName, uint bufferLength);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern bool GetNamedPipeServerProcessId(IntPtr pipe, out uint serverProcessId);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern int WaitForSingleObject(IntPtr handle, uint milliseconds);

            [DllImport("advapi32.dll", SetLastError = true)]
            internal static extern bool OpenProcessToken(IntPtr process, uint desiredAccess, out IntPtr token);

            [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            internal static extern bool LookupPrivilegeValue(string systemName, string name, out Luid luid);

            [DllImport("advapi32.dll", SetLastError = true)]
            internal static extern bool AdjustTokenPrivileges(IntPtr token, bool disableAll, ref TokenPrivileges newState,
                uint bufferLength, IntPtr previousState, IntPtr returnLength);

            [DllImport("advapi32.dll", SetLastError = true)]
            internal static extern bool GetKernelObjectSecurity(IntPtr handle, uint information, IntPtr descriptor,
                uint length, out uint needed);

            [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            internal static extern bool ConvertSecurityDescriptorToStringSecurityDescriptor(IntPtr descriptor,
                uint revision, uint information, out IntPtr text, IntPtr textLength);

            [DllImport("advapi32.dll")]
            internal static extern bool IsValidSecurityDescriptor(IntPtr descriptor);

            [DllImport("kernel32.dll", SetLastError = true)]
            internal static extern IntPtr LocalFree(IntPtr memory);

            [DllImport("ntdll.dll")]
            internal static extern int NtQueryObject(IntPtr handle, int objectInformationClass,
                out PublicObjectBasicInformation information, uint informationLength, out uint returnLength);

            [DllImport("ntdll.dll")]
            internal static extern int NtSetSecurityObject(IntPtr handle, uint securityInformation, IntPtr descriptor);

            internal static Win32Exception LastError(string message)
            {
                int error = Marshal.GetLastWin32Error();
                return WithWin32Error(error, message);
            }

            internal static Win32Exception WithWin32Error(int error, string message)
            {
                string annotated = message + " (Win32 error " + error + ", 0x" + error.ToString("X8") + ")";
                return new Win32Exception(error, annotated);
            }
        }
    }
}
