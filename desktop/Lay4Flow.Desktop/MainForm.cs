using System.Diagnostics;
using System.IO.Pipes;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Lay4Flow.Desktop;

internal sealed class MainForm : Form
{
    private readonly DesktopOptions options;
    private readonly WebView2 browser = new() { Dock = DockStyle.Fill, Visible = false };
    private readonly Label loading = new() { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter, Font = new Font("Microsoft YaHei UI", 13), Text = "正在启动 lay4flow-remote…" };
    private readonly CancellationTokenSource lifetime = new();
    private readonly string logPath;
    private readonly List<Form> childWindows = [];
    private bool historyPending;
    private bool checkPending;
    private int updateRunning;
    private readonly string windowStatePath;
    private static string Version => Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "1.3.0";

    public MainForm(DesktopOptions options)
    {
        this.options = options;
        Text = "lay4flow-remote";
        Icon = Icon.ExtractAssociatedIcon(Environment.ProcessPath!);
        StartPosition = FormStartPosition.CenterScreen;
        var workArea = Screen.FromPoint(Cursor.Position).WorkingArea;
        ApplyWindowMinimum(workArea);
        Size = new Size(Math.Min(1600, workArea.Width), Math.Min(1000, workArea.Height));
        WindowState = FormWindowState.Maximized;
        BackColor = Color.White;
        logPath = Path.Combine(Path.GetDirectoryName(options.UserDataDir)!, "desktop.log");
        windowStatePath = Path.Combine(Path.GetDirectoryName(options.UserDataDir)!, "window.json");
        Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
        if (!File.Exists(logPath)) File.WriteAllText(logPath, $"lay4flow-remote desktop v{Version}\n", Encoding.UTF8);
        var menu = new MenuStrip { BackColor = Color.White, Font = new Font("Microsoft YaHei UI", 9) };
        var file = new ToolStripMenuItem("应用");
        file.DropDownItems.Add("刷新", null, (_, _) => { if (browser.CoreWebView2 is not null) browser.Reload(); });
        file.DropDownItems.Add("打开安装目录", null, (_, _) => OpenLocal(options.InstallDir));
        file.DropDownItems.Add("启动日志", null, (_, _) => OpenLocal(logPath));
        file.DropDownItems.Add(new ToolStripSeparator());
        file.DropDownItems.Add("关闭窗口（后台任务继续）", null, (_, _) => Close());
        menu.Items.Add(file);
        menu.Items.Add("更新日志", null, async (_, _) => await OpenHistory());
        menu.Items.Add("检查更新", null, async (_, _) => await OpenHistory(true));
        menu.Items.Add(new ToolStripMenuItem($"桌面端 v{Version}") { Alignment = ToolStripItemAlignment.Right, Enabled = false });
        Controls.Add(browser);
        Controls.Add(loading);
        Controls.Add(menu);
        MainMenuStrip = menu;
        Shown += async (_, _) => { ApplyWindowMinimum(Screen.FromHandle(Handle).WorkingArea); _ = ListenForActivation(); await StartApplication(); };
        FormClosing += (_, eventArgs) =>
        {
            if (Volatile.Read(ref updateRunning) == 0) return;
            eventArgs.Cancel = true;
            PostUpdateStatus("running", "更新正在进行中，完成后会自动重启桌面端。请稍候…");
        };
        FormClosed += (_, _) =>
        {
            SaveWindowBounds();
            lifetime.Cancel();
            foreach (var child in childWindows.ToArray()) child.Close();
            browser.Dispose();
            // The Docker service owns queued work and survives desktop window closure.
        };
        LoadWindowBounds();
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        base.OnDpiChanged(e);
        ApplyWindowMinimum(Screen.FromHandle(Handle).WorkingArea);
    }

    private void ApplyWindowMinimum(Rectangle workArea)
    {
        // Minimum client workspace is 1100 x 680 at 100% DPI.  On smaller
        // displays cap it to the actual working area instead of hiding edges.
        var scale = DeviceDpi / 96d;
        MinimumSize = new Size(
            Math.Min((int)Math.Round(1100 * scale), workArea.Width),
            Math.Min((int)Math.Round(720 * scale), workArea.Height));
    }

