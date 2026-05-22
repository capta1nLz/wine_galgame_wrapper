#!/usr/bin/env python3
"""
GalgameWineWrapper - Creates a self-contained macOS .app Wine wrapper for a visual novel.
Run this script and follow the prompts.
"""

import os
import sys
import subprocess
import shutil
import json
import plistlib
import struct
import tempfile
from pathlib import Path

# ---------- configuration ----------
WINE_RUNTIME_SOURCE = Path("/Applications/Wine Stable.app/Contents/Resources/wine")
DEFAULT_WIN_VERSION = "win10"
TEMPLATE_APP = None  # we build from scratch, no template needed

# ---------- helper functions ----------
def cmd(cmdline, env=None, cwd=None, timeout=None):
    """Run a command, print output, raise on error."""
    print(f"[CMD] {' '.join(cmdline)}")
    result = subprocess.run(cmdline, env=env, cwd=cwd,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        raise RuntimeError(f"Command failed: {cmdline}")
    return result

def choose_from_list(prompt, items, default_idx=0):
    """Show a numbered list and return the selected item."""
    print(f"\n{prompt}")
    for i, item in enumerate(items):
        print(f"  [{i}] {item}")
    while True:
        sel = input(f"Enter number (default {default_idx}): ").strip()
        if sel == "":
            return items[default_idx]
        if sel.isdigit() and 0 <= int(sel) < len(items):
            return items[int(sel)]
        print("Invalid choice.")

def scan_exes(folder):
    """Return list of .exe files relative to folder."""
    exes = []
    for root, dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".exe"):
                rel = os.path.relpath(os.path.join(root, f), folder)
                exes.append(rel)
    return exes

def is_64bit_pe(exe_path):
    """Check PE header machine type. Returns True if 64-bit, False if 32-bit."""
    with open(exe_path, "rb") as f:
        # DOS header: e_lfanew offset at 0x3C
        f.seek(0x3C)
        pe_offset = struct.unpack("<I", f.read(4))[0]
        f.seek(pe_offset + 4)  # Machine field
        machine = struct.unpack("<H", f.read(2))[0]
        # 0x8664 = AMD64, 0x014C = i386, others rare
        return machine == 0x8664

def clean_macos_metadata(path):
    """Remove macOS metadata files and xattrs that make codesign fail."""
    path = Path(path)
    for root, dirs, files in os.walk(path, topdown=False):
        root_path = Path(root)
        for name in files:
            if name == ".DS_Store" or name.startswith("._"):
                try:
                    (root_path / name).unlink()
                except OSError as e:
                    print(f"Warning: could not remove metadata file {root_path / name}: {e}")
        for name in dirs:
            if name == "__MACOSX":
                try:
                    shutil.rmtree(root_path / name)
                except OSError as e:
                    print(f"Warning: could not remove metadata directory {root_path / name}: {e}")

    subprocess.run(["xattr", "-cr", str(path)], check=False)
    subprocess.run(["dot_clean", "-m", str(path)], check=False)

