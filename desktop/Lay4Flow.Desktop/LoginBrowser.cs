using System.Diagnostics;

namespace Lay4Flow.Desktop;

internal static class LoginBrowser
{
    internal static ProcessStartInfo StartInfo(DesktopOptions options, Func<string, bool>? exists = null)
    {
        exists ??= File.Exists;
        var roots = new[]
        {
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData)
        }.Where(root => !string.IsNullOrWhiteSpace(root));
        var paths = roots.Select(root => Path.Combine(root, "Google", "Chrome", "Application", "chrome.exe"))
            .Concat(roots.Select(root => Path.Combine(root, "Microsoft", "Edge", "Application", "msedge.exe")));
        var executable = paths.FirstOrDefault(exists)
            ?? throw new InvalidOperationException("请先安装 Chrome 或 Microsoft Edge，然后重新打开扩展登录。");
        // The URL belongs to this desktop instance; renderer content cannot
        // provide an executable, arbitrary address or browser command line.
        var management = new Uri(options.Url.GetLeftPart(UriPartial.Authority) + "/#/accounts");
        var info = new ProcessStartInfo(executable) { UseShellExecute = false };
        info.ArgumentList.Add("--new-window");
        info.ArgumentList.Add(management.AbsoluteUri);
        return info;
    }

    internal static void Open(DesktopOptions options)
    {
        using var process = Process.Start(StartInfo(options))
            ?? throw new InvalidOperationException("本机浏览器未启动，请复制管理地址到 Chrome 或 Edge。");
    }
}
