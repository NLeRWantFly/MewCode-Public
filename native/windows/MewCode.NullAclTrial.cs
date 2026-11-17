// One-shot, diagnostic-only \Device\Null DACL experiment. Never called by AUTO.
// The original owner/group/DACL are persisted before mutation; SACL is never changed.
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

public sealed class NullTrialRequest
{
    public string fixture_root { get; set; }
    public string helper { get; set; }
    public string helper_sha256 { get; set; }
    public NullTrialCase[] cases { get; set; }
}

public sealed class NullTrialCase
{
    public string name { get; set; }
    public string request { get; set; }
    public string request_sha256 { get; set; }
}

public static class MewCodeNullAclTrial
{
    private const uint ReadControl = 0x00020000;
    private const uint WriteDac = 0x00040000;
    private const uint DaclSecurityInformation = 0x00000004;
    private const uint OwnerSecurityInformation = 0x00000001;
    private const uint GroupSecurityInformation = 0x00000002;
    private const uint FileShareAll = 0x00000007;
    private const uint OpenExisting = 3;
    private const int ErrorInsufficientBuffer = 122;
    private const int TrialDeadlineSeconds = 150;
    private static readonly IntPtr InvalidHandle = new IntPtr(-1);
    private const string FixtureMarker = "mewcode-synthetic-only-v1\n";
    private const string DevicePath = "\\\\.\\NUL";

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFileW(string path, uint access, uint share, IntPtr security,
        uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetKernelObjectSecurity(IntPtr handle, uint information,
        byte[] descriptor, uint length, out uint needed);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool SetKernelObjectSecurity(IntPtr handle, uint information, byte[] descriptor);

    public static int Main(string[] args)
    {
        try
        {
            if (args.Length == 1 && args[0] == "inspect")
            {
                byte[] sd = ReadDescriptor(false);
                Console.WriteLine("nul_dacl_sha256=" + DaclHash(sd));
                Console.WriteLine("nul_owner_group_dacl_backup_bytes=" + sd.Length);
                return 0;
            }
            if (args.Length == 1 && args[0] == "selftest")
            {
                byte[] original = ReadDescriptor(false);
                byte[] trial = AddTemporaryAce(original);
                if (DaclHash(original) == DaclHash(trial)) throw new Exception("trial DACL did not change");
                if (DaclHash(original) != DaclHash(new RawSecurityDescriptor(original, 0)))
                    throw new Exception("descriptor round-trip mismatch");
                if (!IsOnlyAppContainerAceAdded(new RawSecurityDescriptor(original, 0),
                    new RawSecurityDescriptor(trial, 0)))
                    throw new Exception("unmapped trial ACE was not recognized");
                var mapped = new RawSecurityDescriptor(trial, 0);
                ((CommonAce)mapped.DiscretionaryAcl[mapped.DiscretionaryAcl.Count - 1]).AccessMask = 0x001201bf;
                if (!IsOnlyAppContainerAceAdded(new RawSecurityDescriptor(original, 0), mapped))
                    throw new Exception("Windows-mapped trial ACE was not recognized");
                var unrelatedChange = new RawSecurityDescriptor(Serialize(mapped), 0);
                ((CommonAce)unrelatedChange.DiscretionaryAcl[0]).AccessMask ^= 1;
                if (IsOnlyAppContainerAceAdded(new RawSecurityDescriptor(original, 0), unrelatedChange))
                    throw new Exception("Unrelated DACL change was accepted");
                Console.WriteLine("selftest_ok=true; no ACL write performed");
                return 0;
            }
            if (args.Length == 3 && args[0] == "trial")
                return Trial(args[1], args[2]);
            if (args.Length == 2 && args[0] == "diagnose")
            {
                var original = new RawSecurityDescriptor(File.ReadAllBytes(args[1]), 0);
                var current = new RawSecurityDescriptor(ReadDescriptor(false), 0);
                Console.WriteLine("original_dacl=" + original.GetSddlForm(AccessControlSections.Access));
                Console.WriteLine("current_dacl=" + current.GetSddlForm(AccessControlSections.Access));
                Console.WriteLine("only_one_added_appcontainer_ace=" + IsOnlyAppContainerAceAdded(original, current));
                Console.WriteLine("current_dacl_sha256=" + DaclHash(current));
                return 0;
            }
            if (args.Length == 4 && args[0] == "restore")
                return Restore(args[1], args[2], args[3]);
            if (args.Length == 6 && args[0] == "watchdog")
                return Watchdog(args[1], args[2], Int32.Parse(args[3]),
                    args[4], args[5]);
            Console.Error.WriteLine("Usage: inspect | selftest | trial <manifest> <sha256>");
            return 2;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.GetType().Name + ": " + ex.Message);
            return 1;
        }
    }

