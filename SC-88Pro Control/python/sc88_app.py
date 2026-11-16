#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
SC-88Pro CONTROL  -  MIDI control surface for the Roland SC-88Pro (1996).

Run:      python sc88_app.py
Needs:    pip install pygame
Labels:   edit sc88_data.json (instrument and drum kit names)

Wiring (your setup):  Port A of the SC-88Pro  <-  MIDI Port 4x4 output C
                      Port B of the SC-88Pro  <-  MIDI Port 4x4 output D
Click the two OUT buttons at the top to pick those devices.

Version 3:
  - tabs: Port A / Port B strips, EFFECTS / EQ, INSERT FX (65 types), PART EDIT, DRUM EDIT
  - READ UNIT pulls the unit's state back (needs the IN device = the 4x4 input fed by the SC-88Pro's MIDI OUT)
  - PATCH STARTERS: the 128 preset patches' instruments + insertion effect type
  - EXPORT .MID writes one MIDI file per port into the exports folder, to recall the setup from a DAW
  - setups save and load everything (old setups still load)
"""
import json
import math
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pygame

try:
    import pygame.midi as pgmidi
except Exception:  # portmidi missing
    pgmidi = None

def _data_root(here):
    """<App>/python or <App>/exe  ->  <App>/data  (rebuilds never touch it).  First run also copies any
    factory *.json from <App>/defaults into data, but only the ones that are missing."""
    root = here.parent if here.name.lower() in ("python", "exe") else here
    d = root / "data"
    try:
        d.mkdir(exist_ok=True)
        dflt = root / "defaults"
        if dflt.is_dir():
            import json as _js, shutil as _sh
            for _f in dflt.glob("*.json"):
                t = d / _f.name
                if not t.exists():
                    _sh.copy(_f, t)
                    continue
                try:   # factory files carrying a _data_version replace an older copy (the old one is kept as .old)
                    dv = _js.loads(_f.read_text(encoding="utf-8")).get("_data_version", 0)
                    tv = _js.loads(t.read_text(encoding="utf-8")).get("_data_version", 0)
                    if dv and tv < dv:
                        _sh.copy(t, str(t) + ".old")
                        _sh.copy(_f, t)
                except Exception:
                    pass
        return d
    except Exception:
        return here


if getattr(sys, "frozen", False):          # running as a PyInstaller .exe: keep data/settings beside the exe
    BASE = _data_root(Path(sys.executable).resolve().parent)
    try:
        import shutil
        for _f in Path(sys._MEIPASS).glob("*.json"):
            if not (BASE / _f.name).exists():
                shutil.copy(_f, BASE / _f.name)
    except Exception:
        pass
else:
    BASE = _data_root(Path(__file__).resolve().parent)
DATA_FILE = BASE / "sc88_data.json"
SETUPS_DIR = BASE / "setups"
SETTINGS_FILE = BASE / "sc88_settings.json"

W, H = 1400, 860
GAP = 0.004  # seconds between any two messages (tune if the unit drops anything)

# ---- palette ----------------------------------------------------------------
ACCENT = (255, 138, 38)
ACCENT_DIM = (150, 78, 24)
TEXT = (232, 223, 207)
DIM = (150, 124, 104)
PANEL = (34, 26, 22)
EDGE = (79, 64, 54)
LCD_BG = (15, 7, 2)
AMBER = (255, 224, 110)

# ---- MIDI constants -----------------------------------------------------------
GS_RESET = [0xF0, 0x41, 0x10, 0x42, 0x12, 0x40, 0x00, 0x7F, 0x00, 0x41, 0xF7]
DRUM_CH = {9}  # channel 10 (index 9) of each port is the drum part
FIELDS = [("vol", 7), ("pan", 10), ("rev", 91), ("cho", 93), ("dly", 94)]
FIELD_CC = dict(FIELDS)
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(v):
    return f"{NOTE_NAMES[v % 12]}{v // 12 - 1}"


def _tm(v):
    return f"{v - 64:+d}"


# Per-part SysEx parameters (manual p.196-199).  key, label, mid-byte base, low byte, lo, hi,
# default, kind, names, bytes
PART_EXT = [
    ("mono", "MODE", 0x10, 0x13, 0, 1, 1, "choice", ["Mono", "Poly"], 1),
    ("assign", "ASSIGN", 0x10, 0x14, 0, 2, 1, "choice", ["Single", "Limited-Multi", "Full-Multi"], 1),
    ("kshift", "KEY SHIFT", 0x10, 0x16, 0x28, 0x58, 0x40, "knob", None, 1),
    ("ftune", "FINE TUNE", 0x10, 0x2A, 0, 127, 64, "knob", None, 2),
    ("vdepth", "VEL DEPTH", 0x10, 0x1A, 0, 127, 64, "knob", None, 1),
    ("voffs", "VEL OFFSET", 0x10, 0x1B, 0, 127, 64, "knob", None, 1),
    ("klow", "KEY LOW", 0x10, 0x1D, 0, 127, 0, "knob", None, 1),
    ("khigh", "KEY HIGH", 0x10, 0x1E, 0, 127, 127, "knob", None, 1),
    ("tm_vrate", "VIB RATE", 0x10, 0x30, 0, 127, 64, "knob", None, 1),
    ("tm_vdepth", "VIB DEPTH", 0x10, 0x31, 0, 127, 64, "knob", None, 1),
    ("tm_vdelay", "VIB DELAY", 0x10, 0x37, 0, 127, 64, "knob", None, 1),
    ("tm_cut", "CUTOFF", 0x10, 0x32, 0, 127, 64, "knob", None, 1),
    ("tm_res", "RESONANCE", 0x10, 0x33, 0, 127, 64, "knob", None, 1),
    ("tm_att", "ATTACK", 0x10, 0x34, 0, 127, 64, "knob", None, 1),
    ("tm_dec", "DECAY", 0x10, 0x35, 0, 127, 64, "knob", None, 1),
    ("tm_rel", "RELEASE", 0x10, 0x36, 0, 127, 64, "knob", None, 1),
    ("eq", "PART EQ", 0x40, 0x20, 0, 1, 1, "choice", ["EQ Off", "EQ On"], 1),
    ("output", "OUTPUT", 0x40, 0x21, 0, 3, 0, "choice", ["Output 1", "Output 2", "Output 2 L", "Output 2 R"], 1),
    ("efx", "INSERTION FX", 0x40, 0x22, 0, 1, 0, "choice", ["Bypass", "Insertion FX"], 1),
]
EXT = {e[0]: e for e in PART_EXT}
PART_DEFAULTS = {"bank": 0, "prog": 0, "vol": 100, "pan": 64, "rev": 40, "cho": 0, "dly": 0}
PART_DEFAULTS.update({e[0]: e[6] for e in PART_EXT})


def part_default(p):
    d = dict(PART_DEFAULTS)
    if p % 16 in DRUM_CH:
        d["assign"] = 0
    return d


def new_parts():
    return [part_default(p) for p in range(32)]


def ext_text(key, v):
    e = EXT[key]
    if e[7] == "choice":
        return e[8][max(0, min(len(e[8]) - 1, v))]
    if key == "kshift":
        return f"{v - 64:+d} st"
    if key == "ftune":
        return f"{(v - 64) * 100 / 64:+.0f} ct"
    if key in ("klow", "khigh"):
        return note_name(v)
    if key.startswith("tm_") or key in ("vdepth", "voffs"):
        return _tm(v) if key.startswith("tm_") else str(v)
    return str(v)


def vlq(n):
    """MIDI variable-length quantity."""
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return out[::-1]



HELP_TOPICS = [
    ("Getting started", """
WHAT THIS IS
A control surface for the Roland SC-88Pro. It loads sounds, changes settings, reads the unit back and saves everything to a file, so a setup can be recalled any time (and from your DAW).

FIRST-TIME SETUP
1. Click PORT A OUT and choose the 4x4 output that goes to the SC-88Pro's MIDI IN A (your Port C).
2. Click PORT B OUT and choose the 4x4 output that goes to MIDI IN B (your Port D). The list shows each output with a number, so identical names can be told apart.
3. Optional but recommended: click IN and choose the 4x4 input that receives the SC-88Pro's MIDI OUT (your Port C in). Set the unit's OUT/THRU switch to OUT. This enables READ UNIT, READ NOTE and READ KIT NAMES.
4. Your choices are remembered next time.

THE TOP BAR
GS RESET - sends a GS Reset to both ports and puts the screen back to GS defaults.
SEND ALL - sends everything on screen to the unit (effects first, then each part).
READ UNIT - reads the unit's current state into the screen.
MASTER knob - master volume for the whole unit.
OUT / IN lights - flash when the app sends or receives MIDI.
The status line (top right) shows what just happened or what went wrong.

THE BASICS
Nothing is sent until you touch a control. The screen starts at GS defaults, not at what the unit currently holds - use READ UNIT to bring the unit's real state in.
Knobs: drag up/down (hold Shift for fine), use the mouse wheel, or double-click to reset to the default. Click buttons to open pick-lists.
Esc closes a pick-list, or quits the app when none is open.
"""),
    ("Port A / Port B strips", """
The two Port tabs show the 16 parts of each SC-88Pro port (32 parts in all). Each strip has:

INSTRUMENT SCREEN - the program number, bank (variation) number and the instrument name. Click it for the list of all 128 instruments. Instruments that have variations show (+n); pick one and a second list offers the variations. The mouse wheel over the screen steps the program.
BANK - / + - steps through the variations that really exist for that instrument. Right-click jumps 5 at a time.
PAN - stereo position (C = centre).
REV / CHO / DLY - how much of the part goes to the reverb, chorus and delay effects.
VOL fader - part volume.
Part name (A01, A02 ...) - click it to open that part on the PART EDIT tab.

Part 10 of each port (A10, B10) is the drum part. It shows DRUM, lists drum kits instead of instruments and has no bank buttons.

Instrument names are the Native map names from the manual. Variation numbers that are not in the list show as the base name with a ? mark.
"""),
    ("Effects / EQ", """
These are the unit's global effects, shared by all parts. How much each part uses comes from its REV / CHO / DLY knobs.

REVERB - type (Room 1-3, Hall 1-2, Plate, Delay, Panning Delay), character, pre-LPF, level, time, delay feedback, pre-delay.
CHORUS - type (Chorus 1-4, Feedback Chorus, Flanger, Short Delay, Short Delay FB), pre-LPF, level, feedback, delay, rate, depth and how much goes on to the reverb and delay.
DELAY - type (Delay 1-4, Pan Delay 1-4, Delay to Reverb, Pan Repeat), pre-LPF, time (shown in ms), ratios, levels, feedback and send to reverb.
EQUALIZER - low frequency (200/400 Hz) and gain, high frequency (3/6 kHz) and gain, +/-12 dB.

When you change an effect TYPE the unit resets that block to its own values, so the app immediately re-sends your knob values. The screen therefore always matches the unit.
"""),
    ("Insert FX", """
The insertion effect is a single big effect (distortion, rotary, phaser, guitar multis and so on) that a part can be routed through. There are 65 types.

TYPE - click to choose. After the choice is sent, the app asks the unit for the real starting values if IN is connected; otherwise the values shown are estimates until you press READ UNIT.
KNOBS - each type shows only its own parameters, named as in the manual. Choices (Off/On, filter type and so on) are buttons. Values marked # are steps in the manual's effect data table (rates, delay times and so on), so they show a step number rather than Hz or ms.
OUTPUT OF THE INSERTION EFFECT - how much goes to reverb, chorus and delay, and the EQ on/off.
PATCH STARTERS - the unit's 128 preset patches. Choosing one puts its instruments on parts 1, 2... (drum kits on part 10), sets the insertion effect type and turns each part's INSERTION FX switch on. It is a starting point, not an exact copy: the manual only lists instruments and effect type, not levels or other settings.

IMPORTANT: a part only uses the insertion effect when its INSERTION FX switch (PART EDIT tab, Routing) is set to Insertion FX. Only one insertion effect exists, so all those parts share it.
"""),
    ("Part Edit", """
Detailed settings for any one part. Choose the part with the button at the top or by clicking a part name on a strip.

PLAY MODE / RANGE
Mono/Poly, Assign mode (Single, Limited-Multi, Full-Multi), Key Shift (+/-24 semitones), Fine Tune (+/-100 cents), Velocity depth and offset, Key Low and Key High (the note range the part answers to).
TONE MODIFY
Vibrato rate, depth and delay; filter cutoff and resonance; envelope attack, decay and release. All are relative: the middle position (+0) leaves the instrument as it is.
ROUTING
Part EQ on/off; Output (1, 2, 2L, 2R); Insertion FX bypass / on.

Amber numbers have been changed from the GS default. Double-click a knob to reset it. RESET THIS PART'S EDITS puts the whole part back to defaults.
Tone modify and key range are applied to notes played after the change.
"""),
    ("Drum Edit", """
Fine control over each drum instrument (each note) of the drum kit.

PORT / MAP - choose Port A or B, and drum map 1 or 2 (part 10 uses map 1 unless you changed it).
NOTE - click to pick a drum note. Names come from READ KIT NAMES, otherwise from the General MIDI drum map.
READ NOTE - asks the unit for that note's real values (needs IN). Without it, unread values show --.
READ KIT NAMES - asks the unit for the instrument names of the kit currently on that port's drum part.
KNOBS - level, pan (Rnd = random), reverb, chorus and delay sends, pitch, assign group (Non = none; notes in the same group cut each other off, like open/closed hi-hat). Rx note on/off switches.
FORGET EDITS - forgets the app's own edits for that port and map (it does not change the unit).

Amber = edited in the app. Only edited values are saved with a setup. When the drum kit is changed on the unit, its drum edits are reset, so setups re-send the edits after the kit.
"""),
    ("Setups, reading and exporting", """
SAVING AND LOADING
Type a name in the SETUP box and click SAVE. Setups appear as buttons in the LOAD strip (use < > to page); click one to load it and send it to the unit. Files are kept in the setups folder next to the app. A setup holds the instruments and banks, levels, pans, sends, master volume, the effects/EQ, the insertion effect, part edits and drum edits.
Loading sends only what is needed and resets values that the previous setup had changed.

