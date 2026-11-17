// Trusted, side-effect-free kernel fence probe. No task commands or uploads.
using System;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Web.Script.Serialization;

namespace MewCode.Native.Windows {
    public static class NetworkProbe {
        [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr p, uint a, out IntPtr t);
        [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr t, int c, IntPtr b, int n, out int r);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
        static string PackageSid() {
            IntPtr token;
            if (!OpenProcessToken(Process.GetCurrentProcess().Handle, 8, out token)) throw new InvalidOperationException("token_query_failed");
            IntPtr data = Marshal.AllocHGlobal(256);
            try {
                int returned;
                if (!GetTokenInformation(token, 29, data, 4, out returned) || Marshal.ReadInt32(data) != 1) return null;
                if (!GetTokenInformation(token, 31, data, 256, out returned)) throw new InvalidOperationException("package_query_failed");
                return new SecurityIdentifier(Marshal.ReadIntPtr(data)).Value;
            } finally { Marshal.FreeHGlobal(data); CloseHandle(token); }
        }
        static int Connect(IPAddress address, int port) {
            using (var socket = new Socket(address.AddressFamily, SocketType.Stream, ProtocolType.Tcp)) {
                try {
                    var pending = socket.BeginConnect(address, port, null, null);
                    using (pending.AsyncWaitHandle) {
                        if (!pending.AsyncWaitHandle.WaitOne(1500)) return -2;
                        socket.EndConnect(pending);
                    }
                    return 0;
                } catch (SocketException error) { return error.NativeErrorCode; }
            }
        }
        public static int Main(string[] args) {
            try {
                if (args.Length != 2) throw new ArgumentException("expected package SID and challenge port");
                string expected = new SecurityIdentifier(args[0]).Value;
                int port = Int32.Parse(args[1]);
                if (!expected.StartsWith("S-1-15-2-") || port < 1024 || port > 65535 || port == 60080 || port == 60081)
                    throw new ArgumentException("invalid bounded probe");
                string actual = PackageSid();
                bool identity = actual == expected;
                int proxy = identity ? Connect(IPAddress.Loopback, 60081) : -1;
                int v4 = identity ? Connect(IPAddress.Loopback, port) : -1;
                int v6 = identity ? Connect(IPAddress.IPv6Loopback, port) : -1;
                bool success = identity && proxy == 0 && v4 == 10013 && v6 == 10013;
                Console.WriteLine(new JavaScriptSerializer().Serialize(new {
                    schema = "mewcode-network-probe-v1", package_sid = actual,
                    proxy_reachable = proxy == 0, direct_v4_denied = v4 == 10013,
                    direct_v6_denied = v6 == 10013, v4_error = v4, v6_error = v6,
                    pid = Process.GetCurrentProcess().Id, ready = success
                }));
                return success ? 0 : 2;
            } catch (Exception error) {
                Console.WriteLine(new JavaScriptSerializer().Serialize(new { schema = "mewcode-network-probe-v1", ready = false, error = error.Message }));
                return 2;
            }
        }
    }
}
