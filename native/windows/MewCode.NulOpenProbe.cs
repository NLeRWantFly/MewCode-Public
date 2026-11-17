// Disposable synthetic child used only by the one-shot NUL ACL experiment.
using System;
using System.Runtime.InteropServices;

public static class MewCodeNulOpenProbe
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFileW(string path, uint access, uint share, IntPtr security,
        uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    private static string TryOpen(string label, uint access)
    {
        IntPtr handle = CreateFileW("NUL", access, 3, IntPtr.Zero, 3, 0x80, IntPtr.Zero);
        if (handle == IntPtr.Zero || handle == new IntPtr(-1))
            return label + ":open=failed:win32=" + Marshal.GetLastWin32Error();
        CloseHandle(handle);
        return label + ":open=ok";
    }

    public static int Main(string[] args)
    {
        if (args.Length != 0) return 2;
        Console.WriteLine(TryOpen("nul-read", 0x80000000));
        Console.WriteLine(TryOpen("nul-write", 0x40000000));
        Console.WriteLine(TryOpen("nul-readwrite", 0xC0000000));
        return 0;
    }
}
