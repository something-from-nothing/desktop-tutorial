"""Run OS: try an operating system in a window on Windows, without touching Windows.

Double click RunOS.exe to run Desk OS: it downloads Desk OS itself (and any newer
version later), or offline uses DeskOS.zip or its pieces in Downloads. Drag any .zip, .bin,
.img or .iso onto it, or pick one when it asks. The first time, it installs QEMU (a
free program that runs a computer inside a window) into the user's own folder.
"""
import ctypes
import os
import re
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "RunOS"
QEMU_DIR = HOME / "qemu"
QEMU = QEMU_DIR / "qemu-system-x86_64.exe"
QEMU_SITE = "https://qemu.weilnetz.de/w64/"
DESK_OS_URL = "https://github.com/something-from-nothing/desktop-tutorial/releases/download/os-latest/DeskOS.zip"
IMAGES = (".bin", ".img", ".iso", ".raw")


def say(text):
    print("\n" + text, flush=True)


def get_qemu():
    if QEMU.exists():
        return
    say("First time: getting QEMU (about 150 MB). This happens once.")
    page = urllib.request.urlopen(QEMU_SITE, timeout=60).read().decode("utf-8", "replace")
    names = sorted(set(re.findall(r"qemu-w64-setup-\d{8}\.exe", page)))
    if not names:
        raise SystemExit("Couldn't find the QEMU download. Check the internet connection.")
    setup = HOME / names[-1]
    urllib.request.urlretrieve(QEMU_SITE + names[-1], setup)
    say("Installing QEMU. If Windows asks 'Do you want to allow this app to make changes', click Yes.")
    # The installer needs admin rights, so Windows asks first. /D must come last, unquoted.
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Start-Process -FilePath '{setup}' -ArgumentList '/S','/D={QEMU_DIR}' -Verb RunAs -Wait"], check=False)
    if not QEMU.exists():
        raise SystemExit("QEMU didn't install. Run this again and click Yes when Windows asks.")


def pick_file():
    """The OS to run: what was dropped on the EXE, else DeskOS.zip in Downloads, else ask."""
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        return Path(sys.argv[1])
    # The downloaded Desk OS comes first, so a newer version always wins over an old
    # copy left in Downloads; that copy (or its pieces) is only used offline.
    fetched = fetch_desk_os()
    if fetched:
        return fetched
    downloads = Path.home() / "Downloads"
    desk = downloads / "DeskOS.zip"
    if not desk.exists():
        join_pieces(downloads)
    if desk.exists():
        return desk
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    chosen = filedialog.askopenfilename(title="Choose an OS to run",
                                        filetypes=[("OS files", "*.zip *.iso *.img *.bin *.raw"), ("All files", "*.*")])
    root.destroy()
    if not chosen:
        raise SystemExit("No OS chosen.")
    return Path(chosen)


def fetch_desk_os():
    """Downloads Desk OS the first time, and again whenever a newer build is out (the
    size changes with every build). Returns its path, or None if it can't be reached."""
    target = HOME / "DeskOS.zip"
    try:
        with urllib.request.urlopen(urllib.request.Request(DESK_OS_URL, method="HEAD"), timeout=60) as r:
            size = int(r.headers.get("Content-Length") or 0)
    except Exception as e:
        if target.exists():
            return target  # offline: use the copy we have
        if "404" in str(e):
            raise SystemExit("The Desk OS download isn't public yet. Make the GitHub project public, then run this again.")
        say(f"Couldn't reach the Desk OS download ({e}).")
        return None
    if target.exists() and target.stat().st_size == size:
        return target
    say("Downloading Desk OS (about 1.8 GB). This happens once, and again only when there's a new version."
        if not target.exists() else "A newer Desk OS is out. Downloading it (about 1.8 GB).")
    part = HOME / "DeskOS.zip.download"
    with urllib.request.urlopen(DESK_OS_URL, timeout=60) as r, open(part, "wb") as out:
        done, shown = 0, -1
        while chunk := r.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            pct = done * 100 // size if size else 0
            if pct != shown:
                print(f"\r  {pct}%  ({done // 2**20} of {size // 2**20} MB)", end="", flush=True)
                shown = pct
    print()
    if size and part.stat().st_size != size:
        part.unlink()
        raise SystemExit("The download stopped part way. Run this again to retry.")
    part.replace(target)
    return target


