#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode NoLimits (v5.0) - One-Click Activation
===============================================
What happens when you double-click the icon:

  1. UAC prompt (once) -> one terminal window opens automatically.
  2. THREE worker threads start together:
        [1] SCOUT   -> probes every WARP endpoint in parallel (3 probes at a time)
        [2] TUNNEL  -> owns warp-cli, connects to the fastest candidate first
        [3] VERIFY  -> fetches the new public IP + checks the free-model quota
  3. The FIRST candidate that passes verification wins. Losing attempts are
     abandoned immediately (no zombie workers, no extra windows).
  4. The terminal prints "ACTIVATION DONE", waits ~2 seconds and closes itself.
  5. Only the winning tunnel is left running (it lives inside the Windows
     service warp-svc, so closing the window does NOT kill it).

Why the old "3 threads = 3 tunnels" idea could never work:
    warp-cli is a thin client of ONE Windows service (warp-svc) that owns ONE
    tunnel. Three threads calling `warp-cli connect` with different endpoints
    fight over the same single tunnel: each one disconnects the previous one.
    Real parallelism is implemented where it is actually possible:
    endpoint prescreening, IP lookup and quota verification all run
    concurrently, while tunnel mutation stays serialized.

Usage:
    WarpSetup.exe              one-shot activation (default)
    WarpSetup.exe --stop       disconnect the tunnel and restore normal internet
    WarpSetup.exe --watch      keep the window open and auto-heal (legacy mode)
    WarpSetup.exe --no-close   keep the window open after activation
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

WARP_CLI_CANDIDATES = [
    r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
    os.path.expandvars(r"%ProgramFiles%\Cloudflare\Cloudflare WARP\warp-cli.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Cloudflare\Cloudflare WARP\warp-cli.exe"),
]

# None = Cloudflare default Anycast (usually the most reliable, keep it first).
ROTATION_ENDPOINTS = [
    None,
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

WORKERS = 3                 # prescreen threads ("3 threads" as requested)
CONNECT_TIMEOUT_SEC = 6.0   # hard cap for one handshake
PROBE_TIMEOUT_SEC = 1.2     # prescreen latency probe
DEFAULT_BUDGET_SEC = 120    # keep retrying for this long before giving up
MAX_ROUNDS = 2              # full passes over the endpoint list

TEMP_DIR = tempfile.gettempdir()
DIAG_LOG = os.path.join(TEMP_DIR, "warp_setup_diag.log")
PID_LOCK_FILE = os.path.join(TEMP_DIR, "opencode_nolimits.pid")

STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", TEMP_DIR), "OpenCodeNoLimits")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

SHELL_PROCESS_NAMES = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
                       "conhost.exe", "wt.exe", "bash.exe", "wsl.exe"}

# --------------------------------------------------------------------------- #
# Global runtime state
# --------------------------------------------------------------------------- #

_G_WARP_CLI = None
_G_ACTIVATED = False
_G_SHUTDOWN_LOCK = threading.Lock()
_G_SHUTDOWN_DONE = False
_G_PRINT_LOCK = threading.Lock()


def say(message: str = "") -> None:
    """Thread-safe console output."""
    with _G_PRINT_LOCK:
        try:
            print(message, flush=True)
        except Exception:
            pass


def log(message: str) -> None:
    try:
        with open(DIAG_LOG, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(message) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Windows helpers
# --------------------------------------------------------------------------- #

def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run(cmd, timeout=8):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=creation_flags())
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return 1, "error: %s" % exc


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def request_admin_elevation() -> bool:
    if is_admin():
        return True
    try:
        if getattr(sys, "frozen", False):
            target = sys.executable
            params = " ".join('"%s"' % arg for arg in sys.argv[1:])
        else:
            target = sys.executable
            params = '"%s" ' % os.path.abspath(sys.argv[0]) + \
                     " ".join('"%s"' % arg for arg in sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1)
        return int(rc) > 32
    except Exception as exc:
        log("Elevation error: %s" % exc)
        return False


def process_image_name(pid: int) -> str:
    try:
        code, out = run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"], timeout=3)
        line = out.strip().splitlines()[0]
        return line.split(",")[0].strip('"').lower()
    except Exception:
        return ""


