"""Discover arbitrary system-proxy ports without changing the real registry."""

import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows proxy discovery")
class WindowsNetworkTests(unittest.TestCase):
    def test_system_proxy_and_process_listener_do_not_assume_a_port(self):
        powershell = shutil.which("powershell.exe")
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            script = fixture / "check.ps1"
            script.write_text(
                r"""param([string]$RepoRoot)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/network.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Network script did not parse.' }
$definition = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-ProxyCandidates'}, $false)
Invoke-Expression $definition.Extent.Text
function Resolve-SystemProxy { 'DIRECT' }
function Get-ItemProperty { [pscustomobject]@{ProxyEnable=1;ProxyServer='http=127.0.0.1:18732;https=127.0.0.1:28111;socks=127.0.0.1:31555'} }
function Get-Process { @([pscustomobject]@{ProcessName='verge-mihomo';Id=43210}) }
function Get-NetTCPConnection { @([pscustomobject]@{OwningProcess=43210;LocalAddress='::';LocalPort=49231}) }
$values = Get-ProxyCandidates
foreach ($expected in @('http://127.0.0.1:18732','http://127.0.0.1:28111','socks5://127.0.0.1:31555','http://127.0.0.1:49231','socks5://127.0.0.1:49231')) {
  if ($expected -notin $values) { throw ('Missing detected proxy: ' + $expected) }
}
function Get-ItemProperty { [pscustomobject]@{ProxyEnable=0;AutoConfigURL='https://pac.example.invalid/proxy.pac'} }
$values = @(Get-ProxyCandidates)
if ($values.Count) { throw 'A PAC DIRECT route was replaced with a detected listener.' }
function Resolve-SystemProxy { 'http://proxy.example.invalid:52341/' }
$values = @(Get-ProxyCandidates)
if ($values[0] -ne 'http://proxy.example.invalid:52341/') { throw 'PAC proxy did not take priority.' }
function Resolve-SystemProxy { 'DIRECT' }
function Get-ItemProperty { [pscustomobject]@{ProxyEnable=0} }
function Get-NetTCPConnection { @([pscustomobject]@{OwningProcess=43210;LocalAddress='::1';LocalPort=49231}, [pscustomobject]@{OwningProcess=43210;LocalAddress='192.0.2.10';LocalPort=18251}) }
$values = @(Get-ProxyCandidates)
foreach ($expected in @('http://[::1]:49231','socks5://[::1]:49231','http://192.0.2.10:18251')) {
  if ($expected -notin $values) { throw ('Missing listener address: ' + $expected) }
}
""",
                encoding="utf-8-sig",
            )
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-RepoRoot",
                    str(ROOT),
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_handshake_probes_use_complete_http_and_socks_replies(self):
        def read_exact(connection, count):
            result = bytearray()
            while len(result) < count:
                value = connection.recv(count - len(result))
                if not value:
                    break
                result.extend(value)
            return bytes(result)

        def respond(listener, kind):
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    if kind.startswith("http"):
                        request = bytearray()
                        while b"\r\n\r\n" not in request:
                            request.extend(connection.recv(512))
                        if kind == "http-fragmented":
                            for fragment in (
                                b"HT",
                                b"TP/1.1 200 Connection",
                                b" Established\r\n\r\n",
                            ):
                                connection.sendall(fragment)
                                time.sleep(0.03)
                        elif kind == "http-truncated":
                            connection.sendall(b"HTTP/1.1 200 OK\r\n")
                        else:
                            connection.sendall(
                                b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
                            )
                    else:
                        if read_exact(connection, 3) != b"\x05\x01\x00":
                            return
                        connection.sendall(b"\x05\x00")
                        header = read_exact(connection, 5)
                        if len(header) != 5:
                            return
                        read_exact(connection, header[4] + 2)
                        connection.sendall(b"\x05\x00\x00\x01")
                        if kind == "socks-complete":
                            connection.sendall(b"\x00\x00\x00\x00\x00\x00")
            finally:
                listener.close()

        listeners = []
        threads = []
        cases = (
            ("http-fragmented", "http", True),
            ("http-truncated", "http", False),
            ("http-denied", "http", False),
            ("socks-complete", "socks5", True),
            ("socks-truncated", "socks5", False),
        )
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "probe.ps1"
            code = r"""param([string]$RepoRoot)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/network.ps1'), [ref]$tokens, [ref]$errors)
foreach ($name in @('Read-ProxyBytes', 'Test-HttpProxy')) {
  $definition = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $false)
  Invoke-Expression $definition.Extent.Text
}
"""
            for kind, scheme, expected in cases:
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listeners.append(listener)
                thread = threading.Thread(target=respond, args=(listener, kind), daemon=True)
                threads.append(thread)
                thread.start()
                url = f"{scheme}://127.0.0.1:{listener.getsockname()[1]}"
                code += f"if ((Test-HttpProxy ([Uri]'{url}')) -ne ${str(expected).lower()}) {{ throw '{kind}' }}\n"
            script.write_text(code, encoding="utf-8-sig")
            try:
                result = subprocess.run(
                    [
                        shutil.which("powershell.exe"),
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(script),
                        "-RepoRoot",
                        str(ROOT),
                    ],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                for listener in listeners:
                    listener.close()
                for thread in threads:
                    thread.join(timeout=1)

    def test_explicit_manual_mode_preserves_other_environment_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            env_file = fixture / ".env"
            env_file.write_text(
                "COMPOSE_PROJECT_NAME=fixture\n"
                "LAY4FLOW_REMOTE_PORT=49836\n"
                "LAY4FLOW_HOST_PROXY_MODE=manual\n"
                "LAY4FLOW_HOST_PROXY=socks5://host.docker.internal:19683\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    shutil.which("powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/windows/network.ps1"),
                    "-InstallDir",
                    str(fixture),
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("COMPOSE_PROJECT_NAME=fixture", env_file.read_text(encoding="utf-8"))
            self.assertIn(
                "LAY4FLOW_HOST_PROXY=socks5://host.docker.internal:19683",
                env_file.read_text(encoding="utf-8"),
            )

    def test_invalid_manual_proxy_fails_before_environment_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            env_file = fixture / ".env"
            for proxy in (
                "",
                "file:///fixture",
                "socks5://host.docker.internal",
                "http://127.0.0.1:0",
                "http://127.0.0.1:1080/path",
            ):
                with self.subTest(proxy=proxy):
                    original = f"LAY4FLOW_HOST_PROXY_MODE=manual\nLAY4FLOW_HOST_PROXY={proxy}\n"
                    env_file.write_text(original, encoding="utf-8")
                    result = subprocess.run(
                        [
                            shutil.which("powershell.exe"),
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(ROOT / "scripts/windows/network.ps1"),
                            "-InstallDir",
                            str(fixture),
                        ],
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=15,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(env_file.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