    private void LoadWindowBounds()
    {
        try
        {
            if (!File.Exists(windowStatePath)) return;
            var saved = JsonSerializer.Deserialize<SavedWindow>(File.ReadAllText(windowStatePath));
            if (saved is null) return;
            var proposed = new Rectangle(saved.X, saved.Y, saved.Width, saved.Height);
            var screen = Screen.AllScreens.FirstOrDefault(item => item.WorkingArea.IntersectsWith(proposed));
            if (screen is null) return;
            var workArea = screen.WorkingArea;
            ApplyWindowMinimum(workArea);
            var width = Math.Clamp(saved.Width, MinimumSize.Width, workArea.Width);
            var height = Math.Clamp(saved.Height, MinimumSize.Height, workArea.Height);
            StartPosition = FormStartPosition.Manual;
            Bounds = new Rectangle(Math.Clamp(saved.X, workArea.Left, workArea.Right - width), Math.Clamp(saved.Y, workArea.Top, workArea.Bottom - height), width, height);
            WindowState = saved.Maximized ? FormWindowState.Maximized : FormWindowState.Normal;
        }
        catch (Exception error) { Log("Window restore: " + error.Message); }
    }

    private void SaveWindowBounds()
    {
        try
        {
            var bounds = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
            File.WriteAllText(windowStatePath, JsonSerializer.Serialize(new SavedWindow(bounds.X, bounds.Y, bounds.Width, bounds.Height, WindowState != FormWindowState.Normal)), Encoding.UTF8);
        }
        catch { }
    }

    private sealed record SavedWindow(int X, int Y, int Width, int Height, bool Maximized);

