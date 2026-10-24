
### Method: browser DevTools (F12 → Media)

Works in **Chrome** or **Edge**.

1. Open [abibuilder.com/merchant](https://abibuilder.com/merchant).
2. Press **F12** (or right-click → Inspect).
3. Open the **Media** tab.  
   If you do not see it:  
   **⋮** (panel menu) → **More tools** → **Media**.
4. On the page, click **Pick up** or **Drop** for the sound group you need.  
   The clip should appear in the Media panel player list.
5. Select the entry in Media.
6. Open the audio URL (often under **Player** / source link), or use:
   - right-click the media → **Open in new tab**, then  
   - right-click the player → **Save audio as…**
7. Save the file with the correct name, for example:
   - group **18**, pick up → `18_pickup.ogg`
   - group **18**, drop → `18_drop.ogg`
8. Put all saved files in one folder.
9. In ABI Sound ID: **Library** → **Import sounds from folder** → select that folder.


## Quick start

1. **Import sounds**  
   Open the **Library** tab → **Import sounds from folder**.  
   Use files named by group id, for example:
   - `18_pickup.ogg`
   - `18_drop.ogg`
   - `6_pickup.wav`

2. **Audio source**  
   Leave **Auto (any available audio)** selected, or pick a specific loopback / `CABLE Output` device.  
   Press **↻** to refresh the list.

3. **Game settings**  
   Set Arena Breakout: Infinite to **borderless window** (not exclusive fullscreen).

4. **Run**  
   Press **Start**. Drag items at the vendor; matches appear on the overlay.

5. **Language**  
   Main tab → **Language / Язык** → English or Русский → restart the app.

---

## User interface

### Main

| Control | Purpose |
|---------|---------|
| **Start / Stop** | Begin or stop live recognition |
| **Source** | Audio input (Auto = best loopback/input found) |
| **Sensitivity** | How easily quiet events are accepted |
| **Input level** | Live meter — should move when game audio plays |
| **Item size filter** | Limit matches to a grid size (e.g. 2×3) |
| **Font size / Hide after** | Overlay appearance |
| **Move overlay** | Drag the overlay, then uncheck |
| **Strict waveform** | Fewer false positives; turn off if nothing is detected |
| **Language** | English or Russian (restart required) |

### Library

- Choose the library folder
- Import `ogg` / `wav` packs
- Check library and auto-tune thresholds
- List groups

### Record from game

1. Enter item name (and optional size / “red item”).
2. Start recording and drag the item in-game several times (pause ≥ 1 s between drags).
3. Samples are saved into the library; if the sound matches an existing group, you can merge them.

---

## Command-line tools

```bat
py -3.12 abi_sound_id.py devices
py -3.12 abi_sound_id.py import path\to\sounds
py -3.12 abi_sound_id.py check --apply
py -3.12 abi_sound_id.py listen
py -3.12 abi_sound_id.py selftest
```

**Audio diagnostics** (backends + device list):

```bat
py -3.12 -c "from abi_audio import diagnostics_text; print(diagnostics_text())"
```

---

## Project structure

```text
ABI_Sound_ID/
├── abi_gui.py          # GUI and overlay
├── abi_sound_id.py     # Matching engine, library, CLI
├── abi_audio.py        # Capture backends (PyAudioWPatch / sounddevice / soundcard)
├── i18n.py             # English / Russian UI strings
├── requirements.txt
├── start.bat           # Install + launch
├── build_exe.bat       # PyInstaller one-file build
├── LICENSE             # MIT
└── README.md
```

Settings are stored in `%APPDATA%\ABI_Sound_ID\settings.json`.  
The default library path is a folder next to the app (configurable in the UI).

---

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| Program crashes or no devices | Use **Python 3.12**, not 3.13. Run diagnostics (see above). |
| `Error 0x100000001` / COM errors | Typical on Python 3.13 + `soundcard`. Prefer 3.12 + PyAudioWPatch. |
| Level bar does not move | Play sound in the game; try another source; install VB-Cable and select **CABLE Output**. |
| Overlay not visible | Use **borderless window** mode in the game. |
| Nothing is recognized | Import or record samples first; lower sensitivity; disable strict waveform. |
| Too many false matches | Enable strict waveform; raise sensitivity; filter by item size. |
| Language did not change | Change language → confirm → **fully restart** the app. |
| EXE has no audio devices | Rebuild with `build_exe.bat` so `sounddevice` / `pyaudiowpatch` are collected. |

---

## Privacy & safety

- Recognition is **local only** — no account, no telemetry for matching.
- The app does **not** modify the game client or read its memory.
- Use at your own discretion regarding the game’s Terms of Service.

---

## Building from source

```bat
py -3.12 -m pip install -r requirements.txt pyinstaller
build_exe.bat
```

Requires the same Python version you use to run the GUI.

---

## Contributing

Issues and pull requests are welcome. Please include:

- Windows version  
- Python version (`py -3.12 -c "import sys; print(sys.version)"`)  
- Output of the audio diagnostics command  

---

