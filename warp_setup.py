#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode NoLimits (v6.0) - Live Console, WARP + warp-plus Fallback
==================================================================
Design rules (as requested):

  * NOTHING closes by itself.  The window stays open and shows every step.
  * Cloudflare WARP runs as ONE worker thread that keeps retrying until the
    quota is really reset. No parallel tunnel attempts (warp-cli is a client
    of a single Windows service, parallel attempts would fight each other).
  * If WARP fails 2 rounds in a row, warp-plus is downloaded (once, cached)
    and tried as a fallback. If it works, its SOCKS5 proxy stays alive.
  * Losing attempts are stopped immediately; the winner is the only thing
    left running.
  * Ctrl+C stops everything cleanly.

Usage:
    WarpSetup.exe                 start and keep retrying (default)
    WarpSetup.exe --stop          stop WARP + warp-plus and restore internet
    WarpSetup.exe --no-fallback   never download/use warp-plus
    WarpSetup.exe --probe-threads 1
    WarpSetup.exe --no-elevate    run without UAC prompt (limited cleanup)
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

WARP_CLI_CANDIDATES = [
    r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
    os.path.expandvars(r"%ProgramFiles%\Cloudflare\Cloudflare WARP\warp-cli.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Cloudflare\Cloudflare WARP\warp-cli.exe"),
]

WARP_SERVICE_NAMES = ["CloudflareWARP", "warp-svc"]

# None = Cloudflare default Anycast (usually the most reliable).
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
OPENCODE_API = "https://opencode.ai/inference/openai/v1/chat/completions"
OPENCODE_MODEL = "deepseek-v4.1-flash-free"

PROBE_TIMEOUT_SEC = 1.2
CONNECT_TIMEOUT_SEC = 7.0
VERIFY_TIMEOUT_SEC = 6.0

# warp-plus fallback
WARP_PLUS_ZIP_URL = ("https://github.com/bepass-org/warp-plus/releases/latest/download/"
                     "warp-plus_windows-amd64.zip")
WARP_PLUS_PORT = 8086
WARP_PLUS_BOOT_TIMEOUT = 40.0

TEMP_DIR = tempfile.gettempdir()
DIAG_LOG = os.path.join(TEMP_DIR, "warp_setup_diag.log")
PID_LOCK_FILE = os.path.join(TEMP_DIR, "opencode_nolimits.pid")

STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", TEMP_DIR), "OpenCodeNoLimits")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
WARP_PLUS_DIR = os.path.join(STATE_DIR, "warp-plus")
WARP_PLUS_EXE = os.path.join(WARP_PLUS_DIR, "warp-plus.exe")
WARP_PLUS_CACHE = os.path.join(WARP_PLUS_DIR, "cache")

SHELL_PROCESS_NAMES = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
                       "conhost.exe", "wt.exe", "bash.exe", "wsl.exe"}

# --------------------------------------------------------------------------- #
# Global state
# --------------------------------------------------------------------------- #

_G_WARP_CLI = None
_G_WARP_PLUS_PROC = None
_G_KEEP_TUNNEL = False
_G_EXIT_LOCK = threading.Lock()
_G_EXIT_DONE = False
_G_PRINT_LOCK = threading.Lock()
_G_STOP = threading.Event()
_G_REPAIRED = False
_G_HINTED = False


def say(message: str = "") -> None:
    with _G_PRINT_LOCK:
        try:
            print(message, flush=True)
        except Exception:
            pass


def step(tag: str, message: str) -> None:
    say("  [%s] %-8s %s" % (time.strftime("%H:%M:%S"), tag, message))


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


def request_admin_elevation(argv) -> bool:
    if is_admin():
        return True
    try:
        if getattr(sys, "frozen", False):
            target, params = sys.executable, " ".join('"%s"' % a for a in argv)
        else:
            target = sys.executable
            params = '"%s" ' % os.path.abspath(sys.argv[0]) + " ".join('"%s"' % a for a in argv)
        return int(ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1)) > 32
    except Exception as exc:
        log("Elevation error: %s" % exc)
        return False


