"""Run the packaged Electron shell and its bundled WeFish service smoke test."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RELEASE = ROOT / "release"


def packaged_executable():
    if sys.platform == "win32":
        return RELEASE / "win-unpacked" / "上班墨鱼.exe", None
    if sys.platform == "darwin":
        apps = sorted(RELEASE.glob("mac*/上班墨鱼.app"))
        if not apps:
            raise SystemExit("no macOS WeFish app found under release/")
        return apps[0] / "Contents" / "MacOS" / "上班墨鱼", apps[0]
    raise SystemExit(f"packaged smoke requires macOS or Windows, got {sys.platform}")


def main() -> int:
    executable, app_path = packaged_executable()
    if not executable.exists():
        raise SystemExit(f"packaged executable missing: {executable}")
    with tempfile.TemporaryDirectory(prefix="wefish-package-smoke-") as temp:
        temp_root = Path(temp)
        if app_path is not None:
            # A downloaded unsigned app can retain a macOS provenance flag
            # that blocks Electron's framework loader. A normal copy mirrors
            # the clean workspace used by the macOS CI runner.
            staged_app = temp_root / app_path.name
            shutil.copytree(app_path, staged_app, symlinks=True)
            executable = staged_app / "Contents" / "MacOS" / "上班墨鱼"
        user_data = temp_root / "userdata"
        result = subprocess.run(
            [str(executable), "--user-data-dir", str(user_data), "--mock", "--self-test"],
            cwd=str(ROOT.parent),
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ),
        )
        output = (result.stdout or "") + (result.stderr or "")
        sys.stdout.write(output)
        if result.returncode != 0 or "WEFISH_SELF_TEST=ok" not in output:
            raise SystemExit(f"packaged smoke failed with exit code {result.returncode}")
    print("packaged-smoke-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
