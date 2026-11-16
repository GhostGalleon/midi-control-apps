#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
PRO VS MINI CONTROL  -  MIDI control surface for the Behringer Pro VS Mini.

Run:      python provs_app.py
Needs:    pip install pygame

Every control sends a MIDI Control Change on the channel you pick (top right).  Controller numbers
come from the community MIDI list for the Pro VS Mini (midi.guide).  The Mini cannot be read back,
so the app shows its own values: press SEND ALL (or load a sound from the LIBRARY) to make the
synth match the screen.  If your Mini sends CCs when you turn its own knobs, the app follows them.
"""
import json
import math
import random
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
else:
    BASE = _data_root(Path(__file__).resolve().parent)
SETTINGS_FILE = BASE / "provs_settings.json"
SOUNDS_FILE = BASE / "provs_sounds.json"

W, H = 1400, 860
GAP = 0.010            # seconds between any two MIDI messages

# ---- palette: graphite + mint (re-coloured to green at the end of the build) ----------------------
AMBER = (71, 235, 93)        # accent (name kept so the shared drawing code works)
AMBER_DIM = (31, 230, 57)
ORANGE = (255, 140, 30)
SOLO_RED = (235, 40, 40)
TEXT = (234, 246, 236)
DIM = (150, 172, 150)
PANEL = (36, 52, 36)
EDGE = (121, 152, 120)
LCD_BG = (8, 12, 8)
LCD_FG = (51, 255, 78)
LCD_GLOW = (14, 130, 30)
GREEN = (71, 235, 93)
RED = (255, 90, 80)
BTN = (48, 68, 48)
BTN_HOT = (74, 106, 74)
BTN_EDGE = (117, 152, 116)
WELL = (17, 24, 16)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))
WAVES = ['Twang', 'Clav', 'Xylophone', 'Slippery Slope', 'Enharmonic Square', 'Xylophone 2', 'Mellow Square', 'Chaos 1', 'Chaos 2', 'Chaos 3', 'Chaos 4', 'Bright Square', 'Hollow', 'BP Square', 'Ski Slope 1', 'Rasp', 'Peal', 'Fuzz', 'Floss', 'Excite', 'Awaken', 'Energize', 'BP Twang', 'Complex Pulse', 'Mellow Square 2', 'Harmonic Square', 'Thin Ramp', 'Xylophone 3', 'Thin Square', 'Harmonic Ramp', 'Rouse', 'Waken', 'Sine', 'Sawtooth', 'Square', 'Dome 1', 'Dome 2', 'Mellow Dome', 'Dome 3', 'Dome 4', 'Rasp 1', 'Rasp 2', 'Rasp 3', 'Rasp 4', 'Dome 5', 'HP Saw', 'High BP Saw', 'High BP Square', 'Vocal', 'Squeeze box', 'Pulse', 'Limp Saw', 'Spark Wave 1', 'Spark Wave 2', 'Spark Wave 3', 'Mid Wave 1', 'Cacophonous Buzz 1', 'Mid Buzz 1', 'Dome 6', 'Soft Dome 3', 'Dome 7', 'Spark Wave 4', 'Spark Wave 5', 'Soft Dome 4', 'Dome 8', 'Organ', 'Spark Wave 5', 'Soft Wave 1', 'Soft Wave 2', 'Spark Wave 7', 'Reed', 'Soft Wave 3', 'Soft Wave 4', 'Saxophone', 'Soft Wave 5', 'Trashy Wave 1', 'Trashy Wave 2', 'Trashy Wave 3', 'Trashy Wave 4', 'Trashy Wave 5', 'Trashy Wave 6', 'Jaw Harp 1', 'Jaw Harp 2', 'Soft Wave 6', 'Thin 1', 'Thin 2', 'Spark Wave 8', 'Spark Wave 9', 'Spark Wave 10', 'Soft Dome 5', 'Soft Dome 6', 'Cacophonous Buzz 2', 'Thin 3', 'Spark Wave 11', 'Soft Dome 7', 'Soft Wave 7', 'Sine 2', "Sync'd Sine", 'Trashy Wave 7', 'Twingle Pad', 'Dome 9', 'Thin 4', 'Thin 5', 'Trashy Wave 8', 'Trashy Wave 9', 'Trashy Wave 10', 'Trashy Wave 11', 'Trashy Wave 12', 'Trashy Wave 13', 'Spark Wave 12', 'Spark Wave 13', 'Trashy Wave 14', 'Trashy Wave 15', 'Rainbow', 'Soft Dome 8', 'Trashy Wave 15', 'Soothing 1', 'Soothing 2', 'Space Wave', '5th Rasp', 'Octave Wave', 'Triangle + Overtones', 'Pulse + Overtones', 'Rising Square', 'Soft Wave 8', 'Cacophonous Buzz 3', 'Silence', 'White Noise']
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


def metal(w, h, top=(70, 94, 70), bot=(22, 31, 21)):
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


def rounded_metal(surf, rect, radius=10, top=(70, 94, 70), bot=(22, 31, 21)):
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
    pygame.draw.rect(surf, (28, 40, 27), r, 1, border_radius=10)
    pygame.draw.rect(surf, EDGE, r.inflate(-2, -2), 1, border_radius=9)
    pygame.draw.line(surf, (238, 248, 239), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (240, 248, 241), (r.x + 16, r.y + 8), bold=True)
    if title:
        pygame.draw.line(surf, (53, 76, 52), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
        pygame.draw.line(surf, (71, 235, 93), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (21, 29, 20), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (202, 232, 206), (sx, r.bottom - 12), 4, 1)
        pygame.draw.line(surf, (149, 176, 148), (sx - 2, r.bottom - 14), (sx + 2, r.bottom - 10))


def lcd(surf, rect, text, max_size=24):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, (21, 29, 20), r.inflate(6, 6), border_radius=8)
    pygame.draw.rect(surf, (191, 210, 191), r.inflate(6, 6), 1, border_radius=8)
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
        pygame.draw.circle(surf, (47, 64, 47), pos, 4)
        pygame.draw.circle(surf, (159, 188, 158), pos, 4, 1)


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
        pygame.draw.rect(surf, (22, 31, 22), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
        pygame.draw.line(surf, (230, 244, 232), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
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
                pygame.draw.rect(surf, (125, 164, 125), r, border_radius=5)
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


class WavePopup(Popup):
    """All 128 waves in four columns (click one to pick it)."""

    def __init__(self, title, current, on_pick):
        self.title, self.current, self.on_pick = title, current, on_pick
        self.options = [(i, f"{i:>3}  {WAVES[i]}") for i in range(128)]
        rows, colw, rh = 32, 308, 21
        self.panel = pygame.Rect(0, 0, 4 * colw + 24, rows * rh + 62)
        self.panel.center = (W // 2, H // 2 - 14)
        self.items = []
        for i, (val, lab) in enumerate(self.options):
            c, rw = divmod(i, rows)
            self.items.append((pygame.Rect(self.panel.x + 12 + c * colw, self.panel.y + 46 + rw * rh, colw - 4, rh - 1), val, lab))

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
                pygame.draw.rect(surf, (125, 164, 125), r, border_radius=4)
            if val == self.current:
                pygame.draw.rect(surf, AMBER_DIM, r, 1, border_radius=4)
            txt(surf, lab, 14, AMBER if val == self.current else TEXT, (r.x + 8, r.centery), "midleft")
# =============================================================================
# PARAMETER TABLE  (CC numbers from the Pro VS Mini MIDI list; ranges as published)
# =============================================================================
PARAMS = {}        # cc -> dict(name, hi, default)


def P(cc, name, hi, default):
    PARAMS[cc] = {"name": name, "hi": hi, "default": default}


for _i, _L in enumerate("ABCD"):
    P(24 + _i, f"Voice {_L} wave", 127, [33, 126, 126, 126][_i])
    P(111 + _i, f"Voice {_L} fine", 99, 50)
    P(115 + _i, f"Voice {_L} coarse", 99, 50)
for _i, (_n, _d) in enumerate((("attack", 0), ("decay", 50), ("sustain", 80), ("release", 20))):
    P(81 + _i, "Amp env " + _n, 99, _d)
    P(85 + _i, "Filter env " + _n, 99, _d)
P(74, "Filter cutoff", 99, 99)
P(71, "Filter resonance", 99, 0)
P(47, "Filter env amount", 127, 0)
P(54, "LFO 1 waveform", 127, 12)
P(70, "LFO 1 amount", 99, 0)
P(72, "LFO 1 rate", 99, 30)
P(56, "LFO 1 destination", 127, 0)
P(55, "LFO 2 waveform", 127, 12)
P(28, "LFO 2 amount", 99, 0)
P(73, "LFO 2 rate", 99, 30)
P(9, "FX engine", 127, 21)
P(91, "FX depth", 99, 50)
P(92, "FX rate", 99, 50)
P(1, "Modulation", 127, 0)
P(5, "Portamento time", 127, 0)

LFO_WAVES = [("TRI", 12), ("SQR", 38), ("SAW", 64), ("RAMP", 90), ("S&H", 115)]
FX_ENGINES = [("CHORUS", 21), ("ENSEMBLE", 64), ("REVERB", 106)]


def lfo_zone(v):
    return 0 if v < 26 else 1 if v < 51 else 2 if v < 77 else 3 if v < 102 else 4


def fx_zone(v):
    return 0 if v < 43 else 1 if v < 85 else 2


def wave_name(v):
    return WAVES[clamp(int(v), 0, 127)]


def describe(m):
    """Plain-English text for a short MIDI message."""
    k, ch = m[0] & 0xF0, (m[0] & 0x0F) + 1
    if k == 0xB0:
        p = PARAMS.get(m[1])
        extra = ""
        if p:
            if m[1] in (24, 25, 26, 27):
                extra = f"  ({wave_name(m[2])})"
            elif m[1] in (54, 55):
                extra = f"  ({LFO_WAVES[lfo_zone(m[2])][0]})"
            elif m[1] == 9:
                extra = f"  ({FX_ENGINES[fx_zone(m[2])][0]})"
        return f"ch{ch:>2}  CC{m[1]:>3} = {m[2]:>3}  {p['name'] if p else '(not in the list)'}{extra}"
    if k == 0x90:
        return f"ch{ch:>2}  note {m[1]} on  velocity {m[2]}"
    if k == 0x80:
        return f"ch{ch:>2}  note {m[1]} off"
    if k == 0xC0:
        return f"ch{ch:>2}  program change {m[1]}"
    return " ".join(f"{b:02X}" for b in m)
# =============================================================================
# THE APP
# =============================================================================
TABS = ["SOUND", "LIBRARY", "MONITOR", "HELP"]

HELP_TEXT = [
    ("H", "Getting connected"),
    ("T", "Connect the Pro VS Mini's MIDI IN to a MIDI OUT of your interface (USB MIDI works too). Click MIDI OUT at the top and pick it. "
          "If you also connect the Mini's MIDI OUT to an interface input, pick that as MIDI IN and the app will follow the Mini's own knobs, "
          "if it sends them."),
    ("T", "Set CH (top right) to the channel your Mini listens on."),
    ("H", "What the screen shows"),
    ("T", "The Pro VS Mini cannot be asked for its settings, so the app can only show what it last sent. Press SEND ALL, or load a sound from the "
          "LIBRARY, to make the synth match the screen. After that every knob you move is sent as it moves."),
    ("T", "Drag a knob up or down. Hold SHIFT for fine steps. Use the mouse wheel over a knob for single steps. Double-click a knob to put it "
          "back to its starting value (the tuning knobs have no known centre, so they ignore this)."),
    ("H", "Voices"),
    ("T", "Each of the four voices has a wave (0 to 127, named from the published list), a fine tune and a coarse tune. Click the wave name for the full "
          "list; use the arrows to step through it."),
    ("H", "Sounds"),
    ("T", "LIBRARY keeps your sounds in provs_sounds.json next to the app. SAVE stores every value on screen; LOAD sends them all to the Mini. "
          "RANDOM SOUND makes a new patch from envelope, filter, LFO and FX values and the four waves (tuning, modulation, portamento and "
          "LFO destination are left alone)."),
    ("H", "Notes"),
    ("T", "LFO destination (CC56) has a published range of 0 to 127 but no list of what the values mean, so it is a plain 0 to 127 knob. "
          "Try it with the monitor open and listen. The waveform and FX engine buttons send a value in the middle of each published range."),
    ("T", "Values shown in a dim colour have not been sent yet: they are only the app's starting values, not necessarily what the Mini is set to."),
    ("T", "The keyboard at the bottom plays notes on the same channel so you can hear a sound while you tweak it."),
]


class App:
    def __init__(self, headless=False, tab=0):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("PRO VS MINI CONTROL")
        self.tab, self.running, self.popup, self.drag = tab, True, None, None
        self._hits, self.hits = [], []
        self.v = {cc: p["default"] for cc, p in PARAMS.items()}
        self.touched = set()
        self.events = deque(maxlen=3000)
        self.counts = {"in": 0, "out": 0}
        self.flash_out = self.flash_in = 0.0
        self.status = "Choose MIDI OUT, then play with the knobs. Press SEND ALL to make the Mini match the screen."
        self.status_t = time.time()
        self.out = self.inp = None
        self.out_lock = threading.Lock()
        self.settings, self.sounds = {}, {}
        for f, attr in ((SETTINGS_FILE, "settings"), (SOUNDS_FILE, "sounds")):
            try:
                setattr(self, attr, json.loads(f.read_text()))
            except Exception:
                pass
        self.ch = clamp(int(self.settings.get("channel", 1)), 1, 16)
        self.octave = clamp(int(self.settings.get("octave", 3)), 0, 6)
        self.sel_sound, self.sound_scroll, self.mon_scroll, self.help_scroll = None, 0, 0, 0
        self.held = None
        self.last_click = {}
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
                for ln in wrap(text, 15, 1240):
                    self.help_lines.append(("T", ln))

    def _make_bg(self):
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

    # ---- model ---------------------------------------------------------------------------
    def get(self, cc):
        return self.v.get(cc, PARAMS[cc]["default"])

    def set(self, cc, val, send=True):
        val = clamp(int(val), 0, PARAMS[cc]["hi"])
        if send and self.v.get(cc) == val and cc in self.touched:
            return
        self.v[cc] = val
        self.touched.add(cc)
        if send:
            self.send([[0xB0 | (self.ch - 1), cc, val]], group=cc)

    def set_status(self, s):
        self.status, self.status_t = s, time.time()

    def log(self, d, text):
        self.events.append((time.time(), d, text))

    def send_all(self, _b=1):
        msgs = [[0xB0 | (self.ch - 1), cc, self.v[cc]] for cc in sorted(PARAMS)]
        self.touched |= set(PARAMS)
        self.send(msgs)
        self.set_status(f"Sent all {len(msgs)} values on channel {self.ch}.")

    def panic(self, _b=1):
        self.send([[0xB0 | (self.ch - 1), 123, 0], [0xB0 | (self.ch - 1), 120, 0]])
        self.set_status("All notes off sent.")

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
            dev = pgmidi.Output(i) if kind == "out" else pgmidi.Input(i, 4096)
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
        self.popup = Popup("MIDI OUT (to the Pro VS Mini's MIDI IN)" if kind == "out" else "MIDI IN (from the Mini's MIDI OUT)",
                           opts, self.settings.get(kind + "_name"), pick)

    def save_settings(self):
        self.settings["channel"], self.settings["octave"] = self.ch, self.octave
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    def set_channel(self, d):
        self.ch = clamp(self.ch + d, 1, 16)
        self.save_settings()

    # ---- sending ---------------------------------------------------------------------------
    def write_msg(self, m):
        with self.out_lock:
            if self.out:
                try:
                    self.out.write_short(*m)
                except Exception as ex:
                    self.status = f"MIDI write error: {ex}"
        self.flash_out = time.time()
        self.counts["out"] += 1
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

    def note(self, n, on):
        self.send([[(0x90 if on else 0x80) | (self.ch - 1), n, 100 if on else 0]])

    # ---- incoming MIDI -----------------------------------------------------------------------
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
            if b0 < 0x80 or b0 >= 0xF0:
                continue
            kind = b0 & 0xF0
            if kind not in (0xB0, 0xC0, 0x90, 0x80):
                continue
            m = [b0, data[1]] if kind == 0xC0 else [b0, data[1], data[2]]
            self.flash_in = time.time()
            self.counts["in"] += 1
            self.log("IN ", describe(m))
            if kind == 0xB0 and (b0 & 0x0F) == self.ch - 1 and m[1] in PARAMS:
                self.set(m[1], m[2], send=False)

    # ---- sounds ----------------------------------------------------------------------------------
    def save_sounds(self):
        try:
            SOUNDS_FILE.write_text(json.dumps(self.sounds, indent=1))
        except Exception as ex:
            self.set_status(f"Could not save the sound list: {ex}")

    def ask_save_sound(self, _b=1):
        def done(name):
            name = re.sub(r"[^\w .+#&'-]", "", name).strip()[:32]
            if not name:
                return
            self.sounds[name] = {str(cc): self.v[cc] for cc in sorted(PARAMS)}
            self.sel_sound = name
            self.save_sounds()
            self.set_status(f"Saved '{name}'.")
        self.popup = TextPrompt("Save sound", "Name for this sound (an existing name is replaced):", self.sel_sound or "", done)

    def load_sound(self, _b=1):
        s = self.sounds.get(self.sel_sound)
        if not s:
            return
        for cc in PARAMS:
            if str(cc) in s:
                self.v[cc] = clamp(int(s[str(cc)]), 0, PARAMS[cc]["hi"])
        self.send_all()
        self.set_status(f"Loaded '{self.sel_sound}' and sent every value to the Mini.")

    def delete_sound(self, _b=1):
        name = self.sel_sound
        if not name:
            return

        def go():
            self.sounds.pop(name, None)
            self.sel_sound = None
            self.save_sounds()
        self.popup = Info("Delete sound?", [f"'{name}' will be removed from your sound list."], [("DELETE", go), ("CANCEL", None)])

    def random_sound(self, _b=1):
        r = random.Random()
        g = lambda a, b: r.randint(a, b)
        v = self.v
        v[24] = g(0, 125)
        for cc in [25, 26, 27]:
            v[cc] = 126 if r.random() < 0.45 else g(0, 125)
        for base, (a, d, s, rl) in ((81, ((0, 30), (10, 80), (20, 99), (5, 60))), (85, ((0, 40), (10, 80), (10, 90), (5, 60)))):
            for i, (lo, hi) in enumerate((a, d, s, rl)):
                v[base + i] = g(lo, hi)
        v[74], v[71], v[47] = g(35, 99), g(0, 60), g(0, 100)
        v[54], v[55] = r.choice(LFO_WAVES)[1], r.choice(LFO_WAVES)[1]
        v[70], v[72], v[28], v[73] = g(0, 30), g(0, 99), g(0, 30), g(0, 99)
        v[9] = r.choice(FX_ENGINES)[1]
        v[91], v[92] = g(20, 80), g(20, 80)
        self.send_all()
        self.set_status("Random sound made and sent. Like it? SAVE it in the LIBRARY.")

    # ---- immediate-mode widgets ------------------------------------------------------------------
    def hit(self, rect, kind, a=None, b=None):
        self._hits.append((pygame.Rect(rect), kind, a, b))

    def btn(self, rect, label, cb, active=False, enabled=True, size=14):
        r = pygame.Rect(rect)
        hover = enabled and r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, (22, 31, 22), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(self.screen, (18, 163, 37) if active else (BTN_HOT if hover else BTN), r, border_radius=6)
        pygame.draw.rect(self.screen, AMBER if (active or hover) else BTN_EDGE, r, 1, border_radius=6)
        col = (255, 255, 255) if (active or hover) else (TEXT if enabled else (100, 112, 100))
        txt(self.screen, label, fit_size(label, r.w - 10, (size, 13, 12, 11, 10), bold=True), col, r.center, "center", bold=True)
        if enabled:
            self.hit(r, "btn", cb)

    def knob(self, cc, cx, cy, label, big=True, reset=True):
        s = self.screen
        p = PARAMS[cc]
        v = self.get(cc)
        R = 24 if big else 17
        t = v / float(p["hi"])
        hot = self.drag == cc or math.hypot(pygame.mouse.get_pos()[0] - cx, pygame.mouse.get_pos()[1] - cy) <= R + 9
        pygame.draw.circle(s, (10, 14, 10), (cx, cy), R + 7)
        pygame.draw.circle(s, AMBER if hot else (131, 160, 130), (cx, cy), R + 7, 1)
        pygame.draw.lines(s, (31, 45, 31), False, arc_pts(cx, cy, R + 3, 135, 405), 4)
        a1 = 135 + 270 * t
        if t > 0.001:
            pygame.draw.lines(s, AMBER if cc in self.touched else AMBER_DIM, False, arc_pts(cx, cy, R + 3, 135, a1), 4)
        pygame.draw.circle(s, (96, 106, 96), (cx, cy), R)
        pygame.draw.circle(s, (150, 162, 150), (cx, cy), R - 2)
        pygame.draw.circle(s, (190, 202, 190), (cx - 3, cy - 4), max(2, R - 12))
        ang = math.radians(a1)
        pygame.draw.line(s, (13, 19, 13), (cx + math.cos(ang) * R * 0.3, cy + math.sin(ang) * R * 0.3),
                         (cx + math.cos(ang) * R * 0.88, cy + math.sin(ang) * R * 0.88), 3)
        txt(s, label, 11, DIM, (cx, cy + R + 14), "center")
        txt(s, str(v), 14, AMBER if cc in self.touched else DIM, (cx, cy + R + 30), "center", bold=True)
        self.hit((cx - R - 9, cy - R - 9, 2 * R + 18, 2 * R + 40), "knob", cc, reset)

    def wave_selector(self, cc, rect, label):
        r = pygame.Rect(rect)
        v = self.get(cc)
        txt(self.screen, label, 12, DIM, (r.x + 2, r.y - 17), bold=True)
        self.btn((r.x, r.y, 30, r.h), "<", lambda b, cc=cc: self.set(cc, self.get(cc) - 1), size=15)
        self.btn((r.right - 30, r.y, 30, r.h), ">", lambda b, cc=cc: self.set(cc, self.get(cc) + 1), size=15)
        mid = pygame.Rect(r.x + 34, r.y, r.w - 68, r.h)
        hover = mid.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, (8, 12, 8), mid, border_radius=5)
        pygame.draw.rect(self.screen, AMBER if hover else BTN_EDGE, mid, 1, border_radius=5)
        name = f"{v:>3}  {wave_name(v)}"
        txt(self.screen, name, fit_size(name, mid.w - 12, (15, 14, 13, 12, 11), bold=True), LCD_FG if cc in self.touched else AMBER_DIM,
            mid.center, "center", bold=True)
        self.hit(mid, "btn", lambda b, cc=cc, label=label: self.pick_wave(cc, label))

    def pick_wave(self, cc, label):
        self.popup = WavePopup(f"{label} wave", self.get(cc), lambda v, cc=cc: self.set(cc, v))

    def env_plot(self, rect, base, title=""):
        s = self.screen
        r = pygame.Rect(rect)
        pygame.draw.rect(s, (8, 12, 8), r, border_radius=5)
        pygame.draw.rect(s, BTN_EDGE, r, 1, border_radius=5)
        a, d, sus, rel = (self.get(base + i) / 99.0 for i in range(4))
        hold = 0.25
        tot = a + d + hold + rel + 0.02
        x0, x1, yb, yt = r.x + 8, r.right - 8, r.bottom - 8, r.y + 8
        sx = lambda t: x0 + (x1 - x0) * t / tot
        ys = yb - (yb - yt) * sus
        pts = [(x0, yb), (sx(a), yt), (sx(a + d), ys), (sx(a + d + hold), ys), (sx(a + d + hold + rel), yb)]
        fill = pygame.Surface(r.size, pygame.SRCALPHA)
        pygame.draw.polygon(fill, (*LCD_FG, 40), [(x - r.x, y - r.y) for x, y in pts])
        s.blit(fill, r.topleft)
        pygame.draw.lines(s, LCD_FG, False, pts, 2)
        for x, y in pts[1:4]:
            pygame.draw.circle(s, (255, 255, 255), (int(x), int(y)), 3)

    def filter_plot(self, rect):
        s = self.screen
        r = pygame.Rect(rect)
        pygame.draw.rect(s, (8, 12, 8), r, border_radius=5)
        pygame.draw.rect(s, BTN_EDGE, r, 1, border_radius=5)
        fc = 0.08 + 0.84 * self.get(74) / 99.0
        res = self.get(71) / 99.0
        x0, x1, yb, yt = r.x + 8, r.right - 8, r.bottom - 8, r.y + 14
        pts = []
        for i in range(0, 101):
            f = i / 100.0
            if f <= fc:
                y = 0.55 + 0.45 * res * math.exp(-((f - fc) / 0.05) ** 2)
            else:
                y = max(0.0, 0.55 * math.exp(-(f - fc) * 14))
                y += 0.45 * res * math.exp(-((f - fc) / 0.05) ** 2)
            pts.append((x0 + (x1 - x0) * f, yb - (yb - yt) * min(1.0, y)))
        fill = pygame.Surface(r.size, pygame.SRCALPHA)
        pygame.draw.polygon(fill, (*LCD_FG, 40), [(x - r.x, y - r.y) for x, y in pts] + [(x1 - r.x, yb - r.y), (x0 - r.x, yb - r.y)])
        s.blit(fill, r.topleft)
        pygame.draw.lines(s, LCD_FG, False, pts, 2)

    def choice_row(self, cc, y, x0, labels, zone_fn, w, gap, h=28):
        z = zone_fn(self.get(cc))
        for i, (lab, val) in enumerate(labels):
            self.btn((x0 + i * (w + gap), y, w, h), lab, lambda b, cc=cc, val=val: self.set(cc, val),
                     active=(cc in self.touched and i == z) or (cc not in self.touched and False), size=13)

    # ---- keyboard --------------------------------------------------------------------------------------
    def draw_keyboard(self, s):
        base = (self.octave + 1) * 12
        x0, y0, kw, kh = 130, 738, 54, 52
        white = [0, 2, 4, 5, 7, 9, 11]
        keys = []
        for i in range(21):
            octv, k = divmod(i, 7)
            keys.append((x0 + i * kw, base + octv * 12 + white[k]))
        blacks = []
        for i in range(21):
            octv, k = divmod(i, 7)
            if k in (0, 1, 3, 4, 5) and i < 20:
                blacks.append((x0 + i * kw + kw - 16, base + octv * 12 + white[k] + 1))
        for x, n in keys:
            r = pygame.Rect(x, y0, kw - 2, kh)
            down = self.held == n
            pygame.draw.rect(s, (71, 235, 93) if down else (226, 232, 226), r, border_radius=3)
            pygame.draw.rect(s, (60, 70, 60), r, 1, border_radius=3)
            if n % 12 == 0:
                txt(s, f"C{n // 12 - 1}", 10, (60, 70, 60), (r.centerx, r.bottom - 12), "center")
        for x, n in blacks:
            r = pygame.Rect(x, y0, 30, 32)
            down = self.held == n
            pygame.draw.rect(s, (40, 231, 65) if down else (22, 26, 22), r, border_radius=3)
            pygame.draw.rect(s, (90, 100, 90), r, 1, border_radius=3)
        for x, n in keys:
            self.hit((x, y0 + 32, kw - 2, kh - 32), "key", n)
        for x, n in blacks:
            self.hit((x, y0, 30, 32), "key", n)
        self.btn((30, 738, 90, 24), "OCTAVE -", lambda b: self.set_octave(-1), size=12)
        self.btn((30, 766, 90, 24), "OCTAVE +", lambda b: self.set_octave(1), size=12)
        txt(s, f"C{self.octave} - B{self.octave + 2}", 13, DIM, (x0 + 21 * kw + 14, y0 + 20))

    def set_octave(self, d):
        self.octave = clamp(self.octave + d, 0, 6)
        self.save_settings()

    # ---- events ------------------------------------------------------------------------------------------
    def handle(self, e):
        if e.type == pygame.QUIT:
            self.running = False
            return
        if self.popup:
            self.popup.handle(e, self)
            return
        if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
            self.running = False
            return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for r, kind, a, b in reversed(self.hits):
                if not r.collidepoint(e.pos):
                    continue
                if kind == "btn":
                    a(1)
                elif kind == "knob":
                    now = time.time()
                    if b and a not in (111, 112, 113, 114, 115, 116, 117, 118) and now - self.last_click.get(a, 0) < 0.35:
                        self.set(a, PARAMS[a]["default"])
                        self.last_click[a] = 0
                    else:
                        self.last_click[a] = now
                        self.drag, self.acc = a, float(self.get(a))
                elif kind == "key":
                    self.held = a
                    self.note(a, True)
                elif kind == "sound":
                    self.sel_sound = a
                return
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = None
            if self.held is not None:
                self.note(self.held, False)
                self.held = None
        elif e.type == pygame.MOUSEMOTION and self.drag is not None:
            hi = PARAMS[self.drag]["hi"]
            fine = pygame.key.get_mods() & pygame.KMOD_SHIFT
            self.acc = clamp(self.acc - e.rel[1] * hi / 130.0 * (0.25 if fine else 1.0), 0, hi)
            self.set(self.drag, int(round(self.acc)))
        elif e.type == pygame.MOUSEWHEEL:
            pos = pygame.mouse.get_pos()
            for r, kind, a, b in reversed(self.hits):
                if kind == "knob" and r.collidepoint(pos):
                    step = 5 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 1
                    self.set(a, self.get(a) + e.y * step)
                    return
            if self.tab == 1:
                self.sound_scroll = max(0, min(max(0, len(self.sounds) - 19), self.sound_scroll - e.y))
            elif self.tab == 3:
                self.help_scroll = max(0, min(max(0, len(self.help_lines) * 24 + 60 - 610), self.help_scroll - e.y * 40))

    # ---- drawing ----------------------------------------------------------------------------------------------
    def draw_header(self, s):
        txt(s, "PRO VS MINI", 38, AMBER, (30, 8), bold=True)
        txt(s, "S Y N T H   C O N T R O L", 11, AMBER_DIM, (32, 52))
        out = (self.settings.get("out_name") or "none  (click to choose)").replace("#0", "")
        inn = (self.settings.get("in_name") or "none  (optional)").replace("#0", "")
        self.btn((310, 8, 520, 30), f"MIDI OUT:  {out}"[:62], lambda b: self.choose_device("out"), size=13)
        self.btn((310, 42, 520, 30), f"MIDI IN:   {inn}"[:62], lambda b: self.choose_device("in"), size=13)
        txt(s, "CH", 13, DIM, (856, 16), bold=True)
        self.btn((884, 8, 34, 30), "-", lambda b: self.set_channel(-1), size=16)
        txt(s, str(self.ch), 20, AMBER, (936, 24), "center", bold=True)
        self.btn((954, 8, 34, 30), "+", lambda b: self.set_channel(1), size=16)
        self.btn((856, 42, 132, 30), "NOTES OFF", self.panic, size=13)
        self.btn((1010, 8, 190, 64), "SEND ALL", self.send_all, size=18)
        now = time.time()
        for i, (lab, t) in enumerate((("OUT", self.flash_out), ("IN", self.flash_in))):
            on = now - t < 0.15
            led(s, (1230 + i * 70, 28), on, AMBER)
            txt(s, lab, 12, TEXT if on else DIM, (1244 + i * 70, 21), bold=True)
        txt(s, f"{self.counts['out']} sent   {self.counts['in']} received", 11, DIM, (1222, 48))
        for i, name in enumerate(TABS):
            self.btn((30 + i * 150, 88, 140, 32), name, lambda b, i=i: setattr(self, "tab", i), active=(i == self.tab), size=14)

    def draw_sound(self, s):
        # voices
        panel(s, (30, 130, 980, 200), "Voices  (wave, fine tune, coarse tune)")
        for i, L in enumerate("ABCD"):
            x0 = 42 + i * 242
            self.wave_selector(24 + i, (x0, 190, 222, 34), f"VOICE {L}")
            self.knob(111 + i, x0 + 62, 266, "FINE", big=False, reset=False)
            self.knob(115 + i, x0 + 168, 266, "COARSE", big=False, reset=False)
            if i:
                pygame.draw.line(s, (60, 76, 60), (x0 - 6, 168), (x0 - 6, 314))
        panel(s, (1030, 130, 340, 200), "Performance")
        self.knob(1, 1110, 232, "MODULATION")
        self.knob(5, 1290, 232, "PORTAMENTO")
        # envelopes + filter
        for x, base, title in ((30, 81, "Amp envelope"), (450, 85, "Filter envelope")):
            panel(s, (x, 340, 410, 214), title)
            self.env_plot((x + 16, 372, 378, 64), base)
            for i, n in enumerate(("ATTACK", "DECAY", "SUSTAIN", "RELEASE")):
                self.knob(base + i, x + 56 + i * 99, 486, n)
        panel(s, (870, 340, 500, 214), "Filter")
        self.filter_plot((886, 372, 468, 64))
        self.knob(74, 960, 486, "CUTOFF")
        self.knob(71, 1120, 486, "RESONANCE")
        self.knob(47, 1280, 486, "ENV AMOUNT")
        # lfos + fx
        panel(s, (30, 564, 660, 164), "LFO 1")
        self.choice_row(54, 596, 46, LFO_WAVES, lfo_zone, 98, 8)
        self.knob(70, 120, 664, "AMOUNT")
        self.knob(72, 280, 664, "RATE")
        self.knob(56, 440, 664, "DESTINATION")
        panel(s, (700, 564, 330, 164), "LFO 2")
        self.choice_row(55, 596, 712, LFO_WAVES, lfo_zone, 58, 5)
        self.knob(28, 790, 664, "AMOUNT")
        self.knob(73, 940, 664, "RATE")
        panel(s, (1040, 564, 330, 164), "FX")
        self.choice_row(9, 596, 1052, FX_ENGINES, fx_zone, 98, 8)
        self.knob(91, 1130, 664, "DEPTH")
        self.knob(92, 1280, 664, "RATE")
        self.draw_keyboard(s)

    def draw_library(self, s):
        panel(s, (30, 130, 700, 600), "Your sounds")
        names = sorted(self.sounds, key=str.lower)
        self.sound_scroll = clamp(self.sound_scroll, 0, max(0, len(names) - 19))
        if not names:
            txt(s, "No sounds yet. Set a sound on the SOUND tab, then press SAVE CURRENT AS...", 14, DIM, (54, 170))
        for r, n in enumerate(names[self.sound_scroll:self.sound_scroll + 19]):
            rect = pygame.Rect(46, 160 + r * 29, 668, 27)
            sel = n == self.sel_sound
            pygame.draw.rect(s, AMBER_DIM if sel else (BTN_HOT if rect.collidepoint(pygame.mouse.get_pos()) else BTN), rect, border_radius=5)
            pygame.draw.rect(s, AMBER if sel else BTN_EDGE, rect, 1, border_radius=5)
            txt(s, n, 15, (255, 255, 255) if sel else TEXT, (rect.x + 12, rect.centery), "midleft", bold=True)
            self.hit(rect, "sound", n)
        if len(names) > 19:
            txt(s, f"{self.sound_scroll + 1}-{min(len(names), self.sound_scroll + 19)} of {len(names)}  (wheel)", 11, DIM, (380, 712), "center")
        panel(s, (750, 130, 620, 600), "Actions")
        self.btn((770, 170, 580, 44), "SAVE CURRENT AS...", self.ask_save_sound, size=16)
        sel = self.sel_sound in self.sounds
        self.btn((770, 226, 280, 44), "LOAD AND SEND", self.load_sound, enabled=sel, size=15)
        self.btn((1070, 226, 280, 44), "DELETE", self.delete_sound, enabled=sel, size=15)
        self.btn((770, 282, 280, 44), "RANDOM SOUND", self.random_sound, size=15)
        self.btn((1070, 282, 280, 44), "SEND ALL", self.send_all, size=15)
        y = 350
        if sel:
            sd = self.sounds[self.sel_sound]
            txt(s, self.sel_sound, 20, AMBER, (770, y), bold=True)
            g = lambda cc: int(sd.get(str(cc), PARAMS[cc]["default"]))
            lines = [f"Voice {L}:  {wave_name(g(24 + i))}   fine {g(111 + i)}  coarse {g(115 + i)}" for i, L in enumerate("ABCD")]
            lines += ["", f"Amp env     A {g(81)}  D {g(82)}  S {g(83)}  R {g(84)}",
                      f"Filter env  A {g(85)}  D {g(86)}  S {g(87)}  R {g(88)}",
                      f"Filter      cutoff {g(74)}  resonance {g(71)}  env amount {g(47)}", "",
                      f"LFO 1  {LFO_WAVES[lfo_zone(g(54))][0]}  amount {g(70)}  rate {g(72)}  dest {g(56)}",
                      f"LFO 2  {LFO_WAVES[lfo_zone(g(55))][0]}  amount {g(28)}  rate {g(73)}",
                      f"FX  {FX_ENGINES[fx_zone(g(9))][0]}  depth {g(91)}  rate {g(92)}", "",
                      f"Modulation {g(1)}   Portamento {g(5)}"]
            for i, ln in enumerate(lines):
                txt(s, ln, 14, TEXT, (770, y + 36 + i * 22))
        else:
            txt(s, "Click a sound in the list.", 14, DIM, (770, y))
        txt(s, "Sounds are stored in provs_sounds.json next to this app.", 12, DIM, (770, 696))

    def draw_monitor(self, s):
        panel(s, (30, 130, 1340, 600), "MIDI monitor  (newest at the bottom)")
        self.btn((1230, 126, 120, 26), "CLEAR", lambda b: self.events.clear(), size=12)
        evs = list(self.events)[-26:]
        if not evs:
            txt(s, "Nothing yet. Move a knob, or choose MIDI OUT at the top.", 14, DIM, (54, 170))
        for i, (t, d, text) in enumerate(evs):
            col = AMBER if d == "OUT" else GREEN
            txt(s, time.strftime("%H:%M:%S", time.localtime(t)), 13, DIM, (54, 164 + i * 21))
            txt(s, d, 13, col, (150, 164 + i * 21), bold=True)
            txt(s, text, 13, TEXT, (200, 164 + i * 21))

    def draw_help(self, s):
        panel(s, (30, 130, 1340, 660), "Help")
        clip = pygame.Rect(40, 160, 1320, 620)
        s.set_clip(clip)
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

    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        self._hits = []
        self.draw_header(s)
        [self.draw_sound, self.draw_library, self.draw_monitor, self.draw_help][self.tab](s)
        panel(s, (30, 800, 1340, 50), "")
        age = time.time() - self.status_t
        for i, ln in enumerate(wrap(self.status, 13, 1300)[:2]):
            txt(s, ln, 13, TEXT if age < 40 else DIM, (46, 808 + i * 17))
        self.hits = self._hits
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
