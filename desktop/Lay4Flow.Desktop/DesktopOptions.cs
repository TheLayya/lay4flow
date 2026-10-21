using System.Security.Cryptography;
using System.Text;

namespace Lay4Flow.Desktop;

internal sealed record DesktopOptions(
    string InstallDir, Uri Url, string UserDataDir, bool NoStart,
    string? ReadyFile, int? DebuggingPort, string InstanceKey)
{
    public static DesktopOptions Parse(string[] args)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        var noStart = false;
        for (var index = 0; index < args.Length; index++)
        {
            if (args[index] == "--no-start") { noStart = true; continue; }
            if (args[index] is not ("--install-dir" or "--url" or "--user-data-dir" or "--ready-file" or "--remote-debugging-port") || index + 1 >= args.Length)
                throw new ArgumentException("Unrecognized or incomplete desktop argument: " + args[index]);
            if (!values.TryAdd(args[index], args[++index])) throw new ArgumentException("Repeated desktop argument: " + args[index - 1]);
        }
        var install = Path.GetFullPath(values.GetValueOrDefault("--install-dir") ?? Path.Combine(AppContext.BaseDirectory, "..", ".."));
        var port = 38000;
        var env = Path.Combine(install, ".env");
        if (File.Exists(env))
            foreach (var line in File.ReadLines(env))
                if (line.StartsWith("LAY4FLOW_REMOTE_PORT=", StringComparison.Ordinal) &&
                    int.TryParse(line.Split('=', 2)[1].Trim(), out var configured)) port = configured;
        var url = new Uri(values.GetValueOrDefault("--url") ?? $"http://127.0.0.1:{port}/login");
        if (!IsLoopbackUrl(url)) throw new ArgumentException("Desktop startup URL must use HTTP on a loopback address.");
        if (noStart && !values.ContainsKey("--url")) throw new ArgumentException("--no-start requires an explicit loopback --url.");
        var keyInput = noStart ? url.GetLeftPart(UriPartial.Authority) : install.ToUpperInvariant();
        var instance = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(keyInput)))[..20];
        var userData = Path.GetFullPath(values.GetValueOrDefault("--user-data-dir") ??
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "lay4flow-remote", "desktop", instance, "webview2"));
        int? debug = null;
        if (values.TryGetValue("--remote-debugging-port", out var debugValue))
        {
            if (!int.TryParse(debugValue, out var parsed) || parsed < 1024 || parsed > 65535)
                throw new ArgumentException("Remote debugging port must be between 1024 and 65535.");
            debug = parsed;
        }
        return new(install, url, userData, noStart, values.GetValueOrDefault("--ready-file"), debug, instance);
    }

    public static bool IsLoopbackUrl(Uri uri) => uri.IsAbsoluteUri && uri.Scheme == "http" &&
        uri.IsLoopback && uri.UserInfo.Length == 0;

    public bool IsAppUrl(string value) => Uri.TryCreate(value, UriKind.Absolute, out var uri) &&
        IsLoopbackUrl(uri) && uri.GetLeftPart(UriPartial.Authority).Equals(Url.GetLeftPart(UriPartial.Authority), StringComparison.OrdinalIgnoreCase);
}
