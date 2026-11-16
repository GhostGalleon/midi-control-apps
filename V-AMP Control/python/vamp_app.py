#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
V-AMP CONTROL  -  MIDI control surface for the Behringer V-AMP (Mk1, 2001).

Run:      python vamp_app.py
Needs:    pip install pygame
Labels:   vamp_data.json (amp / cab / effect / reverb names and the preset library)

What the app knows comes from your own captures:
  * amp CC19, effect CC20, cabinet CC23 (0 = bypass) are all in KNOB ORDER
  * turning the unit's FX Mix / Reverb Mix knob also switches that effect ON (CC21 / CC22 = 127)
  * the unit's own knobs top out at 126
  * CC80 = 80 makes the unit report its state; amp type comes back as CC61, effect type as CC60
  * the 'd' total dump = 125 presets x 48 bytes (32 data bytes + 16 character name)
  * programming order: effects / reverb / drive / wah OFF first, then amp + cab + knobs,
    then effect type + parameters, reverb, and the switches are turned ON last
"""
import json
import math
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
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
DATA_FILE = BASE / "vamp_data.json"
SOUNDS_DIR = BASE / "vamp_sounds"
DUMPS_DIR = (BASE.parent / "data_backups" / "dumps") if BASE.name.lower() == "data" else BASE / "dumps"   # .syx backups
OLD_DUMPS_DIR = BASE / "dumps"                   # where earlier versions kept them (still read)
EXPORT_DIR = BASE / "exports"
SETTINGS_FILE = BASE / "vamp_settings.json"
NAMES_FILE = BASE / "vamp_names.json"
DEFAULT_DUMP = DUMPS_DIR / "factory_default.syx"

W, H = 1400, 860

# ---- timing (raise these if the unit ever drops messages) ---------------------
GAP = 0.008                                    # seconds between any two messages
SETTLE = {19: 0.06, 20: 0.06, 23: 0.04, 24: 0.03}   # extra wait after selector changes
ECHO_IGNORE = 0.5                              # ignore incoming CCs this long after we change something

# ---- palette (V-AMP metallic blue) ---------------------------------------------------
AMBER = (150, 214, 255)       # accent: bright ice blue (name kept from the earlier palette)
AMBER_DIM = (86, 132, 186)
TEXT = (236, 242, 250)
DIM = (150, 168, 192)
PANEL = (30, 54, 98)
EDGE = (118, 150, 202)
LCD_BG = (22, 4, 4)
LCD_FG = (255, 70, 50)        # the unit's red LED display
LCD_GLOW = (120, 24, 16)
GREEN = (90, 230, 130)
RED = (255, 70, 60)
BTN = (40, 66, 114)
BTN_HOT = (62, 104, 170)
BTN_EDGE = (110, 146, 200)
WELL = (14, 26, 52)

# =============================================================================
# PARAMETER TABLE  (single source of truth: UI, send, save and load all use it)
# =============================================================================
KNOBS = {}    # cc -> label, default, max
TOGGLES = {}  # cc -> label, default, states[(value,text)], thresholds


def K(cc, label, default, mx=127):
    KNOBS[cc] = {"label": label, "default": default, "max": mx}


def T(cc, label, default, states, thresholds):
    TOGGLES[cc] = {"label": label, "default": default, "states": states, "thr": thresholds}


ONOFF = [(0, "OFF"), (127, "ON")]
ONOFF_THR = [64]

K(12, "Gain", 64); K(13, "Treble", 64); K(14, "Mid", 64); K(15, "Bass", 64); K(16, "Volume", 100)
K(25, "Gate", 5, 15)
T(26, "Drive", 0, ONOFF, ONOFF_THR)
K(27, "Wah Pedal", 0)                       # 0 = wah off, 1-127 = wah on at that position
K(7, "Vol Pedal", 127)                      # live only: never saved or loaded
K(18, "Reverb Mix", 40)
T(22, "Reverb", 127, ONOFF, ONOFF_THR)
K(59, "FX Mix", 64)
T(21, "Effects", 0, ONOFF, ONOFF_THR)
# advanced effect blocks (labels are generic: the manual does not name them per effect)
T(44, "Pre Fx Type", 0, [(0, "TYPE 1"), (1, "TYPE 2"), (2, "TYPE 3")], [1, 2])
K(45, "Pre 1", 64); K(46, "Pre 2", 64); K(47, "Pre 3", 64); K(48, "Pre 4", 64)
T(49, "Delay Type", 0, [(0, "TYPE 1"), (1, "TYPE 2"), (2, "TYPE 3")], [1, 2])
K(50, "Time Hi", 20, 117); K(51, "Time Lo", 0); K(52, "Spread", 32); K(53, "Feedback", 40); K(54, "Delay Mix", 40)
T(55, "Post Fx Mode", 0, [(i, f"MODE {i + 1}") for i in range(7)], [1, 2, 3, 4, 5, 6])
K(56, "Post 1", 64); K(57, "Post 2", 64); K(58, "Post 3", 64)

# selectors + the one raw value we keep but never show
SELECT_DEFAULTS = {19: 3, 20: 0, 23: 5, 24: 1, 17: 0}
LABELS = {19: "Amp", 20: "Effect Type", 23: "Cabinet", 24: "Reverb Type", 17: "Fx Mix Assign", 1: "Wah Pedal In"}

DEFAULTS = dict(SELECT_DEFAULTS)
for _cc, _p in list(KNOBS.items()) + list(TOGGLES.items()):
    DEFAULTS[_cc] = _p["default"]
    LABELS[_cc] = _p["label"]

LIVE_ONLY = {7}
# order of the 32 data bytes in a dump record
DUMP_CCS = [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27] + list(range(44, 60))
MAXV = {cc: p["max"] for cc, p in KNOBS.items()}
MAXV.update({19: 15, 20: 15, 23: 15, 24: 8, 44: 2, 49: 2, 55: 6, 17: 127})


def clamp(cc, v):
    return max(0, min(MAXV.get(cc, 127), int(v)))


def build_load_sequence(v):
    """Full sound load. Everything that makes noise is OFF first, switches come back last."""
    seq = [("cc", 21, 0), ("cc", 22, 0), ("cc", 26, 0), ("cc", 27, 0)]
    seq += [("cc", 19, v[19]), ("cc", 23, v[23])]
    seq += [("cc", c, v[c]) for c in (12, 13, 14, 15, 16, 25)]
    seq += [("cc", 20, v[20])]
    seq += [("cc", c, v[c]) for c in (44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59)]
    seq += [("cc", 24, v[24]), ("cc", 18, v[18])]
    if v[22] >= 64:
        seq.append(("cc", 22, 127))
    if v[26] >= 64:
        seq.append(("cc", 26, 127))
    if v[21] >= 64:
        seq.append(("cc", 21, 127))
    if v[27] > 0:
        seq.append(("cc", 27, v[27]))
    return seq


def vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def export_midi(seq, channel, path):
    """Standard MIDI file: drop it on an empty track in your DAW, routed to the V-AMP."""
    ppq = 480
    ev = bytearray()
    first = True
    for kind, d1, d2 in seq:
        gap = 2 + int((SETTLE.get(d1, 0.0) if kind == "cc" else 0.0) * 960)
        ev += vlq(0 if first else gap)
        first = False
        ev += bytes([0xB0 | channel, d1, d2]) if kind == "cc" else bytes([0xC0 | channel, d1])
    ev += vlq(480) + b"\xff\x2f\x00"
    trk = vlq(0) + b"\xff\x03" + bytes([6]) + b"V-AMP " + bytes(ev)
    data = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") + ppq.to_bytes(2, "big")
    data += b"MTrk" + len(trk).to_bytes(4, "big") + trk
    Path(path).write_bytes(data)


def block_to_values(b):
    vals = dict(DEFAULTS)
    for cc, x in zip(DUMP_CCS, b):
        vals[cc] = clamp(cc, x)
    return vals


def values_to_block(vals):
    return [clamp(cc, vals.get(cc, DEFAULTS[cc])) for cc in DUMP_CCS]


HELP_TEXT = [
    ("H", "WHAT THIS IS"),
    ("P", "A control surface for the Behringer V-AMP (Mk1). Every knob and switch on the screen sends MIDI to the unit as you move it, "
          "and the unit's own knobs move the screen when MIDI IN is connected. Sounds save to files, a 125-preset library mirrors the unit, "
          "and any sound can be exported as a .mid clip so your DAW can recall it with the song."),
    ("H", "FIRST TIME SETUP"),
    ("B", "Click MIDI OUT and pick the port that goes to the V-AMP's MIDI IN. Click MIDI IN and pick the port that receives the V-AMP's MIDI OUT."),
    ("B", "Click CH to step the MIDI channel (the V-AMP's SETUP button A sets the channel on the unit). Your unit answered on channel 1."),
    ("B", "Press READ UNIT. The unit reports its current sound and the screen follows it."),
    ("B", "The unit has ONE MIDI port. Close the app (or the DAW's port) when the other one needs it - Windows lets only one program own a port."),
    ("H", "MAIN SCREEN"),
    ("B", "PRESET (PC): recalls one of the unit's own 125 memories with a Program Change. The knobs on screen do not know its contents until you press READ UNIT, or use the LIBRARY instead."),
    ("B", "AMP / CABINET: click the display for the full list, use the arrows or the mouse wheel. The line under the amp shows the real amp it models."),
    ("B", "Knobs: drag up and down, mouse wheel to step (hold SHIFT for fine), double-click to reset. Toggles: click to switch (right-click steps backwards)."),
    ("B", "GATE is 0 off, 1-5 mild (clean / single coil), 6-12 standard (high gain), 13-15 maximum clamp."),
    ("B", "WAH: moving the WAH PEDAL knob switches the wah on at that position (CC27). WAH OFF sends CC27 = 0."),
    ("B", "VOL PEDAL is live only. It is never saved into a sound."),
    ("H", "LOAD ORDER (Bypass first)"),
    ("P", "Whenever a whole sound is sent (LOAD, NEW SOUND, SEND ALL, a library slot, the .mid export) the app first switches effects, reverb, drive and wah OFF, "
          "then sets amp, cabinet, knobs and gate, then the effect type and its parameters, then reverb, and only then turns reverb, drive and effects back ON. "
          "No clicks, no half-built sounds."),
    ("H", "ADVANCED FX TAB"),
    ("P", "The pre effect, delay and post effect blocks (CC44-CC59) as raw controls. The manual does not name the parameters per effect, "
          "so they are labelled generically. FX Mix on the main screen is the same CC59 the unit's Effects Mix knob sends."),
    ("H", "LIBRARY TAB"),
    ("B", "Shows the unit's 125 presets, 25 banks of A-E. Left-click loads that preset into the screen AND sends it to the unit (Bypass-first). Right-click only selects the slot."),
    ("B", "RECEIVE: listen for a dump. On the unit press both arrows (SETUP), press MIDI (A) and hold it until the display shows d. The library fills when the dump arrives."),
    ("B", "LOAD DUMP / SAVE DUMP: .syx files live in the dumps folder. Your own dump is there as my_unit_dump.syx - a ready-made backup."),
    ("B", "SET DEFAULT saves the library you see as dumps/factory_default.syx; the app loads it at every start. LOAD DEFAULT brings it back after experiments. Do a factory reset on the unit, RECEIVE its dump, then press SET DEFAULT."),
    ("B", "STORE HERE: puts the current sound into the selected slot (name from the sound box, 16 characters)."),
    ("B", "SEND DUMP: writes the whole library to the unit and OVERWRITES ALL 125 PRESETS. Back up first (SAVE DUMP). The unit may need to be waiting for the dump; if nothing happens, try again after putting it in SETUP."),
    ("H", "BACK TO ALL FACTORY SETTINGS"),
    ("P", "Two separate things, in this order. MIDI cannot reset the unit, so step 1 is done on the V-AMP itself."),
    ("B", "Step 1, on the unit (manual section 5.5): switch the V-AMP off. Hold the D and E buttons and switch it on. When the display reads CL, let go of D and E, then press BOTH arrow keys. All user-edited presets are replaced by the original artist presets."),
    ("B", "Step 2, in this app: open the LIBRARY tab and press RECEIVE. On the unit press both arrow keys (SETUP), press MIDI (A) and hold it until the display shows d. The 125 factory names and settings arrive in the library."),
    ("B", "Step 3: press SET DEFAULT. The library is saved as dumps/factory_default.syx and loads every time the app starts, so a reset library is always one click (LOAD DEFAULT) away. Do steps 2 and 3 straight after a reset, before you store anything of your own."),
    ("B", "To put the saved library back ONTO the unit later, use SEND DUMP (this overwrites all 125 presets). To undo the edits on just one preset, hold both arrow keys until the display reads Pr, then hold the preset button for about 2 seconds (manual section 5.4)."),
    ("B", "Keep your own backups: SAVE DUMP writes a dated .syx file into the dumps folder."),
    ("H", "SOUNDS AND DAW USE"),
    ("B", "Type a name and press SAVE: a small .json file in the vamp_sounds folder. Click a chip to load it."),
    ("B", "EXPORT MID writes the sound as a .mid file (exports folder). Put it on a MIDI track at the start of the song, routed to the V-AMP, and the DAW recalls the sound."),
    ("B", "QUICK ACTIONS: BYPASS ALL silences effects, reverb, drive and wah. SEND ALL re-sends what you see."),
    ("B", "TUNER is a switch: the first press sends Program Change 127 (tuner on, red light), the second press leaves it by recalling the last unit preset. If the app does not know that preset, it re-sends the on-screen sound instead."),
    ("B", "FACTORY RESET shows the front-panel steps (hold D + E at power-on, wait for CL, press both arrows). MIDI cannot do this, so it is a guide."),
    ("B", "Names: a name you type and STORE into a slot is remembered in vamp_names.json. When a fresh dump arrives and the unit still calls that slot USER PRESET, your name is put back. The unit's own front-panel save keeps whatever name the slot had; names only reach the unit through SEND DUMP."),
    ("H", "MIDI QUIRKS HANDLED"),
    ("B", "Turning the unit's FX Mix or Reverb Mix knob also switches that block on. The app mirrors that."),
    ("B", "The unit's knobs stop at 126, so a knob shown at 127 is the app reaching one step further."),
    ("B", "Echoes of our own messages are ignored for half a second, so the screen never fights the unit."),
    ("B", "CC17 (FX mix assign) and the display text (CC81) are stored but never sent: the manual marks them request-only."),
]


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


def fit_size(s, maxw, sizes=(24, 22, 20, 18, 16, 14, 12), bold=False):
    for sz in sizes:
        if font(sz, bold).size(s)[0] <= maxw:
            return sz
    return sizes[-1]


def arc_pts(cx, cy, r, a0, a1, steps=48):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / steps))) for i in range(steps + 1)]


_metal_cache = {}


def metal(w, h, top=(70, 104, 164), bot=(24, 42, 82)):
    key = (w, h, top, bot)
    if key not in _metal_cache:
        sf = pygame.Surface((w, h), pygame.SRCALPHA)
        for y in range(h):
            t = y / max(1, h - 1)
            # bright sheen near the top third, falling to deep blue
            k = 1.0 - t
            sheen = max(0.0, 1.0 - abs(t - 0.18) * 5.0) * 26
            col = tuple(min(255, int(bot[i] + (top[i] - bot[i]) * k * k + sheen)) for i in range(3))
            pygame.draw.line(sf, col, (0, y), (w, y))
        import random
        rnd = random.Random(w * 31 + h)
        for _ in range(max(40, w * h // 900)):
            y = rnd.randrange(h)
            x = rnd.randrange(w)
            ln = rnd.randrange(w // 6, w // 2 + 2)
            c = (255, 255, 255, rnd.randrange(2, 6)) if rnd.random() < 0.5 else (0, 0, 20, rnd.randrange(3, 7))
            pygame.draw.line(sf, c, (x, y), (min(w, x + ln), y))
        _metal_cache[key] = sf
    return _metal_cache[key]


def rounded_metal(surf, rect, radius=10, top=(70, 104, 164), bot=(24, 42, 82)):
    r = pygame.Rect(rect)
    img = metal(r.w, r.h, top, bot).copy()
    mask = pygame.Surface((r.w, r.h), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=radius)
    img.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    surf.blit(img, r.topleft)


def panel(surf, rect, title):
    r = pygame.Rect(rect)
    rounded_metal(surf, r, 10)
    pygame.draw.rect(surf, (10, 18, 40), r, 1, border_radius=10)
    pygame.draw.rect(surf, EDGE, r.inflate(-2, -2), 1, border_radius=9)
    pygame.draw.line(surf, (200, 224, 255), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (204, 228, 255), (r.x + 16, r.y + 8), bold=True)
    pygame.draw.line(surf, (20, 36, 76), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
    pygame.draw.line(surf, (110, 146, 204), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (8, 14, 30), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (170, 196, 230), (sx, r.bottom - 12), 4, 1)
        pygame.draw.line(surf, (110, 134, 176), (sx - 2, r.bottom - 14), (sx + 2, r.bottom - 10))


def lcd(surf, rect, text, max_size=24, fg=None, glow_c=None):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, (8, 14, 30), r.inflate(6, 6), border_radius=8)
    pygame.draw.rect(surf, (150, 176, 214), r.inflate(6, 6), 1, border_radius=8)
    pygame.draw.rect(surf, LCD_BG, r, border_radius=6)
    for y in range(r.y + 3, r.bottom - 3, 3):
        pygame.draw.line(surf, (34, 8, 6), (r.x + 3, y), (r.right - 4, y))
    pygame.draw.rect(surf, (96, 22, 18), r, 1, border_radius=6)
    size = fit_size(text, r.w - 84, sizes=tuple(s for s in (24, 22, 20, 18, 16, 14, 12) if s <= max_size))
    glow = font(size, True).render(text, True, glow_c or LCD_GLOW)
    main = font(size, True).render(text, True, fg or LCD_FG)
    gr = glow.get_rect(center=r.center)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        surf.blit(glow, gr.move(dx, dy))
    surf.blit(main, main.get_rect(center=r.center))


def led(surf, pos, on, color=AMBER):
    if on:
        glow = pygame.Surface((40, 40), pygame.SRCALPHA)
        for rad, a in ((18, 28), (13, 55), (9, 95)):
            pygame.draw.circle(glow, (*color, a), (20, 20), rad)
        surf.blit(glow, (pos[0] - 20, pos[1] - 20))
        pygame.draw.circle(surf, color, pos, 4)
        pygame.draw.circle(surf, (255, 255, 235), (pos[0] - 1, pos[1] - 1), 1)
    else:
        pygame.draw.circle(surf, (24, 36, 64), pos, 4)
        pygame.draw.circle(surf, (110, 140, 190), pos, 4, 1)


def triangle(surf, cx, cy, direction, color):
    d = 7 * direction
    pygame.draw.polygon(surf, color, [(cx + d, cy), (cx - d, cy - 9), (cx - d, cy + 9)])


# =============================================================================
# WIDGETS
# =============================================================================
class Knob:
    R = 26

    def __init__(self, cc, cx, cy):
        self.cc, self.cx, self.cy = cc, cx, cy
        self.visible = True
        self.acc = 0.0
        self.last_click = 0.0

    def hit(self, pos):
        return self.visible and math.hypot(pos[0] - self.cx, pos[1] - self.cy) <= self.R + 12

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.hit(e.pos):
            now = time.time()
            if now - self.last_click < 0.35:  # double-click = reset
                app.set_value(self.cc, DEFAULTS[self.cc])
                self.last_click = 0.0
                return True
            self.last_click = now
            self.acc = float(app.values[self.cc])
            app.drag = self
            return True
        if e.type == pygame.MOUSEWHEEL and self.hit(pygame.mouse.get_pos()):
            step = 1 if (pygame.key.get_mods() & pygame.KMOD_SHIFT or KNOBS[self.cc]["max"] < 40) else 3
            app.set_value(self.cc, app.values[self.cc] + e.y * step)
            return True
        return False

    def drag_by(self, dy, app):
        fine = pygame.key.get_mods() & pygame.KMOD_SHIFT
        mx = KNOBS[self.cc]["max"]
        self.acc = max(0.0, min(float(mx), self.acc - dy * (0.25 if fine else 0.9) * mx / 127.0))
        app.set_value(self.cc, int(round(self.acc)))

    def draw(self, surf, app):
        if not self.visible:
            return
        v = app.values[self.cc]
        t = min(1.0, v / float(KNOBS[self.cc]["max"]))
        cx, cy, R = self.cx, self.cy, self.R
        pygame.draw.circle(surf, (8, 14, 32), (cx, cy), R + 10)
        pygame.draw.circle(surf, (120, 150, 200), (cx, cy), R + 10, 1)
        pygame.draw.lines(surf, (24, 40, 76), False, arc_pts(cx, cy, R + 5, 135, 405), 4)
        if v > 0:
            a1 = 135 + 270 * t
            pygame.draw.lines(surf, (36, 86, 150), False, arc_pts(cx, cy, R + 5, 135, a1), 8)
            pygame.draw.lines(surf, AMBER, False, arc_pts(cx, cy, R + 5, 135, a1), 4)
        pygame.draw.circle(surf, (96, 104, 118), (cx, cy), R)
        pygame.draw.circle(surf, (150, 160, 178), (cx, cy), R - 3)
        pygame.draw.circle(surf, (118, 128, 146), (cx, cy), R - 8)
        pygame.draw.circle(surf, (190, 200, 216), (cx - 5, cy - 6), R - 18)
        ang = math.radians(135 + 270 * t)
        p0 = (cx + math.cos(ang) * R * 0.30, cy + math.sin(ang) * R * 0.30)
        p1 = (cx + math.cos(ang) * R * 0.88, cy + math.sin(ang) * R * 0.88)
        pygame.draw.line(surf, (20, 30, 60), p0, p1, 3)
        label = KNOBS[self.cc]["label"]
        txt(surf, label, fit_size(label, 84, (13, 12, 11, 10)), DIM, (cx, cy + R + 16), "center")
        txt(surf, str(v), 15, AMBER, (cx, cy + R + 32), "center", bold=True)


class Toggle:
    def __init__(self, cc, cx, cy, w=92):
        self.cc = cc
        self.rect = pygame.Rect(0, 0, w, 34)
        self.rect.center = (cx, cy)
        self.visible = True

    def move(self, cx, cy):
        self.rect.center = (cx, cy)

    def index(self, app):
        p = TOGGLES[self.cc]
        return sum(1 for t in p["thr"] if app.values[self.cc] >= t)

    def handle(self, e, app):
        if not self.visible or e.type != pygame.MOUSEBUTTONDOWN or e.button not in (1, 3):
            return False
        if not self.rect.collidepoint(e.pos):
            return False
        states = TOGGLES[self.cc]["states"]
        step = 1 if e.button == 1 else -1
        idx = (self.index(app) + step) % len(states)
        app.set_value(self.cc, states[idx][0])
        return True

    def draw(self, surf, app):
        if not self.visible:
            return
        p = TOGGLES[self.cc]
        idx = self.index(app)
        two = len(p["states"]) == 2
        lit = idx > 0 if two else True
        r = self.rect
        label = p["label"]
        txt(surf, label, fit_size(label, r.w + 14, (13, 12, 11, 10)), DIM, (r.centerx, r.y - 11), "center")
        pygame.draw.rect(surf, (8, 14, 32), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN if not (two and idx) else (36, 84, 150), r, border_radius=6)
        pygame.draw.rect(surf, AMBER if (lit and (idx or not two)) else BTN_EDGE, r, 1, border_radius=6)
        led(surf, (r.x + 12, r.centery), two and idx > 0)
        state_txt = p["states"][idx][1]
        col = AMBER if (idx > 0 or not two) else DIM
        txt(surf, state_txt, fit_size(state_txt, r.w - 30, (14, 13, 12, 11, 10)), col,
            (r.centerx + 8, r.centery), "center", bold=True)


LOADED_FG = (120, 255, 150)
LOADED_GLOW = (20, 90, 40)


class Selector:
    """LCD with prev / next arrows; click the screen for a full list."""

    def __init__(self, key, caption, rect, options):
        self.key, self.caption = key, caption
        self.rect = pygame.Rect(rect)
        self.options = options  # [(value, label)]
        self.lcd_rect = pygame.Rect(self.rect.x, self.rect.y + 20, self.rect.w, 46)
        self.left = pygame.Rect(self.lcd_rect.x, self.lcd_rect.y, 38, self.lcd_rect.h)
        self.right = pygame.Rect(self.lcd_rect.right - 38, self.lcd_rect.y, 38, self.lcd_rect.h)

    def label_for(self, v):
        for val, lab in self.options:
            if val == v:
                return lab
        return "--"

    def step(self, app, d):
        vals = [o[0] for o in self.options]
        cur = app.sel_value(self.key)
        idx = vals.index(cur) if cur in vals else (-1 if d > 0 else 0)
        app.pick(self.key, vals[(idx + d) % len(vals)])

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.lcd_rect.collidepoint(e.pos):
            if self.left.collidepoint(e.pos):
                self.step(app, -1)
            elif self.right.collidepoint(e.pos):
                self.step(app, +1)
            else:
                app.popup = Popup(self.caption, self.options, app.sel_value(self.key),
                                  lambda v, k=self.key: app.pick(k, v))
            return True
        if e.type == pygame.MOUSEWHEEL and self.lcd_rect.collidepoint(pygame.mouse.get_pos()):
            self.step(app, -1 if e.y > 0 else 1)
            return True
        return False

    def draw(self, surf, app):
        txt(surf, " ".join(self.caption.upper()), 12, (186, 212, 246), (self.rect.x + 4, self.rect.y), bold=True)
        cur = app.sel_value(self.key)
        if self.key == "preset" and app.loaded_name:
            lcd(surf, self.lcd_rect, app.loaded_name, fg=LOADED_FG, glow_c=LOADED_GLOW)
        else:
            lcd(surf, self.lcd_rect, self.label_for(cur))
        triangle(surf, self.left.centerx, self.left.centery, -1, AMBER_DIM)
        triangle(surf, self.right.centerx, self.right.centery, +1, AMBER_DIM)


class Button:
    def __init__(self, rect, label, cb, big=False):
        self.rect = pygame.Rect(rect)
        self.label, self.cb, self.big = label, cb, big

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3) and self.rect.collidepoint(e.pos):
            self.cb(e.button)
            return True
        return False

    def draw(self, surf, app, label=None):
        r = self.rect
        hover = r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(surf, (8, 14, 32), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
        pygame.draw.line(surf, (190, 214, 250), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
        pygame.draw.rect(surf, AMBER if hover else BTN_EDGE, r, 1, border_radius=6)
        s = label or self.label
        txt(surf, s, fit_size(s, r.w - 14, (15, 14, 13, 12, 11), bold=True),
            (255, 255, 255) if hover else TEXT, r.center, "center", bold=True)


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
                app.save_sound()
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
        pygame.draw.rect(surf, WELL, r, border_radius=5)
        pygame.draw.rect(surf, AMBER if self.focus else BTN_EDGE, r, 1, border_radius=5)
        shown = self.text + ("_" if self.focus and int(time.time() * 2) % 2 == 0 else "")
        if not self.text and not self.focus:
            txt(surf, "name this sound...", 14, (96, 120, 156), (r.x + 8, r.centery), "midleft")
        else:
            txt(surf, shown[-24:], 15, TEXT, (r.x + 8, r.centery), "midleft")


class Popup:
    ROW_H, ROWS = 30, 12  # defaults; Popup picks bigger grids for long lists

    def __init__(self, title, options, current, on_pick):
        self.title, self.options, self.current, self.on_pick = title, options, current, on_pick
        if len(options) > 60:
            self.ROWS, self.ROW_H = 25, 24
        cols = math.ceil(len(options) / self.ROWS)
        colw = min(440, max(font(16).size(lab)[0] for _, lab in options) + 56)
        rows = min(self.ROWS, len(options))
        self.panel = pygame.Rect(0, 0, cols * colw + 24, rows * self.ROW_H + 62)
        self.panel.center = (W // 2, H // 2 - 20)
        self.items = []
        for i, (val, lab) in enumerate(options):
            c, rw = divmod(i, self.ROWS)
            self.items.append((pygame.Rect(self.panel.x + 12 + c * colw, self.panel.y + 46 + rw * self.ROW_H,
                                           colw - 4, self.ROW_H - 2), val, lab))

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
        dim.fill((0, 0, 0, 170))
        surf.blit(dim, (0, 0))
        rounded_metal(surf, self.panel, 12)
        pygame.draw.rect(surf, AMBER_DIM, self.panel, 1, border_radius=12)
        txt(surf, " ".join(self.title.upper()), 13, AMBER, (self.panel.x + 18, self.panel.y + 16), bold=True)
        mouse = pygame.mouse.get_pos()
        for r, val, lab in self.items:
            if r.collidepoint(mouse):
                pygame.draw.rect(surf, (50, 100, 170), r, border_radius=5)
            if val == self.current:
                pygame.draw.rect(surf, AMBER_DIM, r, 1, border_radius=5)
            txt(surf, lab, 16, AMBER if val == self.current else TEXT, (r.x + 10, r.centery), "midleft")




class Info:
    """Modal text box with a title, wrapped paragraphs and buttons [(label, callback or None)]."""

    def __init__(self, title, paras, buttons):
        self.title, self.buttons_def = title, buttons
        self.lines = []
        for p in paras:
            if p == "":
                self.lines.append(("", False))
                continue
            head = p.startswith("#")
            for ln in wrap(p.lstrip("# "), 15, 700):
                self.lines.append((ln, head))
        h = 84 + len(self.lines) * 22 + 70
        self.panel = pygame.Rect(0, 0, 780, h)
        self.panel.center = (W // 2, H // 2 - 10)
        self.btns = []
        bw = 190
        x = self.panel.centerx - (len(buttons) * (bw + 12) - 12) // 2
        for lab, cb in buttons:
            self.btns.append((pygame.Rect(x, self.panel.bottom - 56, bw, 38), lab, cb))
            x += bw + 12

    def handle(self, e, app):
        if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
            app.popup = None
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for r, lab, cb in self.btns:
                if r.collidepoint(e.pos):
                    app.popup = None
                    if cb:
                        cb()
                    return True
        return True

    def draw(self, surf, app):
        dim = pygame.Surface((W, H), pygame.SRCALPHA)
        dim.fill((0, 6, 20, 190))
        surf.blit(dim, (0, 0))
        rounded_metal(surf, self.panel, 12)
        pygame.draw.rect(surf, AMBER, self.panel, 1, border_radius=12)
        txt(surf, " ".join(self.title.upper()), 14, (255, 255, 255), (self.panel.x + 24, self.panel.y + 20), bold=True)
        y = self.panel.y + 60
        for ln, head in self.lines:
            txt(surf, ln, 15, (255, 255, 255) if head else TEXT, (self.panel.x + 28, y), bold=head)
            y += 22
        for r, lab, cb in self.btns:
            hover = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
            pygame.draw.rect(surf, AMBER if hover else BTN_EDGE, r, 1, border_radius=6)
            txt(surf, lab, 14, (255, 255, 255), r.center, "center", bold=True)


# =============================================================================
# THE APP
# =============================================================================
TABS = ["MAIN", "ADVANCED FX", "LIBRARY", "HELP"]


def gate_band(v):
    if v == 0:
        return "GATE OFF"
    if v <= 5:
        return "MILD GATE"
    if v <= 12:
        return "STANDARD GATE"
    return "MAXIMUM GATE"


def wrap(text, size, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if font(size).size(t)[0] <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


class App:
    def __init__(self, headless=False, tab=0):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("V-AMP CONTROL")
        self.data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        self.library = [dict(name=p["name"], bytes=list(p["bytes"])) for p in self.data["library"]]
        self.header = self.data["header"]
        try:
            self.names = json.loads(NAMES_FILE.read_text())
        except Exception:
            self.names = {}
        self.apply_names()
        self.values = dict(DEFAULTS)
        self.preset = None
        self.loaded_name = None          # name of the library sound on screen (shown green in the Preset LCD)
        self.lib_sel = 0
        self.tab = tab
        self.running = True
        self.drag = None
        self.popup = None
        self.help_scroll = 0
        self.status = "Nothing is sent until you touch a control. Press READ UNIT to see the unit's sound."
        self.status_t = time.time()
        self.mon = deque(maxlen=11)
        self.flash_out = 0.0
        self.flash_in = 0.0
        self.ignore_in_until = 0.0
        self.last_out = {}
        self.sx = None
        self.rx_note = ""
        self.tuner_on = False

        self.out = None
        self.inp = None
        self.out_lock = threading.Lock()
        self.settings = {}
        try:
            self.settings = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
        self.channel = int(self.settings.get("channel", 0)) & 15
        if pgmidi:
            try:
                pgmidi.init()
            except Exception as ex:
                self.set_status(f"MIDI unavailable: {ex}")
        self.refresh_devices()
        self.open_saved_devices()

        self.q = deque()
        self.qlock = threading.Lock()
        threading.Thread(target=self._sender_loop, daemon=True).start()

        self.bg = self._make_bg()
        self.sounds = []
        self.sound_off = 0
        self.refresh_sounds()
        self.help_lines = []
        for kind, text in HELP_TEXT:
            if kind == "H":
                self.help_lines.append(("H", text))
            else:
                pre = "-  " if kind == "B" else ""
                for i, ln in enumerate(wrap(text, 15, 1240)):
                    self.help_lines.append(("T", (pre if i == 0 else ("   " if kind == "B" else "")) + ln))
        self.build_ui()
        self.load_default(startup=True)

    # ---- status / log -------------------------------------------------------
    def set_status(self, s):
        self.status, self.status_t = s, time.time()

    def log(self, direction, text):
        self.mon.append((direction, text))

    def save_settings(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    # ---- MIDI devices -------------------------------------------------------
    def refresh_devices(self):
        self.out_devs, self.in_devs = [], []
        if not pgmidi:
            return
        seen = {}
        for i in range(pgmidi.get_count()):
            _, name, is_in, is_out, _ = pgmidi.get_device_info(i)
            name = name.decode(errors="replace")
            kind = "in" if is_in else "out"
            n = seen.get((kind, name), 0)
            seen[(kind, name)] = n + 1
            (self.in_devs if is_in else self.out_devs).append((i, f"{name}#{n}", name))
        print("MIDI outputs:", [n for _, n, _ in self.out_devs])
        print("MIDI inputs: ", [n for _, n, _ in self.in_devs])

    def open_saved_devices(self):
        for kind in ("out", "in"):
            key = self.settings.get(kind + "_name")
            if key:
                self.open_device(kind, key)

    def open_device(self, kind, key):
        devs = self.out_devs if kind == "out" else self.in_devs
        with self.out_lock:
            cur = self.out if kind == "out" else self.inp
            if cur:
                try:
                    cur.close()
                except Exception:
                    pass
            if kind == "out":
                self.out = None
            else:
                self.inp = None
            if key is None:
                return
            for i, k, _ in devs:
                if k == key:
                    try:
                        dev = pgmidi.Output(i) if kind == "out" else pgmidi.Input(i)
                    except Exception as ex:
                        self.set_status(f"Could not open {key}: {ex}")
                        return
                    if kind == "out":
                        self.out = dev
                    else:
                        self.inp = dev
                    return

    def choose_device(self, kind, _button=1):
        self.refresh_devices()
        devs = self.out_devs if kind == "out" else self.in_devs
        opts = [(None, "(none)")] + [(k, k.replace("#0", "") if k.endswith("#0") else k) for _, k, _ in devs]

        def pick(key):
            self.settings[kind + "_name"] = key
            self.open_device(kind, key)
            self.save_settings()
        self.popup = Popup("MIDI OUT (to the V-AMP's MIDI IN)" if kind == "out" else "MIDI IN (from the V-AMP's MIDI OUT)",
                           opts, self.settings.get(kind + "_name"), pick)

    def cycle_channel(self, button):
        self.channel = (self.channel + (1 if button == 1 else -1)) % 16
        self.settings["channel"] = self.channel
        self.save_settings()

    # ---- sending ------------------------------------------------------------
    def write_midi(self, kind, d1, d2=0):
        ch = self.channel & 15
        with self.out_lock:
            if self.out:
                try:
                    if kind == "cc":
                        self.out.write_short(0xB0 | ch, d1, d2)
                    else:
                        self.out.write_short(0xC0 | ch, d1)
                except Exception as ex:
                    self.status = f"MIDI write error: {ex}"
        if kind == "cc":
            self.last_out[d1] = time.time()
            self.log("OUT", f"{LABELS.get(d1, 'CC' + str(d1))[:12]:<12}{d2:>4}  c{d1}")
        else:
            self.log("OUT", f"Program Change {d1:>3}")
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
            self.write_midi(item[0], item[1], item[2])
            time.sleep(GAP + item[3])

    def send_live(self, cc, v):
        with self.qlock:
            for i, m in enumerate(self.q):
                if m[4] == "live" and m[1] == cc:
                    self.q[i] = ("cc", cc, v, m[3], "live")
                    return
            self.q.append(("cc", cc, v, SETTLE.get(cc, 0.0), "live"))

    def send_seq(self, items, group):
        with self.qlock:
            if group:
                self.q = deque(m for m in self.q if m[4] != group)
            for kind, d1, d2 in items:
                self.q.append((kind, d1, d2, SETTLE.get(d1, 0.0) if kind == "cc" else 0.0, group))

    # ---- state changes ------------------------------------------------------
    def set_value(self, cc, v):
        v = clamp(cc, v)
        if self.values[cc] == v:
            return
        # the unit's own Mix knobs also switch their block on - mirror that
        if cc == 18 and self.values[22] < 64:
            self.values[22] = 127
            self.send_live(22, 127)
        if cc == 59 and self.values[21] < 64:
            self.values[21] = 127
            self.send_live(21, 127)
        self.values[cc] = v
        self.send_live(cc, v)

    def sel_value(self, key):
        return {"preset": self.preset, "amp": self.values[19], "cab": self.values[23],
                "fx": self.values[20], "rev": self.values[24]}[key]

    def pick(self, key, value):
        self.ignore_in_until = time.time() + ECHO_IGNORE
        if key == "preset":
            self.preset = value
            self.loaded_name = None
            self.send_seq([("pc", value, 0)], "pc")
            self.set_status(f"Program Change {value} sent. Press READ UNIT to refresh the screen from the unit.")
        elif key == "amp":
            self.values[19] = value
            self.send_seq([("cc", 19, value)] + [("cc", c, self.values[c]) for c in (12, 13, 14, 15, 16)], "amp")
        elif key == "cab":
            self.values[23] = value
            self.send_seq([("cc", 23, value)], "cab")
        elif key == "rev":
            self.values[24] = value
            self.send_seq([("cc", 24, value)], "rev")
        elif key == "fx":
            self.values[20] = value
            self.send_seq([("cc", 20, value)] + [("cc", c, self.values[c]) for c in range(44, 60)], "fx")

    def new_sound(self, _b=1):
        self.values = dict(DEFAULTS)
        self.loaded_name = None
        self.send_all()
        self.set_status("New sound: everything switched off first, then defaults.")

    def send_all(self, _b=1):
        self.ignore_in_until = time.time() + ECHO_IGNORE
        self.send_seq(build_load_sequence(self.values), "load")
        self.set_status("Sent the whole sound: effects / reverb / drive / wah OFF first, switches back ON last.")

    def bypass_all(self, _b=1):
        for cc in (21, 22, 26, 27):
            self.values[cc] = 0
        self.send_seq([("cc", 21, 0), ("cc", 22, 0), ("cc", 26, 0), ("cc", 27, 0)], "bypass")
        self.set_status("Effects, reverb, drive and wah are OFF.")

    def wah_off(self, _b=1):
        self.values[27] = 0
        self.send_seq([("cc", 27, 0)], "wah")

    def tuner(self, _b=1):
        if not self.tuner_on:
            self.tuner_on = True
            self.send_seq([("pc", 127, 0)], "pc")
            self.set_status("Tuner ON (Program Change 127). Press TUNER again to leave it.")
            return
        self.tuner_on = False
        p = self.preset if self.preset is not None else 0
        seq = [("pc", p, 0)]
        if self.preset is None:       # we do not know which memory was active: put the on-screen sound back instead
            seq += build_load_sequence(self.values)
        self.ignore_in_until = time.time() + ECHO_IGNORE
        self.send_seq(seq, "pc")
        self.set_status(f"Tuner OFF: recalled unit preset {p // 5 + 1:02d}{'ABCDE'[p % 5]}"
                        + ("." if self.preset is not None else " and re-sent the on-screen sound."))

    def factory_reset(self, _b=1):
        self.popup = Info("Factory reset the V-AMP", [
            "# This is done on the unit itself. MIDI has no factory-reset command.",
            "",
            "# Restore ALL original artist presets (manual section 5.5)",
            "1.  Switch the V-AMP off.",
            "2.  Hold the D and E buttons down and switch it on.",
            "3.  Wait until the display reads CL, then let go of D and E.",
            "4.  Press BOTH arrow keys. All user-edited presets are overwritten by the original artist presets.",
            "",
            "# Cancel your edits on ONE preset (section 5.4)",
            "After editing, hold both arrow keys until the display reads Pr, then press the preset button for about 2 seconds to store it.",
            "",
            "# Afterwards",
            "Press RECEIVE here, then on the unit press both arrows (SETUP) and hold MIDI (A) until it shows d. The library will match the unit again.",
        ], [("OK", None), ("NEW SOUND ON SCREEN", self.new_sound)])

    def read_unit(self, _b=1):
        if not self.inp:
            self.set_status("Choose MIDI IN first, so the unit's answer can be heard.")
        self.read_count = 0
        self.send_seq([("cc", 80, 80)], "read")
        self.set_status("Asked the unit for its state (CC80 = 80)...")

    # ---- incoming MIDI -------------------------------------------------------
    def poll_in(self):
        if not self.inp:
            return
        try:
            if not self.inp.poll():
                return
            events = self.inp.read(1024)
        except Exception:
            return
        for data, _ts in events:
            b0 = data[0]
            if self.sx is not None or b0 == 0xF0:
                for b in data[:4]:
                    if b == 0xF0:
                        self.sx = [0xF0]
                    elif self.sx is not None:
                        if b == 0xFE:
                            continue
                        self.sx.append(b)
                        if b == 0xF7:
                            self.on_sysex(self.sx)
                            self.sx = None
                            break
                if self.sx is not None:
                    self.rx_note = f"receiving dump... {len(self.sx)} bytes"
                    self.flash_in = time.time()
                continue
            if b0 == 0xFE:
                continue
            if (b0 & 15) != self.channel:
                continue
            self.flash_in = time.time()
            kind, d1, d2 = b0 & 0xF0, data[1], data[2]
            if kind == 0xC0:
                self.tuner_on = d1 == 127
                if d1 != 127:
                    self.preset = d1
                    self.loaded_name = None
                self.log("IN ", f"Program Change {d1:>3}")
            elif kind == 0xB0:
                tgt = {61: 19, 60: 20}.get(d1, d1)
                if tgt in self.values and tgt != 80:
                    self.log("IN ", f"{LABELS.get(tgt, 'CC' + str(tgt))[:12]:<12}{d2:>4}  c{d1}")
                    now = time.time()
                    if now >= self.ignore_in_until and now - self.last_out.get(d1, 0.0) > 0.3:
                        self.values[tgt] = clamp(tgt, d2)

    @staticmethod
    def is_lib_dump(msg):
        """Full 125-preset dump. Byte 4 is the unit's device id (0 on one unit, 1 on another), so it is not compared."""
        return len(msg) == 6010 and msg[:4] == [0xF0, 0, 0x20, 0x32] and msg[5] == 0x11 and msg[6] == 0x21

    def on_sysex(self, msg):
        if self.is_lib_dump(msg):
            self.load_dump_bytes(msg, "unit")
            try:      # keep a copy of everything the unit sends, just in case
                DUMPS_DIR.mkdir(parents=True, exist_ok=True)
                fn = DUMPS_DIR / ("vamp_received_" + time.strftime("%Y%m%d_%H%M%S") + ".syx")
                fn.write_bytes(bytes(msg))
                self.set_status(self.status + f" Copy saved as {fn.name}.")
            except Exception:
                pass
        elif len(msg) == 58 and msg[:4] == [0xF0, 0, 0x20, 0x32] and msg[5] == 0x11 and msg[6] == 0x20:
            rec = msg[9:57]            # one preset: 32 data bytes + 16 name characters
            name = bytes(rec[32:48]).decode("latin1").rstrip()
            self.values = block_to_values(rec[:32])
            self.textbox.text = name
            self.loaded_name = name or None
            self.rx_note = ""
            self.set_status(f"Received the unit's current sound '{name}'. Shown on screen only; nothing was sent back to the unit.")
        else:
            self.log("IN ", f"SysEx {len(msg)} bytes")
            try:
                DUMPS_DIR.mkdir(parents=True, exist_ok=True)
                fn = DUMPS_DIR / ("VAMP_UNRECOGNISED_dump_" + time.strftime("%Y%m%d_%H%M%S") + ".syx")
                fn.write_bytes(bytes(msg))
                self.rx_note = ""
                self.set_status(f"Received a SysEx of {len(msg)} bytes that is not a full V-AMP library dump. Kept as {fn.name} in data_backups\\dumps.")
            except Exception:
                pass

    def load_dump_bytes(self, msg, origin):
        body = msg[9:-1]
        self.header = list(msg[:9])
        lib = []
        for i in range(125):
            r = body[i * 48:(i + 1) * 48]
            lib.append(dict(name=bytes(r[32:48]).decode("latin1").rstrip(), bytes=list(r[:32])))
        self.library = lib
        self.rx_note = ""
        kept = self.apply_names()
        self.refresh_preset_opts()
        self.set_status(f"Library loaded from {origin}: 125 presets" + (f", {kept} of your own names kept." if kept else "."))

    def dump_bytes(self):
        out = bytearray(self.header)
        for p in self.library:
            out += bytes(p["bytes"])
            out += p["name"].ljust(16)[:16].encode("latin1", "replace")
        out.append(0xF7)
        return bytes(out)

    def apply_names(self):
        """Names you gave to slots survive a fresh dump when the unit still calls that slot USER PRESET."""
        n = 0
        for k, name in self.names.items():
            i = int(k)
            cur = self.library[i]["name"].strip().upper()
            if cur in ("", "USER PRESET"):
                self.library[i]["name"] = name
                n += 1
        return n

    def save_names(self):
        try:
            NAMES_FILE.write_text(json.dumps(self.names, indent=1))
        except Exception:
            pass

    def refresh_preset_opts(self):
        self.sel_main[0].options = [(i, f"{i // 5 + 1:02d}{'ABCDE'[i % 5]}  {self.library[i]['name']}") for i in range(125)]

    # ---- library --------------------------------------------------------------
    def lib_click(self, i, button):
        self.lib_sel = i
        if button != 1:
            return
        p = self.library[i]
        self.values = block_to_values(p["bytes"])
        self.textbox.text = p["name"]
        self.loaded_name = p["name"].strip() or None
        self.send_all()
        self.set_status(f"Library {i // 5 + 1:02d}{'ABCDE'[i % 5]} '{p['name']}' loaded and sent (Bypass first).")

    def lib_store(self, _b=1):
        name = self.textbox.text.strip()[:16] or self.library[self.lib_sel]["name"]
        self.library[self.lib_sel] = dict(name=name, bytes=values_to_block(self.values))
        self.names[str(self.lib_sel)] = name
        self.loaded_name = name
        self.save_names()
        self.refresh_preset_opts()
        self.set_status(f"Stored into slot {self.lib_sel // 5 + 1:02d}{'ABCDE'[self.lib_sel % 5]} as '{name}'. "
                        "SAVE DUMP to keep the library, SEND DUMP to put it on the unit.")

    def lib_receive(self, _b=1):
        if not self.inp:
            self.set_status("Choose MIDI IN first. Then on the unit: both arrows (SETUP), hold MIDI (A) until it shows d.")
        else:
            self.set_status("Listening. On the unit: both arrows (SETUP), then hold MIDI (A) until the display shows d.")

    def load_default(self, _b=1, startup=False):
        dd = DEFAULT_DUMP if DEFAULT_DUMP.exists() else OLD_DUMPS_DIR / "factory_default.syx"
        if not dd.exists():
            if not startup:
                self.set_status("No default yet: RECEIVE a dump, then press SET DEFAULT.")
            return
        try:
            raw = list(dd.read_bytes())
            if not self.is_lib_dump(raw):
                raise ValueError("not a V-AMP dump")
            self.load_dump_bytes(raw, "your default (factory_default.syx)")
        except Exception as ex:
            self.set_status(f"Could not read factory_default.syx: {ex}")

    def set_default(self, _b=1):
        DUMPS_DIR.mkdir(parents=True, exist_ok=True)
        DEFAULT_DUMP.write_bytes(self.dump_bytes())
        self.set_status("Saved as your default library (data_backups/dumps/factory_default.syx). It loads every time the app starts.")

    def lib_save(self, _b=1):
        DUMPS_DIR.mkdir(parents=True, exist_ok=True)
        p = DUMPS_DIR / f"library_{datetime.now():%Y%m%d_%H%M%S}.syx"
        p.write_bytes(self.dump_bytes())
        self.set_status(f"Saved {p.name} in the dumps folder.")

    def lib_load(self, _b=1):
        DUMPS_DIR.mkdir(parents=True, exist_ok=True)
        paths = {}
        for d in (OLD_DUMPS_DIR, DUMPS_DIR):          # the new folder wins if a name exists in both
            if d.exists():
                for f in d.glob("*.syx"):
                    paths[f.name] = f
        files = sorted(paths.values(), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            self.set_status("No .syx files in the dumps folder.")
            return

        def pick(name):
            try:
                self.load_dump_bytes(list(paths[name].read_bytes()), name)
            except Exception as ex:
                self.set_status(f"Could not read {name}: {ex}")
        self.popup = Popup("LOAD DUMP", [(f.name, f.name) for f in files], None, pick)

    def lib_send(self, _b=1):
        def go(v):
            if v:
                data = self.dump_bytes()
                threading.Thread(target=self._send_sysex, args=(data,), daemon=True).start()
        self.popup = Popup("SEND DUMP: THIS OVERWRITES ALL 125 PRESETS ON THE UNIT",
                           [(1, "YES - overwrite the unit's presets"), (0, "Cancel")], 0, go)

    def _send_sysex(self, data):
        with self.out_lock:
            if not self.out:
                self.set_status("Choose MIDI OUT first.")
                return
            try:
                self.out.write_sys_ex(0, list(data))
            except Exception as ex:
                self.set_status(f"SysEx send error: {ex}")
                return
        self.flash_out = time.time()
        self.log("OUT", f"SysEx dump {len(data)} bytes")
        self.set_status("Dump sent (about 2 seconds at MIDI speed). Watch the unit's display.")

    # ---- sounds ---------------------------------------------------------------
    def refresh_sounds(self):
        SOUNDS_DIR.mkdir(exist_ok=True)
        files = sorted(SOUNDS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        self.sounds = [p.stem for p in files]

    def save_sound(self, _b=1):
        name = re.sub(r"[^A-Za-z0-9 _.-]", "_", self.textbox.text).strip()
        if not name:
            self.set_status("Type a name first.")
            return
        SOUNDS_DIR.mkdir(exist_ok=True)
        vals = {str(k): v for k, v in self.values.items() if k not in LIVE_ONLY}
        (SOUNDS_DIR / f"{name}.json").write_text(json.dumps({"version": 1, "name": name, "values": vals}, indent=2))
        self.refresh_sounds()
        self.sound_off = 0
        self.set_status(f"Saved sound '{name}'.")

    def load_sound(self, name):
        try:
            d = json.loads((SOUNDS_DIR / f"{name}.json").read_text())
        except Exception as ex:
            self.set_status(f"Could not load '{name}': {ex}")
            return
        vals = dict(DEFAULTS)
        for k, v in d.get("values", {}).items():
            if int(k) in vals and int(k) not in LIVE_ONLY:
                vals[int(k)] = clamp(int(k), v)
        self.values = vals
        self.textbox.text = name
        self.send_all()
        self.set_status(f"Loaded '{name}' (everything off first, then amp, knobs, effect, switches).")

    def export_mid(self, _b=1):
        EXPORT_DIR.mkdir(exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9 _.-]", "_", self.textbox.text).strip() or "vamp_sound"
        p = EXPORT_DIR / f"{name}.mid"
        export_midi(build_load_sequence(self.values), self.channel, p)
        self.set_status(f"Exported exports/{p.name}: put it on a MIDI track routed to the V-AMP.")

    # ---- UI construction ----------------------------------------------------
    def build_ui(self):
        d = self.data
        lib = self.library
        presets = [(i, f"{i // 5 + 1:02d}{'ABCDE'[i % 5]}  {lib[i]['name']}") for i in range(125)]
        self.preset_opts = presets
        amp_opts = [(i, a["name"]) for i, a in enumerate(d["amps"])]
        cab_opts = [(i, c) for i, c in enumerate(d["cabs"])]
        fx_opts = [(i, e["name"]) for i, e in enumerate(d["effects"])]
        rev_opts = [(i, r["name"]) for i, r in enumerate(d["reverbs"])]

        self.sel_main = [
            Selector("preset", "Preset (Program Change)", (30, 124, 400, 70), presets),
            Selector("amp", "Amp Model", (450, 124, 500, 70), amp_opts),
            Selector("cab", "Cabinet", (970, 124, 400, 70), cab_opts),
            Selector("rev", "Reverb Type", (50, 476, 520, 70), rev_opts),
            Selector("fx", "Effect Type", (630, 476, 480, 70), fx_opts),
        ]
        self.panels = {
            0: [((30, 222, 690, 206), "Amp"), ((740, 222, 250, 206), "Noise Gate"), ((1010, 222, 360, 206), "Wah / Pedal"),
                ((30, 440, 560, 260), "Reverb"), ((610, 440, 520, 260), "Effects"), ((1150, 440, 220, 260), "MIDI Monitor"),
                ((30, 712, 1340, 72), "Quick Actions")],
            1: [((30, 130, 430, 270), "Pre Effect"), ((480, 130, 520, 270), "Delay"), ((1020, 130, 350, 270), "Post Effect")],
        }
        self.knobs, self.toggles = [], []   # (tab, widget)

        def knob(tab, cc, cx, cy):
            self.knobs.append((tab, Knob(cc, cx, cy)))

        def toggle(tab, cc, cx, cy, w=92):
            self.toggles.append((tab, Toggle(cc, cx, cy, w)))

        for i, cc in enumerate([12, 13, 14, 15, 16]):
            knob(0, cc, 95 + i * 95, 290)
        toggle(0, 26, 625, 290, 110)
        knob(0, 25, 865, 290)
        knob(0, 27, 1075, 290)
        knob(0, 7, 1310, 290)
        knob(0, 18, 115, 624); toggle(0, 22, 250, 626, 110)
        knob(0, 59, 700, 624); toggle(0, 21, 835, 626, 110)
        toggle(1, 44, 245, 190, 140)
        for i, cc in enumerate([45, 46, 47, 48]):
            knob(1, cc, 85 + i * 95, 300)
        toggle(1, 49, 740, 190, 140)
        for i, cc in enumerate([50, 51, 52, 53, 54]):
            knob(1, cc, 535 + i * 90, 300)
        toggle(1, 55, 1195, 190, 140)
        for i, cc in enumerate([56, 57, 58, 59]):
            knob(1, cc, 1062 + i * 82, 300)

        self.top_btns = [
            Button((480, 6, 360, 26), "", lambda b: self.choose_device("out", b)),
            Button((480, 38, 360, 26), "", lambda b: self.choose_device("in", b)),
            Button((880, 18, 80, 34), "", self.cycle_channel),
            Button((975, 12, 185, 44), "READ UNIT", self.read_unit, big=True),
            Button((1175, 12, 195, 44), "NEW SOUND", self.new_sound, big=True),
        ]
        self.out_btn, self.in_btn, self.ch_btn = self.top_btns[:3]
        self.tab_btns = [Button((30 + i * 150, 82, 140, 30), t, lambda b, i=i: self.set_tab(i)) for i, t in enumerate(TABS)]
        self.main_btns = [
            Button((1135, 268, 120, 40), "WAH OFF", self.wah_off),
            Button((60, 738, 200, 40), "BYPASS ALL", self.bypass_all),
            Button((270, 738, 160, 40), "TUNER", self.tuner),
            Button((440, 738, 200, 40), "SEND ALL", self.send_all),
            Button((650, 738, 200, 40), "FACTORY RESET...", self.factory_reset),
        ]
        self.lib_btns = [
            Button((30, 124, 150, 36), "RECEIVE", self.lib_receive),
            Button((190, 124, 150, 36), "LOAD DUMP", self.lib_load),
            Button((350, 124, 150, 36), "SAVE DUMP", self.lib_save),
            Button((510, 124, 170, 36), "STORE HERE", self.lib_store),
            Button((690, 124, 150, 36), "SEND DUMP", self.lib_send),
            Button((850, 124, 170, 36), "SET DEFAULT", self.set_default),
            Button((1030, 124, 170, 36), "LOAD DEFAULT", self.load_default),
        ]
        self.textbox = TextBox((120, 806, 230, 32))
        self.save_btn = Button((360, 806, 84, 32), "SAVE", self.save_sound)
        self.mid_btn = Button((452, 806, 100, 32), "EXPORT MID", self.export_mid)
        self.arrow_l = Button((650, 806, 32, 32), "<", lambda b: self.page_sounds(-1))
        self.arrow_r = Button((1316, 806, 32, 32), ">", lambda b: self.page_sounds(+1))
        self.chips = []
        self.lib_cells = []

    def set_tab(self, i):
        self.tab = i

    def page_sounds(self, d):
        self.sound_off = max(0, min(max(0, len(self.sounds) - 1), self.sound_off + d))

    def _make_bg(self):
        import random
        bg = pygame.Surface((W, H))
        for y in range(H):
            t = y / H
            sheen = max(0.0, 1.0 - abs(t - 0.10) * 6.0) * 30
            col = (int(14 + 20 * (1 - t) + sheen * 0.5), int(30 + 40 * (1 - t) + sheen * 0.8), int(70 + 70 * (1 - t) + sheen))
            pygame.draw.line(bg, col, (0, y), (W, y))
        rnd = random.Random(7)
        brush = pygame.Surface((W, H), pygame.SRCALPHA)
        for _ in range(2600):
            y = rnd.randrange(H); x = rnd.randrange(W); ln = rnd.randrange(80, 500)
            c = (255, 255, 255, rnd.randrange(2, 6)) if rnd.random() < 0.5 else (0, 0, 24, rnd.randrange(3, 8))
            pygame.draw.line(brush, c, (x, y), (x + ln, y))
        bg.blit(brush, (0, 0))
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
            self.drag.drag_by(e.rel[1], self)
            return
        if e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = None
            return
        if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE and not self.textbox.focus:
            self.running = False
            return
        if self.textbox.handle(e, self):
            return
        if self.tab == 3 and e.type == pygame.MOUSEWHEEL:
            self.help_scroll = max(0, min(self.help_max(), self.help_scroll - e.y * 40))
            return
        widgets = list(self.top_btns) + list(self.tab_btns) + [self.save_btn, self.mid_btn, self.arrow_l, self.arrow_r]
        if self.tab == 0:
            widgets += self.sel_main + self.main_btns
        if self.tab == 2:
            widgets += self.lib_btns
        widgets += [w for t, w in self.knobs if t == self.tab] + [w for t, w in self.toggles if t == self.tab]
        for w in widgets:
            if w.handle(e, self):
                return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            for r, name in self.chips:
                if r.collidepoint(e.pos) and e.button == 1:
                    self.load_sound(name)
                    return
            if self.tab == 2:
                for r, i in self.lib_cells:
                    if r.collidepoint(e.pos):
                        self.lib_click(i, e.button)
                        return

    def help_max(self):
        return max(0, len(self.help_lines) * 24 + 40 - 610)

    # ---- drawing ------------------------------------------------------------
    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        now = time.time()

        txt(s, "V-AMP", 40, (236, 244, 255), (30, 8), bold=True)
        txt(s, "M I D I   C O N T R O L   S U R F A C E", 11, (170, 200, 240), (32, 52))
        out_name = (self.settings.get("out_name") or "none  (click to choose)").replace("#0", "")
        in_name = (self.settings.get("in_name") or "none  (click to choose)").replace("#0", "")
        self.out_btn.draw(s, self, f"MIDI OUT:  {out_name}"[:44])
        self.in_btn.draw(s, self, f"MIDI IN:   {in_name}"[:44])
        led(s, (858, 19), now - self.flash_out < 0.12, AMBER)
        led(s, (858, 51), now - self.flash_in < 0.12, GREEN)
        self.ch_btn.draw(s, self, f"CH {self.channel + 1}")
        self.top_btns[3].draw(s, self)
        self.top_btns[4].draw(s, self)

        for i, b in enumerate(self.tab_btns):
            if i == self.tab:
                pygame.draw.rect(s, (36, 96, 170), b.rect, border_radius=6)
                pygame.draw.rect(s, AMBER, b.rect, 1, border_radius=6)
                txt(s, b.label, 14, (255, 255, 255), b.rect.center, "center", bold=True)
            else:
                b.draw(s, self)

        if self.tab == 0:
            self.draw_main(s, now)
        elif self.tab == 1:
            self.draw_adv(s)
        elif self.tab == 2:
            self.draw_lib(s, now)
        else:
            self.draw_help(s)

        panel(s, (30, 796, 1340, 54), "")
        txt(s, "SOUND", 13, AMBER_DIM, (50, 814), bold=True)
        self.textbox.draw(s, self)
        self.save_btn.draw(s, self)
        self.mid_btn.draw(s, self)
        txt(s, "LOAD", 13, AMBER_DIM, (574, 814), bold=True)
        self.arrow_l.draw(s, self)
        self.arrow_r.draw(s, self)
        self.chips = []
        x = 690
        for name in self.sounds[self.sound_off:]:
            w = min(170, font(14).size(name)[0] + 26)
            if x + w > 1306:
                break
            r = pygame.Rect(x, 806, w, 32)
            hover = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, BTN_HOT if hover else BTN, r, border_radius=6)
            pygame.draw.rect(s, AMBER if hover else BTN_EDGE, r, 1, border_radius=6)
            txt(s, name[:22], 14, AMBER if hover else TEXT, r.center, "center")
            self.chips.append((r, name))
            x += w + 8
        if not self.sounds:
            txt(s, "no saved sounds yet", 13, (140, 164, 200), (690, 822), "midleft")

        if self.popup:
            self.popup.draw(s, self)

    def status_line(self, s, pos):
        if self.rx_note:
            txt(s, self.rx_note[:80], 12, GREEN, pos)
        elif time.time() - self.status_t < 20:
            if self.tab == 0:
                for i, ln in enumerate(wrap(self.status, 12, 480)[:2]):
                    txt(s, ln, 12, (190, 226, 255), (pos[0], pos[1] - 8 + i * 15))
            else:
                txt(s, self.status[:(150 if self.tab == 2 else 150)], 12, (190, 226, 255), pos)

    def draw_main(self, s, now):
        for sel in self.sel_main[:3]:
            sel.draw(s, self)
        amp = self.data["amps"][self.values[19]]
        txt(s, f"models: {amp['desc']}", 12, DIM, (452, 200))
        for rect, title in self.panels[0]:
            panel(s, rect, title)
        for sel in self.sel_main[3:]:
            sel.draw(s, self)
        rv = self.data["reverbs"][self.values[24]]
        for i, ln in enumerate(wrap(rv["desc"], 12, 510)[:4]):
            txt(s, ln, 12, DIM, (52, 556 + i * 15))
        fx = self.data["effects"][self.values[20]]
        txt(s, fx["desc"], 12, DIM, (632, 556))
        txt(s, "FX Mix is the unit's Effects Mix knob (CC59).", 11, (130, 156, 196), (632, 574))
        for tab, k in self.knobs:
            if tab == 0:
                k.draw(s, self)
        for tab, t in self.toggles:
            if tab == 0:
                t.draw(s, self)
        txt(s, gate_band(self.values[25]), 13, AMBER, (865, 380), "center", bold=True)
        txt(s, "ON" if self.values[27] > 0 else "OFF", 13, AMBER if self.values[27] > 0 else DIM, (1195, 330), "center", bold=True)
        led(s, (1175, 330), self.values[27] > 0)
        for b in self.main_btns:
            if b.label == "TUNER":
                b.draw(s, self, "TUNER  ON" if self.tuner_on else "TUNER")
                led(s, (b.rect.x + 14, b.rect.centery), self.tuner_on, RED)
            else:
                b.draw(s, self)
        mx, my = 1166, 476
        for i, (dirn, line) in enumerate(self.mon):
            txt(s, f"{dirn} {line}"[:29], 12, AMBER if dirn == "OUT" else GREEN, (mx, my + i * 15))
        self.status_line(s, (870, 752))
        if self.preset is not None and now - self.status_t > 20:
            txt(s, f"unit preset {self.preset // 5 + 1:02d}{'ABCDE'[self.preset % 5]}", 12, DIM, (870, 752))

    def draw_adv(self, s):
        for rect, title in self.panels[1]:
            panel(s, rect, title)
        for tab, k in self.knobs:
            if tab == 1:
                k.draw(s, self)
        for tab, t in self.toggles:
            if tab == 1:
                t.draw(s, self)
        notes = ["These are the raw effect blocks (CC44 - CC59). The manual does not name the parameters per effect, so they are generic.",
                 "Delay time = Time Hi (0-117) and Time Lo (0-127). Use SAVE to keep a sound; changes are sent live to the unit.",
                 "Pre / Delay / Post type buttons: click to step (right-click steps back).",
                 "After choosing an effect type on the MAIN tab, these values are re-sent so the unit keeps your settings."]
        for i, ln in enumerate(notes):
            txt(s, ln, 13, DIM, (40, 430 + i * 22))
        self.status_line(s, (40, 540))

    def draw_lib(self, s, now):
        for b in self.lib_btns:
            b.draw(s, self)
        self.status_line(s, (34, 163))
        self.lib_cells = []
        x0, y0, cw, rh = 100, 196, 252, 23
        for c, letter in enumerate("ABCDE"):
            txt(s, letter, 14, AMBER_DIM, (x0 + c * cw + cw // 2, 180), "center", bold=True)
        mouse = pygame.mouse.get_pos()
        for i in range(125):
            bank, c = divmod(i, 5)
            r = pygame.Rect(x0 + c * cw, y0 + bank * rh, cw - 4, rh - 2)
            if c == 0:
                txt(s, f"{bank + 1:02d}", 13, AMBER_DIM, (x0 - 14, r.centery), "midright", bold=True)
            name = self.library[i]["name"]
            sel = i == self.lib_sel
            hov = r.collidepoint(mouse)
            pygame.draw.rect(s, (40, 100, 180) if sel else ((50, 84, 140) if hov else (24, 42, 82)), r, border_radius=4)
            pygame.draw.rect(s, AMBER if sel else (74, 104, 156), r, 1, border_radius=4)
            txt(s, name, 13, (120, 144, 182) if name.upper().startswith("USER PRESET") else ((255, 255, 255) if sel else TEXT),
                (r.x + 8, r.centery), "midleft")
            self.lib_cells.append((r, i))

    def draw_help(self, s):
        panel(s, (30, 124, 1340, 656), "Help")
        clip = pygame.Rect(46, 148, 1308, 612)
        s.set_clip(clip)
        y = 160 - self.help_scroll
        for kind, text in self.help_lines:
            if kind == "H":
                y += 8
                txt(s, " ".join(text), 14, AMBER, (56, y), bold=True)
                y += 26
            else:
                txt(s, text, 15, TEXT, (60, y))
                y += 24
        s.set_clip(None)

    def run(self, shot=None):
        clock = pygame.time.Clock()
        frames = 0
        while self.running:
            for e in pygame.event.get():
                self.handle(e)
            self.poll_in()
            self.draw()
            pygame.display.flip()
            clock.tick(60)
            frames += 1
            if shot and frames >= 3:
                pygame.image.save(self.screen, shot)
                break
        self.shutdown()

    def shutdown(self):
        with self.out_lock:
            for dev in (self.out, self.inp):
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
    tab = 0
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]
    if "--tab" in sys.argv:
        tab = int(sys.argv[sys.argv.index("--tab") + 1])
    App(headless=bool(shot), tab=tab).run(shot)


if __name__ == "__main__":
    main()
