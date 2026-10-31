# OpenCode NoLimits (v5.0) — One-Click WARP Activation

A one-shot Windows utility that brings up a Cloudflare WARP tunnel (WireGuard), rotates the public IP until the free model answers again, then **closes its own terminal window** — the winning tunnel stays running in the background.

---

## The flow (exactly what happens on a double-click)

```text
 click icon ──► UAC (once) ──► terminal opens
                                   │
        ┌──────────────────────────┴──────────────────────────┐
        │  [1] SCOUT   : probe all 9 endpoints, 3 at a time    │  ≈0.3 s
        │  [2] TUNNEL  : connect to the fastest one first      │  ≈2–6 s
        │  [3] VERIFY  : public IP + quota check in parallel   │  ≈1–3 s
        └──────────────────────────┬──────────────────────────┘
                                   │ first candidate that passes wins
                       ┌───────────┴───────────┐
                    passed                 nothing passed
                       │                       │
        "ACTIVATION DONE" card          "ACTIVATION FAILED" card
        tunnel stays ON                 tunnel closed, internet restored
        window closes after 2 s         window closes after 4 s
```

* Losing attempts are abandoned immediately — **only the successful configuration stays up**.
* The tunnel lives inside the Windows service `warp-svc`, so it survives the window closing.
* Second run is nearly instant: the last working endpoint is cached in
  `%LOCALAPPDATA%\OpenCodeNoLimits\state.json` and tried first.
* A "fast path" also checks the tunnel that is *already* connected before touching anything.

---

## Why the previous "3 threads = 3 tunnels" idea could not work

`warp-cli.exe` is only a thin client for **one** Windows service (`warp-svc`) that owns **one** tunnel.
Three threads calling `warp-cli connect` with three different endpoints fight over the same tunnel —
every connect disconnects the previous one, so the last thread always wins and the results of the
others are meaningless. Worse, `registration delete/new` in parallel can corrupt the registration state.

So v5.0 keeps **one** owner thread for the tunnel and moves parallelism where it is real and useful:

| Work | Threads | Why it is parallelizable |
| --- | --- | --- |
| Endpoint latency prescreening (TCP to `:443`) | 3 (pool) | reads only, no state |
| Public IP lookup + quota probe | 2 (pool) | independent HTTP requests |
| Tunnel connect / registration rotation | **1** (serialized) | single global service |

---

## Usage

```bat
WarpSetup.exe               :: one-shot activation (default)
WarpSetup.exe --stop        :: disconnect + restore normal internet
WarpSetup.exe --watch       :: keep the window open, auto-heal on limit (old Sentinel)
WarpSetup.exe --no-close    :: do not auto-close the window
WarpSetup.exe --timeout 60  :: keep retrying for 60 s instead of 120 s
```

Requirements: Cloudflare WARP installed (`warp-cli.exe`), Windows 10/11, Administrator (UAC).

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
Output: `dist\WarpSetup.exe` (console + `uac-admin` manifest).

---

## Free / faster alternatives (honest comparison)

| Tool | Free? | Speed | Multi-tunnel / rotation | Notes for this use case |
| --- | --- | --- | --- | --- |
| **Cloudflare WARP + `warp-cli`** (this tool) | yes | fast (WireGuard) | 1 tunnel, rotate endpoint/identity | easiest, already installed, but the whole system is tunneled |
| **[warp-plus](https://github.com/bepass-org/warp-plus)** | yes | fast | **yes** — several SOCKS5 proxies on different ports, `--scan` for best endpoint | the only realistic way to get *real* parallel tunnels; use with `HTTPS_PROXY` |
| **[wgcf](https://github.com/ViRb3/wgcf) + [wireproxy](https://github.com/pufferffish/wireproxy)** | yes | very fast | **yes** — one lightweight proxy process per config | no admin (no TUN adapter), smallest footprint, ideal for API traffic only |
| **sing-box / Hiddify / NekoBox** | yes | fast | yes (multiple outbounds + selector) | GUI/JSON config, best for routing only certain apps |
| **Psiphon** | yes | medium | rotates exits automatically | no config, but slower and shared IPs (often already rate-limited) |
| **Tor** | yes | slow | rotates with NEWNYM | works for HTTP APIs but latency kills streaming/large prompts |
| **ProtonVPN / Windscribe free** | yes | fast | few exits, no CLI rotation | good privacy, poor for fast rotation |
| **Just use another free model/provider** | yes | fastest | n/a | see below — usually the *real* answer |

### The bigger picture

IP rotation only helps when the provider limits **per IP**. If the limit is bound to your
**account/API key**, a VPN changes nothing — and rotating IPs to keep using a free tier after
its limit is both slower and a terms-of-service violation (accounts can get flagged).

Cheaper/simpler alternatives that actually solve "the free quota keeps running out":

* Use the other free models already available in OpenCode (switch model, no network tricks).
* Mix providers: a small paid top-up (e.g. DeepSeek/Groq/OpenRouter) costs pennies per million tokens.
* Run a local model with [Ollama](https://ollama.com) + `qwen2.5-coder` / `deepseek-coder` — zero quota, zero network, fully offline.
* Keep WARP **only** for what needs it; a tunnel for the whole machine slows everything else down.

---

## ملخص عربي سريع

* ضغطة واحدة على الأيقونة ← موافقة UAC ← نافذة تيرمينال واحدة.
* 3 ثريدات تعمل معًا: فحص السرعة لكل نقطة (Scout) / الاتصال (Tunnel) / التحقق من الحصة (Verify).
* أول نقطة تنجح هي التي تبقى شغالة، والباقي يتوقف فورًا.
* بعد النجاح تظهر رسالة `ACTIVATION DONE` وتُغلق النافذة تلقائيًا بعد ثانيتين.
* النفق يظل شغالًا بعد إغلاق النافذة (لأنه داخل خدمة `warp-svc`).
* `WarpSetup.exe --stop` لإيقاف النفق وإرجاع الإنترنت العادي.