# ---------- main wrapper creation ----------
def main():
    print("=" * 60)
    print("GalgameWineWrapper - Create macOS Wine Wrapper")
    print("=" * 60)

    # 1. Choose game folder
    game_folder = Path(input("Enter path to game folder: ").strip()).expanduser().resolve()
    if not game_folder.is_dir():
        sys.exit(f"Error: {game_folder} is not a valid directory.")
    if len(list(game_folder.glob("*.exe"))) == 0 and len(scan_exes(game_folder)) == 0:
        sys.exit("Error: No .exe files found in that folder.")

    # 2. Scan and pick launcher
    exes = scan_exes(game_folder)
    print(f"\nFound {len(exes)} executable(s):")
    if len(exes) == 0:
        sys.exit("No .exe files. Aborting.")
    if len(exes) == 1:
        launcher_rel = exes[0]
        print(f"Only one exe found, using: {launcher_rel}")
    else:
        launcher_rel = choose_from_list("Which exe is the main game launcher?", exes)
    launcher_full = game_folder / launcher_rel

    # 3. Detect PE architecture for prefix creation
    arch_64 = is_64bit_pe(launcher_full)
    # Always use win64 in wow64 mode (modern Wine on Apple Silicon)
    arch_str = "win64"
    print(f"\nDetected architecture: {arch_str}")

    # 4. Get display name for the .app
    app_name = input("\nEnter a name for the app (e.g. 'MyVN'): ").strip()
    if not app_name:
        sys.exit("App name cannot be empty.")
    # Sanitize: remove chars that are illegal in filenames
    safe_name = "".join(c for c in app_name if c.isalnum() or c in " ._-").strip()
    if safe_name != app_name:
        print(f"Using sanitised name: {safe_name}")
        app_name = safe_name
    app_bundle = Path(f"{app_name}.app").resolve()
    if app_bundle.exists():
        overwrite = input(f"{app_bundle} already exists. Replace it? [y/N]: ").strip().lower()
        if overwrite != "y":
            sys.exit("Aborting so the existing app is left unchanged.")
        print("Removing existing app bundle...")
        shutil.rmtree(app_bundle)

    # 5. Choose locale
    locales = {
        "1": ("Japanese", "ja_JP.UTF-8"),
        "2": ("Simplified Chinese", "zh_CN.UTF-8"),
        "3": ("None (system)", None)
    }
    print("\nSelect game language (for locale environment):")
    for key, (desc, _) in locales.items():
        print(f"  [{key}] {desc}")
    loc_choice = input("Choice (1/2/3, default 3): ").strip()
    if loc_choice == "":
        loc_choice = "3"
    locale_env = None
    if loc_choice in locales:
        locale_env = locales[loc_choice][1]
    if locale_env:
        print(f"Locale will be set to {locale_env}")
    else:
        print("No special locale will be set.")

    # 6. Choose Windows compatibility version
    win_versions = {
        "1": ("Windows 10", "win10"),
        "2": ("Windows 7", "win7"),
        "3": ("Windows XP", "winxp"),
    }
    print("\nSelect Wine Windows version:")
    for key, (desc, _) in win_versions.items():
        print(f"  [{key}] {desc}")
    win_choice = input("Choice (1/2/3, default 1): ").strip()
    if win_choice == "":
        win_choice = "1"
    win_version = win_versions.get(win_choice, win_versions["1"])[1]
    print(f"Windows version will be set to {win_version}.")

    # 7. Start building
    print("\nCreating wrapper bundle...")

    # Directory structure
    contents = app_bundle / "Contents"
    macos_dir = contents / "MacOS"
    resources = contents / "Resources"
    prefix_dir = resources / "prefix"
    game_dest = prefix_dir / "drive_c" / "Games" / safe_name
    wine_runtime_dest = resources / "wine-runtime"
    logs_dir = resources / "logs"

    for d in [macos_dir, resources, prefix_dir, game_dest, wine_runtime_dest, logs_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Copy entire game folder into drive_c/Games/GameName
    print("Copying game files...")
    # Copy contents, not the outer folder itself
    for item in game_folder.iterdir():
        dest = game_dest / item.name
        if item.is_dir():
            shutil.copytree(item, dest, symlinks=True, ignore_dangling_symlinks=True)
        else:
            shutil.copy2(item, dest)

    # Copy Wine runtime (bin, lib, share, etc.)
    print("Copying Wine runtime (this may take a while)...")
    # We copy everything except the 'wine' prefix (which is a symlink to /usr/local)
    # Instead, copy the actual 'wine' directory from WINE_RUNTIME_SOURCE.
    # WINE_RUNTIME_SOURCE is the folder containing bin/, lib/, share/, etc.
    # We copy the entire contents to wine_runtime_dest
    if not WINE_RUNTIME_SOURCE.exists():
        sys.exit(f"Wine runtime not found at {WINE_RUNTIME_SOURCE}. Install gcenx-wine first.")
    # Use rsync for efficiency
    subprocess.run(["rsync", "-a", "--delete",
                    f"{WINE_RUNTIME_SOURCE}/", f"{wine_runtime_dest}/"],
                   check=True)

    # Create Wine prefix and set Windows version
    print("Initialising Wine prefix...")
    winebin = wine_runtime_dest / "bin" / "wine"
    wine_env = os.environ.copy()
    wine_env["WINEPREFIX"] = str(prefix_dir)
    wine_env["WINEARCH"] = arch_str
    wine_env["WINEDEBUG"] = "-all"
    if locale_env:
        wine_env["LANG"] = locale_env
        wine_env["LC_ALL"] = locale_env
        wine_env["LC_CTYPE"] = locale_env

    # Run wineboot to create prefix
    cmd([str(winebin), "wineboot", "-u"], env=wine_env, timeout=120)

    # Set Windows version via registry
    reg_content = f"""Windows Registry Editor Version 5.00

[HKEY_CURRENT_USER\\Software\\Wine]
"Version"="{win_version}"
"""
    reg_file = tempfile.NamedTemporaryFile(mode="w", suffix=".reg", delete=False)
    reg_file.write(reg_content)
    reg_file.close()
    cmd([str(winebin), "regedit", reg_file.name], env=wine_env)
    os.unlink(reg_file.name)

    # Install CJK fonts and common fonts using winetricks
    print("Installing CJK fonts and core fonts...")
    # winetricks is expected in PATH
    winetricks_env = wine_env.copy()
    winetricks_env["WINE"] = str(winebin)
    try:
        subprocess.run(["winetricks", "cjkfonts", "corefonts"],
                       env=winetricks_env, check=True, timeout=300)
    except Exception as e:
        print(f"Warning: winetricks step failed (maybe it's not installed?): {e}")

    # Generate launcher script
    launcher_script = macos_dir / "launcher"
    launcher_rel_posix = launcher_rel.replace("\\", "/")
    launcher_dir_posix = str(Path(launcher_rel_posix).parent)
    if launcher_dir_posix == ".":
        launcher_dir_posix = ""
    game_working_dir = f'$RES/prefix/drive_c/Games/{safe_name}'
    game_working_dir_path = game_dest
    if launcher_dir_posix:
        game_working_dir = f'{game_working_dir}/{launcher_dir_posix}'
        game_working_dir_path = game_dest / launcher_dir_posix
    wine_drive_path = f"C:\\Games\\{safe_name}\\{launcher_rel_posix}".replace("/", "\\")
    locale_exports = ""
    if locale_env:
        locale_exports = (
            f'export LANG={locale_env}\n'
            f'export LC_ALL={locale_env}\n'
            f'export LC_CTYPE={locale_env}\n'
        )

    script_content = f'''#!/bin/bash
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RES="$APP_DIR/Resources"
LOG_DIR="$HOME/Library/Logs/{safe_name}"
LOG_FILE="$LOG_DIR/launcher.log"
WINE_LOG="$LOG_DIR/wine.log"

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

echo "===== launch $(date) ====="
echo "APP_DIR=$APP_DIR"
echo "RES=$RES"
echo "WINEPREFIX=$RES/prefix"
echo "GAME_DIR={game_working_dir}"
echo "EXE={wine_drive_path}"

export WINEPREFIX="$RES/prefix"
export WINEDEBUG="-all"
export PATH="$RES/wine-runtime/bin:$PATH"
export WINEDLLOVERRIDES="dxgi,d3d8,d3d9,d3d10core,d3d11=b"
export DXVK_LOG_LEVEL="none"
{locale_exports}
# Run the game
if [ ! -x "$RES/wine-runtime/bin/wine" ]; then
  echo "ERROR: Wine binary is missing or not executable: $RES/wine-runtime/bin/wine"
  exit 1
fi

if ! cd "{game_working_dir}"; then
  echo "ERROR: Could not cd into game directory: {game_working_dir}"
  exit 1
fi

echo "PWD=$(pwd)"
"$RES/wine-runtime/bin/wine" "{wine_drive_path}" >"$WINE_LOG" 2>&1
STATUS=$?
echo "Wine exited with status $STATUS"
echo "Wine log: $WINE_LOG"
exit "$STATUS"
'''
    launcher_script.write_text(script_content)
    launcher_script.chmod(0o755)

    # Generate Info.plist
    plist = {
        "CFBundleName": app_name,
        "CFBundleDisplayName": app_name,
        "CFBundleExecutable": "launcher",
        "CFBundleIdentifier": f"local.wrapper.{app_name.lower().replace(' ', '')}",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
    }
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump(plist, f)

    # Ad-hoc codesign to avoid launch issues
    print("Cleaning macOS metadata before codesign...")
    clean_macos_metadata(app_bundle)

    print("Performing ad-hoc codesign...")
    try:
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(app_bundle)], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Warning: codesign failed, but the wrapper was created: {e}")

    # Test launch (run for 10 seconds, capture log)
    print("\nRunning first-launch test (will close after 10 seconds)...")
    test_env = os.environ.copy()
    test_env["WINEPREFIX"] = str(prefix_dir)
    test_env["WINEDEBUG"] = "+loaddll,+seh,+tid,+timestamp"
    test_env["PATH"] = f"{wine_runtime_dest}/bin:{test_env['PATH']}"
    test_env["WINEDLLOVERRIDES"] = "dxgi,d3d8,d3d9,d3d10core,d3d11=b"
    test_env["DXVK_LOG_LEVEL"] = "none"
    if locale_env:
        test_env["LANG"] = locale_env
        test_env["LC_ALL"] = locale_env
        test_env["LC_CTYPE"] = locale_env

    log_file = logs_dir / "first_launch.log"
    try:
        with open(log_file, "w") as log:
            proc = subprocess.Popen(
                [str(winebin), wine_drive_path],
                env=test_env, stdout=log, stderr=subprocess.STDOUT,
                cwd=game_working_dir_path
            )
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait()
        print(f"Test completed. Log saved to {log_file}")
    except Exception as e:
        print(f"Test launch failed: {e}")

    print("\n" + "=" * 60)
    print(f"Wrapper created: {app_bundle}")
    print("You can now double-click it to play. Enjoy!")
    print("If the game doesn't start, check the log inside Resources/logs/")
    print("=" * 60)

if __name__ == "__main__":
    main()
