// Host-only recovery for temporary ACE cleanup after a completed network
// request. This entry point never launches a child or provisions a profile.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;

namespace MewCode.Native.Windows
{
    public static class MewCodeNetworkCleanup
    {
        public static int Main(string[] args)
        {
            return MewCodeAppContainerProbe.NetworkCleanupRecoveryMain(args);
        }
    }

    internal sealed class NetworkCleanupEvidence
    {
        public string schema { get; set; }
        public string request_id { get; set; }
        public string request_sha256 { get; set; }
        public string backend { get; set; }
        public bool? completed { get; set; }
        public bool? job_cleanup_ok { get; set; }
        public bool? profile_cleanup_ok { get; set; }
        public bool? acl_cleanup_ok { get; set; }
    }

    public static partial class MewCodeAppContainerProbe
    {
        private const int MaxRecoveryDiagnosticChars = 2048;

        internal static int NetworkCleanupRecoveryMain(string[] args)
        {
            var output = new RecoveryOutput();
            Mutex networkLock = null;
            LaunchState state = null;
            try
            {
                if (args == null || args.Length != 0)
                    throw new InvalidDataException("Network cleanup recovery accepts only the bounded request on stdin.");

                EnsureHostProcessToken();
                networkLock = AcquireNetworkProfileLock();

                byte[] requestBytes = ReadRecoveryRequestBytes();
                string payload = new UTF8Encoding(false, true).GetString(requestBytes, 0, requestBytes.Length);
                if (payload.Length > 0 && payload[0] == '\uFEFF') payload = payload.Substring(1);
                StagedRuntimeRequestEnvelope envelope = NewSerializer().Deserialize<StagedRuntimeRequestEnvelope>(payload);
                ValidatedRequest request = ValidateStagedRuntimeRequest(envelope);
                if (envelope.request.schema != "mewcode-lpac-network-request-v1" ||
                    envelope.request.runtime_network != true || !request.RuntimeNetwork)
                    throw new InvalidDataException("Recovery requires the validated network staged-request schema.");

                output.request_id = envelope.request_id;
                string requestSha256 = HashRequestBytes(requestBytes, requestBytes.Length);
                string transaction, cleanupDirectory, oldReceiptPath, recoveryReceiptPath;
                ValidateRecoveryPaths(envelope, request, out transaction, out cleanupDirectory,
                    out oldReceiptPath, out recoveryReceiptPath);
                IntPtr cleanupDirectoryPin = OpenRecoveryDirectoryPin(cleanupDirectory);
                try
                {
                    NetworkCleanupEvidence oldEvidence = ReadRecoveryEvidence(oldReceiptPath);
                    ValidateRecoveryEvidence(oldEvidence, envelope.request_id, requestSha256);
                    // The old proof certifies that the original process job and
                    // profile are gone. ACL cleanup is the only operation here.
                    output.job_cleanup_ok = oldEvidence.job_cleanup_ok.Value;
                    output.profile_cleanup_ok = oldEvidence.profile_cleanup_ok.Value;
                    output.receipt_path = recoveryReceiptPath;

                    RevalidateRecoveryPaths(envelope, request, transaction, cleanupDirectory,
                        oldReceiptPath, recoveryReceiptPath);
                    NetworkProfileDetails profile = NetworkProfileStatus();
                    state = new LaunchState {
                        RuntimeRequest = true,
                        RuntimeNetwork = true,
                        FixtureRoot = request.FixtureRoot,
                        ProfileName = profile.ProfileName,
                        ProfileSid = profile.ProfileSid,
                        UserSid = profile.UserSid
                    };
                    RebuildGrantRules(request, state, new SecurityIdentifier(profile.ProfileSid));
                    PinRuntimePaths(request, state);

                    var cleanup = new ProbeReceipt {
                        stdout = "", stderr = "", error = "", capabilities = new string[0],
                        job_cleanup_ok = oldEvidence.job_cleanup_ok.Value,
                        profile_cleanup_ok = oldEvidence.profile_cleanup_ok.Value,
                        acl_cleanup_ok = false
                    };
                    WithAclLock(delegate { CleanupAclGrants(state, request, cleanup); });
                    if (!cleanup.job_cleanup_ok || !cleanup.profile_cleanup_ok || !cleanup.acl_cleanup_ok)
                        throw new InvalidDataException("Exact temporary ACL cleanup did not complete; no recovery proof was written.");
                    VerifyNoRecoveryAceRemains(request, new SecurityIdentifier(profile.ProfileSid));

                    PersistRecoveryEvidence(recoveryReceiptPath, envelope.request_id, requestSha256, cleanup);
                    output.acl_cleanup_ok = true;
                    output.success = true;
                    output.diagnostic = "Temporary ACL cleanup completed.";
                }
                finally
                {
                    CloseRecoveryDirectoryPin(cleanupDirectoryPin);
                }
            }
            catch (Exception error)
            {
                output.success = false;
                output.diagnostic = ClipRecoveryDiagnostic(Describe(error));
            }
            finally
            {
                if (state != null)
                {
                    foreach (IntPtr pin in state.RuntimePathPins) CloseHandle(pin);
                    state.RuntimePathPins.Clear();
                }
                if (networkLock != null)
                {
                    try { networkLock.ReleaseMutex(); }
                    catch (Exception error)
                    {
                        if (output.success)
                            output.diagnostic = ClipRecoveryDiagnostic("Cleanup proof was written; releasing the profile lock reported: " + Describe(error));
                    }
                    finally { networkLock.Dispose(); }
                }
            }

            Console.OutputEncoding = new UTF8Encoding(false);
            Console.WriteLine(NewSerializer().Serialize(new {
                schema = "mewcode-network-cleanup-recovery-v1",
                request_id = output.request_id,
                success = output.success,
                job_cleanup_ok = output.job_cleanup_ok,
                profile_cleanup_ok = output.profile_cleanup_ok,
                acl_cleanup_ok = output.acl_cleanup_ok,
                receipt_path = output.receipt_path,
                diagnostic = ClipRecoveryDiagnostic(output.diagnostic)
            }));
            return output.success ? 0 : 1;
        }

