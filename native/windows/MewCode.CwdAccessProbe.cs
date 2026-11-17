// Diagnostic-only, read-only cwd probe. All emitted values are status markers;
// paths returned by Windows APIs are deliberately never written to stdout.
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class MewCodeCwdAccessProbe
{
    private const uint MaxPath = 260;
    private const uint RoundtripMaxPath = 32768;
    private const uint ShareReadWriteDelete = 7;
    private const uint OpenExisting = 3;
    private const uint FileFlagBackupSemantics = 0x02000000;
    private const uint FileReadAttributes = 0x00000080;
    private const uint InvalidFileAttributes = 0xffffffff;
    private const uint VolumeNameDos = 0;
    private const uint VolumeNameGuid = 1;
    private const uint VolumeNameNt = 2;
    private const uint VolumeNameNone = 4;
    private const uint FileNameOpened = 8;
    private static readonly IntPtr InvalidHandle = new IntPtr(-1);

    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime
    {
        public uint Low;
        public uint High;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        public uint FileAttributes;
        public FileTime CreationTime;
        public FileTime LastAccessTime;
        public FileTime LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    private struct FileIdentity
    {
        public uint VolumeSerialNumber;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [DllImport("kernel32.dll", EntryPoint = "GetCurrentDirectoryW", CharSet = CharSet.Unicode,
        ExactSpelling = true, SetLastError = true)]
    private static extern uint GetCurrentDirectoryW(uint length, StringBuilder buffer);

    [DllImport("kernel32.dll", EntryPoint = "GetFileAttributesW", CharSet = CharSet.Unicode,
        ExactSpelling = true, SetLastError = true)]
    private static extern uint GetFileAttributesW(string path);

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode,
        ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr CreateFileW(string path, uint access, uint share, IntPtr security,
        uint creation, uint flags, IntPtr template);

    [DllImport("kernel32.dll", EntryPoint = "CloseHandle", ExactSpelling = true,
        SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandle", ExactSpelling = true,
        SetLastError = true)]
    private static extern bool GetFileInformationByHandle(IntPtr handle,
        out ByHandleFileInformation information);

    [DllImport("kernel32.dll", EntryPoint = "FindFirstFileW", CharSet = CharSet.Unicode,
        ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr FindFirstFileW(string pattern, IntPtr data);

    [DllImport("kernel32.dll", EntryPoint = "FindClose", ExactSpelling = true,
        SetLastError = true)]
    private static extern bool FindClose(IntPtr handle);

    [DllImport("kernel32.dll", EntryPoint = "GetFinalPathNameByHandleW", CharSet = CharSet.Unicode,
        ExactSpelling = true, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandleW(IntPtr handle, StringBuilder path,
        uint length, uint flags);

    [DllImport("kernel32.dll", EntryPoint = "GetLongPathNameW", CharSet = CharSet.Unicode,
        ExactSpelling = true, SetLastError = true)]
    private static extern uint GetLongPathNameW(string shortPath, StringBuilder longPath,
        uint length);

    [DllImport("kernel32.dll", EntryPoint = "GetVolumePathNameW", CharSet = CharSet.Unicode,
        ExactSpelling = true, SetLastError = true)]
    private static extern bool GetVolumePathNameW(string fileName, StringBuilder volumePathName,
        uint length);

    private static string Failure(int error)
    {
        return "failed:win32=" + error;
    }

    // Win32's path-returning APIs use zero for failure and a required character
    // count at or above the supplied capacity when the buffer is too small.
    private static string CharacterCountStatus(uint returned, uint capacity, int error)
    {
        if (returned == 0)
            return Failure(error);
        return returned >= capacity ? "buffer_too_small" : "ok";
    }

    private static string AttributesStatus(string path)
    {
        uint value = GetFileAttributesW(path);
        if (value == InvalidFileAttributes)
        {
            int error = Marshal.GetLastWin32Error();
            return Failure(error);
        }
        return "ok";
    }

    private static string OpenStatus(string path, uint access)
    {
        IntPtr handle = CreateFileW(path, access, ShareReadWriteDelete, IntPtr.Zero,
            OpenExisting, FileFlagBackupSemantics, IntPtr.Zero);
        if (handle == InvalidHandle)
        {
            int error = Marshal.GetLastWin32Error();
            return Failure(error);
        }
        try
        {
            return "ok";
        }
        finally
        {
            CloseHandle(handle);
        }
    }

    private static string FindStatus(string pattern)
    {
        IntPtr data = Marshal.AllocHGlobal(4096);
        try
        {
            IntPtr handle = FindFirstFileW(pattern, data);
            if (handle == InvalidHandle)
            {
                int error = Marshal.GetLastWin32Error();
                return Failure(error);
            }
            try
            {
                return "ok";
            }
            finally
            {
                FindClose(handle);
            }
        }
        finally
        {
            Marshal.FreeHGlobal(data);
        }
    }

    private sealed class GitPathResult
    {
        public string CurrentDirectory;
        public string CreateFile;
        public string FinalPath;
        public string LongPathFallback;
        public string CompatibilityAttributes;
        public string Result;

        public string Stage
        {
            get
            {
                return "current_directory=" + CurrentDirectory
                    + ";create_file=" + CreateFile
                    + ";final_path_normalized_dos=" + FinalPath
                    + ";long_path_fallback=" + LongPathFallback
                    + ";compat_attributes=" + CompatibilityAttributes
                    + ";result=" + Result
                    + ";scope=win32_path_only";
            }
        }
    }

    // Mirrors only mingw_getcwd's Win32 path-selection branches. Git's later
    // xwcstoutf conversion depends on the caller's byte buffer and is outside
    // this native API probe.
    private static string DecideGitMingwGetcwd(string currentDirectory, string createFile,
        string finalPath, string longPathFallback, string compatibilityAttributes)
    {
        if (currentDirectory != "ok")
            return currentDirectory;

        if (createFile == "ok")
        {
            if (finalPath == "ok")
                return "ok";
            if (longPathFallback == "not_executed")
                return "not_executed";
            return longPathFallback;
        }

        if (createFile.StartsWith("failed:win32=", StringComparison.Ordinal))
            return compatibilityAttributes == "ok" ? "ok" : "failed:errno=ENOENT";

        return "not_executed";
    }

    private static GitPathResult RunGitMingwGetcwdPathCalls()
    {
        GitPathResult result = new GitPathResult();
        StringBuilder cwd = new StringBuilder((int)MaxPath);
        uint returned = GetCurrentDirectoryW(MaxPath, cwd);
        int currentDirectoryError = returned == 0 ? Marshal.GetLastWin32Error() : 0;
        result.CurrentDirectory = CharacterCountStatus(returned, MaxPath, currentDirectoryError);
        result.CreateFile = "not_executed";
        result.FinalPath = "not_executed";
        result.LongPathFallback = "not_executed";
        result.CompatibilityAttributes = "not_executed";

        if (result.CurrentDirectory != "ok")
        {
            result.Result = DecideGitMingwGetcwd(result.CurrentDirectory, result.CreateFile,
                result.FinalPath, result.LongPathFallback, result.CompatibilityAttributes);
            return result;
        }

        IntPtr handle = CreateFileW(cwd.ToString(), 0, ShareReadWriteDelete, IntPtr.Zero,
            OpenExisting, FileFlagBackupSemantics, IntPtr.Zero);
        if (handle == InvalidHandle)
        {
            int createFileError = Marshal.GetLastWin32Error();
            result.CreateFile = Failure(createFileError);

            uint attributes = GetFileAttributesW(cwd.ToString());
            if (attributes == InvalidFileAttributes)
            {
                int attributesError = Marshal.GetLastWin32Error();
                result.CompatibilityAttributes = Failure(attributesError);
            }
            else
            {
                result.CompatibilityAttributes = "ok";
            }

            result.Result = DecideGitMingwGetcwd(result.CurrentDirectory, result.CreateFile,
                result.FinalPath, result.LongPathFallback, result.CompatibilityAttributes);
            return result;
        }

        StringBuilder resolved = new StringBuilder((int)MaxPath);
        uint finalReturned;
        int finalError;
        try
        {
            // This is the exact call made by Git for its primary normalized DOS path.
            finalReturned = GetFinalPathNameByHandleW(handle, resolved, MaxPath, VolumeNameDos);
            finalError = finalReturned == 0 ? Marshal.GetLastWin32Error() : 0;
        }
        finally
        {
            CloseHandle(handle);
        }
        result.CreateFile = "ok";
        result.FinalPath = CharacterCountStatus(finalReturned, MaxPath, finalError);

        if (result.FinalPath != "ok")
        {
            StringBuilder longPath = new StringBuilder((int)MaxPath);
            uint longReturned = GetLongPathNameW(cwd.ToString(), longPath, MaxPath);
            int longError = longReturned == 0 ? Marshal.GetLastWin32Error() : 0;
            result.LongPathFallback = CharacterCountStatus(longReturned, MaxPath, longError);
        }

        result.Result = DecideGitMingwGetcwd(result.CurrentDirectory, result.CreateFile,
            result.FinalPath, result.LongPathFallback, result.CompatibilityAttributes);
        return result;
    }

    private static string FinalPathStatus(IntPtr handle, uint flags)
    {
        StringBuilder path = new StringBuilder((int)MaxPath);
        uint returned = GetFinalPathNameByHandleW(handle, path, MaxPath, flags);
        int error = returned == 0 ? Marshal.GetLastWin32Error() : 0;
        return CharacterCountStatus(returned, MaxPath, error);
    }

    private sealed class FinalPathQuery
    {
        public string Status;
        public string Path;
    }

    private sealed class PathRoundtripResult
    {
        public string Kind;
        public string Form;
        public string Query;
        public string Reopen;
        public string Identity;
        public string SameFile;
        public string PairedSource;
    }

    private static FinalPathQuery QueryFinalPath(IntPtr handle, uint flags)
    {
        FinalPathQuery query = new FinalPathQuery();
        StringBuilder path = new StringBuilder((int)RoundtripMaxPath);
        uint returned = GetFinalPathNameByHandleW(handle, path, RoundtripMaxPath, flags);
        int error = returned == 0 ? Marshal.GetLastWin32Error() : 0;
        query.Status = CharacterCountStatus(returned, RoundtripMaxPath, error);
        query.Path = query.Status == "ok" ? path.ToString() : null;
        return query;
    }

    private static string ReadFileIdentity(IntPtr handle, out FileIdentity identity)
    {
        identity = new FileIdentity();
        ByHandleFileInformation information;
        if (!GetFileInformationByHandle(handle, out information))
        {
            int error = Marshal.GetLastWin32Error();
            return Failure(error);
        }

        identity.VolumeSerialNumber = information.VolumeSerialNumber;
        identity.FileIndexHigh = information.FileIndexHigh;
        identity.FileIndexLow = information.FileIndexLow;
        return "ok";
    }

    private static bool SameIdentity(FileIdentity left, FileIdentity right)
    {
        return left.VolumeSerialNumber == right.VolumeSerialNumber
            && left.FileIndexHigh == right.FileIndexHigh
            && left.FileIndexLow == right.FileIndexLow;
    }

    private static string CompareIdentityStatuses(string leftStatus, FileIdentity left,
        string rightStatus, FileIdentity right)
    {
        if (leftStatus != "ok" || rightStatus != "ok")
            return "not_executed";
        return SameIdentity(left, right) ? "yes" : "no";
    }

    // Only a literal NT device path with a single, verifiable first component
    // is eligible for GLOBALROOT translation or NONE-volume pairing.
    private static bool TryGetNtVolumePrefix(string path, out string prefix, out string suffix)
    {
        prefix = null;
        suffix = null;
        const string devicePrefix = "\\Device\\";
        if (String.IsNullOrEmpty(path) || !path.StartsWith(devicePrefix, StringComparison.Ordinal))
            return false;

        int componentStart = devicePrefix.Length;
        int separator = path.IndexOf('\\', componentStart);
        int componentEnd = separator < 0 ? path.Length : separator;
        if (componentEnd == componentStart)
            return false;

        string component = path.Substring(componentStart, componentEnd - componentStart);
        if (component == "." || component == "..")
            return false;
        for (int i = 0; i < component.Length; i++)
        {
            char value = component[i];
            bool asciiLetter = (value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z');
            bool asciiDigit = value >= '0' && value <= '9';
            if (!asciiLetter && !asciiDigit && value != '_' && value != '-' && value != '.'
                && value != '#')
                return false;
        }

        prefix = path.Substring(0, componentEnd);
        suffix = separator < 0 ? String.Empty : path.Substring(separator);
        return true;
    }

    private static bool TryBuildGlobalRootPath(string ntPath, out string globalRootPath)
    {
        string prefix;
        string suffix;
        globalRootPath = null;
        if (!TryGetNtVolumePrefix(ntPath, out prefix, out suffix))
            return false;
        globalRootPath = @"\\?\GLOBALROOT" + ntPath;
        return true;
    }

    private static bool TryPairNonePath(string ntPath, string nonePath, out string pairedPath)
    {
        string volumePrefix;
        string ntSuffix;
        pairedPath = null;
        if (!TryGetNtVolumePrefix(ntPath, out volumePrefix, out ntSuffix))
            return false;
        if (!String.Equals(ntSuffix, nonePath, StringComparison.Ordinal))
            return false;

        pairedPath = volumePrefix + nonePath;
        return String.Equals(pairedPath, ntPath, StringComparison.Ordinal);
    }

    private static PathRoundtripResult BuildPathRoundtrip(string kind, string form,
        FinalPathQuery query, FinalPathQuery pairedNtQuery, string rootIdentityStatus,
        FileIdentity rootIdentity)
    {
        PathRoundtripResult result = new PathRoundtripResult();
        result.Kind = kind;
        result.Form = form;
        result.Query = query.Status;
        result.Reopen = "not_executed";
        result.Identity = "not_executed";
        result.SameFile = "not_executed";
        result.PairedSource = "not_executed";

        if (query.Status != "ok")
            return result;

        string reopenPath = query.Path;
        if (kind == "nt")
        {
            if (!TryBuildGlobalRootPath(query.Path, out reopenPath))
                return result;
        }
        else if (kind == "none")
        {
            string pairedPath;
            if (pairedNtQuery == null || pairedNtQuery.Status != "ok"
                || !TryPairNonePath(pairedNtQuery.Path, query.Path, out pairedPath)
                || !TryBuildGlobalRootPath(pairedPath, out reopenPath))
                return result;
            result.PairedSource = "nt_" + form;
        }

        IntPtr handle = CreateFileW(reopenPath, 0, ShareReadWriteDelete, IntPtr.Zero,
            OpenExisting, FileFlagBackupSemantics, IntPtr.Zero);
        if (handle == InvalidHandle)
        {
            int error = Marshal.GetLastWin32Error();
            result.Reopen = Failure(error);
            return result;
        }

        result.Reopen = "ok";
        try
        {
            FileIdentity reopenedIdentity;
            result.Identity = ReadFileIdentity(handle, out reopenedIdentity);
            result.SameFile = CompareIdentityStatuses(rootIdentityStatus, rootIdentity,
                result.Identity, reopenedIdentity);
        }
        finally
        {
            CloseHandle(handle);
        }
        return result;
    }

    private static void WritePathRoundtrip(PathRoundtripResult result)
    {
        string prefix = "roundtrip_" + result.Kind + "_" + result.Form + "_";
        Console.WriteLine(prefix + "query=" + result.Query);
        Console.WriteLine(prefix + "reopen=" + result.Reopen);
        Console.WriteLine(prefix + "identity=" + result.Identity);
        Console.WriteLine(prefix + "same_file=" + result.SameFile);
        if (result.Kind == "none")
            Console.WriteLine(prefix + "paired_source=" + result.PairedSource);
    }

    private static void WriteUnexecutedPathRoundtrip(string kind, string form)
    {
        PathRoundtripResult result = new PathRoundtripResult();
        result.Kind = kind;
        result.Form = form;
        result.Query = "not_executed";
        result.Reopen = "not_executed";
        result.Identity = "not_executed";
        result.SameFile = "not_executed";
        result.PairedSource = "not_executed";
        WritePathRoundtrip(result);
    }

    private static string MetadataOpenStatus(string path, out IntPtr handle)
    {
        handle = CreateFileW(path, 0, ShareReadWriteDelete, IntPtr.Zero,
            OpenExisting, FileFlagBackupSemantics, IntPtr.Zero);
        if (handle == InvalidHandle)
        {
            int error = Marshal.GetLastWin32Error();
            return Failure(error);
        }
        return "ok";
    }

    private static int RunRoundtripV1(string syntheticParent)
    {
        Console.WriteLine("roundtrip_schema=mewcode-cwd-roundtrip-v1");

        StringBuilder cwd = new StringBuilder((int)RoundtripMaxPath);
        uint cwdLength = GetCurrentDirectoryW(RoundtripMaxPath, cwd);
        int cwdError = cwdLength == 0 ? Marshal.GetLastWin32Error() : 0;
        string cwdStatus = CharacterCountStatus(cwdLength, RoundtripMaxPath, cwdError);

        IntPtr rootHandle = InvalidHandle;
        string rootOpenStatus = cwdStatus;
        FileIdentity rootIdentity = new FileIdentity();
        string rootIdentityStatus = rootOpenStatus;
        try
        {
            if (cwdStatus == "ok")
                rootOpenStatus = MetadataOpenStatus(cwd.ToString(), out rootHandle);
            rootIdentityStatus = rootOpenStatus;
            if (rootOpenStatus == "ok")
                rootIdentityStatus = ReadFileIdentity(rootHandle, out rootIdentity);

            if (rootOpenStatus != "ok")
            {
                WriteUnexecutedPathRoundtrip("dos", "normalized");
                WriteUnexecutedPathRoundtrip("dos", "opened");
                WriteUnexecutedPathRoundtrip("guid", "normalized");
                WriteUnexecutedPathRoundtrip("guid", "opened");
                WriteUnexecutedPathRoundtrip("nt", "normalized");
                WriteUnexecutedPathRoundtrip("nt", "opened");
                WriteUnexecutedPathRoundtrip("none", "normalized");
                WriteUnexecutedPathRoundtrip("none", "opened");
            }
            else
            {
                FinalPathQuery dosNormalized = QueryFinalPath(rootHandle, VolumeNameDos);
                FinalPathQuery dosOpened = QueryFinalPath(rootHandle, VolumeNameDos | FileNameOpened);
                FinalPathQuery guidNormalized = QueryFinalPath(rootHandle, VolumeNameGuid);
                FinalPathQuery guidOpened = QueryFinalPath(rootHandle, VolumeNameGuid | FileNameOpened);
                FinalPathQuery ntNormalized = QueryFinalPath(rootHandle, VolumeNameNt);
                FinalPathQuery ntOpened = QueryFinalPath(rootHandle, VolumeNameNt | FileNameOpened);
                FinalPathQuery noneNormalized = QueryFinalPath(rootHandle, VolumeNameNone);
                FinalPathQuery noneOpened = QueryFinalPath(rootHandle, VolumeNameNone | FileNameOpened);

                WritePathRoundtrip(BuildPathRoundtrip("dos", "normalized", dosNormalized, null,
                    rootIdentityStatus, rootIdentity));
                WritePathRoundtrip(BuildPathRoundtrip("dos", "opened", dosOpened, null,
                    rootIdentityStatus, rootIdentity));
                WritePathRoundtrip(BuildPathRoundtrip("guid", "normalized", guidNormalized, null,
                    rootIdentityStatus, rootIdentity));
                WritePathRoundtrip(BuildPathRoundtrip("guid", "opened", guidOpened, null,
                    rootIdentityStatus, rootIdentity));
                WritePathRoundtrip(BuildPathRoundtrip("nt", "normalized", ntNormalized, null,
                    rootIdentityStatus, rootIdentity));
                WritePathRoundtrip(BuildPathRoundtrip("nt", "opened", ntOpened, null,
                    rootIdentityStatus, rootIdentity));
                WritePathRoundtrip(BuildPathRoundtrip("none", "normalized", noneNormalized,
                    ntNormalized, rootIdentityStatus, rootIdentity));
                WritePathRoundtrip(BuildPathRoundtrip("none", "opened", noneOpened,
                    ntOpened, rootIdentityStatus, rootIdentity));
            }

            Console.WriteLine("roundtrip_root_identity=" + rootIdentityStatus);

            IntPtr parentHandle;
            string parentOpenStatus = MetadataOpenStatus(syntheticParent, out parentHandle);
            FileIdentity parentIdentity = new FileIdentity();
            string parentIdentityStatus = "not_executed";
            string parentSameFile = "not_executed";
            if (parentOpenStatus == "ok")
            {
                try
                {
                    parentIdentityStatus = ReadFileIdentity(parentHandle, out parentIdentity);
                    parentSameFile = CompareIdentityStatuses(rootIdentityStatus, rootIdentity,
                        parentIdentityStatus, parentIdentity);
                }
                finally
                {
                    CloseHandle(parentHandle);
                }
            }
            Console.WriteLine("roundtrip_parent_open=" + parentOpenStatus);
            Console.WriteLine("roundtrip_parent_identity=" + parentIdentityStatus);
            Console.WriteLine("roundtrip_parent_same_file=" + parentSameFile);
        }
        finally
        {
            if (rootHandle != InvalidHandle)
                CloseHandle(rootHandle);
        }
        return 0;
    }

    private static int RunRoundtripLogicSelfTest()
    {
        bool ntShapePassed = true;
        bool nonePairingPassed = true;
        bool bufferThresholdPassed = true;
        bool identityPassed = true;
        string prefix;
        string suffix;
        string paired;
        ntShapePassed &= TryGetNtVolumePrefix(@"\Device\HarddiskVolume7\synthetic", out prefix,
            out suffix) && prefix == @"\Device\HarddiskVolume7"
            && suffix == @"\synthetic";
        ntShapePassed &= !TryGetNtVolumePrefix(@"C:\synthetic", out prefix, out suffix);
        ntShapePassed &= !TryGetNtVolumePrefix(@"\Device\\synthetic", out prefix, out suffix);
        ntShapePassed &= !TryGetNtVolumePrefix(@"\Device\..\synthetic", out prefix, out suffix);
        nonePairingPassed &= TryPairNonePath(@"\Device\HarddiskVolume7\synthetic", @"\synthetic",
            out paired) && paired == @"\Device\HarddiskVolume7\synthetic";
        nonePairingPassed &= !TryPairNonePath(@"\Device\HarddiskVolume7\synthetic", @"\different",
            out paired);
        bufferThresholdPassed &= CharacterCountStatus(9, 10, 0) == "ok";
        bufferThresholdPassed &= CharacterCountStatus(10, 10, 0) == "buffer_too_small";
        bufferThresholdPassed &= CharacterCountStatus(11, 10, 0) == "buffer_too_small";
        bufferThresholdPassed &= CharacterCountStatus(0, 10, 5) == "failed:win32=5";
        FileIdentity left = new FileIdentity();
        FileIdentity right = new FileIdentity();
        left.VolumeSerialNumber = 3;
        left.FileIndexHigh = 4;
        left.FileIndexLow = 5;
        right = left;
        identityPassed &= CompareIdentityStatuses("ok", left, "ok", right) == "yes";
        right.FileIndexLow = 6;
        identityPassed &= CompareIdentityStatuses("ok", left, "ok", right) == "no";
        identityPassed &= CompareIdentityStatuses("ok", left, "failed:win32=5", right)
            == "not_executed";
        bool passed = ntShapePassed && nonePairingPassed && bufferThresholdPassed
            && identityPassed;
        Console.WriteLine("roundtrip_logic_nt_shape=" + (ntShapePassed ? "ok" : "failed"));
        Console.WriteLine("roundtrip_logic_none_pairing=" + (nonePairingPassed ? "ok" : "failed"));
        Console.WriteLine("roundtrip_logic_buffer_threshold="
            + (bufferThresholdPassed ? "ok" : "failed"));
        Console.WriteLine("roundtrip_logic_identity=" + (identityPassed ? "ok" : "failed"));
        Console.WriteLine("roundtrip_logic_selftest=" + (passed ? "ok" : "failed"));
        Console.WriteLine("roundtrip_logic_selftest_api_calls=not_executed");
        return passed ? 0 : 1;
    }

    // Accept only an ordinary absolute drive path (for example C:\work).
    // UNC, device-namespace, and relative paths have no directly derived root.
    private static string LocalDriveRoot(string cwd)
    {
        if (String.IsNullOrEmpty(cwd) || cwd.Length < 3)
            return null;
        char drive = cwd[0];
        bool isAsciiLetter = (drive >= 'A' && drive <= 'Z') || (drive >= 'a' && drive <= 'z');
        if (!isAsciiLetter || cwd[1] != ':' || (cwd[2] != '\\' && cwd[2] != '/'))
            return null;
        return Char.ToUpperInvariant(drive) + ":\\";
    }

    private static void WriteIndependentPathDiagnostics(string cwd)
    {
        if (cwd == null)
        {
            Console.WriteLine("git_finalpath_open=not_executed");
            Console.WriteLine("git_finalpath_dos_normalized=not_executed");
            Console.WriteLine("git_finalpath_dos_opened=not_executed");
            Console.WriteLine("git_finalpath_nt_normalized=not_executed");
            Console.WriteLine("git_finalpath_nt_opened=not_executed");
            Console.WriteLine("git_long_path_name=not_executed");
            Console.WriteLine("drive_root_attributes=not_executed");
            Console.WriteLine("drive_root_metadata_open=not_executed");
            Console.WriteLine("volume_root_get=not_executed");
            Console.WriteLine("volume_root_attributes=not_executed");
            Console.WriteLine("volume_root_metadata_open=not_executed");
            return;
        }

        IntPtr handle = CreateFileW(cwd, 0, ShareReadWriteDelete, IntPtr.Zero,
            OpenExisting, FileFlagBackupSemantics, IntPtr.Zero);
        string openStatus;
        if (handle == InvalidHandle)
        {
            int error = Marshal.GetLastWin32Error();
            openStatus = Failure(error);
            Console.WriteLine("git_finalpath_open=" + openStatus);
            Console.WriteLine("git_finalpath_dos_normalized=not_executed");
            Console.WriteLine("git_finalpath_dos_opened=not_executed");
            Console.WriteLine("git_finalpath_nt_normalized=not_executed");
            Console.WriteLine("git_finalpath_nt_opened=not_executed");
        }
        else
        {
            Console.WriteLine("git_finalpath_open=ok");
            try
            {
                Console.WriteLine("git_finalpath_dos_normalized="
                    + FinalPathStatus(handle, VolumeNameDos));
                Console.WriteLine("git_finalpath_dos_opened="
                    + FinalPathStatus(handle, VolumeNameDos | FileNameOpened));
                Console.WriteLine("git_finalpath_nt_normalized="
                    + FinalPathStatus(handle, VolumeNameNt));
                Console.WriteLine("git_finalpath_nt_opened="
                    + FinalPathStatus(handle, VolumeNameNt | FileNameOpened));
            }
            finally
            {
                CloseHandle(handle);
            }
        }

        StringBuilder longPath = new StringBuilder((int)MaxPath);
        uint longReturned = GetLongPathNameW(cwd, longPath, MaxPath);
        int longError = longReturned == 0 ? Marshal.GetLastWin32Error() : 0;
        Console.WriteLine("git_long_path_name="
            + CharacterCountStatus(longReturned, MaxPath, longError));

        string driveRoot = LocalDriveRoot(cwd);
        if (driveRoot == null)
        {
            Console.WriteLine("drive_root_attributes=not_executed");
            Console.WriteLine("drive_root_metadata_open=not_executed");
        }
        else
        {
            Console.WriteLine("drive_root_attributes=" + AttributesStatus(driveRoot));
            Console.WriteLine("drive_root_metadata_open="
                + OpenStatus(driveRoot, FileReadAttributes));
        }

        StringBuilder volumeRoot = new StringBuilder((int)MaxPath);
        bool gotVolumeRoot = GetVolumePathNameW(cwd, volumeRoot, MaxPath);
        int volumeRootError = gotVolumeRoot ? 0 : Marshal.GetLastWin32Error();
        string volumeStatus = gotVolumeRoot ? "ok" : Failure(volumeRootError);
        Console.WriteLine("volume_root_get=" + volumeStatus);
        if (!gotVolumeRoot)
        {
            Console.WriteLine("volume_root_attributes=not_executed");
            Console.WriteLine("volume_root_metadata_open=not_executed");
            return;
        }

        Console.WriteLine("volume_root_attributes=" + AttributesStatus(volumeRoot.ToString()));
        Console.WriteLine("volume_root_metadata_open="
            + OpenStatus(volumeRoot.ToString(), FileReadAttributes));
    }

    private static bool ExpectDecision(string expected, string actual)
    {
        return expected == actual;
    }

    // Pure branch checks. They exercise only the decision function, not any
    // Win32 call, and print a separate marker so they cannot resemble API proof.
    private static int RunLogicSelfTest()
    {
        bool passed = true;
        passed &= ExpectDecision("ok", DecideGitMingwGetcwd(
            "ok", "ok", "failed:win32=5", "ok", "not_executed"));
        passed &= ExpectDecision("ok", DecideGitMingwGetcwd(
            "ok", "ok", "ok", "not_executed", "not_executed"));
        passed &= ExpectDecision("ok", DecideGitMingwGetcwd(
            "ok", "failed:win32=5", "not_executed", "not_executed", "ok"));
        passed &= ExpectDecision("failed:errno=ENOENT", DecideGitMingwGetcwd(
            "ok", "failed:win32=5", "not_executed", "not_executed", "failed:win32=5"));
        passed &= ExpectDecision("buffer_too_small",
            DecideGitMingwGetcwd("buffer_too_small", "not_executed", "not_executed",
                "not_executed", "not_executed"));
        passed &= ExpectDecision("buffer_too_small", DecideGitMingwGetcwd(
            "ok", "ok", "buffer_too_small", "buffer_too_small", "not_executed"));
        passed &= ExpectDecision("failed:win32=5", DecideGitMingwGetcwd(
            "ok", "ok", "failed:win32=5", "failed:win32=5", "not_executed"));
        passed &= LocalDriveRoot("X:\\synthetic") == "X:\\";
        passed &= LocalDriveRoot("c:\\synthetic") == "C:\\";
        passed &= LocalDriveRoot("\\\\server\\share") == null;
        passed &= LocalDriveRoot("\\\\?\\C:\\synthetic") == null;
        passed &= LocalDriveRoot("relative") == null;
        Console.WriteLine("logic_selftest=" + (passed ? "ok" : "failed"));
        Console.WriteLine("logic_selftest_api_calls=not_executed");
        return passed ? 0 : 1;
    }

    public static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--logic-selftest")
            return RunLogicSelfTest();
        if (args.Length == 1 && args[0] == "--roundtrip-logic-selftest")
            return RunRoundtripLogicSelfTest();
        if (args.Length == 1 && args[0] == "--roundtrip-v1")
            return 2;
        if (args.Length == 2 && args[0] == "--roundtrip-v1")
        {
            if (String.IsNullOrEmpty(args[1]))
                return 2;
            return RunRoundtripV1(args[1]);
        }
        if (args.Length != 1)
            return 2;

        StringBuilder cwdBuffer = new StringBuilder(32768);
        uint cwdLength = GetCurrentDirectoryW((uint)cwdBuffer.Capacity, cwdBuffer);
        int cwdError = cwdLength == 0 ? Marshal.GetLastWin32Error() : 0;
        string cwdStatus = CharacterCountStatus(cwdLength, (uint)cwdBuffer.Capacity, cwdError);
        Console.WriteLine("getcwd=" + cwdStatus);

        Console.WriteLine("dot:attributes=" + AttributesStatus("."));
        Console.WriteLine("parent:attributes=" + AttributesStatus(args[0]));
        Console.WriteLine("dot_read_attributes:open=" + OpenStatus(".", FileReadAttributes));
        Console.WriteLine("dot_list_directory:open=" + OpenStatus(".", 0x01));
        Console.WriteLine("parent_list_directory:open=" + OpenStatus(args[0], 0x01));
        Console.WriteLine("dot_star:find=" + FindStatus(".\\*"));
        Console.WriteLine("parent_star:find=" + FindStatus(args[0] + "\\*"));

        GitPathResult gitPath = RunGitMingwGetcwdPathCalls();
        Console.WriteLine("git_mingw_getcwd=" + gitPath.Result);
        Console.WriteLine("git_mingw_getcwd_stage=" + gitPath.Stage);
        WriteIndependentPathDiagnostics(cwdStatus == "ok" ? cwdBuffer.ToString() : null);
        return 0;
    }
}
