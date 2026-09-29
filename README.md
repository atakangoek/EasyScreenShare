# EasyScreenShare

Stream your iPhone's screen **and sound** to a window on your Windows PC over Wi-Fi.
Open the app on both, tap **Start streaming** on the phone, done.

```
iPhone                                              PC
┌─────────────────────────────┐   Wi-Fi (TCP)   ┌──────────────────────────┐
│ ReplayKit broadcast ext.    │ ──────────────▶ │ easyscreenshare.py        │
│  screen → H.264 (hardware)  │  video + audio  │  H.264 decode (FFmpeg)    │
│  app audio → 16-bit PCM     │                 │  window (pygame) + audio  │
└─────────────────────────────┘ ◀── Bonjour ─── │  announces itself (mDNS)  │
                                   discovery    └──────────────────────────┘
```

## PC (Windows)

Double-click **`pc/run.bat`**. The first run installs the Python packages it needs (Python 3.10+ required).
You can also build a standalone app with `pc/build_exe.bat` → `pc/dist/EasyScreenShare/EasyScreenShare.exe`.

**The first time, Windows Firewall asks for access: tick "Private networks" and click _Allow access_.**
Your Wi-Fi must be set as a *Private* network in Windows (Settings › Network › Wi-Fi › your network).

| Key | Action |
| --- | --- |
| F11 / double-click | Fullscreen (Esc to leave) |
| M | Mute |
| R | Rotate 90° (if a landscape app shows up sideways) |
| I | Stats (fps, bitrate, audio buffer) |

The window reshapes itself when you rotate the phone.

Options: `--name "Living room PC"` sets the name in the iPhone's Screen Mirroring list, `--no-airplay` turns AirPlay off, `--verbose` logs more.

## No app? Use AirPlay Screen Mirroring
The PC also shows up as an AirPlay screen-mirroring target, like an Apple TV. On any iPhone or iPad on the same Wi-Fi: **Control Center › Screen Mirroring › *your PC's name***. Video and sound both come through, and the phone's volume buttons work.

- No PIN is asked: anyone on your Wi-Fi can mirror to the PC while it's running (use `--no-airplay` on shared networks).
- It uses TCP port 7000 plus a few random ports, so allow the Windows Firewall prompt as above.
- The PC's AirPlay identity is kept in `%APPDATA%\EasyScreenShare\airplay.json`. Delete it to appear as a new device.
- The FairPlay part comes from [openairplay/airplay2-receiver](https://github.com/openairplay/airplay2-receiver) (GPLv2); see `pc/vendor/README.md`.

## iPhone

The iOS app has to be compiled on a Mac (Apple's rule). Pick one:

**A. You have a Mac**
1. `brew install xcodegen`, then in `ios/`: `xcodegen generate`
2. Open `EasyScreenShare.xcodeproj`. In `project.yml` (or Xcode › Signing), set `APP_BUNDLE_ID` to something unique like `com.yourname.easyscreenshare` and choose your team. Do this for **both** targets. A free Apple ID works.
3. Plug in your iPhone and press Run.

**B. No Mac: build on GitHub, sideload from Windows**
1. Push this folder to a GitHub repo. The workflow in `.github/workflows/ios.yml` builds an unsigned `EasyScreenShare-unsigned.ipa` (Actions tab › *Build iOS app* › *Run workflow* › download the artifact).
2. Install it with [Sideloadly](https://sideloadly.io) (or AltStore) using your Apple ID. With a free Apple ID the app has to be re-installed every 7 days.
   Install it as a normal app, **not inside LiveContainer**: LiveContainer can't run app extensions, and the screen broadcast is one. Don't let the installer remove app extensions.

On the phone you may need to enable **Settings › Privacy & Security › Developer Mode**, and trust your developer profile under *Settings › General › VPN & Device Management*.

### Using it
1. Start the PC app.
2. Open EasyScreenShare on the iPhone and allow **Local Network** access when asked. It should say *Ready: \<your PC\>*.
3. Tap **Start streaming** → **Start Broadcast**. Stop from the app, the red status pill, or Control Center (long-press Screen Recording).

## Good to know
- **Audio** is the phone's app/system sound. The microphone is not sent. Apps that block screen recording (Netflix, some banking apps, DRM video) show black on the PC too. That is iOS policy.
- **Latency** is typically 100–200 ms on a decent Wi-Fi. 5 GHz Wi-Fi helps. If the network can't keep up, frames are dropped instead of building up delay.
- **Quality:** 1920 px on the long side, 8 Mbit/s, up to 60 fps (`H264Encoder.swift`: `maxLongSide`, `bitrate`).
- **Several PCs** running the receiver: the phone uses the first one alphabetically (the app shows which).
- The screen only sends frames when something changes, so a still screen shows 0 fps. That's normal.

## Testing the PC side without a phone
`pc/.venv/Scripts/python pc/tools/fake_iphone.py` finds the receiver just like the phone does and streams a test pattern with a 440 Hz tone, rotating every 5 s.

`pc/.venv/Scripts/python pc/tools/fake_airplay.py` does the same over AirPlay (to `127.0.0.1:7000`, or `--host`): pairing, the FairPlay handshake with a known key, then encrypted video and audio the way iOS sends them.

## Protocol
TCP messages: `[type u8][length u32 BE][payload]`. `0x01` hello (JSON), `0x02` video (`orientation u8, flags u8, pts u64` + H.264 Annex B), `0x03` audio (`rate u32, channels u8, pts u64` + s16le interleaved PCM), `0x10` PC→phone keyframe request. Bonjour service type `_easyscreenshare._tcp`.
