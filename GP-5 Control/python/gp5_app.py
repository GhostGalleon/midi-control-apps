#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
GP-5 CONTROL  -  MIDI control surface for the Valeton GP-5 multi-effects pedal.

Run:      python gp5_app.py
Needs:    pip install pygame

What the GP-5 manual documents over MIDI (its "MIDI Control Information List"): CC0 = patch 00-99, CC7 = patch volume
(0-100), CC22 / CC23 = bank down / up, CC24 / CC25 = patch down / up, CC29 / CC30 = song-list patch down / up,
CC48-57 = module on/off (NR, PRE, DST, N>S, AMP, CAB, EQ, MOD, DLY, RVB; 0-63 off, 64-127 on), CC58 = tuner on/off,
CC69 = CTL (the footswitch's CTL function).  This app drives all of those, keeps patch names, set lists and scenes.
"""
import json
import os
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
def _writable_base():
    """Folder beside the app if we can write there, else the user's own app-data folder."""
    try:
        t = BASE / ".gp5_write_test"
        t.write_text("x")
        t.unlink()
        return BASE
    except Exception:
        d = Path(os.environ.get("APPDATA") or Path.home()) / "GP5Control"
        d.mkdir(parents=True, exist_ok=True)
        return d
DATA_BASE = _writable_base()
SETTINGS_FILE = DATA_BASE / "gp5_settings.json"
DATA_FILE = DATA_BASE / "gp5_data.json"

W, H = 1400, 860
GAP = 0.004            # seconds between queued messages
DEFAULT_CH = 0         # MIDI channel 1 (0-based); the manual does not give a channel, so it is selectable

# ---- palette: graphite + mint (re-coloured to red at the end of the build) ----------------------
AMBER = (235, 148, 71)        # accent (name kept so the shared drawing code works)
AMBER_DIM = (230, 124, 31)
ORANGE = (255, 140, 30)
SOLO_RED = (235, 40, 40)
TEXT = (246, 240, 234)
DIM = (172, 150, 150)
PANEL = (49, 39, 39)
EDGE = (150, 122, 122)
LCD_BG = (11, 9, 9)
LCD_FG = (255, 146, 51)
LCD_GLOW = (130, 69, 14)
GREEN = (235, 148, 71)
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
    pygame.draw.line(surf, (248, 243, 238), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (248, 244, 240), (r.x + 16, r.y + 8), bold=True)
    if title:
        pygame.draw.line(surf, (72, 56, 56), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
        pygame.draw.line(surf, (235, 148, 71), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (28, 22, 22), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (232, 216, 202), (sx, r.bottom - 12), 4, 1)
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
        pygame.draw.line(surf, (244, 237, 230), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
        pygame.draw.rect(surf, AMBER if hover else BTN_EDGE, r, 1, border_radius=6)
        s = label or self.label
        txt(surf, s, fit_size(s, r.w - 14, (15, 14, 13, 12, 11), bold=True),
            (255, 255, 255) if hover else TEXT, r.center, "center", bold=True)




PEDAL_LOOK = {   # name: (body, label plate, label text, knob fill, knob dot, knob layout)
    "NR":  ((205, 205, 205), (150, 150, 150), (60, 60, 60), (30, 30, 30), (240, 240, 240), [(0, -1)]),
    "PRE": ((150, 50, 200), (115, 30, 160), (255, 255, 255), (245, 245, 245), (150, 50, 200), [(0, -1)]),
    "DST": ((215, 55, 75), (175, 35, 55), (255, 255, 255), (245, 245, 245), (215, 55, 75), [(-1, -1), (1, -1), (0, 0)]),
    "N>S": ((115, 55, 205), (80, 35, 160), (255, 255, 255), (245, 245, 245), (115, 55, 205), [(-1, -1), (0, -1), (1, -1), (-0.5, 0), (0.5, 0)]),
    "MOD": ((45, 185, 215), (30, 150, 180), (255, 255, 255), (245, 245, 245), (45, 185, 215), [(-1, -1), (1, -1), (0, 0)]),
    "DLY": ((215, 50, 130), (170, 35, 105), (255, 255, 255), (245, 245, 245), (215, 50, 130), [(-1, -1), (1, -1), (0, 0)]),
    "RVB": ((40, 105, 210), (25, 75, 160), (255, 255, 255), (245, 245, 245), (40, 105, 210), [(-0.6, -1), (0.6, -1), (-0.6, 0), (0.6, 0)]),
    "EQ":  ((215, 170, 105), (185, 135, 75), (40, 25, 10), None, None, []),
}


def _mix(c, f, bg=(24, 22, 20)):
    return tuple(int(bg[i] + (c[i] - bg[i]) * f) for i in range(3))


def draw_module_icon(s, name, cx, cy, f):
    """A little stomp-box / amp / cab picture for one module; f = brightness (1 on, <1 dimmed)."""
    m = lambda c: _mix(c, f)
    if name == "AMP":
        r = pygame.Rect(0, 0, 66, 44); r.center = (cx, cy)
        pygame.draw.rect(s, m((35, 33, 30)), r, border_radius=6)
        face = r.inflate(-8, -8)
        pygame.draw.rect(s, m((200, 165, 110)), face, border_radius=4)
        pygame.draw.rect(s, m((90, 70, 45)), face, 1, border_radius=4)
        txt(s, "AMP", 15, m((255, 255, 255)), (cx, cy - 8), "center", bold=True)
        pygame.draw.rect(s, m((210, 40, 40)), (face.x + 4, face.bottom - 12, 5, 7))
        for k in range(3):
            pygame.draw.circle(s, m((60, 50, 40)), (cx - 6 + k * 14, face.bottom - 8), 5)
            pygame.draw.circle(s, m((235, 215, 170)), (cx - 6 + k * 14, face.bottom - 8), 3)
        return
    if name == "CAB":
        r = pygame.Rect(0, 0, 58, 58); r.center = (cx, cy)
        pygame.draw.rect(s, m((38, 38, 38)), r, border_radius=5)
        inner = r.inflate(-12, -12)
        pygame.draw.rect(s, m((80, 80, 80)), inner, 2, border_radius=3)
        pygame.draw.rect(s, m((55, 55, 55)), inner.inflate(-4, -4))
        for dx in (-1, 1):
            for dy in (-1, 1):
                pygame.draw.rect(s, m((170, 170, 170)), (cx + dx * 26 - 3, cy + dy * 26 - 3, 6, 6))
        txt(s, "CAB", 15, m((255, 255, 255)), (cx, cy), "center", bold=True)
        return
    if name == "TUNER":
        body, plate = (60, 175, 110), (35, 130, 80)
        r = pygame.Rect(0, 0, 40, 58); r.center = (cx, cy)
        pygame.draw.rect(s, m(body), r, border_radius=6)
        pygame.draw.arc(s, m((255, 255, 255)), (cx - 14, cy - 24, 28, 28), 0.5, 2.64, 3)
        pygame.draw.line(s, m((255, 255, 255)), (cx, cy - 8), (cx + 5, cy - 18), 2)
        pl = pygame.Rect(r.x, r.bottom - 22, r.w, 22)
        pygame.draw.rect(s, m(plate), pl, border_bottom_left_radius=6, border_bottom_right_radius=6)
        txt(s, "TUNE", 11, m((255, 255, 255)), pl.center, "center", bold=True)
        return
    look = PEDAL_LOOK.get(name)
    if not look:
        return
    body, plate, ltxt, kfill, kdot, knobs = look
    r = pygame.Rect(0, 0, 40, 58); r.center = (cx, cy)
    pygame.draw.rect(s, m((200, 200, 200)), (r.x - 3, r.y + 25, 3, 4))            # little jack stubs
    pygame.draw.rect(s, m((200, 200, 200)), (r.right, r.y + 25, 3, 4))
    pygame.draw.rect(s, m(body), r, border_radius=6)
    pl = pygame.Rect(r.x, r.bottom - 22, r.w, 22)
    pygame.draw.rect(s, m(plate), pl, border_bottom_left_radius=6, border_bottom_right_radius=6)
    txt(s, name, 12, m(ltxt), pl.center, "center", bold=True)
    if name == "EQ":
        for k in range(4):
            x = r.x + 7 + k * 9
            pygame.draw.line(s, m((110, 75, 35)), (x, r.y + 5), (x, r.y + 32), 2)
            pygame.draw.rect(s, m((245, 235, 215)), (x - 3, r.y + 9 + (k * 7) % 15, 7, 4))
        return
    for kx, ky in knobs:
        px, py = int(cx + kx * 9), int(r.y + 14 + ky * 6 + (6 if ky >= 0 else 0))
        pygame.draw.circle(s, m(kfill), (px, py), 5)
        pygame.draw.circle(s, m(kdot), (px, py - 1), 2)


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
# FACTS FROM THE OWNER'S MANUAL  ("MIDI Control Information List", page 40)
# =============================================================================
MODULES = [("NR", 48, "Noise reduction"), ("PRE", 49, "Compressor / boost"), ("DST", 50, "Distortion"), ("N>S", 51, "SnapTone"),
           ("AMP", 52, "Amp model"), ("CAB", 53, "Cabinet / IR"), ("EQ", 54, "Equaliser"), ("MOD", 55, "Modulation"),
           ("DLY", 56, "Delay"), ("RVB", 57, "Reverb")]
CC_TUNER, CC_CTL, CC_VOL, CC_PATCH = 58, 69, 7, 0
CC_NAMES = {0: "patch select (0-99)", 7: "patch volume (0-100)", 22: "bank down", 23: "bank up", 24: "patch down", 25: "patch up",
            29: "song-list patch down", 30: "song-list patch up", 58: "tuner on/off", 69: "CTL"}
for _n, _cc, _d in MODULES:
    CC_NAMES[_cc] = f"{_n} module on/off"
MAX_SET = 28


def describe(m):
    k, ch = m[0] & 0xF0, (m[0] & 0x0F) + 1
    if k == 0xB0:
        nm = CC_NAMES.get(m[1], "")
        extra = ""
        if 48 <= m[1] <= 58:
            extra = "  ON" if m[2] >= 64 else "  OFF"
        return f"ch{ch:>2}  CC{m[1]:>3} = {m[2]:>3}  {nm}{extra}"
    if k == 0xC0:
        return f"ch{ch:>2}  program change {m[1]}  (patch {m[1]:02d})"
    return " ".join(f"{b:02X}" for b in m)
# =============================================================================
# THE APP
# =============================================================================
TABS = ["PLAY", "SET LIST", "SCENES", "MONITOR", "HELP"]

HELP_TEXT = [
    ("H", "Getting connected"),
    ("T", "The GP-5 appears to your computer as a USB device (the USB-C port). Pick it as MIDI OUT at the top. If it also shows up as a MIDI input, pick that as "
          "MIDI IN to see messages from it. If no GP-5 appears in the lists, its MIDI over USB may need the Valeton driver or the Valeton Suite software; "
          "the manual only documents the messages, not the connection, so this is the first thing to check on the real unit."),
    ("T", "The manual does not state a MIDI channel. The app sends on channel 1 by default; change it with CHANNEL on the PLAY tab. If nothing responds, try the other channels."),
    ("H", "PLAY"),
    ("T", "Click a patch number (00-99) to select it. Right-click a patch to give it a name here; the names are only kept in this app, the GP-5 cannot send its patch names. "
          "PATCH SELECT chooses how patches are chosen: CC0 (the way the manual documents) or a standard Program Change."),
    ("T", "BANK - / + move ten patches (the tens digit), PATCH - / + move one, SONG - / + step through the set list stored inside the GP-5 (A-Z mode)."),
    ("T", "PATCH VOLUME sends CC7, 0-100. The module buttons turn NR, PRE, DST, N>S, AMP, CAB, EQ, MOD, DLY and RVB on or off for the current patch (CC48-57), TUNER is CC58, "
          "and CTL presses the footswitch's CTL function (CC69). The GP-5 does not report its module states, so a button shows the last state this app sent; "
          "'?' means not set yet. ALL ON / ALL OFF send all ten."),
    ("H", "SET LIST"),
    ("T", "Your own running order, kept in this app: ADD CURRENT puts the selected patch on the list, then NEXT and PREVIOUS step through it and select each patch on the GP-5 "
          "(you can also click an entry). SAVE and LOAD keep several lists in gp5_data.json."),
    ("H", "SCENES"),
    ("T", "A scene is a patch, its volume and the on/off state of the ten modules. SAVE CURRENT stores what the PLAY tab shows; RECALL sends it all to the GP-5 "
          "(the patch first, then the rest after a short pause). Patch edits that are not on/off or volume can only be made in the Valeton Suite app."),
    ("H", "What the app cannot do"),
    ("T", "Effect types, amp models and all other parameters are not available over MIDI on the GP-5. Edit those in the Valeton Suite phone or desktop app."),
]


class App:
    def __init__(self, headless=False, tab=0):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("GP-5 CONTROL")
        self.tab, self.running, self.popup, self.drag = tab, True, None, None
        self._hits, self.hits = [], []
        self.events = deque(maxlen=3000)
        self.counts = {"in": 0, "out": 0}
        self.flash_out = self.flash_in = 0.0
        self.status = "Choose MIDI OUT at the top, then click a patch."
        self.status_t = time.time()
        self.out = self.inp = None
        self.out_lock = threading.Lock()
        self.settings, self.data = {}, {}
        for f, attr in ((SETTINGS_FILE, "settings"), (DATA_FILE, "data")):
            try:
                setattr(self, attr, json.loads(f.read_text()))
            except Exception:
                pass
        self.names = self.data.setdefault("names", {})
        self.setlists = self.data.setdefault("setlists", {})
        self.scenes = self.data.setdefault("scenes", {})
        self.pstate = self.data.setdefault("patchstate", {})      # per-patch memory: module on/off + volume
        self.autorestore = bool(self.settings.get("autorestore", True))
        self.patch = clamp(int(self.settings.get("patch", 0)), 0, 99)
        self.vol = clamp(int(self.settings.get("vol", 80)), 0, 100)
        self.ch = clamp(int(self.settings.get("ch", DEFAULT_CH)), 0, 15)
        self.pmode = self.settings.get("pmode", "CC0") if self.settings.get("pmode") in ("CC0", "PC") else "CC0"
        self.mods = {cc: None for _n, cc, _d in MODULES}
        self.mods[CC_TUNER] = None
        self.work = []                     # working set list (patch numbers)
        self.work_name, self.work_sel = "", -1
        self.sel_scene = None
        self.scene_scroll = 0
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
        self.help_lines, self.help_scroll, self.mon_scroll = [], 0, 0
        for kind, text in HELP_TEXT:
            if kind == "H":
                self.help_lines.append(("H", text))
            else:
                for ln in wrap(text, 15, 1240):
                    self.help_lines.append(("T", ln))

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
        base = key.rsplit("#", 1)[0]               # same device, but the #n order moved (re-plugged / other USB port)
        for i, k in (self.out_devs if kind == "out" else self.in_devs):
            if k.rsplit("#", 1)[0] == base:
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
        self.popup = Popup("MIDI OUT (to the GP-5's MIDI IN)" if kind == "out" else "MIDI IN (from the GP-5's MIDI OUT)",
                           opts, self.settings.get(kind + "_name"), pick)

    def save_settings(self):
        self.settings.update(patch=self.patch, vol=self.vol, ch=self.ch, pmode=self.pmode, autorestore=self.autorestore)
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception as ex:
            self.set_status(f"Could not save settings to {SETTINGS_FILE}: {ex}")

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
            if isinstance(item[0], float):
                time.sleep(item[0])
                continue
            self.write_msg(item[0])
            time.sleep(GAP)

    def send(self, msgs, group=None):
        with self.qlock:
            if group is not None:
                self.q = deque(m for m in self.q if m[1] != group)
            for m in msgs:
                self.q.append((m, group))

    # ---- GP-5 messages -----------------------------------------------------------------------
    def pname(self, p):
        return self.names.get(str(p), "")

    def cc(self, num, val, group=None):
        self.send([[0xB0 | self.ch, num, val]], group=group)

    def remember(self):
        """Store the modules / volume now showing against the current patch."""
        st = self.pstate.setdefault(str(self.patch), {})
        known = {str(k): bool(v) for k, v in self.mods.items() if v is not None}
        if known:
            st.setdefault("mods", {}).update(known)
        st["vol"] = self.vol
        self.save_data()

    def patch_changed(self, send):
        """New patch selected: show (and, if send, re-send after the GP-5 has loaded it) what we remembered for it."""
        st = self.pstate.get(str(self.patch)) or {}
        self.mods = {k: None for k in self.mods}
        for k, v in (st.get("mods") or {}).items():
            if int(k) in self.mods:
                self.mods[int(k)] = bool(v)
        if "vol" in st:
            self.vol = clamp(int(st["vol"]), 0, 100)
        if send and self.autorestore and st:
            self.send([0.35])                              # let the GP-5 finish loading the patch
            for k, v in (st.get("mods") or {}).items():
                self.cc(int(k), 127 if v else 0)
            if "vol" in st:
                self.cc(CC_VOL, self.vol)

    def set_patch(self, p, _b=1):
        self.patch = clamp(p, 0, 99)
        if self.pmode == "PC":
            self.send([[0xC0 | self.ch, self.patch]], group="pc")
        else:
            self.cc(CC_PATCH, self.patch, group="pc")
        self.patch_changed(True)
        self.save_settings()

    def step_patch(self, d, _b=1):
        self.patch = (self.patch + d) % 100
        self.cc(25 if d > 0 else 24, 127)
        self.patch_changed(True)
        self.save_settings()

    def step_bank(self, d, _b=1):
        self.patch = (self.patch + 10 * d) % 100
        self.cc(23 if d > 0 else 22, 127)
        self.patch_changed(True)
        self.save_settings()

    def step_song(self, d, _b=1):
        self.cc(30 if d > 0 else 29, 127)
        self.mods = {k: None for k in self.mods}
        self.set_status("Stepped the GP-5's own song list. The patch shown here may differ until you select a patch.")

    def set_vol(self, v):
        self.vol = clamp(int(v), 0, 100)
        self.cc(CC_VOL, self.vol, group="vol")

    def toggle_mod(self, cc, _b=1):
        on = not bool(self.mods.get(cc))
        self.mods[cc] = on
        self.cc(cc, 127 if on else 0)
        self.remember()

    def all_mods(self, on, _b=1):
        for _n, cc, _d in MODULES:
            self.mods[cc] = on
            self.cc(cc, 127 if on else 0)
        self.remember()

    def ctl(self, _b=1):
        self.cc(CC_CTL, 127)

    def toggle_autorestore(self, _b=1):
        self.autorestore = not self.autorestore
        self.save_settings()
        self.set_status("Saved modules will be re-sent when you change patch." if self.autorestore
                        else "Patch changes now just show what the GP-5 does itself.")

    def set_ch(self, d, _b=1):
        self.ch = clamp(self.ch + d, 0, 15)
        self.save_settings()
        self.set_status(f"Sending on MIDI channel {self.ch + 1}.")

    def set_pmode(self, m, _b=1):
        self.pmode = m
        self.save_settings()

    def sound_off(self, _b=1):
        self.send([[0xB0 | self.ch, 120, 0], [0xB0 | self.ch, 123, 0]])
        self.set_status("All Sound Off / All Notes Off sent.")

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
        b0 = data[0]
        if b0 < 0x80 or b0 >= 0xF0:
            return
        kind = b0 & 0xF0
        m = [b0, data[1]] if kind in (0xC0, 0xD0) else [b0, data[1], data[2]]
        self.flash_in = time.time()
        self.counts["in"] += 1
        self.log("IN ", describe(m))
        if kind == 0xC0 and m[1] < 100:
            self.patch = m[1]
            self.patch_changed(False)
        elif kind == 0xB0:
            if m[1] == CC_PATCH and m[2] < 100:
                self.patch = m[2]
                self.patch_changed(False)
            elif m[1] == CC_VOL:
                self.vol = clamp(m[2], 0, 100)
            elif m[1] in self.mods:
                self.mods[m[1]] = m[2] >= 64
                self.remember()

    # ---- names, set lists, scenes ----------------------------------------------------------
    def save_data(self):
        try:
            DATA_FILE.write_text(json.dumps(self.data, indent=1))
        except Exception as ex:
            self.set_status(f"Could not save: {ex}")

    def ask_name_patch(self, p):
        def done(name):
            name = re.sub(r"[^\w .+#&'-]", "", name).strip()[:16]
            if name:
                self.names[str(p)] = name
            else:
                self.names.pop(str(p), None)
            self.save_data()
        self.popup = TextPrompt(f"Name for patch {p:02d}", "Name (kept in this app only; empty clears it):", self.pname(p), done)

    def add_current(self, _b=1):
        if len(self.work) >= MAX_SET:
            self.set_status(f"The set list holds {MAX_SET} patches.")
            return
        self.work.append(self.patch)
        self.work_sel = len(self.work) - 1

    def remove_sel(self, _b=1):
        if 0 <= self.work_sel < len(self.work):
            del self.work[self.work_sel]
            self.work_sel = min(self.work_sel, len(self.work) - 1)

    def move_sel(self, d, _b=1):
        i, j = self.work_sel, self.work_sel + d
        if 0 <= i < len(self.work) and 0 <= j < len(self.work):
            self.work[i], self.work[j] = self.work[j], self.work[i]
            self.work_sel = j

    def go_set(self, i):
        if 0 <= i < len(self.work):
            self.work_sel = i
            self.set_patch(self.work[i])

    def step_set(self, d, _b=1):
        if not self.work:
            self.set_status("The set list is empty. Select a patch and press ADD CURRENT.")
            return
        self.go_set((self.work_sel + d) % len(self.work) if self.work_sel >= 0 else (0 if d > 0 else len(self.work) - 1))

    def ask_save_set(self, _b=1):
        if not self.work:
            return

        def done(name):
            name = re.sub(r"[^\w .+#&'-]", "", name).strip()[:32]
            if name:
                self.setlists[name] = list(self.work)
                self.work_name = name
                self.save_data()
                self.set_status(f"Saved set list '{name}'.")
        self.popup = TextPrompt("Save set list", "Name for this set list:", self.work_name, done)

    def pick_set(self, _b=1):
        if not self.setlists:
            self.set_status("No saved set lists yet.")
            return

        def pick(name):
            self.work, self.work_name, self.work_sel = list(self.setlists[name]), name, -1
        self.popup = Popup("Saved set lists", [(n, n) for n in sorted(self.setlists, key=str.lower)], self.work_name, pick)

    def ask_save_scene(self, _b=1):
        def done(name):
            name = re.sub(r"[^\w .+#&'-]", "", name).strip()[:32]
            if not name:
                return
            self.scenes[name] = {"patch": self.patch, "vol": self.vol,
                                 "mods": {str(k): v for k, v in self.mods.items() if v is not None}}
            self.sel_scene = name
            self.save_data()
            self.set_status(f"Saved scene '{name}'.")
        self.popup = TextPrompt("Save scene", "Name for this scene:", self.sel_scene or "", done)

    def recall_scene(self, _b=1):
        sc = self.scenes.get(self.sel_scene)
        if not sc:
            return
        self.set_patch(int(sc["patch"]))
        self.send([0.35])                                  # let the GP-5 finish loading the patch
        self.vol = clamp(int(sc["vol"]), 0, 100)
        self.cc(CC_VOL, self.vol)
        self.mods = {k: None for k in self.mods}
        for k, v in sc["mods"].items():
            self.mods[int(k)] = bool(v)
            self.cc(int(k), 127 if v else 0)
        self.set_status(f"Recalled '{self.sel_scene}'.")

    def ask_delete_scene(self, _b=1):
        name = self.sel_scene
        if name in self.scenes:
            def go():
                self.scenes.pop(name, None)
                self.sel_scene = None
                self.save_data()
            self.popup = Info(f"Delete scene '{name}'?", ["This cannot be undone."], [("DELETE", go), ("CANCEL", None)])
    # ---- immediate-mode widgets ------------------------------------------------------------------
    def add_hit(self, rect, kind, a=None, b=None):
        self._hits.append((pygame.Rect(rect), kind, a, b))

    def btn(self, rect, label, cb, active=False, enabled=True, size=14):
        r = pygame.Rect(rect)
        hover = enabled and r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, (30, 23, 23), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(self.screen, (163, 86, 18) if active else (BTN_HOT if hover else BTN), r, border_radius=6)
        pygame.draw.rect(self.screen, AMBER if (active or hover) else BTN_EDGE, r, 1, border_radius=6)
        col = (255, 255, 255) if (active or hover) else (TEXT if enabled else (112, 100, 100))
        txt(self.screen, label, fit_size(label, r.w - 10, (size, 13, 12, 11, 10), bold=True), col, r.center, "center", bold=True)
        if enabled:
            self.add_hit(r, "btn", cb)

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
        panel(s, (30, 130, 760, 660), "Patches   (click to select, right-click to name)")
        for p in range(100):
            r, c = divmod(p, 10)
            rect = pygame.Rect(46 + c * 74, 168 + r * 50, 70, 46)
            sel = p == self.patch
            hover = rect.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, (163, 86, 18) if sel else (BTN_HOT if hover else BTN), rect, border_radius=6)
            pygame.draw.rect(s, AMBER if (sel or hover) else BTN_EDGE, rect, 1, border_radius=6)
            txt(s, f"{p:02d}", 15, (255, 255, 255) if sel else TEXT, (rect.x + 7, rect.y + 5), bold=True)
            nm = self.pname(p)
            if nm:
                txt(s, nm, fit_size(nm, 62, (11, 10, 9), bold=False), (255, 255, 255) if sel else DIM, (rect.x + 7, rect.y + 28))
            self.add_hit(rect, "cell", p, None)
        for i, (lab, cb) in enumerate((("BANK -", lambda b: self.step_bank(-1)), ("BANK +", lambda b: self.step_bank(1)),
                                       ("PATCH -", lambda b: self.step_patch(-1)), ("PATCH +", lambda b: self.step_patch(1)),
                                       ("SONG -", lambda b: self.step_song(-1)), ("SONG +", lambda b: self.step_song(1)))):
            self.btn((46 + i * 123, 678, 115, 40), lab, cb, size=14)
        txt(s, "BANK = ten patches.  SONG = the GP-5's own A-Z song list.", 12, DIM, (50, 732))
        txt(s, "Patch names are kept in this app only (right-click a patch).", 12, DIM, (50, 752))

        panel(s, (810, 130, 560, 660), "Current patch")
        pygame.draw.rect(s, LCD_BG, (830, 168, 520, 100), border_radius=8)
        pygame.draw.rect(s, BTN_EDGE, (830, 168, 520, 100), 1, border_radius=8)
        txt(s, f"{self.patch:02d}", 64, LCD_FG, (850, 178), bold=True)
        nm = self.pname(self.patch) or "(no name)"
        txt(s, nm, fit_size(nm, 330, (30, 26, 22, 18), bold=True), LCD_FG if self.pname(self.patch) else DIM, (960, 205), bold=True)
        txt(s, "PATCH VOLUME  (CC7)", 12, DIM, (830, 284), bold=True)
        txt(s, str(self.vol), 16, AMBER, (1350, 284), "topright", bold=True)
        self.slider((830, 308, 520, 26), self.vol, 100, "vol")
        txt(s, "MODULES  (CC48-57) - on / off for the current patch", 12, DIM, (830, 354), bold=True)
        hover_desc = ""
        for i, (name, cc, desc) in enumerate(MODULES + [("TUNER", CC_TUNER, "Tuner")]):
            c, r = i % 6, i // 6
            rect = pygame.Rect(830 + c * 87, 376 + r * 100, 82, 94)
            st = self.mods.get(cc)
            hov = rect.collidepoint(pygame.mouse.get_pos())
            if hov:
                hover_desc = desc
            pygame.draw.rect(s, (52, 26, 14) if st else ((46, 40, 36) if hov else (30, 27, 24)), rect, border_radius=8)
            pygame.draw.rect(s, AMBER if st else (BTN_EDGE if not hov else DIM), rect, 2 if st else 1, border_radius=8)
            draw_module_icon(s, name, rect.centerx, rect.y + 38, 1.0 if st else (0.55 if st is None else 0.32))
            led(s, (rect.x + 12, rect.y + 11), bool(st), AMBER)
            if st is None:
                txt(s, "?", 12, DIM, (rect.right - 10, rect.y + 11), "center", bold=True)
            txt(s, "ON" if st else ("OFF" if st is False else ""), 12, AMBER if st else DIM,
                (rect.centerx, rect.bottom - 11), "center", bold=True)
            self.add_hit(rect, "btn", lambda b, cc=cc: self.toggle_mod(cc))
        if hover_desc:
            txt(s, hover_desc, 12, DIM, (1350, 354), "topright")
        self.btn((830, 620, 126, 36), "ALL ON", lambda b: self.all_mods(True), size=14)
        self.btn((962, 620, 126, 36), "ALL OFF", lambda b: self.all_mods(False), size=14)
        self.btn((1094, 620, 256, 36), "CTL  (CC69)", self.ctl, size=15)
        txt(s, "CHANNEL", 12, DIM, (830, 676), bold=True)
        self.btn((900, 664, 36, 32), "<", lambda b: self.set_ch(-1), size=15)
        txt(s, str(self.ch + 1), 18, LCD_FG, (958, 681), "center", bold=True)
        self.btn((980, 664, 36, 32), ">", lambda b: self.set_ch(1), size=15)
        txt(s, "PATCH SELECT", 12, DIM, (1060, 676), bold=True)
        self.btn((1170, 664, 80, 32), "CC0", lambda b: self.set_pmode("CC0"), active=self.pmode == "CC0", size=14)
        self.btn((1256, 664, 94, 32), "PROG CHG", lambda b: self.set_pmode("PC"), active=self.pmode == "PC", size=12)
        self.btn((830, 708, 520, 32), "RESTORE SAVED MODULES ON PATCH CHANGE:  " + ("ON" if self.autorestore else "OFF"),
                 self.toggle_autorestore, size=13)
        txt(s, "Modules and volume are remembered per patch. '?' = not set yet.", 11, DIM, (830, 748))
        txt(s, "Effect types and amps are edited in the Valeton Suite app.", 11, DIM, (830, 766))

    def draw_setlist(self, s):
        panel(s, (30, 130, 820, 660), f"Set list   {self.work_name}   ({len(self.work)} / {MAX_SET})")
        if not self.work:
            txt(s, "Empty. Select a patch on the PLAY tab, then press ADD CURRENT here.", 14, DIM, (54, 176))
        for i, p in enumerate(self.work):
            c, r = divmod(i, 14)
            rect = pygame.Rect(46 + c * 400, 168 + r * 40, 390, 36)
            sel = i == self.work_sel
            now = p == self.patch
            pygame.draw.rect(s, AMBER_DIM if sel else (BTN_HOT if rect.collidepoint(pygame.mouse.get_pos()) else BTN), rect, border_radius=5)
            pygame.draw.rect(s, AMBER if sel else BTN_EDGE, rect, 1, border_radius=5)
            txt(s, f"{i + 1:>2}", 14, DIM, (rect.x + 10, rect.centery), "midleft", bold=True)
            txt(s, f"{p:02d}", 16, (255, 255, 255) if sel else TEXT, (rect.x + 46, rect.centery), "midleft", bold=True)
            nm = self.pname(p)
            if nm:
                txt(s, nm, 14, TEXT, (rect.x + 90, rect.centery), "midleft")
            if now:
                led(s, (rect.right - 20, rect.centery), True, AMBER)
            self.add_hit(rect, "setrow", i)
        panel(s, (870, 130, 500, 660), "Controls")
        self.btn((890, 170, 460, 80), "NEXT  >", lambda b: self.step_set(1), size=28)
        self.btn((890, 262, 460, 56), "<  PREVIOUS", lambda b: self.step_set(-1), size=20)
        self.btn((890, 340, 226, 38), "ADD CURRENT", self.add_current, size=14)
        self.btn((1124, 340, 226, 38), "REMOVE", self.remove_sel, enabled=self.work_sel >= 0, size=14)
        self.btn((890, 386, 226, 38), "MOVE UP", lambda b: self.move_sel(-1), enabled=self.work_sel > 0, size=14)
        self.btn((1124, 386, 226, 38), "MOVE DOWN", lambda b: self.move_sel(1), enabled=0 <= self.work_sel < len(self.work) - 1, size=14)
        self.btn((890, 432, 226, 38), "CLEAR LIST", lambda b: (self.work.clear(), setattr(self, "work_sel", -1), setattr(self, "work_name", "")), size=14)
        self.btn((890, 500, 226, 38), "SAVE LIST...", self.ask_save_set, enabled=bool(self.work), size=14)
        self.btn((1124, 500, 226, 38), "LOAD LIST...", self.pick_set, size=14)
        txt(s, f"Now:  {self.patch:02d}  {self.pname(self.patch)}", 16, LCD_FG, (890, 580), bold=True)
        txt(s, "The dot marks the patch that is selected now.", 12, DIM, (890, 612))
        txt(s, "Set lists are kept in gp5_data.json next to the app.", 12, DIM, (890, 632))

    def draw_scenes(self, s):
        names = sorted(self.scenes, key=str.lower)
        panel(s, (30, 130, 660, 660), f"Scenes   ({len(names)})")
        if not names:
            txt(s, "None yet. Set up the PLAY tab, then press SAVE CURRENT.", 14, DIM, (54, 176))
        self.scene_scroll = clamp(self.scene_scroll, 0, max(0, len(names) - 15))
        for n, name in enumerate(names[self.scene_scroll:self.scene_scroll + 15]):
            rect = pygame.Rect(46, 168 + n * 40, 628, 36)
            sel = name == self.sel_scene
            pygame.draw.rect(s, AMBER_DIM if sel else (BTN_HOT if rect.collidepoint(pygame.mouse.get_pos()) else BTN), rect, border_radius=5)
            pygame.draw.rect(s, AMBER if sel else BTN_EDGE, rect, 1, border_radius=5)
            txt(s, name, 15, (255, 255, 255) if sel else TEXT, (rect.x + 12, rect.centery), "midleft", bold=True)
            sc = self.scenes[name]
            txt(s, f"patch {int(sc['patch']):02d}   vol {int(sc['vol'])}", 12, DIM, (rect.right - 12, rect.centery), "midright")
            self.add_hit(rect, "scene", name)
        panel(s, (710, 130, 660, 660), "Scene")
        sc = self.scenes.get(self.sel_scene)
        if sc:
            txt(s, self.sel_scene, 22, AMBER, (734, 172), bold=True)
            txt(s, f"Patch {int(sc['patch']):02d}   {self.pname(int(sc['patch']))}", 18, LCD_FG, (734, 210), bold=True)
            txt(s, f"Patch volume {int(sc['vol'])}", 16, TEXT, (734, 246))
            for i, (name, cc, _d) in enumerate(MODULES):
                st = sc["mods"].get(str(cc))
                x, y = 734 + (i % 5) * 120, 290 + (i // 5) * 50
                led(s, (x + 10, y + 12), bool(st), AMBER)
                txt(s, name, 14, TEXT if st else DIM, (x + 26, y + 4), bold=True)
                txt(s, "on" if st else ("off" if st is False else "-"), 11, DIM, (x + 26, y + 24))
        else:
            txt(s, "Select a scene on the left.", 14, DIM, (734, 176))
        self.btn((734, 440, 300, 44), "SAVE CURRENT...", self.ask_save_scene, size=16)
        self.btn((1044, 440, 300, 44), "RECALL", self.recall_scene, enabled=bool(sc) and bool(self.out), size=16)
        self.btn((734, 494, 300, 40), "DELETE", self.ask_delete_scene, enabled=bool(sc), size=14)
        txt(s, "A scene = patch + patch volume + the on/off state of the ten modules.", 12, DIM, (734, 560))
        txt(s, "Only modules you had set ('on' or 'off') are stored; '-' ones are left alone.", 12, DIM, (734, 580))
        txt(s, "Recall sends the patch first, waits a moment, then the rest.", 12, DIM, (734, 600))

    def draw_monitor(self, s):
        panel(s, (30, 130, 1340, 660), "MIDI monitor  (newest at the bottom)")
        self.btn((1230, 126, 120, 26), "CLEAR", lambda b: self.events.clear(), size=12)
        evs = list(self.events)[-27:]
        if not evs:
            txt(s, "Nothing yet. Click a patch, or choose MIDI OUT at the top.", 14, DIM, (54, 170))
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
        txt(s, "GP-5", 40, AMBER, (30, 4), bold=True)
        txt(s, "M I D I   C O N T R O L", 11, AMBER_DIM, (32, 52))
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
        txt(s, f"PATCH {self.patch:02d}", 20, LCD_FG, (1262, 20), bold=True)
        txt(s, f"ch {self.ch + 1}", 12, DIM, (1262, 48))
        for i, name in enumerate(TABS):
            self.btn((30 + i * 150, 88, 140, 32), name, lambda b, i=i: setattr(self, "tab", i), active=(i == self.tab), size=14)

    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        self._hits = []
        self.draw_header(s)
        [self.draw_play, self.draw_setlist, self.draw_scenes, self.draw_monitor, self.draw_help][self.tab](s)
        panel(s, (30, 800, 1340, 50), "")
        age = time.time() - self.status_t
        for i, ln in enumerate(wrap(self.status, 13, 1300)[:2]):
            txt(s, ln, 13, TEXT if age < 40 else DIM, (46, 808 + i * 17))
        self.hits = self._hits
        if self.popup:
            self.popup.draw(s, self)

    # ---- events ------------------------------------------------------------------------------------------
    def slider_set(self, kind, rect, hi, vertical, pos):
        t = (pos[0] - rect.x) / float(rect.w)
        self.set_vol(int(round(clamp(t, 0.0, 1.0) * hi)))
        self.remember()
        self.save_settings()

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
            elif self.tab == 0 and e.key in (pygame.K_RIGHT, pygame.K_PAGEDOWN, pygame.K_DOWN):
                self.step_patch(1) if e.key != pygame.K_PAGEDOWN else self.step_bank(1)
            elif self.tab == 0 and e.key in (pygame.K_LEFT, pygame.K_PAGEUP, pygame.K_UP):
                self.step_patch(-1) if e.key != pygame.K_PAGEUP else self.step_bank(-1)
            elif self.tab == 1 and e.key in (pygame.K_RIGHT, pygame.K_SPACE, pygame.K_DOWN):
                self.step_set(1)
            elif self.tab == 1 and e.key in (pygame.K_LEFT, pygame.K_UP):
                self.step_set(-1)
            return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            for r, kind, a, b in reversed(self.hits):
                if not r.collidepoint(e.pos):
                    continue
                if e.button == 3:
                    if kind == "cell":
                        self.ask_name_patch(a)
                        return
                    continue
                if kind == "btn":
                    a(1)
                elif kind == "cell":
                    self.set_patch(a)
                elif kind == "setrow":
                    self.go_set(a)
                elif kind == "scene":
                    self.sel_scene = a
                elif kind == "slider":
                    rect, hi, vert = b
                    self.drag = (a, rect, hi, vert)
                    self.slider_set(a, rect, hi, vert, e.pos)
                return
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = None
        elif e.type == pygame.MOUSEMOTION and self.drag:
            self.slider_set(*self.drag, e.pos)
        elif e.type == pygame.MOUSEWHEEL and self.tab == 2:
            self.scene_scroll -= e.y
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
        self.save_settings()
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