def console_is_owned_by_us() -> bool:
    """True when this process created its own console (double-click launch)."""
    if sys.platform != "win32":
        return False
    try:
        buf = (ctypes.c_uint * 64)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buf, 64)
        if count <= 0:
            return False
        pids = [buf[i] for i in range(count)]
        others = [pid for pid in pids if pid != os.getpid()]
        for pid in others:
            if process_image_name(pid) in SHELL_PROCESS_NAMES:
                return False          # launched from an existing terminal: keep it
        return count <= 3             # our process (or PyInstaller parent+child) only
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Cloudflare WARP primitives
# --------------------------------------------------------------------------- #

def find_warp_cli():
    found = shutil.which("warp-cli")
    if found and os.path.isfile(found):
        return found
    for candidate in WARP_CLI_CANDIDATES:
        try:
            if candidate and os.path.isfile(candidate):
                return candidate
        except Exception:
            continue
    return None


WARP_SERVICE_NAMES = ["CloudflareWARP", "warp-svc", "WarpSvcRPC"]


def ensure_service_started() -> None:
    """The service is called CloudflareWARP on new builds and warp-svc on old ones."""
    for name in WARP_SERVICE_NAMES:
        code, out = run(["sc", "query", name], timeout=4)
        if code == 0 or "SERVICE_NAME" in out.upper():
            if "RUNNING" not in out.upper():
                run(["sc", "start", name], timeout=8)
                time.sleep(1.0)
            return
    log("WARP service not found by name, continuing anyway")


def warp_is_connected(warp_cli: str) -> bool:
    code, out = run([warp_cli, "status"], timeout=4)
    text = out.lower()
    if "disconnected" in text or "connecting" in text or "no connection" in text:
        return False
    return "connected" in text


def wait_for_connection_fast(warp_cli: str, timeout: float = CONNECT_TIMEOUT_SEC) -> bool:
    """Poll the tunnel + a real TCP handshake, exit as soon as it is usable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if warp_is_connected(warp_cli):
            try:
                socket.create_connection(("1.1.1.1", 443), timeout=2.0).close()
                return True
            except Exception:
                pass
        time.sleep(0.35)
    return False


def get_public_ip_fast():
    for url in IP_SERVICES:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "warp-setup/5.0"})
            with urllib.request.urlopen(request, timeout=3.0) as response:
                ip = response.read().decode("utf-8", "replace").strip()
            ip = ip.split()[0] if ip else ""
            if ip and ("." in ip or ":" in ip):
                return ip
        except Exception:
            continue
    return None


def execute_cycle(warp_cli: str, endpoint=None, delete_registration: bool = False) -> None:
    """Apply one endpoint + optional new identity, then connect (serialized)."""
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
    """Probe the free model endpoint.

    Returns (verdict, message) where verdict is:
        ok      -> HTTP 200, the model answered
        limited -> 429 / FreeUsageLimitError, this IP is out of quota
        unknown -> reachable but not verifiable without credentials
        error   -> the request never reached the API (bad tunnel)
    """
    url = "https://opencode.ai/inference/openai/v1/chat/completions"
    payload = {
        "model": "deepseek-v4.1-flash-free",
        "messages": [{"role": "user", "content": "ping"}],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "OpenCode/2.2.2"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return ("ok", "200 OK") if response.status == 200 else ("unknown", "status %s" % response.status)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")
        except Exception:
            pass
        if exc.code == 429 or "FreeUsageLimitError" in body or "usage limit" in body.lower():
            return "limited", "429 rate limit"
        if exc.code in (401, 403):
            return "unknown", "HTTP %s (endpoint reachable)" % exc.code
        return "error", "HTTP %s" % exc.code
    except Exception as exc:
        return "error", "network: %s" % exc


def disable_cloudflare_startup_and_tray() -> None:
    """Silence the tray app and stop Cloudflare from coming back on boot."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/IM", "Cloudflare WARP.exe"],
                       capture_output=True, creationflags=creation_flags(), timeout=2)
    except Exception:
        pass

    try:
        import winreg
    except Exception:
        return

    locations = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ]
    for root, key_path in locations:
        try:
            with winreg.OpenKey(root, key_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ) as key:
                for value_name in ("CloudflareWARP", "Cloudflare WARP", "CloudflareOne"):
                    try:
                        winreg.DeleteValue(key, value_name)
                        log("Removed auto-start entry: %s" % value_name)
                    except FileNotFoundError:
                        pass
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# State cache (so the 2nd click is nearly instant)
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(endpoint, ip) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"good_endpoint": endpoint, "ip": ip, "at": time.time()},
                      fh, indent=2)
    except Exception as exc:
        log("State save error: %s" % exc)


