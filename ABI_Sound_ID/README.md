# ABI Sound ID

Identify items in **Arena Breakout: Infinite** by the sound of dragging them at the vendor.

The app only **listens** to system audio (loopback / virtual cable). It does not inject into the game process or read game memory.

**Languages:** Russian / English (switch in the main tab; restart to apply).

---

## Features

- Real-time recognition via transparent overlay
- WASAPI loopback (hear what plays in speakers/headphones)
- Fallback: `sounddevice` + VB-Audio Virtual Cable
- Import sound packs (`18_pickup.ogg`, …) or record from the game
- Adjustable sensitivity and strict waveform matching

## Requirements

- **Windows 10/11**
- **Python 3.11 or 3.12** (3.13 often breaks audio COM)
- One of: [PyAudioWPatch](https://pypi.org/project/PyAudioWPatch/) (recommended) or [sounddevice](https://pypi.org/project/sounddevice/)

Optional: [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) if loopback is unavailable.

## Quick start

```bat
git clone https://github.com/YOUR_USER/ABI_Sound_ID.git
cd ABI_Sound_ID
start.bat
```

Or manually:

```bat
py -3.12 -m pip install -r requirements.txt
py -3.12 abi_gui.py
```

1. Open the **Library** tab → **Import sounds** (folder with `N_pickup.ogg` / `N_drop.ogg`).
2. Leave source as **Auto**.
3. Set the game to **borderless window**.
4. Press **Start**.

## Project layout

| File | Role |
|------|------|
| `abi_gui.py` | UI + overlay |
| `abi_sound_id.py` | Matching engine, library, CLI |
| `abi_audio.py` | Capture backends (PyAudioWPatch / sounddevice / soundcard) |
| `i18n.py` | RU / EN strings |
| `start.bat` | Install deps + launch |
| `build_exe.bat` | One-file EXE via PyInstaller |
| `requirements.txt` | Python dependencies |

## CLI

```bat
py -3.12 abi_sound_id.py devices
py -3.12 abi_sound_id.py import path\to\sounds
py -3.12 abi_sound_id.py check --apply
py -3.12 abi_sound_id.py listen
py -3.12 -c "from abi_audio import diagnostics_text; print(diagnostics_text())"
```

## Build EXE

```bat
build_exe.bat
```

Output: `dist\ABI_Sound_ID.exe`

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| No devices / capture error | Use Python **3.12**, install `PyAudioWPatch`, run diagnostics (CLI above) |
| Level bar does not move | Select another source, or install VB-Cable and pick **CABLE Output** |
| Overlay not visible | Game → borderless window mode |
| False matches | Enable **strict waveform** / raise sensitivity |
| Language not changing | Change language → OK → **restart** the app |

## Privacy

- No network calls required for recognition
- Settings stored under `%APPDATA%\ABI_Sound_ID\settings.json`
- Sound library is a local folder (default: next to the app)

## License

MIT — see [LICENSE](LICENSE).

---

# ABI Sound ID (RU)

Определение предмета в **Arena Breakout: Infinite** по звуку перетаскивания у торговца.

Программа только **слушает** системный звук. К процессу игры не подключается.

**Языки:** русский / English (выбор на вкладке «Главная», нужен перезапуск).

### Быстрый старт

```bat
py -3.12 -m pip install -r requirements.txt
py -3.12 abi_gui.py
```

или двойной клик по `start.bat`.

Рекомендуется **Python 3.12**. На 3.13 захват звука часто падает с ошибкой COM.
