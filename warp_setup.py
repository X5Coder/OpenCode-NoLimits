#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare WARP Auto Setup & IP Checker (Windows)."""
import ctypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

WARP_CLI_CANDIDATES = [
    r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
    os.path.expandvars(r"%ProgramFiles%\Cloudflare\Cloudflare WARP\warp-cli.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Cloudflare\Cloudflare WARP\warp-cli.exe"),
]
WARP_GUI_CANDIDATES = [
    r"C:\Program Files\Cloudflare\Cloudflare WARP\Cloudflare WARP.exe",
    os.path.expandvars(r"%ProgramFiles%\Cloudflare\Cloudflare WARP\Cloudflare WARP.exe"),
]
OFFICIAL_MSI_URLS = [
    "https://downloads.cloudflareclient.com/v1/download/windows/ga",
    "https://downloads.cloudflareclient.com/v1/download/windows/version/2026.7.1343.0",
]
OFFICIAL_MANUAL_URL = "https://one.one.one.one/"
CONNECT_TIMEOUT_SEC = 120
RETRY_SLEEP_SEC = 5
IP_SERVICES = ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]
HTTP_TIMEOUT = 15
DIAG_LOG = os.path.join(tempfile.gettempdir(), "warp_setup_diag.log")

def log(msg):
    line = "[warp-setup] " + msg
    print(line, flush=True)
    try:
        with open(DIAG_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass

def err(msg):
    log("ERROR: " + msg)

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "command not found"
    except Exception as e:
        return 1, "run error: %s" % e

def find_warp_cli():
    w = shutil.which("warp-cli")
    if w and os.path.isfile(w):
        return w
    for c in WARP_CLI_CANDIDATES:
        try:
            if c and os.path.isfile(c):
                return c
        except Exception:
            continue
    return None

def check_installed_registry():
    try:
        import winreg
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for root, key in roots:
            try:
                h = winreg.OpenKey(root, key)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(h, i)
                    i += 1
                except OSError:
                    break
                try:
                    sh = winreg.OpenKey(h, sub)
                    try:
                        name, _ = winreg.QueryValueEx(sh, "DisplayName")
                    except OSError:
                        continue
                    if name and ("cloudflare" in name.lower() and "warp" in name.lower()):
                        return True
                except OSError:
                    continue
    except Exception as e:
        log("registry check skipped: %s" % e)
    return False

def is_warp_installed():
    if find_warp_cli():
        return True
    for g in WARP_GUI_CANDIDATES:
        try:
            if g and os.path.isfile(g):
                return True
        except Exception:
            pass
    if check_installed_registry():
        return True
    return False

def download_official_msi(dest):
    log("Downloading official Cloudflare WARP MSI ...")
    last_err = None
    for url in OFFICIAL_MSI_URLS:
        log("URL: " + url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "warp-setup/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                total = r.headers.get("Content-Length")
                total = int(total) if total and total.isdigit() else 0
                done = 0
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        print("\r  ... %d%%" % pct, end="", flush=True)
                print()
            size = os.path.getsize(dest)
            log("Downloaded %d bytes -> %s" % (size, dest))
            if size < 1000000:
                log("File too small, trying next URL...")
                last_err = "file too small (%d bytes)" % size
                continue
            return dest
        except Exception as e:
            last_err = e
            log("URL failed: %s (%s), trying next..." % (url, e))
            continue
    raise RuntimeError("all official download URLs failed (last: %s)" % last_err)

def install_msi(msi_path):
    log("Launching official MSI installer (UAC prompt needs your consent)...")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "msiexec.exe", '/i "%s"' % msi_path, None, 1)
    if int(rc) <= 32:
        raise RuntimeError("UAC elevation refused (code %s)" % rc)
    log("Installer launched. Finish the official wizard, then press ENTER here...")
    try:
        input()
    except EOFError:
        time.sleep(10)

def open_warp_gui():
    """Open the WARP window (GUI panel) so the user can press Connect manually."""
    try:
        task = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Cloudflare WARP.exe"],
                              capture_output=True, text=True, timeout=15)
        if "Cloudflare WARP.exe" in (task.stdout or ""):
            log("WARP GUI already running - bringing window to front...")
            for g in WARP_GUI_CANDIDATES:
                if g and os.path.isfile(g):
                    try:
                        os.startfile(g)  # type: ignore[attr-defined]
                    except Exception:
                        subprocess.Popen([g])
                    break
            return True
        for g in WARP_GUI_CANDIDATES:
            if g and os.path.isfile(g):
                log("Opening WARP GUI window: %s" % g)
                subprocess.Popen([g])
                return True
        log("WARP GUI exe not found - user must open WARP manually.")
        return False
    except Exception as e:
        log("open GUI failed: %s" % e)
        return False

def ensure_warp_running(warp_cli):
    code, out = run(["sc", "query", "warp-svc"], timeout=15)
    log("service warp-svc query rc=%s" % code)
    if "RUNNING" not in out.upper():
        log("Starting warp-svc service (may need admin)...")
        run(["sc", "start", "warp-svc"], timeout=30)
        time.sleep(3)
    code, out = run([warp_cli, "status"], timeout=20)
    log("warp-cli status: %s" % out.strip()[:300])
    low = out.lower()
    if "unregistered" in low or ("registration" in low and "missing" in low):
        log("Registering WARP identity (official local registration)...")
        code2, out2 = run([warp_cli, "registration", "new"], timeout=60)
        log("registration rc=%s: %s" % (code2, out2.strip()[:300]))
    log("Requesting WARP connect (keeps your existing settings)...")
    code, out = run([warp_cli, "connect"], timeout=60)
    log("connect rc=%s: %s" % (code, out.strip()[:300]))
    try:
        task = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Cloudflare WARP.exe"],
                              capture_output=True, text=True, timeout=15)
        if "Cloudflare WARP.exe" not in task.stdout:
            for g in WARP_GUI_CANDIDATES:
                if g and os.path.isfile(g):
                    log("Opening WARP GUI (single instance)...")
                    subprocess.Popen([g])
                    break
        else:
            log("WARP GUI already running - not starting another copy.")
    except Exception as e:
        log("GUI check skipped: %s" % e)

