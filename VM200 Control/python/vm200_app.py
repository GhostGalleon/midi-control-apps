#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
VM200 CONTROL  -  MIDI control surface for the Fostex VM200 digital mixer (1999).

Run:      python vm200_app.py
Needs:    pip install pygame

It talks to the mixer with the "Fostex Exclusive" system-exclusive messages documented in the
VM200 User's Guide (MIDI In/Out Data Format): faders, ON/SOLO, pan, phase, aux/effect sends,
4-band EQ, effects library, scene recall - and reads the mixer state back with parameter
requests.  It also does MMC transport, a MIDI bulk-dump backup of the whole mixer, and shows
every message in plain English on the MONITOR tab.

Mixer set-up (MIDI key, page 1:MIDI TX&RX): SYSTEM EX TX&RX = ON, DevID = 0, BULK RX = ON,
PROGRAM CHANGE RX/TX = ON, table 02 (preset).  See the SYSTEM and HELP tabs.
"""
import datetime
import json
import math
import cmath
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
SETTINGS_FILE = BASE / "vm200_settings.json"
SCENES_FILE = BASE / "vm200_scenes.json"
BACKUP_DIR = BASE / "vm200_backups"
LOG_FILE = BASE / "vm200_log.txt"

W, H = 1400, 860
GAP = 0.012            # seconds between any two messages (the VM200 is a 1999 machine: be kind to it)

# ---- palette: graphite + mint (re-coloured at the end of the build) ----------------------
AMBER = (120, 240, 214)        # accent (name kept so the shared drawing code works)
AMBER_DIM = (70, 150, 136)
ORANGE = (255, 140, 30)
SOLO_RED = (235, 40, 40)
TEXT = (236, 244, 242)
DIM = (150, 172, 170)
PANEL = (36, 46, 52)
EDGE = (120, 150, 152)
LCD_BG = (4, 16, 16)
LCD_FG = (120, 255, 226)
LCD_GLOW = (20, 84, 74)
GREEN = (90, 230, 130)
RED = (255, 90, 80)
BTN = (46, 64, 70)
BTN_HOT = (70, 104, 110)
BTN_EDGE = (116, 150, 152)
WELL = (14, 22, 26)

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

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


def metal(w, h, top=(70, 92, 94), bot=(20, 28, 32)):
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


def rounded_metal(surf, rect, radius=10, top=(70, 92, 94), bot=(20, 28, 32)):
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
    pygame.draw.rect(surf, (27, 40, 37), r, 1, border_radius=10)
    pygame.draw.rect(surf, EDGE, r.inflate(-2, -2), 1, border_radius=9)
    pygame.draw.line(surf, (231, 255, 250), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (233, 255, 251), (r.x + 16, r.y + 8), bold=True)
    if title:
        pygame.draw.line(surf, (52, 76, 71), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
        pygame.draw.line(surf, (164, 204, 196), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (20, 30, 28), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (204, 230, 225), (sx, r.bottom - 12), 4, 1)
        pygame.draw.line(surf, (148, 176, 171), (sx - 2, r.bottom - 14), (sx + 2, r.bottom - 10))


def lcd(surf, rect, text, max_size=24):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, (20, 30, 28), r.inflate(6, 6), border_radius=8)
    pygame.draw.rect(surf, (187, 214, 209), r.inflate(6, 6), 1, border_radius=8)
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
        pygame.draw.circle(surf, (47, 64, 60), pos, 4)
        pygame.draw.circle(surf, (156, 190, 183), pos, 4, 1)


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
        pygame.draw.rect(surf, (21, 32, 30), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
        pygame.draw.line(surf, (224, 250, 245), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
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
            txt(surf, "name this scene...", 14, (130, 156, 151), (r.x + 8, r.centery), "midleft")
        else:
            txt(surf, shown[-24:], 15, TEXT, (r.x + 8, r.centery), "midleft")




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
                pygame.draw.rect(surf, (119, 170, 160), r, border_radius=5)
            if val == self.current:
                pygame.draw.rect(surf, AMBER_DIM, r, 1, border_radius=5)
            txt(surf, lab, 16, AMBER if val == self.current else TEXT, (r.x + 10, r.centery), "midleft")






class MultiPopup(Popup):
    """Tick-list: each click toggles one item and the list stays open. ESC / click outside closes."""

    def __init__(self, title, options, chosen, on_toggle):
        super().__init__(title, options, None, None)
        self.chosen, self.on_toggle = set(chosen), on_toggle
        old = self.panel
        self.panel = pygame.Rect(0, 0, max(old.w, 740), old.h + 34)
        self.panel.center = (W // 2, H // 2 - 20)
        dx, dy = self.panel.x - old.x, self.panel.y - old.y
        self.items = [(pygame.Rect(r.x + dx, r.y + dy, r.w + self.panel.w - old.w, r.h), v, l) for r, v, l in self.items]

    def handle(self, e, app):
        if e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_RETURN):
            app.popup = None
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for r, val, _ in self.items:
                if r.collidepoint(e.pos):
                    if val in self.chosen:
                        self.chosen.discard(val)
                    else:
                        self.chosen.add(val)
                    self.on_toggle(val, val in self.chosen)
                    return True
            if not self.panel.collidepoint(e.pos):
                app.popup = None
        return True

    def draw(self, surf, app):
        self.current = None
        super().draw(surf, app)
        for r, val, _ in self.items:
            box = pygame.Rect(r.right - 30, r.centery - 8, 16, 16)
            pygame.draw.rect(surf, (20, 30, 28), box, border_radius=3)
            pygame.draw.rect(surf, AMBER if val in self.chosen else AMBER_DIM, box, 1, border_radius=3)
            if val in self.chosen:
                pygame.draw.line(surf, AMBER, (box.x + 3, box.centery), (box.x + 6, box.bottom - 4), 3)
                pygame.draw.line(surf, AMBER, (box.x + 6, box.bottom - 4), (box.right - 3, box.y + 4), 3)
        txt(surf, "click to tick / untick   -   Esc or click outside to close", 12, DIM,
            (self.panel.x + 18, self.panel.bottom - 22))


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






# =============================================================================
# VM200 PROTOCOL  (Fostex Exclusive:  F0 51 <DevId> <cmd> ... F7)
# =============================================================================
N_CH = 20
SIMPLE = {"on": 0x10, "vol": 0x11, "solo": 0x19}
SIMPLE_REV = {v: k for k, v in SIMPLE.items()}
CH_PARAM = {"on": 0x10, "vol": 0x11, "pan": 0x12, "solo": 0x19, "phase": 0x21, "spdif": 0x26, "rst": 0x30,
            "rrec": 0x31, "radat": 0x33, "pair": 0x70, "fgrp": 0x71, "mgrp": 0x72, "ssafe": 0x73, "rsafe": 0x74}
CH_REV = {v: k for k, v in CH_PARAM.items()}
IDX_PARAM = {"aux": 0x41, "auxpp": 0x43, "eff": 0x49, "effpp": 0x4B}
IDX_REV = {v: k for k, v in IDX_PARAM.items()}
EQ_PARAM = {"on": 0x50, "type": 0x51, "gain": 0x52, "freq": 0x53}
EQ_REV = {v: k for k, v in EQ_PARAM.items()}
BANDS = ["LO", "LO-MID", "HI-MID", "HI"]
FREQ_DEFAULT = {1: 55, 2: 111, 3: 159, 4: 207}


def clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


def addr(key):
    """Key -> (command family byte, address bytes)."""
    k = key[0]
    if k == "st":
        return 0x19, [0x01, SIMPLE[key[1]]]
    if k == "rec":
        return 0x19, [0x02, SIMPLE[key[1]]]
    if k == "aux":
        return 0x19, [0x05, key[1], SIMPLE[key[2]]]
    if k == "eff":
        return 0x19, [0x06, key[1], SIMPLE[key[2]]]
    if k == "ch":
        if key[2] in CH_PARAM:
            return 0x19, [0x10, key[1], CH_PARAM[key[2]]]
        return 0x19, [0x10, key[1], IDX_PARAM[key[2]], key[3]]
    if k == "eq":
        return 0x19, [0x10, key[1], EQ_PARAM[key[3]], key[2]]
    if k == "efft":
        return 0x1A, [key[1], 0x10]
    if k == "effp":
        return 0x1A, [key[1], 0x11, key[2]]
    raise KeyError(key)


def two_bytes(key):
    return (key[0] == "eq" and key[3] == "freq") or key[0] == "effp"


def data_of(key, val):
    return [val & 0x7F, (val >> 7) & 0x7F] if two_bytes(key) else [val & 0x7F]


def range_of(key):
    k = key[0]
    if k in ("st", "rec", "aux", "eff"):
        f = key[-1]
        return (0, 127) if f == "vol" else (0, 1)
    if k == "ch":
        f = key[2]
        if f in ("vol", "aux", "eff"):
            return 0, 127
        if f == "pan":
            return 0, 40
        if f in ("fgrp", "mgrp"):
            return 0, 3
        return 0, 1
    if k == "eq":
        return {"on": (0, 1), "type": (0, 46), "gain": (0, 72), "freq": (0, 239)}[key[3]]
    return 0, 16383


def default_of(key):
    k = key[0]
    if k == "ch":
        return {"vol": 0, "pan": 20}.get(key[2], 0)
    if k in ("st", "rec", "aux", "eff") and key[-1] == "vol":
        return 0
    if k == "eq":
        return {"on": 0, "type": 30, "gain": 36, "freq": FREQ_DEFAULT.get(key[2], 100)}[key[3]]
    return 0


def sx_cmd(dev, key, val):
    fam, b = addr(key)
    return [0xF0, 0x51, dev, fam, *b, *data_of(key, val), 0xF7]


def sx_req(dev, key):
    fam, b = addr(key)
    return [0xF0, 0x51, dev, fam + 0x10, *b, 0xF7]


def clean(b):
    return "".join(chr(c) for c in b if 32 <= c < 127).strip()


def parse_vm(m):
    """Parse a Fostex Exclusive command/status message -> (key, value) or None."""
    if len(m) < 7 or m[0] != 0xF0 or m[1] != 0x51:
        return None
    fam, body = m[3], m[4:-1] if m[-1] == 0xF7 else m[4:]
    hi, lo = fam & 0xF0, fam & 0x0F
    if hi not in (0x10, 0x30):
        return None
    try:
        if lo == 9:
            if body[0] == 1:
                f = SIMPLE_REV.get(body[1])
                return (("st", f), body[2]) if f else None
            if body[0] == 2:
                f = SIMPLE_REV.get(body[1])
                return (("rec", f), body[2]) if f in ("vol", "solo") else None
            if body[0] in (5, 6):
                f = SIMPLE_REV.get(body[2])
                if f and 1 <= body[1] <= 4:
                    return (("aux" if body[0] == 5 else "eff", body[1], f), body[3])
                return None
            if body[0] == 0x10 and 1 <= body[1] <= N_CH:
                n, p = body[1], body[2]
                if p in CH_REV:
                    return ("ch", n, CH_REV[p]), body[3]
                if p in IDX_REV:
                    return ("ch", n, IDX_REV[p], body[3]), body[4]
                if p in EQ_REV and 1 <= body[3] <= 4:
                    val = body[4] | (body[5] << 7) if p == 0x53 else body[4]
                    return ("eq", n, body[3], EQ_REV[p]), val
        elif lo == 0xA and body[0] in (1, 2):
            e, p = body[0], body[1]
            if p == 0x10:
                return ("efft", e), body[2]
            if p == 0x11:
                return ("effp", e, body[2]), body[3] | (body[4] << 7)
            if p == 0x70:
                return ("effname", e), clean(bytes(body[2:]))
        elif lo == 8 and body[0] == 0x20:
            return ("scname",), clean(bytes(body[1:]))
    except IndexError:
        return None
    return None


def key_text(key, val):
    k = key[0]
    if k == "st":
        return f"ST buss {key[1]} = {val}"
    if k == "rec":
        return f"REC buss {key[1]} = {val}"
    if k in ("aux", "eff"):
        return f"{k.upper()}{key[1]} master {key[2]} = {val}"
    if k == "ch":
        extra = f" {key[3]}" if len(key) > 3 else ""
        return f"CH{key[1]} {key[2]}{extra} = {val}"
    if k == "eq":
        return f"CH{key[1]} EQ {BANDS[key[2] - 1]} {key[3]} = {val}"
    if k == "efft":
        return f"EFF{key[1]} type = {val}"
    if k == "effp":
        return f"EFF{key[1]} param {key[2]} = {val}"
    if k == "effname":
        return f"EFF{key[1]} name = {val}"
    if k == "scname":
        return f"scene name = {val}"
    return f"{key} = {val}"


def freq_hz(step):
    return 20.3 * 2 ** (step / 24.0)


def fmt_hz(step):
    f = freq_hz(step)
    return f"{f:.0f} Hz" if f < 1000 else f"{f / 1000:.2f} kHz"


def q_of(typ):
    return 0.1 * (200.0 ** ((clamp(typ, 2, 46) - 2) / 44.0))


def eq_response(app, ch, npts=180):
    """dB at log-spaced frequencies for the four bands of one channel (RBJ biquads, 44.1 kHz)."""
    fs = 44100.0
    freqs = [20.0 * (1000.0 ** (i / (npts - 1))) for i in range(npts)]
    total = [0.0] * npts
    for band in range(1, 5):
        if not app.get(("eq", ch, band, "on")):
            continue
        typ = app.get(("eq", ch, band, "type"))
        gdb = (app.get(("eq", ch, band, "gain")) - 36) / 2.0
        f0 = min(freq_hz(app.get(("eq", ch, band, "freq"))), fs * 0.45)
        w0 = 2 * math.pi * f0 / fs
        cw, sw = math.cos(w0), math.sin(w0)
        A = 10 ** (gdb / 40.0)
        Q = q_of(typ)
        if typ >= 2:
            al = sw / (2 * Q)
            b = (1 + al * A, -2 * cw, 1 - al * A)
            a = (1 + al / A, -2 * cw, 1 - al / A)
        elif typ == 1 and band in (1, 4):
            al = sw / 2 * math.sqrt(2.0)
            s2 = 2 * math.sqrt(A) * al
            if band == 1:
                b = (A * ((A + 1) - (A - 1) * cw + s2), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - s2))
                a = ((A + 1) + (A - 1) * cw + s2, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - s2)
            else:
                b = (A * ((A + 1) + (A - 1) * cw + s2), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - s2))
                a = ((A + 1) - (A - 1) * cw + s2, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - s2)
        elif typ == 0 and band in (1, 4):
            al = sw / (2 * 0.707)
            if band == 1:
                b = ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2)
            else:
                b = ((1 - cw) / 2, 1 - cw, (1 - cw) / 2)
            a = (1 + al, -2 * cw, 1 - al)
        else:
            continue
        for i, f in enumerate(freqs):
            z1 = cmath.exp(-1j * 2 * math.pi * f / fs)
            z2 = z1 * z1
            h = (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)
            total[i] += 20 * math.log10(max(1e-6, abs(h)))
    return freqs, total


EFF_PRESETS = ["NomL.Hall", "LowL.Hall", "PrsL.HALL", "WetL.HALL", "STADIUM", "NomS.Hall", "LowS.HALL", "PrsS.HALL",
               "WetS.HALL", "NoER.HALL", "Nom.ROOM", "GARAGE", "Dead.ROOM", "Prs.ROOM", "Dr.BOOTH", "Nom.PLATE",
               "Old.PLATE", "Prs.PLATE", "Wet.PLATE", "DigiPLATE", "PVo.BOOTH", "NVo.BOOTH", "Vo.REVERB", "Solo.VO",
               "Arena VO", "Nom.GATE", "MnBD.GATE", "StSD.GATE", "Hall.ER", "Room.ER", "Spring.ER", "Random.ER",
               "Reverse.ER", "MonoDELAY", "Pan.DELAY", "DualDELAY", "3ch.DELAY", "CHORUS", "3D.CHORUS", "FLANGE",
               "MonoPITCH", "DualPITCH", "MnDL-NmHL", "MnDL-NmPL", "PnDL-NmHL", "PnDL-NmPL", "CHORS-REV", "REV-CHORS",
               "FLG-REV", "REV-FLG"]
MMC = [("REWIND", 5), ("STOP", 1), ("PLAY", 2), ("FORWARD", 4), ("RECORD", 6), ("PAUSE", 9)]
BUSES = [("AUX 1", "aux", 1), ("AUX 2", "aux", 2), ("AUX 3", "aux", 3), ("AUX 4", "aux", 4),
         ("EFF 1", "eff", 1), ("EFF 2", "eff", 2)]
BULK_ID = [0x56, 0x4D, 0x32, 0x30, 0x30, 0x41]   # 'V' 'M' '2' '0' '0' 'A'


def _build_cc():
    t = {}
    for n in range(1, 21):
        t[n] = (("ch", n, "vol"), "v")
        t[41 + n] = (("ch", n, "pan"), "pan")
        t[61 + n] = (("ch", n, "eff", 1), "v")
    for n in range(1, 12):
        t[20 + n] = (("ch", n, "on"), "on")
    for n in range(12, 21):
        t[21 + n] = (("ch", n, "on"), "on")
    for n in range(1, 15):
        t[81 + n] = (("ch", n, "eff", 2), "v")
    for n in range(15, 21):
        t[87 + n] = (("ch", n, "eff", 2), "v")
    t[108] = (("st", "vol"), "v")
    for a in range(1, 5):
        t[108 + a] = (("aux", a, "vol"), "v")
    t[113] = (("eff", 1, "vol"), "v")
    t[114] = (("eff", 2, "vol"), "v")
    t[115] = (("st", "on"), "on")
    return t


CC_MAP = _build_cc()


def cc_to_key(cc, val):
    """Control Change (preset table 2 / default user table) -> (key, value) or None."""
    e = CC_MAP.get(cc)
    if not e:
        return None
    key, kind = e
    if kind == "on":
        return key, 1 if val > 0 else 0
    if kind == "pan":
        return key, round(val * 40 / 127)
    return key, val


# =============================================================================
# MIXER WIDGETS  (every widget holds a parameter key - or a function that returns one)
# =============================================================================
def kof(k, app):
    return k(app) if callable(k) else k


class PToggle:
    def __init__(self, k, rect, label, color=GREEN, show_on=True):
        self.k, self.rect, self.label, self.color = k, pygame.Rect(rect), label, color

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.rect.collidepoint(e.pos):
            key = kof(self.k, app)
            app.set(key, 0 if app.get(key) else 1)
            return True
        return False

    def draw(self, surf, app):
        key = kof(self.k, app)
        on = bool(app.get(key))
        r = self.rect
        hover = r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(surf, (8, 14, 16), r.inflate(3, 3), border_radius=6)
        pygame.draw.rect(surf, tuple(int(c * (0.85 if self.color in (ORANGE, SOLO_RED) else 0.45)) for c in self.color) if on else (BTN_HOT if hover else BTN), r, border_radius=5)
        pygame.draw.rect(surf, self.color if on else BTN_EDGE, r, 1, border_radius=5)
        txt(surf, self.label, 12, (255, 255, 255) if on else DIM, r.center, "center", bold=True)


class PFader:
    def __init__(self, k, cx, y, h, lo=0, hi=127, default=100, color=AMBER):
        self.k, self.cx, self.y, self.h, self.default, self.color = k, cx, y, h, default, color
        self.rect = pygame.Rect(cx - 20, y - 6, 40, h + 12)
        self.last_click = 0.0

    def set_y(self, my, app):
        t = 1.0 - (my - self.y - 24) / max(1, self.h - 48)
        app.set(kof(self.k, app), int(round(max(0.0, min(1.0, t)) * 127)))

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.rect.collidepoint(e.pos):
            now = time.time()
            if now - self.last_click < 0.35:
                app.set(kof(self.k, app), self.default)
                self.last_click = 0.0
                return True
            self.last_click = now
            app.drag = self
            self.set_y(e.pos[1], app)
            return True
        if e.type == pygame.MOUSEWHEEL and self.rect.collidepoint(pygame.mouse.get_pos()):
            key = kof(self.k, app)
            app.set(key, app.get(key) + e.y * (1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 4))
            return True
        return False

    def drag_pos(self, pos, app):
        self.set_y(pos[1], app)

    def draw(self, surf, app):
        v = app.get(kof(self.k, app))
        t = v / 127.0
        pygame.draw.rect(surf, (6, 12, 14), (self.cx - 5, self.y, 10, self.h), border_radius=5)
        pygame.draw.rect(surf, (110, 140, 140), (self.cx - 5, self.y, 10, self.h), 1, border_radius=5)
        top = int(self.y + 24 + (1 - t) * (self.h - 48))
        pygame.draw.rect(surf, (30, 110, 98), (self.cx - 3, top, 6, self.y + self.h - 24 - top), border_radius=3)
        for k in range(0, 11):
            yy = self.y + 24 + k * (self.h - 48) / 10
            pygame.draw.line(surf, (96, 120, 120), (self.cx + 9, yy), (self.cx + 14, yy))
            pygame.draw.line(surf, (96, 120, 120), (self.cx - 14, yy), (self.cx - 9, yy))
        cap = pygame.Rect(0, 0, 34, 64)
        cap.center = (self.cx, top)
        pygame.draw.rect(surf, (10, 6, 2), cap.inflate(4, 4), border_radius=6)
        # orange body, shaded top-to-bottom like a dished finger grip
        for yy in range(cap.height):
            f = abs((yy + 0.5) / cap.height - 0.5) * 2.0          # 1 at the edges, 0 in the middle
            shade = 0.62 + 0.38 * (1.0 - f) ** 0.6
            col = (int(255 * shade), int(140 * shade), int(30 * shade))
            pygame.draw.line(surf, col, (cap.x + 2, cap.y + yy), (cap.right - 3, cap.y + yy))
        pygame.draw.rect(surf, (120, 54, 6), cap, 1, border_radius=5)
        pygame.draw.line(surf, (255, 205, 140), (cap.x + 4, cap.y + 1), (cap.right - 5, cap.y + 1))
        for dy in (-26, -21, -16, -11, -6, 6, 11, 16, 21, 26):                                  # finger ridges
            pygame.draw.line(surf, (150, 66, 4), (cap.x + 4, cap.centery + dy), (cap.right - 5, cap.centery + dy))
            pygame.draw.line(surf, (255, 176, 90), (cap.x + 4, cap.centery + dy + 1), (cap.right - 5, cap.centery + dy + 1))
        pygame.draw.line(surf, (70, 28, 0), (cap.x + 3, cap.centery), (cap.right - 4, cap.centery), 2)   # centre groove = value mark
        pygame.draw.line(surf, (255, 225, 190), (cap.x + 3, cap.centery + 2), (cap.right - 4, cap.centery + 2))


class PKnob:
    def __init__(self, k, cx, cy, label, small=True, fmt=None, default=None, sens=0.9, lo=None, bipolar=False):
        self.k, self.cx, self.cy, self.label, self.small = k, cx, cy, label, small
        self.R = 15 if small else 24
        self.fmt, self.default, self.sens, self.lo = fmt, default, sens, lo
        self.bipolar = bipolar
        self.acc, self.last_click = 0.0, 0.0

    def hit(self, pos):
        return math.hypot(pos[0] - self.cx, pos[1] - self.cy) <= self.R + 9

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.hit(e.pos):
            key = kof(self.k, app)
            now = time.time()
            if now - self.last_click < 0.35:
                app.set(key, default_of(key) if self.default is None else self.default)
                self.last_click = 0.0
                return True
            self.last_click = now
            self.acc = float(app.get(key))
            app.drag = self
            return True
        if e.type == pygame.MOUSEWHEEL and self.hit(pygame.mouse.get_pos()):
            key = kof(self.k, app)
            app.set(key, max(self.lo if self.lo is not None else -9999, app.get(key) + e.y))
            return True
        return False

    def drag_by(self, dy, app):
        key = kof(self.k, app)
        lo, hi = range_of(key)
        lo = hi if False else (self.lo if self.lo is not None else lo)
        fine = pygame.key.get_mods() & pygame.KMOD_SHIFT
        self.acc = max(lo, min(hi, self.acc - dy * (hi - lo) / 127.0 * self.sens * (0.25 if fine else 1.0)))
        app.set(key, int(round(self.acc)))

    def draw(self, surf, app):
        key = kof(self.k, app)
        v = app.get(key)
        lo, hi = range_of(key)
        lo = self.lo if self.lo is not None else lo
        t = max(0.0, (v - lo) / float(max(1, hi - lo)))
        cx, cy, R = self.cx, self.cy, self.R
        pygame.draw.circle(surf, (8, 14, 16), (cx, cy), R + 7)
        pygame.draw.circle(surf, (130, 160, 160), (cx, cy), R + 7, 1)
        w = 3 if self.small else 4
        pygame.draw.lines(surf, (30, 44, 46), False, arc_pts(cx, cy, R + 3, 135, 405), w)
        a1 = 135 + 270 * t
        if self.bipolar:
            pygame.draw.line(surf, (150, 170, 170), (cx, cy - R - 6), (cx, cy - R - 10), 2)
            if abs(a1 - 270) > 1.5:
                pygame.draw.lines(surf, AMBER, False, arc_pts(cx, cy, R + 3, min(a1, 270), max(a1, 270)), w)
        elif t > 0.001:
            pygame.draw.lines(surf, AMBER, False, arc_pts(cx, cy, R + 3, 135, a1), w)
        pygame.draw.circle(surf, (96, 104, 106), (cx, cy), R)
        pygame.draw.circle(surf, (150, 160, 162), (cx, cy), R - 2)
        pygame.draw.circle(surf, (190, 200, 202), (cx - 3, cy - 4), max(2, R - 12))
        ang = math.radians(a1)
        pygame.draw.line(surf, (10, 22, 22), (cx + math.cos(ang) * R * 0.3, cy + math.sin(ang) * R * 0.3),
                         (cx + math.cos(ang) * R * 0.88, cy + math.sin(ang) * R * 0.88), 2 if self.small else 3)
        if self.label:
            txt(surf, self.label, 10 if self.small else 12, DIM, (cx, cy + R + (12 if self.small else 15)), "center")
        shown = self.fmt(v) if self.fmt else str(v)
        txt(surf, shown, 11 if self.small else 14, AMBER, (cx, cy + R + (25 if self.small else 31)), "center", bold=True)


def fmt_pan(v):
    return "C" if v == 20 else (f"L{20 - v}" if v < 20 else f"R{v - 20}")


class Choice:
    """Button that is lit when is_on(app); calls cb(app) on click."""

    def __init__(self, rect, label, is_on, cb, size=16, led_on=True):
        self.rect, self.label, self.is_on, self.cb, self.size, self.led_on = pygame.Rect(rect), label, is_on, cb, size, led_on

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3) and self.rect.collidepoint(e.pos):
            self.cb(app) if e.button == 1 else getattr(self, "alt", lambda a: None)(app)
            return True
        return False

    def draw(self, surf, app, label=None):
        r = self.rect
        on = self.is_on(app)
        hover = r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(surf, (21, 32, 30), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, (30, 110, 98) if on else (BTN_HOT if hover else BTN), r, border_radius=6)
        pygame.draw.rect(surf, AMBER if (on or hover) else BTN_EDGE, r, 1, border_radius=6)
        s = label or self.label
        if self.led_on and r.w > 90:
            led(surf, (r.x + 16, r.centery), on, AMBER)
            txt(surf, s, fit_size(s, r.w - 44, (self.size, 15, 14, 13, 12), bold=True), (255, 255, 255) if on else TEXT,
                (r.x + 32, r.centery), "midleft", bold=True)
        else:
            txt(surf, s, fit_size(s, r.w - 8, (self.size, 14, 13, 12, 11, 10), bold=True), (255, 255, 255) if on else TEXT,
                r.center, "center", bold=True)


def hexs(b):
    return " ".join(f"{x:02X}" for x in b)


def describe(m):
    st = m[0]
    if st == 0xF0:
        if len(m) > 4 and m[1] == 0x7C and m[3] == 0x06:
            return "Identity request" if m[4] == 1 else "Identity reply: the VM200 says hello"
        if len(m) > 4 and m[1] == 0x7F and m[3] == 0x06:
            names = {5: "REWIND", 1: "STOP", 2: "PLAY", 4: "FORWARD", 6: "RECORD", 9: "PAUSE"}
            return f"MMC {names.get(m[4], hexs(m[4:5]))}"
        if len(m) > 12 and m[1] == 0x51 and m[3] in (0x2B, 0x3B):
            what = {1: "setup", 2: "scene", 3: "effect lib", 4: "EQ lib", 5: "PGM table", 6: "CTL table"}
            i = 4 if m[3] == 0x2B else 6
            kind = what.get(m[i + 6] if len(m) > i + 6 else 0, "?")
            return f"Bulk dump {'request' if m[3] == 0x2B else 'data'}: {kind} ({len(m)} bytes)"
        p = parse_vm(m)
        if p:
            return "Fostex: " + key_text(*p)
        if len(m) > 4 and m[1] == 0x51:
            fam = m[3]
            body = m[4:-1]
            if fam & 0xF0 == 0x20:
                return f"Fostex request {hexs(m[3:-1])}"
            if fam == 0x18 and body[:1] == [0x22]:
                return f"Fostex: scene recall {body[1]}"
            if fam == 0x18 and body[:1] == [0x23]:
                return f"Fostex: scene store {body[1]}"
            if fam == 0x1A and len(body) > 2 and body[1] == 0x72:
                return f"Fostex: effect {body[0]} library recall {body[2]}"
            return f"Fostex {hexs(m[3:-1])}"
        return f"SysEx {len(m)} bytes: {hexs(m)[:70]}"
    kind, ch = st & 0xF0, (st & 15) + 1
    if kind == 0xC0:
        return f"ch{ch:02d} Program Change {m[1]}" + (f"  (scene {m[1]:02d} if table 02)" if m[1] < 100 else "")
    if kind == 0xB0:
        return f"ch{ch:02d} Control Change {m[1]} = {m[2]}"
    return hexs(m)


HELP_TEXT = [
    ("H", "WHAT THIS IS"),
    ("P", "A control surface for the Fostex VM200 digital mixer. It moves the faders, ON and SOLO keys, pans, aux and effect sends and EQ over MIDI "
          "with the Fostex system-exclusive messages from the VM200 User's Guide, recalls scenes, loads effect presets, drives a recorder by MMC, "
          "reads the mixer's state back, and backs the whole mixer up to a file."),
    ("H", "FIRST TIME - GETTING THE MIXER TALKING"),
    ("B", "On the mixer press [MIDI] (page 1:MIDI TX&RX) and set: SYSTEM EX. TX&RX = ON, DevID = 0, BULK RX = ON, PROGRAM CHANGE TX and RX = ON, program change TABLE = 02, TX CH = 1, RX CH = OMNI, ECHO = OFF."),
    ("B", "Cables: the VM200's MIDI IN comes from a MIDI OUT of your interface; the VM200's MIDI OUT goes to a MIDI IN of your interface. Click MIDI OUT and MIDI IN at the top of this app and pick the matching ports."),
    ("B", "Press FIND VM200. If the status line says the mixer answered, everything is wired and set correctly. Then press READ MIXER and the screen fills with the mixer's real settings."),
    ("B", "Only one program can own a Windows MIDI port: close MIDI-OX or any DAW using these ports first."),
    ("B", "Nothing is sent when the app starts, and the screen shows defaults until you READ MIXER or move something."),
    ("H", "MIXER TAB"),
    ("B", "20 channel strips: ON, SOLO, phase (the circle-slash key), PAN and the FADER. Double-click a knob or fader to reset it; the mouse wheel nudges (Shift for fine). The ST BUSS and REC BUSS masters are on the right."),
    ("B", "The faders are motorised: when this app moves one over MIDI the real fader moves. If the mixer's own faders move, the screen follows (SYSTEM EX TX&RX must be ON)."),
    ("H", "SENDS TAB"),
    ("P", "Pick a bus along the top (AUX 1-4, EFF 1-2): the faders become every channel's send level into that bus, like the mixer's own Fader Mode keys. PRE/POST chooses whether the send is before or after the channel fader. The bus master (ON, SOLO, fader) is on the right."),
    ("H", "EQ TAB"),
    ("B", "Pick a channel (1-16). Each of the four bands has ON, gain (-18 to +18 dB), frequency and Q. The LO and HI bands can also be a high-pass / low-pass filter or a shelf. The graph is calculated from the settings."),
    ("B", "The mixer only runs EQ on the channels chosen by EQ ENABLE (1-8, 9-16 or 1-16) on its CURRENT SCENE STATUS page; the factory setting is 1-8."),
    ("H", "EFFECTS TAB"),
    ("B", "Choose any of the 50 presets or 50 user programs for effect 1 or 2. The effect master fader and ON are here too. Editing the individual effect parameters (reverb time and so on) can be added once their value scaling has been captured from your unit."),
    ("H", "SCENES TAB"),
    ("B", "Click a scene to recall it. With the mixer's program change table on 02 (preset), program 1 = scene 00, 2 = scene 01 ... 100 = scene 99, 101-103 = the three preset scenes."),
    ("B", "RIGHT-click a scene to give it a name here (the VM200 itself only shows its own 9-letter scene names). STORE writes the mixer's current state into the selected scene - it asks first."),
    ("H", "SYSTEM TAB"),
    ("B", "BACKUP asks the mixer for everything (setup, all 100 scenes, both libraries, the program and control tables) and saves one .syx file in vm200_backups. RESTORE sends a backup back. Do a backup before experimenting."),
    ("B", "MMC buttons drive any recorder connected by MIDI that understands MIDI Machine Control."),
    ("B", "Not every message could be confirmed without the mixer in front of me - the MONITOR tab and SAVE LOG are there to show exactly what was sent and what came back."),
]


# =============================================================================
# THE APP
# =============================================================================
TABS = ["MIXER", "SENDS", "EQ", "EFFECTS", "SCENES", "SYSTEM", "MONITOR", "HELP"]
SCENE_PRESETS = [("P0  InitMix", 100), ("P1  Recording", 101), ("P2  Mix Down", 102)]

CHECKLIST = [
    "# On the VM200 (press the [MIDI] key; page 1:MIDI TX&RX; turn EQ/LO and EQ/LO-MID GAIN to change values)",
    "TX CH = 1      RX CH = OMNI (or 1)      ECHO = OFF",
    "PROGRAM CHANGE  TX = ON   RX = ON   TABLE = 02 (preset: program 1 = scene 00, 2 = scene 01 ...)",
    "SYSTEM EX. TX&RX = ON      DevID = 0      BULK RX = ON",
    "CONTROL CHANGE TX = ON, RX = ON, TABLE = 02 (the mixer sends fader / ON / pan moves this way; the app sends via SysEx).",
    "# Cables",
    "VM200 MIDI IN  <-  a MIDI OUT of your interface (pick it as MIDI OUT here).",
    "VM200 MIDI OUT  ->  a MIDI IN of your interface (pick it as MIDI IN here).",
    "Then press FIND VM200 (top right). A reply means both cables and the settings are right.",
]


class App:
    def __init__(self, headless=False, tab=0):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("VM200 CONTROL")
        self.tab, self.running, self.drag, self.popup = tab, True, None, None
        self.help_scroll = 0
        self.v = {}
        self.sx = None
        self.events = deque(maxlen=6000)
        self.flash_out = self.flash_in = 0.0
        self.found = False
        self.eq_ch, self.last_q = 1, {}
        self.bus = 0
        self.scene = 0
        self.scname = ""
        self.names = {}
        self.bulk, self.bulk_active, self.bulk_last = [], False, 0.0
        self.counts = {"in": 0, "out": 0, "fostex": 0}
        self.status = "Nothing is sent until you touch a control. Set up the mixer (SYSTEM tab), then press FIND VM200."
        self.status_t = time.time()
        self.out = self.inp = None
        self.out_lock = threading.Lock()
        self.settings = {}
        try:
            self.settings = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
        try:
            self.names = json.loads(SCENES_FILE.read_text())
        except Exception:
            self.names = {}
        self.dev = clamp(self.settings.get("dev", 0), 0, 126)
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
        self.bg = self._make_bg()
        self.help_lines = []
        for kind, text in HELP_TEXT:
            if kind == "H":
                self.help_lines.append(("H", text))
            else:
                pre = "-  " if kind == "B" else ""
                for i, ln in enumerate(wrap(text, 15, 1240)):
                    self.help_lines.append(("T", (pre if i == 0 else ("   " if kind == "B" else "")) + ln))
        self.build_ui()

    # ---- model --------------------------------------------------------------
    def get(self, key):
        return self.v.get(key, default_of(key))

    def set(self, key, val, send=True):
        lo, hi = range_of(key)
        val = clamp(val, lo, hi)
        if send and self.get(key) == val and key in self.v:
            return
        self.v[key] = val
        pt = self.partner(key)
        if pt:
            self.v[pt] = val
        if key[0] == "eq" and key[3] == "type" and val >= 2:
            self.last_q[(key[1], key[2])] = val
        if send:
            self.send([sx_cmd(self.dev, key, val)], group=str(key), pauses=[0.0])

    def partner(self, key):
        """Key of the paired channel's matching control (faders / ON follow each other on the VM200)."""
        if len(key) == 3 and key[0] == "ch" and key[2] in ("vol", "on"):
            n = key[1]
            lo = n if n % 2 == 1 else n - 1
            if self.v.get(("ch", lo, "pair"), 1 if lo >= 17 else 0):
                return ("ch", n + 1 if n == lo else lo, key[2])
        return None

    def set_status(self, s):
        self.status, self.status_t = s, time.time()

    def log(self, d, text):
        self.events.append((time.time(), d, text))

    # ---- MIDI devices ------------------------------------------------------------
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
        print("MIDI outputs:", [n for _, n in self.out_devs])
        print("MIDI inputs: ", [n for _, n in self.in_devs])

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
            dev = pgmidi.Output(i) if kind == "out" else pgmidi.Input(i, 4096)
        except Exception as ex:
            if retry:
                self.rescan()
                self.reopen_all()
                return
            self.set_status(f"Could not open {key}: {ex}. Close any other program using it (MIDI-OX, a DAW) or re-plug the interface.")
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
        self.popup = Popup("MIDI OUT (to the VM200's MIDI IN)" if kind == "out" else "MIDI IN (from the VM200's MIDI OUT)",
                           opts, self.settings.get(kind + "_name"), pick)

    def save_settings(self):
        self.settings["dev"] = self.dev
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    # ---- sending --------------------------------------------------------------
    def write_msg(self, m):
        with self.out_lock:
            if self.out:
                try:
                    if m[0] == 0xF0:
                        self.out.write_sys_ex(0, list(m))
                    else:
                        self.out.write_short(*m)
                except Exception as ex:
                    self.status = f"MIDI write error: {ex}"
        self.flash_out = time.time()
        self.counts["out"] += 1
        if len(m) < 400:
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
            time.sleep(GAP + item[1])

    def send(self, msgs, group=None, pauses=None):
        with self.qlock:
            if group:
                self.q = deque(m for m in self.q if m[2] != group)
            for i, m in enumerate(msgs):
                self.q.append((m, pauses[i] if pauses else 0.0, group))

    # ---- actions ---------------------------------------------------------------------
    def find_vm200(self, _b=1):
        self.found = False
        self.send([[0xF0, 0x7C, self.dev, 0x06, 0x01, 0xF7]], group="find")
        self.set_status("Identity request sent. If the VM200 answers, the status here will say so "
                        "(needs both cables, SYSTEM EX TX&RX = ON and the same DevID).")

    def read_mixer(self, full=True, _b=1):
        d, r = self.dev, []
        for n in range(1, N_CH + 1):
            for f in ("on", "vol", "pan", "solo", "phase", "pair"):
                r.append(sx_req(d, ("ch", n, f)))
        for f in ("on", "vol", "solo"):
            r.append(sx_req(d, ("st", f)))
        for f in ("vol", "solo"):
            r.append(sx_req(d, ("rec", f)))
        for kind in ("aux", "eff"):
            for a in range(1, 5 if kind == "aux" else 3):
                for f in ("on", "vol", "solo"):
                    r.append(sx_req(d, (kind, a, f)))
        if full:
            for n in range(1, N_CH + 1):
                for a in range(1, 5):
                    r += [sx_req(d, ("ch", n, "aux", a)), sx_req(d, ("ch", n, "auxpp", a))]
                for e in (1, 2):
                    r += [sx_req(d, ("ch", n, "eff", e)), sx_req(d, ("ch", n, "effpp", e))]
            for n in range(1, 17):
                for b in range(1, 5):
                    for f in ("on", "type", "gain", "freq"):
                        r.append(sx_req(d, ("eq", n, b, f)))
            for e in (1, 2):
                r += [sx_req(d, ("efft", e)), [0xF0, 0x51, d, 0x2A, e, 0x70, 0xF7]]
            r.append([0xF0, 0x51, d, 0x28, 0x20, 0xF7])
        self.send(r, group="read", pauses=[0.0] * len(r))
        self.set_status(f"Asked the mixer for {len(r)} values - the screen fills in as the answers arrive "
                        f"(about {int(len(r) * (GAP + 0.004)) + 1} s). Nothing answers? See the SYSTEM tab checklist.")

    def mmc(self, cmd):
        dev = self.settings.get("mmc_dev", 0x7F)
        self.send([[0xF0, 0x7F, dev, 0x06, cmd, 0xF7]], group="mmc")

    def recall_scene(self, s, _b=1):
        self.scene = s
        if self.settings.get("scene_via") == "sysex" and s < 100:
            self.send([[0xF0, 0x51, self.dev, 0x18, 0x22, s, 0xF7]], group="scene")
        else:
            self.send([[0xC0 | (self.settings.get("pc_ch", 1) - 1), s & 0x7F]], group="scene")
        self.scname = self.names.get(str(s), "")
        self.set_status(f"Scene {self.scene_label(s)} recalled" + (" by SysEx." if self.settings.get("scene_via") == "sysex"
                                                                    else " by Program Change.") + " Reading the mixer back...")
        threading.Timer(0.8, lambda: self.read_mixer(full=False)).start()

    def scene_label(self, s):
        return f"P{s - 100}" if s >= 100 else f"{s:02d}"

    def store_scene(self, s):
        self.send([[0xF0, 0x51, self.dev, 0x18, 0x23, s, 0xF7]], group="store")
        self.set_status(f"Store scene {s:02d} sent (SysEx). The mixer needs SYSTEM EX TX&RX ON.")

    def ask_store(self, _b=1):
        s = self.scene
        if s >= 100:
            self.set_status("The preset scenes cannot be overwritten. Pick a scene 00-99.")
            return
        self.popup = Info("Store the mixer's current settings", [
            f"This overwrites scene {s:02d} in the VM200's memory with whatever the mixer is set to now.",
            "There is no undo. A backup first (SYSTEM tab) is a good idea."],
            [("STORE", lambda: self.store_scene(s)), ("CANCEL", None)])

    def set_scene_name(self, _b=1):
        name = re.sub(r"[^ -~]", "", self.textbox.text).strip()[:9]
        self.names[str(self.scene)] = name
        try:
            SCENES_FILE.write_text(json.dumps(self.names, indent=1))
        except Exception:
            pass
        self.scname = name
        self.send([[0xF0, 0x51, self.dev, 0x18, 0x20, *name.encode("ascii", "ignore"), 0xF7]], group="scname")
        self.set_status(f"Name '{name}' saved here and sent to the mixer's current scene name (max 9 letters).")

    def eff_recall(self, e, lib):
        self.send([[0xF0, 0x51, self.dev, 0x1A, e, 0x72, lib, 0xF7]], group=f"effrcl{e}")
        self.set_status(f"Effect {e}: library program {lib:02d} recalled.")
        threading.Timer(0.5, lambda: self.send([sx_req(self.dev, ("efft", e)), [0xF0, 0x51, self.dev, 0x2A, e, 0x70, 0xF7]])).start()

    def pick_effect(self, e, user=False):
        opts = [(i, f"{i:02d} {EFF_PRESETS[i]}") for i in range(50)] if not user else [(i, f"{i}") for i in range(50, 100)]
        self.popup = Popup(f"Effect {e}: " + ("user library" if user else "preset library"), opts, None, lambda i: self.eff_recall(e, i))

    def eq_lib(self, _b=1):
        ch = self.eq_ch
        self.popup = Popup(f"EQ library recall into channel {ch}", [(i, f"{i:02d}") for i in range(100)], None,
                           lambda i: (self.send([[0xF0, 0x51, self.dev, 0x19, 0x10, ch, 0x5E, i, 0xF7]], group="eqlib"),
                                      self.set_status(f"EQ library {i:02d} recalled into channel {ch}."),
                                      threading.Timer(0.6, lambda: self.send([sx_req(self.dev, ("eq", ch, b, f)) for b in range(1, 5)
                                                                              for f in ("on", "type", "gain", "freq")])).start()))

    # ---- bulk backup ------------------------------------------------------------------------
    def backup(self, _b=1):
        d = self.dev
        reqs = [[0xF0, 0x51, d, 0x2B, *BULK_ID, 0x01, 0x7F, 0xF7], [0xF0, 0x51, d, 0x2B, *BULK_ID, 0x02, 0x7F, 0xF7],
                [0xF0, 0x51, d, 0x2B, *BULK_ID, 0x03, 0x7F, 0xF7], [0xF0, 0x51, d, 0x2B, *BULK_ID, 0x04, 0x7F, 0xF7],
                [0xF0, 0x51, d, 0x2B, *BULK_ID, 0x05, 0x00, 0xF7], [0xF0, 0x51, d, 0x2B, *BULK_ID, 0x06, 0x00, 0xF7]]
        self.bulk, self.bulk_active, self.bulk_last = [], True, time.time()
        self.send(reqs, group="bulk", pauses=[2.0, 14.0, 5.0, 5.0, 2.0, 2.0])
        self.set_status("Backup started: asking the mixer for setup, scenes, effect library, EQ library and tables "
                        "(BULK RX must be ON). It saves by itself when the data stops arriving - about 40 s.")

    def finish_backup(self):
        self.bulk_active = False
        if not self.bulk:
            self.set_status("The mixer sent no bulk data. Check BULK RX = ON and SYSTEM EX TX&RX = ON, the cables, and DevID.")
            return
        BACKUP_DIR.mkdir(exist_ok=True)
        p = BACKUP_DIR / f"vm200_{datetime.datetime.now():%Y%m%d_%H%M%S}.syx"
        p.write_bytes(b"".join(bytes(m) for m in self.bulk))
        self.set_status(f"Saved {len(self.bulk)} bulk messages ({p.stat().st_size // 1024} KB) to vm200_backups/{p.name}")

    def ask_restore(self, _b=1):
        files = sorted(BACKUP_DIR.glob("*.syx"), reverse=True) if BACKUP_DIR.exists() else []
        if not files:
            self.set_status("No backups yet (vm200_backups folder). Press BACKUP first.")
            return
        self.popup = Popup("Restore which backup to the VM200?", [(f, f.name) for f in files[:24]], None, self.confirm_restore)

    def confirm_restore(self, path):
        self.popup = Info("Restore backup", [
            f"Send {path.name} back to the VM200?",
            "This overwrites the mixer's setup, scenes, libraries and tables with the backup. It cannot be undone, "
            "so make a fresh BACKUP first if the mixer holds anything you need."],
            [("RESTORE", lambda: self.do_restore(path)), ("CANCEL", None)])

    def do_restore(self, path):
        data, msgs, cur = path.read_bytes(), [], []
        for b in data:
            if b == 0xF0:
                cur = [b]
            elif cur:
                cur.append(b)
                if b == 0xF7:
                    msgs.append(cur)
                    cur = []
        self.send(msgs, group="restore", pauses=[0.25] * len(msgs))
        self.set_status(f"Restoring {len(msgs)} messages to the mixer (BULK RX must be ON). Wait until the log stops moving.")

    def save_log(self, _b=1):
        t0 = self.events[0][0] if self.events else 0
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            for t, d, s in self.events:
                f.write(f"{t - t0:9.3f}  {d}  {s}\n")
        self.set_status(f"Saved {LOG_FILE.name} ({len(self.events)} events).")

    def clear_log(self, _b=1):
        self.events.clear()

    # ---- UI construction ----------------------------------------------------------------
    def build_ui(self):
        self.top_btns = [
            Button((330, 6, 400, 26), "", lambda b: self.choose_device("out", b)),
            Button((330, 38, 400, 26), "", lambda b: self.choose_device("in", b)),
            Button((1010, 12, 170, 44), "READ MIXER", self.read_mixer, big=True),
            Button((1195, 12, 175, 44), "FIND VM200", self.find_vm200, big=True),
        ]
        self.out_btn, self.in_btn = self.top_btns[:2]
        self.tab_btns = [Button((30 + i * 118, 82, 112, 30), t, lambda b, i=i: self.set_tab(i)) for i, t in enumerate(TABS)]
        # --- mixer tab
        self.w_mixer = []
        for i in range(N_CH):
            n, cx = i + 1, 38 + i * 58 + 29
            self.w_mixer += [PToggle(("ch", n, "on"), (cx - 24, 158, 48, 26), "ON"),
                             PToggle(("ch", n, "solo"), (cx - 24, 190, 48, 26), "SOLO", SOLO_RED),
                             PToggle(("ch", n, "phase"), (cx - 24, 222, 48, 22), "PH", ORANGE),
                             PKnob(("ch", n, "pan"), cx, 290, "PAN", True, fmt_pan, bipolar=True),
                             PFader(("ch", n, "vol"), cx, 356, 340)]
        self.w_mixer += [PToggle(("st", "on"), (1238, 158, 48, 26), "ON"),
                         PToggle(("st", "solo"), (1238, 190, 48, 26), "SOLO", SOLO_RED),
                         PFader(("st", "vol"), 1262, 356, 340),
                         PToggle(("rec", "solo"), (1304, 190, 48, 26), "SOLO", SOLO_RED),
                         PFader(("rec", "vol"), 1328, 356, 340)]
        # --- sends tab
        self.w_sends = [Choice((40 + i * 160, 128, 150, 40), name, lambda a, i=i: a.bus == i, lambda a, i=i: setattr(a, "bus", i),
                               led_on=False) for i, (name, _, _) in enumerate(BUSES)]
        for i in range(N_CH):
            n, cx = i + 1, 38 + i * 58 + 29
            self.w_sends += [PToggle(lambda a, n=n: ("ch", n, "auxpp" if BUSES[a.bus][1] == "aux" else "effpp", BUSES[a.bus][2]),
                                     (cx - 24, 206, 48, 24), "PRE", AMBER),
                             PFader(lambda a, n=n: ("ch", n, BUSES[a.bus][1], BUSES[a.bus][2]), cx, 270, 420, default=0)]
        self.w_sends += [PToggle(lambda a: (BUSES[a.bus][1], BUSES[a.bus][2], "on"), (1244, 216, 56, 28), "ON"),
                         PToggle(lambda a: (BUSES[a.bus][1], BUSES[a.bus][2], "solo"), (1306, 216, 56, 28), "SOLO", SOLO_RED),
                         PFader(lambda a: (BUSES[a.bus][1], BUSES[a.bus][2], "vol"), 1295, 290, 400)]
        # --- eq tab
        self.w_eq = [Choice((40 + i * 44, 128, 40, 34), str(i + 1), lambda a, i=i: a.eq_ch == i + 1,
                            lambda a, i=i: setattr(a, "eq_ch", i + 1), led_on=False) for i in range(16)]
        for b in range(1, 5):
            x = 30 + (b - 1) * 336
            ck = lambda a, b=b: ("eq", a.eq_ch, b, "on")
            self.w_eq += [PToggle(ck, (x + 14, 524, 56, 28), "ON"),
                          PKnob(lambda a, b=b: ("eq", a.eq_ch, b, "gain"), x + 60, 640, "GAIN", False,
                                lambda v: f"{(v - 36) / 2:+.1f} dB", default=36, bipolar=True),
                          PKnob(lambda a, b=b: ("eq", a.eq_ch, b, "freq"), x + 161, 640, "FREQ", False, fmt_hz, sens=1.6),
                          PKnob(lambda a, b=b: ("eq", a.eq_ch, b, "type"), x + 262, 640, "Q", False,
                                lambda v: (f"Q {q_of(v):.2f}" if v >= 2 else "-"), lo=2, default=30)]
            if b in (1, 4):
                for j, (lab, mode) in enumerate((("HPF" if b == 1 else "LPF", 0), ("SHELF", 1), ("PEAK", 2))):
                    self.w_eq.append(Choice((x + 82 + j * 76, 524, 72, 28), lab, lambda a, b=b, m=mode: self.eq_mode(a, b) == m,
                                            lambda a, b=b, m=mode: self.set_eq_mode(b, m), led_on=False, size=13))
        self.w_eq += [Button((800, 380, 250, 40), "EQ LIBRARY RECALL...", self.eq_lib),
                      Button((1070, 380, 280, 40), "FLAT: ALL BANDS 0 dB", self.eq_flat)]
        # --- effects tab
        self.w_fx = []
        for e in (1, 2):
            x = 30 + (e - 1) * 680
            self.w_fx += [PToggle(("eff", e, "on"), (x + 30, 168, 70, 30), "ON"),
                          PToggle(("eff", e, "solo"), (x + 30, 206, 70, 30), "SOLO", SOLO_RED),
                          PFader(("eff", e, "vol"), x + 65, 262, 296),
                          Button((x + 150, 250, 340, 46), "PRESET LIBRARY  (00-49)...", lambda b, e=e: self.pick_effect(e)),
                          Button((x + 150, 308, 340, 46), "USER LIBRARY  (50-99)...", lambda b, e=e: self.pick_effect(e, True)),
                          Button((x + 150, 366, 340, 46), "READ THIS EFFECT", lambda b, e=e: self.send(
                              [sx_req(self.dev, ("efft", e)), [0xF0, 0x51, self.dev, 0x2A, e, 0x70, 0xF7]], group=f"fxr{e}"))]
        # --- scenes tab
        self.textbox = TextBox((890, 372, 330, 36))
        self.w_scenes = [Button((890, 260, 220, 40), "RECALL", lambda b: self.recall_scene(self.scene)),
                         Button((1130, 260, 220, 40), "STORE...", self.ask_store),
                         Button((890, 312, 220, 40), "< PREV", lambda b: self.step_scene(-1)),
                         Button((1130, 312, 220, 40), "NEXT >", lambda b: self.step_scene(1)),
                         Button((1230, 372, 120, 36), "SET NAME", self.set_scene_name),
                         Choice((890, 420, 460, 28), "recall by program change", lambda a: a.settings.get("scene_via") != "sysex",
                                lambda a: (a.settings.__setitem__("scene_via", "pc" if a.settings.get("scene_via") == "sysex" else "sysex"),
                                           a.save_settings()), size=13, led_on=False)]
        for j, (lab, v) in enumerate(SCENE_PRESETS):
            self.w_scenes.append(Button((890 + j * 156, 500, 148, 40), lab, lambda b, v=v: self.recall_scene(v)))
        # --- system tab
        self.w_sys = [Button((50, 360, 300, 46), "FIND VM200", self.find_vm200),
                      Button((370, 360, 300, 46), "READ MIXER (everything)", self.read_mixer),
                      Button((730, 180, 280, 46), "BACKUP THE WHOLE MIXER", self.backup),
                      Button((1030, 180, 320, 46), "RESTORE A BACKUP...", self.ask_restore),
                      Button((260, 170, 36, 32), "-", lambda b: self.step_dev(-1)), Button((300, 170, 36, 32), "+", lambda b: self.step_dev(1)),
                      Button((260, 220, 36, 32), "-", lambda b: self.step_pc(-1)), Button((300, 220, 36, 32), "+", lambda b: self.step_pc(1))]
        self.w_sys += [Button((50 + i * 218, 506, 206, 44), name, lambda b, c=c: self.mmc(c), big=True) for i, (name, c) in enumerate(MMC)]
        self.w_mon = [Button((960, 130, 190, 36), "SAVE LOG", self.save_log), Button((1170, 130, 190, 36), "CLEAR", self.clear_log)]
        self.tab_lists = [self.w_mixer, self.w_sends, self.w_eq, self.w_fx, self.w_scenes, self.w_sys, self.w_mon, []]

    def set_tab(self, i):
        self.tab = i
        self.help_scroll = 0

    def step_dev(self, d):
        self.dev = clamp(self.dev + d, 0, 126)
        self.save_settings()

    def step_pc(self, d):
        self.settings["pc_ch"] = clamp(self.settings.get("pc_ch", 1) + d, 1, 16)
        self.save_settings()

    def step_scene(self, d):
        self.scene = (self.scene + d) % 100
        self.scname = self.names.get(str(self.scene), "")
        self.recall_scene(self.scene)

    def eq_mode(self, a, band):
        t = a.get(("eq", a.eq_ch, band, "type"))
        return 0 if t == 0 else (1 if t == 1 else 2)

    def set_eq_mode(self, band, mode):
        ch = self.eq_ch
        self.set(("eq", ch, band, "type"), mode if mode < 2 else self.last_q.get((ch, band), 30))

    def eq_flat(self, _b=1):
        for b in range(1, 5):
            self.set(("eq", self.eq_ch, b, "gain"), 36)

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
            y = rnd.randrange(H); x = rnd.randrange(W); ln = rnd.randrange(80, 500)
            c = (255, 255, 255, rnd.randrange(2, 5)) if rnd.random() < 0.5 else (0, 10, 10, rnd.randrange(3, 8))
            pygame.draw.line(brush, c, (x, y), (x + ln, y))
        bg.blit(brush, (0, 0))
        return bg

    # ---- incoming MIDI ---------------------------------------------------------------------
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
                continue
            if b0 in (0xFE, 0xF8, 0xFA, 0xFB, 0xFC) or b0 < 0x80:
                continue
            self.flash_in = time.time()
            self.counts["in"] += 1
            kind = b0 & 0xF0
            m = [b0, data[1]] if kind in (0xC0, 0xD0) else [b0, data[1], data[2]]
            self.log("IN ", describe(m))
            if kind == 0xB0:
                r = cc_to_key(m[1], m[2])
                if r:
                    self.counts["fostex"] += 1
                    self.v[r[0]] = clamp(r[1], *range_of(r[0]))
                    pt = self.partner(r[0])
                    if pt:
                        self.v[pt] = self.v[r[0]]
                elif m[1] in (116, 117):
                    e = m[1] - 115
                    threading.Timer(0.4, lambda e=e: self.send([sx_req(self.dev, ("efft", e)),
                                                                [0xF0, 0x51, self.dev, 0x2A, e, 0x70, 0xF7]])).start()
            if kind == 0xC0:
                self.scene = m[1] if m[1] <= 102 else self.scene
                self.scname = self.names.get(str(self.scene), "")
                threading.Timer(0.8, lambda: self.read_mixer(full=False)).start()

    def on_sysex(self, m):
        self.flash_in = time.time()
        self.counts["in"] += 1
        if len(m) > 3 and m[1] in (0x7C, 0x51) and not self.found:
            self.found = True
            self.found_t = time.time()
            self.set_status("FOUND!!  The VM200 answered - cables, SysEx and DevID are all right. Press READ MIXER to load its settings.")
        if len(m) > 3 and m[1] == 0x51 and m[3] == 0x3B:
            if self.bulk_active:
                self.bulk.append(list(m))
                self.bulk_last = time.time()
                self.counts["fostex"] += 1
            self.log("IN ", describe(m))
            return
        self.log("IN ", describe(m))
        p = parse_vm(m)
        if p:
            self.counts["fostex"] += 1
            key, val = p
            if key[0] == "scname":
                self.scname = val
            else:
                self.v[key] = val if key[0] in ("effname",) else clamp(val, *range_of(key)) if key[0] != "effp" else val

    # ---- events -------------------------------------------------------------------------------
    def handle(self, e):
        if e.type == pygame.QUIT:
            self.running = False
            return
        if self.popup:
            self.popup.handle(e, self)
            return
        if e.type == pygame.MOUSEMOTION and self.drag:
            if hasattr(self.drag, "drag_pos"):
                self.drag.drag_pos(e.pos, self)
            else:
                self.drag.drag_by(e.rel[1], self)
            return
        if e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = None
            return
        if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE and not self.textbox.focus:
            self.running = False
            return
        if self.tab == 4 and self.textbox.handle(e, self):
            return
        if self.tab == 7 and e.type == pygame.MOUSEWHEEL:
            self.help_scroll = max(0, min(max(0, len(self.help_lines) * 24 + 60 - 610), self.help_scroll - e.y * 40))
            return
        for w in list(self.top_btns) + list(self.tab_btns) + self.tab_lists[self.tab]:
            if w.handle(e, self):
                return
        if self.tab == 4 and e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            for s, r in enumerate(self.scene_rects):
                if r.collidepoint(e.pos):
                    if e.button == 1:
                        self.scname = self.names.get(str(s), "")
                        self.recall_scene(s)
                    else:
                        self.scene, self.scname = s, self.names.get(str(s), "")
                        self.textbox.text = self.scname
                        self.set_status(f"Scene {s:02d} selected - type a name and press SET NAME.")
                    return

    # ---- drawing ---------------------------------------------------------------------------------
    def status_line(self, s, pos, width=900, lines=2):
        if time.time() - self.status_t < 40:
            for i, ln in enumerate(wrap(self.status, 12, width)[:lines]):
                txt(s, ln, 12, (190, 240, 228), (pos[0], pos[1] + i * 15))

    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        now = time.time()
        txt(s, "VM200", 40, ORANGE, (30, 8), bold=True)
        txt(s, "D I G I T A L   M I X E R   C O N T R O L", 11, (235, 135, 40), (32, 52))
        out_name = (self.settings.get("out_name") or "none  (click to choose)").replace("#0", "")
        in_name = (self.settings.get("in_name") or "none  (click to choose)").replace("#0", "")
        self.out_btn.draw(s, self, f"MIDI OUT:  {out_name}"[:50])
        self.in_btn.draw(s, self, f"MIDI IN:   {in_name}"[:50])
        led(s, (752, 19), now - self.flash_out < 0.12, AMBER)
        led(s, (752, 51), now - self.flash_in < 0.12, GREEN)
        txt(s, "VM200 online" if self.found else "", 12, GREEN, (770, 28))
        self.top_btns[2].draw(s, self)
        self.top_btns[3].draw(s, self)
        for i, b in enumerate(self.tab_btns):
            if i == self.tab:
                pygame.draw.rect(s, (30, 110, 98), b.rect, border_radius=6)
                pygame.draw.rect(s, AMBER, b.rect, 1, border_radius=6)
                txt(s, b.label, 14, (255, 255, 255), b.rect.center, "center", bold=True)
            else:
                b.draw(s, self)
        [self.draw_mixer, self.draw_sends, self.draw_eq, self.draw_fx, self.draw_scenes, self.draw_system,
         self.draw_monitor, self.draw_help][self.tab](s)
        panel(s, (30, 796, 1340, 54), "")
        self.status_line(s, (50, 806), 1000)
        sc = f"SCENE {self.scene_label(self.scene)}" + (f"  {self.scname}" if self.scname else "")
        txt(s, sc, 14, AMBER, (1350, 812), "topright", bold=True)
        txt(s, f"DevID {self.dev}", 12, DIM, (1350, 830), "topright")
        if self.popup:
            self.popup.draw(s, self)
        ft = getattr(self, "found_t", 0)
        if now - ft < 5:
            r = pygame.Rect(0, 0, 520, 130); r.center = (W // 2, H // 2)
            pygame.draw.rect(s, (8, 40, 30), r, border_radius=16)
            pygame.draw.rect(s, GREEN, r, 4, border_radius=16)
            txt(s, "FOUND!!", 72, GREEN, (r.centerx, r.y + 10), "midtop", bold=True)
            txt(s, "VM200 is talking to the app", 18, TEXT, (r.centerx, r.bottom - 34), "midtop")

    def strips(self, s, title, rect, label_y=140):
        panel(s, rect, title)
        for i in range(N_CH):
            x = 38 + i * 58
            txt(s, str(i + 1), 14, TEXT, (x + 29, label_y), "center", bold=True)
            if i:
                pygame.draw.line(s, (50, 76, 78), (x - 1, rect[1] + 30), (x - 1, rect[1] + rect[3] - 12))

    def draw_mixer(self, s):
        self.strips(s, "", (30, 124, 1180, 662))
        panel(s, (1220, 124, 150, 662), "")
        txt(s, "ST BUSS", 12, DIM, (1262, 140), "center", bold=True)
        txt(s, "REC BUSS", 12, DIM, (1328, 140), "center", bold=True)
        for w in self.w_mixer:
            w.draw(s, self)
        for i in range(N_CH):
            cx = 38 + i * 58 + 29
            txt(s, str(self.get(("ch", i + 1, "vol"))), 13, AMBER, (cx, 712), "center", bold=True)
            txt(s, "FADER", 9, DIM, (cx, 732), "center")
        txt(s, str(self.get(("st", "vol"))), 13, AMBER, (1262, 712), "center", bold=True)
        txt(s, str(self.get(("rec", "vol"))), 13, AMBER, (1328, 712), "center", bold=True)

    def draw_sends(self, s):
        for c in self.w_sends[:6]:
            c.draw(s, self)
        name, kind, idx = BUSES[self.bus]
        self.strips(s, "", (30, 184, 1180, 602), label_y=196)
        txt(s, f"SEND LEVEL OF EVERY CHANNEL INTO {name}", 13, AMBER, (1010, 146), bold=True)
        panel(s, (1220, 184, 150, 602), "")
        txt(s, f"{name} MASTER", 12, DIM, (1295, 196), "center", bold=True)
        for w in self.w_sends[6:]:
            w.draw(s, self)
        for i in range(N_CH):
            cx = 38 + i * 58 + 29
            txt(s, str(self.get(("ch", i + 1, kind, idx))), 13, AMBER, (cx, 700), "center", bold=True)
        txt(s, str(self.get((kind, idx, "vol"))), 13, AMBER, (1295, 700), "center", bold=True)

    def draw_eq(self, s):
        for c in self.w_eq[:16]:
            c.draw(s, self)
        ch = self.eq_ch
        panel(s, (30, 176, 740, 300), f"Channel {ch} - frequency response")
        g = pygame.Rect(60, 206, 690, 240)
        pygame.draw.rect(s, (6, 12, 14), g, border_radius=4)
        pygame.draw.rect(s, (110, 140, 140), g, 1, border_radius=4)
        for db in (-18, -12, -6, 0, 6, 12, 18):
            y = g.centery - db / 18.0 * (g.h / 2 - 6)
            pygame.draw.line(s, (60, 86, 86) if db else (140, 170, 170), (g.x, y), (g.right, y))
            txt(s, f"{db:+d}" if db else "0", 10, DIM, (g.x + 4, y - 12))
        for f, lab in ((50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1k"), (2000, "2k"), (5000, "5k"), (10000, "10k")):
            x = g.x + (math.log10(f / 20.0) / 3.0) * g.w
            pygame.draw.line(s, (40, 62, 62), (x, g.y), (x, g.bottom))
            txt(s, lab, 10, DIM, (x, g.bottom + 3), "midtop")
        freqs, dbs = eq_response(self, ch)
        pts = [(g.x + (math.log10(f / 20.0) / 3.0) * g.w, g.centery - max(-18, min(18, d)) / 18.0 * (g.h / 2 - 6)) for f, d in zip(freqs, dbs)]
        pygame.draw.lines(s, AMBER, False, pts, 3)
        panel(s, (790, 176, 580, 300), f"Channel {ch} EQ")
        note = ("Channels 1-16 have a 4-band EQ. The mixer's EQ ENABLE setting (CURRENT SCENE STATUS page) decides which channels it "
                "runs on: 1-8 by default, or 9-16 / 1-16. Graph shows the settings on screen (press READ MIXER first to load the real ones).")
        for i, ln in enumerate(wrap(note, 13, 540)):
            txt(s, ln, 13, TEXT, (810, 220 + i * 19))
        for c in self.w_eq[16:]:
            if isinstance(c, Button):
                c.draw(s, self)
        for b in range(1, 5):
            x = 30 + (b - 1) * 336
            panel(s, (x, 490, 322, 230), BANDS[b - 1] + " band")
        for c in self.w_eq[16:]:
            if not isinstance(c, Button):
                c.draw(s, self)
        for b in range(1, 5):
            x = 30 + (b - 1) * 336
            t = self.get(("eq", ch, b, "type"))
            mode = {0: ("high-pass filter" if b == 1 else "low-pass filter") if b in (1, 4) else "peaking", 1: "shelving"}.get(t, "peaking (bell)")
            if b in (2, 3):
                mode = "peaking (bell)"
            txt(s, mode, 12, DIM, (x + 161, 580), "center")

    def draw_fx(self, s):
        for e in (1, 2):
            x = 30 + (e - 1) * 680
            panel(s, (x, 124, 660, 500), f"Effect {e}")
            lcd(s, (x + 150, 160, 340, 64), self.fx_name(e), 26)
        panel(s, (30, 640, 1340, 140), "About the effects")
        note = ("The effect name is read back from the mixer (press READ THIS EFFECT or READ MIXER). Each channel's send into EFF 1 and EFF 2 is on the "
                "SENDS tab. Recalling a library program replaces the effect type and all its parameters, like the RECALL key on the mixer.")
        for i, ln in enumerate(wrap(note, 14, 1290)):
            txt(s, ln, 14, TEXT, (50, 680 + i * 20))
        for w in self.w_fx:
            w.draw(s, self)
        for e in (1, 2):
            x = 30 + (e - 1) * 680
            txt(s, str(self.get(("eff", e, "vol"))), 14, AMBER, (x + 65, 574), "center", bold=True)
            txt(s, "MASTER", 11, DIM, (x + 65, 596), "center")

    def fx_name(self, e):
        nm = self.v.get(("effname", e))
        return nm if nm else f"EFFECT {e}  (not read yet)"

    def draw_scenes(self, s):
        panel(s, (30, 124, 820, 660), "Scene memory 00 - 99")
        txt(s, "click = recall    right-click = name", 12, DIM, (500, 130))
        self.scene_rects = []
        mouse = pygame.mouse.get_pos()
        for n in range(100):
            r = pygame.Rect(42 + (n % 10) * 80, 160 + (n // 10) * 61, 76, 56)
            self.scene_rects.append(r)
            cur = n == self.scene
            pygame.draw.rect(s, (30, 110, 98) if cur else (BTN_HOT if r.collidepoint(mouse) else BTN), r, border_radius=6)
            pygame.draw.rect(s, AMBER if cur else BTN_EDGE, r, 1, border_radius=6)
            txt(s, f"{n:02d}", 14, AMBER if cur else AMBER_DIM, (r.x + 8, r.y + 6), bold=True)
            nm = self.names.get(str(n), "")
            if nm:
                txt(s, nm[:11], 11, (255, 255, 255) if cur else TEXT, (r.x + 8, r.y + 32))
        panel(s, (870, 124, 500, 340), "Current scene")
        panel(s, (870, 470, 500, 314), "Preset scenes")
        lcd(s, (890, 164, 460, 70), f"{self.scene_label(self.scene)}  {self.scname}"[:22], 30)
        for w in self.w_scenes:
            w.draw(s, self, "RECALL BY:  " + ("SYSEX" if self.settings.get("scene_via") == "sysex" else "PROGRAM CHANGE")
                   if (isinstance(w, Choice)) else None)
        self.textbox.draw(s, self)
        note = ("Program change n recalls scene n-1 when the mixer's table is 02. The preset scenes are InitMix, Recording and Mix Down. "
                "RECALL is by MIDI program change on the channel set in SYSTEM; switch to SysEx if you prefer. After a recall the app "
                "reads the mixer so the faders on screen follow.")
        for i, ln in enumerate(wrap(note, 13, 460)):
            txt(s, ln, 13, TEXT, (890, 556 + i * 19))

    def draw_system(self, s):
        panel(s, (30, 124, 680, 336), "Connection")
        panel(s, (720, 124, 650, 160), "Backup")
        panel(s, (720, 292, 650, 92), "Status")
        panel(s, (30, 470, 1340, 100), "MMC transport (to a MIDI-controlled recorder)")
        panel(s, (30, 580, 1340, 204), "Set up the mixer first")
        txt(s, "VM200 DevID", 15, TEXT, (50, 176))
        txt(s, str(self.dev), 22, AMBER, (212, 172), bold=True)
        txt(s, "scene PC channel", 15, TEXT, (50, 226))
        txt(s, str(self.settings.get("pc_ch", 1)), 22, AMBER, (212, 222), bold=True)
        txt(s, "= mixer's DevID (default 0)", 12, DIM, (356, 180))
        txt(s, "channel for scene recalls", 12, DIM, (356, 230))
        for i, ln in enumerate(wrap("FIND VM200 sends a MIDI identity request. A reply proves the cables, DevID and SYSTEM EX TX&RX are right. "
                                    "READ MIXER then asks for about 700 values (takes ~12 s).", 13, 640)):
            txt(s, ln, 13, TEXT, (50, 282 + i * 19))
        for i, ln in enumerate(wrap("A backup is one .syx file in vm200_backups. The mixer must have BULK RX = ON. It takes about 40 seconds.", 13, 610)):
            txt(s, ln, 13, TEXT, (740, 236 + i * 19))
        txt(s, f"found: {'YES' if self.found else 'no reply yet'}     in: {self.counts['in']}   out: {self.counts['out']}   VM200 messages: {self.counts['fostex']}",
            13, GREEN if self.found else DIM, (740, 330))
        if self.bulk_active:
            txt(s, f"backup running... {len(self.bulk)} messages so far", 13, AMBER, (740, 354))
        for w in self.w_sys:
            w.draw(s, self)
        y = 616
        for ln in CHECKLIST:
            if ln.startswith("# "):
                txt(s, ln[2:], 13, AMBER, (50, y), bold=True)
                y += 20
            else:
                txt(s, ln, 13, TEXT, (64, y))
                y += 18

    def draw_monitor(self, s):
        panel(s, (30, 124, 900, 660), "Everything sent and received")
        panel(s, (950, 210, 420, 574), "Connection")
        for w in self.w_mon:
            w.draw(s, self)
        txt(s, "IN = from the VM200   OUT = from this app", 12, DIM, (960, 180))
        evs = list(self.events)[-34:]
        t0 = self.events[0][0] if self.events else 0
        for i, (t, d, line) in enumerate(evs):
            txt(s, f"{t - t0:8.2f}  {d} {line}"[:100], 13, AMBER if d == "OUT" else (150, 240, 190), (50, 156 + i * 17))
        if not evs:
            txt(s, "Nothing yet. Choose MIDI OUT and MIDI IN, then press FIND VM200.", 14, DIM, (50, 160))
        for i, (k, v) in enumerate([("VM200 found", "yes" if self.found else "no reply yet"), ("messages in", self.counts["in"]),
                                    ("messages out", self.counts["out"]), ("VM200 parameter messages", self.counts["fostex"])]):
            txt(s, k, 12, AMBER_DIM, (970, 250 + i * 40))
            txt(s, str(v), 14, TEXT, (970, 266 + i * 40))

    def draw_help(self, s):
        panel(s, (30, 124, 1340, 656), "Help")
        s.set_clip(pygame.Rect(46, 148, 1308, 612))
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
            if self.bulk_active and not self.q and time.time() - self.bulk_last > 6:
                self.finish_backup()
            self.draw()
            pygame.display.flip()
            clock.tick(60)
            frames += 1
            if shot and frames >= 3:
                pygame.image.save(self.screen, shot)
                break
        self.close_all()
        pygame.quit()


def main():
    shot = tab = None
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]
    tab = 0
    if "--tab" in sys.argv:
        tab = int(sys.argv[sys.argv.index("--tab") + 1])
    App(headless=bool(shot), tab=tab).run(shot)


if __name__ == "__main__":
    main()
