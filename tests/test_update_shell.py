"""Exercise the Linux host orchestration using offline Docker/curl fixtures."""

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "Linux shell check")
class UpdateShellTests(unittest.TestCase):
    def test_complete_transaction_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "installed"
            stage = base / "fixture-candidate"
            root.mkdir()
            (root / "scripts").mkdir()
            (root / "data").mkdir()
            (root / "docker-compose.yml").write_text("services: {}\n")
            (root / ".env").write_text("COMPOSE_PROJECT_NAME=fixture\n")
            (root / "scripts/update_runtime.py").write_text("# copied helper fixture\n")
            stage.mkdir()
            (stage / "VERSION").write_text("1.2.0\n")
            assets = base / "runtime.tar.gz"
            with tarfile.open(assets, "w:gz") as archive:
                content = b"1.2.0\n"
                info = tarfile.TarInfo("VERSION")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            checksums = base / "SHA256SUMS"
            checksums.write_text("fixture\n")
            bin_dir = base / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\n"
                "import json,os,pathlib,shutil,sys\n"
                "a=sys.argv[1:]; root=pathlib.Path(os.environ['FIXTURE_ROOT'])\n"
                "with open(os.environ['FIXTURE_LOG'],'a') as log: log.write(json.dumps(a)+'\\n')\n"
                "if a[:2]==['context','show']: print('fixture');sys.exit()\n"
                "if a[:1]==['--context']: a=a[2:]\n"
                "if a[:1]==['compose']:\n"
                " if 'ps' in a: print('fixture-container')\n"
                " elif '--images' in a: print('fixture:old')\n"
                " sys.exit()\n"
                "if a[:1]==['inspect']:\n"
                " print(str(root) if 'working_dir' in a[2] else 'fixture:old');sys.exit()\n"
                "if a[:1]==['build']: sys.exit()\n"
                "if a[:1]==['run']:\n"
                " if '-c' in a:\n"
                "  field=a[-1];print(json.load(sys.stdin).get(field,''));sys.exit()\n"
                " i=a.index('/update/update_runtime.py'); op=a[i+1];opts=a[i+2:]\n"
                " source=next(x.split('source=',1)[1].split(',target=',1)[0] for x in a if x.startswith('type=bind,source=') and x.endswith('target=/update'))\n"
                " if op=='prepare': shutil.copytree(os.environ['FIXTURE_CANDIDATE'],pathlib.Path(source)/'candidate')\n"
                " elif op=='snapshot':\n"
                "  p=root/opts[opts.index('--backup')+1].removeprefix('/deploy/');p.mkdir(parents=True);(p/'metadata.json').write_text('{}')\n"
                " print('{}');sys.exit()\n"
            )
            docker.chmod(0o755)
            curl = bin_dir / "curl"
            curl.write_text(
                "#!/usr/bin/env python3\nimport json,sys\n"
                "a=sys.argv[1:];url=a[-1]\n"
                "if url.endswith('/health'): print(json.dumps({'backend_running':True,'version':'1.2.0','update_active_requests':0,'update_pending_imports':0}))\n"
                "elif url.endswith('/v1/models'): print('401')\nelse: print('<html>fixture</html>')\n"
            )
            curl.chmod(0o755)
            env = dict(os.environ)
            env.update(
                PATH=str(bin_dir) + os.pathsep + env["PATH"],
                FIXTURE_ROOT=str(root),
                FIXTURE_CANDIDATE=str(stage),
                FIXTURE_LOG=str(base / "commands.jsonl"),
            )
            script = Path(__file__).resolve().parents[1] / "scripts/update.sh"
            command = [
                "bash",
                str(script),
                "--install-dir",
                str(root),
                "--version",
                "1.2.0",
                "--source-archive",
                str(assets),
                "--checksum-file",
                str(checksums),
                "--health-timeout",
                "2",
            ]
            completed = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("Update complete", completed.stdout)
            self.assertFalse((root / "data/update-maintenance").exists())
            (root / ".lay4flow-updates/update.lock.d").mkdir()
            completed = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("already running", completed.stderr)


if __name__ == "__main__":
    unittest.main()