    private async Task ListenForActivation()
    {
        while (!lifetime.IsCancellationRequested)
        {
            try
            {
                await using var pipe = new NamedPipeServerStream("lay4flow-remote.Desktop." + options.InstanceKey, PipeDirection.In, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                await pipe.WaitForConnectionAsync(lifetime.Token);
                if (await pipe.ReadAsync(new byte[1], lifetime.Token) > 0)
                    BeginInvoke(() => { if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal; Show(); Activate(); });
            }
            catch (OperationCanceledException) { break; }
            catch (Exception error) { Log("Activation: " + error.Message); }
        }
    }

    private async Task StartApplication()
    {
        try
        {
            if (!options.NoStart) await StartService();
            lifetime.Token.ThrowIfCancellationRequested();
            loading.Text = "正在打开创作工坊…";
            var arguments = options.DebuggingPort is int port ? $"--remote-debugging-port={port} --remote-debugging-address=127.0.0.1" : "";
            var environment = await CoreWebView2Environment.CreateAsync(null, options.UserDataDir, new CoreWebView2EnvironmentOptions(arguments));
            await browser.EnsureCoreWebView2Async(environment);
            ConfigureView(browser);
            browser.CoreWebView2.WebMessageReceived += (_, args) => _ = HandleWebMessage(args);
            await browser.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync($"window.lay4flowDesktop = Object.freeze({JsonSerializer.Serialize(new { version = Version })});");
            browser.NavigationCompleted += async (_, eventArgs) =>
            {
                if (!eventArgs.IsSuccess) { ShowFailure(new Exception("页面加载失败：" + eventArgs.WebErrorStatus)); return; }
                if (!options.IsAppUrl(browser.Source.AbsoluteUri)) return;
                browser.Visible = true;
                loading.Visible = false;
                if (options.ReadyFile is string file)
                {
                    var path = Path.GetFullPath(file);
                    Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                    await File.WriteAllTextAsync(path, JsonSerializer.Serialize(new { pid = Environment.ProcessId, url = browser.Source.AbsoluteUri, version = Version, window_handle = Handle.ToInt64() }));
                }
                if (historyPending) { historyPending = false; await OpenHistory(checkPending); checkPending = false; }
                var updateStatus = Path.Combine(options.InstallDir, ".lay4flow-updates", "desktop-update-status.json");
                if (File.Exists(updateStatus))
                {
                    try
                    {
                        using var status = JsonDocument.Parse(await File.ReadAllTextAsync(updateStatus));
                        PostUpdateStatus(status.RootElement.GetProperty("state").GetString() ?? "failed", status.RootElement.GetProperty("message").GetString() ?? "");
                        File.Delete(updateStatus);
                    }
                    catch (Exception error) { Log("Update result: " + error.Message); }
                }
            };
            browser.Source = options.Url;
        }
        catch (OperationCanceledException) { }
        catch (WebView2RuntimeNotFoundException error)
        {
            Log(error.ToString());
            loading.Text = "需要 Microsoft Edge WebView2 Runtime。\n请安装后重新打开 lay4flow-remote。";
            if (MessageBox.Show("桌面端需要 Microsoft Edge WebView2 Runtime。是否打开 Microsoft 官方安装页面？", Text, MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
                OpenExternal("https://developer.microsoft.com/microsoft-edge/webview2/");
        }
        catch (Exception error) { ShowFailure(error); }
    }

    private async Task StartService()
    {
        var script = Path.Combine(options.InstallDir, ".lay4flow-windows", "start.ps1");
        if (!File.Exists(script)) throw new FileNotFoundException("安装文件缺失，请重新运行安装包。", script);
        loading.Text = "正在检查 Docker 并启动服务…\n首次启动可能需要稍等片刻。";
        var info = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = options.InstallDir,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
        };
        foreach (var arg in new[] { "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-InstallDir", options.InstallDir, "-NoBrowser" }) info.ArgumentList.Add(arg);
        using var process = Process.Start(info) ?? throw new Exception("服务启动进程未建立。");
        var output = process.StandardOutput.ReadToEndAsync(lifetime.Token);
        var errors = process.StandardError.ReadToEndAsync(lifetime.Token);
        try { await process.WaitForExitAsync(lifetime.Token); }
        catch (OperationCanceledException)
        {
            // Only terminate this launcher process; Docker keeps accepted server work.
            if (!process.HasExited) process.Kill();
            throw;
        }
        var detail = (await output) + Environment.NewLine + (await errors);
        Log(detail);
        if (process.ExitCode != 0) throw new Exception($"服务启动失败（{process.ExitCode}）。\n{detail.Trim()}\n\n日志：{logPath}");
    }

    private void ConfigureView(WebView2 view)
    {
        var core = view.CoreWebView2;
        core.Settings.AreDevToolsEnabled = options.DebuggingPort.HasValue;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreBrowserAcceleratorKeysEnabled = true;
        core.Settings.IsGeneralAutofillEnabled = false;
        core.Settings.IsPasswordAutosaveEnabled = false;
        core.NavigationStarting += (_, args) =>
        {
            if (options.IsAppUrl(args.Uri) || args.Uri == "about:blank") return;
            args.Cancel = true;
            OpenExternal(args.Uri);
        };
        core.NewWindowRequested += async (_, args) =>
        {
            var deferral = args.GetDeferral();
            try
            {
                if (!options.IsAppUrl(args.Uri) && args.Uri != "about:blank") { args.Handled = true; OpenExternal(args.Uri); return; }
                var child = new Form { Text = "lay4flow-remote · 账号浏览器", Icon = Icon, Size = new Size(1280, 860), MinimumSize = new Size(800, 560), StartPosition = FormStartPosition.CenterParent };
                var childView = new WebView2 { Dock = DockStyle.Fill };
                child.Controls.Add(childView);
                await childView.EnsureCoreWebView2Async(core.Environment);
                ConfigureView(childView);
                childView.CoreWebView2.WindowCloseRequested += (_, _) => child.Close();
                child.FormClosed += (_, _) => { childWindows.Remove(child); childView.Dispose(); };
                childWindows.Add(child);
                args.NewWindow = childView.CoreWebView2;
                args.Handled = true;
                child.Show(this);
            }
            catch (Exception error) { args.Handled = true; ShowFailure(error); }
            finally { deferral.Complete(); }
        };
        core.DownloadStarting += (_, args) => { args.Handled = false; };
    }

    private async Task HandleWebMessage(CoreWebView2WebMessageReceivedEventArgs args)
    {
        try
        {
            using var payload = JsonDocument.Parse(args.WebMessageAsJson);
            if (!payload.RootElement.TryGetProperty("type", out var type) || type.GetString() != "apply-update") return;
            if (browser.CoreWebView2 is null || !options.IsAppUrl(args.Source) || !options.IsAppUrl(browser.Source.AbsoluteUri)) return;
            if (Interlocked.Exchange(ref updateRunning, 1) != 0)
            {
                PostUpdateStatus("running", "更新正在进行中，请稍候…");
                return;
            }
            var version = payload.RootElement.TryGetProperty("version", out var versionNode) ? versionNode.GetString() : null;
            await RunDesktopUpdate(version);
        }
        catch (JsonException) { }
        catch (Exception error)
        {
            Log("Update bridge: " + error);
            PostUpdateStatus("failed", error.Message);
            Interlocked.Exchange(ref updateRunning, 0);
        }
    }

    private async Task RunDesktopUpdate(string? version)
    {
        var script = Path.Combine(options.InstallDir, ".lay4flow-windows", "desktop-update.ps1");
        if (!File.Exists(script)) throw new FileNotFoundException("安装目录中缺少更新脚本，请重新安装最新版。", script);
        PostUpdateStatus("running", "正在下载并校验桌面更新…");
        var info = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = options.InstallDir,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        var args = new List<string> { "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-InstallDir", options.InstallDir, "-Phase", "Prepare" };
        if (!string.IsNullOrWhiteSpace(version)) { args.Add("-Version"); args.Add(version); }
        foreach (var arg in args)
            info.ArgumentList.Add(arg);
        using var process = Process.Start(info) ?? throw new Exception("更新进程未建立。");
        var output = new StringBuilder();
        async Task Pump(StreamReader reader)
        {
            while (await reader.ReadLineAsync() is { } line)
            {
                if (output.Length > 6000) output.Remove(0, output.Length - 6000);
                output.AppendLine(line);
                if (line.Length > 0) PostUpdateStatus("running", line);
            }
        }
        var stdout = Pump(process.StandardOutput);
        var stderr = Pump(process.StandardError);
        await process.WaitForExitAsync(lifetime.Token);
        await Task.WhenAll(stdout, stderr);
        Log("Update output:\n" + output);
        if (process.ExitCode != 0)
        {
            PostUpdateStatus("failed", $"更新失败（{process.ExitCode}）。{output.ToString().Trim()}");
            Interlocked.Exchange(ref updateRunning, 0);
            return;
        }
        PostUpdateStatus("running", "服务更新完成，正在重启桌面端…");
        Interlocked.Exchange(ref updateRunning, 0);
        var apply = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = options.InstallDir
        };
        foreach (var arg in new[] { "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-InstallDir", options.InstallDir, "-Phase", "Apply", "-ParentProcessId", Environment.ProcessId.ToString() })
            apply.ArgumentList.Add(arg);
        Process.Start(apply);
        PostUpdateStatus("completed", "更新已准备，桌面端将自动重启…");
        await Task.Delay(250);
        Close();
    }

    private void PostUpdateStatus(string state, string message)
    {
        if (IsDisposed || browser.CoreWebView2 is null) return;
        var json = JsonSerializer.Serialize(new { type = "desktop-update-status", state, message });
        try
        {
            browser.CoreWebView2.PostWebMessageAsJson(json);
            _ = browser.ExecuteScriptAsync("window.dispatchEvent(new CustomEvent('desktop-update-status', { detail: " + json + " }));");
        }
        catch { }
    }

    private async Task OpenHistory(bool check = false)
    {
        if (browser.CoreWebView2 is null) { historyPending = true; checkPending = check; return; }
        await browser.ExecuteScriptAsync("window.dispatchEvent(new CustomEvent('open-release-history', { detail: { check: " + (check ? "true" : "false") + " } }));");
    }

    private void ShowFailure(Exception error)
    {
        Log(error.ToString());
        if (IsDisposed) return;
        loading.Visible = true;
        loading.Text = "启动遇到问题，请检查 Docker 是否已启动。\n点击「应用 → 启动日志」查看具体原因。\n\n" + error.Message;
        browser.Visible = false;
    }

    private void Log(string text)
    {
        try { File.AppendAllText(logPath, $"[{DateTimeOffset.Now:O}] {text}\n", Encoding.UTF8); }
        catch { }
    }

    private static void OpenLocal(string path) => Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });

    private static void OpenExternal(string value)
    {
        if (Uri.TryCreate(value, UriKind.Absolute, out var uri) && uri.Scheme is "https" or "http" && uri.UserInfo.Length == 0 && !uri.IsLoopback)
            Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
    }
}