READ UNIT
Reads about 830 values: the effects, the insertion effect and all 32 parts. It takes a few seconds and the status line shows progress. Then SAVE to keep it. This is the quickest way to capture a sound you made on the unit's front panel.

EXPORT .MID (for your DAW)
Writes two standard MIDI files into the exports folder: name_PortA.mid and name_PortB.mid. Each starts with a GS Reset and then sets everything with safe timing.
In the DAW: put the Port A clip on a track routed to your Port A output and the Port B clip on a track routed to Port B, both at the very start of the song with a bar or two of lead-in before the music. Play from the start and the SC-88Pro is set up by the song itself.
The files hold the setup, not notes: add your own note tracks as usual.
"""),
    ("Tips and troubleshooting", """
NO SOUND CHANGE
- Check the OUT buttons point to the right 4x4 ports and the status line says the port is open.
- Windows allows only one program to use a MIDI port at a time. If the status says a port could not be opened, close your DAW or MIDI monitor (or share the port through loopMIDI).
- Press GS RESET once if the unit was in an odd state, then SEND ALL.

READ UNIT RETURNS NOTHING
- The IN button must point to the 4x4 input that has the SC-88Pro's MIDI OUT cable.
- The unit's OUT/THRU switch must be on OUT. The IN light should flash during a read.

THE SCREEN DOES NOT MATCH THE UNIT
- Front-panel changes are not followed live. Press READ UNIT to catch up.

GOOD HABITS
- Start a session with GS RESET, then load a setup.
- Both ports need their own output; Port B parts are on the second MIDI input of the unit.
- Save often; setups are small text files you can back up or share.

KEYS
Esc - close a list / quit. Shift while dragging - fine adjust. Double-click - reset a value. Mouse wheel - step a value.
"""),
]


def wrap_text(text, size, maxw):
    f = font(size)
    out = []
    for para in text.strip("\n").split("\n"):
        if not para.strip():
            out.append("")
            continue
        indent = "  " if para.startswith(("- ", "1.", "2.", "3.", "4.")) else ""
        cur = ""
        for w in para.split(" "):
            t = (cur + " " + w) if cur else w
            if f.size(t)[0] <= maxw:
                cur = t
            else:
                out.append(cur)
                cur = indent + w
        out.append(cur)
    return out


def part_block(p):
    c = p % 16
    return c + 1 if c < 9 else (0 if c == 9 else c)  # part 10 = block 0, part 11 = block A


def part_addr(p, mid, low):
    return (0x40 << 16) | ((mid | part_block(p)) << 8) | low


def rq1(addr, size):
    """Roland 'Data Request 1' (manual p.191).  size = byte count (int) or 3 raw bytes."""
    sz = list(size) if isinstance(size, (list, tuple)) else [0, 0, size]
    body = [(addr >> 16) & 0x7F, (addr >> 8) & 0x7F, addr & 0x7F] + sz
    return [0xF0, 0x41, 0x10, 0x42, 0x11] + body + [(128 - sum(body) % 128) % 128, 0xF7]


# drum setup parameters (manual p.199): name -> middle-byte nibble
DRUM_PARAMS = {"pitch": 1, "level": 2, "group": 3, "pan": 4, "rev": 5, "cho": 6, "rxoff": 7, "rxon": 8, "dly": 9}
GM_DRUM = {35: "Acoustic Bass Drum", 36: "Bass Drum 1", 37: "Side Stick", 38: "Acoustic Snare", 39: "Hand Clap",
           40: "Electric Snare", 41: "Low Floor Tom", 42: "Closed Hi-Hat", 43: "High Floor Tom", 44: "Pedal Hi-Hat",
           45: "Low Tom", 46: "Open Hi-Hat", 47: "Low-Mid Tom", 48: "Hi-Mid Tom", 49: "Crash Cymbal 1", 50: "High Tom",
           51: "Ride Cymbal 1", 52: "Chinese Cymbal", 53: "Ride Bell", 54: "Tambourine", 55: "Splash Cymbal",
           56: "Cowbell", 57: "Crash Cymbal 2", 58: "Vibraslap", 59: "Ride Cymbal 2", 60: "Hi Bongo", 61: "Low Bongo",
           62: "Mute Hi Conga", 63: "Open Hi Conga", 64: "Low Conga", 65: "High Timbale", 66: "Low Timbale",
           67: "High Agogo", 68: "Low Agogo", 69: "Cabasa", 70: "Maracas", 71: "Short Whistle", 72: "Long Whistle",
           73: "Short Guiro", 74: "Long Guiro", 75: "Claves", 76: "Hi Wood Block", 77: "Low Wood Block",
           78: "Mute Cuica", 79: "Open Cuica", 80: "Mute Triangle", 81: "Open Triangle"}


def drum_addr(m, note, name):
    return (0x41 << 16) | (((m << 4) | DRUM_PARAMS[name]) << 8) | note


def drum_message(m, note, name, v):
    return roland_dt1(drum_addr(m, note, name), [v])


# what to read for one part: (name, address, bytes)
PART_READ = [("tone", 0x10, 0x00, 2), ("vol", 0x10, 0x19, 1), ("pan", 0x10, 0x1C, 1),
             ("cho", 0x10, 0x21, 1), ("rev", 0x10, 0x22, 1), ("dly", 0x10, 0x2C, 1)] + \
            [(e[0], e[2], e[3], e[9]) for e in PART_EXT]


def ext_message(p, key, v):
    e = EXT[key]
    addr = part_addr(p, e[2], e[3])
    if e[9] == 2:
        v14 = max(0, min(16383, round(8192 + (v - 64) * 8191 / 64)))
        return roland_dt1(addr, [v14 >> 7, v14 & 0x7F])
    return roland_dt1(addr, [v])
MASTER_DEFAULT = 127


def master_volume(v):
    """Universal real-time master volume."""
    return [0xF0, 0x7F, 0x7F, 0x04, 0x01, 0x00, v & 0x7F, 0xF7]


def part_name(p):
    return f"{'AB'[p // 16]}{p % 16 + 1:02d}"


def is_drum(p):
    return (p % 16) in DRUM_CH


def roland_dt1(addr, data):
    """Roland GS 'Data Set 1' message with checksum (manual p.191, 207)."""
    body = [(addr >> 16) & 0x7F, (addr >> 8) & 0x7F, addr & 0x7F] + [d & 0x7F for d in data]
    return [0xF0, 0x41, 0x10, 0x42, 0x12] + body + [(128 - sum(body) % 128) % 128, 0xF7]


def delay_ms(v):
    """DELAY TIME CENTER code -> milliseconds (manual p.195)."""
    table = [(0x01, 0x14, 0.1, 0.1), (0x14, 0x23, 2.0, 0.2), (0x23, 0x2D, 5.0, 0.5),
             (0x2D, 0x37, 10.0, 1.0), (0x37, 0x46, 20.0, 2.0), (0x46, 0x50, 50.0, 5.0),
             (0x50, 0x5A, 100.0, 10.0), (0x5A, 0x69, 200.0, 20.0), (0x69, 0x74, 500.0, 50.0)]
    for lo, hi, base, step in table:
        if lo <= v < hi:
            return base + (v - lo) * step
    return 1000.0 if v >= 0x73 else 0.1


REVERB_TYPES = ["Room 1", "Room 2", "Room 3", "Hall 1", "Hall 2", "Plate", "Delay", "Panning Delay"]
CHORUS_TYPES = ["Chorus 1", "Chorus 2", "Chorus 3", "Chorus 4", "Feedback Chorus", "Flanger",
                "Short Delay", "Short Delay (FB)"]
DELAY_TYPES = ["Delay 1", "Delay 2", "Delay 3", "Delay 4", "Pan Delay 1", "Pan Delay 2",
               "Pan Delay 3", "Pan Delay 4", "Delay to Reverb", "Pan Repeat"]


def _sgn(v):
    return f"{v - 64:+d}"


# key, label, address, lo, hi, default, kind ("choice" -> names), names/format
FX_SECTIONS = [
    ("REVERB", "rv_macro", [
        ("rv_macro", "TYPE", 0x400130, 0, 7, 4, "choice", REVERB_TYPES),
        ("rv_char", "CHARACTER", 0x400131, 0, 7, 4, "knob", None),
        ("rv_lpf", "PRE-LPF", 0x400132, 0, 7, 0, "knob", None),
        ("rv_level", "LEVEL", 0x400133, 0, 127, 64, "knob", None),
        ("rv_time", "TIME", 0x400134, 0, 127, 64, "knob", None),
        ("rv_fb", "DLY FEEDBACK", 0x400135, 0, 127, 0, "knob", None),
        ("rv_pre", "PRE-DELAY ms", 0x400137, 0, 127, 0, "knob", None)]),
    ("CHORUS", "ch_macro", [
        ("ch_macro", "TYPE", 0x400138, 0, 7, 2, "choice", CHORUS_TYPES),
        ("ch_lpf", "PRE-LPF", 0x400139, 0, 7, 0, "knob", None),
        ("ch_level", "LEVEL", 0x40013A, 0, 127, 64, "knob", None),
        ("ch_fb", "FEEDBACK", 0x40013B, 0, 127, 8, "knob", None),
        ("ch_delay", "DELAY", 0x40013C, 0, 127, 80, "knob", None),
        ("ch_rate", "RATE", 0x40013D, 0, 127, 3, "knob", None),
        ("ch_depth", "DEPTH", 0x40013E, 0, 127, 19, "knob", None),
        ("ch_rev", "TO REVERB", 0x40013F, 0, 127, 0, "knob", None),
        ("ch_dly", "TO DELAY", 0x400140, 0, 127, 0, "knob", None)]),
    ("DELAY", "dl_macro", [
        ("dl_macro", "TYPE", 0x400150, 0, 9, 0, "choice", DELAY_TYPES),
        ("dl_lpf", "PRE-LPF", 0x400151, 0, 7, 0, "knob", None),
        ("dl_time", "TIME", 0x400152, 1, 0x73, 0x61, "knob", None),
        ("dl_rl", "RATIO L", 0x400153, 1, 0x78, 4, "knob", None),
        ("dl_rr", "RATIO R", 0x400154, 1, 0x78, 4, "knob", None),
        ("dl_lc", "LEVEL CTR", 0x400155, 0, 127, 127, "knob", None),
        ("dl_ll", "LEVEL L", 0x400156, 0, 127, 0, "knob", None),
        ("dl_lr", "LEVEL R", 0x400157, 0, 127, 0, "knob", None),
        ("dl_level", "LEVEL", 0x400158, 0, 127, 64, "knob", None),
        ("dl_fb", "FEEDBACK", 0x400159, 0, 127, 80, "knob", None),
        ("dl_rev", "TO REVERB", 0x40015A, 0, 127, 0, "knob", None)]),
    ("EQUALIZER", "eq_lf", [
        ("eq_lf", "LOW FREQ", 0x400200, 0, 1, 0, "choice", ["200 Hz", "400 Hz"]),
        ("eq_lg", "LOW GAIN", 0x400201, 0x34, 0x4C, 0x40, "knob", None),
        ("eq_hf", "HIGH FREQ", 0x400202, 0, 1, 0, "choice", ["3 kHz", "6 kHz"]),
        ("eq_hg", "HIGH GAIN", 0x400203, 0x34, 0x4C, 0x40, "knob", None)]),
]
FX_PARAMS = {p[0]: p for _n, _m, ps in FX_SECTIONS for p in ps}
FX_DEFAULTS = {k: p[5] for k, p in FX_PARAMS.items()}
FX_MACRO_CHILDREN = {}
for _n, _m, _ps in FX_SECTIONS:
    FX_MACRO_CHILDREN[_m] = [p[0] for p in _ps if p[0] != _m]


def fx_text(key, v):
    p = FX_PARAMS[key]
    if p[6] == "choice":
        return p[7][max(0, min(len(p[7]) - 1, v))]
    if key == "dl_time":
        ms = delay_ms(v)
        return f"{ms:.1f} ms" if ms < 10 else f"{ms:.0f} ms"
    if key in ("dl_rl", "dl_rr"):
        return f"{v * 100 / 24:.0f} %"
    if key in ("dl_fb",):
        return _sgn(v)
    if key in ("eq_lg", "eq_hg"):
        return f"{v - 64:+d} dB"
    return str(v)


def fx_message(key, v):
    return roland_dt1(FX_PARAMS[key][2], [v])


def fx_sequence(fx):
    """All effect settings. Each type first, then its parameters (the unit resets a type's
    parameters when the type changes)."""
    seq = []
    for _n, macro, ps in FX_SECTIONS:
        order = [macro] + FX_MACRO_CHILDREN[macro]
        for k in order:
            seq.append((0, 0, "sx", fx_message(k, fx[k]), 0, 0.012 if k == macro else 0.0))
    return seq


INS_BASE = 0x400300
INS_SEND_ADDR = {"rev": 0x400317, "cho": 0x400318, "dly": 0x400319, "eq": 0x40031F}
INS_SEND_DEFAULT = {"rev": 40, "cho": 0, "dly": 0, "eq": 1}


def efx_meta(prm):
    """Value kind / range for one insertion-effect parameter, from the manual's table."""
    r, desc = prm["range"], prm["desc"]
    if "/" in r:
        raws = [int(x, 16) for x in r.split("/")]
        names = desc.split("/")
        if len(names) != len(raws):
            names = [f"{x:02X}" for x in raws]
        return {"kind": "choice", "raws": raws, "names": names, "lo": min(raws), "hi": max(raws)}
    if r.startswith("*"):
        return {"kind": "knob", "lo": 0, "hi": 15 if r == "*14" else 127, "fmt": "tbl"}
    lo, hi = [int(x, 16) for x in r.split("-")]
    fmt = {"34-4C": "db", "0F-71": "pct", "28-4C": "semi", "0E-72": "ct"}.get(r)
    if fmt is None:
        fmt = "pan" if desc.startswith("L63") else "raw"
    return {"kind": "knob", "lo": lo, "hi": hi, "fmt": fmt}


