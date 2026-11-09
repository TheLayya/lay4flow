"""Compile the actual native launch planner; no browser or network is opened."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Native Windows browser launch planner")
class DesktopLoginBrowserTests(unittest.TestCase):
    def test_fixed_loopback_target_arguments_and_supported_browser_selection(self):
        dotnet = Path(os.environ["LOCALAPPDATA"]) / "TkCRM-Dev/dotnet/dotnet.exe"
        if not dotnet.is_file():
            self.skipTest(".NET 10 SDK unavailable")
        with tempfile.TemporaryDirectory(prefix="lay4flow-login-browser-") as directory:
            target = Path(directory)
            for name in ("LoginBrowser.cs", "DesktopOptions.cs"):
                shutil.copyfile(ROOT / "desktop/Lay4Flow.Desktop" / name, target / name)
            (target / "check.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                "<OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework>"
                "<ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )
            (target / "Program.cs").write_text(
                r"""
using Lay4Flow.Desktop;
static void Check(bool value) { if (!value) throw new Exception("launch contract failed"); }
var options = DesktopOptions.Parse(["--url", "http://127.0.0.1:38177/login", "--no-start"]);
var chrome = LoginBrowser.StartInfo(options, path => path.EndsWith("chrome.exe"));
Check(chrome.FileName.EndsWith("chrome.exe"));
Check(!chrome.UseShellExecute);
Check(chrome.ArgumentList.Count == 2);
Check(chrome.ArgumentList[0] == "--new-window");
Check(chrome.ArgumentList[1] == "http://127.0.0.1:38177/#/accounts");
var edge = LoginBrowser.StartInfo(options, path => path.EndsWith("msedge.exe"));
Check(edge.FileName.EndsWith("msedge.exe"));
try { LoginBrowser.StartInfo(options, _ => false); throw new Exception("missing browser accepted"); }
catch (InvalidOperationException) { }
try { DesktopOptions.Parse(["--url", "https://evil.invalid/"]); throw new Exception("foreign origin accepted"); }
catch (ArgumentException) { }
Console.WriteLine("PASS native login browser launch plan");
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(dotnet), "run", "--project", str(target / "check.csproj"), "-c", "Release"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS native login browser launch plan", result.stdout)
