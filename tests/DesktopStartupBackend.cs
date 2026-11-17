// Isolated console-subsystem backend for native startup/health/retry verification.
using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;

class DesktopStartupBackend {
    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    static int Main(string[] args) {
        var root = Environment.GetEnvironmentVariable("MEWCODE_USER_DATA_DIR");
        Directory.CreateDirectory(root);
        var attemptFile = Path.Combine(root, "fixture-attempt.txt");
        int attempt = File.Exists(attemptFile) ? Int32.Parse(File.ReadAllText(attemptFile)) + 1 : 1;
        File.WriteAllText(attemptFile, attempt.ToString());
        File.WriteAllText(Path.Combine(root, "fixture-child-" + attempt + ".json"),
            "{\"pid\":" + Process.GetCurrentProcess().Id + ",\"console_window\":" + GetConsoleWindow().ToInt64() + "}");
        if (File.Exists(Path.Combine(root, "fail-startup"))) return 7;
        int port = Int32.Parse(args[Array.IndexOf(args, "--port") + 1]);
        var listener = new TcpListener(IPAddress.Loopback, port);
        listener.Start();
        while (true) {
            using (var client = listener.AcceptTcpClient()) {
            var stream = client.GetStream();
            var reader = new StreamReader(stream, Encoding.UTF8);
            var request = reader.ReadLine();
            if (request == null) continue;
            string path = request.Split(' ')[1].Split('?')[0];
            int length = 0;
            string header;
            while (!String.IsNullOrEmpty(header = reader.ReadLine())) {
                if (header.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                    length = Int32.Parse(header.Substring(15).Trim());
            }
            for (int i = 0; i < length; i++) reader.Read();
            string body = "{}";
            string status = "200 OK", headers = "Content-Type: application/json; charset=utf-8\r\n";
            if (path == "/api/health") {
                if (!File.Exists(Path.Combine(root, "ready"))) status = "503 Not Ready";
            } else if (path == "/desktop-boot") {
                status = "302 Found";
                headers += "Location: /\r\n";
            } else if (path == "/") {
                headers = "Content-Type: text/html; charset=utf-8\r\n";
                body = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>MewCode</title>" +
                    "<body style='font:16px Segoe UI;text-align:center;padding:180px 24px;background:#f8f9fc'>" +
                    "<h1>MewCode 启动验证通过</h1><p>服务就绪后已自动进入工作区。</p></body></html>";
            }
            byte[] bytes = Encoding.UTF8.GetBytes(body);
            byte[] prefix = Encoding.ASCII.GetBytes("HTTP/1.1 " + status + "\r\n" + headers +
                "Connection: close\r\nContent-Length: " + bytes.Length + "\r\n\r\n");
            stream.Write(prefix, 0, prefix.Length);
            stream.Write(bytes, 0, bytes.Length);
            }
        }
    }
}