# --------------------------------------------------------------------------- #
# WARP primitives
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


def ensure_service_started() -> None:
    for name in WARP_SERVICE_NAMES:
        code, out = run(["sc", "query", name], timeout=4)
        if code == 0 or "SERVICE_NAME" in out.upper():
            if "RUNNING" not in out.upper():
                step("SVC", "starting service %s ..." % name)
                run(["sc", "start", name], timeout=10)
                time.sleep(1.5)
            return
    step("SVC", "WARP service not found (Cloudflare WARP not installed?)")


def repair_registration(warp_cli: str) -> str:
    """Registration missing is usually a stuck daemon: restart the service once."""
    step("WARP", "repair: registration missing -> restarting the WARP service ...")
    for name in WARP_SERVICE_NAMES:
        code, out = run(["sc", "query", name], timeout=4)
        if code == 0 or "SERVICE_NAME" in out.upper():
            run(["sc", "stop", name], timeout=20)
            time.sleep(2.0)
            run(["sc", "start", name], timeout=20)
            time.sleep(3.0)
            break
    run([warp_cli, "--accept-tos", "registration", "new"], timeout=12)
    status = warp_status_text(warp_cli)
    step("WARP", "repair result: %s" % status[:110])
    return status


def warp_status_text(warp_cli: str) -> str:
    code, out = run([warp_cli, "--accept-tos", "status"], timeout=5)
    return out.strip().replace("\n", " | ")


def warp_is_connected(warp_cli: str) -> bool:
    text = warp_status_text(warp_cli).lower()
    if "disconnected" in text or "connecting" in text or "unable" in text or "no connection" in text:
        return False
    return "connected" in text


def wait_for_connection_fast(warp_cli: str, timeout: float = CONNECT_TIMEOUT_SEC) -> bool:
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


def get_public_ip_fast(timeout: float = 3.0):
    for url in IP_SERVICES:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "warp-setup/6.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                ip = response.read().decode("utf-8", "replace").strip()
            ip = ip.split()[0] if ip else ""
            if ip and ("." in ip or ":" in ip):
                return ip
        except Exception:
            continue
    return None


def execute_cycle(warp_cli: str, endpoint=None, delete_registration: bool = False) -> None:
    run([warp_cli, "--accept-tos", "disconnect"], timeout=4)

    if delete_registration:
        run([warp_cli, "--accept-tos", "registration", "delete"], timeout=6)
        run([warp_cli, "--accept-tos", "registration", "new"], timeout=10)

    run([warp_cli, "--accept-tos", "tunnel", "protocol", "set", "WireGuard"], timeout=4)
    run([warp_cli, "--accept-tos", "mode", "warp"], timeout=4)

    if endpoint:
        run([warp_cli, "--accept-tos", "tunnel", "endpoint", "set", endpoint], timeout=4)
    else:
        run([warp_cli, "--accept-tos", "tunnel", "endpoint", "reset"], timeout=3)

    run([warp_cli, "--accept-tos", "tunnel", "rotate-keys"], timeout=4)
    run([warp_cli, "--accept-tos", "connect"], timeout=8)


def parse_verdict(status, body: str):
    if status == 200:
        return "ok", "200 OK"
    lowered = (body or "").lower()
    if status == 429 or "freeusagelimiterror" in lowered or "usage limit" in lowered:
        return "limited", "429 rate limit"
    if status in (401, 403):
        return "unknown", "HTTP %s (endpoint reachable)" % status
    return "error", "HTTP %s" % status


