#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
TD-3 CONTROL  -  MIDI control surface for the Roland TD-3 V-Drums percussion sound module (2004).

Run:      python td3_app.py
Needs:    pip install pygame

What the TD-3 listens to (from its owner's manual): MIDI channel 10 (fixed), note-on 0-127 (the pads' note
numbers), key aftertouch (chokes a cymbal), CC4 foot control (the hi-hat pedal), Program Change 0-31 (the 32 kits),
All Sound Off, Reset All Controllers, and System Exclusive (bulk dump).  This app plays the pads on screen, runs a
step sequencer through the module, changes kits, and saves / restores the module's data with Bulk Dump.
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
else:
    BASE = _data_root(Path(__file__).resolve().parent)
SETTINGS_FILE = BASE / "td3_settings.json"
PATTERNS_FILE = BASE / "td3_patterns.json"
BACKUP_DIR = BASE / "td3_backups"

W, H = 1400, 860
GAP = 0.004            # seconds between queued messages
CH = 9                 # MIDI channel 10 (0-based) - fixed on the TD-3

# ---- palette: graphite + mint (re-coloured to red at the end of the build) ----------------------
AMBER = (235, 71, 80)        # accent (name kept so the shared drawing code works)
AMBER_DIM = (230, 31, 41)
ORANGE = (255, 140, 30)
SOLO_RED = (235, 40, 40)
TEXT = (246, 234, 235)
DIM = (172, 150, 150)
PANEL = (49, 39, 39)
EDGE = (150, 122, 122)
LCD_BG = (11, 9, 9)
LCD_FG = (255, 51, 61)
LCD_GLOW = (130, 14, 20)
GREEN = (235, 71, 80)
RED = (255, 90, 80)
BTN = (65, 51, 51)
BTN_HOT = (101, 79, 79)
BTN_EDGE = (149, 119, 119)
WELL = (22, 18, 18)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))
def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


# =============================================================================
# DRAWING HELPERS AND SHARED WIDGETS
# =============================================================================
_font_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _font_cache:
        _font_cache[key] = pygame.font.SysFont(
            "dejavusansmono,consolas,menlo,couriernew,monospace", size, bold=bold)
    return _font_cache[key]


def txt(surf, s, size, color, pos, anchor="topleft", bold=False):
    s = "".join(c for c in str(s) if c.isprintable())
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