def efx_default(prm, meta):
    if meta["kind"] == "choice":
        return meta["raws"][0]
    parts = [x.strip() for x in prm["desc"].split(" - ")]
    mid = None
    if len(parts) == 3:
        try:
            mid = int(float(parts[1].replace("+", "")))
        except ValueError:
            mid = None
    f = meta["fmt"]
    if f == "db":
        return 64 + (mid or 0)
    if f in ("pct", "semi", "ct", "pan"):
        return 64
    if f == "tbl":
        return 0 if meta["hi"] < 20 else 64
    if mid is not None and meta["lo"] <= mid <= meta["hi"]:
        return mid
    return (meta["lo"] + meta["hi"]) // 2


def efx_text(prm, meta, v):
    if meta["kind"] == "choice":
        return meta["names"][meta["raws"].index(v)] if v in meta["raws"] else f"{v:02X}"
    f = meta["fmt"]
    if f == "db":
        return f"{v - 64:+d} dB"
    if f == "pct":
        return f"{(v - 64) * 2:+d} %"
    if f == "semi":
        return f"{v - 64:+d} st"
    if f == "ct":
        return f"{(v - 64) * 2:+d} ct"
    if f == "pan":
        return "C" if v == 64 else (f"L{64 - v}" if v < 64 else f"R{v - 64}")
    if f == "tbl":
        return f"#{v}"
    return str(v)


def ins_sequence(ins, efx):
    """Insertion effect: type first, then its parameters, then the sends."""
    t = efx.get(ins["type"])
    if t is None:
        return []
    seq = [(0, 0, "sx", roland_dt1(INS_BASE, [t["msb"], t["lsb"]]), 0, 0.02)]
    for prm in t["params"]:
        v = ins["vals"].get(prm["addr"])
        if v is not None:
            seq.append((0, 0, "sx", roland_dt1(INS_BASE | prm["addr"], [v]), 0, 0.0))
    for k, a in INS_SEND_ADDR.items():
        seq.append((0, 0, "sx", roland_dt1(a, [ins[k]]), 0, 0.0))
    return seq


def build_setup_sequence(parts, master, fx=None, touched=(), ins=None, efx=None, drum=None):
    """Whole setup, in an order that works: bank before program, program before levels.
    Items are (port, channel, kind, a, b, pause)."""
    seq = [(port, 0, "sx", master_volume(master), 0, 0.02) for port in (0, 1)]
    if fx:
        seq += fx_sequence(fx)
    if ins is not None and efx and (ins["type"] != 0 or ins.get("touched")):
        seq += ins_sequence(ins, efx)
    for p, part in enumerate(parts):
        port, ch = divmod(p, 16)
        if not is_drum(p):
            seq.append((port, ch, "cc", 0, part["bank"], 0.0))
        seq.append((port, ch, "pc", part["prog"], 0, 0.006))
        for f, cc in FIELDS:
            seq.append((port, ch, "cc", cc, part[f], 0.0))
        for e in PART_EXT:
            k = e[0]
            if part[k] != part_default(p)[k] or (p, k) in touched:
                seq.append((port, 0, "sx", ext_message(p, k, part[k]), 0, 0.0))
    for (port, m, note), vals in (drum or {}).items():
        for name, v in vals.items():
            seq.append((port, 0, "sx", drum_message(m, note, name, v), 0, 0.0))
    return seq


# =============================================================================
# DRAWING HELPERS
# =============================================================================
_font_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _font_cache:
        _font_cache[key] = pygame.font.SysFont(
            "dejavusansmono,consolas,menlo,couriernew,monospace", size, bold=bold)
    return _font_cache[key]


def txt(surf, s, size, color, pos, anchor="topleft", bold=False):
    img = font(size, bold).render(s, True, color)
    r = img.get_rect()
    setattr(r, anchor, pos)
    surf.blit(img, r)
    return r


def fit_size(s, maxw, sizes=(16, 14, 13, 12, 11, 10), bold=False):
    for sz in sizes:
        if font(sz, bold).size(s)[0] <= maxw:
            return sz
    return sizes[-1]


def wrap(text, size, maxw, lines=2):
    f = font(size, True)
    out, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if f.size(t)[0] <= maxw:
            cur = t
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    out = out[:lines]
    clipped = []
    for ln in out:
        while f.size(ln)[0] > maxw and len(ln) > 1:
            ln = ln[:-1]
        clipped.append(ln)
    return clipped


def arc_pts(cx, cy, r, a0, a1, steps=36):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / steps))) for i in range(steps + 1)]


def draw_knob(surf, cx, cy, R, t):
    pygame.draw.circle(surf, (12, 9, 7), (cx, cy), R + 7)
    pygame.draw.circle(surf, (70, 56, 48), (cx, cy), R + 7, 1)
    pygame.draw.lines(surf, (48, 24, 7), False, arc_pts(cx, cy, R + 3, 135, 405), 3)
    if t > 0:
        a1 = 135 + 270 * t
        pygame.draw.lines(surf, ACCENT_DIM, False, arc_pts(cx, cy, R + 3, 135, a1), 6)
        pygame.draw.lines(surf, ACCENT, False, arc_pts(cx, cy, R + 3, 135, a1), 3)
    pygame.draw.circle(surf, (70, 57, 48), (cx, cy), R)
    pygame.draw.circle(surf, (102, 84, 71), (cx, cy), R - 2)
    pygame.draw.circle(surf, (86, 70, 59), (cx, cy), R - 5)
    ang = math.radians(135 + 270 * t)
    p0 = (cx + math.cos(ang) * R * 0.30, cy + math.sin(ang) * R * 0.30)
    p1 = (cx + math.cos(ang) * R * 0.88, cy + math.sin(ang) * R * 0.88)
    pygame.draw.line(surf, (254, 244, 226), p0, p1, 2)


def panel(surf, rect, title=""):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, PANEL, r, border_radius=10)
    pygame.draw.rect(surf, EDGE, r, 1, border_radius=10)
    pygame.draw.line(surf, (111, 91, 77), (r.x + 12, r.y + 1), (r.right - 12, r.y + 1))
    if title:
        txt(surf, " ".join(title.upper()), 12, ACCENT_DIM, (r.x + 16, r.y + 8), bold=True)
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (17, 12, 10), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (101, 83, 70), (sx, r.bottom - 12), 4, 1)


def lcd_box(surf, rect):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, (6, 3, 0), r.inflate(4, 4), border_radius=6)
    pygame.draw.rect(surf, LCD_BG, r, border_radius=5)
    for y in range(r.y + 2, r.bottom - 2, 3):
        pygame.draw.line(surf, (24, 12, 3), (r.x + 2, y), (r.right - 3, y))
    pygame.draw.rect(surf, (86, 44, 13), r, 1, border_radius=5)


# =============================================================================
# WIDGETS
# =============================================================================
class Button:
    def __init__(self, rect, label, cb):
        self.rect = pygame.Rect(rect)
        self.label, self.cb = label, cb

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3) and self.rect.collidepoint(e.pos):
            self.cb(e.button)
            return True
        return False

    def draw(self, surf, app, label=None, active=False):
        r = self.rect
        hover = r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(surf, (12, 9, 7), r.inflate(4, 4), border_radius=8)
        fill = (70, 36, 11) if active else ((54, 28, 8) if hover else (49, 40, 33))
        pygame.draw.rect(surf, fill, r, border_radius=6)
        pygame.draw.rect(surf, ACCENT if (hover or active) else (101, 83, 70), r, 1, border_radius=6)
        s = label or self.label
        txt(surf, s, fit_size(s, r.w - 14, (15, 14, 13, 12, 11, 10), bold=True),
            ACCENT if (hover or active) else TEXT, r.center, "center", bold=True)


class TextBox:
    def __init__(self, rect):
        self.rect = pygame.Rect(rect)
        self.text = ""
        self.focus = False

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            self.focus = self.rect.collidepoint(e.pos)
            return self.focus
        if e.type == pygame.KEYDOWN and self.focus:
            if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                app.save_setup()
            elif e.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif e.key == pygame.K_ESCAPE:
                self.focus = False
            elif e.unicode and e.unicode.isprintable() and len(self.text) < 40:
                self.text += e.unicode
            return True
        return False

    def draw(self, surf, app):
        r = self.rect
        pygame.draw.rect(surf, (10, 7, 6), r, border_radius=5)
        pygame.draw.rect(surf, ACCENT if self.focus else (101, 83, 70), r, 1, border_radius=5)
        if not self.text and not self.focus:
            txt(surf, "name this setup...", 14, (103, 84, 71), (r.x + 8, r.centery), "midleft")
        else:
            shown = self.text + ("_" if self.focus and int(time.time() * 2) % 2 == 0 else "")
            txt(surf, shown[-24:], 15, TEXT, (r.x + 8, r.centery), "midleft")