def join_pieces(folder):
    """Desk OS sent in a chat comes as DeskOS.zip.001, .002 and so on, because chats
    limit file size. Put them back together into DeskOS.zip."""
    pieces = sorted(folder.glob("DeskOS.zip.[0-9][0-9][0-9]"))
    if not pieces:
        return
    numbers = [int(p.suffix[1:]) for p in pieces]
    if numbers != list(range(1, len(numbers) + 1)):
        missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
        raise SystemExit("Some Desk OS pieces are missing from Downloads: "
                         + ", ".join(f"DeskOS.zip.{n:03d}" for n in missing))
    say(f"Joining the {len(pieces)} Desk OS pieces into DeskOS.zip.")
    target = folder / "DeskOS.zip"
    part = folder / "DeskOS.zip.joining"
    with open(part, "wb") as out:
        for piece in pieces:
            with open(piece, "rb") as f:
                while chunk := f.read(1 << 22):
                    out.write(chunk)
    try:
        with zipfile.ZipFile(part) as z:
            z.namelist()
    except zipfile.BadZipFile:
        part.unlink()
        raise SystemExit("The pieces didn't join into a working file. One of them didn't download fully: "
                         "delete the DeskOS.zip pieces in Downloads and download them again.")
    part.rename(target)
    for piece in pieces:
        piece.unlink()
    say("Joined. The pieces have been tidied away.")


def disk_from_zip(path):
    """A .zip (like DeskOS.zip) holds a disk image: unpack it once and reuse it after,
    so changes made inside the OS are kept. A newer zip gets a fresh copy."""
    out = HOME / f"disk-{path.stem}-{int(path.stat().st_mtime)}"
    if not out.exists():
        for old in HOME.glob(f"disk-{path.stem}-*"):
            for f in old.iterdir():
                f.unlink()
            old.rmdir()
        say(f"Unpacking {path.name}. This takes a few minutes the first time.")
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
    found = [f for f in out.rglob("*") if f.suffix.lower() in IMAGES]
    if not found:
        raise SystemExit("That zip has no disk image inside.")
    return found[0]


def memory_mb():
    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong),
                    ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("ve", ctypes.c_ulonglong)]
    s = Status()
    s.length = ctypes.sizeof(Status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return min(4096, s.total // 2**20 // 2)


def main():
    HOME.mkdir(parents=True, exist_ok=True)
    os.system("title Run OS")
    get_qemu()
    share = QEMU_DIR / "share"
    code, vars_template = share / "edk2-x86_64-code.fd", share / "edk2-i386-vars.fd"
    if "--check" in sys.argv:  # used by the build to prove QEMU installs and runs
        print(subprocess.run([str(QEMU), "--version"], capture_output=True, text=True).stdout)
        print("firmware:", code.exists(), vars_template.exists())
        return
    path = pick_file().resolve()
    if path.suffix.lower() == ".zip":
        path = disk_from_zip(path)
    vars_ = HOME / "efi-vars.fd"
    if not vars_.exists():
        vars_.write_bytes(vars_template.read_bytes())
    args = [str(QEMU),
            "-accel", "whpx,kernel-irqchip=off", "-accel", "tcg",  # fast if Windows' hypervisor is on
            "-machine", "q35", "-m", str(memory_mb()), "-smp", "2",
            "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
            "-drive", f"if=pflash,format=raw,file={vars_}",
            "-device", "virtio-vga", "-display", "sdl",
            "-device", "qemu-xhci", "-device", "usb-tablet", "-device", "usb-kbd",
            "-nic", "user,model=virtio-net-pci",
            "-name", f"Run OS: {path.name}"]
    if path.suffix.lower() == ".iso":
        args += ["-cdrom", str(path)]
    else:  # like a USB stick plugged in
        args += ["-drive", f"file={path},format=raw,if=none,id=stick", "-device", "usb-storage,drive=stick"]
    say(f"Starting {path.name} in a window.")
    print("Click inside the window to use it. Ctrl + Alt + G lets go of the mouse.")
    print("Close the window to switch it off. Your changes are kept for next time.")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        say("It stopped with this message:\n" + (result.stderr or result.stdout)[-1500:])


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            say(str(e.code))
    except Exception as e:
        say(f"Something went wrong: {e}")
    if "--check" not in sys.argv:
        input("\nPress Enter to close.")
