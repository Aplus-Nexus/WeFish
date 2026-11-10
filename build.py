import argparse, platform, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(); parser.add_argument("--target", choices=["current", "mac", "windows"], default="current")
args = parser.parse_args(); target = platform.system().lower() if args.target == "current" else args.target
name = "WeFish"
cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "WeFish.spec")]
if target not in {"darwin", "mac", "windows", "win"}:
    raise SystemExit("Use --target current, mac, or windows")
host = platform.system().lower()
if target in {"windows", "win"} and host != "windows":
    raise SystemExit("Windows packaging must run on a Windows host; use the CI workflow or run this command on Windows.")
if target in {"darwin", "mac"} and host != "darwin":
    raise SystemExit("macOS packaging must run on a macOS host; use the CI workflow or run this command on macOS.")
print("Packaging:", " ".join(cmd)); subprocess.run(cmd, cwd=ROOT, check=True)

if host == "darwin":
    bundle = ROOT / "dist" / "WeFish.app"
    archive_name = "WeFish-mac.zip"
else:
    bundle = ROOT / "dist" / "WeFish"
    archive_name = "WeFish-windows.zip"

if not bundle.exists():
    raise SystemExit(f"Packaging completed but expected output is missing: {bundle}")

archive = ROOT / "dist" / archive_name
if archive.exists():
    archive.unlink()
shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=bundle.parent, base_dir=bundle.name)
print("Artifact:", archive)

