"""Build a standalone Windows bridge; run with the Python used for PyInstaller."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if sys.platform != "win32":
    raise SystemExit("The installer currently targets Windows only.")
subprocess.run([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
    "--name", "tokenmaxxing-engine", "--distpath", str(root / "build" / "engine"),
    "--workpath", str(root / "build" / "pyinstaller"),
    "--specpath", str(root / "build"), "--collect-all", "tzdata",
    str(root / "hud_bridge.py"),
], cwd=root, check=True)
engine = root / "build" / "engine" / "tokenmaxxing-engine" / "tokenmaxxing-engine.exe"
subprocess.run([str(engine), "--once", "hello"], cwd=root, check=True)
