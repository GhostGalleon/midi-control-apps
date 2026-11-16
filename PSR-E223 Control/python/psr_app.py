#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
PSR-E223 CONTROL  -  MIDI control surface for the Yamaha PSR-E223 / YPT-220 keyboard.

Run:      python psr_app.py
Needs:    pip install pygame
Data:     psr_data.json  (the 375 voices from the owner's manual: bank MSB / LSB / program)

What it does
  * picks any of the 375 voices (and the 13 drum kits) by MIDI bank select + program change
  * treats the keyboard as a 16-part XGlite sound module: voice, volume, pan, reverb and chorus
    send for every channel, saved as setups and exportable as a .mid clip for your DAW
  * shows everything the keyboard sends (notes, program changes, sysex) and decodes its
    'InitSend' panel dump, so you can see exactly what the keyboard says
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
DATA_FILE = BASE / "psr_data.json"
SOUNDS_DIR = BASE / "psr_setups"
EXPORT_DIR = BASE / "exports"
SETTINGS_FILE = BASE / "psr_settings.json"
LOG_FILE = BASE / "psr_log.txt"

W, H = 1400, 860
GAP = 0.006            # seconds between any two messages
PC_SETTLE = 0.03       # extra wait after a program change

# ---- palette: graphite + mint -----------------------------------------------
AMBER = (171, 114, 240)        # accent (name kept so the shared drawing code works)
AMBER_DIM = (107, 66, 150)
TEXT = (236, 244, 242)
DIM = (162, 149, 172)
PANEL = (42, 28, 41)
EDGE = (143, 118, 152)
LCD_BG = (9, 3, 13)
LCD_FG = (177, 113, 255)
LCD_GLOW = (41, 13, 67)
GREEN = (90, 230, 130)
RED = (255, 90, 80)
BTN = (54, 36, 56)
BTN_HOT = (82, 54, 88)
BTN_EDGE = (141, 114, 152)
WELL = (21, 11, 21)

CH_DEFAULTS = {7: 100, 10: 64, 91: 40, 93: 0, 64: 0}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
DRUM_CH = 9   # channel 10


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


def metal(w, h, top=(54, 38, 66), bot=(14, 9, 20)):
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


def rounded_metal(surf, rect, radius=10, top=(54, 38, 66), bot=(14, 9, 20)):
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
    pygame.draw.rect(surf, (26, 21, 32), r, 1, border_radius=10)
    pygame.draw.rect(surf, EDGE, r.inflate(-2, -2), 1, border_radius=9)
    pygame.draw.line(surf, (241, 230, 255), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (243, 232, 255), (r.x + 16, r.y + 8), bold=True)
    pygame.draw.line(surf, (50, 41, 61), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
    pygame.draw.line(surf, (182, 162, 204), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (20, 16, 24), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (216, 203, 230), (sx, r.bottom - 12), 4, 1)
        pygame.draw.line(surf, (161, 147, 176), (sx - 2, r.bottom - 14), (sx + 2, r.bottom - 10))


def lcd(surf, rect, text, max_size=24):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, (20, 16, 24), r.inflate(6, 6), border_radius=8)
    pygame.draw.rect(surf, (199, 186, 214), r.inflate(6, 6), 1, border_radius=8)
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
        pygame.draw.circle(surf, (43, 37, 51), pos, 4)
        pygame.draw.circle(surf, (171, 154, 190), pos, 4, 1)


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
        pygame.draw.rect(surf, (21, 16, 26), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
        pygame.draw.line(surf, (236, 223, 250), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
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
            txt(surf, "name this sound...", 14, (142, 129, 156), (r.x + 8, r.centery), "midleft")
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
                pygame.draw.rect(surf, (142, 116, 170), r, border_radius=5)
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
            pygame.draw.rect(surf, (20, 16, 24), box, border_radius=3)
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




class Knob2:
    """Round knob bound to (channel, cc). small=True for the mixer strips."""

    def __init__(self, ch, cc, label, cx, cy, small=False, mx=127):
        self.ch, self.cc, self.label, self.cx, self.cy = ch, cc, label, cx, cy
        self.R = 15 if small else 26
        self.small, self.mx = small, mx
        self.acc = 0.0
        self.last_click = 0.0

    def hit(self, pos):
        return math.hypot(pos[0] - self.cx, pos[1] - self.cy) <= self.R + 9

    def get(self, app):
        return app.get_cc(self.ch, self.cc)

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.hit(e.pos):
            now = time.time()
            if now - self.last_click < 0.35:
                app.set_cc(self.ch, self.cc, CH_DEFAULTS.get(self.cc, 0))
                self.last_click = 0.0
                return True
            self.last_click = now
            self.acc = float(self.get(app))
            app.drag = self
            return True
        if e.type == pygame.MOUSEWHEEL and self.hit(pygame.mouse.get_pos()):
            step = 1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 3
            app.set_cc(self.ch, self.cc, self.get(app) + e.y * step)
            return True
        return False

    def drag_by(self, dy, app):
        fine = pygame.key.get_mods() & pygame.KMOD_SHIFT
        self.acc = max(0.0, min(127.0, self.acc - dy * (0.25 if fine else 0.9)))
        app.set_cc(self.ch, self.cc, int(round(self.acc)))

    def draw(self, surf, app):
        v = self.get(app)
        t = v / 127.0
        cx, cy, R = self.cx, self.cy, self.R
        pygame.draw.circle(surf, (12, 6, 13), (cx, cy), R + 8)
        pygame.draw.circle(surf, (150, 128, 160), (cx, cy), R + 8, 1)
        w = 3 if self.small else 4
        pygame.draw.lines(surf, (34, 23, 37), False, arc_pts(cx, cy, R + 4, 135, 405), w)
        if v > 0:
            a1 = 135 + 270 * t
            pygame.draw.lines(surf, (56, 21, 88), False, arc_pts(cx, cy, R + 4, 135, a1), w + 3)
            pygame.draw.lines(surf, AMBER, False, arc_pts(cx, cy, R + 4, 135, a1), w)
        pygame.draw.circle(surf, (84, 76, 85), (cx, cy), R)
        pygame.draw.circle(surf, (160, 149, 162), (cx, cy), R - 2)
        pygame.draw.circle(surf, (130, 119, 132), (cx, cy), max(3, R - 6))
        pygame.draw.circle(surf, (190, 200, 202), (cx - 3, cy - 4), max(2, R - 13))
        ang = math.radians(135 + 270 * t)
        p0 = (cx + math.cos(ang) * R * 0.30, cy + math.sin(ang) * R * 0.30)
        p1 = (cx + math.cos(ang) * R * 0.88, cy + math.sin(ang) * R * 0.88)
        pygame.draw.line(surf, (14, 8, 18), p0, p1, 2 if self.small else 3)
        if self.small:
            txt(surf, self.label, 10, DIM, (cx, cy + R + 12), "center")
            txt(surf, self.fmt(v), 11, AMBER, (cx, cy + R + 25), "center", bold=True)
        else:
            txt(surf, self.label, fit_size(self.label, 90, (13, 12, 11, 10)), DIM, (cx, cy + R + 16), "center")
            txt(surf, self.fmt(v), 15, AMBER, (cx, cy + R + 32), "center", bold=True)

    def fmt(self, v):
        if self.cc == 10:
            return "C" if v == 64 else (f"L{64 - v}" if v < 64 else f"R{v - 64}")
        return str(v)


class MasterKnob(Knob2):
    def get(self, app):
        return app.master_vol

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.hit(e.pos):
            now = time.time()
            if now - self.last_click < 0.35:
                app.set_master(127)
                self.last_click = 0.0
                return True
            self.last_click = now
            self.acc = float(app.master_vol)
            app.drag = self
            return True
        if e.type == pygame.MOUSEWHEEL and self.hit(pygame.mouse.get_pos()):
            app.set_master(app.master_vol + e.y * 3)
            return True
        return False

    def drag_by(self, dy, app):
        self.acc = max(0.0, min(127.0, self.acc - dy * 0.9))
        app.set_master(int(round(self.acc)))


class Fader:
    def __init__(self, ch, cc, x, y, h):
        self.ch, self.cc = ch, cc
        self.rect = pygame.Rect(x - 14, y, 28, h)
        self.cx, self.y, self.h = x, y, h
        self.last_click = 0.0

    def set_from_y(self, my, app):
        t = 1.0 - (my - self.y - 8) / max(1, self.h - 16)
        app.set_cc(self.ch, self.cc, int(round(max(0.0, min(1.0, t)) * 127)))

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.rect.collidepoint(e.pos):
            now = time.time()
            if now - self.last_click < 0.35:
                app.set_cc(self.ch, self.cc, CH_DEFAULTS.get(self.cc, 100))
                self.last_click = 0.0
                return True
            self.last_click = now
            app.drag = self
            self.set_from_y(e.pos[1], app)
            return True
        if e.type == pygame.MOUSEWHEEL and self.rect.collidepoint(pygame.mouse.get_pos()):
            app.set_cc(self.ch, self.cc, app.get_cc(self.ch, self.cc) + e.y * 3)
            return True
        return False

    def drag_by(self, dy, app):
        self.set_from_y(pygame.mouse.get_pos()[1], app)

    def draw(self, surf, app):
        v = app.get_cc(self.ch, self.cc)
        t = v / 127.0
        pygame.draw.rect(surf, (11, 4, 11), (self.cx - 5, self.y, 10, self.h), border_radius=5)
        pygame.draw.rect(surf, (130, 108, 140), (self.cx - 5, self.y, 10, self.h), 1, border_radius=5)
        top = int(self.y + 8 + (1 - t) * (self.h - 16))
        pygame.draw.rect(surf, (56, 21, 88), (self.cx - 3, top, 6, self.y + self.h - 8 - top), border_radius=3)
        for k in range(0, 11):
            yy = self.y + 8 + k * (self.h - 16) / 10
            pygame.draw.line(surf, (112, 95, 120), (self.cx + 9, yy), (self.cx + 14, yy))
        cap = pygame.Rect(0, 0, 26, 14)
        cap.center = (self.cx, top)
        pygame.draw.rect(surf, (160, 149, 162), cap, border_radius=3)
        pygame.draw.rect(surf, (40, 32, 42), cap, 1, border_radius=3)
        pygame.draw.line(surf, AMBER, (cap.x + 3, cap.centery), (cap.right - 3, cap.centery), 2)


class Toggle2:
    def __init__(self, cc, label, cx, cy, w=110):
        self.cc, self.label = cc, label
        self.rect = pygame.Rect(0, 0, w, 34)
        self.rect.center = (cx, cy)

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.rect.collidepoint(e.pos):
            app.toggle(self.cc)
            return True
        return False

    def draw(self, surf, app):
        on = app.get_toggle(self.cc)
        r = self.rect
        txt(surf, self.label, 12, DIM, (r.centerx, r.y - 11), "center")
        pygame.draw.rect(surf, (12, 6, 13), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, (49, 18, 77) if on else BTN, r, border_radius=6)
        pygame.draw.rect(surf, AMBER if on else BTN_EDGE, r, 1, border_radius=6)
        led(surf, (r.x + 12, r.centery), on)
        txt(surf, "ON" if on else "OFF", 14, AMBER if on else DIM, (r.centerx + 8, r.centery), "center", bold=True)


class VoiceButton:
    """LCD that shows the voice of one channel; click opens the voice browser."""

    def __init__(self, ch, caption, rect, small=False, kits_only=False):
        self.ch, self.caption, self.small, self.kits_only = ch, caption, small, kits_only
        self.rect = pygame.Rect(rect)
        self.lcd = pygame.Rect(self.rect.x, self.rect.y + (0 if small else 20), self.rect.w, self.rect.h - (0 if small else 20))

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3) and self.lcd.collidepoint(e.pos):
            if e.button == 1:
                app.open_browser(self.ch, self.kits_only)
            else:
                app.step_voice(self.ch, 1)
            return True
        if e.type == pygame.MOUSEWHEEL and self.lcd.collidepoint(pygame.mouse.get_pos()):
            app.step_voice(self.ch, -1 if e.y > 0 else 1, self.kits_only)
            return True
        return False

    def draw(self, surf, app):
        v = app.voice_of(self.ch)
        if self.small:
            r = self.lcd
            pygame.draw.rect(surf, (11, 4, 11), r.inflate(4, 4), border_radius=6)
            pygame.draw.rect(surf, LCD_BG, r, border_radius=5)
            pygame.draw.rect(surf, (66, 46, 88), r, 1, border_radius=5)
            hover = r.collidepoint(pygame.mouse.get_pos())
            name = v["name"] if v else "--"
            lines = wrap(name, 11, r.w - 8)[:3]
            for i, ln in enumerate(lines):
                txt(surf, ln, 11, (220, 197, 255) if hover else LCD_FG, (r.centerx, r.y + 11 + i * 13), "center")
            if v:
                txt(surf, f"{v['no']:03d}", 10, AMBER_DIM, (r.centerx, r.bottom - 8), "center")
            return
        txt(surf, " ".join(self.caption.upper()), 12, (213, 199, 226), (self.rect.x + 4, self.rect.y), bold=True)
        lcd(surf, self.lcd, (f"{v['no']:03d}  {v['name']}" if v else "--"))
        triangle(surf, self.lcd.x + 22, self.lcd.centery, -1, AMBER_DIM)
        triangle(surf, self.lcd.right - 22, self.lcd.centery, +1, AMBER_DIM)


class OnScreenKeys:
    """61 keys (Yamaha C1-C6 = MIDI 36-96). Shows incoming notes, click to play."""
    LOW, HIGH = 36, 96

    def __init__(self, x, y, wkey=36, hw=104, hb=64):
        self.keys = []
        whites = [n for n in range(self.LOW, self.HIGH + 1) if n % 12 in (0, 2, 4, 5, 7, 9, 11)]
        pos = {}
        for i, n in enumerate(whites):
            pos[n] = x + i * wkey
            self.keys.append((pygame.Rect(x + i * wkey, y, wkey - 1, hw), n, False))
        blacks = []
        for n in range(self.LOW, self.HIGH + 1):
            if n % 12 in (1, 3, 6, 8, 10):
                bx = pos[n - 1] + wkey - 11
                blacks.append((pygame.Rect(bx, y, 22, hb), n, True))
        self.keys += blacks
        self.down = None

    def key_at(self, pos):
        for r, n, black in list(reversed([k for k in self.keys if k[2]])) + [k for k in self.keys if not k[2]]:
            if r.collidepoint(pos):
                return n
        return None

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            n = self.key_at(e.pos)
            if n is not None:
                self.down = n
                app.play_note(n, True)
                return True
        if e.type == pygame.MOUSEBUTTONUP and e.button == 1 and self.down is not None:
            app.play_note(self.down, False)
            self.down = None
            return True
        return False

    def draw(self, surf, app):
        lit = app.notes_down | ({self.down} if self.down is not None else set())
        for black in (False, True):
            for r, n, b in self.keys:
                if b != black:
                    continue
                on = n in lit
                if b:
                    pygame.draw.rect(surf, (35, 14, 56) if on else (13, 8, 13), r, border_radius=3)
                    if on:
                        pygame.draw.rect(surf, AMBER, r, 2, border_radius=3)
                    else:
                        pygame.draw.rect(surf, (64, 55, 69), r, 1, border_radius=3)
                else:
                    pygame.draw.rect(surf, (158, 89, 230) if on else (222, 228, 228), r, border_radius=3)
                    pygame.draw.rect(surf, (41, 31, 43), r, 1, border_radius=3)
                    if n % 12 == 0:
                        txt(surf, note_name(n), 10, (46, 40, 50), (r.centerx, r.bottom - 10), "center")


class VoiceBrowser:
    """Full-screen voice picker: categories on the left, type to search, voices on the right."""
    COLS, ROWS, CW, CH_ = 3, 22, 300, 28

    def __init__(self, app, ch, kits_only=False):
        self.app, self.ch = app, ch
        self.cats = ["DRUM KITS"] if kits_only else app.data["cats"]
        self.kits_only = kits_only
        cur = app.voice_of(ch)
        self.cat = (cur["cat"] if cur and cur["cat"] in self.cats else self.cats[0])
        self.query = ""
        self.page = 0
        self.panel = pygame.Rect(40, 36, 1320, 770)
        if kits_only:
            self.panel = pygame.Rect(330, 120, 740, 540)
        self.cat_rects = []
        self.cells = []

    def voices(self):
        vs = self.app.data["voices"]
        if self.query:
            q = self.query.lower()
            return [v for v in vs if q in v["name"].lower() or q == f"{v['no']:03d}" or q == str(v["no"])]
        return [v for v in vs if v["cat"] == self.cat]

    def per_page(self):
        return self.ROWS * (2 if self.kits_only else self.COLS)

    def handle(self, e, app):
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_ESCAPE:
                app.popup = None
            elif e.key == pygame.K_BACKSPACE:
                self.query = self.query[:-1]
                self.page = 0
            elif e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                vs = self.voices()
                if vs:
                    app.popup = None
                    app.set_voice(self.ch, vs[0]["no"])
            elif e.key == pygame.K_PAGEDOWN:
                self.page += 1
            elif e.key == pygame.K_PAGEUP:
                self.page = max(0, self.page - 1)
            elif e.unicode and e.unicode.isprintable() and len(self.query) < 24 and not self.kits_only:
                self.query += e.unicode
                self.page = 0
        elif e.type == pygame.MOUSEWHEEL:
            self.page = max(0, self.page - e.y)
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for r, c in self.cat_rects:
                if r.collidepoint(e.pos):
                    self.cat, self.query, self.page = c, "", 0
                    return True
            for r, v in self.cells:
                if r.collidepoint(e.pos):
                    app.popup = None
                    app.set_voice(self.ch, v["no"])
                    return True
            if not self.panel.collidepoint(e.pos):
                app.popup = None
        return True

    def draw(self, surf, app):
        dim = pygame.Surface((W, H), pygame.SRCALPHA)
        dim.fill((4, 0, 6, 200))
        surf.blit(dim, (0, 0))
        rounded_metal(surf, self.panel, 12)
        pygame.draw.rect(surf, AMBER, self.panel, 1, border_radius=12)
        p = self.panel
        title = f"DRUM KIT  (channel {self.ch + 1})" if self.kits_only else f"CHOOSE A VOICE  (channel {self.ch + 1})"
        txt(surf, " ".join(title.upper()), 14, (255, 255, 255), (p.x + 24, p.y + 16), bold=True)
        mouse = pygame.mouse.get_pos()
        gx = p.x + 24
        gy = p.y + 64
        if not self.kits_only:
            sr = pygame.Rect(p.x + 270, p.y + 10, 420, 34)
            pygame.draw.rect(surf, WELL, sr, border_radius=6)
            pygame.draw.rect(surf, AMBER, sr, 1, border_radius=6)
            show = self.query + ("_" if int(time.time() * 2) % 2 == 0 else "")
            txt(surf, show if self.query else "type to search all 375 voices...", 15,
                TEXT if self.query else (130, 108, 140), (sr.x + 10, sr.centery), "midleft")
            self.cat_rects = []
            for i, c in enumerate(self.cats):
                r = pygame.Rect(p.x + 20, p.y + 60 + i * 30, 230, 28)
                self.cat_rects.append((r, c))
                act = c == self.cat and not self.query
                if act:
                    pygame.draw.rect(surf, (56, 21, 88), r, border_radius=5)
                elif r.collidepoint(mouse):
                    pygame.draw.rect(surf, BTN_HOT, r, border_radius=5)
                txt(surf, c, 14, (255, 255, 255) if act else TEXT, (r.x + 12, r.centery), "midleft", bold=act)
            gx = p.x + 280
        vs = self.voices()
        cols = 2 if self.kits_only else self.COLS
        per = self.per_page()
        pages = max(1, math.ceil(len(vs) / per))
        self.page = max(0, min(self.page, pages - 1))
        cur = app.voice_of(self.ch)
        cw = 340 if self.kits_only else self.CW
        self.cells = []
        for i, v in enumerate(vs[self.page * per:(self.page + 1) * per]):
            c, rw = divmod(i, self.ROWS)
            r = pygame.Rect(gx + c * cw, gy + rw * self.CH_, cw - 6, self.CH_ - 2)
            self.cells.append((r, v))
            if r.collidepoint(mouse):
                pygame.draw.rect(surf, (89, 46, 130), r, border_radius=5)
            if cur and v["no"] == cur["no"]:
                pygame.draw.rect(surf, AMBER, r, 1, border_radius=5)
            txt(surf, f"{v['no']:03d}", 12, AMBER_DIM, (r.x + 8, r.centery), "midleft")
            txt(surf, v["name"], 14, TEXT, (r.x + 48, r.centery), "midleft")
        foot = f"{len(vs)} voices   page {self.page + 1}/{pages}   (mouse wheel or PageUp / PageDown)   Esc to close"
        txt(surf, foot, 12, DIM, (p.x + 280 if not self.kits_only else p.x + 24, p.bottom - 24))


class FxChoice:
    """A big selectable button: lit when its getter says it is the current choice."""

    def __init__(self, rect, label, is_on, cb):
        self.rect = pygame.Rect(rect)
        self.label, self.is_on, self.cb = label, is_on, cb

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.rect.collidepoint(e.pos):
            self.cb(app)
            return True
        return False

    def draw(self, surf, app):
        r = self.rect
        on = self.is_on(app)
        hover = r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(surf, (21, 16, 26), r.inflate(4, 4), border_radius=9)
        pygame.draw.rect(surf, (56, 21, 88) if on else (BTN_HOT if hover else BTN), r, border_radius=7)
        pygame.draw.rect(surf, AMBER if (on or hover) else BTN_EDGE, r, 1, border_radius=7)
        led(surf, (r.x + 22, r.centery), on, AMBER)
        txt(surf, self.label, 18, (255, 255, 255) if on else TEXT, (r.x + 48, r.centery), "midleft", bold=True)


class FxStep:
    """Small button that nudges the tuning (d = cents) or resets it (d = None)."""

    def __init__(self, rect, label, d):
        self.btn = Button(rect, label, lambda b: None)
        self.rect, self.d = self.btn.rect, d

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.rect.collidepoint(e.pos):
            app.set_tune(0 if self.d is None else app.tune + self.d)
            return True
        return False

    def draw(self, surf, app):
        self.btn.draw(surf, app)


class TuneSlider:
    """Horizontal slider for the master tuning, -100..+100 cents."""

    def __init__(self, x, cy, w):
        self.x, self.cy, self.w = x, cy, w
        self.rect = pygame.Rect(x - 16, cy - 22, w + 32, 44)
        self.last_click = 0.0

    def px(self, c):
        return self.x + (c + 100) / 200.0 * self.w

    def handle(self, e, app):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.rect.collidepoint(e.pos):
            now = time.time()
            if now - self.last_click < 0.35:
                app.set_tune(0)
                self.last_click = 0.0
                return True
            self.last_click = now
            app.drag = self
            self.drag_pos(e.pos, app)
            return True
        if e.type == pygame.MOUSEWHEEL and self.rect.collidepoint(pygame.mouse.get_pos()):
            app.set_tune(app.tune + e.y * (1 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 5))
            return True
        return False

    def drag_pos(self, pos, app):
        app.set_tune(round((pos[0] - self.x) / self.w * 200 - 100))

    def draw(self, surf, app):
        cy = self.cy
        pygame.draw.rect(surf, (12, 6, 13), (self.x - 8, cy - 7, self.w + 16, 14), border_radius=7)
        pygame.draw.rect(surf, (150, 128, 160), (self.x - 8, cy - 7, self.w + 16, 14), 1, border_radius=7)
        for c in range(-100, 101, 25):
            x = int(self.px(c))
            pygame.draw.line(surf, (132, 108, 150) if c else AMBER, (x, cy + 12), (x, cy + 20 if c % 50 else cy + 24))
            if c % 50 == 0:
                txt(surf, f"{c:+d}" if c else "0", 11, DIM, (x, cy + 38), "center")
        x0, x1 = sorted((int(self.px(0)), int(self.px(app.tune))))
        if x1 > x0:
            pygame.draw.rect(surf, AMBER, (x0, cy - 3, x1 - x0, 6), border_radius=3)
        hx = int(self.px(app.tune))
        h = pygame.Rect(hx - 11, cy - 19, 22, 38)
        pygame.draw.rect(surf, (84, 76, 85), h, border_radius=5)
        pygame.draw.rect(surf, (181, 169, 184), h.inflate(-6, -6), border_radius=4)
        pygame.draw.line(surf, (14, 8, 18), (hx, cy - 14), (hx, cy + 14), 2)
        pygame.draw.rect(surf, AMBER if app.drag is self else (219, 209, 230), h, 1, border_radius=5)


# =============================================================================
# MIDI HELPERS
# =============================================================================
def msg_cc(ch, cc, v):
    return [0xB0 | ch, cc, v]


def msg_pc(ch, p):
    return [0xC0 | ch, p]


GM_ON = [0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7]
XG_ON = [0xF0, 0x43, 0x10, 0x4C, 0x00, 0x00, 0x7E, 0x00, 0xF7]


def msg_master(v):
    return [0xF0, 0x7F, 0x7F, 0x04, 0x01, 0x00, max(0, min(127, v)), 0xF7]


# Effect types as the keyboard itself sends them (captured from the user's PSR-E223).
# XG effect 1 address 02 01 00 = reverb (MSB, LSB); 02 01 20 = chorus (MSB, LSB).
REVERBS = [("Hall 1", 1, 0), ("Hall 2", 1, 16), ("Hall 3", 1, 17), ("Room 1", 2, 17), ("Room 2", 2, 19),
           ("Stage 1", 3, 16), ("Stage 2", 3, 17), ("Plate 1", 4, 16), ("Plate 2", 4, 17), ("Off", 0, 0)]
CHORUSES = [("Chorus 1", 0x42, 0x11), ("Chorus 2", 0x41, 0x02), ("Flanger 1", 0x43, 0x08),
            ("Flanger 2", 0x43, 0x11), ("Off", 0, 0)]


def msg_reverb(i):
    _, a, b = REVERBS[i]
    return [0xF0, 0x43, 0x10, 0x4C, 0x02, 0x01, 0x00, a, b, 0xF7]


def msg_chorus(i):
    _, a, b = CHORUSES[i]
    return [0xF0, 0x43, 0x10, 0x4C, 0x02, 0x01, 0x20, a, b, 0xF7]


def msg_tune(cents):
    """Yamaha master tune as the panel sends it: value = 128 + cents, split into two nibbles."""
    v = 128 + max(-100, min(100, int(cents)))
    return [0xF0, 0x43, 0x10, 0x27, 0x30, 0x00, 0x00, v >> 4, v & 15, 0x00, 0xF7]


def tune_hz(cents):
    return 440.0 * 2 ** (cents / 1200.0)


def parse_fx(m):
    """Recognise reverb / chorus / tuning sysex. Returns (kind, value, text) or None."""
    if len(m) == 10 and m[:7] == [0xF0, 0x43, 0x10, 0x4C, 0x02, 0x01, 0x00]:
        for i, (n, a, b) in enumerate(REVERBS):
            if (a, b) == (m[7], m[8]):
                return "reverb", i, f"Reverb type = {n}"
        return "reverb", None, f"Reverb type = code {m[7]:02X} {m[8]:02X}"
    if len(m) == 10 and m[:7] == [0xF0, 0x43, 0x10, 0x4C, 0x02, 0x01, 0x20]:
        for i, (n, a, b) in enumerate(CHORUSES):
            if (a, b) == (m[7], m[8]):
                return "chorus", i, f"Chorus type = {n}"
        return "chorus", None, f"Chorus type = code {m[7]:02X} {m[8]:02X}"
    if len(m) == 11 and m[:7] == [0xF0, 0x43, 0x10, 0x27, 0x30, 0x00, 0x00]:
        c = ((m[7] << 4) | (m[8] & 15)) - 128
        return "tune", c, f"Tuning = {c:+d} cents (A = {tune_hz(c):.1f} Hz)"
    return None


XG_NAMES = {(0x00, 0x00, 0x00): "Master Tune", (0x00, 0x00, 0x04): "Master Volume",
            (0x00, 0x00, 0x06): "Transpose", (0x00, 0x00, 0x7E): "XG System On", (0x02, 0x01, 0x00): "Reverb Type (MSB, LSB)",
            (0x02, 0x01, 0x20): "Chorus Type (MSB, LSB)"}


def hexs(b):
    return " ".join(f"{x:02X}" for x in b)


def describe(m):
    """Plain-English version of one MIDI message (list of ints)."""
    st = m[0]
    if st == 0xF0:
        fx = parse_fx(m)
        if fx:
            return "SysEx: " + fx[2]
        if m[:5] == [0xF0, 0x7E, 0x7F, 0x09, 0x01]:
            return "SysEx: GM System On"
        if len(m) > 7 and m[1] == 0x43 and (m[2] & 0xF0) == 0x10 and m[3] == 0x4C:
            addr = tuple(m[4:7])
            if addr == (0, 0, 0x7E):
                return "SysEx: XG System On"
            return f"SysEx: XG {XG_NAMES.get(addr, 'param %02X %02X %02X' % addr)} = {hexs(m[7:-1])}"
        if len(m) > 7 and m[1] == 0x43 and m[3] == 0x27 and m[4] == 0x30:
            return f"SysEx: Yamaha master tune = {hexs(m[7:-1])}"
        if m[:5] == [0xF0, 0x7F, 0x7F, 0x04, 0x01]:
            return f"SysEx: master volume {m[6]}"
        return f"SysEx {len(m)} bytes: {hexs(m)[:70]}"
    kind, ch = st & 0xF0, (st & 15) + 1
    if kind == 0x90:
        return f"ch{ch:02d} NOTE ON  {note_name(m[1]):<4} v{m[2]}" if m[2] else f"ch{ch:02d} note off {note_name(m[1])}"
    if kind == 0x80:
        return f"ch{ch:02d} note off {note_name(m[1])}"
    if kind == 0xB0:
        nm = {0: "Bank MSB", 32: "Bank LSB", 1: "Modulation", 7: "Volume", 10: "Pan", 11: "Expression",
              64: "Sustain", 91: "Reverb", 93: "Chorus", 120: "All Sound Off", 121: "Reset Ctrl",
              122: "Local", 123: "All Notes Off"}.get(m[1], f"CC{m[1]}")
        return f"ch{ch:02d} {nm} = {m[2]}"
    if kind == 0xC0:
        return f"ch{ch:02d} Program Change {m[1]}"
    if kind == 0xE0:
        return f"ch{ch:02d} Pitch Bend {m[1] | (m[2] << 7)}"
    return hexs(m)


def vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def smf_bytes(msgs, pauses, ppq=480):
    ev = bytearray()
    first = True
    for m, pause in zip(msgs, pauses):
        gap = 0 if first else 2 + int(pause * 960)
        first = False
        ev += vlq(gap)
        if m[0] == 0xF0:
            ev += b"\xF0" + vlq(len(m) - 1) + bytes(m[1:])
        else:
            ev += bytes(m)
    ev += vlq(480) + b"\xff\x2f\x00"
    trk = vlq(0) + b"\xff\x03" + bytes([8]) + b"PSR-E223" + bytes(ev)
    return (b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") + ppq.to_bytes(2, "big")
            + b"MTrk" + len(trk).to_bytes(4, "big") + trk)


HELP_TEXT = [
    ("H", "WHAT THIS IS"),
    ("P", "A control surface for the Yamaha PSR-E223 (YPT-220). It chooses any of the keyboard's 375 voices and 13 drum kits over MIDI, "
          "treats the keyboard as a 16-part sound module with its own mixer, shows everything the keyboard sends, and saves your setups."),
    ("H", "CONNECTING"),
    ("B", "Keyboard MIDI OUT goes to a MIDI IN on your interface; keyboard MIDI IN comes from a MIDI OUT. Click MIDI OUT and MIDI IN at the top to pick the matching ports."),
    ("B", "Switch the keyboard on first, then the interface (the manual's order). Only one program can own a Windows MIDI port: close MIDI-OX or the DAW port while this app uses it."),
    ("B", "Nothing is sent when the app starts. The screen shows defaults until you touch something."),
    ("H", "MAIN TAB"),
    ("B", "MAIN VOICE (channel 1): click the display to open the voice browser, or use the wheel / right-click to step. Categories on the left, type to search all 375 voices, Enter picks the first match."),
    ("B", "DRUM KIT (channel 10): the 13 kits (Standard, Room, Rock, Electronic, Analog, Dance, Jazz, Brush, Symphony, SFX)."),
    ("B", "Volume, Pan, Reverb and Chorus are the standard MIDI controls (CC7, 10, 91, 93) for channel 1. SUSTAIN is CC64. LOCAL is CC122: OFF makes the keys silent but still transmitting - use that when a DAW plays the keyboard's sound."),
    ("B", "MASTER VOLUME is the universal MIDI master volume message. GM + XG RESET sends the same two messages the keyboard sends itself and puts everything back to defaults. ALL NOTES OFF stops stuck notes on every channel."),
    ("B", "PLAY FROM: tick any other MIDI inputs (a Vmini, a pad controller, a DAW port) and everything they play - notes, pitch bend, controllers, program changes - is sent straight to the keyboard. Several can be on at once. MIDI IN stays for the keyboard itself: it feeds the monitor, the on-screen keys and the panel readout."),
    ("B", "Tip: if a device plays on a channel other than 1, the keyboard answers on that channel, so set a voice for it on the MIXER tab."),
    ("B", "If a port will not open (an 'Invalid device ID' message), the app re-scans Windows' MIDI list by itself. If it still fails, close MIDI-OX or any DAW using that port, or re-plug the interface and restart."),
    ("B", "LOCAL OFF stops the keyboard's keys sounding by themselves; they only send MIDI. While Local is OFF and MIDI IN + MIDI OUT are both set, this app echoes the keys back to the keyboard, so you still hear them and any other device ticked in PLAY FROM can play too. A DAW does the same job when it is in the loop."),
    ("B", "If notes sound twice, the keyboard's real Local state differs from the screen (for example after switching the keyboard off and on): press Local OFF then ON once to bring them back in step."),
    ("B", "FAVOURITES: click a slot to load it into the main voice; RIGHT-click a slot to store the current main voice there."),
    ("B", "The keyboard at the bottom lights up with the notes you play on the real keys. Click it to audition the current voice on the AUDITION channel."),
    ("H", "MIXER TAB"),
    ("P", "All 16 parts. Each strip has the voice (click it), Pan, Reverb send, Chorus send and a Volume fader. Channel 10 starts as the drum part. "
          "This is how a DAW or sequencer uses the keyboard as a multi-timbral sound module."),
    ("H", "EFFECTS TAB"),
    ("B", "REVERB TYPE: Hall 1-3, Room 1-2, Stage 1-2, Plate 1-2 or Off. CHORUS TYPE: Chorus 1-2, Flanger 1-2 or Off. These are the exact messages the keyboard sends when you step the same Function settings on its panel."),
    ("B", "TUNING: -100 to +100 cents in 1-cent steps (the readout also shows the matching A pitch in Hz). Drum kits ignore tuning. Double-click the slider to return to 0."),
    ("B", "The type buttons start un-lit because the keyboard cannot be asked what it is set to. Press InitSend on the keyboard, or just step the setting on the panel, and the app lights the right button."),
    ("H", "MONITOR TAB"),
    ("B", "Lists every message the keyboard sends: notes, controllers, program changes and system exclusive, in plain English."),
    ("B", "READ PANEL: on the keyboard press FUNCTION until the display shows InitSend, then press +. The keyboard sends its panel settings; the app decodes them and the voices and mixer on screen follow."),
    ("B", "SAVE LOG writes psr_log.txt with everything seen - send it to me if a message needs decoding."),
    ("H", "SETUPS AND DAW USE"),
    ("B", "Type a name and press SAVE: all 16 voices, the mixer and the master volume go into a .json file in psr_setups. Click a chip to load it."),
    ("B", "EXPORT MID writes the whole setup as a .mid file in the exports folder. Put it at the start of a MIDI track routed to the keyboard and the DAW recalls the sound."),
    ("B", "A load sends bank select (CC0, CC32) then program change for every channel, then the mixer controls, so voices never arrive half-set."),
    ("H", "WHAT MIDI CANNOT DO ON THIS KEYBOARD"),
    ("B", "Styles and songs, tempo, accompaniment and the Function settings (style / song volume, split point and so on) are panel only - the manual lists no MIDI control for them."),
    ("B", "Transpose, split point and the other Function settings have not been captured yet. Step one on the keyboard with MIDI IN open, send me psr_log.txt, and it can be added the same way as reverb, chorus and tuning."),
    ("B", "The panel voice shown on the keyboard's display may not follow a voice you choose over MIDI; the sound does."),
]


# =============================================================================
# THE APP
# =============================================================================
TABS = ["MAIN", "MIXER", "EFFECTS", "MONITOR", "HELP"]
FAV_DEFAULT = [1, 6, 10, 18, 24, 26, 34, 62, 49, 65, 73, 82]


class App:
    def __init__(self, headless=False, tab=0):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("PSR-E223 CONTROL")
        self.data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        self.by_no = {v["no"]: v for v in self.data["voices"]}
        self.by_code = {}
        for v in self.data["voices"]:
            self.by_code.setdefault((v["msb"], v["lsb"], v["pc"]), v["no"])
        self.tab = tab
        self.running = True
        self.drag = None
        self.popup = None
        self.help_scroll = 0
        self.notes_down = set()
        self.aud_ch = 0
        self.master_vol = 127
        self.local_on = True
        self.vals = {}
        self.part = [1] * 16
        self.reset_state()
        self.in_bank = [[0, 0] for _ in range(16)]
        self.sx = None
        self.ctls = {}
        self.panel_state = {}
        self.events = deque(maxlen=4000)
        self.mon = deque(maxlen=14)
        self.flash_out = self.flash_in = 0.0
        self.status = "Nothing is sent until you touch a control. Press the keys on the keyboard to see them here."
        self.status_t = time.time()

        self.out = self.inp = None
        self.out_lock = threading.Lock()
        self.settings = {}
        try:
            self.settings = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
        self.favs = list(self.settings.get("favs", FAV_DEFAULT))[:12]
        self.favs += FAV_DEFAULT[len(self.favs):]
        self.aud_ch = int(self.settings.get("aud_ch", 0)) & 15
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
        self.sounds, self.sound_off = [], 0
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

    # ---- state ------------------------------------------------------------
    def reset_state(self):
        self.vals = {(ch, cc): v for ch in range(16) for cc, v in CH_DEFAULTS.items()}
        self.part = [1] * 16
        self.part[DRUM_CH] = 130
        self.master_vol = 127
        self.local_on = True
        self.reverb = None      # index into REVERBS, None = unknown
        self.chorus = None      # index into CHORUSES, None = unknown
        self.tune = 0           # cents, -100..+100

    def voice_of(self, ch):
        return self.by_no.get(self.part[ch])

    def get_cc(self, ch, cc):
        return self.vals.get((ch, cc), CH_DEFAULTS.get(cc, 0))

    def get_toggle(self, cc):
        return self.local_on if cc == 122 else self.get_cc(0, cc) >= 64

    def set_status(self, s):
        self.status, self.status_t = s, time.time()

    def log(self, direction, text):
        self.mon.append((direction, text))
        self.events.append((time.time(), direction, text))

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
            (self.in_devs if is_in else self.out_devs).append((i, f"{name}#{n}"))
        print("MIDI outputs:", [n for _, n in self.out_devs])
        print("MIDI inputs: ", [n for _, n in self.in_devs])

    def _find(self, kind, key):
        devs = self.out_devs if kind == "out" else self.in_devs
        for i, k in devs:
            if k == key:
                return i
        raise RuntimeError("device is not in the list any more")

    def close_all(self):
        with self.out_lock:
            devs = [self.out, self.inp] + list(self.ctls.values())
            self.out = self.inp = None
            self.ctls = {}
        for d in devs:
            try:
                if d:
                    d.close()
            except Exception:
                pass

    def rescan(self):
        """Close every port and make Windows list the devices again (needed after a USB interface is re-plugged)."""
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
        for key in self.settings.get("ctl_names", []):
            self.open_ctl(key, retry=False)

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
            dev = pgmidi.Output(i) if kind == "out" else pgmidi.Input(i)
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

    def open_ctl(self, key, retry=True):
        if key in self.ctls or not pgmidi:
            return
        try:
            dev = pgmidi.Input(self._find("in", key))
        except Exception as ex:
            if retry:
                self.rescan()
                self.reopen_all()
                return
            self.set_status(f"Could not open controller {key}: {ex}")
            return
        self.ctls[key] = dev

    def close_ctl(self, key):
        dev = self.ctls.pop(key, None)
        try:
            if dev:
                dev.close()
        except Exception:
            pass

    def choose_ctls(self, _b=1):
        self.refresh_devices()
        skip = self.settings.get("in_name")
        opts = [(k, k[:-2] if k.endswith("#0") else k) for _, k in self.in_devs if k != skip]

        def toggle(key, on):
            names = [n for n in self.settings.get("ctl_names", []) if n != key]
            if on:
                names.append(key)
                self.open_ctl(key)
            else:
                self.close_ctl(key)
            self.settings["ctl_names"] = names
            self.save_settings()
            self.set_status(("Playing from: " + ", ".join(n[:-2] if n.endswith('#0') else n for n in names))
                            if names else "No controllers selected.")
        self.popup = MultiPopup("Play the keyboard from these MIDI inputs", opts,
                                self.settings.get("ctl_names", []), toggle)

    def choose_device(self, kind, _b=1):
        self.rescan()
        self.reopen_all()
        devs = self.out_devs if kind == "out" else self.in_devs
        opts = [(None, "(none)")] + [(k, k[:-2] if k.endswith("#0") else k) for _, k in devs]

        def pick(key):
            self.settings[kind + "_name"] = key
            self.open_device(kind, key)
            self.save_settings()
        self.popup = Popup("MIDI OUT (to the keyboard's MIDI IN)" if kind == "out" else "MIDI IN (from the keyboard's MIDI OUT)",
                           opts, self.settings.get(kind + "_name"), pick)

    def save_settings(self):
        self.settings["favs"] = self.favs
        self.settings["aud_ch"] = self.aud_ch
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    # ---- sending ------------------------------------------------------------
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
        if (m[0] & 0xF0) not in (0x90, 0x80):
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
                p = pauses[i] if pauses else (PC_SETTLE if (m[0] & 0xF0) == 0xC0 else 0.0)
                self.q.append((m, p, group))

    # ---- parameter changes ----------------------------------------------------
    def set_cc(self, ch, cc, v):
        v = max(0, min(127, int(v)))
        if self.get_cc(ch, cc) == v:
            return
        self.vals[(ch, cc)] = v
        self.send([msg_cc(ch, cc, v)], group=f"cc{ch}_{cc}")

    def set_master(self, v):
        v = max(0, min(127, int(v)))
        if v != self.master_vol:
            self.master_vol = v
            self.send([msg_master(v)], group="master")

    def set_reverb(self, i):
        self.reverb = i
        self.send([msg_reverb(i)], group="reverb", pauses=[0.02])
        self.set_status(f"Reverb type: {REVERBS[i][0]}")

    def set_chorus(self, i):
        self.chorus = i
        self.send([msg_chorus(i)], group="chorus", pauses=[0.02])
        self.set_status(f"Chorus type: {CHORUSES[i][0]}")

    def set_tune(self, c):
        c = max(-100, min(100, int(c)))
        if c == self.tune:
            return
        self.tune = c
        self.send([msg_tune(c)], group="tune", pauses=[0.02])

    def toggle(self, cc):
        if cc == 122:
            self.local_on = not self.local_on
            self.send([msg_cc(0, 122, 127 if self.local_on else 0)], group="local")
            self.set_status("Local ON: keys play the keyboard's own sound." if self.local_on
                            else "Local OFF: the keyboard's keys only send MIDI. With MIDI IN and MIDI OUT both set, this app echoes them "
                                 "back so you still hear them (a DAW would normally do that).")
        else:
            self.set_cc(0, cc, 0 if self.get_toggle(cc) else 127)

    def voice_msgs(self, ch, no):
        v = self.by_no[no]
        return [msg_cc(ch, 0, v["msb"]), msg_cc(ch, 32, v["lsb"]), msg_pc(ch, v["pc"])]

    def set_voice(self, ch, no):
        self.part[ch] = no
        self.send(self.voice_msgs(ch, no), group=f"voice{ch}")
        self.set_status(f"Channel {ch + 1}: {no:03d} {self.by_no[no]['name']}")

    def step_voice(self, ch, d, kits_only=False):
        vs = [v for v in self.data["voices"] if (v["cat"] == "DRUM KITS") == bool(kits_only or ch == DRUM_CH)]
        nos = [v["no"] for v in vs]
        cur = self.part[ch]
        i = nos.index(cur) if cur in nos else 0
        self.set_voice(ch, nos[(i + d) % len(nos)])

    def open_browser(self, ch, kits_only=False):
        self.popup = VoiceBrowser(self, ch, kits_only)

    def play_note(self, n, on):
        self.send([[(0x90 if on else 0x80) | self.aud_ch, n, 100 if on else 0]], group=None, pauses=[0.0])
        self.flash_out = time.time()

    def cycle_aud(self, b):
        self.aud_ch = (self.aud_ch + (1 if b == 1 else -1)) % 16
        self.save_settings()

    def panic(self, _b=1):
        msgs = []
        for ch in range(16):
            msgs += [msg_cc(ch, 123, 0), msg_cc(ch, 120, 0)]
        self.send(msgs, group="panic", pauses=[0.0] * len(msgs))
        self.set_status("All notes off / all sound off sent on all 16 channels.")

    def reset_gmxg(self, _b=1):
        self.send([GM_ON, XG_ON, msg_cc(90, 0, 127)], group="reset", pauses=[0.15, 0.25, 0.0])
        self.reset_state()
        self.set_status("GM System On + XG System On sent. All parts are back to defaults.")

    def read_panel_help(self, _b=1):
        self.popup = Info("Read the keyboard's panel", [
            "# On the keyboard",
            "1.  Press FUNCTION repeatedly until the display shows InitSend.",
            "2.  Press the + button once. The keyboard sends its panel settings over MIDI.",
            "",
            "# In this app",
            "Choose MIDI IN first (the port wired to the keyboard's MIDI OUT). The MONITOR tab lists what arrives, and the reverb type, chorus type and tuning in the dump are applied to the screen, along with any voice, volume, pan or send levels.",
            "Nothing is written back to the keyboard.",
        ], [("OK", None)])

    # ---- incoming MIDI ----------------------------------------------------------
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
            self.on_short(b0, data[1], data[2])

    def poll_ctls(self):
        for key, dev in list(self.ctls.items()):
            try:
                if not dev.poll():
                    continue
                events = dev.read(256)
            except Exception:
                continue
            for data, _ts in events:
                st = data[0]
                if st < 0x80 or st >= 0xF0:
                    continue
                kind = st & 0xF0
                m = [st, data[1]] if kind in (0xC0, 0xD0) else [st, data[1], data[2]]
                self.send([m], group=None, pauses=[0.0])
                self.flash_in = time.time()
                if kind == 0x90 and m[2]:
                    self.notes_down.add(m[1])
                elif kind in (0x80, 0x90):
                    self.notes_down.discard(m[1])
                if kind in (0x80, 0x90) and not (kind == 0x80 or (kind == 0x90 and not m[2])):
                    self.mon.append(("CTL", describe(m)))
                elif kind not in (0x80, 0x90):
                    self.mon.append(("CTL", describe(m)))
                    self.events.append((time.time(), "CTL", describe(m)))

    def on_sysex(self, m):
        self.flash_in = time.time()
        self.log("IN ", describe(m))
        fx = parse_fx(m)
        if fx:
            kind, val, text = fx
            label, _, shown = text.partition(" = ")
            self.panel_state[label] = shown
            if kind == "reverb" and val is not None:
                self.reverb = val
            elif kind == "chorus" and val is not None:
                self.chorus = val
            elif kind == "tune":
                self.tune = max(-100, min(100, val))
        elif m[1] == 0x43 and len(m) > 8 and m[3] == 0x4C:
            addr = tuple(m[4:7])
            self.panel_state[XG_NAMES.get(addr, "XG %02X %02X %02X" % addr)] = ("received" if addr == (0, 0, 0x7E) else hexs(m[7:-1]))
        elif m[1] == 0x43 and len(m) > 8 and m[3] == 0x27:
            self.panel_state["Master tune (Yamaha)"] = hexs(m[7:-1])
        elif m[:5] == [0xF0, 0x7E, 0x7F, 0x09, 0x01]:
            self.panel_state["GM System On"] = "received"

    def on_short(self, st, d1, d2):
        kind, ch = st & 0xF0, st & 15
        m = [st, d1, d2] if kind not in (0xC0, 0xD0) else [st, d1]
        if kind == 0x90 and d2:
            self.notes_down.add(d1)
        elif kind in (0x80, 0x90):
            self.notes_down.discard(d1)
        if not self.local_on and kind in (0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0) and not (kind == 0xB0 and d1 == 122):
            # Local is OFF, so the keyboard's own keys are silent until their MIDI comes back in: echo it.
            self.send([m], group=None, pauses=[0.0])
        self.events.append((time.time(), "IN ", describe(m)))
        if kind not in (0x80, 0x90) or (kind == 0x90 and d2):
            self.mon.append(("IN ", describe(m)))
        if kind == 0xB0:
            if d1 == 0:
                self.in_bank[ch][0] = d2
            elif d1 == 32:
                self.in_bank[ch][1] = d2
            elif d1 in (7, 10, 91, 93, 64):
                self.vals[(ch, d1)] = d2
            elif d1 == 122 and ch == 0:
                self.local_on = d2 >= 64
        elif kind == 0xC0:
            msb, lsb = self.in_bank[ch]
            no = (self.by_code.get((msb, lsb, d1)) or self.by_code.get((msb, 0, d1))
                  or self.by_code.get((0, 0, d1)))
            if no:
                self.part[ch] = no
                self.mon.append(("IN ", f"   -> {no:03d} {self.by_no[no]['name']}"))
                self.events.append((time.time(), "IN ", f"      -> voice {no:03d} {self.by_no[no]['name']}"))

    # ---- setups -------------------------------------------------------------------
    def build_sequence(self):
        msgs, pauses = [], []
        if self.reverb is not None:
            msgs.append(msg_reverb(self.reverb))
            pauses.append(0.02)
        if self.chorus is not None:
            msgs.append(msg_chorus(self.chorus))
            pauses.append(0.02)
        msgs.append(msg_tune(self.tune))
        pauses.append(0.02)
        for ch in range(16):
            msgs += self.voice_msgs(ch, self.part[ch])
            pauses += [0.0, 0.0, PC_SETTLE]
        for ch in range(16):
            for cc in (7, 10, 91, 93):
                msgs.append(msg_cc(ch, cc, self.get_cc(ch, cc)))
                pauses.append(0.0)
        msgs.append(msg_cc(0, 64, self.get_cc(0, 64)))
        pauses.append(0.0)
        msgs.append(msg_master(self.master_vol))
        pauses.append(0.0)
        return msgs, pauses

    def send_all(self, _b=1):
        msgs, pauses = self.build_sequence()
        self.send(msgs, group="load", pauses=pauses)
        self.set_status("Sent every voice, then the mixer, then the master volume.")

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
        d = {"version": 1, "name": name, "voices": self.part, "master": self.master_vol,
             "reverb": self.reverb, "chorus": self.chorus, "tune": self.tune,
             "vals": {f"{ch}:{cc}": v for (ch, cc), v in self.vals.items()}}
        (SOUNDS_DIR / f"{name}.json").write_text(json.dumps(d, indent=1))
        self.refresh_sounds()
        self.sound_off = 0
        self.set_status(f"Saved setup '{name}'.")

    def load_sound(self, name):
        try:
            d = json.loads((SOUNDS_DIR / f"{name}.json").read_text())
            self.reset_state()
            self.part = [n if n in self.by_no else 1 for n in d["voices"]][:16]
            self.master_vol = int(d.get("master", 127))
            rv, ch_, tn = d.get("reverb"), d.get("chorus"), d.get("tune", 0)
            self.reverb = rv if isinstance(rv, int) and 0 <= rv < len(REVERBS) else None
            self.chorus = ch_ if isinstance(ch_, int) and 0 <= ch_ < len(CHORUSES) else None
            self.tune = max(-100, min(100, int(tn)))
            for k, v in d["vals"].items():
                ch, cc = map(int, k.split(":"))
                self.vals[(ch, cc)] = max(0, min(127, int(v)))
        except Exception as ex:
            self.set_status(f"Could not load '{name}': {ex}")
            return
        self.textbox.text = name
        self.send_all()
        self.set_status(f"Loaded '{name}'.")

    def export_mid(self, _b=1):
        EXPORT_DIR.mkdir(exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9 _.-]", "_", self.textbox.text).strip() or "psr_setup"
        msgs, pauses = self.build_sequence()
        p = EXPORT_DIR / f"{name}.mid"
        p.write_bytes(smf_bytes(msgs, pauses))
        self.set_status(f"Exported exports/{p.name}: put it on a MIDI track routed to the keyboard.")

    def save_log(self, _b=1):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            t0 = self.events[0][0] if self.events else 0
            for t, d, s in self.events:
                f.write(f"{t - t0:9.3f}  {d}  {s}\n")
            f.write("\nPANEL STATE\n")
            for k, v in self.panel_state.items():
                f.write(f"  {k}: {v}\n")
        self.set_status(f"Saved {LOG_FILE.name} ({len(self.events)} events).")

    def clear_log(self, _b=1):
        self.events.clear()
        self.mon.clear()
        self.panel_state.clear()

    # ---- favourites --------------------------------------------------------------------
    def fav_click(self, i, b):
        if b == 1:
            self.set_voice(0, self.favs[i])
        else:
            cur = self.part[0]
            self.favs[i] = cur
            self.save_settings()
            self.set_status(f"Favourite {i + 1} = {cur:03d} {self.by_no[cur]['name']}")

    # ---- UI construction --------------------------------------------------------------------
    def build_ui(self):
        self.top_btns = [
            Button((330, 6, 400, 26), "", lambda b: self.choose_device("out", b)),
            Button((330, 38, 400, 26), "", lambda b: self.choose_device("in", b)),
            Button((1010, 12, 170, 44), "SEND ALL", self.send_all, big=True),
            Button((1195, 12, 175, 44), "ALL NOTES OFF", self.panic, big=True),
        ]
        self.out_btn, self.in_btn = self.top_btns[:2]
        self.tab_btns = [Button((30 + i * 150, 82, 140, 30), t, lambda b, i=i: self.set_tab(i)) for i, t in enumerate(TABS)]
        self.main_voice = VoiceButton(0, "Main voice (channel 1)", (30, 124, 700, 70))
        self.drum_voice = VoiceButton(DRUM_CH, "Drum kit (channel 10)", (750, 124, 620, 70), kits_only=True)
        self.main_knobs = [Knob2(0, cc, lab, 95 + i * 105, 296) for i, (cc, lab) in
                           enumerate([(7, "Volume"), (10, "Pan"), (91, "Reverb"), (93, "Chorus")])]
        self.main_toggles = [Toggle2(64, "Sustain", 575, 292, 110), Toggle2(122, "Local", 575, 358, 110)]
        self.master_knob = MasterKnob(0, 0, "Master Vol", 797, 296)
        self.main_btns = [
            Button((880, 250, 230, 40), "GM + XG RESET", self.reset_gmxg),
            Button((1130, 250, 220, 40), "READ PANEL...", self.read_panel_help),
            Button((880, 306, 230, 40), "SAVE LOG", self.save_log),
            Button((1130, 306, 220, 40), "AUDITION CH", self.cycle_aud),
            Button((880, 358, 470, 40), "PLAY FROM", self.choose_ctls),
        ]
        self.keys = OnScreenKeys(52, 590)
        self.fav_rects = [pygame.Rect(48 + (i % 6) * 220, 462 + (i // 6) * 42, 208, 34) for i in range(12)]
        self.mix_widgets = []
        self.mix_voice = []
        for i in range(16):
            x = 40 + i * 83
            self.mix_voice.append(VoiceButton(i, "", (x + 2, 176, 78, 52), small=True))
            for j, (cc, lab) in enumerate([(10, "Pan"), (91, "Rev"), (93, "Cho")]):
                self.mix_widgets.append(Knob2(i, cc, lab, x + 41, 268 + j * 72, small=True))
            self.mix_widgets.append(Fader(i, 7, x + 41, 486, 226))
        self.fx_widgets = []
        for k in range(len(REVERBS)):
            col, row = divmod(k, 5)
            self.fx_widgets.append(FxChoice((50 + col * 320, 164 + row * 56, 300, 46), REVERBS[k][0],
                                            lambda a, k=k: a.reverb == k, lambda a, k=k: a.set_reverb(k)))
        for k in range(len(CHORUSES)):
            col, row = (0, k) if k < 3 else (1, k - 3)
            self.fx_widgets.append(FxChoice((730 + col * 320, 164 + row * 56, 300, 46), CHORUSES[k][0],
                                            lambda a, k=k: a.chorus == k, lambda a, k=k: a.set_chorus(k)))
        self.tune_slider = TuneSlider(430, 540, 560)
        self.fx_widgets.append(self.tune_slider)
        for j, (lab, d) in enumerate([("-10", -10), ("-1", -1), ("0", None), ("+1", 1), ("+10", 10)]):
            self.fx_widgets.append(FxStep((430 + 20 + j * 108, 604, 96, 32), lab, d))
        self.mon_btns = [Button((960, 130, 190, 36), "SAVE LOG", self.save_log),
                         Button((1170, 130, 190, 36), "CLEAR", self.clear_log)]
        self.textbox = TextBox((120, 806, 230, 32))
        self.save_btn = Button((360, 806, 84, 32), "SAVE", self.save_sound)
        self.mid_btn = Button((452, 806, 100, 32), "EXPORT MID", self.export_mid)
        self.arrow_l = Button((650, 806, 32, 32), "<", lambda b: self.page_sounds(-1))
        self.arrow_r = Button((1316, 806, 32, 32), ">", lambda b: self.page_sounds(+1))
        self.chips = []

    def set_tab(self, i):
        self.tab = i

    def page_sounds(self, d):
        self.sound_off = max(0, min(max(0, len(self.sounds) - 1), self.sound_off + d))

    def _make_bg(self):
        import random
        bg = pygame.Surface((W, H))
        for y in range(H):
            t = y / H
            sheen = max(0.0, 1.0 - abs(t - 0.10) * 6.0) * 22
            col = (int(8 + 12 * (1 - t) + sheen * 0.7), int(5 + 7 * (1 - t) + sheen * 0.35), int(12 + 18 * (1 - t) + sheen * 1.0))
            pygame.draw.line(bg, col, (0, y), (W, y))
        rnd = random.Random(11)
        brush = pygame.Surface((W, H), pygame.SRCALPHA)
        for _ in range(2200):
            y = rnd.randrange(H); x = rnd.randrange(W); ln = rnd.randrange(80, 500)
            c = (255, 255, 255, rnd.randrange(2, 5)) if rnd.random() < 0.5 else (0, 10, 10, rnd.randrange(3, 8))
            pygame.draw.line(brush, c, (x, y), (x + ln, y))
        bg.blit(brush, (0, 0))
        return bg

    # ---- events ---------------------------------------------------------------------
    def tab_widgets(self):
        if self.tab == 0:
            return ([self.main_voice, self.drum_voice, self.master_knob, self.keys] + self.main_knobs +
                    self.main_toggles + self.main_btns)
        if self.tab == 1:
            return self.mix_voice + self.mix_widgets
        if self.tab == 2:
            return self.fx_widgets
        if self.tab == 3:
            return self.mon_btns
        return []

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
            if self.tab == 0:
                self.keys.handle(e, self)
            return
        if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE and not self.textbox.focus:
            self.running = False
            return
        if self.textbox.handle(e, self):
            return
        if self.tab == 4 and e.type == pygame.MOUSEWHEEL:
            self.help_scroll = max(0, min(max(0, len(self.help_lines) * 24 + 60 - 610), self.help_scroll - e.y * 40))
            return
        widgets = list(self.top_btns) + list(self.tab_btns) + [self.save_btn, self.mid_btn, self.arrow_l, self.arrow_r]
        widgets += self.tab_widgets()
        for w in widgets:
            if w.handle(e, self):
                return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            for r, name in self.chips:
                if r.collidepoint(e.pos) and e.button == 1:
                    self.load_sound(name)
                    return
            if self.tab == 0:
                for i, r in enumerate(self.fav_rects):
                    if r.collidepoint(e.pos):
                        self.fav_click(i, e.button)
                        return

    # ---- drawing ----------------------------------------------------------------------
    def status_line(self, s, pos, width=480):
        if time.time() - self.status_t < 25:
            for i, ln in enumerate(wrap(self.status, 12, width)[:3]):
                txt(s, ln, 12, (210, 187, 240), (pos[0], pos[1] + i * 15))

    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        now = time.time()
        txt(s, "PSR-E223", 40, (236, 250, 247), (30, 8), bold=True)
        txt(s, "M I D I   C O N T R O L   S U R F A C E", 11, (190, 168, 214), (32, 52))
        out_name = (self.settings.get("out_name") or "none  (click to choose)").replace("#0", "")
        in_name = (self.settings.get("in_name") or "none  (click to choose)").replace("#0", "")
        self.out_btn.draw(s, self, f"MIDI OUT:  {out_name}"[:50])
        self.in_btn.draw(s, self, f"MIDI IN:   {in_name}"[:50])
        led(s, (752, 19), now - self.flash_out < 0.12, AMBER)
        led(s, (752, 51), now - self.flash_in < 0.12, GREEN)
        txt(s, f"{len(self.notes_down)} keys down" if self.notes_down else "", 12, DIM, (770, 28))
        self.top_btns[2].draw(s, self)
        self.top_btns[3].draw(s, self)
        for i, b in enumerate(self.tab_btns):
            if i == self.tab:
                pygame.draw.rect(s, (56, 21, 88), b.rect, border_radius=6)
                pygame.draw.rect(s, AMBER, b.rect, 1, border_radius=6)
                txt(s, b.label, 14, (255, 255, 255), b.rect.center, "center", bold=True)
            else:
                b.draw(s, self)
        [self.draw_main, self.draw_mixer, self.draw_fx, self.draw_monitor, self.draw_help][self.tab](s)

        panel(s, (30, 796, 1340, 54), "")
        txt(s, "SETUP", 13, AMBER_DIM, (50, 814), bold=True)
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
            txt(s, "no saved setups yet", 13, (145, 128, 160), (690, 822), "midleft")
        if self.popup:
            self.popup.draw(s, self)

    def draw_main(self, s):
        self.main_voice.draw(s, self)
        self.drum_voice.draw(s, self)
        for rect, title in [((30, 210, 700, 206), "Main voice controls"), ((750, 210, 620, 206), "Master / system"),
                            ((30, 426, 1340, 116), "Favourite voices   (right-click = store the current main voice)"),
                            ((30, 552, 1340, 154), f"Keyboard   (audition channel {self.aud_ch + 1})"),
                            ((30, 714, 1340, 72), "Last messages")]:
            panel(s, rect, title)
        for k in self.main_knobs + [self.master_knob]:
            k.draw(s, self)
        for t in self.main_toggles:
            t.draw(s, self)
        for b in self.main_btns:
            lab = {"AUDITION CH": f"AUDITION CH  {self.aud_ch + 1}",
                   "PLAY FROM": "PLAY FROM:  " + (", ".join(n.replace("#0", "") for n in self.settings.get("ctl_names", []))
                                                  or "other MIDI devices...  (click to add)")}.get(b.label)
            b.draw(s, self, lab)
        if not self.local_on:
            txt(s, "Local OFF: keys are echoed back via this app", 12, AMBER, (440, 394), bold=True)
        mouse = pygame.mouse.get_pos()
        for i, r in enumerate(self.fav_rects):
            no = self.favs[i]
            v = self.by_no.get(no)
            cur = self.part[0] == no
            pygame.draw.rect(s, (56, 21, 88) if cur else (BTN_HOT if r.collidepoint(mouse) else BTN), r, border_radius=6)
            pygame.draw.rect(s, AMBER if cur else BTN_EDGE, r, 1, border_radius=6)
            txt(s, f"{no:03d}", 11, AMBER_DIM if not cur else (220, 197, 255), (r.x + 8, r.centery), "midleft")
            txt(s, v["name"][:20] if v else "--", 14, (255, 255, 255) if cur else TEXT, (r.x + 42, r.centery), "midleft")
        self.keys.draw(s, self)
        for i, (d, line) in enumerate(list(self.mon)[-3:]):
            txt(s, f"{d} {line}"[:90], 13, AMBER if d == "OUT" else (150, 240, 190), (50, 738 + i * 15))
        self.status_line(s, (760, 738), 580)

    def draw_mixer(self, s):
        panel(s, (30, 124, 1340, 660), "Parts  (all 16 channels)")
        for i in range(16):
            x = 40 + i * 83
            label = f"{i + 1:02d}" + (" DRUM" if i == DRUM_CH else "")
            txt(s, label, 13, AMBER if i == self.aud_ch else TEXT, (x + 41, 160), "center", bold=True)
            if i > 0:
                pygame.draw.line(s, (56, 39, 62), (x - 1, 150), (x - 1, 770))
        for w in self.mix_voice + self.mix_widgets:
            w.draw(s, self)
        for i in range(16):
            x = 40 + i * 83
            txt(s, str(self.get_cc(i, 7)), 13, AMBER, (x + 41, 738), "center", bold=True)
            txt(s, "VOL", 10, DIM, (x + 41, 756), "center")

    def draw_fx(self, s):
        panel(s, (30, 124, 660, 336), "Reverb type")
        panel(s, (710, 124, 660, 336), "Chorus / flanger type")
        panel(s, (30, 476, 1340, 190), "Master tuning")
        panel(s, (30, 682, 1340, 100), "About these settings")
        for w in self.fx_widgets:
            w.draw(s, self)
        if self.reverb is None:
            txt(s, "current type unknown until you pick one (or step it on the keyboard)", 12, DIM, (50, 436))
        if self.chorus is None:
            txt(s, "current type unknown until you pick one (or step it on the keyboard)", 12, DIM, (730, 436))
        note = ("Chorus 1 / 2 are the keyboard's two chorus types; Flanger 1 / 2 are the two flangers. "
                "The per-part Reverb and Chorus knobs (MAIN and MIXER) set how much of each part is sent into the effect chosen here.")
        for i, ln in enumerate(wrap(note, 13, 600)):
            txt(s, ln, 13, TEXT, (730, 340 + i * 20))
        lcd(s, (60, 514, 330, 66), f"A = {tune_hz(self.tune):.1f} Hz", 28)
        txt(s, f"{self.tune:+d} cents" if self.tune else "0 cents  (standard pitch)", 16, AMBER, (225, 604), "center", bold=True)
        txt(s, "drag the slider, use the wheel (Shift = 1 cent), or the buttons. Double-click = back to 0.", 12, DIM, (430, 648))
        for i, ln in enumerate(wrap("Drum kits ignore tuning. These controls send exactly what the keyboard's own Function menu sends "
                                    "(captured from your PSR-E223), so turning them on the keyboard moves them here too.", 13, 300)):
            txt(s, ln, 13, TEXT, (1050, 516 + i * 18))
        txt(s, "Every setting here is also in SEND ALL, SAVE and EXPORT MID.", 13, TEXT, (50, 706))
        txt(s, "Open MIDI IN and step Reverb / Chorus / Tuning on the keyboard to see the buttons follow.", 13, TEXT, (50, 728))
        self.status_line(s, (760, 706), 580)

    def draw_monitor(self, s):
        panel(s, (30, 124, 900, 660), "Everything the keyboard sends")
        panel(s, (950, 210, 420, 574), "Panel state (from InitSend)")
        self.mon_btns[0].draw(s, self)
        self.mon_btns[1].draw(s, self)
        txt(s, "IN = from the keyboard   OUT = from this app", 12, DIM, (960, 180))
        evs = list(self.events)[-34:]
        t0 = self.events[0][0] if self.events else 0
        for i, (t, d, line) in enumerate(evs):
            txt(s, f"{t - t0:8.2f}  {d} {line}"[:100], 13, AMBER if d == "OUT" else (150, 240, 190), (50, 156 + i * 17))
        if not evs:
            txt(s, "Nothing yet. Choose MIDI IN, then play the keyboard.", 14, DIM, (50, 160))
        y = 246
        if not self.panel_state:
            for i, ln in enumerate(wrap("Press FUNCTION on the keyboard until InitSend shows, then press +. The settings it sends appear here.", 13, 390)):
                txt(s, ln, 13, DIM, (970, y + i * 18))
        for k, v in self.panel_state.items():
            txt(s, k, 12, AMBER_DIM, (970, y))
            txt(s, v[:48], 13, TEXT, (970, y + 15))
            y += 38

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
            self.poll_ctls()
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
