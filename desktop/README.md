# PersonalAGI desktop shell

A thin Tauri window over the CLI. Tauri rather than Electron: ~5 MB against
~120 MB, and it uses the OS webview instead of shipping a second browser.

## The one design decision worth defending

**Open means active. Closed means it is not watching.**

The window's visibility IS the capture state — not a setting inside a window
that stays running when you close it. That makes the state visible at a glance,
makes turning it off a physical gesture, and doubles as the consent mechanism
when transcription is added later. A tray icon that keeps listening after you
close the window is the design that gets an assistant uninstalled.

`close` therefore calls `halt_capture` before the window goes, and the tray
icon reflects the real state rather than a remembered preference.

## Running it

```bash
cd desktop
npm install
npm run tauri dev
```

Requires the Rust toolchain (`rustup`) and Node. The Python side must be
importable — the shell shells out to `python -m personalagi`, so it inherits
whatever `.env` the CLI uses and cannot see anything the CLI cannot.

## What it does NOT do

- No transcription. Microphone and system audio are not touched; capturing the
  far side of a call on macOS needs a virtual audio device or the newer
  ScreenCaptureKit APIs, and consent has to be designed before capture is.
- No sending. The shell renders proposals and records outcomes; `SEND_ENABLED`
  still lives in `.env` and is not exposed here, deliberately. A UI toggle for
  "allow sending" is a toggle that gets clicked by accident.
- No direct database access. Everything goes through the CLI, so the shell can
  never do something the terminal cannot, and there is one set of rules.