    private static bool Elevated()
    {
        return new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
    }

    private static byte[] ReadDescriptor(bool writable)
    {
        IntPtr handle = CreateFileW(DevicePath, ReadControl | (writable ? WriteDac : 0),
            FileShareAll, IntPtr.Zero, OpenExisting, 0, IntPtr.Zero);
        if (handle == InvalidHandle || handle == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot open \\.\\NUL security descriptor");
        try { return ReadDescriptor(handle); }
        finally { CloseHandle(handle); }
    }

    private static byte[] ReadDescriptor(IntPtr handle)
    {
        uint info = OwnerSecurityInformation | GroupSecurityInformation | DaclSecurityInformation;
        uint needed;
        GetKernelObjectSecurity(handle, info, null, 0, out needed);
        if (Marshal.GetLastWin32Error() != ErrorInsufficientBuffer || needed == 0 || needed > 65536)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot size NUL descriptor");
        byte[] bytes = new byte[needed];
        if (!GetKernelObjectSecurity(handle, info, bytes, (uint)bytes.Length, out needed))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot read NUL descriptor");
        return bytes;
    }

    private static byte[] Serialize(RawSecurityDescriptor descriptor)
    {
        byte[] bytes = new byte[descriptor.BinaryLength];
        descriptor.GetBinaryForm(bytes, 0);
        return bytes;
    }

    private static string Sha(byte[] bytes)
    {
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
    }

    private static string DaclHash(byte[] descriptor)
    {
        return DaclHash(new RawSecurityDescriptor(descriptor, 0));
    }

    private static string DaclHash(RawSecurityDescriptor descriptor)
    {
        RawAcl acl = descriptor.DiscretionaryAcl;
        if (acl == null) throw new InvalidDataException("NUL has no DACL; refusing trial");
        byte[] bytes = new byte[acl.BinaryLength];
        acl.GetBinaryForm(bytes, 0);
        return Sha(bytes);
    }

    private static byte[] AddTemporaryAce(byte[] original)
    {
        var sd = new RawSecurityDescriptor(original, 0);
        RawAcl oldAcl = sd.DiscretionaryAcl;
        if (oldAcl == null) throw new InvalidDataException("NUL has no DACL; refusing trial");
        var newAcl = new RawAcl(oldAcl.Revision, oldAcl.Count + 1);
        for (int i = 0; i < oldAcl.Count; i++) newAcl.InsertAce(i, oldAcl[i]);
        // Ordinary AppContainer only. No world/administrator or LPAC permission is added.
        var appPackages = new SecurityIdentifier("S-1-15-2-1");
        const int genericReadWriteExecute = unchecked((int)0xE0000000);
        newAcl.InsertAce(newAcl.Count, new CommonAce(AceFlags.None,
            AceQualifier.AccessAllowed, genericReadWriteExecute, appPackages, false, null));
        sd.DiscretionaryAcl = newAcl;
        return Serialize(sd);
    }

    private static byte[] AceBytes(GenericAce ace)
    {
        byte[] bytes = new byte[ace.BinaryLength];
        ace.GetBinaryForm(bytes, 0);
        return bytes;
    }

    private static bool IsOnlyAppContainerAceAdded(RawSecurityDescriptor original,
        RawSecurityDescriptor current)
    {
        RawAcl before = original.DiscretionaryAcl;
        RawAcl after = current.DiscretionaryAcl;
        if (before == null || after == null || after.Count != before.Count + 1) return false;
        for (int i = 0; i < before.Count; i++)
            if (Sha(AceBytes(before[i])) != Sha(AceBytes(after[i]))) return false;
        var added = after[after.Count - 1] as CommonAce;
        return added != null && added.AceQualifier == AceQualifier.AccessAllowed
            && added.AceFlags == AceFlags.None && !added.IsCallback
            && added.SecurityIdentifier.Value == "S-1-15-2-1"
            && (added.AccessMask == unchecked((int)0xE0000000) || added.AccessMask == 0x001201bf);
    }

    private static int Restore(string backupPath, string backupHash, string observedCurrentDaclHash)
    {
        if (!Elevated()) throw new UnauthorizedAccessException("Administrator token required for recovery");
        byte[] original = File.ReadAllBytes(backupPath);
        if (Sha(original) != backupHash) throw new InvalidDataException("Backup hash mismatch");
        byte[] current = ReadDescriptor(false);
        if (DaclHash(current) != observedCurrentDaclHash
            || !IsOnlyAppContainerAceAdded(new RawSecurityDescriptor(original, 0),
                new RawSecurityDescriptor(current, 0)))
            throw new InvalidOperationException("Current NUL DACL is not precisely the trial delta");
        SetDaclIfExpected(observedCurrentDaclHash, original);
        bool verified = DaclHash(ReadDescriptor(false)) == DaclHash(original);
        Console.WriteLine("original_dacl_restored=" + verified);
        if (!verified) throw new InvalidOperationException("Restored DACL verification failed");
        return 0;
    }

    private static void SetDaclIfExpected(string expected, byte[] replacement)
    {
        IntPtr handle = CreateFileW(DevicePath, ReadControl | WriteDac, FileShareAll,
            IntPtr.Zero, OpenExisting, 0, IntPtr.Zero);
        if (handle == InvalidHandle || handle == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot open NUL for DACL change");
        try
        {
            string actual = DaclHash(ReadDescriptor(handle));
            if (actual != expected)
                throw new InvalidOperationException("NUL DACL changed independently; refusing to overwrite");
            if (!SetKernelObjectSecurity(handle, DaclSecurityInformation, replacement))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot set NUL DACL");
        }
        finally { CloseHandle(handle); }
    }

    private static string FullPathWithin(string path, string root)
    {
        if (String.IsNullOrWhiteSpace(path) || !Path.IsPathRooted(path))
            throw new InvalidDataException("Trial path must be absolute");
        string full = Path.GetFullPath(path);
        if (!full.StartsWith(root.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Trial path escapes the synthetic fixture");
        for (string part = full; !String.Equals(part, root, StringComparison.OrdinalIgnoreCase);
            part = Path.GetDirectoryName(part))
        {
            if (part == null) throw new InvalidDataException("Invalid fixture path");
            if ((File.GetAttributes(part) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("Reparse point in trial path");
        }
        return full;
    }

    private static NullTrialRequest LoadManifest(string path, string expectedHash)
    {
        string full = Path.GetFullPath(path);
        string root = Path.GetDirectoryName(full);
        if (root == null || !Path.GetFileName(root).StartsWith("mewcode-lpac-probe-", StringComparison.OrdinalIgnoreCase)
            || !String.Equals(Path.GetFileName(Path.GetDirectoryName(root)), ".tmp", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Manifest must be inside a direct .tmp synthetic fixture");
        FullPathWithin(full, root);
        if (File.ReadAllText(Path.Combine(root, ".mewcode-synthetic-fixture"), Encoding.ASCII) != FixtureMarker)
            throw new InvalidDataException("Synthetic fixture marker mismatch");
        byte[] bytes = File.ReadAllBytes(full);
        if (Sha(bytes) != expectedHash) throw new InvalidDataException("Manifest hash mismatch");
        var manifest = new JavaScriptSerializer().Deserialize<NullTrialRequest>(Encoding.UTF8.GetString(bytes));
        if (manifest == null || !String.Equals(Path.GetFullPath(manifest.fixture_root), root, StringComparison.OrdinalIgnoreCase)
            || manifest.cases == null || manifest.cases.Length != 3)
            throw new InvalidDataException("Expected exactly three synthetic requests");
        string helper = FullPathWithin(manifest.helper, root);
        if (Sha(File.ReadAllBytes(helper)) != manifest.helper_sha256)
            throw new InvalidDataException("Helper hash mismatch");
        var names = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in manifest.cases)
        {
            if (item == null || !names.Add(item.name) || Sha(File.ReadAllBytes(
                FullPathWithin(item.request, root))) != item.request_sha256)
                throw new InvalidDataException("Synthetic request hash mismatch");
        }
        if (!names.SetEquals(new[] { "nul", "git_version", "git_status" }))
            throw new InvalidDataException("Unexpected synthetic request names");
        return manifest;
    }

    private static void WriteNew(string path, byte[] data)
    {
        using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            stream.Write(data, 0, data.Length);
            stream.Flush(true);
        }
    }

    private static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }

    private static Process StartWatchdog(string originalPath, string originalHash,
        string readyPath, string resultPath)
    {
        var info = new ProcessStartInfo(typeof(MewCodeNullAclTrial).Assembly.Location,
            "watchdog " + Quote(originalPath) + " " + originalHash + " " +
            Process.GetCurrentProcess().Id + " " + Quote(readyPath) + " " + Quote(resultPath));
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.WindowStyle = ProcessWindowStyle.Hidden;
        return Process.Start(info);
    }

    private static int RunHelper(string helper, string request, string resultPath)
    {
        var info = new ProcessStartInfo(helper, Quote(request));
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.WindowStyle = ProcessWindowStyle.Hidden;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        using (var child = Process.Start(info))
        {
            Task<string> output = Task.Factory.StartNew(() => child.StandardOutput.ReadToEnd());
            Task<string> error = Task.Factory.StartNew(() => child.StandardError.ReadToEnd());
            if (!child.WaitForExit(35000))
            {
                child.Kill();
                child.WaitForExit(5000);
                throw new TimeoutException("Synthetic helper exceeded 35 seconds");
            }
            string stdout = output.Result;
            string stderr = error.Result;
            WriteNew(resultPath, Encoding.UTF8.GetBytes(stdout));
            if (stderr.Length > 0) WriteNew(resultPath + ".stderr.txt", Encoding.UTF8.GetBytes(stderr));
            return child.ExitCode;
        }
    }

    private static int Trial(string manifestPath, string manifestHash)
    {
        if (!Elevated()) throw new UnauthorizedAccessException("Elevated Administrator token required; no ACL changed");
        NullTrialRequest manifest = LoadManifest(manifestPath, manifestHash);
        string root = Path.GetFullPath(manifest.fixture_root);
        string backup = Path.Combine(root, "nul-original-owner-group-dacl.bin");
        string ready = Path.Combine(root, "nul-watchdog.ready");
        string watchdogResult = Path.Combine(root, "nul-watchdog-result.txt");
        string report = Path.Combine(root, "nul-acl-trial-result.txt");
        byte[] original = ReadDescriptor(true);
        byte[] trial = AddTemporaryAce(original);
        string originalHash = DaclHash(original);
        string trialHash = DaclHash(trial);
        WriteNew(backup, original);
        string backupHash = Sha(original);
        Process watchdog = StartWatchdog(backup, backupHash, ready, watchdogResult);
        var timer = Stopwatch.StartNew();
        while (!File.Exists(ready) && timer.ElapsedMilliseconds < 5000 && !watchdog.HasExited)
            Thread.Sleep(50);
        if (!File.Exists(ready) || watchdog.HasExited)
            throw new InvalidOperationException("Independent restore watchdog did not become ready; no ACL changed");
        var log = new List<string> {
            "original_dacl_sha256=" + originalHash,
            "temporary_dacl_sha256=" + trialHash,
            "original_descriptor_sha256=" + backupHash,
            "watchdog_ready=true"
        };
        bool changed = false;
        bool restored = false;
        try
        {
            SetDaclIfExpected(originalHash, trial);
            changed = true;
            if (!IsOnlyAppContainerAceAdded(new RawSecurityDescriptor(original, 0),
                new RawSecurityDescriptor(ReadDescriptor(false), 0)))
                throw new InvalidOperationException("Temporary DACL verification failed");
            foreach (var item in manifest.cases)
            {
                if (!IsOnlyAppContainerAceAdded(new RawSecurityDescriptor(original, 0),
                    new RawSecurityDescriptor(ReadDescriptor(false), 0)))
                    throw new InvalidOperationException("Watchdog restored early or NUL DACL changed");
                int exit = RunHelper(manifest.helper, item.request,
                    Path.Combine(root, "nul-acl-" + item.name + ".receipt.json"));
                log.Add(item.name + "_helper_exit=" + exit);
            }
        }
        finally
        {
            if (changed)
            {
                byte[] currentDescriptor = ReadDescriptor(false);
                string current = DaclHash(currentDescriptor);
                if (IsOnlyAppContainerAceAdded(new RawSecurityDescriptor(original, 0),
                    new RawSecurityDescriptor(currentDescriptor, 0)))
                    SetDaclIfExpected(current, original);
                else if (current != originalHash)
                    log.Add("restore_conflict=true; manual inspection required");
                restored = DaclHash(ReadDescriptor(false)) == originalHash;
            }
            log.Add("restore_verified=" + restored);
            log.Add("duration_ms=" + timer.ElapsedMilliseconds);
            WriteNew(report, Encoding.UTF8.GetBytes(String.Join("\n", log) + "\n"));
        }
        if (!restored) throw new InvalidOperationException("Original NUL DACL was not verified restored");
        return 0;
    }

    private static int Watchdog(string backup, string backupHash,
        int parentPid, string readyPath, string resultPath)
    {
        if (!Elevated()) throw new UnauthorizedAccessException("Watchdog lost Administrator token");
        byte[] original = File.ReadAllBytes(backup);
        if (Sha(original) != backupHash) throw new InvalidDataException("Watchdog backup hash mismatch");
        string originalHash = DaclHash(original);
        WriteNew(readyPath, Encoding.ASCII.GetBytes("ready\n"));
        var timer = Stopwatch.StartNew();
        while (timer.Elapsed.TotalSeconds < TrialDeadlineSeconds)
        {
            bool exited;
            try { exited = Process.GetProcessById(parentPid).HasExited; }
            catch (ArgumentException) { exited = true; }
            if (exited) break;
            Thread.Sleep(250);
        }
        byte[] currentDescriptor = ReadDescriptor(false);
        string current = DaclHash(currentDescriptor);
        string outcome;
        if (IsOnlyAppContainerAceAdded(new RawSecurityDescriptor(original, 0),
            new RawSecurityDescriptor(currentDescriptor, 0)))
        {
            SetDaclIfExpected(current, original);
            outcome = DaclHash(ReadDescriptor(false)) == originalHash ? "restored" : "restore_failed";
        }
        else if (current == originalHash) outcome = "already_original";
        else outcome = "conflict_manual_inspection_required";
        WriteNew(resultPath, Encoding.ASCII.GetBytes(outcome + "\n"));
        if (outcome == "restore_failed" || outcome.StartsWith("conflict")) return 1;
        return 0;
    }
}
