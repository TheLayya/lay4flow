using System.IO.Pipes;

namespace Lay4Flow.Desktop;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--validate-options-json")
        {
            try
            {
                var options = DesktopOptions.Parse(args[1..]);
                if (options.ReadyFile is null) throw new ArgumentException("--ready-file is required for validation output");
                File.WriteAllText(options.ReadyFile, System.Text.Json.JsonSerializer.Serialize(new { valid = true, url = options.Url.AbsoluteUri }));
            }
            catch { Environment.ExitCode = 2; }
            return;
        }
        ApplicationConfiguration.Initialize();
        try
        {
            var options = DesktopOptions.Parse(args);
            using var instance = new Mutex(true, "Local\\lay4flow-remote.Desktop." + options.InstanceKey, out var created);
            if (!created)
            {
                try
                {
                    using var pipe = new NamedPipeClientStream(".", "lay4flow-remote.Desktop." + options.InstanceKey, PipeDirection.Out);
                    pipe.Connect(1500);
                    pipe.WriteByte(1);
                }
                catch { MessageBox.Show("lay4flow-remote 已在运行，请从任务栏打开窗口。", "lay4flow-remote"); }
                return;
            }
            Application.Run(new MainForm(options));
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "lay4flow-remote 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