def warp_is_connected(warp_cli):
    code, out = run([warp_cli, "status"], timeout=20)
    t = out.lower()
    noconn = t.replace("connected", "")
    return ("connect" in t and ("success" in t or "connected" in t)
            and "disconnected" not in t and "connecting" not in noconn)

def wait_for_connection(warp_cli, timeout=CONNECT_TIMEOUT_SEC):
    log("Waiting for WARP connection (retry %ds, max %ds)..." % (RETRY_SLEEP_SEC, timeout))
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if warp_is_connected(warp_cli):
            log("WARP reports: Connected.")
            try:
                socket.create_connection(("1.1.1.1", 443), timeout=8).close()
                log("Internet reachability confirmed (1.1.1.1:443).")
                return True
            except Exception as e:
                log("net check failed (try %d): %s" % (attempt, e))
        else:
            log("... not connected yet (try %d)" % attempt)
        if attempt % 6 == 0:
            run([warp_cli, "connect"], timeout=30)
        time.sleep(RETRY_SLEEP_SEC)
    return False

def get_public_ip():
    last_err = None
    for url in IP_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "warp-setup/1.0"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                ip = r.read().decode("utf-8", "replace").strip()
                ip = ip.split()[0] if ip else ""
                if ip and ("." in ip or ":" in ip):
                    return ip, url
                last_err = "bad response from %s" % url
        except Exception as e:
            last_err = "%s: %s" % (url, e)
    raise RuntimeError("could not obtain public IP (%s)" % last_err)

def main():
    print("=" * 60)
    print(" Cloudflare WARP Auto Setup & IP Checker (Windows)")
    print("=" * 60)
    log("diag log: %s" % DIAG_LOG)
    log("admin: %s" % is_admin())
    print("\n[0/5] Opening WARP window ...")
    print(">>> WARP window should pop up now - press Connect/ON there if needed. <<<")
    open_warp_gui()
    ip_before = None
    try:
        ip_before, src0 = get_public_ip()
        log("IP before WARP connect: %s (via %s)" % (ip_before, src0))
    except Exception as e:
        log("IP-before unavailable: %s" % e)
    print("\n[1/5] Checking Cloudflare WARP installation ...")
    if is_warp_installed():
        log("Cloudflare WARP is installed. Skipping download.")
    else:
        log("Cloudflare WARP NOT found. Official install required.")
        print("WARP is not installed. Download official installer from Cloudflare?")
        print("Continue? [Y/n]: ", end="", flush=True)
        try:
            ans = input().strip().lower()
        except EOFError:
            ans = "y"
        if ans not in ("", "y", "yes"):
            err("Install declined by user. Exiting safely.")
            return 2
        if not is_admin():
            log("NOTE: installer needs Administrator consent - accept UAC prompt.")
        msi = os.path.join(tempfile.gettempdir(), "Cloudflare_WARP_Release-x64.msi")
        try:
            if os.path.isfile(msi) and os.path.getsize(msi) > 1000000:
                log("Reusing existing MSI: %s" % msi)
            else:
                download_official_msi(msi)
        except Exception as e:
            err("Download failed: %s" % e)
            print("Download manually from %s and re-run." % OFFICIAL_MANUAL_URL)
            traceback.print_exc()
            return 3
        try:
            install_msi(msi)
        except Exception as e:
            err("Installer launch failed: %s" % e)
            traceback.print_exc()
            return 4
        time.sleep(3)
        if not is_warp_installed():
            err("WARP still not detected. Finish the wizard and re-run.")
            return 5
        log("Install verified.")
    warp_cli = find_warp_cli()
    if not warp_cli:
        err("warp-cli.exe not found. Open WARP once manually, then re-run.")
        return 6
    print("\n[2/5] Starting Cloudflare WARP ...")
    try:
        ensure_warp_running(warp_cli)
    except Exception as e:
        err("Could not start WARP: %s" % e)
        traceback.print_exc()
        return 7
    print("\n[3/5] Waiting for WARP connection ...")
    if not wait_for_connection(warp_cli):
        err("WARP did not connect within %d seconds." % CONNECT_TIMEOUT_SEC)
        print("Open WARP, make sure it is toggled ON, then re-run.")
        return 8
    print("\n[4/5] Getting public IP ...")
    try:
        ip_after, src = get_public_ip()
    except Exception as e:
        err("Connected but IP lookup failed: %s" % e)
        return 9
    print("")
    print("*** WARP connected successfully! Your public IP: %s ***" % ip_after)
    log("IP after connect: %s (via %s)" % (ip_after, src))
    print("\n[5/5] IP change check ...")
    if ip_before and ip_after:
        if ip_before != ip_after:
            print("IP changed: %s -> %s (WARP route active)" % (ip_before, ip_after))
        else:
            print("IP is the same as before (%s). No change claimed." % ip_after)
    else:
        print("IP-before unavailable; current public IP: %s" % ip_after)
    print("\nDone. NOTE: this tool only sets up connection + IP check;")
    print("it does NOT renew any OpenCode quota.")
    log("finished OK")
    return 0

if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        code = 130
    except Exception:
        traceback.print_exc()
        code = 99
    print("\n[Press ENTER to close]")
    try:
        input()
    except Exception:
        pass
    sys.exit(code)
