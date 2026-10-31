<div align="center">

<img src="icon.png" width="100" alt="OpenCode NoLimits">

# OpenCode NoLimits v6.3

**Intelligent Cloudflare WARP IP Rotator for OpenCode API**

A professional Windows console tool that automatically rotates Cloudflare WARP IPs until it finds one with available OpenCode API quota. Features smart startup detection, infinite retry capability, and real-time GUI monitoring.

</div>

---

## ✨ Features

- **🧠 Smart Startup**: Automatically detects if WARP is already connected and verifies quota before rotating
- **♾️ Infinite Retry**: Keeps trying forever until finding an IP with good quota or manual stop
- **👁️ Real-time GUI**: Launches Cloudflare WARP GUI so you can monitor connection status visually
- **⚡ Intelligent Prescreening**: Tests endpoint latency before connecting for faster results
- **🔧 Self-Repair**: Automatically fixes WARP registration issues when running as Administrator
- **📊 Live Console**: Never-closing window with detailed step-by-step progress
- **🛡️ Error Handling**: Comprehensive error detection and clear reporting

---

## 📋 Requirements

- Windows 10/11
- [Cloudflare WARP](https://1.1.1.1/) installed
- Administrator privileges (recommended for self-repair)

---

## 🚀 Installation

### Method 1: Download EXE (Recommended)

1. Download the latest `WarpSetup.exe` from [Releases](https://github.com/YOUR_USERNAME/YOUR_REPO/releases)
2. Place it anywhere on your system
3. Run it (UAC prompt will appear)

### Method 2: Build from Source

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
pip install -r requirements.txt
python -m PyInstaller WarpSetup.spec
```

The compiled EXE will be in `dist/WarpSetup.exe`.

---

## 💻 Usage

### Basic Usage

```bat
# Start with infinite retry (default)
WarpSetup.exe

# Stop WARP and restore normal internet
WarpSetup.exe --stop
```

### Advanced Options

```bat
# Don't launch Cloudflare WARP GUI
WarpSetup.exe --no-gui

# Skip UAC prompt (limited cleanup)
WarpSetup.exe --no-elevate

# Run with multiple probe threads
WarpSetup.exe --probe-threads 3

# Run for limited cycles (for testing)
WarpSetup.exe --cycles 2 --no-monitor
```

### All Flags

| Flag | Description |
|------|-------------|
| `--stop` | Stop WARP tunnel and restore normal internet |
| `--no-gui` | Don't launch Cloudflare WARP GUI |
| `--no-elevate` | Skip UAC prompt (limited self-repair) |
| `--probe-threads N` | Number of latency probe threads (default: 1) |
| `--cycles N` | Stop after N cycles (default: infinite) |
| `--no-monitor` | Don't monitor after success (for testing) |

---

## 🔄 How It Works

```
┌─────────────────────────────────────────────────────────────┐
│  1. Check if WARP is already connected                      │
│       ├─ Yes → Verify quota                                 │
│       │       ├─ Good → Keep connection + monitor           │
│       │       └─ Bad → Start IP rotation                   │
│       └─ No → Start IP rotation directly                    │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  2. IP Rotation Cycle                                       │
│       ├─ Prescreen 9 endpoints (latency test)               │
│       ├─ Connect to fastest endpoint                        │
│       ├─ Verify IP + OpenCode quota                        │
│       ├─ Success? → Keep connection + monitor               │
│       └─ Failure? → Try next endpoint (infinite retry)     │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  3. Monitoring Mode (after success)                          │
│       ├─ Check connection every 12 seconds                   │
│       ├─ Verify OpenCode quota periodically                  │
│       ├─ Quota exhausted? → Rotate IP automatically          │
│       └─ Connection lost? → Reconnect automatically          │
└─────────────────────────────────────────────────────────────┘
```

---

## 📁 File Locations

| Item | Path |
|------|------|
| Last working endpoint/method | `%LOCALAPPDATA%\OpenCodeNoLimits\state.json` |
| Diagnostic logs | `%TEMP%\warp_setup_diag.log` |
| WARP service logs | `C:\ProgramData\Cloudflare\cfwarp_service_log.txt` |

---

## 🔧 Troubleshooting

### "Registration Missing" Error

If you see `Status update: Unable — Reason: Registration Missing`:

1. Run the tool as Administrator
2. The tool will automatically restart the WARP service and re-register
3. If it persists, reinstall Cloudflare WARP from [1.1.1.1](https://1.1.1.1/)

### All IPs Show 429 Rate Limit

This means your OpenCode API quota is exhausted. The tool will automatically:
- Keep retrying infinitely
- Try different WARP endpoints
- Alert you when a new IP with available quota is found

You can also:
- Wait for quota reset (daily/weekly)
- Subscribe to [OpenCode Go](https://opencode.ai/go)

### WARP GUI Not Launching

- Ensure Cloudflare WARP is installed in the default location
- Use `--no-gui` flag if you don't need the GUI
- Manually launch `Cloudflare WARP.exe` from Start Menu

---

## 🛠️ Development

### Project Structure

```
open-code-ip/
├── warp_setup.py       # Main application code
├── WarpSetup.spec      # PyInstaller specification
├── build_exe.bat       # Build script for Windows
├── requirements.txt    # Python dependencies
├── README.md          # This file
└── icon.png           # Application icon
```

### Building the EXE

```bash
# Using batch script
build_exe.bat

# Or manually
pip install -r requirements.txt
python -m PyInstaller WarpSetup.spec
```

The compiled EXE is a single-file portable executable with:
- Console interface
- UAC admin manifest
- No external dependencies

---

## 📝 Changelog

### v6.3 (Current)
- ✨ Added smart startup: checks existing WARP connection
- ✨ Added infinite retry: never stops until success
- 🐛 Improved OpenCode API verification with detailed logging
- 🎨 Updated UI with clearer status messages
- 📖 Improved documentation

### v6.2
- Added Cloudflare WARP GUI launcher
- Removed IPNET fallback
- Improved error handling

### v6.1
- Added IPNET integration (removed in v6.2)
- Improved endpoint prescreening

### v6.0
- Initial release with WARP + warp-plus fallback

---

## 🤝 Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

---

## 📄 License

This project is provided as-is for educational purposes. Use responsibly and respect the terms of service of all involved services (Cloudflare WARP, OpenCode API).

---

## ⚠️ Disclaimer

This tool is designed for legitimate use cases only. Do not use it to bypass rate limits or violate terms of service. The authors are not responsible for misuse of this software.

---

## 🙏 Acknowledgments

- [Cloudflare WARP](https://1.1.1.1/) for providing the VPN service
- [OpenCode](https://opencode.ai) for the AI API
- The open-source community for various utilities

---

<div align="center">

**Made with ❤️ for the community**

If you find this tool useful, please ⭐ star the repository!

</div>