def build_candidates(state: dict):
    cached = state.get("good_endpoint", "__missing__")
    order = []
    if cached in ROTATION_ENDPOINTS:
        order.append(cached)
    for endpoint in ROTATION_ENDPOINTS:
        if endpoint not in order:
            order.append(endpoint)
    return order


# --------------------------------------------------------------------------- #
# Thread 1: SCOUT - parallel endpoint prescreening
# --------------------------------------------------------------------------- #

def probe_endpoint(endpoint, timeout: float = PROBE_TIMEOUT_SEC):
    """TCP RTT to the anycast edge. Returns seconds, or None if unreachable."""
    host = endpoint.split(":")[0] if endpoint else "162.159.192.1"
    started = time.perf_counter()
    try:
        sock = socket.create_connection((host, 443), timeout=timeout)
        sock.close()
        return time.perf_counter() - started
    except Exception:
        return None


def prescreen(candidates, workers: int = WORKERS):
    """Probe all candidates in parallel and return them fastest-first."""
    scores = {}
    say("  [1] Scout   : probing %d endpoints with %d parallel threads ..." % (len(candidates), workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_endpoint, endpoint): endpoint for endpoint in candidates}
        for future in futures:
            endpoint = futures[future]
            try:
                scores[endpoint] = future.result()
            except Exception:
                scores[endpoint] = None

    alive = [endpoint for endpoint in candidates if scores.get(endpoint) is not None]
    alive.sort(key=lambda endpoint: scores[endpoint])
    dead = [endpoint for endpoint in candidates if scores.get(endpoint) is None]

    for endpoint in alive:
        say("      + %-22s %4d ms" % (endpoint or "anycast (default)", int(scores[endpoint] * 1000)))
    for endpoint in dead:
        say("      - %-22s unreachable (queued last)" % (endpoint or "anycast (default)"))
    return alive + dead


# --------------------------------------------------------------------------- #
# Thread 3: VERIFY - IP + quota in parallel
# --------------------------------------------------------------------------- #