def metal(w, h, top=(92, 72, 72), bot=(29, 23, 23)):
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
        for _ in range(max(40, w * h // 3000)):
            y = rnd.randrange(h)
            x = rnd.randrange(w)
            ln = rnd.randrange(w // 6, w // 2 + 2)
            c = (255, 255, 255, rnd.randrange(2, 6)) if rnd.random() < 0.5 else (0, 12, 14, rnd.randrange(2, 4))
            pygame.draw.line(sf, c, (x, y), (min(w, x + ln), y))
        _metal_cache[key] = sf
    return _metal_cache[key]


def rounded_metal(surf, rect, radius=10, top=(92, 72, 72), bot=(29, 23, 23)):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, bot, r, border_radius=radius)
    img = metal(r.w, r.h, top, bot).copy()
    mask = pygame.Surface((r.w, r.h), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=radius)
    img.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    surf.blit(img, r.topleft)


def panel(surf, rect, title):
    r = pygame.Rect(rect)
    rounded_metal(surf, r, 10)
    pygame.draw.rect(surf, (38, 29, 29), r, 1, border_radius=10)
    pygame.draw.rect(surf, EDGE, r.inflate(-2, -2), 1, border_radius=9)
    pygame.draw.line(surf, (248, 238, 239), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (248, 240, 240), (r.x + 16, r.y + 8), bold=True)
    if title:
        pygame.draw.line(surf, (72, 56, 56), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
        pygame.draw.line(surf, (235, 71, 80), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (28, 22, 22), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (232, 202, 203), (sx, r.bottom - 12), 4, 1)
        pygame.draw.line(surf, (173, 151, 151), (sx - 2, r.bottom - 14), (sx + 2, r.bottom - 10))


def lcd(surf, rect, text, max_size=24):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, (28, 22, 22), r.inflate(6, 6), border_radius=8)
    pygame.draw.rect(surf, (207, 194, 194), r.inflate(6, 6), 1, border_radius=8)
    pygame.draw.rect(surf, LCD_BG, r, border_radius=6)
    for y in range(r.y + 3, r.bottom - 3, 3):
        pygame.draw.line(surf, (34, 8, 6), (r.x + 3, y), (r.right - 4, y))
    pygame.draw.rect(surf, (96, 22, 18), r, 1, border_radius=6)
    size = fit_size(text, r.w - 84, sizes=tuple(s for s in (24, 22, 20, 18, 16, 14, 12) if s <= max_size))
    glow = font(size, True).render(text, True, LCD_GLOW)
    main = font(size, True).render(text, True, LCD_FG)
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
        pygame.draw.circle(surf, (62, 49, 49), pos, 4)
        pygame.draw.circle(surf, (183, 163, 163), pos, 4, 1)


def triangle(surf, cx, cy, direction, color):
    d = 7 * direction
    pygame.draw.polygon(surf, color, [(cx + d, cy), (cx - d, cy - 9), (cx - d, cy + 9)])


# =============================================================================
# WIDGETS
# =============================================================================


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
        pygame.draw.rect(surf, (30, 23, 23), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
        pygame.draw.line(surf, (244, 230, 231), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
        pygame.draw.rect(surf, AMBER if hover else BTN_EDGE, r, 1, border_radius=6)
        s = label or self.label
        txt(surf, s, fit_size(s, r.w - 14, (15, 14, 13, 12, 11), bold=True),
            (255, 255, 255) if hover else TEXT, r.center, "center", bold=True)




class Popup:
    ROW_H, ROWS = 30, 12  # defaults; Popup picks bigger grids for long lists

    def __init__(self, title, options, current, on_pick):
        self.title, self.options, self.current, self.on_pick = title, options, current, on_pick
        if len(options) > 60:
            self.ROWS, self.ROW_H = 25, 24
        cols = math.ceil(len(options) / self.ROWS)
        colw = min(440, max([font(16).size(lab)[0] for _, lab in options] or [200]) + 56)
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
                pygame.draw.rect(surf, (158, 131, 131), r, border_radius=5)
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




class TextPrompt:
    """One-line text entry (used for a typed folder path)."""

    def __init__(self, title, prompt, initial, on_ok):
        self.title, self.prompt, self.buf, self.on_ok = title, prompt, initial, on_ok
        self.panel = pygame.Rect(0, 0, 900, 190)
        self.panel.center = (W // 2, H // 2 - 20)
        self.field = pygame.Rect(self.panel.x + 24, self.panel.y + 90, self.panel.w - 48, 36)
        self.okr = pygame.Rect(self.panel.right - 220, self.panel.bottom - 50, 90, 34)
        self.cancelr = pygame.Rect(self.panel.right - 118, self.panel.bottom - 50, 90, 34)

    def handle(self, e, app):
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_ESCAPE:
                app.popup = None
            elif e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                app.popup = None
                self.on_ok(self.buf.strip().strip('"'))
            elif e.key == pygame.K_BACKSPACE:
                self.buf = self.buf[:-1]
            elif e.key == pygame.K_v and (e.mod & pygame.KMOD_CTRL):
                try:
                    pygame.scrap.init()
                    clip = pygame.scrap.get_text()
                    if clip:
                        self.buf += clip.replace("\x00", "").strip()[:200]
                except Exception:
                    pass
            elif e.unicode and e.unicode.isprintable() and len(self.buf) < 200:
                self.buf += e.unicode
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            if self.okr.collidepoint(e.pos):
                app.popup = None
                self.on_ok(self.buf.strip().strip('"'))
            elif self.cancelr.collidepoint(e.pos) or not self.panel.collidepoint(e.pos):
                app.popup = None
        return True

    def draw(self, surf, app):
        dim = pygame.Surface((W, H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 170))
        surf.blit(dim, (0, 0))
        rounded_metal(surf, self.panel, 12)
        pygame.draw.rect(surf, AMBER_DIM, self.panel, 1, border_radius=12)
        txt(surf, " ".join(self.title.upper()), 13, AMBER, (self.panel.x + 18, self.panel.y + 16), bold=True)
        txt(surf, self.prompt, 14, TEXT, (self.panel.x + 24, self.panel.y + 52))
        pygame.draw.rect(surf, WELL, self.field, border_radius=5)
        pygame.draw.rect(surf, AMBER, self.field, 1, border_radius=5)
        shown = self.buf + ("_" if int(time.time() * 2) % 2 == 0 else "")
        txt(surf, shown[-70:], 15, TEXT, (self.field.x + 8, self.field.centery), "midleft")
        mouse = pygame.mouse.get_pos()
        for r, lab in ((self.okr, "OK"), (self.cancelr, "CANCEL")):
            hover = r.collidepoint(mouse)
            pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
            pygame.draw.rect(surf, AMBER if hover else BTN_EDGE, r, 1, border_radius=6)
            txt(surf, lab, 14, (255, 255, 255), r.center, "center", bold=True)


# =============================================================================
# FACTS FROM THE OWNER'S MANUAL
# =============================================================================
KITS = ["Acoustic", "Groove", "Oldies", "2 Step", "Ballad", "Electronic", "Techno House", "Hip-Hop", "Jazz", "Disco Beat",
        "Open Rock", "Standard", "Acid House", "TR-808", "Dirty", "Jungle", "Funk", "Latin House", "Abstract", "Club Mix",
        "Jazz Fusion", "Garage House", "Light Tuning", "Brushes", "R&B", "Techno Cumbia", "Classic R&B", "Pop-Rock",
        "Timbales Kit", "Conga Kit", "Percussion Set 1", "Percussion Set 2"]

# factory note numbers (manual p. 61).  Each kit can change them on the unit, so these are the defaults.
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


# pads on screen: key -> (label, centre x, centre y, radius, inner note, outer-ring note, computer key)
PADS = [
    ("HI-HAT", 450, 225, 58, 42, 22, "F"),
    ("OPEN HAT", 450, 345, 44, 46, 26, "G"),
    ("CRASH 1", 590, 215, 62, 49, 55, "E"),
    ("CRASH 2", 770, 205, 58, 57, 52, "R"),
    ("RIDE", 985, 245, 68, 51, 53, "T"),
    ("TOM 1", 640, 335, 48, 48, None, "U"),
    ("TOM 2", 770, 335, 48, 45, None, "I"),
    ("TOM 3", 940, 430, 56, 41, None, "O"),
    ("SNARE", 540, 505, 60, 38, 40, "J"),
    ("KICK", 730, 620, 85, 36, None, "SPACE"),
]
PEDAL_NOTE = 44
KEYMAP = {pygame.K_SPACE: 36, pygame.K_j: 38, pygame.K_k: 40, pygame.K_f: 42, pygame.K_d: 44, pygame.K_g: 46,
          pygame.K_u: 48, pygame.K_i: 45, pygame.K_o: 41, pygame.K_e: 49, pygame.K_r: 57, pygame.K_t: 51, pygame.K_y: 53}
CHOKES = [("CHOKE CRASH 1", 49), ("CHOKE CRASH 2", 57), ("CHOKE RIDE", 51)]

# sequencer tracks (name, note)
TRACKS = [("KICK", 36), ("SNARE", 38), ("SNARE RIM", 40), ("CLOSED HAT", 42), ("OPEN HAT", 46), ("PEDAL HAT", 44),
          ("TOM 1", 48), ("TOM 2", 45), ("TOM 3", 41), ("CRASH 1", 49), ("RIDE", 51)]
LEVELS = [0, 50, 90, 127]           # step level 0 = off, 1 = soft, 2 = normal, 3 = accent
LEVEL_NEXT = {0: 2, 2: 3, 3: 1, 1: 0}


def S(s):
    """'x' normal, 'X' accent, 'o' soft, '.' off  ->  list of levels"""
    return [{".": 0, "o": 1, "x": 2, "X": 3}[c] for c in s]


PRESETS = {
    "ROCK": (16, 110, 0, {"KICK": "X.......x.x.....", "SNARE": "....X.......X...", "CLOSED HAT": "x.x.x.x.x.x.x.x.", "CRASH 1": "X..............."}),
    "FUNK": (16, 100, 0, {"KICK": "X..x..x...x..x..", "SNARE": "....X..o.o..X..o", "CLOSED HAT": "xoxoxoxoxoxoxoxo", "OPEN HAT": "..............x."}),
    "HOUSE": (16, 124, 0, {"KICK": "X...X...X...X...", "SNARE": "....x.......x...", "OPEN HAT": "..x...x...x...x.", "CLOSED HAT": "o.o.o.o.o.o.o.o."}),
    "HIP-HOP": (16, 88, 30, {"KICK": "X.....x...x.....", "SNARE": "....X.......X...", "CLOSED HAT": "x.x.x.x.x.x.x.x.", "OPEN HAT": "...............x"}),
    "SHUFFLE": (12, 96, 0, {"KICK": "X.....x.....", "SNARE": "...x.....X..", "RIDE": "x.xx.xx.xx.x"}),
}


def describe(m):
    k, ch = m[0] & 0xF0, (m[0] & 0x0F) + 1
    if k == 0x90:
        return f"ch{ch:>2}  note {m[1]:>3} ({note_name(m[1])}) on   velocity {m[2]}"
    if k == 0x80:
        return f"ch{ch:>2}  note {m[1]:>3} off"
    if k == 0xA0:
        return f"ch{ch:>2}  key aftertouch  note {m[1]}  value {m[2]}  ({'choke' if m[2] else 'release'})"
    if k == 0xB0:
        nm = {4: "hi-hat pedal (foot control)", 120: "all sound off", 121: "reset all controllers"}.get(m[1], "")
        return f"ch{ch:>2}  CC{m[1]:>3} = {m[2]:>3}  {nm}"
    if k == 0xC0:
        return f"ch{ch:>2}  program change {m[1]}  (kit {m[1] + 1}: {KITS[m[1]] if m[1] < 32 else '?'})"
    return " ".join(f"{b:02X}" for b in m)
# =============================================================================
# THE APP
# =============================================================================
TABS = ["PLAY", "BEAT", "BACKUP", "MONITOR", "HELP"]

HELP_TEXT = [
    ("H", "Getting connected"),
    ("T", "Connect the TD-3's MIDI IN to a MIDI OUT of your interface and pick it as MIDI OUT at the top. To see pad hits from the TD-3 itself, "
          "and to save its data, also connect the TD-3's MIDI OUT to an interface input and pick it as MIDI IN. The TD-3 always uses MIDI channel 10; "
          "there is nothing to set."),
    ("H", "PLAY"),
    ("T", "Click a pad to hit it. The middle of a cymbal is the bow, the outer ring is the edge; the snare's outer ring is the rim. "
          "Hold SHIFT for a hard hit, CTRL for a soft one, or set VELOCITY on the right. Pads also light when you hit the real pads (MIDI IN)."),
    ("T", "Computer keys:  SPACE kick,  J snare,  K snare rim,  F closed hat,  D pedal hat,  G open hat,  U tom 1,  I tom 2,  O tom 3,  "
          "E crash 1,  R crash 2,  T ride,  Y ride edge."),
    ("T", "Click a kit on the left to change kit (Program Change). The HI-HAT PEDAL slider sends Control Change 4, the foot control. "
          "CHOKE sends the key-aftertouch message the TD-3 uses to choke a cymbal."),
    ("T", "The pads send the TD-3's factory note numbers. If you have changed a pad's note number on the unit, the matching pad here will play a different sound."),
    ("H", "BEAT"),
    ("T", "A 16-step drum pattern played through the TD-3. Click a square to cycle: normal, accent, soft, off. Right-click clears it. "
          "PRESETS load a ready-made groove. SAVE PATTERN keeps yours in td3_patterns.json. The timing runs in its own thread, so it stays steady while "
          "you edit."),
    ("H", "BACKUP  (Bulk Dump)"),
    ("T", "The TD-3 can send all its kits and settings as System Exclusive data. On the TD-3: hold DRUM KIT while switching it on, choose what to save "
          "with + / -, press START LISTENING here, then hold DRUM KIT on the TD-3 to begin sending. When the count stops rising, press SAVE."),
    ("T", "To put a backup back: press DRUM KIT on the TD-3 so it shows the kit number, pick a file in the list and press SEND TO TD-3. "
          "The TD-3's current data is overwritten. Do not switch it off until the progress bar is full."),
    ("H", "What the app cannot do"),
    ("T", "Kit and pad settings (levels, instruments, pad sensitivity) are not available over MIDI except through Bulk Dump, so they are edited on the "
          "unit itself. This app plays, sequences, changes kits and backs up."),
]


class App:
    def __init__(self, headless=False, tab=0):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("TD-3 CONTROL")
        self.tab, self.running, self.popup, self.drag = tab, True, None, None
        self._hits, self.hits = [], []
        self.events = deque(maxlen=3000)
        self.counts = {"in": 0, "out": 0}
        self.flash_out = self.flash_in = 0.0
        self.pad_flash = {}                    # note -> (time, velocity)
        self.status = "Choose MIDI OUT at the top, then click a pad."
        self.status_t = time.time()
        self.out = self.inp = None
        self.out_lock = threading.Lock()
        self.settings, self.patterns = {}, {}
        for f, attr in ((SETTINGS_FILE, "settings"), (PATTERNS_FILE, "patterns")):
            try:
                setattr(self, attr, json.loads(f.read_text()))
            except Exception:
                pass
        self.kit = clamp(int(self.settings.get("kit", 0)), 0, 31)
        self.vel = clamp(int(self.settings.get("vel", 100)), 1, 127)
        self.hat = 0
        self.last_click = {}
        # sequencer
        self.steps = 16
        self.bpm, self.swing = clamp(int(self.settings.get("bpm", 110)), 40, 260), 0
        self.grid = [[0] * 32 for _ in TRACKS]
        self.playing, self.cur_step = False, -1
        self.pat_name = ""
        self.load_preset("ROCK", quiet=True)
        # bulk dump
        self.listening, self.sysex_buf, self.received = False, None, []
        self.recv_t = 0.0
        self.sel_file, self.sending, self.send_pos, self.send_total, self.cancel_send = None, False, 0, 0, False
        self.files = []
        self.sound_scroll = self.mon_scroll = self.help_scroll = 0
        if pgmidi:
            try:
                pgmidi.init()
            except Exception as ex:
                self.set_status(f"MIDI unavailable: {ex}")
        self.refresh_devices()
        self.reopen_all()
        self.q = deque()
        self.qlock = threading.Lock()
        threading.Thread(target=self._sender_loop, daemon=True).start()
        threading.Thread(target=self._seq_loop, daemon=True).start()
        self.bg = self._make_bg()
        self.help_lines = []
        for kind, text in HELP_TEXT:
            if kind == "H":
                self.help_lines.append(("H", text))
            else:
                for ln in wrap(text, 15, 1240):
                    self.help_lines.append(("T", ln))
        self.scan_files()

    def _make_bg(self):
        import random
        bg = pygame.Surface((W, H))
        for y in range(H):
            t = y / H
            sheen = max(0.0, 1.0 - abs(t - 0.10) * 6.0) * 22
            col = (int(14 + 18 * (1 - t) + sheen * 0.6), int(20 + 26 * (1 - t) + sheen * 0.9), int(22 + 28 * (1 - t) + sheen))
            pygame.draw.line(bg, col, (0, y), (W, y))
        rnd = random.Random(11)
        brush = pygame.Surface((W, H), pygame.SRCALPHA)
        for _ in range(2200):
            y = rnd.randrange(H)
            x = rnd.randrange(W)
            ln = rnd.randrange(80, 500)
            c = (255, 255, 255, rnd.randrange(2, 5)) if rnd.random() < 0.5 else (0, 10, 10, rnd.randrange(3, 8))
            pygame.draw.line(brush, c, (x, y), (x + ln, y))
        bg.blit(brush, (0, 0))
        return bg

    def set_status(self, s):
        self.status, self.status_t = s, time.time()

    def log(self, d, text):
        self.events.append((time.time(), d, text))

    # ---- MIDI devices ----------------------------------------------------------------------
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
            (self.in_devs if is_in else self.out_devs).append((i, f"{name}#{n}"))

    def _find(self, kind, key):
        for i, k in (self.out_devs if kind == "out" else self.in_devs):
            if k == key:
                return i
        raise RuntimeError("device is not in the list any more")

    def close_all(self):
        with self.out_lock:
            devs = [self.out, self.inp]
            self.out = self.inp = None
        for d in devs:
            try:
                if d:
                    d.close()
            except Exception:
                pass

    def rescan(self):
        self.close_all()
        if pgmidi:
            try:
                pgmidi.quit()
                pgmidi.init()
            except Exception as ex:
                self.set_status(f"MIDI re-scan failed: {ex}")
        self.refresh_devices()

    def reopen_all(self):
        for kind in ("out", "in"):
            if self.settings.get(kind + "_name"):
                self.open_device(kind, self.settings[kind + "_name"], retry=False)

    def open_device(self, kind, key, retry=True):
        with self.out_lock:
            cur = self.out if kind == "out" else self.inp
            if kind == "out":
                self.out = None
            else:
                self.inp = None
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        if key is None or not pgmidi:
            return
        try:
            i = self._find(kind, key)
            dev = pgmidi.Output(i) if kind == "out" else pgmidi.Input(i, 8192)
        except Exception as ex:
            if retry:
                self.rescan()
                self.reopen_all()
                return
            self.set_status(f"Could not open {key}: {ex}. Close any other program using it (a DAW, MIDI-OX) or re-plug the interface.")
            return
        with self.out_lock:
            if kind == "out":
                self.out = dev
            else:
                self.inp = dev

    def choose_device(self, kind, _b=1):
        self.rescan()
        self.reopen_all()
        devs = self.out_devs if kind == "out" else self.in_devs
        opts = [(None, "(none)")] + [(k, k[:-2] if k.endswith("#0") else k) for _, k in devs]

        def pick(key):
            self.settings[kind + "_name"] = key
            self.open_device(kind, key)
            self.save_settings()
        self.popup = Popup("MIDI OUT (to the TD-3's MIDI IN)" if kind == "out" else "MIDI IN (from the TD-3's MIDI OUT)",
                           opts, self.settings.get(kind + "_name"), pick)

    def save_settings(self):
        self.settings.update(kit=self.kit, vel=self.vel, bpm=self.bpm)
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    # ---- sending ---------------------------------------------------------------------------
    def write_msg(self, m, quiet=False):
        with self.out_lock:
            if self.out:
                try:
                    self.out.write_short(*m)
                except Exception as ex:
                    self.status = f"MIDI write error: {ex}"
        self.flash_out = time.time()
        self.counts["out"] += 1
        if not quiet:
            self.log("OUT", describe(m))

    def _sender_loop(self):
        while True:
            item = None
            with self.qlock:
                if self.q:
                    item = self.q.popleft()
            if item is None:
                time.sleep(0.002)
                continue
            self.write_msg(item[0])
            time.sleep(GAP)

    def send(self, msgs, group=None):
        with self.qlock:
            if group is not None:
                self.q = deque(m for m in self.q if m[1] != group)
            for m in msgs:
                self.q.append((m, group))

    def hit(self, note, vel=None):
        vel = clamp(int(vel or self.vel), 1, 127)
        self.pad_flash[note] = (time.time(), vel)
        self.send([[0x90 | CH, note, vel]])

    def choke(self, note):
        self.send([[0xA0 | CH, note, 127], [0xA0 | CH, note, 0]])

    def set_kit(self, k, _b=1):
        self.kit = clamp(k, 0, 31)
        self.send([[0xC0 | CH, self.kit]], group="pc")
        self.save_settings()

    def set_hat(self, v):
        self.hat = clamp(int(v), 0, 127)
        self.send([[0xB0 | CH, 4, self.hat]], group="cc4")

    def sound_off(self, _b=1):
        self.send([[0xB0 | CH, 120, 0]])
        self.set_status("All Sound Off sent.")

    def reset_ctrl(self, _b=1):
        self.send([[0xB0 | CH, 121, 0]])
        self.set_status("Reset All Controllers sent.")

    # ---- incoming MIDI ---------------------------------------------------------------------
    def poll_in(self):
        if not self.inp:
            return
        try:
            if not self.inp.poll():
                return
            events = self.inp.read(256)
        except Exception:
            return
        for data, _ts in events:
            self.feed(data)

    def feed(self, data):
        """data = up to four MIDI bytes as PortMidi delivers them."""
        b0 = data[0]
        if self.sysex_buf is not None or b0 == 0xF0:
            for b in data[:4]:
                if b == 0xF0:
                    self.sysex_buf = [0xF0]
                elif self.sysex_buf is not None:
                    if b == 0xFE:
                        continue
                    self.sysex_buf.append(b)
                    if b == 0xF7:
                        self.on_sysex(self.sysex_buf)
                        self.sysex_buf = None
                        break
            return
        if b0 in (0xFE, 0xF8, 0xFA, 0xFB, 0xFC) or b0 < 0x80:
            return
        kind = b0 & 0xF0
        m = [b0, data[1]] if kind in (0xC0, 0xD0) else [b0, data[1], data[2]]
        self.flash_in = time.time()
        self.counts["in"] += 1
        self.log("IN ", describe(m))
        if kind == 0x90 and m[2] > 0:
            self.pad_flash[m[1]] = (time.time(), m[2])
        elif kind == 0xB0 and m[1] == 4:
            self.hat = m[2]
        elif kind == 0xC0 and m[1] < 32:
            self.kit = m[1]

    def on_sysex(self, m):
        self.flash_in = time.time()
        self.counts["in"] += 1
        self.log("IN ", f"SysEx  {len(m)} bytes   {' '.join(f'{b:02X}' for b in m[:8])} ...")
        if self.listening:
            self.received.append(list(m))
            self.recv_t = time.time()

    # ---- backup (bulk dump) ---------------------------------------------------------------------
    def scan_files(self):
        try:
            self.files = sorted(BACKUP_DIR.glob("*.syx"), reverse=True)
        except Exception:
            self.files = []

    def start_listening(self, _b=1):
        self.received, self.listening = [], True
        self.set_status("Listening. Now start the bulk dump on the TD-3 (hold DRUM KIT).")

    def stop_listening(self, _b=1):
        self.listening = False

    def save_received(self, _b=1):
        if not self.received:
            return
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            f = BACKUP_DIR / datetime.now().strftime("td3_%Y%m%d_%H%M%S.syx")
            f.write_bytes(bytes(b for m in self.received for b in m))
            self.listening = False
            n = len(self.received)
            self.received = []
            self.scan_files()
            self.sel_file = f
            self.set_status(f"Saved {n} messages to {f.name}.")
        except Exception as ex:
            self.set_status(f"Could not save: {ex}")

    def ask_send_file(self, _b=1):
        f = self.sel_file
        if not f or self.sending:
            return
        self.popup = Info("Send this backup to the TD-3?", [
            f"{f.name} will be sent to the TD-3 and overwrites ALL of its current kit and setup data.",
            "On the TD-3, press DRUM KIT first so it shows the kit number. Do not switch it off until the bar is full."],
            [("SEND", lambda: self.send_file(f)), ("CANCEL", None)])

    def send_file(self, f):
        try:
            data = list(Path(f).read_bytes())
        except Exception as ex:
            self.set_status(f"Could not read {f}: {ex}")
            return
        msgs, cur = [], None
        for b in data:
            if b == 0xF0:
                cur = [0xF0]
            elif cur is not None:
                cur.append(b)
                if b == 0xF7:
                    msgs.append(cur)
                    cur = None
        if not msgs:
            self.set_status("That file has no System Exclusive messages in it.")
            return
        self.sending, self.send_pos, self.send_total, self.cancel_send = True, 0, len(msgs), False

        def run():
            for i, m in enumerate(msgs):
                if self.cancel_send:
                    break
                with self.out_lock:
                    if self.out:
                        try:
                            self.out.write_sys_ex(0, list(m))
                        except Exception as ex:
                            self.status = f"MIDI write error: {ex}"
                self.send_pos = i + 1
                self.flash_out = time.time()
                self.counts["out"] += 1
                time.sleep(0.06)
            self.sending = False
            self.set_status("Backup sent." if not self.cancel_send else "Sending cancelled.")
        threading.Thread(target=run, daemon=True).start()

    # ---- sequencer ---------------------------------------------------------------------------------
    def toggle_play(self, _b=1):
        self.playing = not self.playing
        if not self.playing:
            self.cur_step = -1

    def _seq_loop(self):
        step, nxt = 0, 0.0
        was = False
        while True:
            if not self.playing:
                was = False
                time.sleep(0.01)
                continue
            now = time.perf_counter()
            if not was:
                step, nxt, was = 0, now + 0.05, True
            if now < nxt:
                time.sleep(0.0005 if nxt - now < 0.004 else 0.002)
                continue
            n = self.steps
            self.cur_step = step % n
            for t, (_name, note) in enumerate(TRACKS):
                lv = self.grid[t][step % n]
                if lv:
                    vel = LEVELS[lv]
                    self.pad_flash[note] = (time.time(), vel)
                    self.write_msg([0x90 | CH, note, vel], quiet=True)
            base = 60.0 / self.bpm / (3 if n == 12 else 4)
            sw = self.swing / 100.0 * 0.5 if n in (8, 16) else 0.0
            nxt += base * (1 + sw) if step % 2 == 0 else base * (1 - sw)
            step += 1

    def load_preset(self, name, quiet=False):
        n, bpm, sw, rows = PRESETS[name]
        self.steps, self.bpm, self.swing = n, bpm, sw
        self.grid = [[0] * 32 for _ in TRACKS]
        for t, (tname, _note) in enumerate(TRACKS):
            if tname in rows:
                for i, lv in enumerate(S(rows[tname])):
                    self.grid[t][i] = lv
        self.pat_name = name
        if not quiet:
            self.set_status(f"Loaded the {name} groove ({bpm} BPM).")

    def clear_pattern(self, _b=1):
        self.grid = [[0] * 32 for _ in TRACKS]
        self.pat_name = ""

    def set_steps(self, n):
        self.steps = n

    def adj(self, attr, d, lo, hi):
        setattr(self, attr, clamp(getattr(self, attr) + d, lo, hi))
        self.save_settings()

    def ask_save_pattern(self, _b=1):
        def done(name):
            name = re.sub(r"[^\w .+#&'-]", "", name).strip()[:32]
            if not name:
                return
            self.patterns[name] = {"steps": self.steps, "bpm": self.bpm, "swing": self.swing, "kit": self.kit,
                                   "grid": ["".join(str(self.grid[t][i]) for i in range(self.steps)) for t in range(len(TRACKS))]}
            self.pat_name = name
            try:
                PATTERNS_FILE.write_text(json.dumps(self.patterns, indent=1))
            except Exception as ex:
                self.set_status(f"Could not save: {ex}")
                return
            self.set_status(f"Saved pattern '{name}'.")
        self.popup = TextPrompt("Save pattern", "Name for this pattern:", self.pat_name, done)

    def pick_pattern(self, _b=1):
        if not self.patterns:
            self.set_status("No saved patterns yet. Make one, then press SAVE PATTERN.")
            return

        def pick(name):
            p = self.patterns[name]
            self.steps, self.bpm, self.swing = int(p["steps"]), int(p["bpm"]), int(p["swing"])
            self.grid = [[0] * 32 for _ in TRACKS]
            for t, row in enumerate(p["grid"][:len(TRACKS)]):
                for i, ch in enumerate(row[:32]):
                    self.grid[t][i] = int(ch) if ch.isdigit() else 0
            self.pat_name = name
            self.set_status(f"Loaded '{name}'.")
        self.popup = Popup("Saved patterns", [(n, n) for n in sorted(self.patterns, key=str.lower)], self.pat_name, pick)

    # ---- immediate-mode widgets ------------------------------------------------------------------
    def add_hit(self, rect, kind, a=None, b=None):
        self._hits.append((pygame.Rect(rect), kind, a, b))

    def btn(self, rect, label, cb, active=False, enabled=True, size=14):
        r = pygame.Rect(rect)
        hover = enabled and r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, (30, 23, 23), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(self.screen, (163, 18, 25) if active else (BTN_HOT if hover else BTN), r, border_radius=6)
        pygame.draw.rect(self.screen, AMBER if (active or hover) else BTN_EDGE, r, 1, border_radius=6)
        col = (255, 255, 255) if (active or hover) else (TEXT if enabled else (112, 100, 100))
        txt(self.screen, label, fit_size(label, r.w - 10, (size, 13, 12, 11, 10), bold=True), col, r.center, "center", bold=True)
        if enabled:
            self.add_hit(r, "btn", cb)

    def flash_of(self, note):
        f = self.pad_flash.get(note)
        if not f:
            return 0.0
        age = time.time() - f[0]
        return max(0.0, 1.0 - age / 0.28) * (0.35 + 0.65 * f[1] / 127.0)

    def glow(self, surf, pos, r, amt, ring=None):
        if amt <= 0.01:
            return
        g = pygame.Surface((2 * r + 4, 2 * r + 4), pygame.SRCALPHA)
        if ring:
            pygame.draw.circle(g, (*AMBER, int(230 * amt)), (r + 2, r + 2), r, ring)
        else:
            pygame.draw.circle(g, (*AMBER, int(230 * amt)), (r + 2, r + 2), r)
        surf.blit(g, (pos[0] - r - 2, pos[1] - r - 2))

    def draw_pad(self, s, spec):
        name, cx, cy, r, n_in, n_out, key = spec
        kick = name == "KICK"
        pygame.draw.circle(s, (9, 7, 7), (cx, cy), r + 4)
        pygame.draw.circle(s, (62, 52, 52) if n_out else (84, 74, 74), (cx, cy), r)
        pygame.draw.circle(s, BTN_EDGE, (cx, cy), r, 2)
        ri = int(r * 0.66) if n_out else r - 6
        pygame.draw.circle(s, (104, 92, 92), (cx, cy), ri)
        pygame.draw.circle(s, (142, 130, 130), (cx, cy), ri, 1)
        if n_out:
            pygame.draw.circle(s, (142, 130, 130), (cx, cy), r - 2, 1)
        self.glow(s, (cx, cy), ri, self.flash_of(n_in))
        if n_out:
            self.glow(s, (cx, cy), r - 2, self.flash_of(n_out), ring=int(r * 0.30))
        hover = math.hypot(pygame.mouse.get_pos()[0] - cx, pygame.mouse.get_pos()[1] - cy) <= r
        if hover:
            pygame.draw.circle(s, AMBER, (cx, cy), r + 2, 2)
        txt(s, name, 14 if not kick else 20, (255, 255, 255), (cx, cy - (6 if key else 0)), "center", bold=True)
        if key:
            txt(s, f"[{key}]", 11, (213, 201, 201), (cx, cy + (14 if not kick else 22)), "center")
        self.add_hit((cx - r, cy - r, 2 * r, 2 * r), "pad", spec)

    def slider(self, rect, value, hi, kind, vertical=False):
        s = self.screen
        r = pygame.Rect(rect)
        pygame.draw.rect(s, (11, 9, 9), r, border_radius=6)
        pygame.draw.rect(s, BTN_EDGE, r, 1, border_radius=6)
        t = value / float(hi)
        if vertical:
            f = pygame.Rect(r.x + 3, r.bottom - 3 - int((r.h - 6) * t), r.w - 6, int((r.h - 6) * t))
        else:
            f = pygame.Rect(r.x + 3, r.y + 3, int((r.w - 6) * t), r.h - 6)
        pygame.draw.rect(s, AMBER_DIM, f, border_radius=4)
        if vertical:
            pygame.draw.rect(s, (255, 255, 255), (r.x + 1, max(r.y + 1, f.y - 3), r.w - 2, 6), border_radius=3)
        else:
            pygame.draw.rect(s, (255, 255, 255), (min(r.right - 7, f.right - 3), r.y + 1, 6, r.h - 2), border_radius=3)
        self.add_hit(r.inflate(0, 8) if not vertical else r.inflate(8, 0), "slider", kind, (r, hi, vertical))

    def draw_play(self, s):
        panel(s, (30, 130, 330, 660), "Kits")
        for i, name in enumerate(KITS):
            c, rw = divmod(i, 16)
            self.btn((42 + c * 158, 170 + rw * 37, 150, 33), f"{i + 1:>2} {name}", lambda b, i=i: self.set_kit(i), active=(i == self.kit), size=13)
        panel(s, (380, 130, 720, 660), "Pads")
        for spec in PADS:
            self.draw_pad(s, spec)
        # pedal hi-hat (foot) button
        r = pygame.Rect(390, 420, 120, 36)
        fl = self.flash_of(PEDAL_NOTE)
        pygame.draw.rect(s, (30, 23, 23), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(s, (104, 92, 92), r, border_radius=6)
        if fl > 0.02:
            pygame.draw.rect(s, AMBER, r, 0, border_radius=6)
        pygame.draw.rect(s, AMBER if r.collidepoint(pygame.mouse.get_pos()) else BTN_EDGE, r, 2, border_radius=6)
        txt(s, "PEDAL HAT [D]", 12, (255, 255, 255), r.center, "center", bold=True)
        self.add_hit(r, "note", PEDAL_NOTE)
        txt(s, "Cymbals: middle = bow, outer ring = edge.   Snare: outer ring = rim.", 12, DIM, (400, 764))
        panel(s, (1120, 130, 250, 660), "Playing")
        txt(s, "VELOCITY", 12, DIM, (1140, 168), bold=True)
        txt(s, str(self.vel), 14, AMBER, (1350, 168), "topright", bold=True)
        self.slider((1140, 192, 210, 24), self.vel, 127, "vel")
        txt(s, "SHIFT = hard, CTRL = soft", 10, DIM, (1140, 222))
        txt(s, "HI-HAT PEDAL  (CC4)", 12, DIM, (1140, 258), bold=True)
        self.slider((1140, 284, 56, 210), self.hat, 127, "hat", vertical=True)
        txt(s, str(self.hat), 22, AMBER, (1275, 380), "center", bold=True)
        self.btn((1214, 284, 136, 30), "ZERO", lambda b: self.set_hat(0), size=13)
        self.btn((1214, 460, 136, 30), "FULL", lambda b: self.set_hat(127), size=13)
        txt(s, "Sends CC4, like the", 10, DIM, (1214, 326))
        txt(s, "hi-hat foot pedal.", 10, DIM, (1214, 340))
        for i, (lab, note) in enumerate(CHOKES):
            self.btn((1140, 520 + i * 38, 210, 32), lab, lambda b, n=note: self.choke(n), size=13)
        self.btn((1140, 644, 210, 32), "ALL SOUND OFF", self.sound_off, size=13)
        self.btn((1140, 684, 210, 32), "RESET CONTROLLERS", self.reset_ctrl, size=13)
        txt(s, f"Kit {self.kit + 1}: {KITS[self.kit]}", 14, AMBER, (1245, 738), "center", bold=True)
        txt(s, "MIDI channel 10 (fixed)", 11, DIM, (1245, 760), "center")

    def draw_beat(self, s):
        panel(s, (30, 130, 1340, 450), "Pattern   (click a square to cycle: normal, accent, soft, off.   Right-click clears.)")
        x0, y0, cw, ch = 176, 168, 70, 32
        for t, (name, note) in enumerate(TRACKS):
            y = y0 + t * 36
            fl = self.flash_of(note)
            txt(s, name, 13, AMBER if fl > 0.1 else TEXT, (46, y + ch // 2), "midleft", bold=True)
            for i in range(self.steps):
                r = pygame.Rect(x0 + i * (cw + 4), y, cw, ch)
                lv = self.grid[t][i]
                base = (62, 50, 50) if (i // 4) % 2 == 0 else (50, 40, 40)
                pygame.draw.rect(s, base, r, border_radius=5)
                if lv:
                    col = [None, AMBER_DIM, AMBER, (255, 255, 255)][lv]
                    pygame.draw.rect(s, col, r.inflate(-6, -6) if lv == 1 else r.inflate(-2, -2), border_radius=5)
                pygame.draw.rect(s, BTN_EDGE if i != self.cur_step else (255, 255, 255), r, 1 if i != self.cur_step else 3, border_radius=5)
                self.add_hit(r, "cell", t, i)
        for i in range(self.steps):
            txt(s, str(i + 1), 11, AMBER if i % 4 == 0 else DIM, (x0 + i * (cw + 4) + cw // 2, y0 + len(TRACKS) * 36 + 2), "center")
        panel(s, (30, 590, 1340, 200), "Controls")
        txt(s, "TEMPO", 12, DIM, (46, 632), bold=True)
        for lab, d, x, w in (("-10", -10, 106, 50), ("-1", -1, 162, 40)):
            self.btn((x, 624, w, 34), lab, lambda b, d=d: self.adj("bpm", d, 40, 260), size=13)
        txt(s, f"{self.bpm}", 22, LCD_FG, (264, 641), "center", bold=True)
        pygame.draw.rect(s, BTN_EDGE, (216, 624, 96, 34), 1, border_radius=5)
        for lab, d, x, w in (("+1", 1, 322, 40), ("+10", 10, 368, 50)):
            self.btn((x, 624, w, 34), lab, lambda b, d=d: self.adj("bpm", d, 40, 260), size=13)
        txt(s, "SWING", 12, DIM, (450, 632), bold=True)
        self.btn((512, 624, 40, 34), "-", lambda b: self.adj("swing", -5, 0, 50), size=15)
        txt(s, f"{self.swing}%", 16, LCD_FG, (586, 641), "center", bold=True)
        self.btn((620, 624, 40, 34), "+", lambda b: self.adj("swing", 5, 0, 50), size=15)
        txt(s, "STEPS", 12, DIM, (700, 632), bold=True)
        for i, n in enumerate([8, 12, 16]):
            self.btn((760 + i * 56, 624, 50, 34), str(n), lambda b, n=n: self.set_steps(n), active=(self.steps == n), size=14)
        self.btn((1130, 620, 210, 76), "STOP" if self.playing else "PLAY", self.toggle_play, active=self.playing, size=26)
        txt(s, "GROOVES", 12, DIM, (46, 690), bold=True)
        for i, name in enumerate(PRESETS):
            self.btn((126 + i * 118, 680, 110, 34), name, lambda b, name=name: self.load_preset(name), active=(self.pat_name == name), size=13)
        self.btn((46, 736, 180, 36), "SAVE PATTERN...", self.ask_save_pattern, size=14)
        self.btn((236, 736, 180, 36), "LOAD PATTERN...", self.pick_pattern, size=14)
        self.btn((426, 736, 110, 36), "CLEAR", self.clear_pattern, size=14)
        txt(s, "KIT", 12, DIM, (640, 746), bold=True)
        self.btn((680, 736, 40, 36), "<", lambda b: self.set_kit(self.kit - 1), size=16)
        pygame.draw.rect(s, (11, 9, 9), (726, 736, 300, 36), border_radius=5)
        pygame.draw.rect(s, BTN_EDGE, (726, 736, 300, 36), 1, border_radius=5)
        txt(s, f"{self.kit + 1}  {KITS[self.kit]}", 15, LCD_FG, (876, 754), "center", bold=True)
        self.btn((1032, 736, 40, 36), ">", lambda b: self.set_kit(self.kit + 1), size=16)
        if self.pat_name:
            txt(s, f"Pattern: {self.pat_name}", 12, DIM, (1130, 706))

    def draw_backup(self, s):
        panel(s, (30, 130, 660, 660), "1   Save the TD-3's data to this computer")
        lines = ["On the TD-3:",
                 "  - switch it OFF, then hold DRUM KIT and switch it ON",
                 "  - press + or - to choose what to save (all, kits, setup)",
                 "Then here: press START LISTENING.",
                 "Then on the TD-3: hold DRUM KIT until it begins sending.",
                 "When the number below stops rising, press SAVE."]
        yy = 172
        for ln in lines:
            for part in wrap(ln, 14, 620):
                txt(s, part, 14, TEXT, (54, yy))
                yy += 23
        self.btn((54, 360, 290, 44), "START LISTENING", self.start_listening, active=self.listening, enabled=bool(self.inp), size=16)
        self.btn((354, 360, 160, 44), "STOP", self.stop_listening, enabled=self.listening, size=16)
        self.btn((524, 360, 146, 44), "SAVE", self.save_received, enabled=bool(self.received), size=16)
        if not self.inp:
            txt(s, "Choose MIDI IN at the top first (the TD-3's MIDI OUT).", 13, ORANGE, (54, 416))
        now = time.time()
        live = self.listening and now - self.recv_t < 1.0
        led(s, (66, 450), live, AMBER)
        txt(s, "RECEIVING" if live else ("LISTENING" if self.listening else "not listening"), 14, AMBER if self.listening else DIM, (86, 442), bold=True)
        nbytes = sum(len(m) for m in self.received)
        txt(s, f"{len(self.received)}", 56, LCD_FG, (54, 480), bold=True)
        txt(s, "messages", 14, DIM, (54, 546))
        txt(s, f"{nbytes:,} bytes", 20, TEXT, (300, 504))
        txt(s, f"Backups are saved in the folder  td3_backups  next to this app.", 12, DIM, (54, 740))
        panel(s, (710, 130, 660, 660), "2   Send a backup back to the TD-3")
        txt(s, "On the TD-3 press DRUM KIT first so it shows the kit number.", 13, TEXT, (734, 172))
        if not self.files:
            txt(s, "No backups yet.", 14, DIM, (734, 214))
        for r, f in enumerate(self.files[:14]):
            rect = pygame.Rect(734, 204 + r * 32, 612, 29)
            sel = f == self.sel_file
            pygame.draw.rect(s, AMBER_DIM if sel else (BTN_HOT if rect.collidepoint(pygame.mouse.get_pos()) else BTN), rect, border_radius=5)
            pygame.draw.rect(s, AMBER if sel else BTN_EDGE, rect, 1, border_radius=5)
            try:
                size = f.stat().st_size
            except Exception:
                size = 0
            txt(s, f.name, 14, (255, 255, 255) if sel else TEXT, (rect.x + 12, rect.centery), "midleft", bold=True)
            txt(s, f"{size:,} bytes", 12, DIM, (rect.right - 12, rect.centery), "midright")
            self.add_hit(rect, "file", f)
        self.btn((734, 668, 250, 44), "SEND TO TD-3", self.ask_send_file, enabled=bool(self.sel_file) and not self.sending and bool(self.out), size=16)
        self.btn((994, 668, 170, 44), "CANCEL", lambda b: setattr(self, "cancel_send", True), enabled=self.sending, size=16)
        self.btn((1174, 668, 172, 44), "REFRESH LIST", lambda b: self.scan_files(), size=14)
        bar = pygame.Rect(734, 728, 612, 22)
        pygame.draw.rect(s, (11, 9, 9), bar, border_radius=5)
        if self.send_total:
            f = int((bar.w - 4) * self.send_pos / self.send_total)
            pygame.draw.rect(s, AMBER, (bar.x + 2, bar.y + 2, f, bar.h - 4), border_radius=4)
            txt(s, f"{self.send_pos} / {self.send_total} messages", 12, TEXT, bar.center, "center", bold=True)
        pygame.draw.rect(s, BTN_EDGE, bar, 1, border_radius=5)

    def draw_monitor(self, s):
        panel(s, (30, 130, 1340, 660), "MIDI monitor  (newest at the bottom)")
        self.btn((1230, 126, 120, 26), "CLEAR", lambda b: self.events.clear(), size=12)
        evs = list(self.events)[-27:]
        if not evs:
            txt(s, "Nothing yet. Click a pad, or choose MIDI OUT at the top.", 14, DIM, (54, 170))
        for i, (t, d, text) in enumerate(evs):
            col = AMBER if d == "OUT" else GREEN
            txt(s, time.strftime("%H:%M:%S", time.localtime(t)), 13, DIM, (54, 164 + i * 21))
            txt(s, d, 13, col, (150, 164 + i * 21), bold=True)
            txt(s, text, 13, TEXT, (200, 164 + i * 21))

    def draw_help(self, s):
        panel(s, (30, 130, 1340, 660), "Help")
        s.set_clip(pygame.Rect(40, 160, 1320, 620))
        y = 170 - self.help_scroll
        for kind, text in self.help_lines:
            if kind == "H":
                y += 8
                txt(s, text.upper(), 15, AMBER, (56, y), bold=True)
                y += 30
            else:
                txt(s, text, 15, TEXT, (56, y))
                y += 24
        s.set_clip(None)

    def draw_header(self, s):
        txt(s, "TD-3", 40, AMBER, (30, 4), bold=True)
        txt(s, "V - D R U M S   C O N T R O L", 11, AMBER_DIM, (32, 52))
        out = (self.settings.get("out_name") or "none  (click to choose)").replace("#0", "")
        inn = (self.settings.get("in_name") or "none  (optional)").replace("#0", "")
        self.btn((310, 8, 560, 30), f"MIDI OUT:  {out}"[:68], lambda b: self.choose_device("out"), size=13)
        self.btn((310, 42, 560, 30), f"MIDI IN:   {inn}"[:68], lambda b: self.choose_device("in"), size=13)
        self.btn((890, 8, 170, 64), "ALL SOUND OFF", self.sound_off, size=15)
        now = time.time()
        for i, (lab, t) in enumerate((("OUT", self.flash_out), ("IN", self.flash_in))):
            on = now - t < 0.15
            led(s, (1100 + i * 70, 28), on, AMBER)
            txt(s, lab, 12, TEXT if on else DIM, (1114 + i * 70, 21), bold=True)
        txt(s, f"{self.counts['out']} sent   {self.counts['in']} received", 11, DIM, (1092, 48))
        txt(s, f"KIT {self.kit + 1}", 22, LCD_FG, (1290, 20), bold=True)
        txt(s, KITS[self.kit], 12, DIM, (1290, 48))
        for i, name in enumerate(TABS):
            self.btn((30 + i * 150, 88, 140, 32), name, lambda b, i=i: setattr(self, "tab", i), active=(i == self.tab), size=14)

    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        self._hits = []
        self.draw_header(s)
        [self.draw_play, self.draw_beat, self.draw_backup, self.draw_monitor, self.draw_help][self.tab](s)
        panel(s, (30, 800, 1340, 50), "")
        age = time.time() - self.status_t
        for i, ln in enumerate(wrap(self.status, 13, 1300)[:2]):
            txt(s, ln, 13, TEXT if age < 40 else DIM, (46, 808 + i * 17))
        self.hits = self._hits
        if self.popup:
            self.popup.draw(s, self)

    # ---- events ------------------------------------------------------------------------------------------
    def vel_mod(self):
        m = pygame.key.get_mods()
        return 127 if m & pygame.KMOD_SHIFT else (45 if m & pygame.KMOD_CTRL else self.vel)

    def slider_set(self, kind, rect, hi, vertical, pos):
        t = (rect.bottom - pos[1]) / float(rect.h) if vertical else (pos[0] - rect.x) / float(rect.w)
        v = int(round(clamp(t, 0.0, 1.0) * hi))
        if kind == "vel":
            self.vel = clamp(v, 1, 127)
            self.save_settings()
        else:
            self.set_hat(v)

    def handle(self, e):
        if e.type == pygame.QUIT:
            self.running = False
            return
        if self.popup:
            self.popup.handle(e, self)
            return
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_ESCAPE:
                self.running = False
            elif self.tab in (0, 1) and e.key in KEYMAP:
                self.hit(KEYMAP[e.key], self.vel_mod())
            return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            for r, kind, a, b in reversed(self.hits):
                if not r.collidepoint(e.pos):
                    continue
                if e.button == 3:
                    if kind == "cell":
                        self.grid[a][b] = 0
                        return
                    continue
                if kind == "btn":
                    a(1)
                elif kind == "pad":
                    name, cx, cy, rad, n_in, n_out, key = a
                    d = math.hypot(e.pos[0] - cx, e.pos[1] - cy)
                    self.hit(n_out if (n_out and d > rad * 0.66) else n_in, self.vel_mod())
                elif kind == "note":
                    self.hit(a, self.vel_mod())
                elif kind == "cell":
                    self.grid[a][b] = LEVEL_NEXT[self.grid[a][b]]
                    self.pat_name = self.pat_name if self.pat_name in self.patterns else ""
                elif kind == "slider":
                    rect, hi, vert = b
                    self.drag = (a, rect, hi, vert)
                    self.slider_set(a, rect, hi, vert, e.pos)
                elif kind == "file":
                    self.sel_file = a
                return
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = None
        elif e.type == pygame.MOUSEMOTION and self.drag:
            self.slider_set(*self.drag, e.pos)
        elif e.type == pygame.MOUSEWHEEL and self.tab == 4:
            self.help_scroll = max(0, min(max(0, len(self.help_lines) * 24 + 60 - 610), self.help_scroll - e.y * 40))

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
        self.playing = False
        self.close_all()
        try:
            if pgmidi:
                pgmidi.quit()
        except Exception:
            pass
        pygame.quit()


def main():
    shot, tab = None, 0
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]
    if "--tab" in sys.argv:
        tab = int(sys.argv[sys.argv.index("--tab") + 1])
    App(headless=bool(shot), tab=tab).run(shot)


if __name__ == "__main__":
    main()