def verify_opencode_direct():
    payload = json.dumps({
        "model": OPENCODE_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")
    request = urllib.request.Request(
        OPENCODE_API, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "OpenCode/2.2.2"},
    )
    try:
        with urllib.request.urlopen(request, timeout=VERIFY_TIMEOUT_SEC) as response:
            body = response.read().decode("utf-8", "ignore")
            return parse_verdict(response.status, body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        return parse_verdict(exc.code, body)
    except Exception as exc:
        return "error", "network: %s" % exc


def verify_ip_and_quota(ip_getter=None):
    """IP lookup and quota probe run at the same time (two threads, read-only)."""
    ip_getter = ip_getter or get_public_ip_fast
    with ThreadPoolExecutor(max_workers=2) as pool:
        ip_future = pool.submit(ip_getter)
        quota_future = pool.submit(verify_opencode_direct)
        try:
            ip = ip_future.result(timeout=10)
        except Exception:
            ip = None
        try:
            verdict, message = quota_future.result(timeout=10)
        except Exception:
            verdict, message = "error", "timeout"
    return ip, verdict, message


def disable_cloudflare_tray() -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/IM", "Cloudflare WARP.exe"],
                       capture_output=True, creationflags=creation_flags(), timeout=2)
    except Exception:
        pass


def remove_cloudflare_autostart() -> None:
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
                    except FileNotFoundError:
                        pass
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Endpoint prescreening (read-only latency probe)
# --------------------------------------------------------------------------- #

def probe_endpoint(endpoint, timeout: float = PROBE_TIMEOUT_SEC):
    host = endpoint.split(":")[0] if endpoint else "162.159.192.1"
    started = time.perf_counter()
    try:
        sock = socket.create_connection((host, 443), timeout=timeout)
        sock.close()
        return time.perf_counter() - started
    except Exception:
        return None


def prescreen(candidates, workers: int = 3):
    scores = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(probe_endpoint, endpoint): endpoint for endpoint in candidates}
        for future in futures:
            endpoint = futures[future]
            try:
                scores[endpoint] = future.result()
            except Exception:
                scores[endpoint] = None

    alive = [e for e in candidates if scores.get(e) is not None]
    alive.sort(key=lambda e: scores[e])
    dead = [e for e in candidates if scores.get(e) is None]
    return alive + dead, scores


# --------------------------------------------------------------------------- #
# State cache
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(endpoint=None, ip=None, method=None, proxy=None) -> None:
    data = load_state()
    if endpoint is not None:
        data["good_endpoint"] = endpoint
    if ip is not None:
        data["ip"] = ip
    if method is not None:
        data["method"] = method
    if proxy is not None:
        data["proxy"] = proxy
    data["at"] = time.time()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
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
# SOCKS5 client (used to test / use warp-plus)
# --------------------------------------------------------------------------- #

class SocksError(Exception):
    pass