def verify_after_connect():
    """Run the IP lookup and the quota probe at the same time."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        ip_future = pool.submit(get_public_ip_fast)
        quota_future = pool.submit(verify_opencode_deepseek)
        try:
            ip = ip_future.result(timeout=8)
        except Exception:
            ip = None
        try:
            verdict, message = quota_future.result(timeout=8)
        except Exception:
            verdict, message = "error", "timeout"
    return ip, verdict, message


# --------------------------------------------------------------------------- #
# Thread 2: TUNNEL - one owner of warp-cli, fastest candidate first
# --------------------------------------------------------------------------- #

def attempt_endpoint(warp_cli, endpoint, attempt_no: int):
    label = endpoint or "anycast (default)"
    say("  [2] Tunnel  : attempt %d -> %s" % (attempt_no, label))

    execute_cycle(warp_cli, endpoint=endpoint, delete_registration=(attempt_no % 2 == 1))

    if not wait_for_connection_fast(warp_cli):
        say("      x no handshake via %s" % label)
        return None

    say("  [3] Verify  : tunnel up, checking IP + quota ...")
    ip, verdict, message = verify_after_connect()

    if verdict == "ok":
        say("      * quota OK on %s (IP %s)" % (label, ip or "?"))
        return {"endpoint": endpoint, "ip": ip, "status": "ok", "message": message}

    if verdict == "unknown" and ip:
        say("      ~ reachable but unverified (%s) - kept as fallback" % message)
        return {"endpoint": endpoint, "ip": ip, "status": "fallback", "message": message}

    say("      x %s rejected (%s)" % (ip or label, message))
    return None


def activate(warp_cli, budget: int = DEFAULT_BUDGET_SEC):
    """Keep trying until one candidate works. Returns the winning result dict."""
    candidates = build_candidates(load_state())
    ordered = prescreen(candidates)

    deadline = time.time() + budget
    fallback = None
    attempt_no = 0

    for round_index in range(MAX_ROUNDS):
        if round_index:
            say("\n  [*] Round %d: all candidates failed, retrying with fresh identities ..."
                % (round_index + 1))
            shuffled = list(ordered)
            random.shuffle(shuffled)
            ordered = shuffled

        for endpoint in ordered:
            if time.time() > deadline:
                say("\n  [!] Time budget (%ds) exhausted." % budget)
                break
            attempt_no += 1
            result = attempt_endpoint(warp_cli, endpoint, attempt_no)
            if result and result["status"] == "ok":
                return result
            if result and result["status"] == "fallback" and fallback is None:
                fallback = result

        if fallback:
            return fallback
        if time.time() > deadline:
            break

    return None


# --------------------------------------------------------------------------- #
# Teardown / stop
# --------------------------------------------------------------------------- #

def disconnect_tunnel() -> None:
    if not _G_WARP_CLI:
        return
    try:
        subprocess.run([_G_WARP_CLI, "--accept-tos", "disconnect"],
                       capture_output=True, timeout=5, creationflags=creation_flags())
    except Exception:
        pass


def on_exit() -> None:
    """Keep the winning tunnel alive; clean up only a half-finished attempt."""
    global _G_SHUTDOWN_DONE
    with _G_SHUTDOWN_LOCK:
        if _G_SHUTDOWN_DONE:
            return
        _G_SHUTDOWN_DONE = True

    if not _G_ACTIVATED:
        disconnect_tunnel()

    try:
        if os.path.isfile(PID_LOCK_FILE):
            os.remove(PID_LOCK_FILE)
    except Exception:
        pass


def setup_exit_handlers() -> None:
    atexit.register(on_exit)

    def _sig(_signum, _frame):
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _sig)
        except Exception:
            pass

    if sys.platform == "win32":
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _ctrl(_ctrl_type):
            on_exit()
            return True

        global _WIN_CTRL_HANDLER
        _WIN_CTRL_HANDLER = handler_type(_ctrl)
        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_WIN_CTRL_HANDLER, True)
        except Exception:
            pass


def enforce_single_instance() -> None:
    """One window only: a second click replaces the previous instance."""
    my_pid = os.getpid()
    try:
        parent_pid = os.getppid()
    except Exception:
        parent_pid = 0

    if os.path.isfile(PID_LOCK_FILE):
        try:
            with open(PID_LOCK_FILE, "r") as fh:
                recorded = fh.read().strip()
            if recorded.isdigit():
                old_pid = int(recorded)
                if old_pid not in (my_pid, parent_pid):
                    log("Killing previous instance %d" % old_pid)
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(old_pid)],
                                   capture_output=True, creationflags=creation_flags(), timeout=2)
        except Exception as exc:
            log("PID lock cleanup error: %s" % exc)

    try:
        listing = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WarpSetup.exe",
                                  "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True,
                                 creationflags=creation_flags(), timeout=3)
        for line in listing.stdout.strip().splitlines():
            parts = [col.strip('"') for col in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                pid = int(parts[1])
                if pid not in (my_pid, parent_pid):
                    log("Closing older WarpSetup window (PID %d)" % pid)
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, creationflags=creation_flags(), timeout=2)
    except Exception as exc:
        log("Process cleanup error: %s" % exc)

    record = parent_pid if (parent_pid > 0 and getattr(sys, "frozen", False)) else my_pid
    try:
        with open(PID_LOCK_FILE, "w") as fh:
            fh.write(str(record))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

def print_header() -> None:
    say("\n" + "+" + "-" * 62 + "+")
    say("|        OpenCode NoLimits v5.0 - One-Click Activation         |")
    say("|     3-thread pipeline :  Scout  ->  Tunnel  ->  Verify        |")
    say("+" + "-" * 62 + "+")


def print_done_card(result, window_will_close: bool) -> None:
    endpoint = result["endpoint"] or "anycast (default)"
    ip = result["ip"] or "unknown"
    verdict = "quota reset & verified" if result["status"] == "ok" else "tunnel ready (unverified)"
    say("\n+" + "-" * 62 + "+")
    say("|                     ACTIVATION DONE                          |")
    say("+" + "-" * 62 + "+")
    say("|  Status   : %-49s|" % verdict)
    say("|  New IP   : %-49s|" % ip)
    say("|  Endpoint : %-49s|" % endpoint)
    say("|  Protocol : %-49s|" % "WireGuard (full tunnel, low latency)")
    say("|  Tunnel   : %-49s|" % ("stays running after this window closes"
                                 if window_will_close else "stays running in background"))
    say("+" + "-" * 62 + "+")


def print_failure_card() -> None:
    say("\n+" + "-" * 62 + "+")
    say("|                 ACTIVATION FAILED - TUNNEL OFF               |")
    say("+" + "-" * 62 + "+")
    say("|  No endpoint passed verification, so the tunnel was closed   |")
    say("|  and your normal internet connection is restored.            |")
    say("|  Try again later, or use --watch for continuous retries.     |")
    say("+" + "-" * 62 + "+")


def auto_close_window(seconds: float = 2.0, enabled: bool = True) -> None:
    """Close the terminal window only if this process created it."""
    if not enabled:
        say("\n  (window stays open - --no-close / shared terminal)")
        return
    if not console_is_owned_by_us():
        say("\n  (shared terminal detected - window left open)")
        return
    for remaining in range(int(seconds), 0, -1):
        say("  Closing this window in %d ..." % remaining)
        time.sleep(1.0)


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def mode_stop() -> int:
    global _G_WARP_CLI
    _G_WARP_CLI = find_warp_cli()
    say("\n  Disconnecting Cloudflare WARP tunnel ...")
    disconnect_tunnel()
    disable_cloudflare_startup_and_tray()
    say("  [OK] Tunnel closed, normal internet restored.\n")
    return 0


def mode_watch(warp_cli, current=None) -> int:
    """Legacy sentinel: keep the window open and self-heal on limits."""
    say("\n  [WATCH] Window stays open. Ctrl+C or close it to stop monitoring.")
    if current is None:
        current = activate(warp_cli)
    while True:
        time.sleep(12)
        try:
            if not warp_is_connected(warp_cli):
                say("\n  [!] Tunnel dropped - reconnecting ...")
                current = activate(warp_cli, budget=60)
                continue
            verdict, message = verify_opencode_deepseek()
            if verdict == "limited":
                say("\n  [!] Quota reached (%s) - rotating IP ..." % message)
                current = activate(warp_cli, budget=60)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            log("Watch loop: %s" % exc)
    return 0


def mode_activate(warp_cli, args) -> int:
    global _G_ACTIVATED

    print_header()

    initial_ip = get_public_ip_fast()
    if initial_ip:
        say("  Current public IP : %s" % initial_ip)

    say("  Budget            : %ds (keeps trying until one passes)\n" % args.timeout)

    # Fast path: the tunnel may already be up from a previous click.
    result = None
    if warp_is_connected(warp_cli):
        say("  [*] Tunnel already connected - verifying current quota first ...")
        ip, verdict, message = verify_after_connect()
        if verdict == "ok":
            result = {"endpoint": load_state().get("good_endpoint"), "ip": ip,
                      "status": "ok", "message": message}
            say("      * current tunnel is already unlocked (IP %s)" % (ip or "?"))
        else:
            say("      ~ current tunnel is not usable (%s) - rotating" % message)

    if result is None:
        result = activate(warp_cli, budget=args.timeout)

    if not result:
        disconnect_tunnel()
        print_failure_card()
        _G_ACTIVATED = False
        if args.watch:
            return mode_watch(warp_cli)
        auto_close_window(4.0, enabled=not args.no_close)
        return 3

    save_state(result["endpoint"], result["ip"])
    _G_ACTIVATED = True
    print_done_card(result, window_will_close=not args.no_close)

    if args.watch:
        return mode_watch(warp_cli, result)

    auto_close_window(2.0, enabled=not args.no_close)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="WarpSetup",
        description="One-click Cloudflare WARP activation with 3-thread pipeline.",
    )
    parser.add_argument("--stop", action="store_true",
                        help="disconnect the tunnel and restore normal internet")
    parser.add_argument("--watch", action="store_true",
                        help="keep the window open and auto-heal on limits")
    parser.add_argument("--no-close", action="store_true",
                        help="do not close the terminal window after activation")
    parser.add_argument("--timeout", type=int, default=DEFAULT_BUDGET_SEC,
                        help="max seconds to keep trying (default %d)" % DEFAULT_BUDGET_SEC)
    return parser


def main(argv=None) -> int:
    global _G_WARP_CLI

    args = build_parser().parse_args(argv)

    if args.stop:
        if not is_admin() and not request_admin_elevation():
            say("[X] Administrator privileges are required.")
            return 1
        return mode_stop()

    if not is_admin():
        say("[!] Requesting Administrator privileges (UAC) ...")
        if request_admin_elevation():
            return 0
        say("[X] Administrator privileges are required to manage the tunnel.")
        time.sleep(4)
        return 1

    enforce_single_instance()
    disable_cloudflare_startup_and_tray()

    warp_cli = find_warp_cli()
    if not warp_cli:
        say("[X] warp-cli.exe not found - install Cloudflare WARP first.")
        say("    https://1.1.1.1/")
        time.sleep(6)
        return 2

    _G_WARP_CLI = warp_cli
    setup_exit_handlers()
    ensure_service_started()

    try:
        return mode_activate(warp_cli, args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log("Fatal: %s" % exc)
        say("\n[X] Unexpected error: %s" % exc)
        time.sleep(6)
        return 1


if __name__ == "__main__":
    sys.exit(main())
