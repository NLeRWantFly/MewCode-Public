// Explicitly authorized, one-shot synthetic experiment. Never a production launcher.
// Changes only DACLs on NUL and the actual Windows drive root, and restores both.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

public sealed class HostAclArtifact
{
    public string path { get; set; }
    public string sha256 { get; set; }
}
public sealed class HostAclCase
{
    public string name { get; set; }
    public string request { get; set; }
    public string request_sha256 { get; set; }
}
public sealed class HostAclManifest
{
    public string schema { get; set; }
    public string fixture_root { get; set; }
    public string system_drive_root { get; set; }
    public string helper { get; set; }
    public string helper_sha256 { get; set; }
    public HostAclCase[] cases { get; set; }
    public HostAclArtifact[] artifacts { get; set; }
}
public sealed class HostAclBackup
{
    public string label { get; set; }
    public string target { get; set; }
    public string path { get; set; }
    public string sha256 { get; set; }
    public int mask { get; set; }
}
public sealed class HostAclRecovery
{
    public string schema { get; set; }
    public string fixture_root { get; set; }
    public string mutex_name { get; set; }
    public HostAclBackup[] backups { get; set; }
}

public static class MewCodeNativeHostAclTrial
{
    private const string Device = "\\\\.\\NUL";
    private const int NullMask = 0x001201bf;
    private const int RootMask = 0x00120088;
    private const int DeadlineSeconds = 240;
    private const uint ReadControl = 0x20000, WriteDac = 0x40000;
    private const uint OwnerGroupDacl = 7, DaclOnly = 4;
    private static readonly IntPtr InvalidHandle = new IntPtr(-1);
    private static readonly string[] PackageSids = { "S-1-15-2-1", "S-1-15-2-2" };
    private static readonly string[] CaseNames = {
        "ac_nul", "ac_cwd", "ac_git_version", "ac_git_status", "ac_powershell",
        "lpac_nul", "lpac_cwd", "lpac_git_version", "lpac_git_status", "lpac_powershell" };

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr CreateFileW(string path, uint access, uint share, IntPtr security,
        uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern uint GetWindowsDirectoryW(StringBuilder path, uint length);
    [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = true)]
    private static extern bool GetKernelObjectSecurity(IntPtr handle, uint info,
        byte[] descriptor, uint length, out uint needed);
    [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = true)]
    private static extern bool SetKernelObjectSecurity(IntPtr handle, uint info, byte[] descriptor);
    [DllImport("advapi32.dll", ExactSpelling = true)]
    private static extern uint SetSecurityInfo(IntPtr handle, int objectType, uint info,
        IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);
    [DllImport("ntdll.dll", ExactSpelling = true)]
    private static extern int NtSetSecurityObject(IntPtr handle, uint info, byte[] descriptor);
    [StructLayout(LayoutKind.Sequential)]
    private struct FileInformation
    {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    private static extern bool GetFileInformationByHandle(IntPtr handle, out FileInformation info);

    private static JavaScriptSerializer Json()
    {
        return new JavaScriptSerializer { MaxJsonLength = 2 * 1024 * 1024, RecursionLimit = 30 };
    }
    private static string Hash(byte[] value)
    {
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(value)).Replace("-", "").ToLowerInvariant();
    }
    private static byte[] Bytes(GenericAce ace)
    {
        var value = new byte[ace.BinaryLength]; ace.GetBinaryForm(value, 0); return value;
    }
    private static byte[] Bytes(RawSecurityDescriptor sd)
    {
        var value = new byte[sd.BinaryLength]; sd.GetBinaryForm(value, 0); return value;
    }
    private static string DaclHash(byte[] value)
    {
        var acl = new RawSecurityDescriptor(value, 0).DiscretionaryAcl;
        if (acl == null) throw new InvalidDataException("Missing DACL");
        var bytes = new byte[acl.BinaryLength]; acl.GetBinaryForm(bytes, 0); return Hash(bytes);
    }
    private static string SystemRoot()
    {
        var buffer = new StringBuilder(32768);
        uint length = GetWindowsDirectoryW(buffer, (uint)buffer.Capacity);
        if (length == 0 || length >= buffer.Capacity) throw new InvalidOperationException("Windows root lookup failed");
        string root = Path.GetPathRoot(buffer.ToString());
        if (root == null || root.Length != 3 || root[1] != ':' || root[2] != '\\')
            throw new InvalidDataException("Expected a local Windows drive root");
        return root;
    }
    private static void RequireAdmin()
    {
        if (!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))
            throw new UnauthorizedAccessException("An explicitly approved Administrator token is required");
    }
    private static IntPtr OpenTarget(string path, bool write)
    {
        if (path != Device && !String.Equals(path, SystemRoot(), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Unexpected system target");
        IntPtr handle = CreateFileW(path, ReadControl | (write ? WriteDac : 0), 7,
            IntPtr.Zero, 3, path == Device ? 0u : 0x02000000u, IntPtr.Zero);
        if (handle == InvalidHandle) throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot open fixed system target");
        return handle;
    }
    private static byte[] ReadDescriptor(IntPtr handle)
    {
        uint required;
        bool first = GetKernelObjectSecurity(handle, OwnerGroupDacl, null, 0, out required);
        int error = Marshal.GetLastWin32Error();
        if (first || error != 122 || required == 0 || required > 65536)
            throw new Win32Exception(error, "Cannot size system descriptor");
        var value = new byte[required];
        if (!GetKernelObjectSecurity(handle, OwnerGroupDacl, value, required, out required))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot read system descriptor");
        return value;
    }
    private static byte[] ReadDescriptor(string path)
    {
        IntPtr handle = OpenTarget(path, false);
        try { return ReadDescriptor(handle); } finally { CloseHandle(handle); }
    }
    private static bool MetadataEqual(RawSecurityDescriptor a, RawSecurityDescriptor b)
    {
        // SACL was neither requested nor changed. Preserve owner/group and DACL controls.
        const ControlFlags mask = ControlFlags.DiscretionaryAclPresent | ControlFlags.DiscretionaryAclProtected
            | ControlFlags.DiscretionaryAclAutoInherited | ControlFlags.DiscretionaryAclAutoInheritRequired;
        return Object.Equals(a.Owner, b.Owner) && Object.Equals(a.Group, b.Group)
            && (a.ControlFlags & mask) == (b.ControlFlags & mask);
    }
    private static bool Same(byte[] a, byte[] b)
    {
        return DaclHash(a) == DaclHash(b)
            && MetadataEqual(new RawSecurityDescriptor(a, 0), new RawSecurityDescriptor(b, 0));
    }
    private static bool IsPackageAce(GenericAce ace, string sid, int mask)
    {
        var entry = ace as CommonAce;
        return entry != null && !entry.IsCallback && entry.AceFlags == AceFlags.None
            && entry.AceQualifier == AceQualifier.AccessAllowed
            && entry.SecurityIdentifier.Value == sid && entry.AccessMask == mask;
    }
    private static byte[] AddDelta(byte[] original, int mask)
    {
        var sd = new RawSecurityDescriptor(original, 0);
        var old = sd.DiscretionaryAcl;
        if (old == null) throw new InvalidDataException("Refusing a NULL DACL");
        int insert = old.Count;
        for (int i = 0; i < old.Count; i++)
        {
            var known = old[i] as KnownAce;
            if (known != null && Array.IndexOf(PackageSids, known.SecurityIdentifier.Value) >= 0)
                throw new InvalidDataException("Package SID already has an ACE; refuse merging or changing it");
            if (insert == old.Count && (old[i].AceFlags & AceFlags.Inherited) != 0) insert = i;
        }
        var next = new RawAcl(old.Revision, old.Count + PackageSids.Length);
        for (int i = 0; i < old.Count; i++) next.InsertAce(i, old[i]);
        for (int i = 0; i < PackageSids.Length; i++)
            next.InsertAce(insert + i, new CommonAce(AceFlags.None, AceQualifier.AccessAllowed,
                mask, new SecurityIdentifier(PackageSids[i]), false, null));
        sd.DiscretionaryAcl = next;
        return Bytes(sd);
    }
    private static bool IsDelta(byte[] original, byte[] current, int mask)
    {
        var before = new RawSecurityDescriptor(original, 0);
        var after = new RawSecurityDescriptor(current, 0);
        if (!MetadataEqual(before, after) || before.DiscretionaryAcl == null || after.DiscretionaryAcl == null
            || after.DiscretionaryAcl.Count != before.DiscretionaryAcl.Count + PackageSids.Length) return false;
        var found = new HashSet<string>();
        int index = 0;
        foreach (GenericAce ace in after.DiscretionaryAcl)
        {
            string added = null;
            foreach (string sid in PackageSids) if (IsPackageAce(ace, sid, mask)) added = sid;
            if (added != null) { if (!found.Add(added)) return false; continue; }
            if (index >= before.DiscretionaryAcl.Count
                || Hash(Bytes(ace)) != Hash(Bytes(before.DiscretionaryAcl[index++]))) return false;
        }
        return found.Count == PackageSids.Length && index == before.DiscretionaryAcl.Count;
    }
    private static void SetExpected(string target, byte[] expected, byte[] replacement)
    {
        // Preserve NTFS inheritance control without the recursive Win32 file
        // ACL provider. The restoration path is independently fixture-tested.
        IntPtr handle = OpenTarget(target, true);
        try
        {
            if (!Same(ReadDescriptor(handle), expected))
                throw new InvalidOperationException("Descriptor changed independently; refusing overwrite");
            if (target != Device) SetDirectoryDacl(handle, replacement);
            else if (!SetKernelObjectSecurity(handle, DaclOnly, replacement))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot set target DACL");
        }
        finally { CloseHandle(handle); }
    }
    private static IntPtr OpenDirectoryMetadata(string target)
    {
        IntPtr handle = CreateFileW(target, ReadControl | WriteDac, 7,
            IntPtr.Zero, 3, 0x02000000u /* FILE_FLAG_BACKUP_SEMANTICS */, IntPtr.Zero);
        if (handle == InvalidHandle)
        { int error = Marshal.GetLastWin32Error(); throw new Win32Exception(error, "Cannot open metadata handle: win32=" + error); }
        return handle;
    }
    private static void SetDirectoryDacl(IntPtr handle, byte[] replacement)
    {
        var sd = new RawSecurityDescriptor(replacement, 0);
        if (sd.DiscretionaryAcl == null) throw new InvalidDataException("Refusing NULL directory DACL");
        if ((sd.ControlFlags & ControlFlags.DiscretionaryAclAutoInherited) != 0)
            sd.SetFlags(sd.ControlFlags | ControlFlags.DiscretionaryAclAutoInheritRequired);
        uint protection = (sd.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0 ? 0x80000000u : 0x20000000u;
        int status = NtSetSecurityObject(handle, DaclOnly | protection, Bytes(sd));
        if (status != 0) throw new InvalidOperationException("Directory restore failed: NTSTATUS=" + status.ToString("X8"));
    }
    private static bool KnownRootNormalization(HostAclBackup item, byte[] original, byte[] current)
    {
        // Recovery only: permit precisely the observed AI-bit loss, never any
        // owner/group/protection change, ACE mutation, reordering or new grant.
        if (item.label != "drive_root" || item.mask != RootMask) return false;
        var normalized = new RawSecurityDescriptor(original, 0);
        if ((normalized.ControlFlags & ControlFlags.DiscretionaryAclAutoInherited) == 0) return false;
        normalized.SetFlags(normalized.ControlFlags & ~ControlFlags.DiscretionaryAclAutoInherited);
        byte[] value = Bytes(normalized);
        return IsDelta(value, current, item.mask) || Same(value, current);
    }
    private static string Ordinary(string path, string root, bool directory)
    {
        if (String.IsNullOrEmpty(path) || !Path.IsPathRooted(path)) throw new InvalidDataException("Absolute path required");
        if (path.Length < 3 || !Char.IsLetter(path[0]) || path[1] != ':' || path[2] != '\\'
            || path.IndexOf(':', 2) >= 0 || path.IndexOf('\0') >= 0)
            throw new InvalidDataException("Only local drive paths without alternate streams are accepted");
        string full = Path.GetFullPath(path);
        if (root != null && !full.StartsWith(root.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Path escaped synthetic fixture");
        for (string part = full; part != null; part = Path.GetDirectoryName(part))
            if ((File.GetAttributes(part) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("Reparse paths are not accepted");
        if (directory ? !Directory.Exists(full) : !File.Exists(full)) throw new InvalidDataException("Wrong path kind");
        if (!directory)
        {
            IntPtr file = CreateFileW(full, 0, 7, IntPtr.Zero, 3, 0, IntPtr.Zero);
            if (file == InvalidHandle) throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                FileInformation info;
                if (!GetFileInformationByHandle(file, out info) || info.Links != 1)
                    throw new InvalidDataException("Hard-linked or uninspectable input file");
            }
            finally { CloseHandle(file); }
        }
        return full;
    }
    private static string Fixture(string path)
    {
        string root = Ordinary(path, null, true);
        if (!Path.GetFileName(root).StartsWith("mewcode-lpac-probe-", StringComparison.Ordinal)
            || Path.GetFileName(Path.GetDirectoryName(root)) != ".tmp")
            throw new InvalidDataException("Expected a .tmp synthetic fixture");
        string marker = Ordinary(Path.Combine(root, ".mewcode-synthetic-fixture"), root, false);
        if (File.ReadAllText(marker, Encoding.ASCII) != "mewcode-synthetic-only-v1\n")
            throw new InvalidDataException("Synthetic marker mismatch");
        return root;
    }
    private static void VerifyFile(string path, string root, string expected)
    {
        if (String.IsNullOrEmpty(expected) || expected.Length != 64
            || Hash(File.ReadAllBytes(Ordinary(path, root, false))) != expected)
            throw new InvalidDataException("Input file hash mismatch");
    }
    private static HostAclManifest LoadManifest(string path, string hash)
    {
        string root = Fixture(Path.GetDirectoryName(Path.GetFullPath(path)));
        VerifyFile(path, root, hash);
        var manifest = Json().Deserialize<HostAclManifest>(File.ReadAllText(path, Encoding.UTF8));
        if (manifest == null || manifest.schema != "mewcode-native-host-acl-trial-v1"
            || !String.Equals(root, manifest.fixture_root, StringComparison.OrdinalIgnoreCase)
            || !String.Equals(manifest.system_drive_root, SystemRoot(), StringComparison.OrdinalIgnoreCase)
            || manifest.cases == null || manifest.cases.Length != CaseNames.Length
            || manifest.artifacts == null || manifest.artifacts.Length == 0)
            throw new InvalidDataException("Unexpected joint trial manifest");
        var names = new HashSet<string>();
        foreach (var item in manifest.cases)
        {
            if (item == null || !names.Add(item.name)) throw new InvalidDataException("Duplicate or missing case");
            VerifyFile(item.request, root, item.request_sha256);
            var request = Json().Deserialize<Dictionary<string, object>>(File.ReadAllText(item.request, Encoding.UTF8));
            bool lpac = item.name.StartsWith("lpac_", StringComparison.Ordinal);
            if (request == null || !request.ContainsKey("lpac") || !Object.Equals(request["lpac"], lpac)
                || !request.ContainsKey("fixture_root") || !Object.Equals(request["fixture_root"], root))
                throw new InvalidDataException("Case mode or fixture identity mismatch");
            foreach (string capability in new[] { "runtime_registry_read", "runtime_instrumentation", "runtime_com" })
                if (!request.ContainsKey(capability) || !Object.Equals(request[capability], lpac))
                    throw new InvalidDataException("Case runtime capability mismatch");
        }
        if (!names.SetEquals(CaseNames)) throw new InvalidDataException("Unexpected synthetic case set");
        VerifyInputs(manifest);
        return manifest;
    }
    private static void VerifyInputs(HostAclManifest manifest)
    {
        VerifyFile(manifest.helper, manifest.fixture_root, manifest.helper_sha256);
        foreach (var item in manifest.artifacts)
        {
            if (item == null) throw new InvalidDataException("Missing artifact");
            VerifyFile(item.path, manifest.fixture_root, item.sha256);
        }
    }
    private static void WriteNew(string path, byte[] data)
    {
        using (var file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        { file.Write(data, 0, data.Length); file.Flush(true); }
    }
    private static void WriteJson(string path, object value)
    { WriteNew(path, Encoding.UTF8.GetBytes(Json().Serialize(value))); }
    private static string Quote(string value)
    {
        var result = new StringBuilder("\""); int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            result.Append('\\', ch == '"' ? slashes * 2 + 1 : slashes);
            result.Append(ch); slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }
    private static T Locked<T>(string name, Func<T> action)
    {
        using (var mutex = new Mutex(false, name))
        {
            bool acquired = false;
            try
            {
                try { acquired = mutex.WaitOne(10000); } catch (AbandonedMutexException) { acquired = true; }
                if (!acquired) throw new TimeoutException("Recovery synchronization unavailable");
                return action();
            }
            finally { if (acquired) mutex.ReleaseMutex(); }
        }
    }
    private static HostAclRecovery LoadRecovery(string path, string expected)
    {
        string root = Fixture(Path.GetDirectoryName(Path.GetFullPath(path)));
        VerifyFile(path, root, expected);
        var plan = Json().Deserialize<HostAclRecovery>(File.ReadAllText(path, Encoding.UTF8));
        Guid id;
        if (plan == null || plan.schema != "mewcode-host-acl-recovery-v1" || plan.fixture_root != root
            || plan.backups == null || plan.backups.Length != 2 || String.IsNullOrEmpty(plan.mutex_name)
            || !plan.mutex_name.StartsWith("Local\\MewCodeHostAclTrial-", StringComparison.Ordinal)
            || !Guid.TryParseExact(plan.mutex_name.Substring("Local\\MewCodeHostAclTrial-".Length), "N", out id))
            throw new InvalidDataException("Invalid recovery plan");
        var labels = new HashSet<string>();
        foreach (var backup in plan.backups)
        {
            if (backup == null || !labels.Add(backup.label)
                || (backup.label == "nul" ? backup.target != Device || backup.mask != NullMask
                    : backup.label != "drive_root" || backup.mask != RootMask
                    || !String.Equals(backup.target, SystemRoot(), StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("Unexpected recovery target");
            VerifyFile(backup.path, root, backup.sha256);
        }
        return plan;
    }
    private static Dictionary<string, object> RestoreAll(HostAclRecovery plan)
    {
        return Locked(plan.mutex_name, () => {
            var result = new Dictionary<string, object>();
            // Try every target even if another target has a conflict or a read failure.
            for (int i = plan.backups.Length - 1; i >= 0; i--)
            {
                var item = plan.backups[i];
                try
                {
                    VerifyFile(item.path, plan.fixture_root, item.sha256);
                    byte[] original = File.ReadAllBytes(item.path), current = ReadDescriptor(item.target);
                    string outcome = "already_original";
                    if (!Same(original, current))
                    {
                        if (!IsDelta(original, current, item.mask) && !KnownRootNormalization(item, original, current))
                            throw new InvalidOperationException("Independent descriptor change; no overwrite");
                        SetExpected(item.target, current, original);
                        outcome = "restored";
                    }
                    bool verified = Same(original, ReadDescriptor(item.target));
                    result[item.label] = new { outcome = outcome, verified = verified,
                        restored_dacl_sha256 = DaclHash(ReadDescriptor(item.target)) };
                    if (!verified) result[item.label + "_error"] = "restoration_verification_failed";
                }
                catch (Exception ex) { result[item.label + "_error"] = ex.GetType().Name + ": " + ex.Message; }
            }
            return result;
        });
    }
    private static bool Restored(HostAclRecovery plan)
    {
        foreach (var item in plan.backups)
            if (!Same(File.ReadAllBytes(item.path), ReadDescriptor(item.target))) return false;
        return true;
    }
    private static int RestoreEntries(string path, string hash)
    {
        RequireAdmin(); var plan = LoadRecovery(path, hash);
        var result = Locked(plan.mutex_name, () => {
            var entries = new Dictionary<string, object>();
            foreach (var item in plan.backups)
            {
                byte[] original = File.ReadAllBytes(item.path), current = ReadDescriptor(item.target);
                if (!Same(original, current))
                {
                    if (!IsDelta(original, current, item.mask) && !KnownRootNormalization(item, original, current))
                        throw new InvalidOperationException("Entry rollback refused independent change");
                    IntPtr handle = OpenTarget(item.target, true);
                    try
                    {
                        if (!Same(ReadDescriptor(handle), current)) throw new InvalidOperationException("Concurrent entry change");
                        if (!SetKernelObjectSecurity(handle, DaclOnly, original)) throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    finally { CloseHandle(handle); }
                }
                byte[] after = ReadDescriptor(item.target);
                entries[item.label] = new { dacl_restored = DaclHash(original) == DaclHash(after),
                    fully_restored = Same(original, after), dacl_sha256 = DaclHash(after),
                    controls = (int)new RawSecurityDescriptor(after, 0).ControlFlags };
                if (DaclHash(original) != DaclHash(after)) throw new InvalidOperationException("Entry rollback verification failed");
            }
            return entries;
        });
        result["restore_verified"] = Restored(plan);
        WritePrecreated(Path.Combine(plan.fixture_root, "host-acl-entries-restoration.json"), plan.fixture_root, result);
        return 0;
    }
    private static int Watchdog(string path, string hash, int pid, long parentStart)
    {
        RequireAdmin(); var plan = LoadRecovery(path, hash);
        foreach (var item in plan.backups) using (var probe = new HandleGuard(OpenTarget(item.target, true))) { }
        string root = plan.fixture_root;
        WriteNew(Path.Combine(root, "host-acl-watchdog.ready"), Encoding.ASCII.GetBytes("ready\n"));
        var watch = Stopwatch.StartNew();
        while (watch.Elapsed.TotalSeconds < DeadlineSeconds)
        {
            if (File.Exists(Path.Combine(root, "host-acl-parent-restored.flag"))) break;
            try
            {
                using (var parent = Process.GetProcessById(pid))
                    if (parent.HasExited || parent.StartTime.ToUniversalTime().Ticks != parentStart) break;
            }
            // Failure to inspect the parent is a reason to restore, never to abandon recovery.
            catch (Exception) { break; }
            Thread.Sleep(100);
        }
        var result = RestoreAll(plan);
        WriteJson(Path.Combine(root, "host-acl-watchdog-result.json"), result);
        return Restored(plan) ? 0 : 1;
    }
    private sealed class HandleGuard : IDisposable
    {
        private IntPtr handle;
        public HandleGuard(IntPtr value) { handle = value; }
        public void Dispose() { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
    }
    private static Process StartWatchdog(string path, string hash)
    {
        using (var parent = Process.GetCurrentProcess())
        {
            var info = new ProcessStartInfo(typeof(MewCodeNativeHostAclTrial).Assembly.Location,
                "watchdog " + Quote(path) + " " + hash + " " + parent.Id + " " + parent.StartTime.ToUniversalTime().Ticks);
            info.UseShellExecute = false; info.CreateNoWindow = true; info.WindowStyle = ProcessWindowStyle.Hidden;
            return Process.Start(info);
        }
    }
    private static object RunCase(HostAclManifest manifest, HostAclCase item, string prefix)
    {
        VerifyInputs(manifest); VerifyFile(item.request, manifest.fixture_root, item.request_sha256);
        var info = new ProcessStartInfo(manifest.helper, Quote(item.request));
        info.UseShellExecute = false; info.CreateNoWindow = true; info.WindowStyle = ProcessWindowStyle.Hidden;
        info.WorkingDirectory = manifest.fixture_root;
        info.RedirectStandardOutput = true; info.RedirectStandardError = true;
        using (var child = Process.Start(info))
        {
            var stdout = Task.Factory.StartNew(() => child.StandardOutput.ReadToEnd());
            var stderr = Task.Factory.StartNew(() => child.StandardError.ReadToEnd());
            if (!child.WaitForExit(25000))
            {
                child.Kill(); child.WaitForExit(5000);
                throw new TimeoutException("Synthetic helper timed out; cleanup unknown");
            }
            if (!Task.WaitAll(new Task[] { stdout, stderr }, 5000)) throw new TimeoutException("Helper output did not close");
            string receiptPath = Path.Combine(manifest.fixture_root, prefix + item.name + ".receipt.json");
            WriteNew(receiptPath, Encoding.UTF8.GetBytes(stdout.Result));
            if (stderr.Result.Length > 0) WriteNew(receiptPath + ".stderr.txt", Encoding.UTF8.GetBytes(stderr.Result));
            var receipt = Json().Deserialize<Dictionary<string, object>>(stdout.Result);
            if (receipt == null || !receipt.ContainsKey("appcontainer_verified") || !Object.Equals(receipt["appcontainer_verified"], true)
                || !receipt.ContainsKey("lpac_verified") || !Object.Equals(receipt["lpac_verified"], true))
                throw new InvalidDataException("Missing verified AppContainer identity");
            foreach (string key in new[] { "job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok" })
                if (!receipt.ContainsKey(key) || !Object.Equals(receipt[key], true))
                    throw new InvalidDataException("Synthetic process cleanup was not confirmed");
            return new { name = item.name, helper_exit_code = child.ExitCode, receipt = receiptPath };
        }
    }
    private static int Baseline(string path, string hash)
    {
        var manifest = LoadManifest(path, hash);
        var observations = new List<object>();
        var log = new Dictionary<string, object> { { "schema", "mewcode-native-host-acl-baseline-v1" },
            { "system_acl_writes", false }, { "manifest_sha256", hash }, { "cases", observations } };
        try
        {
            foreach (string name in CaseNames)
                observations.Add(RunCase(manifest, Array.Find(manifest.cases, item => item.name == name), "host-acl-baseline-"));
        }
        catch (Exception ex) { log["error"] = ex.GetType().Name + ": " + ex.Message; }
        WriteJson(Path.Combine(manifest.fixture_root, "host-acl-baseline-result.json"), log);
        return log.ContainsKey("error") ? 1 : 0;
    }
    private static int Trial(string path, string hash)
    {
        RequireAdmin(); var manifest = LoadManifest(path, hash); string root = manifest.fixture_root;
        string publicReceipt = Ordinary(Path.Combine(root, "host-acl-recovery-inspection.json"), root, false);
        if (new FileInfo(publicReceipt).Length != 0) throw new InvalidDataException("Expected a new empty host receipt");
        // Rehearse the directory writer and full restoration before touching
        // either system object; a known-bad setter must not reach the trial.
        DirectorySelfTest(root);
        var backups = new List<HostAclBackup>();
        foreach (string label in new[] { "nul", "drive_root" })
        {
            string target = label == "nul" ? Device : SystemRoot();
            int mask = label == "nul" ? NullMask : RootMask;
            byte[] original;
            using (var probe = new HandleGuard(OpenTarget(target, true))) original = ReadDescriptor(target);
            AddDelta(original, mask); // Validate before either system object is changed.
            string backup = Path.Combine(root, "host-acl-" + label + "-original.bin");
            WriteNew(backup, original);
            backups.Add(new HostAclBackup { label = label, target = target, mask = mask,
                path = backup, sha256 = Hash(original) });
        }
        var plan = new HostAclRecovery { schema = "mewcode-host-acl-recovery-v1", fixture_root = root,
            mutex_name = "Local\\MewCodeHostAclTrial-" + Guid.NewGuid().ToString("N"), backups = backups.ToArray() };
        string recovery = Path.Combine(root, "host-acl-recovery.json"); WriteJson(recovery, plan);
        string recoveryHash = Hash(File.ReadAllBytes(recovery));
        var log = new Dictionary<string, object> { { "schema", "mewcode-native-host-acl-trial-result-v1" },
            { "production_authorized", false }, { "manifest_sha256", hash }, { "recovery_sha256", recoveryHash },
            { "nul_mask", NullMask }, { "root_mask", RootMask }, { "package_sids", PackageSids } };
        var observations = new List<object>(); log["cases"] = observations;
        var timer = Stopwatch.StartNew();
        using (var watchdog = StartWatchdog(recovery, recoveryHash))
        {
            while (!File.Exists(Path.Combine(root, "host-acl-watchdog.ready"))
                && timer.ElapsedMilliseconds < 5000 && !watchdog.HasExited) Thread.Sleep(50);
            try
            {
                if (!File.Exists(Path.Combine(root, "host-acl-watchdog.ready")) || watchdog.HasExited)
                    throw new InvalidOperationException("Independent watchdog is not ready; no ACL writes");
                log["watchdog_ready"] = true;
                Locked(plan.mutex_name, () => {
                    foreach (var item in plan.backups)
                    {
                        byte[] original = File.ReadAllBytes(item.path);
                        log[item.label + "_original_dacl_sha256"] = DaclHash(original);
                        SetExpected(item.target, original, AddDelta(original, item.mask));
                        if (!IsDelta(original, ReadDescriptor(item.target), item.mask))
                            throw new InvalidOperationException("Temporary descriptor verification failed: " + item.label);
                    }
                    return true;
                });
                foreach (string caseName in CaseNames)
                {
                    if (timer.Elapsed.TotalSeconds > DeadlineSeconds - 35) throw new TimeoutException("Trial approaching hard deadline");
                    Locked(plan.mutex_name, () => {
                        foreach (var item in plan.backups)
                            if (!IsDelta(File.ReadAllBytes(item.path), ReadDescriptor(item.target), item.mask))
                                throw new InvalidOperationException("Temporary permissions no longer match");
                        return true;
                    });
                    HostAclCase current = Array.Find(manifest.cases, item => item.name == caseName);
                    observations.Add(RunCase(manifest, current, "host-acl-"));
                }
            }
            catch (Exception ex) { log["error"] = ex.GetType().Name + ": " + ex.Message; }
            finally
            {
                try { log["restoration"] = RestoreAll(plan); }
                catch (Exception ex) { log["restoration_error"] = ex.GetType().Name + ": " + ex.Message; }
                bool restored = false;
                try { restored = Restored(plan); }
                catch (Exception ex) { log["restoration_verification_error"] = ex.GetType().Name + ": " + ex.Message; }
                log["restore_verified"] = restored;
                if (restored)
                {
                    WriteNew(Path.Combine(root, "host-acl-parent-restored.flag"), Encoding.ASCII.GetBytes("restored\n"));
                    log["watchdog_exited"] = watchdog.WaitForExit(10000);
                    if (watchdog.HasExited) log["watchdog_exit_code"] = watchdog.ExitCode;
                }
                log["duration_ms"] = timer.ElapsedMilliseconds;
                WriteJson(Path.Combine(root, "host-acl-trial-result.json"), log);
                DiagnoseRecovery(path, hash);
            }
        }
        return Restored(plan) && !log.ContainsKey("error") && !log.ContainsKey("restoration_error") ? 0 : 1;
    }
    private static int SelfTest()
    {
        foreach (int mask in new[] { NullMask, RootMask })
        {
            byte[] original = Bytes(new RawSecurityDescriptor("O:BAG:SYD:(A;;FA;;;BA)(A;OICI;0x1200a9;;;BU)"));
            byte[] trial = AddDelta(original, mask);
            if (!IsDelta(original, trial, mask) || Same(original, trial)) throw new Exception("Delta selftest failed");
            var modified = new RawSecurityDescriptor(trial, 0);
            ((CommonAce)modified.DiscretionaryAcl[0]).AccessMask ^= 1;
            if (IsDelta(original, Bytes(modified), mask)) throw new Exception("Independent mutation accepted");
            modified = new RawSecurityDescriptor(trial, 0);
            ((CommonAce)modified.DiscretionaryAcl[modified.DiscretionaryAcl.Count - 1]).AceFlags = AceFlags.ContainerInherit;
            if (IsDelta(original, Bytes(modified), mask)) throw new Exception("Inherited trial grant accepted");
            bool refused = false;
            try { AddDelta(trial, mask); } catch (InvalidDataException) { refused = true; }
            if (!refused) throw new Exception("Existing package ACE was merged");
            byte[] inherited = Bytes(new RawSecurityDescriptor("O:BAG:SYD:(A;;FA;;;BA)(A;ID;FR;;;BU)"));
            byte[] inheritedTrial = AddDelta(inherited, mask);
            if (!IsDelta(inherited, inheritedTrial, mask)) throw new Exception("Inherited original ACE order changed");
        }
        if ((RootMask & (0x1 | 0x2 | 0x4 | 0x10 | 0x100 | 0x10000 | 0x40000 | 0x80000)) != 0)
            throw new Exception("Root mask contains list/write/ownership rights");
        byte[] rootOriginal = Bytes(new RawSecurityDescriptor("O:BAG:SYD:PAI(A;;FA;;;BA)(A;;FA;;;SY)"));
        var normalized = new RawSecurityDescriptor(AddDelta(rootOriginal, RootMask), 0);
        normalized.SetFlags(normalized.ControlFlags & ~ControlFlags.DiscretionaryAclAutoInherited);
        var rootBackup = new HostAclBackup { label = "drive_root", mask = RootMask };
        if (!KnownRootNormalization(rootBackup, rootOriginal, Bytes(normalized))) throw new Exception("Known AI normalization rejected");
        if (KnownRootNormalization(new HostAclBackup { label = "nul", mask = NullMask }, rootOriginal, Bytes(normalized)))
            throw new Exception("Root normalization accepted for NUL");
        normalized.Owner = new SecurityIdentifier("S-1-5-18");
        if (KnownRootNormalization(rootBackup, rootOriginal, Bytes(normalized))) throw new Exception("Independent owner change accepted");
        normalized = new RawSecurityDescriptor(AddDelta(rootOriginal, RootMask), 0);
        normalized.SetFlags(normalized.ControlFlags & ~ControlFlags.DiscretionaryAclAutoInherited);
        ((CommonAce)normalized.DiscretionaryAcl[0]).AccessMask ^= 1;
        if (KnownRootNormalization(rootBackup, rootOriginal, Bytes(normalized))) throw new Exception("Independent root ACE change accepted");
        Console.WriteLine("selftest_ok=true; system_acl_writes=0"); return 0;
    }
    private static int DirectorySelfTest(string fixture)
    {
        string root = Fixture(fixture);
        string directory = Path.Combine(root, "directory-acl-selftest-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        IntPtr handle = OpenDirectoryMetadata(directory);
        try
        {
            var protectedSd = new RawSecurityDescriptor(ReadDescriptor(handle), 0);
            protectedSd.SetFlags(protectedSd.ControlFlags | ControlFlags.DiscretionaryAclProtected | ControlFlags.DiscretionaryAclAutoInherited);
            SetDirectoryDacl(handle, Bytes(protectedSd));
            byte[] original = ReadDescriptor(handle);
            if ((new RawSecurityDescriptor(original, 0).ControlFlags & ControlFlags.DiscretionaryAclAutoInherited) == 0)
                throw new InvalidOperationException("Directory setter did not preserve inheritance model");
            string childPath = Path.Combine(directory, "witness"); Directory.CreateDirectory(childPath);
            IntPtr child = OpenDirectoryMetadata(childPath);
            try
            {
                byte[] witness = ReadDescriptor(child);
                try
                {
                    SetDirectoryDacl(handle, AddDelta(original, RootMask));
                    if (!IsDelta(original, ReadDescriptor(handle), RootMask)) throw new InvalidOperationException("Directory delta mismatch");
                    if (!Same(witness, ReadDescriptor(child))) throw new InvalidOperationException("Unexpected child propagation");
                    // An inheritable test-only delta would alter the witness if
                    // propagation were active. Never use this delta on C:\\.
                    var inheritable = new RawSecurityDescriptor(AddDelta(original, RootMask), 0);
                    foreach (GenericAce ace in inheritable.DiscretionaryAcl)
                        foreach (string sid in PackageSids)
                            if (IsPackageAce(ace, sid, RootMask)) ace.AceFlags = AceFlags.ObjectInherit | AceFlags.ContainerInherit;
                    SetDirectoryDacl(handle, Bytes(inheritable));
                    if (!Same(witness, ReadDescriptor(child))) throw new InvalidOperationException("Inheritable test ACE propagated");
                }
                finally { SetDirectoryDacl(handle, original); }
                if (!Same(original, ReadDescriptor(handle)) || !Same(witness, ReadDescriptor(child)))
                    throw new InvalidOperationException("Directory restore or witness verification failed");
                Console.WriteLine("directory_selftest_ok=true; child_unchanged=true; restore_verified=true; system_acl_writes=0");
                return 0;
            }
            finally { CloseHandle(child); }
        }
        finally { CloseHandle(handle); }
    }
    private static void WritePrecreated(string path, string root, object value)
    {
        Ordinary(path, root, false);
        using (var file = new FileStream(path, FileMode.Open, FileAccess.Write, FileShare.None))
        {
            if (file.Length != 0) throw new InvalidDataException("Diagnostic output must be precreated and empty");
            byte[] bytes = Encoding.UTF8.GetBytes(Json().Serialize(value)); file.Write(bytes, 0, bytes.Length); file.Flush(true);
        }
    }
    private static int ControlProbe(string fixture)
    {
        string root = Fixture(fixture); var results = new List<object>();
        foreach (string mode in new[] { "kernel-protected", "kernel-auto-request", "kernel-protected-auto-request",
            "securityinfo-protection-only", "securityinfo-kernel-object", "nt-dacl", "nt-protected-auto-request" })
        {
            string directory = Path.Combine(root, "control-probe-" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(directory);
            IntPtr handle = OpenDirectoryMetadata(directory);
            try
            {
                var sd = new RawSecurityDescriptor(ReadDescriptor(handle), 0);
                sd.SetFlags(sd.ControlFlags | ControlFlags.DiscretionaryAclProtected | ControlFlags.DiscretionaryAclAutoInherited); SetDirectoryDacl(handle, Bytes(sd));
                byte[] original = ReadDescriptor(handle);
                string witnessPath = Path.Combine(directory, "witness"); Directory.CreateDirectory(witnessPath);
                IntPtr witness = OpenDirectoryMetadata(witnessPath);
                try
                {
                    byte[] childOriginal = ReadDescriptor(witness);
                    var wanted = new RawSecurityDescriptor(AddDelta(original, RootMask), 0);
                    foreach (GenericAce ace in wanted.DiscretionaryAcl)
                        foreach (string sid in PackageSids)
                            if (IsPackageAce(ace, sid, RootMask)) ace.AceFlags = AceFlags.ObjectInherit | AceFlags.ContainerInherit;
                    byte[] desired = Bytes(wanted);
                    if (!SetKernelObjectSecurity(handle, DaclOnly, desired)) throw new Win32Exception(Marshal.GetLastWin32Error());
                    if (!Same(childOriginal, ReadDescriptor(witness))) throw new InvalidOperationException("Probe setup propagated");
                    int status = 0;
                    try
                    {
                        if (mode.Contains("auto-request")) wanted.SetFlags(wanted.ControlFlags | ControlFlags.DiscretionaryAclAutoInheritRequired);
                        if (mode.StartsWith("kernel-"))
                        {
                            if (!SetKernelObjectSecurity(handle, DaclOnly | (mode.Contains("protected") ? 0x80000000u : 0), Bytes(wanted)))
                                status = Marshal.GetLastWin32Error();
                        }
                        else if (mode == "securityinfo-protection-only")
                            status = (int)SetSecurityInfo(handle, 1, 0x80000000u, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
                        else if (mode == "securityinfo-kernel-object")
                        {
                            byte[] acl = new byte[wanted.DiscretionaryAcl.BinaryLength]; wanted.DiscretionaryAcl.GetBinaryForm(acl, 0);
                            var pinned = GCHandle.Alloc(acl, GCHandleType.Pinned);
                            try { status = (int)SetSecurityInfo(handle, 6, 0x80000004u, IntPtr.Zero, IntPtr.Zero, pinned.AddrOfPinnedObject(), IntPtr.Zero); }
                            finally { pinned.Free(); }
                        }
                        else status = NtSetSecurityObject(handle, DaclOnly | (mode.Contains("protected") ? 0x80000000u : 0), Bytes(wanted));
                        byte[] after = ReadDescriptor(handle);
                        results.Add(new { mode = mode, status = status, desired_controls = (int)new RawSecurityDescriptor(desired, 0).ControlFlags,
                            controls = (int)new RawSecurityDescriptor(after, 0).ControlFlags, parent_exact = Same(desired, after),
                            child_unchanged = Same(childOriginal, ReadDescriptor(witness)) });
                    }
                    finally { SetDirectoryDacl(handle, original); }
                }
                finally { CloseHandle(witness); }
            }
            finally { CloseHandle(handle); }
        }
        Console.WriteLine(Json().Serialize(results)); return 0;
    }
    private static int DiagnoseRecovery(string path, string hash)
    {
        RequireAdmin(); var manifest = LoadManifest(path, hash);
        string recovery = Path.Combine(manifest.fixture_root, "host-acl-recovery.json");
        string recoveryHash = Hash(File.ReadAllBytes(Ordinary(recovery, manifest.fixture_root, false)));
        var plan = LoadRecovery(recovery, recoveryHash);
        var observations = new List<object>();
        foreach (var item in plan.backups)
        {
            byte[] original = File.ReadAllBytes(item.path), current = ReadDescriptor(item.target);
            var old = new RawSecurityDescriptor(original, 0); var now = new RawSecurityDescriptor(current, 0);
            observations.Add(new { label = item.label, original_sha256 = Hash(original),
                original_dacl_sha256 = DaclHash(original), current_dacl_sha256 = DaclHash(current),
                original_sddl = old.GetSddlForm(AccessControlSections.Owner | AccessControlSections.Group | AccessControlSections.Access),
                current_sddl = now.GetSddlForm(AccessControlSections.Owner | AccessControlSections.Group | AccessControlSections.Access),
                original_controls = (int)old.ControlFlags, current_controls = (int)now.ControlFlags });
        }
        var result = new Dictionary<string, object> { { "recovery_sha256", recoveryHash }, { "objects", observations } };
        foreach (string name in new[] { "host-acl-trial-result.json", "host-acl-watchdog-result.json" })
            if (File.Exists(Path.Combine(manifest.fixture_root, name)))
                result[name] = Json().DeserializeObject(File.ReadAllText(Ordinary(Path.Combine(manifest.fixture_root, name), manifest.fixture_root, false)));
        var cases = new Dictionary<string, object>();
        foreach (string name in CaseNames)
        {
            string receipt = Path.Combine(manifest.fixture_root, "host-acl-" + name + ".receipt.json");
            if (File.Exists(receipt)) cases[name] = Json().DeserializeObject(File.ReadAllText(Ordinary(receipt, manifest.fixture_root, false)));
        }
        result["trial_case_receipts"] = cases;
        // The ordinary host precreates an empty output so it can read admin diagnostics.
        string output = Ordinary(Path.Combine(manifest.fixture_root, "host-acl-recovery-inspection.json"), manifest.fixture_root, false);
        WritePrecreated(output, manifest.fixture_root, result);
        return 0;
    }
    public static int Main(string[] args)
    {
        try
        {
            if (args.Length == 1 && args[0] == "selftest") return SelfTest();
            if (args.Length == 2 && args[0] == "directory-selftest") return DirectorySelfTest(args[1]);
            if (args.Length == 2 && args[0] == "control-probe") return ControlProbe(args[1]);
            if (args.Length == 1 && args[0] == "inspect")
            {
                Console.WriteLine("nul_dacl_sha256=" + DaclHash(ReadDescriptor(Device)));
                Console.WriteLine("drive_root_dacl_sha256=" + DaclHash(ReadDescriptor(SystemRoot())));
                return 0;
            }
            if (args.Length == 3 && args[0] == "validate")
            { LoadManifest(args[1], args[2]); Console.WriteLine("manifest_valid=true; system_acl_writes=0"); return 0; }
            if (args.Length == 3 && args[0] == "trial") return Trial(args[1], args[2]);
            if (args.Length == 3 && args[0] == "baseline") return Baseline(args[1], args[2]);
            if (args.Length == 3 && args[0] == "diagnose-recovery") return DiagnoseRecovery(args[1], args[2]);
            if (args.Length == 3 && args[0] == "restore-entries") return RestoreEntries(args[1], args[2]);
            if (args.Length == 5 && args[0] == "watchdog") return Watchdog(args[1], args[2], Int32.Parse(args[3]), Int64.Parse(args[4]));
            if (args.Length == 3 && args[0] == "restore")
            {
                RequireAdmin(); var plan = LoadRecovery(args[1], args[2]);
                Console.WriteLine(Json().Serialize(RestoreAll(plan))); return Restored(plan) ? 0 : 1;
            }
            if (args.Length == 3 && (args[0] == "restore-report" || args[0] == "restore-control-report"))
            {
                RequireAdmin(); var plan = LoadRecovery(args[1], args[2]); var result = RestoreAll(plan);
                result["restore_verified"] = Restored(plan);
                string name = args[0] == "restore-report" ? "host-acl-manual-restoration.json" : "host-acl-control-restoration.json";
                WritePrecreated(Path.Combine(plan.fixture_root, name), plan.fixture_root, result);
                return Restored(plan) ? 0 : 1;
            }
            Console.Error.WriteLine("Usage: inspect | selftest | validate/baseline/trial/restore <manifest> <sha256>"); return 2;
        }
        catch (Exception ex) { Console.Error.WriteLine(ex.GetType().Name + ": " + ex.Message); return 1; }
    }
}
