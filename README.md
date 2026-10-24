```
```markdown 
# ABI Sound ID

**Identify Arena Breakout: Infinite vendor items by the sound of dragging them.**

ABI Sound ID listens to your system audio, detects the short “pickup / drag” sounds at the trader, and shows the matching item on a transparent overlay. It never attaches to the game process and does not read game memory.

| | |
|---|---|
| **Platform** | Windows 10 / 11 |
| **Python** | 3.11 or **3.12** (recommended) |
| **UI languages** | English · Russian |
| **License** | MIT |

---

## Features

- **Real-time recognition** with a click-through overlay on top of the game
- **WASAPI loopback** — hears what plays in your speakers or headphones (via PyAudioWPatch)
- **Fallback capture** through `sounddevice` and optional [VB-Audio Virtual Cable](https://vb-audio.com/Cable/)
- **Sound library**: import packs (`18_pickup.ogg`, `18_drop.ogg`, …) or record samples from the game
- **Adjustable sensitivity** and optional strict waveform matching to reduce false positives
- **Bilingual UI** (English / Russian) — change language on the Main tab, then restart

---

## How it works

1. You import or record short audio templates for each item (or item group).
2. While you play, the app continuously listens to system audio.
3. When a drag sound is detected, it is compared against the library (spectral + waveform similarity).
4. The best match is shown on a transparent overlay. The game must run in **borderless window** mode so the overlay stays visible.

The app only **listens**. It does not inject code, read process memory, or send data over the network for recognition.

---

## Requirements

### Software

- Windows 10 or 11
- [Python 3.12](https://www.python.org/downloads/) (64-bit) — **strongly recommended**  
  Python 3.13 often fails with Windows audio COM errors.
- During Python setup, enable **“Add python.exe to PATH”**

### Python packages

Listed in `requirements.txt`:

- `numpy`, `scipy`, `soundfile`
- `sounddevice`
- `PyAudioWPatch` (best loopback backend on Windows)

### Optional

- [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) — if loopback devices are missing or unreliable

---

## Installation

### Option A — script (easiest)

1. Clone or download this repository.
2. Double-click **`start.bat`**.  
   It installs dependencies with Python 3.12 and starts the GUI.

### Option B — manual

```bat
git clone https://github.com/YOUR_USER/ABI_Sound_ID.git
cd ABI_Sound_ID

py -3.12 -m pip install -r requirements.txt
py -3.12 abi_gui.py
```

### Option C — standalone EXE

```bat
build_exe.bat
```

Output: `dist\ABI_Sound_ID.exe`  
You can copy the EXE anywhere; the sound library folder is created next to it (or chosen in the UI).

---

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

