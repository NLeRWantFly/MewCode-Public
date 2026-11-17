"""Run the offline browser scenarios with the repository's existing Electron."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    electron = root / 'client/node_modules/electron/dist/electron.exe'
    if not electron.is_file():
        raise SystemExit('Install the existing client dependencies before running this Windows harness.')
    env = dict(os.environ, NODE_PATH=str(root / 'client/node_modules'),
               MEWCODE_OFFLINE_TEST='1', PYTHONUTF8='1')
    env.pop('ELECTRON_RUN_AS_NODE', None)
    # Waiting for the GUI executable here also works from Windows PowerShell.
    result = subprocess.run([str(electron), 'scripts/run_workbench_browser.cjs'],
        cwd=root, env=env, capture_output=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
