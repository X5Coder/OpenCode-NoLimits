#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode NoLimits (v3.0) — High-Performance Background Tunnel & Limit Auto-Reset
================================================================================
- Automatic Administrator (UAC) elevation.
- Single-instance enforcement: closes previous instances/windows automatically.
- Permanently suppresses Cloudflare taskbar icon & disables Windows startup entries.
- Enforces WireGuard tunnel protocol (zero UDP packet loss, restores direct cable speed).
- Fast endpoint rotation and automated OpenCode DeepSeek free limit unlocking.
- Active Watcher (Continuous Listener): auto-heals and renews tunnel if it drops.
- Clean Teardown: closing the terminal/Ctrl+C gracefully terminates the tunnel.
- Preserves all user chats and projects completely intact.
"""

import atexit
import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

# Ensure UTF-8 output in Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

WARP_CLI_CANDIDATES = [
    r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
    os.path.expandvars(r"%ProgramFiles%\Cloudflare\Cloudflare WARP\warp-cli.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Cloudflare\Cloudflare WARP\warp-cli.exe"),
]

ROTATION_ENDPOINTS = [
    None,  # Default Anycast
    "162.159.192.1:2408",
    "162.159.193.1:2408",
    "162.159.195.1:2408",
    "188.114.96.1:2408",
    "188.114.97.1:2408",
    "188.114.98.1:2408",
    "188.114.99.1:2408",
    "162.159.198.2:2408",
]

IP_SERVICES = ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]
CONNECT_TIMEOUT_SEC = 8
DIAG_LOG = os.path.join(tempfile.gettempdir(), "warp_setup_diag.log")

# Global state for clean teardown
_G_WARP_CLI = None
_G_SHUTDOWN_TRIGGERED = False


def log(msg):
    try:
        with open(DIAG_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg) + "\n")
    except Exception:
        pass


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def request_admin_elevation():
    """Relaunch the current process with Administrator privileges if not elevated."""
    if is_admin():
        return True
    try:
        if getattr(sys, "frozen", False):
            target = sys.executable
            params = " ".join([f'"{arg}"' for arg in sys.argv[1:]])
        else:
            target = sys.executable
            params = f'"{os.path.abspath(sys.argv[0])}" ' + " ".join([f'"{arg}"' for arg in sys.argv[1:]])

        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1)
        if int(rc) > 32:
            sys.exit(0)
        return False
    except Exception as e:
        log(f"Elevation error: {e}")
        return False


def disable_cloudflare_startup_and_tray():
    """
    Remove Cloudflare WARP from Windows auto-startup and kill the tray process.
    Prevents Cloudflare from ever sitting in the taskbar or launching on boot.
    """
    # 1. Kill Cloudflare WARP tray GUI immediately
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.run(["taskkill", "/F", "/T", "/IM", "Cloudflare WARP.exe"],
                       capture_output=True, creationflags=creationflags, timeout=2)
    except Exception:
        pass

    # 2. Remove from Registry Auto-Run
    try:
        import winreg
        paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        ]
        for root, key_path in paths:
            try:
                with winreg.OpenKey(root, key_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ) as key:
                    for val_name in ["CloudflareWARP", "Cloudflare WARP", "CloudflareOne"]:
                        try:
                            winreg.DeleteValue(key, val_name)
                            log(f"Removed startup entry: {val_name}")
                        except FileNotFoundError:
                            pass
            except Exception:
                pass
    except Exception as e:
        log(f"Startup registry cleanup error: {e}")


def cleanup_old_instances():
    """Single-instance enforcement: close previous WarpSetup processes/windows."""
    my_pid = os.getpid()
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WarpSetup.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, creationflags=creationflags, timeout=3)
        for line in p.stdout.strip().splitlines():
            parts = [col.strip('"') for col in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                pid = int(parts[1])
                if pid != my_pid:
                    log(f"Terminating older WarpSetup instance (PID: {pid})")
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, creationflags=creationflags, timeout=2)
    except Exception as e:
        log(f"Instance cleanup error: {e}")


def run(cmd, timeout=10):
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=creationflags)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, f"error: {e}"


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


def ensure_service_started():
    code, out = run(["sc", "query", "warp-svc"], timeout=4)
    if "RUNNING" not in out.upper():
        run(["sc", "start", "warp-svc"], timeout=8)
        time.sleep(1)


def warp_is_connected(warp_cli):
    code, out = run([warp_cli, "status"], timeout=4)
    t = out.lower()
    return "connected" in t and "disconnected" not in t and "connecting" not in t.replace("connected", "")


def wait_for_connection_fast(warp_cli, timeout=CONNECT_TIMEOUT_SEC):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if warp_is_connected(warp_cli):
            try:
                socket.create_connection(("1.1.1.1", 443), timeout=2.0).close()
                return True
            except Exception:
                pass
        time.sleep(0.6)
    return False


def get_public_ip_fast():
    for url in IP_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "warp-setup/3.0"})
            with urllib.request.urlopen(req, timeout=3.0) as r:
                ip = r.read().decode("utf-8", "replace").strip()
                ip = ip.split()[0] if ip else ""
                if ip and ("." in ip or ":" in ip):
                    return ip
        except Exception:
            continue
    return None


def execute_cycle(warp_cli, endpoint=None, delete_registration=False):
    """Executes a silent, rapid renewal and reconnection cycle."""
    run([warp_cli, "--accept-tos", "disconnect"], timeout=4)

    if delete_registration:
        run([warp_cli, "--accept-tos", "registration", "delete"], timeout=6)
        run([warp_cli, "--accept-tos", "registration", "new"], timeout=8)

    run([warp_cli, "--accept-tos", "tunnel", "protocol", "set", "WireGuard"], timeout=4)
    run([warp_cli, "--accept-tos", "mode", "warp"], timeout=4)

    if endpoint:
        run([warp_cli, "--accept-tos", "tunnel", "endpoint", "set", endpoint], timeout=4)
    else:
        run([warp_cli, "--accept-tos", "tunnel", "endpoint", "reset"], timeout=3)

    run([warp_cli, "--accept-tos", "tunnel", "rotate-keys"], timeout=4)
    run([warp_cli, "--accept-tos", "connect"], timeout=6)


def verify_opencode_deepseek():
    """Probe OpenCode free inference endpoint for deepseek-v4.1-flash-free."""
    url = "https://opencode.ai/inference/openai/v1/chat/completions"
    data = {
        "model": "deepseek-v4.1-flash-free",
        "messages": [{"role": "user", "content": "ping"}]
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "OpenCode/2.2.2"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            if resp.status == 200:
                return True, "200 OK"
            return False, f"Status {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if e.code == 429 or "FreeUsageLimitError" in body:
            return False, "429 Rate Limit"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"Net: {e}"


def shutdown_tunnel():
    """Graceful teardown: disconnect tunnel so everything cleanly reverts upon exit."""
    global _G_SHUTDOWN_TRIGGERED, _G_WARP_CLI
    if _G_SHUTDOWN_TRIGGERED:
        return
    _G_SHUTDOWN_TRIGGERED = True

    print("\n\n" + "─" * 60)
    print("  [!] جاري إغلاق نفق الشبكة وإعادة الاتصال لحالته الطبيعية...")
    print("─" * 60)

    if _G_WARP_CLI:
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            subprocess.run([_G_WARP_CLI, "--accept-tos", "disconnect"],
                           capture_output=True, timeout=4, creationflags=creationflags)
        except Exception:
            pass

    disable_cloudflare_startup_and_tray()
    print("  [✓] تم إنهاء النفق بنجاح. وداعاً!\n")


def setup_exit_handlers():
    """Register exit handlers for Ctrl+C, window close button (X), and normal exit."""
    atexit.register(shutdown_tunnel)
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    if sys.platform == "win32":
        # Handle Windows Console Close button (X)
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def win_ctrl_handler(ctrl_type):
            # 0: CTRL_C, 2: CTRL_CLOSE, 5: LOGOFF, 6: SHUTDOWN
            shutdown_tunnel()
            return True

        global _win_handler_ref
        _win_handler_ref = PHANDLER_ROUTINE(win_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_win_handler_ref, True)


def run_renewal_routine(warp_cli):
    """Executes the IP rotation sequence until a working OpenCode quota is verified."""
    print("  [1/2] جاري تدوير الـ IP وفك الليمت عبر WireGuard السريع...", flush=True)

    for idx, ep in enumerate(ROTATION_ENDPOINTS):
        ep_label = ep if ep else "Anycast الأساسي"
        print(f"      • المحاولة {idx + 1}: تجديد النفق عبر ({ep_label})...", flush=True)

        delete_reg = (idx % 2 == 0)
        execute_cycle(warp_cli, endpoint=ep, delete_registration=delete_reg)

        connected = wait_for_connection_fast(warp_cli, timeout=CONNECT_TIMEOUT_SEC)
        if not connected:
            continue

        ip = get_public_ip_fast()
        if not ip:
            continue

        print(f"        ✓ تم الاتصال بـ IP: {ip} | فحص كوتة OpenCode...", flush=True)

        ok, msg = verify_opencode_deepseek()
        if ok:
            return True, ip

        print(f"        × الـ IP ({ip}) ما زال مقيداً، تدوير مباشر إلى سيرفر آخر...", flush=True)
        time.sleep(0.3)

    # Return current state if all cycles completed
    return False, get_public_ip_fast()


def print_success_card(ip):
    print("\n" + "╭" + "─" * 58 + "╮")
    print("│                     OPENCODE UNLOCKED                     │")
    print("├" + "─" * 58 + "┤")
    print(f"│  ✔ الحالة    : تم فك الليمت بنجاح والاتصال مستقر           │")
    print(f"│  ◈ الـ IP    : {ip:<40} │")
    print("│  ◈ البروتوكول: WireGuard (نفق فائق السرعة وبدون بطء)      │")
    print("│  ◈ الموديل   : DeepSeek جاهز للرد الفوري في OpenCode       │")
    print("│  ◈ المحادثات : جميع محادثاتك وسجلاتك محفوظة بالكامل       │")
    print("╰" + "─" * 58 + "╯\n")


def active_sentinel_loop(warp_cli, current_ip):
    """
    Continuous background monitor:
    Keeps the terminal active, listening and self-healing if connection drops or rate-limits.
    """
    print("╭" + "─" * 58 + "╮")
    print("│  [●] المراقبة النشطة قيد العمل (Active Sentinel Mode)     │")
    print("│  - إذا انقطع النفق أو انتهى الليمت سيتم التجديد تلقائياً  │")
    print("│  - للإيقاف وإلغاء النفق: أغلق النافذة أو اضغط Ctrl + C    │")
    print("╰" + "─" * 58 + "╯")

    fail_count = 0
    while True:
        try:
            time.sleep(12)

            # Check if still connected
            if not warp_is_connected(warp_cli):
                print(f"\n[!] تم رصد انقطاع النفق في {time.strftime('%H:%M:%S')} - جاري الإصلاح الذاتي...")
                success, new_ip = run_renewal_routine(warp_cli)
                if success:
                    current_ip = new_ip
                    print_success_card(current_ip)
                continue

            # Check OpenCode API status
            ok, msg = verify_opencode_deepseek()
            if not ok and "429" in msg:
                fail_count += 1
                if fail_count >= 2:
                    print(f"\n[!] تم استهلاك ليمت الـ IP ({current_ip}) - جاري تدوير الـ IP تلقائياً...")
                    success, new_ip = run_renewal_routine(warp_cli)
                    if success:
                        current_ip = new_ip
                        fail_count = 0
                        print_success_card(current_ip)
            else:
                fail_count = 0

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"Sentinel loop exception: {e}")
            time.sleep(5)


def main():
    global _G_WARP_CLI

    # 1. Administrator Check & Elevation
    if not is_admin():
        print("[!] جاري طلب صلاحيات المسؤول (Administrator)...")
        if not request_admin_elevation():
            print("[X] تعذر الحصول على صلاحيات المسؤول.")
            return 1
        return 0

    # 2. Cleanup old instances & disable Cloudflare tray / auto-start
    cleanup_old_instances()
    disable_cloudflare_startup_and_tray()

    warp_cli = find_warp_cli()
    if not warp_cli:
        print("[X] لم يتم العثور على warp-cli.exe. تأكد من تثبيت Cloudflare WARP.")
        return 2

    _G_WARP_CLI = warp_cli
    setup_exit_handlers()
    ensure_service_started()

    # Header Banner
    print("\n" + "╭" + "─" * 58 + "╮")
    print("│             OpenCode NoLimits — Ultra Engine             │")
    print("│       أداة كسر حدود OpenCode وتأمين النفق المستقر         │")
    print("╰" + "─" * 58 + "╯\n")

    initial_ip = get_public_ip_fast()
    if initial_ip:
        print(f"  [*] عنوان IP الحالي قبل التغيير: {initial_ip}\n")

    # 3. Execute renewal
    success, assigned_ip = run_renewal_routine(warp_cli)

    if success:
        print_success_card(assigned_ip)
    else:
        print(f"\n  [!] النفق متصل بـ IP: {assigned_ip or 'Active'} | جاري الدخول لوضع الحراسة...")

    # 4. Continuous listening / auto-healing sentinel
    active_sentinel_loop(warp_cli, assigned_ip)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as ex:
        log(f"Fatal error: {ex}")
        sys.exit(1)
