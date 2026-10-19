"""Regression checks for portable, safer public-release defaults."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicDefaultsTests(unittest.TestCase):
    def test_server_defaults_to_loopback(self):
        env = {key: value for key, value in os.environ.items()
               if key not in {'MCP_HOST', 'MCP_PORT', 'MCP_PATH', 'CDP_HOST', 'CDP_PORT'}}
        result = subprocess.run(
            [sys.executable, '-c',
             "import runpy; config=runpy.run_path('src/server.py'); print(config['MCP_HOST'])"],
            cwd=ROOT, env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), '127.0.0.1')

    def test_sensitive_artifact_ignore_rules_present(self):
        rules = set((ROOT / '.gitignore').read_text().splitlines())
        self.assertTrue({'p.txt', 'pat.txt', '.runtime/', '.venv/', '*.log', '*.zip', '*.csv'} <= rules)

    def test_wsl_launcher_delegates_path_discovery(self):
        launcher = (ROOT / 'scripts/launch-edge.sh').read_text()
        self.assertIn('launch-edge.ps1', launcher)
        self.assertIn('wslpath', launcher)
        self.assertNotIn('/mnt/c/Program Files', launcher)
        self.assertNotIn('C:\\Users\\', launcher)
        self.assertNotIn('--remote-debugging-address=0.0.0.0', launcher)


if __name__ == '__main__':
    unittest.main()