        private sealed class RecoveryOutput
        {
            public string request_id;
            public bool success;
            public bool job_cleanup_ok;
            public bool profile_cleanup_ok;
            public bool acl_cleanup_ok;
            public string receipt_path;
            public string diagnostic = "Recovery did not complete.";
        }

        private static byte[] ReadRecoveryRequestBytes()
        {
            var buffer = new byte[MaxRequestBytes + 1];
            int count = 0, read;
            using (Stream input = Console.OpenStandardInput())
            {
                while (count < buffer.Length && (read = input.Read(buffer, count, buffer.Length - count)) > 0)
                    count += read;
            }
            if (count == 0 || count > MaxRequestBytes)
                throw new InvalidDataException("Invalid bounded staged-request size.");
            var result = new byte[count];
            Buffer.BlockCopy(buffer, 0, result, 0, count);
            return result;
        }

        private static void ValidateRecoveryPaths(StagedRuntimeRequestEnvelope envelope,
            ValidatedRequest request, out string transaction, out string cleanupDirectory,
            out string oldReceiptPath, out string recoveryReceiptPath)
        {
            transaction = Path.GetDirectoryName(TrimSeparators(request.StagedWorkspace));
            if (String.IsNullOrEmpty(transaction))
                throw new InvalidDataException("The staged transaction parent is unavailable.");
            transaction = ValidateExistingPath(transaction, "recovery transaction", true);
            cleanupDirectory = Path.GetFullPath(Path.Combine(transaction, "cleanup"));
            cleanupDirectory = ValidateExistingPath(cleanupDirectory, "recovery cleanup directory", true);
            if ((File.GetAttributes(cleanupDirectory) & FileAttributes.Device) != 0)
                throw new InvalidDataException("Recovery cleanup parent must be an ordinary directory.");

            oldReceiptPath = Path.GetFullPath(Path.Combine(cleanupDirectory, "receipt.json"));
            recoveryReceiptPath = Path.GetFullPath(Path.Combine(cleanupDirectory, "recovery-receipt.json"));
            string requestedReceipt = ValidateAbsolutePath(envelope.cleanup_receipt_path, "cleanup_receipt_path");
            if (!String.Equals(TrimSeparators(requestedReceipt), TrimSeparators(oldReceiptPath),
                StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("The input cleanup receipt must be the fixed transaction/cleanup/receipt.json path.");

            EnsureRecoveryPathOutsideGrants(envelope, request, transaction, cleanupDirectory,
                oldReceiptPath, recoveryReceiptPath);
            ValidateExistingPath(oldReceiptPath, "original cleanup evidence", false);
            RejectHardLinkedFile(oldReceiptPath, "original cleanup evidence");
            CheckNoReparseAncestors(cleanupDirectory, "recovery receipt parent");
            if (File.Exists(recoveryReceiptPath) || Directory.Exists(recoveryReceiptPath))
                throw new IOException("The fixed recovery receipt already exists; refusing to overwrite it.");
        }

        private static void EnsureRecoveryPathOutsideGrants(StagedRuntimeRequestEnvelope envelope,
            ValidatedRequest request, string transaction, string cleanupDirectory,
            string oldReceiptPath, string recoveryReceiptPath)
        {
            string original = ValidateExistingPath(envelope.original_workspace, "original_workspace", true);
            string[] hostPaths = { cleanupDirectory, oldReceiptPath, recoveryReceiptPath };
            string[] protectedRoots = { original, request.StagedWorkspace };
            if (request.StagedRuntimeRoots != null)
            {
                var extended = new string[protectedRoots.Length + request.StagedRuntimeRoots.Length];
                Array.Copy(protectedRoots, extended, protectedRoots.Length);
                Array.Copy(request.StagedRuntimeRoots, 0, extended, protectedRoots.Length,
                    request.StagedRuntimeRoots.Length);
                protectedRoots = extended;
            }
            // The fixed host cleanup files must be disjoint from the original,
            // staged, and private runtime roots, while the staged/runtime roots
            // themselves remain legitimate child-grant targets.
            foreach (string hostPath in hostPaths)
                foreach (string protectedRoot in protectedRoots)
                    if (PathsOverlap(hostPath, protectedRoot))
                        throw new InvalidDataException("A host cleanup path overlaps an original, staged, or private runtime root.");
            foreach (PathGrant grant in request.Grants.Values)
            {
                foreach (string hostPath in hostPaths)
                    if (PathsOverlap(hostPath, grant.Path))
                        throw new InvalidDataException("A host cleanup path overlaps a child-granted path.");
            }
            if (!String.Equals(TrimSeparators(Path.GetDirectoryName(cleanupDirectory)),
                TrimSeparators(transaction), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("The cleanup directory is outside its fixed transaction.");
        }

        private static void RevalidateRecoveryPaths(StagedRuntimeRequestEnvelope envelope,
            ValidatedRequest request, string transaction, string cleanupDirectory,
            string oldReceiptPath, string recoveryReceiptPath)
        {
            string validatedWorkspace = ValidateExistingPath(request.StagedWorkspace, "staged workspace", true);
            if (!String.Equals(validatedWorkspace, request.StagedWorkspace, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("The staged workspace path changed during recovery.");
            ValidateStagedTree(request.StagedWorkspace);
            if (request.StagedRuntimeRoots != null)
                foreach (string root in request.StagedRuntimeRoots) ValidateStagedTree(root);
            ValidateExistingPath(envelope.original_workspace, "original_workspace", true);
            ValidateExistingPath(transaction, "recovery transaction", true);
            ValidateExistingPath(cleanupDirectory, "recovery cleanup directory", true);
            ValidateExistingPath(oldReceiptPath, "original cleanup evidence", false);
            RejectHardLinkedFile(oldReceiptPath, "original cleanup evidence");
            CheckNoReparseAncestors(cleanupDirectory, "recovery receipt parent");
            foreach (PathGrant grant in request.Grants.Values)
            {
                string path = ValidateExistingPath(grant.Path, "recovery child path", grant.IsDirectory);
                if (!grant.IsDirectory) RejectHardLinkedFile(path, "recovery child file");
            }
            EnsureRecoveryPathOutsideGrants(envelope, request, transaction, cleanupDirectory,
                oldReceiptPath, recoveryReceiptPath);
        }

        private static NetworkCleanupEvidence ReadRecoveryEvidence(string path)
        {
            CheckNoReparseAncestors(path, "original cleanup evidence");
            IntPtr handle = OpenFileForLinkCount(path, GenericRead, FileShareRead,
                IntPtr.Zero, OpenExisting, FileFlagOpenReparsePoint, IntPtr.Zero);
            if (handle == IntPtr.Zero || handle == InvalidHandleValue)
                throw LastWin32("Could not safely open the original cleanup evidence.");
            FileStream stream = null;
            try
            {
                BY_HANDLE_FILE_INFORMATION information;
                if (!GetFileInformationByHandle(handle, out information))
                    throw LastWin32("Could not inspect the original cleanup evidence handle.");
                if ((information.FileAttributes & (uint)FileAttributes.ReparsePoint) != 0 ||
                    (information.FileAttributes & (uint)FileAttributes.Directory) != 0 ||
                    information.NumberOfLinks != 1)
                    throw new InvalidDataException("The original cleanup evidence must be an ordinary, single-link file.");
                var safeHandle = new Microsoft.Win32.SafeHandles.SafeFileHandle(handle, true);
                handle = IntPtr.Zero;
                stream = new FileStream(safeHandle, FileAccess.Read);
                if (!SetHandleInformation(stream.SafeFileHandle.DangerousGetHandle(), HandleFlagInherit, 0))
                    throw LastWin32("Could not make the cleanup evidence handle non-inheritable.");
                if (stream.Length <= 0 || stream.Length > MaxCleanupEvidenceBytes)
                    throw new InvalidDataException("The original cleanup evidence is empty or oversized.");
                byte[] bytes = new byte[(int)stream.Length];
                int offset = 0, read;
                while (offset < bytes.Length && (read = stream.Read(bytes, offset, bytes.Length - offset)) > 0)
                    offset += read;
                if (offset != bytes.Length || stream.ReadByte() != -1)
                    throw new InvalidDataException("The original cleanup evidence changed while it was read.");
                string json = new UTF8Encoding(false, true).GetString(bytes, 0, bytes.Length);
                NetworkCleanupEvidence evidence = NewSerializer().Deserialize<NetworkCleanupEvidence>(json);
                if (evidence == null || !evidence.completed.HasValue || !evidence.job_cleanup_ok.HasValue ||
                    !evidence.profile_cleanup_ok.HasValue || !evidence.acl_cleanup_ok.HasValue)
                    throw new InvalidDataException("Original cleanup evidence is missing required fields.");
                string canonical = NewSerializer().Serialize(new {
                    schema = evidence.schema,
                    request_id = evidence.request_id,
                    request_sha256 = evidence.request_sha256,
                    backend = evidence.backend,
                    completed = evidence.completed.Value,
                    job_cleanup_ok = evidence.job_cleanup_ok.Value,
                    profile_cleanup_ok = evidence.profile_cleanup_ok.Value,
                    acl_cleanup_ok = evidence.acl_cleanup_ok.Value
                });
                byte[] canonicalBytes = new UTF8Encoding(false).GetBytes(canonical);
                if (canonicalBytes.Length > MaxCleanupEvidenceBytes ||
                    !String.Equals(json, canonical, StringComparison.Ordinal))
                    throw new InvalidDataException("Original cleanup evidence is not the exact canonical native receipt.");
                return evidence;
            }
            finally
            {
                if (stream != null) stream.Dispose();
                if (handle != IntPtr.Zero && handle != InvalidHandleValue) CloseHandle(handle);
            }
        }

        private static void ValidateRecoveryEvidence(NetworkCleanupEvidence evidence,
            string requestId, string requestSha256)
        {
            if (evidence == null || evidence.schema != "mewcode-native-cleanup-v1" ||
                evidence.backend != "mewcode-windows-lpac" || evidence.request_id != requestId ||
                evidence.request_sha256 != requestSha256 || evidence.completed != true ||
                evidence.job_cleanup_ok != true || evidence.profile_cleanup_ok != true ||
                !evidence.acl_cleanup_ok.HasValue)
                throw new InvalidDataException("Original cleanup evidence does not prove this request's completed job and profile cleanup.");

        }

        private static void RebuildGrantRules(ValidatedRequest request, LaunchState state,
            SecurityIdentifier packageSid)
        {
            foreach (PathGrant grant in request.Grants.Values)
            {
                if (grant.StagedMutableTree || grant.StagedReadOnly)
                {
                    FileSystemRights stagedRights = grant.StagedMutableTree ? FileSystemRights.Modify :
                        FileSystemRights.Write | FileSystemRights.Delete;
                    InheritanceFlags inheritance = grant.IsDirectory ?
                        InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit : InheritanceFlags.None;
                    grant.Rule = new FileSystemAccessRule(packageSid, stagedRights, inheritance,
                        PropagationFlags.None, grant.StagedMutableTree ? AccessControlType.Allow : AccessControlType.Deny);
                }
                else
                {
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
                    grant.Rule = new FileSystemAccessRule(packageSid, (FileSystemRights)rights,
                        InheritanceFlags.None, PropagationFlags.None, AccessControlType.Allow);
                }
                // Recovery reconstructs only the original exact ACE. Never call
                // AddAccessRule, SetAccessControl, or any grant-application path.
                state.AppliedGrants.Add(grant);
            }
        }

        private static IntPtr OpenRecoveryDirectoryPin(string path)
        {
            CheckNoReparseAncestors(path, "recovery cleanup directory");
            IntPtr handle = OpenFileForLinkCount(path, FileReadAttributes,
                FileShareRead | FileShareWrite, IntPtr.Zero, OpenExisting,
                FileFlagOpenReparsePoint | FileFlagBackupSemantics, IntPtr.Zero);
            if (handle == IntPtr.Zero || handle == InvalidHandleValue)
                throw LastWin32("Could not pin the recovery cleanup directory.");
            try
            {
                BY_HANDLE_FILE_INFORMATION information;
                if (!GetFileInformationByHandle(handle, out information) ||
                    (information.FileAttributes & (uint)FileAttributes.ReparsePoint) != 0 ||
                    (information.FileAttributes & (uint)FileAttributes.Directory) == 0)
                    throw new InvalidDataException("The recovery cleanup path is not an ordinary directory.");
                if (!SetHandleInformation(handle, HandleFlagInherit, 0))
                    throw LastWin32("Could not make the recovery cleanup directory handle non-inheritable.");
                return handle;
            }
            catch
            {
                CloseHandle(handle);
                throw;
            }
        }

        private static void VerifyNoRecoveryAceRemains(ValidatedRequest request, SecurityIdentifier sid)
        {
            var checkedPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (PathGrant grant in request.Grants.Values)
                VerifyRecoveryPathAcl(grant.Path, sid, checkedPaths);
            var roots = new List<string> { request.StagedWorkspace };
            if (request.StagedRuntimeRoots != null) roots.AddRange(request.StagedRuntimeRoots);
            foreach (string root in roots)
            {
                var pending = new Stack<string>();
                pending.Push(root);
                int count = 0;
                while (pending.Count > 0)
                {
                    if (++count > MaxStagedEntries) throw new InvalidDataException("Recovery verification tree exceeds its bound.");
                    string path = pending.Pop();
                    CheckNoReparseAncestors(path, "recovery verification");
                    FileAttributes attributes = File.GetAttributes(NativeFilesystemPath(path));
                    VerifyRecoveryPathAcl(path, sid, checkedPaths);
                    if ((attributes & FileAttributes.Directory) != 0)
                        foreach (string child in Directory.EnumerateFileSystemEntries(NativeFilesystemPath(path))) pending.Push(PlainFilesystemPath(child));
                    else RejectHardLinkedFile(path, "recovery verification file");
                }
            }
        }

        private static void VerifyRecoveryPathAcl(string path, SecurityIdentifier sid, HashSet<string> checkedPaths)
        {
            if (!checkedPaths.Add(path)) return;
            CheckNoReparseAncestors(path, "recovery ACL verification");
            bool directory = (File.GetAttributes(NativeFilesystemPath(path)) & FileAttributes.Directory) != 0;
            FileSystemSecurity security = directory ? (FileSystemSecurity)Directory.GetAccessControl(NativeFilesystemPath(path)) : File.GetAccessControl(NativeFilesystemPath(path));
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
                if (sid.Equals(rule.IdentityReference))
                    throw new InvalidDataException("Temporary package SID ACE remains after exact cleanup: " + path);
        }

        private static void CloseRecoveryDirectoryPin(IntPtr handle)
        {
            if (handle != IntPtr.Zero && handle != InvalidHandleValue) CloseHandle(handle);
        }

        private static void PersistRecoveryEvidence(string path, string requestId,
            string requestSha256, ProbeReceipt cleanup)
        {
            FileStream stream = null;
            bool created = false;
            bool completed = false;
            try
            {
                stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
                created = true;
                if (!SetHandleInformation(stream.SafeFileHandle.DangerousGetHandle(), HandleFlagInherit, 0))
                    throw LastWin32("Could not make the recovery evidence handle non-inheritable.");
                WriteCleanupEvidence(stream, requestId, requestSha256, cleanup);
                completed = true;
            }
            finally
            {
                if (stream != null) stream.Dispose();
                if (created && !completed)
                {
                    try { File.Delete(path); }
                    catch { /* A missing/invalid recovery proof is never treated as success. */ }
                }
            }
        }

        private static string ClipRecoveryDiagnostic(string value)
        {
            if (String.IsNullOrEmpty(value)) return "Recovery did not complete.";
            return value.Length <= MaxRecoveryDiagnosticChars ? value : value.Substring(0, MaxRecoveryDiagnosticChars);
        }
    }
}
