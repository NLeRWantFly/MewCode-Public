// Fixed synthetic-only escape probes for the experimental Windows LPAC boundary.
// No user scripts, arbitrary paths, destinations, or file contents are accepted.
using System;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

internal static class MewCodeEscapePayload
{
    private const int NetworkTimeoutMs = 2000;
    private const string FixturePrefix = "mewcode-lpac-probe-";
    private const string ExpectedInput = "SYNTHETIC_INPUT";

    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args == null || args.Length == 0)
            return InvalidArguments();

        if (String.Equals(args[0], "files", StringComparison.Ordinal))
        {
            string workspace;
            string outside;
            if (args.Length != 4 || !TryValidateFixturePaths(args[1], args[2], out workspace, out outside) ||
                !IsSafeNonce(args[3]))
                return InvalidArguments();
            return ProbeFiles(workspace, outside, args[3]);
        }

        if (String.Equals(args[0], "network", StringComparison.Ordinal))
        {
            int port;
            if (args.Length != 4 || !IsNetworkMode(args[1]) ||
                !Int32.TryParse(args[2], NumberStyles.None, CultureInfo.InvariantCulture, out port) ||
                port < 1 || port > 65535 || !IsSafeNonce(args[3]))
                return InvalidArguments();
            return ProbeNetwork(args[1], port, args[3]);
        }

        return InvalidArguments();
    }

    private static int InvalidArguments()
    {
        Emit("ESCAPE_INVALID_ARGUMENTS");
        return 2;
    }

    private static bool TryValidateFixturePaths(string workspaceRaw, string outsideRaw,
        out string workspace, out string outside)
    {
        workspace = null;
        outside = null;
        try
        {
            workspace = ValidateAbsoluteLocalPath(workspaceRaw);
            outside = ValidateAbsoluteLocalPath(outsideRaw);
            string fixture = Path.GetDirectoryName(workspace);
            if (!String.Equals(Path.GetFileName(workspace), "workspace", StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(Path.GetFileName(outside), "outside", StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(Path.GetDirectoryName(outside), fixture, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(Path.GetFileName(Path.GetDirectoryName(fixture)), ".tmp", StringComparison.OrdinalIgnoreCase))
                return false;
            string leaf = Path.GetFileName(fixture);
            if (!leaf.StartsWith(FixturePrefix, StringComparison.OrdinalIgnoreCase)) return false;
            string suffix = leaf.Substring(FixturePrefix.Length);
            if (suffix.Length < 8 || suffix.Length > 64) return false;
            foreach (char value in suffix)
                if (!IsAsciiLetter(value) && (value < '0' || value > '9') && value != '_' && value != '-')
                    return false;
            return true;
        }
        catch
        {
            workspace = null;
            outside = null;
            return false;
        }
    }

    private static string ValidateAbsoluteLocalPath(string raw)
    {
        if (String.IsNullOrEmpty(raw) || raw.IndexOf('\0') >= 0 || raw.Length < 3 ||
            !IsAsciiLetter(raw[0]) || raw[1] != ':' || (raw[2] != '\\' && raw[2] != '/') ||
            raw.StartsWith("\\\\?\\", StringComparison.Ordinal) ||
            raw.StartsWith("\\\\.\\", StringComparison.Ordinal) || raw.IndexOf(':', 2) >= 0)
            throw new ArgumentException();
        foreach (string part in raw.Replace('/', '\\').Split('\\'))
            if (part == "..") throw new ArgumentException();
        string normalized = Path.GetFullPath(raw);
        if (!String.Equals(normalized, raw.Replace('/', '\\').TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException();
        return normalized;
    }

    private static bool IsAsciiLetter(char value)
    {
        return (value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z');
    }

    private static bool IsSafeNonce(string nonce)
    {
        if (String.IsNullOrEmpty(nonce) || nonce.Length > 128) return false;
        foreach (char value in nonce)
            if (!IsAsciiLetter(value) && (value < '0' || value > '9') && value != '-' && value != '_')
                return false;
        return true;
    }

    private static bool IsNetworkMode(string mode)
    {
        return mode == "tcp4" || mode == "tcp6" || mode == "udp4" || mode == "udp6";
    }

    private static int ProbeFiles(string workspace, string outside, string nonce)
    {
        // The host validates the fresh fixture and junction target before launch.
        // LPAC cannot necessarily read the fixture root or outside directory.
        Emit("FILES_" + nonce);
        bool success = ProbeInput(Path.Combine(workspace, "input.txt"));
        success = ProbeWriteNew(Path.Combine(workspace, "junction", "junction-escaped.txt"),
            "JUNCTION_WRITE", "SYNTHETIC_ESCAPE_" + nonce) && success;
        string grants = Path.Combine(workspace, ".mewcode", "permissions.grants.json");
        string rules = Path.Combine(workspace, ".mewcode", "rules", "host.rules");
        success = ProbeReadExisting(grants, "AUTH_GRANTS_READ") && success;
        success = ProbeWriteExisting(grants, "AUTH_GRANTS_WRITE") && success;
        success = ProbeReadExisting(rules, "AUTH_RULES_READ") && success;
        success = ProbeWriteExisting(rules, "AUTH_RULES_WRITE") && success;
        Emit("FILES_DONE");
        return success ? 0 : 2;
    }

    private static bool ProbeInput(string path)
    {
        try
        {
            bool valid = String.Equals(File.ReadAllText(path, Encoding.UTF8), ExpectedInput, StringComparison.Ordinal);
            Emit(valid ? "INPUT_OK" : "INPUT_INVALID");
            return valid;
        }
        catch (Exception ex)
        {
            Emit("INPUT_UNKNOWN_" + SafeType(ex));
            return false;
        }
    }

    private static bool ProbeWriteNew(string path, string marker, string value)
    {
        try
        {
            File.WriteAllText(path, value, Encoding.ASCII);
            Emit(marker + "_ALLOWED");
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            Emit(marker + "_DENIED");
            return true;
        }
        catch (Exception ex)
        {
            Emit(marker + "_UNKNOWN_" + SafeType(ex));
            return false;
        }
    }

    private static bool ProbeReadExisting(string path, string marker)
    {
        try
        {
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                stream.ReadByte(); // Never output or retain the synthetic authority file content.
            Emit(marker + "_ALLOWED");
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            Emit(marker + "_DENIED");
            return true;
        }
        catch (Exception ex)
        {
            Emit(marker + "_UNKNOWN_" + SafeType(ex));
            return false;
        }
    }

    private static bool ProbeWriteExisting(string path, string marker)
    {
        try
        {
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Write, FileShare.ReadWrite))
                stream.WriteByte((byte)'X'); // Fixed synthetic file only; visible if the boundary fails.
            Emit(marker + "_ALLOWED");
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            Emit(marker + "_DENIED");
            return true;
        }
        catch (Exception ex)
        {
            Emit(marker + "_UNKNOWN_" + SafeType(ex));
            return false;
        }
    }

    private static int ProbeNetwork(string mode, int port, string nonce)
    {
        IPAddress address = mode.EndsWith("4", StringComparison.Ordinal) ? IPAddress.Loopback : IPAddress.IPv6Loopback;
        byte[] bytes = Encoding.ASCII.GetBytes(nonce);
        string outcome = mode.StartsWith("tcp", StringComparison.Ordinal)
            ? ProbeTcp(address, port, bytes) : ProbeUdp(address, port, bytes);
        Emit("NETWORK_" + mode.ToUpperInvariant() + "_" + outcome);
        return outcome == "DENIED" ? 0 : 2;
    }

    private static string ProbeTcp(IPAddress address, int port, byte[] bytes)
    {
        try
        {
            using (TcpClient client = new TcpClient(address.AddressFamily))
            {
                client.SendTimeout = NetworkTimeoutMs;
                IAsyncResult pending = client.BeginConnect(address, port, null, null);
                using (WaitHandle wait = pending.AsyncWaitHandle)
                {
                    if (!wait.WaitOne(NetworkTimeoutMs)) return "UNKNOWN_TIMEOUT";
                }
                client.EndConnect(pending);
                using (NetworkStream stream = client.GetStream())
                {
                    stream.WriteTimeout = NetworkTimeoutMs;
                    stream.Write(bytes, 0, bytes.Length);
                }
                return "ALLOWED";
            }
        }
        catch (SocketException ex)
        {
            return ex.SocketErrorCode == SocketError.AccessDenied ? "DENIED" : "UNKNOWN_" + SafeType(ex);
        }
        catch (Exception ex)
        {
            return "UNKNOWN_" + SafeType(ex);
        }
    }

    private static string ProbeUdp(IPAddress address, int port, byte[] bytes)
    {
        try
        {
            using (Socket socket = new Socket(address.AddressFamily, SocketType.Dgram, ProtocolType.Udp))
            {
                socket.SendTimeout = NetworkTimeoutMs;
                int sent = socket.SendTo(bytes, new IPEndPoint(address, port));
                return sent == bytes.Length ? "ALLOWED" : "UNKNOWN_SHORT_SEND";
            }
        }
        catch (SocketException ex)
        {
            return ex.SocketErrorCode == SocketError.AccessDenied ? "DENIED" : "UNKNOWN_" + SafeType(ex);
        }
        catch (Exception ex)
        {
            return "UNKNOWN_" + SafeType(ex);
        }
    }

    private static string SafeType(Exception ex)
    {
        string name = ex.GetType().Name;
        StringBuilder safe = new StringBuilder(Math.Min(name.Length, 48));
        for (int i = 0; i < name.Length && safe.Length < 48; i++)
        {
            char value = name[i];
            if (IsAsciiLetter(value) || (value >= '0' && value <= '9')) safe.Append(value);
            else safe.Append('_');
        }
        return safe.ToString();
    }

    private static void Emit(string value)
    {
        Console.Out.WriteLine(value);
        Console.Out.Flush();
    }
}