def socks5_connect(proxy_host: str, proxy_port: int, host: str, port: int, timeout: float = 10.0):
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        reply = sock.recv(2)
        if len(reply) != 2 or reply[0] != 5 or reply[1] != 0:
            raise SocksError("SOCKS5 handshake rejected")
        request = b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + port.to_bytes(2, "big")
        sock.sendall(request)
        head = sock.recv(4)
        if len(head) < 4 or head[1] != 0:
            raise SocksError("SOCKS5 connect failed (code %s)" % (head[1] if len(head) > 1 else "?"))
        atyp = head[3]
        if atyp == 1:
            sock.recv(4)
        elif atyp == 3:
            length = sock.recv(1)[0]
            sock.recv(length)
        elif atyp == 4:
            sock.recv(16)
        sock.recv(2)
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def socks5_http_request(proxy_host, proxy_port, method: str, url: str,
                        body: bytes = None, headers: dict = None, timeout: float = 10.0):
    """Minimal HTTPS client over a SOCKS5 proxy (no third-party dependency)."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    raw = socks5_connect(proxy_host, proxy_port, host, port, timeout)
    if parts.scheme == "https":
        context = ssl.create_default_context()
        raw = context.wrap_socket(raw, server_hostname=host)

    lines = ["%s %s HTTP/1.1" % (method, path), "Host: %s" % host,
             "User-Agent: OpenCode/2.2.2", "Connection: close"]
    for key, value in (headers or {}).items():
        lines.append("%s: %s" % (key, value))
    if body is not None:
        lines.append("Content-Length: %d" % len(body))
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + (body or b"")
    raw.sendall(request)

    chunks = []
    raw.settimeout(timeout)
    while True:
        try:
            chunk = raw.recv(65536)
        except (socket.timeout, ConnectionResetError, OSError):
            break
        if not chunk:
            break
        chunks.append(chunk)
    raw.close()

    response = b"".join(chunks)
    head, _, payload = response.partition(b"\r\n\r\n")
    head_text = head.decode("utf-8", "ignore")
    status = 0
    try:
        status = int(head_text.split(" ")[1])
    except Exception:
        pass
    return status, payload.decode("utf-8", "ignore")


def verify_through_proxy(proxy_host: str, proxy_port: int):
    ip = None
    for url in IP_SERVICES:
        try:
            status, body = socks5_http_request(proxy_host, proxy_port, "GET", url, timeout=8)
            candidate = (body or "").strip().split()[0] if body.strip() else ""
            if candidate and ("." in candidate or ":" in candidate):
                ip = candidate
                break
        except Exception:
            continue

    payload = json.dumps({
        "model": OPENCODE_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")
    try:
        status, body = socks5_http_request(
            proxy_host, proxy_port, "POST", OPENCODE_API, body=payload,
            headers={"Content-Type": "application/json"}, timeout=12)
        verdict, message = parse_verdict(status, body)
    except Exception as exc:
        verdict, message = "error", "proxy: %s" % exc
    return ip, verdict, message


# --------------------------------------------------------------------------- #
# warp-plus backend
# --------------------------------------------------------------------------- #

def find_warp_plus():
    for path in (WARP_PLUS_EXE, shutil.which("warp-plus")):
        if path and os.path.isfile(path):
            return path
    return None


def download_warp_plus():
    """Download + extract warp-plus once, with visible progress."""
    if find_warp_plus():
        return find_warp_plus()

    os.makedirs(WARP_PLUS_DIR, exist_ok=True)
    archive = os.path.join(WARP_PLUS_DIR, "warp-plus.zip")
    step("DL", "downloading warp-plus (one time, ~7 MB) ...")
    step("DL", "from %s" % WARP_PLUS_ZIP_URL)

    try:
        request = urllib.request.Request(WARP_PLUS_ZIP_URL, headers={"User-Agent": "warp-setup/6.0"})
        with urllib.request.urlopen(request, timeout=30) as response, open(archive, "wb") as fh:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_print = 0.0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if time.time() - last_print > 0.7:
                    last_print = time.time()
                    if total:
                        step("DL", "  %5.1f%%  (%.1f / %.1f MB)" % (
                            done * 100.0 / total, done / 1048576.0, total / 1048576.0))
                    else:
                        step("DL", "  %.1f MB" % (done / 1048576.0))
    except Exception as exc:
        step("DL", "download failed: %s" % exc)
        return None

    try:
        with zipfile.ZipFile(archive) as bundle:
            names = [n for n in bundle.namelist() if n.lower().endswith("warp-plus.exe")]
            if not names:
                step("DL", "archive does not contain warp-plus.exe")
                return None
            with bundle.open(names[0]) as src, open(WARP_PLUS_EXE, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.remove(archive)
    except Exception as exc:
        step("DL", "extract failed: %s" % exc)
        return None

    if os.path.isfile(WARP_PLUS_EXE):
        step("DL", "ready: %s" % WARP_PLUS_EXE)
        return WARP_PLUS_EXE
    return None


def stop_warp_plus() -> None:
    global _G_WARP_PLUS_PROC
    proc = _G_WARP_PLUS_PROC
    _G_WARP_PLUS_PROC = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    # kill leftovers started from our folder only
    try:
        listing = subprocess.run(["tasklist", "/FI", "IMAGENAME eq warp-plus.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True,
                                 creationflags=creation_flags(), timeout=3)
        for line in listing.stdout.strip().splitlines():
            parts = [c.strip('"') for c in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                subprocess.run(["taskkill", "/F", "/PID", parts[1]],
                               capture_output=True, creationflags=creation_flags(), timeout=3)
    except Exception:
        pass


def port_is_open(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=1.0).close()
        return True
    except Exception:
        return False


def start_warp_plus(exe: str, mode: str, port: int):
    """Launch warp-plus in one of its modes; returns the Popen object."""
    args = [exe, "-4", "-b", "127.0.0.1:%d" % port, "--cache-dir", WARP_PLUS_CACHE]
    if mode == "psiphon":
        args += ["--cfon", "--country", "US"]
    elif mode == "gool":
        args += ["--gool"]
    elif mode == "scan":
        args += ["--scan"]

    log_path = os.path.join(WARP_PLUS_DIR, "warp-plus-%s.log" % mode)
    try:
        handle = open(log_path, "w", encoding="utf-8", errors="replace")
    except Exception:
        handle = subprocess.DEVNULL
    creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=creation), log_path


def warp_plus_activate(exe: str, port: int = WARP_PLUS_PORT):
    """Try every warp-plus mode until one proxy really works."""
    global _G_WARP_PLUS_PROC

    modes = [("warp", "normal WARP"), ("psiphon", "WARP over Psiphon (US)"), ("gool", "WARP in WARP")]
    for mode, label in modes:
        if _G_STOP.is_set():
            return None
        step("W+", "trying mode: %s" % label)
        try:
            proc, log_path = start_warp_plus(exe, mode, port)
        except Exception as exc:
            step("W+", "cannot start: %s" % exc)
            continue
        _G_WARP_PLUS_PROC = proc

        deadline = time.time() + WARP_PLUS_BOOT_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                step("W+", "process exited early (code %s)" % proc.returncode)
                break
            if port_is_open("127.0.0.1", port):
                step("W+", "SOCKS5 is up on 127.0.0.1:%d - verifying ..." % port)
                ip, verdict, message = verify_through_proxy("127.0.0.1", port)
                if verdict == "ok":
                    step("W+", "SUCCESS - IP %s via proxy" % (ip or "?"))
                    return {"method": "warp-plus", "endpoint": mode, "ip": ip,
                            "proxy": "socks5://127.0.0.1:%d" % port, "status": "ok",
                            "message": message}
                if verdict == "unknown" and ip:
                    step("W+", "reachable but unverified (%s) - keeping as fallback" % message)
                    return {"method": "warp-plus", "endpoint": mode, "ip": ip,
                            "proxy": "socks5://127.0.0.1:%d" % port, "status": "fallback",
                            "message": message}
                step("W+", "rejected (%s: %s)" % (verdict, message))
                break
            time.sleep(1.0)
        else:
            step("W+", "timed out after %ds" % int(WARP_PLUS_BOOT_TIMEOUT))

        stop_warp_plus()
        try:
            tail = ""
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                tail = "".join(fh.readlines()[-3:]).strip()
            if tail:
                for line in tail.splitlines():
                    step("W+", "  log: %s" % line[:110])
        except Exception:
            pass

    stop_warp_plus()
    return None


# --------------------------------------------------------------------------- #
# Teardown
# --------------------------------------------------------------------------- #

def disconnect_tunnel() -> None:
    if _G_WARP_CLI:
        try:
            subprocess.run([_G_WARP_CLI, "--accept-tos", "disconnect"],
                           capture_output=True, timeout=5, creationflags=creation_flags())
        except Exception:
            pass


def on_exit() -> None:
    global _G_EXIT_DONE
    with _G_EXIT_LOCK:
        if _G_EXIT_DONE:
            return
        _G_EXIT_DONE = True
    if not _G_KEEP_TUNNEL:
        disconnect_tunnel()
        stop_warp_plus()
    try:
        if os.path.isfile(PID_LOCK_FILE):
            os.remove(PID_LOCK_FILE)
    except Exception:
        pass


def setup_exit_handlers() -> None:
    atexit.register(on_exit)

    def _sig(_signum, _frame):
        _G_STOP.set()
        on_exit()
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
        listing = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WarpSetup.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True,
                                 creationflags=creation_flags(), timeout=3)
        for line in listing.stdout.strip().splitlines():
            parts = [c.strip('"') for c in line.split(",")]
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
    say("")
    say("+" + "-" * 66 + "+")
    say("|      OpenCode NoLimits v6.0 - Live Console (nothing closes)      |")
    say("|      Cloudflare WARP  ->  fallback: warp-plus (if 2 fails)       |")
    say("+" + "-" * 66 + "+")
    say("")


def print_success_card(result) -> None:
    ip = result.get("ip") or "unknown"
    if result.get("method") == "warp-plus":
        where = result.get("proxy", "")
        protocol = "SOCKS5 proxy (warp-plus)"
    else:
        where = result.get("endpoint") or "anycast (default)"
        protocol = "WireGuard full tunnel (warp-cli)"
    status = "quota reset & verified" if result.get("status") == "ok" else "connected (unverified)"

    say("")
    say("+" + "-" * 66 + "+")
    say("|                        ACTIVATION DONE                           |")
    say("+" + "-" * 66 + "+")
    say("|  Status   : %-52s |" % status)
    say("|  New IP   : %-52s |" % ip)
    say("|  Via      : %-52s |" % where[:51])
    say("|  Protocol : %-52s |" % protocol)
    say("|  Mode     : %-52s |" % ("stays ON - this window stays open" if _G_KEEP_TUNNEL
                                 else "stays ON - window stays open"))
    say("+" + "-" * 66 + "+")
    say("")


def print_failed_round(warp_failures: int, next_action: str) -> None:
    say("")
    step("WARP", "round failed (consecutive failures: %d). Next: %s" % (warp_failures, next_action))
    say("")


def describe_admin() -> str:
    return "administrator" if is_admin() else "standard user"


# --------------------------------------------------------------------------- #
# Main workers
# --------------------------------------------------------------------------- #

def warp_round(warp_cli: str, workers: int = 1):
    """ONE worker: prescreen, then try candidates serially until one passes."""
    global _G_REPAIRED, _G_HINTED
    state = load_state()
    candidates = build_candidates(state)

    step("WARP", "prescreening %d endpoints (%d read-only probes) ..." % (len(candidates), workers))
    ordered, scores = prescreen(candidates, workers=workers)
    for endpoint in ordered[:4]:
        score = scores.get(endpoint)
        step("WARP", "  candidate %-22s %s" % (endpoint or "anycast (default)",
                                               "%4d ms" % int(score * 1000) if score else "unreachable"))

    attempt_no = 0
    for endpoint in ordered:
        if _G_STOP.is_set():
            return None
        attempt_no += 1
        label = endpoint or "anycast (default)"
        step("WARP", "attempt %d/%d -> %s" % (attempt_no, len(ordered), label))

        execute_cycle(warp_cli, endpoint=endpoint, delete_registration=(attempt_no % 2 == 1))

        if not wait_for_connection_fast(warp_cli):
            status = warp_status_text(warp_cli)
            step("WARP", "  no handshake (%s)" % status[:90])
            if "registration missing" in status.lower() and is_admin() and not _G_REPAIRED:
                _G_REPAIRED = True
                status = repair_registration(warp_cli)
                if "connected" in status.lower() or "disconnected" in status.lower():
                    step("WARP", "  retrying after repair ...")
                    execute_cycle(warp_cli, endpoint=endpoint, delete_registration=False)
                    if wait_for_connection_fast(warp_cli):
                        step("WARP", "  tunnel up after repair, verifying ...")
                        ip, verdict, message = verify_ip_and_quota()
                        if verdict in ("ok", "unknown") and ip:
                            return {"method": "warp", "endpoint": endpoint, "ip": ip,
                                    "status": "ok" if verdict == "ok" else "fallback",
                                    "message": message}
            elif "registration missing" in status.lower() and not is_admin() and not _G_HINTED:
                _G_HINTED = True
                step("WARP", "  hint: run as Administrator so the service can be repaired")
            continue

        step("WARP", "  tunnel up, verifying IP + quota ...")
        ip, verdict, message = verify_ip_and_quota()

        if verdict == "ok":
            step("WARP", "  OK - IP %s, quota available" % (ip or "?"))
            return {"method": "warp", "endpoint": endpoint, "ip": ip,
                    "status": "ok", "message": message}
        if verdict == "unknown" and ip:
            step("WARP", "  reachable but unverified (%s)" % message)
            return {"method": "warp", "endpoint": endpoint, "ip": ip,
                    "status": "fallback", "message": message}
        step("WARP", "  rejected - IP %s (%s)" % (ip or "?", message))

    return None


def monitor(warp_cli, result) -> None:
    """After success: keep watching, and re-activate when the quota comes back."""
    step("MON", "monitoring every 12s - Ctrl+C to stop")
    while not _G_STOP.is_set():
        time.sleep(12)
        if _G_STOP.is_set():
            return
        if result.get("method") == "warp":
            if not warp_is_connected(warp_cli):
                step("MON", "tunnel dropped - reactivating")
                return
            verdict, message = verify_opencode_direct()
        else:
            proxy = result.get("proxy", "")
            if not proxy:
                return
            host, port = proxy.replace("socks5://", "").rsplit(":", 1)
            try:
                _, verdict, message = verify_through_proxy(host, int(port))
            except Exception as exc:
                verdict, message = "error", str(exc)
        if verdict == "limited":
            step("MON", "quota reached again (%s) - rotating" % message)
            return
        if verdict == "error":
            step("MON", "verification error (%s)" % message)


def run_forever(warp_cli, args) -> int:
    global _G_KEEP_TUNNEL

    warp_failures = 0
    cycle = 0

    while not _G_STOP.is_set():
        if args.cycles and cycle >= args.cycles:
            step("CYCLE", "reached --cycles %d, stopping (tunnel stays as it is)" % args.cycles)
            return 0
        cycle += 1
        say("")
        say("-" * 68)
        step("CYCLE", "#%d  (%s, uptime %s)" % (cycle, describe_admin(),
                                                time.strftime("%H:%M:%S")))
        say("-" * 68)

        result = warp_round(warp_cli, workers=args.probe_threads)

        if result:
            _G_KEEP_TUNNEL = result.get("status") == "ok"
            save_state(endpoint=result.get("endpoint"), ip=result.get("ip"),
                       method=result.get("method"), proxy=result.get("proxy"))
            print_success_card(result)
            if args.no_monitor:
                return 0
            monitor(warp_cli, result)
            continue

        warp_failures += 1

        if args.no_fallback:
            print_failed_round(warp_failures, "retrying WARP")
            time.sleep(4)
            continue

        if warp_failures >= 2:
            say("")
            step("W+", "WARP failed %d rounds in a row -> switching to warp-plus" % warp_failures)
            exe = find_warp_plus() or download_warp_plus()
            if not exe:
                step("W+", "warp-plus unavailable, back to WARP")
                warp_failures = 0
                time.sleep(4)
                continue

            plus_result = warp_plus_activate(exe, args.warp_plus_port)
            if plus_result:
                _G_KEEP_TUNNEL = True
                save_state(method="warp-plus", ip=plus_result.get("ip"),
                           proxy=plus_result.get("proxy"), endpoint=plus_result.get("endpoint"))
                print_success_card(plus_result)
                if args.no_monitor:
                    return 0
                monitor(warp_cli, plus_result)
                continue

            step("W+", "all warp-plus modes failed")
            warp_failures = 0

        print_failed_round(warp_failures, "retrying WARP in 4s" if warp_failures else "warp-plus next")
        time.sleep(4)

    return 0


def mode_stop() -> int:
    global _G_WARP_CLI
    _G_WARP_CLI = find_warp_cli()
    say("")
    step("STOP", "disconnecting WARP tunnel ...")
    disconnect_tunnel()
    step("STOP", "stopping warp-plus ...")
    stop_warp_plus()
    disable_cloudflare_tray()
    try:
        if os.path.isfile(PID_LOCK_FILE):
            os.remove(PID_LOCK_FILE)
    except Exception:
        pass
    step("STOP", "done - normal internet restored")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="WarpSetup",
        description="Live, never-closing WARP activation with warp-plus fallback.",
    )
    parser.add_argument("--stop", action="store_true", help="stop tunnels and exit")
    parser.add_argument("--no-fallback", action="store_true", help="never use warp-plus")
    parser.add_argument("--no-elevate", action="store_true", help="do not ask for UAC")
    parser.add_argument("--probe-threads", type=int, default=1,
                        help="read-only latency probe threads; 1 = strictly one worker (default 1)")
    parser.add_argument("--warp-plus-port", type=int, default=WARP_PLUS_PORT,
                        help="local SOCKS5 port for warp-plus (default %d)" % WARP_PLUS_PORT)
    parser.add_argument("--cycles", type=int, default=0,
                        help="stop after N WARP cycles (0 = never stop, default)")
    parser.add_argument("--no-monitor", action="store_true",
                        help="do not keep watching after a success (useful for testing)")
    return parser


def main(argv=None) -> int:
    global _G_WARP_CLI, _G_KEEP_TUNNEL

    args = build_parser().parse_args(argv)

    if args.stop:
        return mode_stop()

    if not is_admin() and not args.no_elevate:
        step("ADM", "requesting Administrator privileges (UAC) ...")
        if request_admin_elevation(list(sys.argv[1:]) if argv is None else argv):
            return 0
        step("ADM", "continuing without elevation (limited cleanup)")

    enforce_single_instance()
    print_header()

    warp_cli = find_warp_cli()
    if not warp_cli:
        step("ERR", "warp-cli.exe not found - install Cloudflare WARP from https://1.1.1.1/")
        step("ERR", "warp-plus fallback can still be used, continuing ...")
    else:
        _G_WARP_CLI = warp_cli
        ensure_service_started()
        step("WARP", "status: %s" % warp_status_text(warp_cli)[:110])

    disable_cloudflare_tray()
    if is_admin():
        remove_cloudflare_autostart()

    setup_exit_handlers()

    initial_ip = get_public_ip_fast()
    step("NET", "current public IP: %s" % (initial_ip or "unknown"))
    step("NET", "window stays open until you stop it (Ctrl+C)")

    if not warp_cli:
        # no WARP client: go straight to warp-plus
        exe = find_warp_plus() or download_warp_plus()
        if exe:
            args.no_fallback = True  # avoid double handling
            result = warp_plus_activate(exe, args.warp_plus_port)
            if result:
                _G_KEEP_TUNNEL = True
                save_state(method="warp-plus", ip=result.get("ip"),
                           proxy=result.get("proxy"), endpoint=result.get("endpoint"))
                print_success_card(result)
                monitor(warp_cli, result)
                return 0
        step("ERR", "nothing to run. Install Cloudflare WARP or place warp-plus.exe next to this tool.")
        return 2

    return run_forever(warp_cli, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        on_exit()
        sys.exit(0)
    except Exception as exc:
        log("Fatal: %s" % exc)
        say("")
        step("ERR", "unexpected error: %s" % exc)
        step("ERR", "window stays open. Press Enter to close.")
        try:
            input()
        except Exception:
            time.sleep(10)
        sys.exit(1)
