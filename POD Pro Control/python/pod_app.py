#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
POD PRO CONTROL  -  MIDI control surface for the Line 6 POD Pro (1999).

Run:      python pod_app.py
Needs:    pip install pygame
Labels:   edit pod_data.json (amp / cab / effect / preset names)

Everything the app knows about the unit comes from your MIDI-OX captures:
  * amp model CC12 values are NOT in knob order (see amp_knob_order in the json)
  * changing amp (CC12) makes the unit re-send a burst of ~22 CCs, so we always
    send the amp first and the individual knob values after it
  * changing effect (CC19) does the same for that effect's own parameters
"""
import json
import math
import os
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
DATA_FILE = BASE / "pod_data.json"
SOUNDS_DIR = BASE / "sounds"
SETTINGS_FILE = BASE / "settings.json"
DUMPS_DIR = (BASE.parent / "data_backups" / "dumps") if BASE.name.lower() == "data" else BASE / "dumps"   # .syx backups
OLD_DUMPS_DIR = BASE / "dumps"                   # where earlier versions kept them (still listed)
NAMES_FILE = BASE / "pod_program_names.json"     # program names read from the unit

W, H = 1400, 860

# ---- timing (tune these if the unit ever drops messages) --------------------
GAP = 0.006                              # seconds between any two messages
SETTLE = {12: 0.10, 19: 0.10, 71: 0.04}  # extra wait after these CCs (unit sends its own burst)
ECHO_IGNORE = 0.6                        # ignore incoming CCs this long after amp/effect change

BYPASS = 10  # CC19 value for Bypass

# ---- palette ----------------------------------------------------------------
AMBER = (255, 196, 176)
AMBER_DIM = (214, 104, 92)
TEXT = (250, 238, 232)
DIM = (214, 160, 150)
PANEL = (110, 20, 24)
EDGE = (226, 128, 118)
LCD_BG = (24, 6, 4)
LCD_FG = (255, 190, 70)
LCD_GLOW = (126, 78, 18)
BTN = (124, 30, 34)
BTN_HOT = (176, 52, 52)
BTN_EDGE = (222, 120, 108)
WELL = (42, 6, 8)
GREEN = (90, 230, 130)
RED = (255, 70, 60)

# =============================================================================
# PARAMETER TABLE  (single source of truth: UI, send, save and load all use it)
# =============================================================================
KNOBS = {}    # cc -> dict(label, default)
TOGGLES = {}  # cc -> dict(label, default, states[(value,text)], thresholds)


def K(cc, label, default):
    KNOBS[cc] = {"label": label, "default": default}


def T(cc, label, default, states, thresholds):
    TOGGLES[cc] = {"label": label, "default": default, "states": states, "thr": thresholds}


ONOFF = [(0, "OFF"), (127, "ON")]
ONOFF_THR = [64]

# amp
K(13, "Drive", 40); K(20, "Drive 2", 64); K(14, "Bass", 64); K(15, "Mid", 64); K(16, "Treble", 64)
K(17, "Ch Volume", 100); K(21, "Presence", 40)
T(25, "Distortion", 0, ONOFF, ONOFF_THR); T(26, "Drive Boost", 127, ONOFF, ONOFF_THR)
T(27, "Presence On", 127, ONOFF, ONOFF_THR); T(73, "Bright", 127, ONOFF, ONOFF_THR)
# gate
T(22, "Noise Gate", 127, ONOFF, ONOFF_THR); K(23, "Gate Thresh", 100); K(24, "Gate Decay", 80)
# pedals
K(4, "Wah Pos", 127); K(7, "Volume", 127); K(46, "Vol Min", 0)
K(9, "Dig Out Gain", 64)  # live only: never sent by New Sound / Load
T(47, "Vol Pedal", 0, [(0, "BEFORE TUBE"), (127, "AFTER TUBE")], ONOFF_THR)
# reverb
T(36, "Reverb", 127, ONOFF, ONOFF_THR); T(37, "Reverb Type", 0, [(0, "SPRING"), (127, "HALL")], ONOFF_THR)
K(18, "Rev Level", 40); K(38, "Rev Decay", 40); K(39, "Rev Tone", 64)
K(40, "Rev Diffusion", 100); K(41, "Rev Density", 100)
# wah
T(43, "Wah", 0, ONOFF, ONOFF_THR); K(44, "Wah Low", 0); K(45, "Wah High", 127)
# effects
K(1, "Tweak", 64)
T(42, "Compressor", 0, [(0, "OFF"), (33, "1.4:1"), (56, "2:1"), (79, "3:1"), (102, "6:1"), (121, "INF:1")],
  [22, 45, 68, 91, 114])
T(50, "Mod FX", 0, ONOFF, ONOFF_THR)
K(58, "Trem Speed", 11); K(59, "Trem Depth", 90)
K(51, "Mod Speed", 64); K(52, "Mod Depth", 90); K(54, "PreDelay", 40)
K(53, "Feedback", 127)    # 0-63 = negative (0 = most), 64-127 = positive (127 = most)
T(55, "Rotary", 127, [(0, "SLOW"), (127, "FAST")], ONOFF_THR)
K(56, "Rotary Max", 2); K(57, "Rotary Min", 8)
T(48, "Vol Swell", 0, ONOFF, ONOFF_THR); K(49, "Swell Ramp", 64)
T(28, "Delay", 0, ONOFF, ONOFF_THR)
K(30, "Delay Time", 16); K(62, "Delay Fine", 32); K(32, "Delay Fdbk", 90); K(34, "Delay Level", 64)

# selectors (CC values, kept in the same values dict)
SELECT_DEFAULTS = {12: 1, 71: 11, 19: BYPASS}
LABELS = {12: "Amp Model", 71: "Cab", 19: "Effect", 64: "Tap Tempo"}

DEFAULTS = dict(SELECT_DEFAULTS)
for _cc, _p in list(KNOBS.items()) + list(TOGGLES.items()):
    DEFAULTS[_cc] = _p["default"]
    LABELS[_cc] = _p["label"]

# ---- what the unit resets when we change amp / effect (from your captures) --
AMP_RESET = [13, 14, 15, 16, 21, 17, 24, 23, 73, 25, 26, 27,
             37, 38, 39, 40, 41, 46, 47, 45, 44]


def amp_default_values(data, amp):
    """What the unit itself sets when this amp is picked (from your captures)."""
    return {int(k): v for k, v in data.get("amp_defaults", {}).get(str(amp), {}).items()}


def amp_reset_list(data, amp):
    """CCs the unit resets on an amp change. Drive 2 (CC20) only exists on some amps."""
    ccs = list(AMP_RESET)
    if 20 in amp_default_values(data, amp):
        ccs.insert(1, 20)
    return ccs


STATIC_AFTER = [22, 18, 36, 43, 4, 7]
SWITCH_CCS = {28, 48, 50}

MOD_CCS = {
    "none": [],
    "comp": [42],
    "trem": [50, 58, 59],
    "chorus": [50, 51, 52, 54, 53],
    "flanger": [50, 51, 52, 54, 53],
    "rotary": [50, 55, 56, 57],
    "swell": [48, 49],
}
DELAY_CCS = [28, 30, 62, 32, 34]


def active_ccs(eff):
    """Controls shown for an effect, in display order."""
    if eff["cc"] == BYPASS:
        return []
    ccs = [1] + MOD_CCS[eff["mod"]]
    if eff["delay"]:
        ccs += DELAY_CCS
    return ccs


def effect_send_list(values, eff):
    """Effect parameters first, on/off switches last."""
    ccs = active_ccs(eff)
    ordered = [c for c in ccs if c not in SWITCH_CCS] + [c for c in ccs if c in SWITCH_CCS]
    return [("cc", c, values[c]) for c in ordered]


def effect_by_cc(data, v):
    for e in data["effects"]:
        if e["cc"] == v:
            return e
    return data["effects"][0]


def build_load_sequence(values, data):
    """Full sound load. Effects ALWAYS start on Bypass, amp goes before its knobs,
    the real effect is selected last, then its own parameters."""
    seq = [("cc", 19, BYPASS),
           ("cc", 12, values[12]),
           ("cc", 71, values[71])]
    seq += [("cc", c, values[c]) for c in amp_reset_list(data, values[12])]
    seq += [("cc", c, values[c]) for c in STATIC_AFTER]
    if values[19] != BYPASS:
        seq.append(("cc", 19, values[19]))
        seq += effect_send_list(values, effect_by_cc(data, values[19]))
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


def fit_size(s, maxw, sizes=(24, 22, 20, 18, 16, 14, 12), bold=False):
    for sz in sizes:
        if font(sz, bold).size(s)[0] <= maxw:
            return sz
    return sizes[-1]


def arc_pts(cx, cy, r, a0, a1, steps=48):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / steps))) for i in range(steps + 1)]


_metal_cache = {}
TOP_C, BOT_C = (190, 48, 46), (72, 8, 12)


def metal(w, h, top=TOP_C, bot=BOT_C):
    key = (w, h, top, bot)
    if key not in _metal_cache:
        import random
        sf = pygame.Surface((w, h), pygame.SRCALPHA)
        for y in range(h):
            t = y / max(1, h - 1)
            k = 1.0 - t
            sheen = max(0.0, 1.0 - abs(t - 0.18) * 5.0) * 30
            col = tuple(min(255, int(bot[i] + (top[i] - bot[i]) * k * k + sheen)) for i in range(3))
            pygame.draw.line(sf, col, (0, y), (w, y))
        rnd = random.Random(w * 31 + h)
        for _ in range(max(40, w * h // 900)):
            y = rnd.randrange(h); x = rnd.randrange(w)
            ln = rnd.randrange(w // 6, w // 2 + 2)
            c = (255, 255, 255, rnd.randrange(2, 6)) if rnd.random() < 0.5 else (30, 0, 0, rnd.randrange(3, 7))
            pygame.draw.line(sf, c, (x, y), (min(w, x + ln), y))
        _metal_cache[key] = sf
    return _metal_cache[key]


def rounded_metal(surf, rect, radius=10):
    r = pygame.Rect(rect)
    img = metal(r.w, r.h).copy()
    mask = pygame.Surface((r.w, r.h), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=radius)
    img.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    surf.blit(img, r.topleft)


def panel(surf, rect, title):
    r = pygame.Rect(rect)
    rounded_metal(surf, r, 10)
    pygame.draw.rect(surf, (36, 4, 6), r, 1, border_radius=10)
    pygame.draw.rect(surf, EDGE, r.inflate(-2, -2), 1, border_radius=9)
    pygame.draw.line(surf, (255, 214, 204), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (255, 224, 216), (r.x + 16, r.y + 8), bold=True)
    pygame.draw.line(surf, (70, 8, 12), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
    pygame.draw.line(surf, (214, 110, 100), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (30, 4, 6), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (236, 170, 160), (sx, r.bottom - 12), 4, 1)
        pygame.draw.line(surf, (190, 120, 112), (sx - 2, r.bottom - 14), (sx + 2, r.bottom - 10))


def lcd(surf, rect, text, max_size=24, fg=None, glow_c=None):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, (30, 4, 6), r.inflate(6, 6), border_radius=8)
    pygame.draw.rect(surf, (236, 170, 160), r.inflate(6, 6), 1, border_radius=8)
    pygame.draw.rect(surf, LCD_BG, r, border_radius=6)
    for y in range(r.y + 3, r.bottom - 3, 3):
        pygame.draw.line(surf, (40, 22, 8), (r.x + 3, y), (r.right - 4, y))
    pygame.draw.rect(surf, (110, 70, 20), r, 1, border_radius=6)
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
        pygame.draw.circle(surf, (70, 14, 16), pos, 4)
        pygame.draw.circle(surf, (220, 140, 130), pos, 4, 1)


def triangle(surf, cx, cy, direction, color):
    d = 7 * direction
    pygame.draw.polygon(surf, color, [(cx + d, cy), (cx - d, cy - 9), (cx - d, cy + 9)])


# =============================================================================
# WIDGETS
# =============================================================================
# =============================================================================
# POD PRO SYSEX  (Line 6 "POD Pro MIDI / Sysex Specification")
# =============================================================================
SX_HEAD = [0xF0, 0x00, 0x01, 0x0C, 0x01]
SX_REQ_EDIT = SX_HEAD + [0x00, 0x01, 0xF7]        # edit buffer dump request
SX_REQ_ALL = SX_HEAD + [0x00, 0x02, 0xF7]         # all programs dump request
SX_INQUIRY = [0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]  # universal device inquiry (all channels)
PROG_NIB = 142                                     # 71 data bytes = 142 nibble bytes per program (measured on a real unit: 36 x 142 + 9 = 5121)
EDIT_DUMP_LEN = 8 + PROG_NIB + 1                   # F0 00 01 0C 01 01 01 <version> <data> F7
ALL_DUMP_LEN = 8 + 36 * PROG_NIB + 1               # F0 00 01 0C 01 01 02 <version> <data> F7
COMP_CC = [0, 33, 56, 79, 102, 121]


def nib_dec(ns):
    """High-low nibble pairs -> bytes."""
    return bytes(((ns[i] & 15) << 4) | (ns[i + 1] & 15) for i in range(0, len(ns) - 1, 2))


def _h2(v):
    """POD stores 0-63 for a 0-127 controller (MIDI/2); put it back on the 0-127 scale."""
    return 127 if v >= 63 else v * 2


def _le(b, *addrs):
    """Multi-byte field: the listed bytes, the LAST one is the most significant (it holds only the top bits)."""
    v = 0
    for i, a in enumerate(addrs):
        v |= b[a] << (8 * i)
    return v


def decode_program(b, data):
    """72 data bytes of one program -> ({cc: value}, name, raw-bytes note).  Single-byte fields follow the spec
    table exactly; the timing fields (delay time, mod speed ...) span several bytes and are best-effort."""
    c = {}
    for cc, addr in ((25, 0), (26, 1), (27, 2), (28, 3), (50, 4), (36, 5), (22, 6), (73, 7)):
        c[cc] = 127 if b[addr] & 1 else 0
    c[12] = b[8] & 31
    for cc, addr in ((13, 9), (20, 10), (14, 11), (15, 12), (16, 13), (21, 14), (17, 15)):
        c[cc] = _h2(b[addr] & 63)
    c[23] = max(0, min(127, int(round(127 - (b[16] & 127) * 256 / 194))))
    c[24] = _h2(b[17] & 63)
    c[4], c[44], c[45] = b[18] & 127, b[19] & 127, b[20] & 127
    c[7], c[46] = b[22] & 127, b[23] & 127
    c[47] = 127 if b[24] & 1 else 0
    n = _le(b, 26, 27, 28) & 0x1FFFF                      # delay time in samples @31.2kHz = (coarse<<7 | fine) * 6
    m = min(16383, n // 6)
    c[30], c[62] = m >> 7, m & 127
    c[32], c[34], c[9] = _h2(b[34] & 63), _h2(b[36] & 63), _h2(b[35] & 63)
    c[37] = 127 if b[38] & 1 else 0
    c[38], c[39], c[40], c[41], c[18] = (_h2(b[a] & 63) for a in (39, 40, 41, 42, 43))
    c[71] = b[44] & 15
    c[72] = _h2(b[45] & 63)
    fx = b[46] & 15
    c[19] = fx
    c[1] = _h2(b[47] & 63)
    mod = effect_by_cc(data, fx)["mod"]
    c[48] = 127 if mod == "swell" else 0
    if mod == "swell":
        c[49] = _h2(b[48] & 63)
    elif mod == "comp":
        c[42] = COMP_CC[min(5, b[48] & 7)]
    elif mod in ("chorus", "flanger"):
        c[51] = min(127, int(round(_le(b, 48, 49) / 50)))
        c[52] = min(127, int(round(_le(b, 50, 51) * 104 / 256)))
        c[53] = b[52] & 127
        c[54] = min(127, int(round(_le(b, 53, 54) * 42 / 256)))
    elif mod == "rotary":
        c[55] = 127 if b[48] & 1 else 0
        c[56] = max(0, min(127, int(round((_le(b, 49, 50) - 100) / 22))))
        c[57] = max(0, min(127, int(round((_le(b, 51, 52) - 100) / 22))))
    elif mod == "trem":
        c[58] = min(127, int(round(_le(b, 48, 49) / 25)))
        c[59] = b[50] & 127
    name = "".join(chr(x) if 32 <= x < 127 else " " for x in b[55:71]).strip()
    return c, name


def parse_all_dump(msg):
    """Whole all-programs sysex message -> list of 36 (values, name) or None if it is not one."""
    if len(msg) != ALL_DUMP_LEN or msg[:7] != SX_HEAD + [0x01, 0x02] or msg[-1] != 0xF7:
        return None
    return [nib_dec(msg[8 + i * PROG_NIB: 8 + (i + 1) * PROG_NIB]) for i in range(36)]


LOADED_FG = (120, 255, 150)          # preset window: a saved sound you loaded, shown in green
LOADED_GLOW = (20, 90, 40)
RECALL_FG = (130, 210, 255)          # the "preset recalled on the unit" note


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
            step = 1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 3
            app.set_value(self.cc, app.values[self.cc] + e.y * step)
            return True
        return False

    def drag_by(self, dy, app):
        fine = pygame.key.get_mods() & pygame.KMOD_SHIFT
        self.acc = max(0.0, min(127.0, self.acc - dy * (0.25 if fine else 0.9)))
        app.set_value(self.cc, int(round(self.acc)))

    def draw(self, surf, app):
        if not self.visible:
            return
        v = app.values[self.cc]
        t = v / 127.0
        cx, cy, R = self.cx, self.cy, self.R
        pygame.draw.circle(surf, (34, 4, 6), (cx, cy), R + 10)
        pygame.draw.circle(surf, (230, 140, 130), (cx, cy), R + 10, 1)
        pygame.draw.lines(surf, (62, 10, 14), False, arc_pts(cx, cy, R + 5, 135, 405), 4)
        if v > 0:
            a1 = 135 + 270 * t
            pygame.draw.lines(surf, (150, 32, 32), False, arc_pts(cx, cy, R + 5, 135, a1), 8)
            pygame.draw.lines(surf, AMBER, False, arc_pts(cx, cy, R + 5, 135, a1), 4)
        pygame.draw.circle(surf, (110, 108, 116), (cx, cy), R)
        pygame.draw.circle(surf, (160, 160, 172), (cx, cy), R - 3)
        pygame.draw.circle(surf, (128, 126, 138), (cx, cy), R - 8)
        pygame.draw.circle(surf, (198, 198, 210), (cx - 5, cy - 6), R - 18)
        ang = math.radians(135 + 270 * t)
        p0 = (cx + math.cos(ang) * R * 0.30, cy + math.sin(ang) * R * 0.30)
        p1 = (cx + math.cos(ang) * R * 0.88, cy + math.sin(ang) * R * 0.88)
        pygame.draw.line(surf, (40, 6, 8), p0, p1, 3)
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
        pygame.draw.rect(surf, (30, 4, 6), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN if not (two and idx) else (186, 52, 46), r, border_radius=6)
        pygame.draw.rect(surf, AMBER if (lit and (idx or not two)) else BTN_EDGE, r, 1, border_radius=6)
        led(surf, (r.x + 12, r.centery), two and idx > 0)
        state_txt = p["states"][idx][1]
        col = AMBER if (idx > 0 or not two) else DIM
        txt(surf, state_txt, fit_size(state_txt, r.w - 30, (14, 13, 12, 11, 10)), col,
            (r.centerx + 8, r.centery), "center", bold=True)


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
        txt(surf, " ".join(self.caption.upper()), 12, (255, 214, 204), (self.rect.x + 4, self.rect.y), bold=True)
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
        pygame.draw.rect(surf, (30, 4, 6), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
        pygame.draw.line(surf, (255, 200, 190), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
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
            txt(surf, "name this sound...", 14, (200, 130, 124), (r.x + 8, r.centery), "midleft")
        else:
            txt(surf, shown[-24:], 15, TEXT, (r.x + 8, r.centery), "midleft")


class Popup:
    ROW_H, ROWS = 30, 12

    def __init__(self, title, options, current, on_pick):
        self.title, self.options, self.current, self.on_pick = title, options, current, on_pick
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
                pygame.draw.rect(surf, (200, 70, 62), r, border_radius=5)
            if val == self.current:
                pygame.draw.rect(surf, AMBER_DIM, r, 1, border_radius=5)
            txt(surf, lab, 16, AMBER if val == self.current else TEXT, (r.x + 10, r.centery), "midleft")


# =============================================================================
# THE APP
# =============================================================================
class App:
    def __init__(self, headless=False):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("POD PRO CONTROL")
        self.data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        self.apply_program_names()
        self.values = dict(DEFAULTS)
        self.preset = None
        self.preset_unknown = False
        self.channel = 0
        self.running = True
        self.drag = None
        self.popup = None
        self.status = "UI shows defaults. Nothing is sent until you touch a control."
        self.status_t = time.time()
        self.mon = deque(maxlen=7)
        self.sx = None            # sysex being received
        self.view = "main"        # "main" or "backup"
        self.loaded_name = None   # name of the saved sound currently loaded (shown in green in the preset window)
        self.pending_save = None  # (name, time) while a SAVE waits for the unit's exact copy
        self.syx_names = set()
        self.in_count, self.last_in = 0, None     # what has arrived on MIDI IN (for the backup screen)
        self.rx_total = 0
        self.file_rows = []
        self.expect = None        # ("edit"|"all"|"inq", time) while waiting for a reply
        self.flash_out = 0.0
        self.flash_in = 0.0
        self.ignore_in_until = 0.0
        self.last_out = {}  # cc -> time we last sent it (echo guard)

        # MIDI devices
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

        # sender thread
        self.q = deque()
        self.qlock = threading.Lock()
        threading.Thread(target=self._sender_loop, daemon=True).start()

        self.bg = self._make_bg()
        self.sounds = []
        self.sound_off = 0
        self.refresh_sounds()
        self.build_ui()

    def apply_program_names(self):
        """Use the program names read from the unit (if we have them) in the preset list."""
        try:
            names = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
            for k, v in names.items():
                if str(int(k)) in self.data["presets"] and v:
                    self.data["presets"][str(int(k))] = v
        except Exception:
            pass

    # ---- status / log -------------------------------------------------------
    def set_status(self, s):
        self.status, self.status_t = s, time.time()

    def log(self, direction, text):
        self.mon.append((direction, text))

    # ---- MIDI devices -------------------------------------------------------
    def refresh_devices(self):
        self.out_devs, self.in_devs = [], []
        if not pgmidi:
            return
        for i in range(pgmidi.get_count()):
            _, name, is_in, is_out, _ = pgmidi.get_device_info(i)
            name = name.decode(errors="replace")
            (self.in_devs if is_in else self.out_devs).append((i, name))
        print("MIDI outputs:", [n for _, n in self.out_devs])
        print("MIDI inputs: ", [n for _, n in self.in_devs])

    def open_saved_devices(self):
        for kind in ("out", "in"):
            name = self.settings.get(kind + "_name")
            if name:
                self.open_device(kind, name)

    def open_device(self, kind, name):
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
            if name is None:
                return
            for i, n in devs:
                if n == name:
                    try:
                        dev = pgmidi.Output(i) if kind == "out" else pgmidi.Input(i)
                    except Exception as ex:
                        self.set_status(f"Could not open {name}: {ex}")
                        return
                    if kind == "out":
                        self.out = dev
                    else:
                        self.inp = dev
                    return

    def cycle_device(self, kind, button):
        devs = self.out_devs if kind == "out" else self.in_devs
        names = [None] + [n for _, n in devs]
        cur = self.settings.get(kind + "_name")
        idx = names.index(cur) if cur in names else 0
        idx = (idx + (1 if button == 1 else -1)) % len(names)
        self.settings[kind + "_name"] = names[idx]
        self.open_device(kind, names[idx])
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

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
            self.log("OUT", f"{LABELS.get(d1, 'CC' + str(d1))[:15]:<15} {d2:>3}  (CC{d1})")
        else:
            self.log("OUT", f"Program Change  {d1:>3}")
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
            kind, d1, d2, pause = item[0], item[1], item[2], item[3]
            self.write_midi(kind, d1, d2)
            time.sleep(GAP + pause)

    def send_live(self, cc, v):
        """Knob drags: newest value replaces an older queued one for the same CC."""
        with self.qlock:
            for i, m in enumerate(self.q):
                if m[4] == "live" and m[1] == cc:
                    self.q[i] = ("cc", cc, v, m[3], "live")
                    return
            self.q.append(("cc", cc, v, SETTLE.get(cc, 0.0), "live"))

    def send_seq(self, items, group):
        """A group replaces any not-yet-sent items of the same group."""
        with self.qlock:
            if group:
                self.q = deque(m for m in self.q if m[4] != group)
            for kind, d1, d2 in items:
                self.q.append((kind, d1, d2, SETTLE.get(d1, 0.0) if kind == "cc" else 0.0, group))

    # ---- state changes ------------------------------------------------------
    def set_value(self, cc, v):
        v = max(0, min(127, int(v)))
        if self.values[cc] != v:
            self.values[cc] = v
            self.send_live(cc, v)

    def sel_value(self, key):
        return {"preset": self.preset, "amp": self.values[12], "cab": self.values[71],
                "fx": self.values[19]}[key]

    def pick(self, key, value):
        if key == "preset":
            self.preset, self.preset_unknown = value, True
            self.loaded_name = None
            self.send_seq([("pc", value, 0)], "pc")
        elif key == "amp":
            self.values[12] = value
            if not self.settings.get("keep_knobs"):
                self.values.update(amp_default_values(self.data, value))  # the unit's own voicing
            self.ignore_in_until = time.time() + ECHO_IGNORE
            seq = [("cc", 12, value), ("cc", 71, self.values[71])]
            seq += [("cc", c, self.values[c]) for c in amp_reset_list(self.data, value)]
            self.send_seq(seq, "amp")
        elif key == "cab":
            self.values[71] = value
            self.send_seq([("cc", 71, value)], "cab")
        elif key == "fx":
            self.values[19] = value
            self.ignore_in_until = time.time() + ECHO_IGNORE
            seq = [("cc", 19, value)]
            if value != BYPASS:
                seq += effect_send_list(self.values, effect_by_cc(self.data, value))
            self.send_seq(seq, "fx")

    def toggle_keep_knobs(self, _button=1):
        self.settings["keep_knobs"] = not self.settings.get("keep_knobs")
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    def new_sound(self, _button=1):
        self.loaded_name = None
        self.values = dict(DEFAULTS)
        self.values.update(amp_default_values(self.data, self.values[12]))
        self.ignore_in_until = time.time() + ECHO_IGNORE
        self.send_seq(build_load_sequence(self.values, self.data), "load")
        self.set_status("New sound: effects on Bypass, everything else at defaults.")

    # ---- incoming MIDI (the unit's knobs) ------------------------------------
    def poll_in(self):
        if self.expect and time.time() - self.expect[1] > (6 if self.expect[0] == "all" else 3) and self.sx is None:
            what = {"edit": "read the unit", "all": "back up", "inq": "check the unit"}[self.expect[0]]
            self.set_status(f"No reply when asked to {what}. Connect the POD Pro's MIDI OUT to your interface and pick it as MIDI IN.")
            self.expect = None
        if self.pending_save and time.time() - self.pending_save[1] > 6:
            n = self.pending_save[0]
            self.pending_save = None
            self.set_status(f"Saved '{n}' (knob values only). The unit did not send an exact copy: check MIDI IN is the POD Pro's output.")
        if not self.inp:
            return
        for _ in range(8):
            try:
                if not self.inp.poll():
                    return
                events = self.inp.read(256)
            except Exception:
                return
            for data, _ts in events:
                b0 = data[0]
                if self.sx is not None or b0 == 0xF0:
                    for x in data[:4]:
                        if x == 0xF0:
                            self.sx = [0xF0]
                        elif self.sx is not None:
                            if x == 0xFE:
                                continue
                            self.sx.append(x)
                            if x == 0xF7:
                                msg, self.sx = self.sx, None
                                self.rx_total = 0
                                self.in_count += 1
                                self.last_in = (time.time(), f"SysEx complete, {len(msg)} bytes")
                                self.on_sysex(msg)
                                break
                    if self.sx is not None:
                        self.flash_in = time.time()
                        sx = self.sx
                        self.rx_total = ALL_DUMP_LEN if len(sx) > 6 and sx[6] == 2 else (EDIT_DUMP_LEN if len(sx) > 6 else 0)
                        self.last_in = (time.time(), f"SysEx, {len(sx)} bytes so far...")
                        self.status, self.status_t = f"receiving dump... {len(sx)} bytes", time.time()
                    continue
                if b0 == 0xFE or b0 >= 0xF8:
                    continue
                st, d1, d2 = data[0], data[1], data[2]
                self.in_count += 1
                self.last_in = (time.time(), f"{ {0xB0: 'CC', 0xC0: 'Program Change', 0x90: 'Note on', 0x80: 'Note off'}.get(st & 0xF0, hex(st)) }"
                                f" {d1}{'' if (st & 0xF0) == 0xC0 else ' = ' + str(d2)}   (channel {(st & 15) + 1})")
                if (st & 15) != self.channel:
                    continue
                self.flash_in = time.time()
                kind = st & 0xF0
                if kind == 0xC0:
                    self.preset, self.preset_unknown = d1, True
                    self.loaded_name = None
                    self.log("IN ", f"Program Change  {d1:>3}")
                elif kind == 0xB0 and d1 in self.values:
                    self.log("IN ", f"{LABELS.get(d1, 'CC' + str(d1))[:15]:<15} {d2:>3}  (CC{d1})")
                    now = time.time()
                    # ignore echoes: right after an amp/effect change, or right after we sent this same CC
                    if now >= self.ignore_in_until and now - self.last_out.get(d1, 0.0) > 0.3:
                        self.values[d1] = d2

    # ---- sysex: read the unit, back up, restore, check ------------------------------------------
    def _send_sysex(self, data, note):
        with self.out_lock:
            if not self.out:
                self.set_status("Choose MIDI OUT first.")
                return False
            try:
                self.out.write_sys_ex(0, list(data))
            except Exception as ex:
                self.set_status(f"SysEx send error: {ex}")
                return False
        self.flash_out = time.time()
        self.log("OUT", note)
        return True

    def pick_channel(self, _b=1):
        def go(v):
            self.channel = v & 15
            self.settings["channel"] = self.channel
            try:
                SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
            except Exception:
                pass
            self.set_status(f"MIDI channel {self.channel + 1}. Set the POD Pro to the same channel (MIDI button on the unit).")
        self.popup = Popup("MIDI CHANNEL", [(i, f"Channel {i + 1}") for i in range(16)],
                           self.channel, go)

    def tap_tempo(self, _b=1):
        self.send_seq([("cc", 64, 127)], "tap")

    def check_unit(self, _b=1):
        if self._send_sysex(SX_INQUIRY, "Device inquiry"):
            self.expect = ("inq", time.time())
            self.set_status("Asking the unit who it is...")

    def ask_one(self, _b=1):
        if self._send_sysex(SX_REQ_EDIT, "Request edit buffer"):
            self.expect = ("edit", time.time())
            self.set_status("Asked the unit for its current sound. (If nothing arrives, use the front-panel steps.)")

    def ask_all(self, _b=1):
        if self._send_sysex(SX_REQ_ALL, "Request ALL programs"):
            self.expect = ("all", time.time())
            self.set_status("Asked the unit for all 36 programs. If nothing arrives, use the front-panel steps on the left.")

    def list_dumps(self):
        """Saved .syx files: [(path, kind, label, when)] newest first.  kind: all / edit / prog"""
        out = []
        files = []
        for d in (DUMPS_DIR, OLD_DUMPS_DIR):
            if d.exists() and (d == DUMPS_DIR or d != DUMPS_DIR):
                files += list(d.glob("*.syx"))
        if files:
            for f in sorted(set(files), key=lambda q: q.stat().st_mtime, reverse=True):
                if f.name == "last_read.syx":
                    continue
                try:
                    raw = list(f.read_bytes())
                except Exception:
                    continue
                when = time.strftime("%d %b %H:%M", time.localtime(f.stat().st_mtime))
                if parse_all_dump(raw) is not None:
                    out.append((f, "all", "ALL 36 programs", when))
                elif raw[:5] == SX_HEAD and len(raw) >= EDIT_DUMP_LEN and raw[5] == 1 and raw[6] in (0, 1):
                    body = raw[8:-1] if raw[6] == 1 else raw[9:-1]
                    nm = "".join(chr(x) if 32 <= x < 127 else " " for x in nib_dec(body[:PROG_NIB])[55:71]).strip()
                    out.append((f, "edit" if raw[6] == 1 else "prog", f"Sound: {nm or 'unnamed'}", when))
        return out

    def send_file(self, entry):
        f, kind, label, _when = entry

        def go(v):
            if v:
                data = list(f.read_bytes())
                threading.Thread(target=lambda: self._send_sysex(data, f"Send {f.name}") and
                                 self.set_status("Sent. Give the unit a few seconds, then check it."), daemon=True).start()
        if kind == "all":
            self.popup = Popup("SEND ALL 36 PROGRAMS?", [(1, "YES - OVERWRITE ALL 36 PROGRAMS ON THE UNIT"), (0, "Cancel")], 0, go)
        elif kind == "edit":
            self.popup = Popup("SEND THIS SOUND?", [(1, "YES - load it into the unit (EDITED lights, press SAVE on the unit to keep it)"), (0, "Cancel")], 0, go)
        else:
            self.popup = Popup("SEND THIS SOUND?", [(1, "YES - this overwrites a stored program on the unit"), (0, "Cancel")], 0, go)

    def open_dumps_folder(self, _b=1):
        try:
            DUMPS_DIR.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(str(DUMPS_DIR))
            else:
                self.set_status(f"Backups are in: {DUMPS_DIR}")
        except Exception as ex:
            self.set_status(f"Could not open the folder: {ex}")

    def show_backup(self, _b=1):
        self.view = "backup"

    def on_sysex(self, msg):
        self.flash_in = time.time()
        # universal device inquiry reply
        if msg[:2] == [0xF0, 0x7E] and len(msg) >= 16 and msg[3:5] == [0x06, 0x02]:
            ver = bytes(msg[12:16]).decode("ascii", "replace")
            ok = msg[5:8] == [0x00, 0x01, 0x0C]
            fam = msg[10:12]
            self.log("IN ", f"Unit: ver {ver}")
            self.set_status(("Connected: Line 6 POD-family unit, software " + ver +
                             ("  (family/member bytes: %s)" % " ".join("%02X" % x for x in msg[8:12]))) if ok
                            else "A device answered, but it is not a Line 6 unit.")
            self.expect = None
            return
        if msg[:5] != SX_HEAD or len(msg) < 9:
            self.log("IN ", f"SysEx {len(msg)} bytes (ignored)")
            return
        op, typ = msg[5], msg[6]
        if op != 0x01:
            return
        if typ == 0x02:                                   # all programs
            self.expect = None
            progs = parse_all_dump(msg)
            if progs is None:
                try:
                    DUMPS_DIR.mkdir(parents=True, exist_ok=True)
                    fn = DUMPS_DIR / ("POD_Pro_UNRECOGNISED_dump_" + time.strftime("%Y%m%d_%H%M%S") + ".syx")
                    fn.write_bytes(bytes(msg))
                    where = f" It was still saved as {fn}"
                except Exception:
                    where = ""
                self.set_status(f"Got a dump of {len(msg)} bytes but it is not the expected {ALL_DUMP_LEN} bytes for 36 programs.{where}")
                return
            DUMPS_DIR.mkdir(parents=True, exist_ok=True)
            fn = DUMPS_DIR / ("POD_Pro_all_programs_" + time.strftime("%Y%m%d_%H%M%S") + ".syx")
            fn.write_bytes(bytes(msg))
            names = {}
            for i, blk in enumerate(progs):
                names[str(i + 1)] = "".join(chr(x) if 32 <= x < 127 else " " for x in blk[55:71]).strip()
            try:
                NAMES_FILE.write_text(json.dumps(names, indent=1), encoding="utf-8")
            except Exception:
                pass
            self.apply_program_names()
            self.build_ui()
            self.expect = None
            self.log("IN ", "All 36 programs saved")
            self.set_status(f"Backed up all 36 programs. Saved as {fn}  (program names now shown in the Preset list).")
        elif typ in (0x00, 0x01):                         # one program / the edit buffer
            body = msg[8:-1] if typ == 0x01 else msg[9:-1]
            label = "edit buffer" if typ == 0x01 else f"program {msg[7] + 1}"
            if len(body) < PROG_NIB:
                self.set_status(f"Got a short {label} dump ({len(body)} bytes).")
                return
            blk = nib_dec(body[:PROG_NIB])
            ps = self.pending_save
            if typ == 0x01 and ps and time.time() - ps[1] < 6:
                self.pending_save = None
                self.expect = None
                self.finish_syx_save(msg, blk, ps[0])
                return
            c, name = decode_program(blk, self.data)
            for cc, v in c.items():
                if cc in self.values:
                    self.values[cc] = v
            if typ == 0x01:
                self.preset_unknown = True
                self.loaded_name = None
            try:
                DUMPS_DIR.mkdir(parents=True, exist_ok=True)
                (DUMPS_DIR / "last_read.syx").write_bytes(bytes(msg))
                safe = re.sub(r"[^A-Za-z0-9 _-]", "_", name).strip() or "unnamed"
                (DUMPS_DIR / f"POD_Pro_sound_{safe}_{time.strftime('%Y%m%d_%H%M%S')}.syx").write_bytes(bytes(msg))
            except Exception:
                pass
            self.log("IN ", f"Read {label}: {name}")
            self.set_status(f"Received {label}: \"{name}\". Saved in {DUMPS_DIR}, and the knobs on the main screen now match it.")
            self.expect = None

    # ---- sounds -------------------------------------------------------------
    def refresh_sounds(self):
        SOUNDS_DIR.mkdir(exist_ok=True)
        files = list(SOUNDS_DIR.glob("*.json")) + list(SOUNDS_DIR.glob("*.syx"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        self.sounds, self.syx_names = [], set()
        for f in files:
            if f.stem not in self.sounds:
                self.sounds.append(f.stem)
            if f.suffix == ".syx":
                self.syx_names.add(f.stem)

    def save_sound(self, _b=1):
        name = re.sub(r"[^A-Za-z0-9 _.-]", "_", self.textbox.text).strip()
        if not name:
            self.set_status("Type a name first.")
            return
        SOUNDS_DIR.mkdir(exist_ok=True)
        (SOUNDS_DIR / f"{name}.json").write_text(json.dumps(
            {"version": 1, "name": name, "values": {str(k): v for k, v in self.values.items()}}, indent=2))
        self.refresh_sounds()
        self.sound_off = 0
        self.loaded_name = name
        self.preset_unknown = False
        if self.out and self.inp:
            # also take an exact copy of the whole sound from the unit (needs MIDI OUT and IN both connected)
            self.pending_save = (name, time.time())
            if self._send_sysex(SX_REQ_EDIT, "Request edit buffer (save)"):
                self.expect = ("edit", time.time())
                self.set_status(f"Saved '{name}'. Now asking the unit for an exact copy of the sound...")
                return
        self.pending_save = None
        self.set_status(f"Saved sound '{name}' (knob values only; connect MIDI IN as well to keep an exact copy).")

    def finish_syx_save(self, msg, blk, name):
        """The unit's edit buffer arrived for a SAVE: store it as <name>.syx with our name inside it."""
        blk = bytearray(blk)
        blk[55:71] = name.encode("ascii", "replace")[:16].ljust(16, b" ")
        nibs = []
        for x in blk[:71]:
            nibs += [x >> 4, x & 15]
        out = list(msg[:8]) + nibs + [0xF7]
        SOUNDS_DIR.mkdir(exist_ok=True)
        (SOUNDS_DIR / f"{name}.syx").write_bytes(bytes(out))
        self.refresh_sounds()
        self.set_status(f"Saved '{name}' with an exact copy of the whole sound (dot = exact copy).")

    def load_sound(self, name):
        fx = SOUNDS_DIR / f"{name}.syx"
        if fx.exists():
            try:
                raw = list(fx.read_bytes())
                body = raw[8:-1]
                c, nm = decode_program(nib_dec(body[:PROG_NIB]), self.data)
            except Exception as ex:
                self.set_status(f"Could not read '{name}.syx': {ex}")
                return
            self.values.update({k: v for k, v in c.items() if k in self.values})
            self.textbox.text = name
            self.loaded_name = name
            self.preset_unknown = False
            threading.Thread(target=self._send_sysex, args=(raw, f"Send sound {name}"), daemon=True).start()
            self.set_status(f"Sent '{name}' to the unit: the exact saved sound (EDITED lights; press SAVE on the unit to store it there).")
            return
        try:
            d = json.loads((SOUNDS_DIR / f"{name}.json").read_text())
        except Exception as ex:
            self.set_status(f"Could not load '{name}': {ex}")
            return
        vals = dict(DEFAULTS)
        for k, v in d.get("values", {}).items():
            if int(k) in vals:
                vals[int(k)] = max(0, min(127, int(v)))
        self.values = vals
        self.textbox.text = name
        self.loaded_name = name
        self.preset_unknown = False
        self.ignore_in_until = time.time() + ECHO_IGNORE
        self.send_seq(build_load_sequence(self.values, self.data), "load")
        self.set_status(f"Loaded '{name}' (Bypass first, then amp, knobs, effect).")

    # ---- UI construction ----------------------------------------------------
    def build_ui(self):
        d = self.data
        presets = []
        for n in range(1, 37):
            bank = f"{(n - 1) // 4 + 1}{'ABCD'[(n - 1) % 4]}"
            presets.append((n, f"{bank}  {d['presets'][str(n)]}"))
        amp_opts = [(v, d["amps"][str(v)]) for v in d["amp_knob_order"]]
        cab_opts = [(int(k), v) for k, v in sorted(d["cabs"].items(), key=lambda kv: int(kv[0]))]
        fx_opts = [(e["cc"], e["name"]) for e in d["effects"]]

        self.selectors = [
            Selector("preset", "Preset", (30, 76, 430, 70), presets),
            Selector("amp", "Amp Model", (480, 76, 460, 70), amp_opts),
            Selector("cab", "Cabinet", (960, 76, 410, 70), cab_opts),
            Selector("fx", "Effect", (50, 666, 310, 70), fx_opts),
        ]
        self.panels = [
            ((30, 182, 650, 222), "Amp"), ((700, 182, 250, 222), "Noise Gate"),
            ((970, 182, 400, 222), "Pedals / Out"), ((30, 416, 720, 210), "Reverb"),
            ((770, 416, 310, 210), "Wah"), ((1100, 416, 270, 210), "MIDI Monitor"),
            ((30, 638, 1340, 152), "Effects"),
        ]
        self.knobs, self.toggles = {}, {}

        def knobs(ccs, x0, dx, cy):
            for i, cc in enumerate(ccs):
                self.knobs[cc] = Knob(cc, x0 + i * dx, cy)

        def toggle(cc, cx, cy, w=92):
            self.toggles[cc] = Toggle(cc, cx, cy, w)

        knobs([13, 14, 15, 16, 21, 17], 95, 90, 254)
        knobs([20], 635, 90, 254)  # Drive 2: shown only for amps that have it
        for i, cc in enumerate([25, 26, 27, 73]):
            toggle(cc, 115 + i * 150, 362)
        knobs([23, 24], 765, 120, 254); toggle(22, 825, 362, 104)
        knobs([4, 7, 46, 9], 1030, 95, 254); toggle(47, 1170, 362, 130)
        knobs([18, 38, 39, 40, 41], 280, 95, 488)
        toggle(36, 125, 482); toggle(37, 125, 556, 104)
        toggle(43, 925, 462); knobs([44, 45], 845, 160, 512)

        # dynamic effect controls: created for all, positioned each frame
        for cc in KNOBS:
            if cc not in self.knobs:
                self.knobs[cc] = Knob(cc, -100, -100)
                self.knobs[cc].visible = False
        for cc in TOGGLES:
            if cc not in self.toggles:
                self.toggles[cc] = Toggle(cc, -100, -100)
                self.toggles[cc].visible = False
        self.effect_ccs = {1, 30, 62, 32, 34, 49, 51, 52, 54, 56, 57, 58, 59,
                           28, 42, 48, 50, 53, 55}

        self.buttons = [
            Button((480, 6, 360, 26), "", lambda b: self.cycle_device("out", b)),
            Button((480, 38, 360, 26), "", lambda b: self.cycle_device("in", b)),
            Button((1180, 12, 190, 44), "NEW SOUND", self.new_sound, big=True),
            Button((960, 12, 200, 44), "", self.toggle_keep_knobs),
        ]
        self.out_btn, self.in_btn, self.new_btn, self.keep_btn = self.buttons
        self.unit_buttons = [
            Button((788, 592, 132, 26), "TAP TEMPO", self.tap_tempo),
            Button((930, 592, 136, 26), "READ FROM UNIT", self.ask_one),
            Button((1112, 566, 248, 40), "BACKUP / RESTORE", self.show_backup, big=True),
            Button((884, 20, 66, 32), "", self.pick_channel),
        ]
        self.back_btn = Button((1180, 12, 190, 44), "BACK TO POD", lambda b: setattr(self, "view", "main"), big=True)
        self.bk_buttons = [
            Button((60, 764, 330, 44), "ASK UNIT: SEND ALL 36", self.ask_all),
            Button((410, 764, 330, 44), "ASK UNIT: SEND THIS SOUND", self.ask_one),
            Button((760, 764, 250, 44), "CHECK UNIT", self.check_unit),
            Button((1030, 764, 310, 44), "OPEN BACKUP FOLDER", self.open_dumps_folder),
        ]
        self.textbox = TextBox((120, 806, 230, 32))
        self.save_btn = Button((360, 806, 84, 32), "SAVE", self.save_sound)
        self.arrow_l = Button((520, 806, 32, 32), "<", lambda b: self.page_sounds(-1))
        self.arrow_r = Button((1316, 806, 32, 32), ">", lambda b: self.page_sounds(+1))
        self.chips = []

    def page_sounds(self, d):
        self.sound_off = max(0, min(max(0, len(self.sounds) - 1), self.sound_off + d))

    def layout_effects(self):
        eff = effect_by_cc(self.data, self.values[19])
        active = active_ccs(eff)
        for cc in self.effect_ccs:
            (self.knobs.get(cc) or self.toggles.get(cc)).visible = False
        for i, cc in enumerate(active):
            cx = 420 + i * 86
            if cc in self.knobs:
                k = self.knobs[cc]
                k.cx, k.cy, k.visible = cx, 704, True
            else:
                t = self.toggles[cc]
                t.rect.w = 78
                t.move(cx, 702)
                t.visible = True
        self._eff_active = active
        self.knobs[20].visible = 20 in amp_default_values(self.data, self.values[12])

    def _make_bg(self):
        import random
        bg = pygame.Surface((W, H))
        for y in range(H):
            t = y / H
            sheen = max(0.0, 1.0 - abs(t - 0.10) * 6.0) * 34
            col = (int(52 + 70 * (1 - t) + sheen), int(8 + 12 * (1 - t) + sheen * 0.3), int(12 + 16 * (1 - t) + sheen * 0.3))
            pygame.draw.line(bg, col, (0, y), (W, y))
        rnd = random.Random(7)
        brush = pygame.Surface((W, H), pygame.SRCALPHA)
        for _ in range(2600):
            y = rnd.randrange(H); x = rnd.randrange(W); ln = rnd.randrange(80, 500)
            c = (255, 255, 255, rnd.randrange(2, 6)) if rnd.random() < 0.5 else (30, 0, 0, rnd.randrange(3, 8))
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
        if self.view == "backup":
            if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                self.view = "main"
                return
            for w in [self.back_btn, self.out_btn, self.in_btn] + self.bk_buttons:
                if w.handle(e, self):
                    return
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                for r, entry in self.file_rows:
                    if r.collidepoint(e.pos):
                        self.send_file(entry)
                        return
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
        widgets = (list(self.selectors) + [b for b in self.buttons] + list(self.unit_buttons) +
                   [self.save_btn, self.arrow_l, self.arrow_r] +
                   [k for k in self.knobs.values() if k.visible] +
                   [t for t in self.toggles.values() if t.visible])
        for w in widgets:
            if w.handle(e, self):
                return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for r, name in self.chips:
                if r.collidepoint(e.pos):
                    self.load_sound(name)
                    return

    # ---- drawing ------------------------------------------------------------
    def wrap_lines(self, text, size, maxw, bold=False):
        out, line = [], ""
        for w in text.split():
            t = (line + " " + w).strip()
            if font(size, bold).size(t)[0] <= maxw:
                line = t
            else:
                out.append(line)
                line = w
        return out + ([line] if line else [])

    def draw_backup(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        now = time.time()
        txt(s, "BACKUP", 36, (255, 240, 234), (30, 6), bold=True)
        txt(s, "A N D   R E S T O R E", 12, (240, 190, 180), (34, 50))
        out_name = self.settings.get("out_name") or "none  (click to choose)"
        in_name = self.settings.get("in_name") or "none  (click to choose)"
        self.out_btn.draw(s, self, f"MIDI OUT:  {out_name}"[:44])
        self.in_btn.draw(s, self, f"MIDI IN:   {in_name}"[:44])
        led(s, (868, 19), now - self.flash_out < 0.12, AMBER)
        led(s, (868, 51), now - self.flash_in < 0.12, GREEN)
        self.back_btn.draw(s, self)

        # ---- instructions
        panel(s, (30, 80, 720, 626), "Instructions")
        y = 118
        def head(t):
            nonlocal y
            txt(s, t, 16, AMBER, (52, y), bold=True)
            y += 28
        def step(n, t):
            nonlocal y
            txt(s, str(n), 15, AMBER, (56, y), bold=True)
            for ln in self.wrap_lines(t, 14, 650):
                txt(s, ln, 14, TEXT, (84, y))
                y += 20
            y += 2
        head("BEFORE YOU START")
        step(1, "Cable: POD Pro MIDI OUT to your interface's MIDI IN. (Backing up only needs this one cable.)")
        step(2, "Pick that interface input as MIDI IN above. Watch the panel on the right: it shows whether anything arrives.")
        y += 2
        head("A.  BACK UP ALL 36 PROGRAMS")
        step(1, "Press the MIDI button on the POD Pro so it is lit.")
        step(2, "Press SAVE. The little display shows \"A\" and SAVE flashes.")
        step(3, "Press SAVE again. The dump starts and takes a few seconds. Do not touch anything else (any other button aborts it).")
        step(4, "The panel on the right counts the bytes. When it finishes, the file appears in SAVED BACKUPS.")
        y += 2
        head("B.  BACK UP ONLY THE SOUND YOU ARE ON")
        step(1, "With the MIDI button NOT lit, pick the sound. Any edits you have made count; the unit sends what is active.")
        step(2, "Press MIDI (lit), then press SAVE. The display shows \"A\".")
        step(3, "Press the UP button so the display changes from \"A\" to \"1\".")
        step(4, "Press SAVE again. The sound arrives here, is saved, and the knobs on the main screen match it.")
        y += 2
        head("RESTORING")
        step(1, "Needs a second cable: interface MIDI OUT to the POD Pro MIDI IN. Click a saved backup on the right.")
        step(2, "A single sound lands in the unit's edit buffer (EDITED lights): press SAVE on the unit to keep it. ALL 36 overwrites every program.")

        # ---- what the app sees
        panel(s, (770, 80, 600, 262), "What the app sees")
        y2 = 118
        ok_in = bool(self.inp)
        txt(s, "MIDI IN port:", 14, DIM, (792, y2))
        txt(s, (self.settings.get("in_name") or "none") if ok_in else "NOT CHOSEN OR NOT OPEN (pick it above)", 14,
            GREEN if ok_in else (255, 150, 130), (930, y2))
        y2 += 26
        txt(s, "Messages seen:", 14, DIM, (792, y2))
        txt(s, str(self.in_count) + ("" if self.in_count else "   (nothing yet: check the cable and port)"), 14, TEXT, (930, y2))
        y2 += 26
        txt(s, "Last message:", 14, DIM, (792, y2))
        if self.last_in:
            txt(s, f"{self.last_in[1]}   ({int(now - self.last_in[0])}s ago)"[:58], 14, TEXT, (930, y2))
        y2 += 34
        txt(s, "Receiving:", 14, DIM, (792, y2))
        bar = pygame.Rect(930, y2 + 2, 410, 18)
        pygame.draw.rect(s, (30, 4, 6), bar, border_radius=5)
        if self.sx is not None and self.rx_total:
            frac = min(1.0, len(self.sx) / self.rx_total)
            pygame.draw.rect(s, AMBER, (bar.x, bar.y, int(bar.w * frac), bar.h), border_radius=5)
            lab = f"{len(self.sx)} / {self.rx_total} bytes"
            txt(s, lab, 12, (0, 0, 0), (bar.centerx + 1, bar.centery + 1), "center", bold=True)
            txt(s, lab, 12, (255, 255, 255), bar.center, "center", bold=True)
        else:
            txt(s, "waiting for a dump", 12, DIM, bar.center, "center")
        pygame.draw.rect(s, BTN_EDGE, bar, 1, border_radius=5)
        y2 += 38
        for ln in self.wrap_lines(self.status, 14, 560)[:4]:
            txt(s, ln, 14, (255, 214, 200), (792, y2))
            y2 += 20

        # ---- saved backups
        panel(s, (770, 356, 600, 350), "Saved backups")
        txt(s, "click one to send it back to the unit", 12, DIM, (1350, 362), "topright")
        self.file_rows = []
        entries = self.list_dumps()
        mouse = pygame.mouse.get_pos()
        for i, entry in enumerate(entries[:9]):
            r = pygame.Rect(790, 394 + i * 30, 560, 27)
            hov = r.collidepoint(mouse)
            pygame.draw.rect(s, BTN_HOT if hov else BTN, r, border_radius=5)
            pygame.draw.rect(s, AMBER if hov else BTN_EDGE, r, 1, border_radius=5)
            txt(s, entry[2][:44], 14, AMBER if hov else TEXT, (r.x + 10, r.centery), "midleft", bold=entry[1] == "all")
            txt(s, entry[3], 13, DIM, (r.right - 10, r.centery), "midright")
            self.file_rows.append((r, entry))
        if not entries:
            txt(s, "none yet. Do step A or B on the left.", 14, (214, 150, 142), (792, 400))
        txt(s, f"Folder: {DUMPS_DIR}"[-86:], 12, DIM, (792, 686), "midleft")

        # ---- other tools
        panel(s, (30, 716, 1340, 110), "")
        txt(s, "OTHER WAYS: ask the unit by MIDI command. Not every unit answers these. If nothing arrives, the front-panel steps above always work.",
            12, (255, 214, 200), (60, 730))
        for b in self.bk_buttons:
            b.draw(s, self)
        if self.popup:
            self.popup.draw(s, self)

    def draw(self):
        if self.view == "backup":
            self.draw_backup()
            return
        s = self.screen
        s.blit(self.bg, (0, 0))
        self.layout_effects()

        # top bar
        txt(s, "POD PRO", 40, (255, 240, 234), (30, 8), bold=True)
        txt(s, "M I D I   C O N T R O L   S U R F A C E", 11, (240, 190, 180), (32, 52))
        out_name = self.settings.get("out_name") or "none  (click to choose)"
        in_name = self.settings.get("in_name") or "none  (click to follow the unit)"
        self.out_btn.draw(s, self, f"MIDI OUT:  {out_name}"[:44])
        self.in_btn.draw(s, self, f"MIDI IN:   {in_name}"[:44])
        now = time.time()
        led(s, (868, 19), now - self.flash_out < 0.12, AMBER)
        led(s, (868, 51), now - self.flash_in < 0.12, GREEN)
        self.new_btn.draw(s, self)
        self.keep_btn.draw(s, self, "AMP CHANGE: KEEP KNOBS" if self.settings.get("keep_knobs")
                           else "AMP CHANGE: FACTORY SETTINGS")

        for sel in self.selectors[:3]:
            sel.draw(s, self)
        if self.preset_unknown:
            txt(s, "preset recalled on the unit - knobs below may not match it", 13, RECALL_FG, (34, 150), bold=True)
        elif now - self.status_t < 12:
            txt(s, self.status[:120], 12, (255, 214, 200), (34, 150))

        for rect, title in self.panels:
            panel(s, rect, title)
        self.selectors[3].draw(s, self)
        for k in self.knobs.values():
            k.draw(s, self)
        for t in self.toggles.values():
            t.draw(s, self)
        for ub in self.unit_buttons:
            ub.draw(s, self, f"CH {self.channel + 1}" if ub.cb == self.pick_channel else None)

        # monitor
        mx, my = 1116, 446
        for i, (dirn, line) in enumerate(self.mon):
            col = AMBER if dirn == "OUT" else GREEN
            txt(s, f"{dirn} {line}"[:34], 12, col, (mx, my + i * 15))

        # sounds bar
        panel(s, (30, 796, 1340, 54), "")
        txt(s, "SOUND", 13, AMBER_DIM, (50, 814), bold=True)
        self.textbox.draw(s, self)
        self.save_btn.draw(s, self)
        txt(s, "LOAD", 13, AMBER_DIM, (466, 814), bold=True)
        self.arrow_l.draw(s, self)
        self.arrow_r.draw(s, self)
        self.chips = []
        x = 562
        for name in self.sounds[self.sound_off:]:
            w = min(170, font(14).size(name)[0] + 26)
            if x + w > 1306:
                break
            r = pygame.Rect(x, 806, w, 32)
            hover = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, BTN_HOT if hover else BTN, r, border_radius=6)
            pygame.draw.rect(s, AMBER if hover else BTN_EDGE, r, 1, border_radius=6)
            if name in self.syx_names:
                pygame.draw.circle(s, AMBER, (r.x + 11, r.centery), 4)
                txt(s, name[:20], 14, AMBER if hover else TEXT, (r.centerx + 5, r.centery), "center")
            else:
                txt(s, name[:22], 14, AMBER if hover else TEXT, r.center, "center")
            self.chips.append((r, name))
            x += w + 8
        if not self.sounds:
            txt(s, "no saved sounds yet", 13, (214, 150, 142), (562, 822), "midleft")

        if self.popup:
            self.popup.draw(s, self)

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
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]
    App(headless=bool(shot)).run(shot)


if __name__ == "__main__":
    main()
