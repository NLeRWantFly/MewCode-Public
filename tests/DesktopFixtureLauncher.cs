// Test-only Tauri sidecar adapter. No model calls, user profiles or shell commands.
// Compile with the installed .NET Framework csc; see the desktop acceptance report.
using System;
using System.Diagnostics;
using System.IO;

internal static class DesktopFixtureLauncher
{
    private static string Quoted(string value)
    {
        if (value.IndexOf('"') >= 0 || value.IndexOf('\n') >= 0 || value.IndexOf('\r') >= 0)
            throw new ArgumentException("Invalid fixture path");
        return "\"" + value + "\"";
    }

    private static int Main(string[] args)
    {
        try
        {
            int port;
            if (args.Length != 4 || args[0] != "--host" || args[1] != "127.0.0.1"
                || args[2] != "--port" || !Int32.TryParse(args[3], out port)
                || port < 1024 || port > 65535)
                throw new ArgumentException("Expected loopback host and unprivileged port");
            string root = Environment.GetEnvironmentVariable("MEWCODE_FIXTURE_ROOT");
            string data = Environment.GetEnvironmentVariable("MEWCODE_USER_DATA_DIR");
            if (String.IsNullOrEmpty(root) || String.IsNullOrEmpty(data))
                throw new ArgumentException("Explicit fixture and data roots required");
            root = Path.GetFullPath(root);
            data = Path.GetFullPath(data);
            string temporaryRoot = Path.Combine(root, ".tmp") + Path.DirectorySeparatorChar;
            if (!data.StartsWith(temporaryRoot, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Fixture data must be under project .tmp");
            string python = Path.Combine(root, ".venv", "Scripts", "python.exe");
            string fixture = Path.Combine(root, "tests", "ui_task_lifecycle_fixture.py");
            if (!File.Exists(python) || !File.Exists(fixture))
                throw new FileNotFoundException("Fixture or project interpreter is missing");
            var info = new ProcessStartInfo(python,
                Quoted(fixture) + " --host 127.0.0.1 --port " + port
                + " --data-dir " + Quoted(data) + " --exit-on-stdin-eof");
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardInput = true;
            using (var child = Process.Start(info))
            {
                if (child == null) throw new InvalidOperationException("No fixture process");
                Console.WriteLine("MEWCODE_FIXTURE_CHILD_PID " + child.Id);
                // Keep this pipe alive. When Tauri stops this wrapper the child
                // sees EOF and shuts down through its real lifespan cleanup.
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Desktop fixture failed: " + error.GetType().Name);
            return 2;
        }
    }
}
