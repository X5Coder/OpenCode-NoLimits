# OpenCode NoLimits (v4.0) — Ultra Background Tunnel & Limit Reset Engine

> A high-performance, background utility for Windows that optimizes Cloudflare WARP with the WireGuard protocol, rotates Anycast endpoints to renew public IPs, and resets OpenCode free usage limits (e.g. DeepSeek) with an active self-healing monitor (Sentinel Mode).

---

## What's New in v4.0

* **100% English Terminal Interface:**
  * Clean, universally compatible ASCII/Unicode cards and status indicators without RTL/font distortion in Windows consoles.
* **Strict Single-Instance Enforcement:**
  * Clicking the executable icon multiple times automatically closes the previous instance and window, ensuring exactly one active monitor window.
* **Console Keep-Alive (No Disappearing Window):**
  * Fixed process parent tracking so PyInstaller bootloader is never killed; the window remains open and visible in front of you.
* **Ultra-Minimalist Dark Mode Icon:**
  * Clean, bold cyan infinity loop over a rounded obsidian disc, optimized for high-DPI screens and Windows 11 taskbars. Provided in `icon.svg`, `icon.png`, and `icon.ico`.
* **Active Sentinel Mode (Self-Healing Background Monitor):**
  * Continuously monitors tunnel health every 12 seconds. If packet loss occurs or OpenCode reaches a rate-limit, it automatically rotates to a clean IP in the background without user intervention.
* **Graceful Tunnel Teardown:**
  * Closing the terminal window (X) or pressing `Ctrl + C` immediately disconnects the tunnel and restores normal internet connectivity.
* **Suppresses Cloudflare Tray & Startup:**
  * Prevents Cloudflare from auto-starting on Windows boot and hides the tray application completely.
* **Preserves OpenCode History:**
  * Never touches or deletes your previous chats, projects, or local databases.

---

## How to Use

1. Download `WarpSetup.exe` from the [Latest Release](https://github.com/X5Coder/OpenCode-NoLimits/releases/latest).
2. Run `WarpSetup.exe` (accept the one-time UAC prompt).
3. The tool will configure WireGuard, rotate to an unlocked IP, and display:
   ```text
   ╭────────────────────────────────────────────────────────────╮
   │                     OPENCODE UNLOCKED                      │
   ├────────────────────────────────────────────────────────────┤
   │  ✔ Status    : Limit reset successful & tunnel active      │
   │  ◈ New IP    : 188.114.97.xxx                              │
   │  ◈ Protocol  : WireGuard (Ultra-low latency, full tunnel)  │
   │  ◈ OpenCode  : DeepSeek free model is ready to respond     │
   │  ◈ History   : All previous chats & projects preserved     │
   ╰────────────────────────────────────────────────────────────╯
   ```
4. Keep the window open while working in OpenCode for automatic self-healing. When finished, simply close the window to disconnect the tunnel.

---

## Build from Source

```bat
build_exe.bat
```
Or directly using Python:
```powershell
python -m pip install -r requirements.txt
python -m PyInstaller WarpSetup.spec
```
Binary output: `dist\WarpSetup.exe`
