# OpenCode NoLimits (v6.0) — Live Console · WARP + warp-plus

A Windows console tool that brings up a Cloudflare WARP tunnel, rotates the public IP until the
free model answers again, and — if WARP fails twice — falls back to **warp-plus** (auto-downloaded).
The window **never closes by itself** and every step is printed live.

---

## Behaviour (v6.0)

```text
 double-click ──► UAC ──► one console window, and it stays
                              │
   cycle #1  ┌────────────────┴─────────────────┐
             │ ONE WARP worker:                  │
             │  • prescreen endpoints (read-only)│
             │  • connect candidate 1 … 9        │
             │  • verify IP + quota (2 requests) │
             └────────────────┬─────────────────┘
                        ok ───┴─── no
                        │         │
              ACTIVATION DONE     cycle #2 (again, one worker)
              monitor every 12s        │
                                  still failing twice
                                       │
                                 warp-plus fallback
                                 (download once, then try
                                  warp → psiphon → gool)
```

* **No auto-close. No hidden work. No parallel tunnel attempts.**
* Cloudflare WARP is a *single* worker loop that keeps retrying until it succeeds.
  (`warp-cli` is a client of one Windows service with one tunnel — parallel attempts only
  fight each other and corrupt the registration.)
* After **2 failed WARP rounds** the tool downloads `warp-plus` (~7 MB, one time) and tries
  its three modes: normal WARP → WARP over Psiphon (US) → WARP-in-WARP.
  The first working SOCKS5 proxy stays alive; the others are killed immediately.
* Losing attempts are stopped; only the winner keeps running.
* `Ctrl+C` stops everything cleanly.

---

## Flags

```bat
WarpSetup.exe                    :: start, keep retrying, window stays open
WarpSetup.exe --stop             :: stop WARP + warp-plus, restore normal internet
WarpSetup.exe --no-fallback      :: WARP only (never download/use warp-plus)
WarpSetup.exe --probe-threads 1  :: strictly one probe worker (default 1)
WarpSetup.exe --warp-plus-port 8086
WarpSetup.exe --no-elevate       :: skip UAC (no service repair / registry cleanup)
WarpSetup.exe --cycles 2 --no-monitor   :: bounded run, useful for testing
```

---

## Where things live

| Item | Path |
| --- | --- |
| Last working endpoint / method | `%LOCALAPPDATA%\OpenCodeNoLimits\state.json` |
| warp-plus binary (auto-downloaded) | `%LOCALAPPDATA%\OpenCodeNoLimits\warp-plus\warp-plus.exe` |
| warp-plus logs (per mode) | `%LOCALAPPDATA%\OpenCodeNoLimits\warp-plus\warp-plus-*.log` |
| Diagnostics | `%TEMP%\warp_setup_diag.log` |

---

## Self-repair

If the daemon answers `Status update: Unable — Reason: Registration Missing`,
the registration is gone (usually a stuck `CloudflareWARP` service). When running as
Administrator the tool restarts the service once, recreates the registration and retries.
If it still fails, the WARP daemon itself cannot reach `api.cloudflareclient.com` — reinstall
Cloudflare WARP or check the daemon log in `C:\ProgramData\Cloudflare\cfwarp_service_log.txt`.

---

## Using the warp-plus proxy

warp-plus exposes a local SOCKS5 proxy (no system-wide tunnel). Point only the tools you want
at it:

```powershell
$env:HTTPS_PROXY="socks5://127.0.0.1:8086"
$env:HTTP_PROXY="socks5://127.0.0.1:8086"
opencode
```

---

## Build

```bat
build_exe.bat
```
or
```powershell
python -m pip install -r requirements.txt
python -m PyInstaller WarpSetup.spec
```
Output: `dist\WarpSetup.exe` (console + `uac-admin` manifest, single file).

---

## ملخص عربي

* ضغطة واحدة ← UAC ← نافذة واحدة **بتفضل مفتوحة** وبتحكي كل خطوة.
* كلاود فلير = **ثريد واحد** بيفضل يجرب لحد ما يظبط (مفيش محاولات متوازية).
* لو فشل دورتين ورا بعض ← بينزّل **warp-plus** ويجرب 3 أوضاع؛ أول واحد ينجح يفضل شغال والباقي يتوقف.
* أول ما ينجح: كارت `ACTIVATION DONE` + متابعة كل 12 ثانية، ولو الحصة رجعت يتدور IP تاني.
* `--stop` لإيقاف كل حاجة، و`Ctrl+C` بيوقف بأمان.