class Popup:
    def __init__(self, title, options, current, on_pick, rows=12, row_h=30, fsize=16):
        self.title, self.options, self.current, self.on_pick = title, options, current, on_pick
        self.fsize = fsize
        cols = math.ceil(len(options) / rows)
        colw = min(420, max(font(fsize).size(lab)[0] for _, lab in options) + 40)
        nrows = min(rows, len(options))
        self.panel = pygame.Rect(0, 0, cols * colw + 24, nrows * row_h + 62)
        self.panel.center = (W // 2, H // 2)
        self.items = []
        for i, (val, lab) in enumerate(options):
            c, rw = divmod(i, rows)
            self.items.append((pygame.Rect(self.panel.x + 12 + c * colw, self.panel.y + 46 + rw * row_h,
                                           colw - 4, row_h - 2), val, lab))

    def handle(self, e, app):
        if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
            app.popup = None
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for r, val, _ in self.items:
                if r.collidepoint(e.pos):
                    app.popup = None
                    self.on_pick(val)
                    return True
            if not self.panel.collidepoint(e.pos):
                app.popup = None
        return True  # modal

    def draw(self, surf, app):
        dim = pygame.Surface((W, H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 175))
        surf.blit(dim, (0, 0))
        pygame.draw.rect(surf, (25, 20, 16), self.panel, border_radius=12)
        pygame.draw.rect(surf, ACCENT_DIM, self.panel, 1, border_radius=12)
        txt(surf, " ".join(self.title.upper()), 13, ACCENT, (self.panel.x + 18, self.panel.y + 16), bold=True)
        mouse = pygame.mouse.get_pos()
        for r, val, lab in self.items:
            if r.collidepoint(mouse):
                pygame.draw.rect(surf, (70, 36, 11), r, border_radius=4)
            if val == self.current:
                pygame.draw.rect(surf, ACCENT_DIM, r, 1, border_radius=4)
            txt(surf, lab, self.fsize, ACCENT if val == self.current else TEXT,
                (r.x + 8, r.centery), "midleft")


class Strip:
    """One channel strip. Always shows part (tab * 16 + i)."""

    def __init__(self, i, x):
        self.i, self.x, self.w = i, x, 83
        self.cx = x + 41
        self.lcd = pygame.Rect(x + 4, 150, 75, 66)
        self.bminus = pygame.Rect(x + 4, 222, 20, 20)
        self.bplus = pygame.Rect(x + 59, 222, 20, 20)
        self.pan = (self.cx, 276, 16)
        self.sends = [("rev", self.cx - 17, 348, "REV"), ("cho", self.cx - 17, 394, "CHO"),
                      ("dly", self.cx - 17, 440, "DLY")]
        self.groove = pygame.Rect(self.cx - 5, 482, 10, 236)

    def part(self, app):
        return app.tab * 16 + self.i

    def knobs(self):
        yield "pan", self.pan[0], self.pan[1], self.pan[2]
        for f, cx, cy, _ in self.sends:
            yield f, cx, cy, 11

    def handle(self, e, app):
        p = self.part(app)
        drum = is_drum(p)
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            if e.button == 1 and self.lcd.collidepoint(e.pos):
                app.open_instrument_popup(p)
                return True
            if e.button == 1 and pygame.Rect(self.cx - 26, 130, 52, 18).collidepoint(e.pos):
                app.edit_part = p
                app.set_tab(3)
                return True
            if not drum:
                if self.bminus.collidepoint(e.pos):
                    app.change_bank(p, -1 if e.button == 1 else -10)
                    return True
                if self.bplus.collidepoint(e.pos):
                    app.change_bank(p, 1 if e.button == 1 else 10)
                    return True
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for f, cx, cy, R in self.knobs():
                if math.hypot(e.pos[0] - cx, e.pos[1] - cy) <= R + 9:
                    app.begin_drag(p, f, "knob")
                    return True
            if pygame.Rect(self.cx - 20, self.groove.y - 10, 40, self.groove.h + 20).collidepoint(e.pos):
                if not app.begin_drag(p, "vol", "fader"):
                    app.drag_fader(e.pos[1], self.groove)
                app.drag_groove = self.groove
                return True
        if e.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            step = 1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 3
            if self.lcd.collidepoint((mx, my)):
                app.change_program(p, -e.y)
                return True
            for f, cx, cy, R in self.knobs():
                if math.hypot(mx - cx, my - cy) <= R + 9:
                    app.set_part(p, f, app.parts[p][f] + e.y * step)
                    return True
            if pygame.Rect(self.cx - 20, self.groove.y - 10, 40, self.groove.h + 20).collidepoint((mx, my)):
                app.set_part(p, "vol", app.parts[p]["vol"] + e.y * step)
                return True
        return False

    def draw(self, surf, app):
        p = self.part(app)
        st = app.parts[p]
        drum = is_drum(p)
        x, cx = self.x, self.cx
        pygame.draw.line(surf, (53, 43, 36), (x + self.w, 128), (x + self.w, 780))
        txt(surf, part_name(p), 15, ACCENT if drum else TEXT, (cx, 134), "center", bold=True)
        if drum:
            txt(surf, "DRUM", 10, ACCENT, (cx, 146), "center", bold=True)

        # instrument LCD
        lcd_box(surf, self.lcd)
        num = f"{st['prog'] + 1:03d}"
        txt(surf, num, 12, ACCENT, (self.lcd.x + 5, self.lcd.y + 4), bold=True)
        if not drum:
            txt(surf, f"B{st['bank']:03d}", 10, ACCENT_DIM if st["bank"] == 0 else AMBER,
                (self.lcd.right - 5, self.lcd.y + 6), "topright", bold=True)
        name = app.instrument_name(p)
        for k, line in enumerate(wrap(name, 11, self.lcd.w - 10, lines=3)):
            txt(surf, line, 11, ACCENT if st["bank"] == 0 or drum else AMBER,
                (self.lcd.x + 5, self.lcd.y + 20 + k * 13), bold=True)

        # bank buttons
        if not drum:
            for r, s in ((self.bminus, "-"), (self.bplus, "+")):
                hover = r.collidepoint(pygame.mouse.get_pos())
                pygame.draw.rect(surf, (54, 28, 8) if hover else (49, 40, 33), r, border_radius=4)
                pygame.draw.rect(surf, ACCENT if hover else (101, 83, 70), r, 1, border_radius=4)
                txt(surf, s, 15, ACCENT if hover else TEXT, r.center, "center", bold=True)
            txt(surf, "BANK", 10, DIM, (cx, 232), "center")

        # pan + sends
        pcx, pcy, pR = self.pan
        draw_knob(surf, pcx, pcy, pR, st["pan"] / 127.0)
        pv = st["pan"]
        pan_txt = "C" if pv == 64 else (f"L{64 - pv}" if pv < 64 else f"R{pv - 64}")
        txt(surf, "PAN", 10, DIM, (cx, pcy + pR + 14), "center")
        txt(surf, pan_txt, 12, ACCENT, (cx, pcy + pR + 28), "center", bold=True)
        for f, kx, ky, lab in self.sends:
            draw_knob(surf, kx, ky, 11, st[f] / 127.0)
            txt(surf, lab, 10, DIM, (kx + 22, ky - 12))
            txt(surf, str(st[f]), 12, ACCENT, (kx + 22, ky + 1), bold=True)

        # fader
        g = self.groove
        pygame.draw.rect(surf, (12, 9, 7), g.inflate(8, 8), border_radius=6)
        pygame.draw.rect(surf, (42, 34, 28), g, border_radius=4)
        t = st["vol"] / 127.0
        hy = int(g.bottom - t * g.h)
        if t > 0:
            pygame.draw.rect(surf, ACCENT_DIM, pygame.Rect(g.x, hy, g.w, g.bottom - hy), border_radius=4)
        for k in range(0, 128, 16):
            ty = int(g.bottom - (k / 127.0) * g.h)
            pygame.draw.line(surf, (79, 64, 54), (g.x - 12, ty), (g.x - 7, ty))
            pygame.draw.line(surf, (79, 64, 54), (g.right + 7, ty), (g.right + 12, ty))
        cap = pygame.Rect(0, 0, 38, 18)
        cap.center = (cx, hy)
        pygame.draw.rect(surf, (15, 10, 9), cap.inflate(4, 4), border_radius=5)
        pygame.draw.rect(surf, (90, 73, 62), cap, border_radius=4)
        pygame.draw.rect(surf, (138, 113, 96), pygame.Rect(cap.x, cap.y, cap.w, 6), border_radius=4)
        pygame.draw.line(surf, ACCENT, (cap.x + 5, cap.centery + 2), (cap.right - 5, cap.centery + 2), 2)
        txt(surf, str(st["vol"]), 14, ACCENT, (cx, 742), "center", bold=True)
        txt(surf, "VOL", 10, DIM, (cx, 760), "center")


# =============================================================================
# THE APP
# =============================================================================
class App:
    def __init__(self, headless=False):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("SC-88Pro CONTROL")
        self.data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        self.gm = self.data["gm"]
        self.kits = {int(k): v for k, v in self.data["drum_kits"].items()}

        self.parts = new_parts()
        self.touched = set()
        self.edit_part = 0
        self.efx = {int(k): v for k, v in self.data.get("efx", {}).items()}
        self.efx_by_code = {(v["msb"], v["lsb"]): int(k) for k, v in self.data.get("efx", {}).items()}
        self.patches = self.data.get("patches", [])
        self.ins = self.new_ins()
        self.drum = {}          # (port, map, note) -> {param: value}   (what we know)
        self.drum_dirty = set() # (port, map, note, param) edited by the user
        self.drum_sel = {"port": 0, "map": 0, "note": 38}
        self.kit_names = {}     # (port, prog) -> {note: name}
        self.master = MASTER_DEFAULT
        self.fx = dict(FX_DEFAULTS)
        self.tones = {int(k): {int(c): n for c, n in v.items()} for k, v in self.data.get("tones", {}).items()}
        self.tab = 0
        self.running = True
        self.drag = None
        self.drag_groove = None
        self.last_click = {}
        self.popup = None
        self.status = "UI shows GS defaults. Nothing is sent until you touch a control."
        self.status_t = time.time()
        self.last_msg = ""
        self.flash_out = 0.0
        self.flash_in = 0.0
        self.inp = None
        self.in_lock = threading.Lock()
        self._sx = None
        self.reading = False
        self.rx = {}
        self.rx_names = []

        # MIDI devices: one output per SC-88Pro port
        self.outs = [None, None]
        self.out_lock = threading.Lock()
        self.settings = {}
        try:
            self.settings = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
        if pgmidi:
            try:
                pgmidi.init()
            except Exception as ex:
                self.set_status(f"MIDI unavailable: {ex}")
        self.refresh_devices()
        for port in (0, 1):
            name = self.settings.get(f"out{'AB'[port]}")
            if name:
                self.open_device(port, name)

        # sender thread
        self.q = deque()
        self.qlock = threading.Lock()
        threading.Thread(target=self._sender_loop, daemon=True).start()
        threading.Thread(target=self._input_loop, daemon=True).start()
        if self.settings.get("in"):
            self.open_input(self.settings["in"])

        self.bg = self._make_bg()
        self.setups = []
        self.setup_off = 0
        self.refresh_setups()
        self.build_ui()

    # ---- status -------------------------------------------------------------
    def set_status(self, s):
        self.status, self.status_t = s, time.time()

    # ---- MIDI devices -------------------------------------------------------
    def refresh_devices(self):
        """List every MIDI output. Ports of one interface can share a name, so each device
        gets a key of 'name#n' (n = which one of that name), and the list shows its index."""
        self.out_devs = []  # (index, name, key)
        self.in_devs = []
        if not pgmidi:
            return
        seen, seen_in = {}, {}
        for i in range(pgmidi.get_count()):
            _, name, is_in, is_out, _ = pgmidi.get_device_info(i)
            name = name.decode(errors="replace")
            if is_out:
                n = seen.get(name, 0)
                seen[name] = n + 1
                self.out_devs.append((i, name, f"{name}#{n}"))
            if is_in:
                n = seen_in.get(name, 0)
                seen_in[name] = n + 1
                self.in_devs.append((i, name, f"{name}#{n}"))
        print("MIDI outputs:")
        for i, name, key in self.out_devs:
            print(f"  [{i}] {name}")
        print("MIDI inputs:")
        for i, name, key in self.in_devs:
            print(f"  [{i}] {name}")

    def device_label(self, key, inputs=False):
        if not key:
            return "none  (click to choose)"
        for i, name, k in (self.in_devs if inputs else self.out_devs):
            if k == key or (("#" not in key) and k == key + "#0"):
                return f"[{i}] {name}"
        return f"{key.split('#')[0]}  (not found)"

    def open_device(self, port, key):
        with self.out_lock:
            if self.outs[port]:
                try:
                    self.outs[port].close()
                except Exception:
                    pass
                self.outs[port] = None
            if key is None:
                return
            for i, name, k in self.out_devs:
                if k == key or (("#" not in key) and k == key + "#0"):
                    try:
                        self.outs[port] = pgmidi.Output(i)
                        self.set_status(f"Port {'AB'[port]} output is now [{i}] {name}")
                    except Exception as ex:
                        self.set_status(f"Could not open [{i}] {name}: {ex} "
                                        f"(is another program using it?)")
                    return
            self.set_status(f"Port {'AB'[port]}: saved device '{key}' not found - choose again.")

    def choose_device(self, port, _button=1):
        self.refresh_devices()
        opts = [(None, "None  (disconnect)")] + [(k, f"[{i}] {n}") for i, n, k in self.out_devs]
        self.popup = Popup(f"Port {'AB'[port]} output", opts, self.settings.get(f"out{'AB'[port]}"),
                           lambda k, port=port: self.set_device(port, k), rows=14, row_h=28, fsize=15)

    def open_input(self, key):
        with self.in_lock:
            if self.inp:
                try:
                    self.inp.close()
                except Exception:
                    pass
                self.inp = None
            if key is None or not pgmidi:
                return
            for i, name, k in self.in_devs:
                if k == key or (("#" not in key) and k == key + "#0"):
                    try:
                        self.inp = pgmidi.Input(i)
                        self.set_status(f"MIDI input is now [{i}] {name}")
                    except Exception as ex:
                        self.set_status(f"Could not open input [{i}] {name}: {ex} "
                                        f"(is another program using it?)")
                    return
            self.set_status(f"Saved input '{key}' not found - choose again.")

    def choose_input(self, _b=1):
        self.refresh_devices()
        opts = [(None, "None  (disconnect)")] + [(k, f"[{i}] {n}") for i, n, k in self.in_devs]
        self.popup = Popup("Input from the SC-88Pro (4x4 input C)", opts, self.settings.get("in"),
                           self.set_input, rows=14, row_h=28, fsize=15)

    def set_input(self, key):
        self.settings["in"] = key
        self.open_input(key)
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    # ---- receiving ------------------------------------------------------------
    def _input_loop(self):
        while True:
            evs = None
            with self.in_lock:
                inp = self.inp
                if inp is not None:
                    try:
                        if inp.poll():
                            evs = inp.read(256)
                    except Exception:
                        evs = None
            if not evs:
                time.sleep(0.003)
                continue
            for data, _ts in evs:
                self._rx_event(data)

    def _rx_event(self, data):
        """pygame hands sysex over as 4-byte chunks; active sensing etc. can be interleaved."""
        b0 = data[0]
        if self._sx is None and b0 != 0xF0:
            return
        if self._sx is not None and b0 >= 0xF8:
            return
        for b in data[:4]:
            if b == 0xF0:
                self._sx = []
            elif self._sx is None:
                return
            elif b == 0xF7:
                msg, self._sx = self._sx, None
                self.on_sysex(msg)
                return
            elif b >= 0x80:
                self._sx = None
                return
            else:
                self._sx.append(b)

    def on_sysex(self, m):
        self.flash_in = time.time()
        # 41 dev 42 12 a1 a2 a3 data... sum
        if len(m) < 9 or m[0] != 0x41 or m[2] != 0x42 or m[3] != 0x12:
            return
        body = m[4:-1]
        if (sum(body) + m[-1]) % 128 != 0:
            return
        addr = (body[0] << 16) | (body[1] << 8) | body[2]
        if addr == 0x0C0003:
            self.rx_names.append(list(body[3:]))
            return
        self.rx[addr] = list(body[3:])

    def set_device(self, port, key):
        self.settings[f"out{'AB'[port]}"] = key
        self.open_device(port, key)
        other = self.settings.get(f"out{'BA'[port]}")
        if key and key == other:
            self.set_status("Warning: Port A and Port B are set to the SAME output. "
                            "Pick a different 4x4 output for each.")
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    # ---- sending ------------------------------------------------------------
    def write_midi(self, port, ch, kind, a, b):
        with self.out_lock:
            out = self.outs[port]
            if out:
                try:
                    if kind == "cc":
                        out.write_short(0xB0 | ch, a, b)
                    elif kind == "pc":
                        out.write_short(0xC0 | ch, a)
                    else:
                        out.write_sys_ex(0, list(a))
                except Exception as ex:
                    self.status = f"MIDI write error: {ex}"
        tag = "AB"[port]
        if kind == "cc":
            self.last_msg = f"OUT  port {tag}  ch {ch + 1:>2}  CC{a} = {b}"
        elif kind == "pc":
            self.last_msg = f"OUT  port {tag}  ch {ch + 1:>2}  Program {a + 1}"
        else:
            self.last_msg = f"OUT  port {tag}  SysEx ({len(a)} bytes)"
        self.flash_out = time.time()

    def _sender_loop(self):
        while True:
            item = None
            with self.qlock:
                if self.q:
                    item = self.q.popleft()
            if item is None:
                time.sleep(0.002)
                continue
            port, ch, kind, a, b, pause, _grp = item
            self.write_midi(port, ch, kind, a, b)
            time.sleep(GAP + pause)

    def send_live(self, port, ch, kind, a, b):
        """Drags: newest value replaces an older queued one for the same controller."""
        with self.qlock:
            for i, m in enumerate(self.q):
                if m[6] == "live" and m[0] == port and m[1] == ch and m[2] == kind and m[3] == a:
                    self.q[i] = (port, ch, kind, a, b, m[5], "live")
                    return
            self.q.append((port, ch, kind, a, b, 0.0, "live"))

    def send_seq(self, items, group):
        """A group replaces any not-yet-sent items of the same group."""
        with self.qlock:
            if group:
                self.q = deque(m for m in self.q if m[6] != group)
            for port, ch, kind, a, b, pause in items:
                self.q.append((port, ch, kind, a, b, pause, group))

    # ---- part state ---------------------------------------------------------
    def set_part(self, p, field, v):
        if field in EXT:
            e = EXT[field]
            v = max(e[4], min(e[5], int(v)))
            if self.parts[p][field] != v:
                self.parts[p][field] = v
                self.touched.add((p, field))
                self.send_seq([(p // 16, 0, "sx", ext_message(p, field, v), 0, 0.0)], f"pe{p}{field}")
            return
        v = max(0, min(127, int(v)))
        if self.parts[p][field] != v:
            self.parts[p][field] = v
            port, ch = divmod(p, 16)
            self.send_live(port, ch, "cc", FIELD_CC[field], v)

    def set_master(self, v):
        v = max(0, min(127, int(v)))
        if v != self.master:
            self.master = v
            self.send_seq([(port, 0, "sx", master_volume(v), 0, 0.0) for port in (0, 1)], "master")

    def new_ins(self):
        ins = {"type": 0, "vals": {}, "touched": False}
        ins.update(INS_SEND_DEFAULT)
        return ins

    def efx_type_from_codes(self, msb, lsb):
        return self.efx_by_code.get((msb, lsb))

    def ins_meta(self, addr):
        t = self.efx.get(self.ins["type"])
        for prm in (t["params"] if t else []):
            if prm["addr"] == addr:
                return prm, efx_meta(prm)
        return None, None

    def param_range(self, p, f):
        if p == -2:
            return tuple(FX_PARAMS[f][3:5])
        if p == -3:
            if f[0] == "p":
                prm, meta = self.ins_meta(f[1])
                return (meta["lo"], meta["hi"]) if meta else (0, 127)
            return (0, 1) if f[1] == "eq" else (0, 127)
        if p >= 0 and f in EXT:
            return tuple(EXT[f][4:6])
        return (0, 127)

    def get_val(self, p, f):
        if p == -3:
            if f[0] == "p":
                prm, meta = self.ins_meta(f[1])
                return self.ins["vals"].get(f[1], efx_default(prm, meta) if meta else 0)
            return self.ins[f[1]]
        if p == -4:
            port, m, note, name = f
            return self.drum.get((port, m, note), {}).get(name, 64)
        if p == -2:
            return self.fx[f]
        return self.master if p < 0 else self.parts[p][f]

    def set_fx(self, key, v, resend_children=False):
        _k, label, addr, lo, hi, _d, kind, _n = FX_PARAMS[key]
        v = max(lo, min(hi, int(v)))
        if self.fx[key] == v and not resend_children:
            return
        self.fx[key] = v
        seq = [(0, 0, "sx", fx_message(key, v), 0, 0.012 if resend_children else 0.0)]
        if resend_children:  # a new type resets that block in the unit: put our values back
            seq += [(0, 0, "sx", fx_message(k, self.fx[k]), 0, 0.0) for k in FX_MACRO_CHILDREN[key]]
        self.send_seq(seq, f"fx{key}")

    def open_fx_choice(self, key):
        p = FX_PARAMS[key]
        opts = [(i, n) for i, n in enumerate(p[7])]
        self.popup = Popup(p[1] if p[1] != "TYPE" else "Type", opts, self.fx[key],
                           lambda v, key=key: self.set_fx(key, v, key in FX_MACRO_CHILDREN), rows=10,
                           row_h=30, fsize=16)

    def set_ins(self, f, v):
        lo, hi = self.param_range(-3, f)
        v = max(lo, min(hi, int(v)))
        self.ins["touched"] = True
        if f[0] == "p":
            if self.ins["vals"].get(f[1]) == v:
                return
            self.ins["vals"][f[1]] = v
            self.send_seq([(0, 0, "sx", roland_dt1(INS_BASE | f[1], [v]), 0, 0.0)], f"ins{f[1]}")
        else:
            if self.ins[f[1]] == v:
                return
            self.ins[f[1]] = v
            self.send_seq([(0, 0, "sx", roland_dt1(INS_SEND_ADDR[f[1]], [v]), 0, 0.0)], f"ins{f[1]}")

    def set_ins_type(self, n, ask_unit=True):
        t = self.efx.get(n)
        if t is None:
            return
        self.ins["type"], self.ins["touched"] = n, True
        self.ins["vals"] = {}
        for prm in t["params"]:
            meta = efx_meta(prm)
            self.ins["vals"][prm["addr"]] = efx_default(prm, meta)
        self.send_seq([(0, 0, "sx", roland_dt1(INS_BASE, [t["msb"], t["lsb"]]), 0, 0.02)], "instype")
        if ask_unit and self.inp is not None and not self.reading:
            threading.Thread(target=self._read_ins_worker, daemon=True).start()
        elif self.inp is None:
            self.set_status("Type sent. Values are estimates until you READ UNIT.")

    def _read_ins_worker(self):
        time.sleep(0.25)
        self.reading = True
        try:
            t = self.efx.get(self.ins["type"])
            wanted = [(INS_BASE | p["addr"], 1) for p in t["params"]] + [(a, 1) for a in INS_SEND_ADDR.values()]
            got = self.request_values(0, wanted, "Reading insertion fx")
            self.apply_ins_read(got, with_type=False)
            self.set_status(f"Insertion effect: {t['name']} - values read from the unit.")
        finally:
            self.reading = False

    def apply_ins_read(self, rx, with_type=True):
        if with_type and rx.get(INS_BASE):
            d = rx[INS_BASE]
            typ = self.efx_type_from_codes(d[0], d[1])
            if typ is not None:
                self.ins["type"] = typ
                self.ins["touched"] = typ != 0 or self.ins.get("touched", False)
        t = self.efx.get(self.ins["type"])
        for prm in (t["params"] if t else []):
            d = rx.get(INS_BASE | prm["addr"])
            if d:
                self.ins["vals"][prm["addr"]] = d[0]
        for k, a in INS_SEND_ADDR.items():
            d = rx.get(a)
            if d:
                self.ins[k] = d[0]

    def drum_dirty_values(self):
        out = {}
        for (port, m, note, name) in sorted(self.drum_dirty):
            out.setdefault((port, m, note), {})[name] = self.drum[(port, m, note)][name]
        return out

    def set_drum(self, key, v):
        port, m, note, name = key
        v = max(0, min(127 if name not in ("rxon", "rxoff") else 1, int(v)))
        cur = self.drum.setdefault((port, m, note), {})
        if cur.get(name) == v:
            return
        cur[name] = v
        self.drum_dirty.add(key)
        self.send_seq([(port, 0, "sx", drum_message(m, note, name, v), 0, 0.0)], f"dr{key}")

    def set_val(self, p, f, v):
        if p == -3:
            self.set_ins(f, v)
        elif p == -4:
            self.set_drum(f, v)
        elif p == -2:
            self.set_fx(f, v)
        elif p < 0:
            self.set_master(v)
        else:
            self.set_part(p, f, v)

    def default_val(self, p, f):
        if p == -3:
            if f[0] == "p":
                prm, meta = self.ins_meta(f[1])
                return efx_default(prm, meta) if meta else 0
            return INS_SEND_DEFAULT[f[1]]
        if p == -4:
            return 127 if f[3] == "level" else (64 if f[3] in ("pan", "pitch") else 0)
        if p == -2:
            return FX_DEFAULTS[f]
        return MASTER_DEFAULT if p < 0 else part_default(p)[f]

    def send_instrument(self, p):
        port, ch = divmod(p, 16)
        st = self.parts[p]
        seq = []
        if not is_drum(p):
            seq.append((port, ch, "cc", 0, st["bank"], 0.0))
        seq.append((port, ch, "pc", st["prog"], 0, 0.006))
        self.send_seq(seq, f"inst{p}")

    def change_program(self, p, d):
        self.parts[p]["prog"] = (self.parts[p]["prog"] + d) % 128
        self.send_instrument(p)

    def change_bank(self, p, d):
        """Step through the variations that really exist for this instrument."""
        var = sorted(self.tones.get(self.parts[p]["prog"], {}))
        cur = self.parts[p]["bank"]
        if var:
            idx = var.index(cur) if cur in var else max(0, sum(1 for v in var if v < cur) - 1)
            if cur not in var and d > 0:
                idx = sum(1 for v in var if v < cur) - 1
            idx = max(0, min(len(var) - 1, idx + (1 if d > 0 else -1) * (1 if abs(d) == 1 else 5)))
            self.parts[p]["bank"] = var[idx]
        else:
            self.parts[p]["bank"] = max(0, min(127, cur + d))
        self.send_instrument(p)

    def pick_program(self, p, prog):
        self.parts[p]["prog"] = prog
        self.send_instrument(p)

    def instrument_name(self, p):
        st = self.parts[p]
        if is_drum(p):
            return self.kits.get(st["prog"], f"Drum Set {st['prog']}")
        return self.tone_name(st["prog"], st["bank"])

    def tone_name(self, prog, bank):
        var = self.tones.get(prog, {})
        if bank in var:
            return var[bank]
        base = var.get(0, self.gm[prog])
        return base + (f" ?{bank}" if bank else "")

    def open_instrument_popup(self, p):
        if is_drum(p):
            opts = [(k, f"{k:03d}  {self.kits.get(k, 'Drum Set ' + str(k))}") for k in range(128)
                    if k in self.kits]
            self.popup = Popup(f"Drum kit  -  part {part_name(p)}", opts, self.parts[p]["prog"],
                               lambda v, p=p: self.pick_program(p, v), rows=12, row_h=30, fsize=16)
        else:
            opts = [(k, f"{k + 1:03d}  {self.tones.get(k, {}).get(0, self.gm[k])}"
                        + (f"  (+{len(self.tones.get(k, {})) - 1})" if len(self.tones.get(k, {})) > 1 else ""))
                    for k in range(128)]
            self.popup = Popup(f"Instrument  -  part {part_name(p)}   (+n = variations)", opts,
                               self.parts[p]["prog"],
                               lambda v, p=p: self.pick_family(p, v), rows=32, row_h=22, fsize=14)

    def pick_family(self, p, prog):
        var = self.tones.get(prog, {})
        if len(var) <= 1:
            self.parts[p]["bank"] = 0
            self.pick_program(p, prog)
            return
        opts = [(b, f"{b:03d}  {n}") for b, n in sorted(var.items())]
        self.popup = Popup(f"Variation  -  {var.get(0, self.gm[prog])}", opts,
                           self.parts[p]["bank"] if self.parts[p]["prog"] == prog else 0,
                           lambda b, p=p, prog=prog: self.pick_variation(p, prog, b),
                           rows=16, row_h=26, fsize=15)

    def pick_variation(self, p, prog, bank):
        self.parts[p]["bank"] = bank
        self.pick_program(p, prog)

    # ---- dragging -----------------------------------------------------------
    def begin_drag(self, p, field, kind):
        """Returns True if this click was a double-click (value reset)."""
        now = time.time()
        key = (p, field)
        if now - self.last_click.get(key, 0.0) < 0.35:
            self.set_val(p, field, self.default_val(p, field))
            self.last_click[key] = 0.0
            return True
        self.last_click[key] = now
        self.drag = {"p": p, "f": field, "kind": kind, "acc": float(self.get_val(p, field))}
        return False

    def drag_fader(self, y, groove):
        if self.drag is None:
            return
        t = (groove.bottom - y) / float(groove.h)
        self.set_val(self.drag["p"], self.drag["f"], round(127 * max(0.0, min(1.0, t))))

    def drag_motion(self, e):
        d = self.drag
        if d["kind"] == "knob":
            fine = pygame.key.get_mods() & pygame.KMOD_SHIFT
            lo, hi = self.param_range(d["p"], d["f"])
            sc = max(0.08, (hi - lo) / 127.0)
            d["acc"] = max(float(lo), min(float(hi), d["acc"] - e.rel[1] * (0.25 if fine else 0.9) * sc
                                          * (3 if hi - lo < 20 else 1)))
            self.set_val(d["p"], d["f"], int(round(d["acc"])))
        elif self.drag_groove is not None:
            self.drag_fader(e.pos[1], self.drag_groove)

    # ---- actions ------------------------------------------------------------
    def gs_reset(self, _b=1):
        self.send_seq([(port, 0, "sx", GS_RESET, 0, 0.18) for port in (0, 1)], "reset")
        self.parts = new_parts()
        self.touched = set()
        self.master = MASTER_DEFAULT
        self.fx = dict(FX_DEFAULTS)
        self.ins = self.new_ins()
        self.drum, self.drum_dirty = {}, set()
        self.set_status("GS Reset sent to both ports. Screen back to GS defaults.")

    def send_all(self, _b=1):
        self.send_seq(build_setup_sequence(self.parts, self.master, self.fx, self.touched, self.ins, self.efx, self.drum_dirty_values()), "load")
        self.set_status("Whole setup sent to the unit (bank, program, then levels, part by part).")

    # ---- read the unit back -------------------------------------------------
    def read_unit(self, _b=1):
        if self.reading:
            return
        if self.inp is None:
            self.set_status("Choose the IN device first (4x4 input C, where the SC-88Pro's MIDI OUT goes).")
            return
        if self.outs[0] is None:
            self.set_status("Choose the Port A output first.")
            return
        with self.qlock:
            self.q.clear()
        threading.Thread(target=self._read_worker, daemon=True).start()

    def request_values(self, port, wanted, label="Reading"):
        """Ask the unit for [(address, size)] and wait for the answers. Returns {address: data}."""
        self.rx = {}
        total = len(wanted)
        for attempt in range(3):
            missing = [w for w in wanted if w[0] not in self.rx]
            if not missing:
                break
            for i in range(0, len(missing), 30):
                chunk = missing[i:i + 30]
                for addr, size in chunk:
                    self.write_midi(port, 0, "sx", rq1(addr, size), 0)
                    time.sleep(0.008)
                t0 = last = time.time()
                n0 = len(self.rx)
                while time.time() - t0 < 1.5 and any(a not in self.rx for a, _ in chunk):
                    time.sleep(0.01)
                    if len(self.rx) != n0:
                        n0, last = len(self.rx), time.time()
                    elif time.time() - last > 0.25:
                        break  # nothing new for a while: the rest were not answered
                n_ok = total - len([w for w in wanted if w[0] not in self.rx])
                self.set_status(f"{label} port {'AB'[port]}...  {n_ok}/{total}")
        return dict(self.rx)

    def _read_worker(self):
        self.reading = True
        try:
            result = {0: {}, 1: {}}
            for port in (0, 1):
                if self.outs[port] is None:
                    continue
                wanted = []
                if port == 0:
                    wanted += [(FX_PARAMS[k][2], 1) for k in FX_PARAMS]
                    wanted += [(0x400300, 2)] + [(a, 1) for a in INS_SEND_ADDR.values()]
                for p in range(port * 16, port * 16 + 16):
                    for name, mid, low, n in PART_READ:
                        wanted.append((part_addr(p, mid, low), n))
                result[port] = self.request_values(port, wanted)
                if port == 0 and result[0].get(0x400300):
                    t = result[0][0x400300]
                    typ = self.efx_type_from_codes(t[0], t[1])
                    if typ is not None and typ in self.efx:
                        more = self.request_values(0, [(0x400300 | p["addr"], 1) for p in self.efx[typ]["params"]],
                                                   "Reading insertion fx")
                        result[0].update(more)
                time.sleep(0.3)
            self._apply_read(result)
        finally:
            self.reading = False

    def _apply_read(self, result):
        got = miss = 0
        for port in (0, 1):
            rx = result[port]
            if not rx:
                continue
            if port == 0:
                self.apply_ins_read(rx)
                for k, pr in FX_PARAMS.items():
                    d = rx.get(pr[2])
                    if d:
                        self.fx[k] = max(pr[3], min(pr[4], d[0]))
                        got += 1
                    else:
                        miss += 1
            for p in range(port * 16, port * 16 + 16):
                part = self.parts[p]
                for name, mid, low, n in PART_READ:
                    d = rx.get(part_addr(p, mid, low))
                    if not d or len(d) < n:
                        miss += 1
                        continue
                    got += 1
                    if name == "tone":
                        part["bank"], part["prog"] = d[0], d[1]
                    elif name == "ftune":
                        part[name] = max(0, min(127, round(64 + ((d[0] << 7) + d[1] - 8192) * 64 / 8191)))
                    elif name in EXT:
                        e = EXT[name]
                        part[name] = max(e[4], min(e[5], d[0]))
                        if part[name] != part_default(p)[name]:
                            self.touched.add((p, name))
                    else:
                        part[name] = d[0]
        tail = "" if not miss else f"  ({miss} values did not answer - check the cables / OUT-THRU switch)"
        self.set_status(f"Read {got} values from the unit.{tail}")

    # ---- setups (files) -----------------------------------------------------
    def refresh_setups(self):
        SETUPS_DIR.mkdir(exist_ok=True)
        files = sorted(SETUPS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        self.setups = [p.stem for p in files]

    def save_setup(self, _b=1):
        name = re.sub(r"[^A-Za-z0-9 _.-]", "_", self.textbox.text).strip()
        if not name:
            self.set_status("Type a name first.")
            return
        SETUPS_DIR.mkdir(exist_ok=True)
        (SETUPS_DIR / f"{name}.json").write_text(json.dumps(
            {"version": 3, "name": name, "master": self.master, "fx": self.fx, "parts": self.parts,
             "ins": {"type": self.ins["type"], "vals": {str(k): v for k, v in self.ins["vals"].items()},
                     "rev": self.ins["rev"], "cho": self.ins["cho"], "dly": self.ins["dly"], "eq": self.ins["eq"]},
             "drum": {f"{'AB'[p]}{m + 1}:{n}": v for (p, m, n), v in self.drum_dirty_values().items()}},
            indent=2))
        self.refresh_setups()
        self.setup_off = 0
        self.set_status(f"Saved setup '{name}'.")

    def load_setup(self, name):
        try:
            d = json.loads((SETUPS_DIR / f"{name}.json").read_text())
            parts = []
            for i in range(32):
                src = d["parts"][i] if i < len(d["parts"]) else {}
                part = part_default(i)
                for k in part:
                    if k in src:
                        part[k] = max(0, min(127, int(src[k])))
                parts.append(part)
            master = max(0, min(127, int(d.get("master", MASTER_DEFAULT))))
            fx = dict(FX_DEFAULTS)
            for k, v in d.get("fx", {}).items():
                if k in FX_PARAMS:
                    fx[k] = max(FX_PARAMS[k][3], min(FX_PARAMS[k][4], int(v)))
            ins = self.new_ins()
            di = d.get("ins")
            if di and int(di.get("type", 0)) in self.efx:
                ins["type"] = int(di["type"])
                ins["touched"] = True
                ins["vals"] = {int(k): int(v) for k, v in di.get("vals", {}).items()}
                for k in INS_SEND_DEFAULT:
                    ins[k] = max(0, min(127 if k != "eq" else 1, int(di.get(k, INS_SEND_DEFAULT[k]))))
            elif self.ins.get("touched"):
                ins["touched"] = True        # setup has no insertion fx: put the unit back to Thru
            drum, dirty = {}, set()
            for key, vals in d.get("drum", {}).items():
                pm, note = key.split(":")
                port, m = "AB".index(pm[0]), int(pm[1:]) - 1
                for nm, v in vals.items():
                    if nm in DRUM_PARAMS:
                        drum.setdefault((port, m, int(note)), {})[nm] = int(v)
                        dirty.add((port, m, int(note), nm))
        except Exception as ex:
            self.set_status(f"Could not load '{name}': {ex}")
            return
        self.ins, self.drum, self.drum_dirty = ins, drum, dirty
        for i in range(32):
            for e in PART_EXT:
                if parts[i][e[0]] != self.parts[i][e[0]]:
                    self.touched.add((i, e[0]))
        self.parts, self.master, self.fx = parts, master, fx
        self.textbox.text = name
        self.send_seq(build_setup_sequence(self.parts, self.master, self.fx, self.touched, self.ins, self.efx, self.drum_dirty_values()), "load")
        self.set_status(f"Loaded '{name}' and sent it to the unit.")

    # ---- MIDI file export (for the DAW) ----------------------------------------
    def export_midi(self, _b=1):
        """Two standard MIDI files (one per SC-88Pro port) that recall the whole setup when played."""
        name = re.sub(r"[^A-Za-z0-9 _.-]", "_", self.textbox.text).strip() or "SC88_setup"
        seq = build_setup_sequence(self.parts, self.master, self.fx, set(), self.ins, self.efx,
                                   self.drum_dirty_values())
        out_dir = BASE / "exports"
        out_dir.mkdir(exist_ok=True)
        files = []
        for port in (0, 1):
            ev = []                                   # (delta ticks, bytes)
            carry = 0
            if port == 0:
                ev.append((0, [0xF0] + vlq(len(GS_RESET) - 1) + GS_RESET[1:]))
                carry = 120                            # >50 ms of quiet after GS Reset (manual p.191)
            else:
                carry = 300                            # port B waits until port A has been reset
            for prt, ch, kind, a, b, pause in seq:
                if prt != port:
                    continue
                if kind == "cc":
                    msg, gap = [0xB0 | ch, a & 0x7F, b & 0x7F], 2
                elif kind == "pc":
                    msg, gap = [0xC0 | ch, a & 0x7F], 4
                else:
                    msg, gap = [0xF0] + vlq(len(a) - 1) + list(a[1:]), 8
                ev.append((carry, msg))
                carry = gap + int(pause * 1000)
            body = bytearray()
            for delta, data in [(0, [0xFF, 0x51, 0x03, 0x07, 0xA1, 0x20]),
                                (0, [0xFF, 0x03] + vlq(len(f"SC-88Pro Port {'AB'[port]}")) + list(f"SC-88Pro Port {'AB'[port]}".encode()))] + ev + [(10, [0xFF, 0x2F, 0x00])]:
                body += bytes(vlq(delta)) + bytes(data)
            mid = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") + \
                (480).to_bytes(2, "big") + b"MTrk" + len(body).to_bytes(4, "big") + bytes(body)
            path = out_dir / f"{name}_Port{'AB'[port]}.mid"
            path.write_bytes(mid)
            files.append(path.name)
        self.set_status(f"Exported {files[0]} + {files[1]} to the exports folder. Put both clips at the song start.")

    # ---- UI -----------------------------------------------------------------
    def build_ui(self):
        self.strips = [Strip(i, 36 + i * 83) for i in range(16)]
        self.out_btns = [Button((330, 6, 400, 26), "", lambda b: self.choose_device(0, b)),
                         Button((330, 38, 400, 26), "", lambda b: self.choose_device(1, b))]
        self.reset_btn = Button((930, 6, 150, 30), "GS RESET", self.gs_reset)
        self.sendall_btn = Button((1092, 6, 150, 30), "SEND ALL", self.send_all)
        self.read_btn = Button((930, 42, 150, 30), "READ UNIT", self.read_unit)
        self.in_btn = Button((1092, 42, 278, 30), "", self.choose_input)
        self.tab_btns = [Button((30, 78, 170, 34), "PORT A  1-16", lambda b: self.set_tab(0)),
                         Button((206, 78, 170, 34), "PORT B  17-32", lambda b: self.set_tab(1)),
                         Button((382, 78, 130, 34), "EFFECTS / EQ", lambda b: self.set_tab(2)),
                         Button((518, 78, 120, 34), "INSERT FX", lambda b: self.set_tab(4)),
                         Button((644, 78, 120, 34), "PART EDIT", lambda b: self.set_tab(3)),
                         Button((770, 78, 120, 34), "DRUM EDIT", lambda b: self.set_tab(5)),
                         Button((896, 78, 80, 34), "HELP", lambda b: self.set_tab(6))]
        self.tab_ids = [0, 1, 2, 4, 3, 5, 6]
        self.help_topic, self.help_scroll = 0, 0
        self.help_cache = {}
        self.ins_type_btn = Button((50, 128, 470, 34), "", lambda b: self.open_ins_types())
        self.patch_btn = Button((530, 128, 270, 34), "PATCH STARTERS (128)", lambda b: self.open_patches())
        self.dr_port_btn = Button((50, 128, 150, 34), "", lambda b: self.drum_toggle("port"))
        self.dr_map_btn = Button((208, 128, 110, 34), "", lambda b: self.drum_toggle("map"))
        self.dr_note_btn = Button((326, 128, 420, 34), "", lambda b: self.open_drum_notes())
        self.dr_read_btn = Button((754, 128, 150, 34), "READ NOTE", lambda b: self.read_drum_note_async())
        self.dr_names_btn = Button((912, 128, 190, 34), "READ KIT NAMES", lambda b: self.read_kit_names_async())
        self.dr_reset_btn = Button((1110, 128, 230, 34), "FORGET EDITS (this map)", lambda b: self.drum_forget())
        self.fx_items = self.layout_fx()
        self.layout_part()
        self.part_btn = Button((50, 128, 560, 34), "", lambda b: self.open_part_chooser())
        self.part_reset_btn = Button((1110, 128, 230, 34), "RESET THIS PART'S EDITS", self.reset_part_edits)
        self.textbox = TextBox((112, 806, 190, 32))
        self.save_btn = Button((310, 806, 66, 32), "SAVE", self.save_setup)
        self.export_btn = Button((382, 806, 120, 32), "EXPORT .MID", self.export_midi)
        self.arrow_l = Button((548, 806, 32, 32), "<", lambda b: self.page_setups(-1))
        self.arrow_r = Button((1316, 806, 32, 32), ">", lambda b: self.page_setups(1))
        self.chips = []
        self.master_pos = (800, 38, 20)

    def set_tab(self, t):
        self.tab = t

    def layout_fx(self):
        """Positions of every effect control: (key, cx, cy, kind)."""
        items, bands = [], []
        y = 128
        for name, macro, ps in FX_SECTIONS:
            h = 150
            bands.append((name, pygame.Rect(40, y, 1320, h - 8)))
            x = 70
            for p in ps:
                if p[6] == "choice":
                    items.append((p[0], pygame.Rect(x, y + 50, 150, 34), "choice"))
                    x += 170
                else:
                    items.append((p[0], (x + 44, y + 72), "knob"))
                    x += 104
            y += h
        self.fx_bands = bands
        return items

    # ---- insertion effect tab ---------------------------------------------------
    def ins_cells(self):
        """[(param, meta, rect-or-centre)] for the current insertion effect (10 per row)."""
        t = self.efx.get(self.ins["type"])
        out = []
        for i, prm in enumerate(t["params"] if t else []):
            col, row = i % 10, i // 10
            x, y = 50 + col * 130, 206 + row * 150
            meta = efx_meta(prm)
            if meta["kind"] == "choice":
                out.append((prm, meta, pygame.Rect(x + 4, y + 44, 118, 34)))
            else:
                out.append((prm, meta, (x + 63, y + 62)))
        return out

    def open_ins_types(self):
        opts = [(n, f"{n:02d}  {self.efx[n]['name']}") for n in sorted(self.efx)]
        self.popup = Popup("Insertion effect type", opts, self.ins["type"], lambda n: self.set_ins_type(n),
                           rows=22, row_h=26, fsize=15)

    def open_patches(self):
        opts = [(i, f"{i + 1:03d} {p['name']}") for i, p in enumerate(self.patches)]
        self.popup = Popup("Patch starters (instruments + insertion effect)", opts, None, self.apply_patch,
                           rows=32, row_h=22, fsize=13)

    def apply_patch(self, i):
        pt = self.patches[i]
        used = []
        seq = []
        n = 0
        for ent in pt["parts"]:
            if ent["drum"]:
                p = 9
                self.parts[p]["prog"] = ent["pc"]
            else:
                if n == 9:
                    n += 1
                p = n
                n += 1
                self.parts[p]["prog"], self.parts[p]["bank"] = ent["pc"], ent["cc"]
            used.append(p)
            self.send_instrument(p)
            if not ent["drum"]:
                self.set_part_force(p, "efx", 1)
        self.set_ins_type(pt["efx"] if pt["efx"] in self.efx else 0)
        self.set_status(f"Patch starter {i + 1:03d} {pt['name']}: parts "
                        f"{', '.join(part_name(p) for p in used)} + insertion effect {pt['efx']}.")
        self.set_tab(4)

    def set_part_force(self, p, key, v):
        self.parts[p][key] = v
        self.touched.add((p, key))
        self.send_seq([(p // 16, 0, "sx", ext_message(p, key, v), 0, 0.0)], f"pe{p}{key}")

    def draw_ins(self, s):
        t = self.efx.get(self.ins["type"])
        self.ins_type_btn.draw(s, self, f"TYPE {self.ins['type']:02d}:  {t['name'] if t else '?'}   (click to change)")
        self.patch_btn.draw(s, self)
        txt(s, "Parts only use the effect when their INSERTION FX switch (PART EDIT tab) is on.",
            11, DIM, (820, 140))
        mouse = pygame.mouse.get_pos()
        if not t or not t["params"]:
            txt(s, "Thru - no insertion effect.", 15, DIM, (60, 230))
        for prm, meta, pos in self.ins_cells():
            v = self.ins["vals"].get(prm["addr"], efx_default(prm, meta))
            label = efx_text(prm, meta, v)
            if meta["kind"] == "choice":
                r = pos
                hover = r.collidepoint(mouse)
                pygame.draw.rect(s, (54, 28, 8) if hover else (49, 40, 33), r, border_radius=6)
                pygame.draw.rect(s, ACCENT if hover else (101, 83, 70), r, 1, border_radius=6)
                txt(s, label, fit_size(label, r.w - 10, (14, 13, 12, 11, 10), True), ACCENT if hover else TEXT,
                    r.center, "center", bold=True)
                cx, ty = r.centerx, r.y - 14
            else:
                cx, cy = pos
                t01 = (v - meta["lo"]) / float(max(1, meta["hi"] - meta["lo"]))
                draw_knob(s, cx, cy, 20, max(0.0, min(1.0, t01)))
                txt(s, label, 12, ACCENT, (cx, cy + 34), "center", bold=True)
                ty = cy - 42
            txt(s, prm["name"][:14], 11, DIM, (cx, ty), "center")
        # sends
        y = 540
        pygame.draw.rect(s, (40, 32, 27), pygame.Rect(40, y - 36, 1320, 140), border_radius=8)
        pygame.draw.rect(s, EDGE, pygame.Rect(40, y - 36, 1320, 140), 1, border_radius=8)
        txt(s, "OUTPUT OF THE INSERTION EFFECT", 13, ACCENT, (54, y - 28), bold=True)
        self.ins_send_pos = {}
        for i, (k, lab) in enumerate((("rev", "TO REVERB"), ("cho", "TO CHORUS"), ("dly", "TO DELAY"), ("eq", "EQ"))):
            cx = 120 + i * 140
            if k == "eq":
                r = pygame.Rect(cx - 52, y + 30, 104, 34)
                self.ins_send_pos[k] = r
                hover = r.collidepoint(mouse)
                pygame.draw.rect(s, (54, 28, 8) if hover else (49, 40, 33), r, border_radius=6)
                pygame.draw.rect(s, ACCENT if hover else (101, 83, 70), r, 1, border_radius=6)
                txt(s, "EQ On" if self.ins["eq"] else "EQ Off", 14, ACCENT if hover else TEXT, r.center, "center", bold=True)
                txt(s, lab, 11, DIM, (cx, y + 8), "center")
            else:
                cy = y + 44
                self.ins_send_pos[k] = (cx, cy)
                draw_knob(s, cx, cy, 20, self.ins[k] / 127.0)
                txt(s, lab, 11, DIM, (cx, y + 8), "center")
                txt(s, str(self.ins[k]), 12, ACCENT, (cx, cy + 34), "center", bold=True)
        txt(s, "Drag a knob (Shift = fine), wheel to step, double-click to reset.  "
               "Values with # are table steps (see the manual's effect data table).", 11, DIM, (40, 750))

    def handle_ins(self, e):
        if self.ins_type_btn.handle(e, self) or self.patch_btn.handle(e, self):
            return True
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for prm, meta, pos in self.ins_cells():
                if meta["kind"] == "choice":
                    if pos.collidepoint(e.pos):
                        opts = [(raw, nm) for raw, nm in zip(meta["raws"], meta["names"])]
                        cur = self.ins["vals"].get(prm["addr"])
                        self.popup = Popup(prm["name"], opts, cur,
                                           lambda v, a=prm["addr"]: self.set_ins(("p", a), v), rows=10, row_h=30, fsize=16)
                        return True
                elif math.hypot(e.pos[0] - pos[0], e.pos[1] - pos[1]) <= 28:
                    self.begin_drag(-3, ("p", prm["addr"]), "knob")
                    return True
            for k, pos in getattr(self, "ins_send_pos", {}).items():
                if k == "eq":
                    if pos.collidepoint(e.pos):
                        self.set_ins(("s", "eq"), 1 - self.ins["eq"])
                        return True
                elif math.hypot(e.pos[0] - pos[0], e.pos[1] - pos[1]) <= 28:
                    self.begin_drag(-3, ("s", k), "knob")
                    return True
        if e.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            for prm, meta, pos in self.ins_cells():
                if meta["kind"] == "knob" and math.hypot(mx - pos[0], my - pos[1]) <= 28:
                    rng = meta["hi"] - meta["lo"]
                    step = 1 if (rng < 40 or pygame.key.get_mods() & pygame.KMOD_SHIFT) else 3
                    f = ("p", prm["addr"])
                    self.set_ins(f, self.get_val(-3, f) + e.y * step)
                    return True
            for k, pos in getattr(self, "ins_send_pos", {}).items():
                if k != "eq" and math.hypot(mx - pos[0], my - pos[1]) <= 28:
                    step = 1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 3
                    self.set_ins(("s", k), self.ins[k] + e.y * step)
                    return True
        return False

    # ---- help tab ---------------------------------------------------------------
    def help_lines(self):
        if self.help_topic not in self.help_cache:
            self.help_cache[self.help_topic] = wrap_text(HELP_TOPICS[self.help_topic][1], 14, 950)
        return self.help_cache[self.help_topic]

    def draw_help(self, s):
        mouse = pygame.mouse.get_pos()
        self.help_btns = []
        for i, (title, _t) in enumerate(HELP_TOPICS):
            r = pygame.Rect(50, 134 + i * 44, 270, 38)
            active = i == self.help_topic
            hover = r.collidepoint(mouse)
            pygame.draw.rect(s, (70, 36, 11) if active else ((54, 28, 8) if hover else (49, 40, 33)), r, border_radius=6)
            pygame.draw.rect(s, ACCENT if (active or hover) else (101, 83, 70), r, 1, border_radius=6)
            txt(s, title, fit_size(title, r.w - 16, (15, 14, 13, 12), True), ACCENT if (active or hover) else TEXT,
                r.center, "center", bold=True)
            self.help_btns.append((r, i))
        area = pygame.Rect(340, 128, 1010, 650)
        pygame.draw.rect(s, (30, 23, 20), area, border_radius=8)
        pygame.draw.rect(s, EDGE, area, 1, border_radius=8)
        lines = self.help_lines()
        lh = 20
        visible = (area.h - 20) // lh
        self.help_scroll = max(0, min(max(0, len(lines) - visible), self.help_scroll))
        clip = s.get_clip()
        s.set_clip(area.inflate(-4, -4))
        y = area.y + 12
        for ln in lines[self.help_scroll:self.help_scroll + visible + 1]:
            head = ln.isupper() and len(ln) > 2
            txt(s, ln, 14, ACCENT if head else TEXT, (area.x + 16, y), bold=head)
            y += lh
        s.set_clip(clip)
        if len(lines) > visible:
            frac = self.help_scroll / float(max(1, len(lines) - visible))
            bar = pygame.Rect(area.right - 10, area.y + 8 + int(frac * (area.h - 80)), 5, 56)
            pygame.draw.rect(s, ACCENT_DIM, bar, border_radius=3)
            txt(s, "scroll with the mouse wheel", 11, DIM, (area.right - 14, area.bottom - 16), "bottomright")

    def handle_help(self, e):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for r, i in getattr(self, "help_btns", []):
                if r.collidepoint(e.pos):
                    self.help_topic, self.help_scroll = i, 0
                    return True
        if e.type == pygame.MOUSEWHEEL:
            self.help_scroll -= e.y * 3
            return True
        return False

    # ---- drum edit tab -------------------------------------------------------------
    def drum_toggle(self, what):
        if what == "port":
            self.drum_sel["port"] ^= 1
        else:
            self.drum_sel["map"] ^= 1

    def drum_kit_key(self):
        port = self.drum_sel["port"]
        return (port, self.parts[port * 16 + 9]["prog"])

    def drum_note_name(self, note):
        names = self.kit_names.get(self.drum_kit_key(), {})
        return names.get(note) or GM_DRUM.get(note, "")

    def open_drum_notes(self):
        opts = [(n, f"{n:3d} {note_name(n):>3}  {self.drum_note_name(n)}") for n in range(13, 97)]
        self.popup = Popup("Drum note", opts, self.drum_sel["note"],
                           lambda n: self.drum_sel.__setitem__("note", n), rows=28, row_h=22, fsize=13)

    def drum_forget(self):
        m, port = self.drum_sel["map"], self.drum_sel["port"]
        self.drum = {k: v for k, v in self.drum.items() if not (k[0] == port and k[1] == m)}
        self.drum_dirty = {k for k in self.drum_dirty if not (k[0] == port and k[1] == m)}
        self.set_status("Forgot the app's drum edits for this port/map (the unit keeps what it has).")

    def read_drum_note_async(self):
        if self.inp is None or self.reading:
            self.set_status("Connect IN first (and wait for any read to finish).")
            return
        threading.Thread(target=self._read_note_worker, daemon=True).start()

    def _read_note_worker(self):
        self.reading = True
        try:
            port, m, note = self.drum_sel["port"], self.drum_sel["map"], self.drum_sel["note"]
            got = self.request_values(port, [(drum_addr(m, note, n), 1) for n in DRUM_PARAMS], "Reading note")
            cur = self.drum.setdefault((port, m, note), {})
            for n in DRUM_PARAMS:
                d = got.get(drum_addr(m, note, n))
                if d and (port, m, note, n) not in self.drum_dirty:
                    cur[n] = d[0]
            self.set_status(f"Read drum note {note} ({len(got)}/{len(DRUM_PARAMS)} values).")
        finally:
            self.reading = False

    def read_kit_names_async(self):
        if self.inp is None or self.reading:
            self.set_status("Connect IN first (and wait for any read to finish).")
            return
        threading.Thread(target=self._read_names_worker, daemon=True).start()

    def _read_names_worker(self):
        self.reading = True
        try:
            port = self.drum_sel["port"]
            prog = self.parts[port * 16 + 9]["prog"]
            names = {}
            for mm in (3, 2, 1):
                self.rx_names = []
                self.write_midi(port, 0, "sx", rq1(0x0C0003, [0, mm, prog]), 0)
                t0 = last = time.time()
                n0 = 0
                while time.time() - t0 < 3 and time.time() - last < 0.6:
                    time.sleep(0.02)
                    if len(self.rx_names) != n0:
                        n0, last = len(self.rx_names), time.time()
                for d in list(self.rx_names):
                    if len(d) >= 16:
                        names[d[3]] = "".join(chr(c) for c in d[4:16] if 32 <= c < 127).strip()
                if names:
                    break
            self.kit_names[(port, prog)] = names
            self.set_status(f"Read {len(names)} instrument names for drum set {prog + 1}." if names
                            else "The unit did not answer the name request.")
        finally:
            self.reading = False

    def draw_drum(self, s):
        sel = self.drum_sel
        port, m, note = sel["port"], sel["map"], sel["note"]
        prog = self.parts[port * 16 + 9]["prog"]
        self.dr_port_btn.draw(s, self, f"PORT {'AB'[port]}  (part {'AB'[port]}10)")
        self.dr_map_btn.draw(s, self, f"MAP {m + 1}")
        kit = self.kits.get(prog, f"set {prog + 1}")
        self.dr_note_btn.draw(s, self, f"{note:3d} {note_name(note)}  {self.drum_note_name(note)}"[:44])
        self.dr_read_btn.draw(s, self)
        self.dr_names_btn.draw(s, self)
        self.dr_reset_btn.draw(s, self)
        pygame.draw.rect(s, (40, 32, 27), pygame.Rect(40, 178, 1320, 250), border_radius=8)
        pygame.draw.rect(s, EDGE, pygame.Rect(40, 178, 1320, 250), 1, border_radius=8)
        txt(s, f"DRUM KIT ON THIS PORT:  {kit}", 13, ACCENT, (54, 188), bold=True)
        cur = self.drum.get((port, m, note), {})
        self.dr_items = []
        mouse = pygame.mouse.get_pos()
        labels = [("level", "LEVEL"), ("pan", "PAN"), ("rev", "REVERB"), ("cho", "CHORUS"), ("dly", "DELAY"),
                  ("pitch", "PITCH"), ("group", "ASSIGN GRP")]
        for i, (name, lab) in enumerate(labels):
            cx, cy = 100 + i * 120, 290
            v = cur.get(name)
            dirty = (port, m, note, name) in self.drum_dirty
            draw_knob(s, cx, cy, 21, (v if v is not None else 0) / 127.0)
            txt(s, lab, 10, DIM, (cx, cy - 40), "center")
            if v is None:
                shown = "--"
            elif name == "pan":
                shown = "Rnd" if v == 0 else ("C" if v == 64 else (f"L{64 - v}" if v < 64 else f"R{v - 64}"))
            elif name == "pitch":
                shown = str(v)
            elif name == "group":
                shown = "Non" if v == 0 else str(v)
            else:
                shown = str(v)
            txt(s, shown, 12, AMBER if dirty else ACCENT, (cx, cy + 36), "center", bold=True)
            self.dr_items.append((name, (cx, cy), "knob"))
        for i, (name, lab) in enumerate((("rxon", "RX NOTE ON"), ("rxoff", "RX NOTE OFF"))):
            r = pygame.Rect(960 + i * 190, 270, 170, 34)
            v = cur.get(name)
            hover = r.collidepoint(mouse)
            pygame.draw.rect(s, (54, 28, 8) if hover else (49, 40, 33), r, border_radius=6)
            pygame.draw.rect(s, ACCENT if hover else (101, 83, 70), r, 1, border_radius=6)
            txt(s, "--" if v is None else ("On" if v else "Off"), 14, ACCENT if hover else TEXT, r.center, "center", bold=True)
            txt(s, lab, 10, DIM, (r.centerx, r.y - 12), "center")
            self.dr_items.append((name, r, "choice"))
        note_lines = [
            "Pick a note, then READ NOTE to load its real values from the unit (needs IN). Amber = edited here.",
            "Changing the drum kit on the part resets the unit's drum edits, so they are re-sent after the kit in setups.",
            "MAP 1 / MAP 2 are the two drum maps; part 10 uses MAP 1 unless you changed it.",
            "Notes are named from the unit after READ KIT NAMES, otherwise from the General MIDI drum map."]
        for i, l in enumerate(note_lines):
            txt(s, l, 11, DIM, (54, 452 + i * 18))
        n_ed = len(self.drum_dirty)
        txt(s, f"{n_ed} drum value(s) edited in this session are saved with the setup.", 12, ACCENT_DIM, (54, 540))

    def handle_drum(self, e):
        for b in (self.dr_port_btn, self.dr_map_btn, self.dr_note_btn, self.dr_read_btn, self.dr_names_btn,
                  self.dr_reset_btn):
            if b.handle(e, self):
                return True
        port, m, note = (self.drum_sel[k] for k in ("port", "map", "note"))
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for name, pos, kind in getattr(self, "dr_items", []):
                if kind == "choice":
                    if pos.collidepoint(e.pos):
                        cur = self.drum.get((port, m, note), {}).get(name, 1)
                        self.set_drum((port, m, note, name), 1 - cur)
                        return True
                elif math.hypot(e.pos[0] - pos[0], e.pos[1] - pos[1]) <= 29:
                    self.begin_drag(-4, (port, m, note, name), "knob")
                    return True
        if e.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            for name, pos, kind in getattr(self, "dr_items", []):
                if kind == "knob" and math.hypot(mx - pos[0], my - pos[1]) <= 29:
                    key = (port, m, note, name)
                    step = 1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 3
                    self.set_drum(key, self.get_val(-4, key) + e.y * step)
                    return True
        return False

    # ---- part edit tab ---------------------------------------------------------
    def layout_part(self):
        self.pe_items, self.pe_bands = [], []
        groups = [("PLAY MODE  /  RANGE", ["mono", "assign", "kshift", "ftune", "vdepth", "voffs", "klow", "khigh"], 172, 150),
                  ("TONE MODIFY   (relative to the instrument)",
                   ["tm_vrate", "tm_vdepth", "tm_vdelay", "tm_cut", "tm_res", "tm_att", "tm_dec", "tm_rel"], 322, 150),
                  ("ROUTING", ["eq", "output", "efx"], 472, 130)]
        for name, keys, y, h in groups:
            self.pe_bands.append((name, pygame.Rect(40, y, 1320, h - 8)))
            x = 70
            for k in keys:
                if EXT[k][7] == "choice":
                    self.pe_items.append((k, pygame.Rect(x, y + 50, 150, 34), "choice"))
                    x += 170
                else:
                    self.pe_items.append((k, (x + 44, y + 72), "knob"))
                    x += 104

    def open_part_chooser(self):
        opts = [(p, f"{part_name(p)}  {self.instrument_name(p)}") for p in range(32)]
        self.popup = Popup("Choose part to edit", opts, self.edit_part,
                           lambda v: setattr(self, "edit_part", v), rows=16, row_h=26, fsize=15)

    def reset_part_edits(self, _b=1):
        p = self.edit_part
        for e in PART_EXT:
            self.set_part(p, e[0], part_default(p)[e[0]])
        self.set_status(f"Part {part_name(p)} edits set back to GS defaults.")

    def draw_part(self, s):
        p = self.edit_part
        st = self.parts[p]
        self.part_btn.draw(s, self, f"PART {part_name(p)}   {self.instrument_name(p)}   (click to change)")
        self.part_reset_btn.draw(s, self)
        for name, r in self.pe_bands:
            pygame.draw.rect(s, (40, 32, 27), r, border_radius=8)
            pygame.draw.rect(s, EDGE, r, 1, border_radius=8)
            txt(s, name, 13, ACCENT, (r.x + 14, r.y + 8), bold=True)
        mouse = pygame.mouse.get_pos()
        for key, pos, kind in self.pe_items:
            e = EXT[key]
            v = st[key]
            if kind == "choice":
                r = pos
                hover = r.collidepoint(mouse)
                pygame.draw.rect(s, (54, 28, 8) if hover else (49, 40, 33), r, border_radius=6)
                pygame.draw.rect(s, ACCENT if hover else (101, 83, 70), r, 1, border_radius=6)
                label = ext_text(key, v)
                txt(s, label, fit_size(label, r.w - 12, (15, 14, 13, 12, 11, 10), True),
                    ACCENT if hover else TEXT, r.center, "center", bold=True)
                txt(s, e[1], 10, DIM, (r.centerx, r.y - 12), "center")
            else:
                cx, cy = pos
                t = (v - e[4]) / float(max(1, e[5] - e[4]))
                draw_knob(s, cx, cy, 21, max(0.0, min(1.0, t)))
                txt(s, e[1], 10, DIM, (cx, cy - 38), "center")
                changed = v != part_default(p)[key]
                txt(s, ext_text(key, v), 12, AMBER if changed else ACCENT, (cx, cy + 36), "center", bold=True)
        txt(s, "Amber = changed from the GS default. Double-click a knob to reset it. "
               "Tone modify and range only affect notes played after the change.", 11, DIM, (40, 612))
        txt(s, "Insertion FX / Output / EQ routing: the insertion effect itself is not editable yet.", 11, DIM, (40, 630))

    def handle_part(self, e):
        p = self.edit_part
        if self.part_btn.handle(e, self) or self.part_reset_btn.handle(e, self):
            return True
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for key, pos, kind in self.pe_items:
                if kind == "choice":
                    if pos.collidepoint(e.pos):
                        ex = EXT[key]
                        opts = [(i, n) for i, n in enumerate(ex[8])]
                        self.popup = Popup(ex[1].title(), opts, self.parts[p][key],
                                           lambda v, p=p, key=key: self.set_part(p, key, v),
                                           rows=8, row_h=30, fsize=16)
                        return True
                elif math.hypot(e.pos[0] - pos[0], e.pos[1] - pos[1]) <= 30:
                    self.begin_drag(p, key, "knob")
                    return True
        if e.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            for key, pos, kind in self.pe_items:
                if kind == "knob" and math.hypot(mx - pos[0], my - pos[1]) <= 30:
                    step = 1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else (1 if EXT[key][5] - EXT[key][4] < 60 else 3)
                    self.set_part(p, key, self.parts[p][key] + e.y * step)
                    return True
        return False

    def draw_fx(self, s):
        for name, r in self.fx_bands:
            pygame.draw.rect(s, (40, 32, 27), r, border_radius=8)
            pygame.draw.rect(s, EDGE, r, 1, border_radius=8)
            txt(s, " ".join(name), 13, ACCENT, (r.x + 14, r.y + 8), bold=True)
        mouse = pygame.mouse.get_pos()
        for key, pos, kind in self.fx_items:
            p = FX_PARAMS[key]
            v = self.fx[key]
            if kind == "choice":
                r = pos
                hover = r.collidepoint(mouse)
                pygame.draw.rect(s, (54, 28, 8) if hover else (49, 40, 33), r, border_radius=6)
                pygame.draw.rect(s, ACCENT if hover else (101, 83, 70), r, 1, border_radius=6)
                label = fx_text(key, v)
                txt(s, label, fit_size(label, r.w - 12, (15, 14, 13, 12, 11, 10), True),
                    ACCENT if hover else TEXT, r.center, "center", bold=True)
                txt(s, p[1], 10, DIM, (r.centerx, r.y - 12), "center")
            else:
                cx, cy = pos
                t = (v - p[3]) / float(max(1, p[4] - p[3]))
                draw_knob(s, cx, cy, 21, max(0.0, min(1.0, t)))
                txt(s, p[1], 10, DIM, (cx, cy - 38), "center")
                txt(s, fx_text(key, v), 12, ACCENT, (cx, cy + 36), "center", bold=True)
        txt(s, "Drag a knob (Shift = fine), wheel to step, double-click to reset. "
               "Changing a TYPE re-sends the block's knob values so the screen always matches the unit.",
            11, DIM, (40, 735))
        txt(s, "Effects are global (sent to Port A). Per-part sends are on the Port tabs.", 11, DIM, (40, 752))

    def handle_fx(self, e):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for key, pos, kind in self.fx_items:
                if kind == "choice":
                    if pos.collidepoint(e.pos):
                        self.open_fx_choice(key)
                        return True
                elif math.hypot(e.pos[0] - pos[0], e.pos[1] - pos[1]) <= 30:
                    self.begin_drag(-2, key, "knob")
                    return True
        if e.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            for key, pos, kind in self.fx_items:
                if kind == "knob" and math.hypot(mx - pos[0], my - pos[1]) <= 30:
                    p = FX_PARAMS[key]
                    step = 1 if (p[4] - p[3] < 20 or pygame.key.get_mods() & pygame.KMOD_SHIFT) else 3
                    self.set_fx(key, self.fx[key] + e.y * step)
                    return True
        return False

    def page_setups(self, d):
        self.setup_off = max(0, min(max(0, len(self.setups) - 1), self.setup_off + d))

    def _make_bg(self):
        bg = pygame.Surface((W, H))
        for y in range(H):
            t = y / H
            pygame.draw.line(bg, (int(24 + 12 * t), int(19 + 9 * t), int(15 + 7 * t)), (0, y), (W, y))
        return bg

    # ---- events -------------------------------------------------------------
    def handle(self, e):
        if e.type == pygame.QUIT:
            self.running = False
            return
        if self.popup:
            self.popup.handle(e, self)
            return
        if e.type == pygame.MOUSEMOTION and self.drag:
            self.drag_motion(e)
            return
        if e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = None
            self.drag_groove = None
            return
        if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE and not self.textbox.focus:
            self.running = False
            return
        if self.textbox.handle(e, self):
            return
        for w in (self.out_btns + [self.reset_btn, self.sendall_btn, self.read_btn, self.in_btn, self.save_btn,
                                   self.arrow_l, self.arrow_r, self.export_btn] + self.tab_btns):
            if w.handle(e, self):
                return
        if self.tab == 2:
            if self.handle_fx(e):
                return
        elif self.tab == 3:
            if self.handle_part(e):
                return
        elif self.tab == 4:
            if self.handle_ins(e):
                return
        elif self.tab == 5:
            if self.handle_drum(e):
                return
        elif self.tab == 6:
            if self.handle_help(e):
                return
        else:
            for w in self.strips:
                if w.handle(e, self):
                    return
        mcx, mcy, mR = self.master_pos
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            if math.hypot(e.pos[0] - mcx, e.pos[1] - mcy) <= mR + 10:
                self.begin_drag(-1, "master", "knob")
                return
            for r, name in self.chips:
                if r.collidepoint(e.pos):
                    self.load_setup(name)
                    return
        if e.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            if math.hypot(mx - mcx, my - mcy) <= mR + 10:
                step = 1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 3
                self.set_master(self.master + e.y * step)

    # ---- drawing ------------------------------------------------------------
    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        now = time.time()

        txt(s, "SC-88Pro", 36, ACCENT, (30, 8), bold=True)
        txt(s, "M I D I   C O N T R O L   S U R F A C E", 11, ACCENT_DIM, (32, 52))
        for port, btn in enumerate(self.out_btns):
            name = self.device_label(self.settings.get(f"out{'AB'[port]}"))
            btn.draw(s, self, f"PORT {'AB'[port]} OUT:  {name}"[:52])
        mcx, mcy, mR = self.master_pos
        draw_knob(s, mcx, mcy, mR, self.master / 127.0)
        txt(s, "MASTER", 11, DIM, (mcx + 34, mcy - 14))
        txt(s, str(self.master), 15, ACCENT, (mcx + 34, mcy + 2), bold=True)
        self.reset_btn.draw(s, self)
        self.sendall_btn.draw(s, self)
        self.read_btn.draw(s, self, "READING..." if self.reading else "READ UNIT")
        self.in_btn.draw(s, self, f"IN: {self.device_label(self.settings.get('in'), True)}"[:34])
        pygame.draw.circle(s, (255, 190, 60) if now - self.flash_out < 0.12 else (50, 44, 30), (1262, 21), 6)
        txt(s, "OUT", 10, DIM, (1272, 15))
        pygame.draw.circle(s, (220, 119, 34) if now - self.flash_in < 0.12 else (50, 26, 8), (1322, 21), 6)
        txt(s, "IN", 10, DIM, (1332, 15))

        for t, btn in zip(self.tab_ids, self.tab_btns):
            btn.draw(s, self, active=(self.tab == t))
        if now - self.status_t < 14:
            txt(s, self.status[:54], 12, (210, 174, 147), (990, 84))
        if self.last_msg:
            txt(s, self.last_msg, 12, DIM, (990, 100))

        panel(s, (30, 118, 1340, 670))
        if self.tab == 2:
            self.draw_fx(s)
        elif self.tab == 3:
            self.draw_part(s)
        elif self.tab == 4:
            self.draw_ins(s)
        elif self.tab == 5:
            self.draw_drum(s)
        elif self.tab == 6:
            self.draw_help(s)
        else:
            for st in self.strips:
                st.draw(s, self)

        panel(s, (30, 796, 1340, 54))
        txt(s, "SETUP", 13, ACCENT_DIM, (50, 814), bold=True)
        self.textbox.draw(s, self)
        self.save_btn.draw(s, self)
        self.export_btn.draw(s, self)
        txt(s, "LOAD", 13, ACCENT_DIM, (510, 814), bold=True)
        self.arrow_l.draw(s, self)
        self.arrow_r.draw(s, self)
        self.chips = []
        x = 590
        for name in self.setups[self.setup_off:]:
            w = min(170, font(14).size(name)[0] + 26)
            if x + w > 1306:
                break
            r = pygame.Rect(x, 806, w, 32)
            hover = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, (54, 28, 8) if hover else (47, 38, 32), r, border_radius=6)
            pygame.draw.rect(s, ACCENT if hover else (90, 73, 62), r, 1, border_radius=6)
            txt(s, name[:22], 14, ACCENT if hover else TEXT, r.center, "center")
            self.chips.append((r, name))
            x += w + 8
        if not self.setups:
            txt(s, "no saved setups yet", 13, (103, 84, 71), (590, 822), "midleft")

        if self.popup:
            self.popup.draw(s, self)

    def run(self, shot=None):
        clock = pygame.time.Clock()
        frames = 0
        while self.running:
            for e in pygame.event.get():
                self.handle(e)
            self.draw()
            pygame.display.flip()
            clock.tick(60)
            frames += 1
            if shot and frames >= 3:
                pygame.image.save(self.screen, shot)
                break
        self.shutdown()

    def shutdown(self):
        self.open_input(None)
        with self.out_lock:
            for dev in self.outs:
                try:
                    if dev:
                        dev.close()
                except Exception:
                    pass
        if pgmidi:
            try:
                pgmidi.quit()
            except Exception:
                pass
        pygame.quit()


def main():
    shot = None
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]
    App(headless=bool(shot)).run(shot)


if __name__ == "__main__":
    main()
