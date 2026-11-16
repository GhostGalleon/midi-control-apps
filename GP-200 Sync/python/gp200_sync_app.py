#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
GP-200 SYNC  -  patch table + MIDI Captain key-state sync for the Valeton GP-200.

Run:      python gp200_sync_app.py
Needs:    pip install pygame

The MIDI Captain does not know which effect blocks a GP-200 patch has switched on.  Firmware 5 of the Captain accepts
"External MIDI Control": CC 20 = jump to page, CC 21-30 (value 1-10) = set a key's press-count so its LED and its next
press match.  This app keeps a table of which GP-200 modules (CC48-57) each patch has on, watches the patch changes
passing between the Captain and the GP-200, and sends the matching key-state messages to the Captain.
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
SETTINGS_FILE = BASE / "gp200_settings.json"
DATA_FILE = BASE / "gp200_table.json"
DEFAULTS_FILE = BASE / "gp200_defaults.json"      # snapshot of the table taken at the start of the evening (SAVE ALL AS DEFAULTS)

W, H = 1400, 860
GAP = 0.004            # seconds between queued messages
DEFAULT_CH = 0         # MIDI channel 1 (0-based); the manual does not give a channel, so it is selectable

# ---- palette: graphite + mint (re-coloured to red at the end of the build) ----------------------
AMBER = (71, 235, 153)        # accent (name kept so the shared drawing code works)
AMBER_DIM = (31, 230, 130)
ORANGE = (255, 140, 30)
SOLO_RED = (235, 40, 40)
TEXT = (234, 246, 240)
DIM = (172, 150, 150)
PANEL = (49, 39, 39)
EDGE = (150, 122, 122)
LCD_BG = (11, 9, 9)
LCD_FG = (51, 255, 153)
LCD_GLOW = (14, 130, 72)
GREEN = (71, 235, 153)
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
    pygame.draw.line(surf, (238, 248, 243), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (240, 248, 244), (r.x + 16, r.y + 8), bold=True)
    if title:
        pygame.draw.line(surf, (72, 56, 56), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
        pygame.draw.line(surf, (71, 235, 153), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (28, 22, 22), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (202, 232, 217), (sx, r.bottom - 12), 4, 1)
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
        pygame.draw.line(surf, (230, 244, 237), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
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


PEDAL_LOOK = {   # name: (body, label plate, label text, knob fill, knob dot, knob layout)
    "NR":  ([205, 205, 205], [150, 150, 150], [60, 60, 60], [30, 30, 30], [240, 240, 240], [(0, -1)]),
    "PRE": ([150, 50, 200], [115, 30, 160], [255, 255, 255], [245, 245, 245], [150, 50, 200], [(0, -1)]),
    "DST": ([215, 55, 75], [175, 35, 55], [255, 255, 255], [245, 245, 245], [215, 55, 75], [(-1, -1), (1, -1), (0, 0)]),
    "N>S": ([115, 55, 205], [80, 35, 160], [255, 255, 255], [245, 245, 245], [115, 55, 205], [(-1, -1), (0, -1), (1, -1), (-0.5, 0), (0.5, 0)]),
    "MOD": ([45, 185, 215], [30, 150, 180], [255, 255, 255], [245, 245, 245], [45, 185, 215], [(-1, -1), (1, -1), (0, 0)]),
    "DLY": ([215, 50, 130], [170, 35, 105], [255, 255, 255], [245, 245, 245], [215, 50, 130], [(-1, -1), (1, -1), (0, 0)]),
    "RVB": ([40, 105, 210], [25, 75, 160], [255, 255, 255], [245, 245, 245], [40, 105, 210], [(-0.6, -1), (0.6, -1), (-0.6, 0), (0.6, 0)]),
    "WAH": ([40, 40, 44], [20, 20, 22], [255, 200, 40], [255, 200, 40], [40, 40, 44], [(0, -1)]),
    "EQ":  ([215, 170, 105], [185, 135, 75], [40, 25, 10], None, None, []),
}


def _mix(c, f, bg=(24, 22, 20)):
    return tuple(int(bg[i] + (c[i] - bg[i]) * f) for i in range(3))


def draw_module_icon(s, name, cx, cy, f, tint=None):
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
        body, plate = (44, 232, 138), (21, 185, 103)
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
    if tint:                                          # use the colour from the loaded layout page
        body = list(tint); plate = tuple(int(c * 0.72) for c in body); kdot = body
        lum = 0.3 * body[0] + 0.59 * body[1] + 0.11 * body[2]
        ltxt = (0, 0, 0) if lum > 170 else (255, 255, 255)
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


STAGE_COL = {'PRE': [170, 70, 225], 'DST': [235, 65, 85], 'AMP': [215, 175, 115], 'NR': [225, 225, 225], 'CAB': [170, 170, 170],
             'EQ': [225, 180, 110], 'MOD': [55, 200, 230], 'DLY': [235, 60, 145], 'RVB': [55, 125, 235], 'WAH': [255, 200, 40]}

STAGE_BRIGHTEN = ('DLY',)        # modules whose layout colour is lifted on the STAGE page; add names here to brighten others
# =============================================================================
# FACTS  (GP-200 manual "MIDI Control Information List"; MIDI Captain FW5.0 command specification, section 11)
# =============================================================================
MODULES = [("PRE", 48), ("DST", 49), ("AMP", 50), ("NR", 51), ("CAB", 52), ("EQ", 53), ("MOD", 54), ("DLY", 55), ("RVB", 56), ("WAH", 57)]
MOD_NAME = {cc: n for n, cc in MODULES}
# fixed look for some modules (lists, so the build's colour re-mapper leaves them alone):  fill, ring
MOD_STYLE = {50: ([0, 0, 0], [110, 110, 110]),          # AMP: black
             52: ([64, 64, 64], [150, 150, 150]),       # CAB: dark grey
             51: ([0, 0, 0], [255, 255, 255]),          # NR: black, white ring
             57: ([0, 0, 0], [255, 200, 40])}           # WAH: black, gold ring
KEYS = ["key1", "key2", "key3", "key4", "key5", "keyA", "keyB", "keyC", "keyD", "keyE"]
KEY_LABEL = ["1", "2", "3", "4", "5", "A", "B", "C", "D", "E"]
KEY_CC = {k: 21 + i for i, k in enumerate(KEYS)}           # CC21-30 set the press-count of key1..key5, keyA..keyE
PRESS_KEYS = ("short_dw", "short_up", "long", "long_up")
BANK_CC0 = {0: 0, 1: 128}                                   # GP-200 as tested on the real unit: CC0=0 -> patches 01-A..32-D (PC 0-127), CC0=1 -> 33-A..64-D (the manual says the opposite)
N_PATCH = 256


def patch_label(n):
    return f"{n // 4 + 1:02d}-{'ABCD'[n % 4]}"


def describe(m):
    k, ch = m[0] & 0xF0, (m[0] & 0x0F) + 1
    if k == 0xB0:
        nm = ""
        if m[1] == 0:
            nm = "bank (GP-200: 1 = patches 01-32, 0 = 33-64)"
        elif m[1] in MOD_NAME:
            nm = f"{MOD_NAME[m[1]]} module {'ON' if m[2] >= 64 else 'OFF'}"
        elif m[1] == 20:
            nm = "Captain: jump to page"
        elif 21 <= m[1] <= 30:
            nm = f"Captain: key {KEY_LABEL[m[1] - 21]} press-count"
        return f"ch{ch:>2}  CC{m[1]:>3} = {m[2]:>3}  {nm}"
    if k == 0xC0:
        return f"ch{ch:>2}  program change {m[1]}"
    return " ".join(f"{b:02X}" for b in m)


def load_layout(path):
    """Read an FW5 Captain page file.  -> (page name, {keyname: info}, notes)
    info = dict(cc=module CC, times=keytimes, on_next=value to send so the key shows ON, off_next=value so it shows OFF)"""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    name, cur, secs = "", None, {}
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^\[([A-Za-z0-9_.]+)\]$", s)
        if m:
            cur = m.group(1)
            secs.setdefault(cur, [])
        elif "=" in s and cur:
            k, v = s.split("=", 1)
            if cur.lower() == "globalsetup" and k.strip() == "page_name":
                name = re.sub(r"[\[\]]", "", v).strip()
            secs[cur].append((k.strip(), v.strip()))
    out, notes = {}, []
    for k in KEYS:
        items = secs.get(k)
        if not items:
            continue
        times, levels, leds = 1, {}, {}
        cct = False
        for key, raw in items:
            if key == "keytimes":
                mm = re.search(r"\d+", raw)
                times = int(mm.group()) if mm else 1
            ml = re.match(r"^ledcolor(\d+)$", key)
            if ml:
                for gcol in re.findall(r"\[(0[xX][0-9A-Fa-f]{1,6})\]", raw):
                    try:
                        v = int(gcol, 16) & 0xFFFFFF
                    except ValueError:
                        continue
                    if v and int(ml.group(1)) not in leds:
                        leds[int(ml.group(1))] = ((v >> 16) & 255, (v >> 8) & 255, v & 255)
            mm = re.match(r"^(short_dw|short_up|long_up|long)(\d+)$", key)
            if mm:
                g = re.findall(r"\[([^\]]*)\]", raw)
                for i in range(0, len(g) - 3, 4):
                    ch, kind, num, val = g[i:i + 4]
                    if kind == "CCT" and num.isdigit() and int(num) in MOD_NAME:
                        cct = True
                    if kind == "CC" and num.isdigit() and int(num) in MOD_NAME and val.isdigit():
                        levels.setdefault(int(mm.group(2)), []).append((int(num), int(val)))
        if cct:
            notes.append(f"key {KEY_LABEL[KEYS.index(k)]}: uses CCT (a hidden on/off toggle) - cannot be synced, give it keytimes 2 with ON and OFF commands")
        if not levels:
            continue
        ccs = {c for lv in levels.values() for c, _ in lv}
        if len(ccs) != 1:
            notes.append(f"key {KEY_LABEL[KEYS.index(k)]}: sends several module CCs - skipped")
            continue
        cc = ccs.pop()
        on = [n for n in sorted(levels) if any(v >= 64 for _, v in levels[n])]
        off = [n for n in sorted(levels) if any(v < 64 for _, v in levels[n])]
        if not on or not off:
            notes.append(f"key {KEY_LABEL[KEYS.index(k)]} ({MOD_NAME[cc]}): has no ON or no OFF press - cannot be synced")
            continue
        out[k] = dict(cc=cc, times=times, on_next=(on[0] % times) + 1, off_next=(off[0] % times) + 1,
                      color=leds.get(on[0]) or next(iter(leds.values()), None))
    return name, out, notes
# =============================================================================
# THE APP
# =============================================================================
TABS = ["SYNC", "PATCHES", "TEST", "MONITOR", "HELP", "STAGE", "RECALL"]

HELP_TEXT = [
    ("H", "What this is for"),
    ("T", "The MIDI Captain does not know which effects a GP-200 patch has switched on, so its key lights can disagree with the GP-200. "
          "FW5 of the Captain can be told over MIDI IN what state each key is in (CC 21-30). This app keeps a table of the effects each patch uses "
          "and sends those messages whenever you change patch."),
    ("H", "Cabling"),
    ("T", "Captain MIDI OUT -> interface MIDI IN (the app listens to the same patch changes the GP-200 gets), and interface MIDI OUT -> Captain MIDI IN. "
          "The Captain can pass on to the GP-200 whatever reaches its MIDI IN (MIDI Through), and on a Captain-to-GP-200 cable the GP-200 would read CC 20-30 "
          "as Quick Access knobs and bank / patch up and down. To keep them away from the GP-200 use a control channel the GP-200 does not listen to: "
          "in the Captain's page set externalmidicontrol = [on] and externalmidicontrolch = [16], and on the GP-200 set the MIDI input channel to a "
          "fixed channel (for example 1) instead of Omni, or switch midithrough off on the page."),
    ("H", "SYNC tab"),
    ("T", "LOAD LAYOUT reads your FW5 page file (for example the page that has your GP-200 stomp keys). The app finds each key that sends a module on/off CC "
          "(CC48-57) with an ON press and an OFF press, and works out which press-count shows ON or OFF. The Captain's LEDs for those keys then follow the table. "
          "AUTO SYNC watches MIDI IN for the GP-200 bank (CC0) and program change and syncs a short moment later. SYNC NOW sends it for the current patch."),
    ("T", "Keys that use CCT (a hidden toggle), have several module CCs, or only one press are listed as 'cannot be synced'. Give them keytimes 2 with a separate ON and OFF command."),
    ("H", "PATCHES tab"),
    ("T", "256 patches, 01-A to 64-D. Select one, click its effect buttons to cycle  not set / ON / OFF, and give it a name. "
          "With LEARN on, any module CC your Captain sends while that patch is current is written into the table, so you can set up a patch by pressing "
          "its keys on the Captain. Patches with a table entry show a dot."),
    ("H", "RECALL tab"),
    ("T", "The GP-200 does not remember stomp changes when you leave a patch. The table (learned from the Captain's key presses) does. With RESTORE AFTER EVERY PATCH CHANGE on, the app sends the last-known module CCs (CC48-57, 127 = on, 0 = off) to the GP-200 after the sync delay, through the Captain's MIDI Through. Only modules the table knows about are sent."),
    ("T", "SAVE ALL AS DEFAULTS copies the whole table to gp200_defaults.json: do it at the start of the evening, once the patches are how you want them. When the Captain sends the ENCODER RESET CC (the encoder push, encoderkeyCCtoggle) the present patch is put back to its saved default: the table is replaced, the states are sent to the GP-200 and the Captain's LEDs are synced. RESET TO DEFAULT NOW does the same from the screen. Pick a reset CC the GP-200 ignores."),
    ("H", "TEST tab"),
    ("T", "Sends the Captain external-control messages by hand: jump to page N (CC20), set a key's press-count (CC21-30, value 1-10), or press a key "
          "(CC1-10, value 1-4 = down, long, up, long up). Use it to check your Captain responds before relying on the sync."),
    ("H", "Notes"),
    ("T", "Patch numbers follow what the GP-200 really does (tested on the unit): CC0 = 0 selects patches 01-A to 32-D (PC 0-127), CC0 = 1 selects 33-A to 64-D. The manual says the opposite. The table is saved in gp200_table.json."),
]

LEARN_NOTE = "Learn: the table for this patch was updated from the Captain."


class App:
    def __init__(self, headless=False, tab=0):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("GP-200 SYNC")
        self.tab, self.running, self.popup, self.drag = tab, True, None, None
        self._hits, self.hits = [], []
        self.events = deque(maxlen=3000)
        self.counts = {"in": 0, "out": 0}
        self.flash_out = self.flash_in = 0.0
        self.status = "Choose MIDI OUT (to the Captain) and MIDI IN at the top, then LOAD LAYOUT."
        self.status_t = time.time()
        self.out = self.inp = None
        self.out_lock = threading.Lock()
        self.settings, self.data = {}, {}
        for f, attr in ((SETTINGS_FILE, "settings"), (DATA_FILE, "data")):
            try:
                setattr(self, attr, json.loads(f.read_text()))
            except Exception:
                pass
        try:
            self.defaults = json.loads(DEFAULTS_FILE.read_text())
        except Exception:
            self.defaults = {}
        self.defaults.setdefault("patches", {})
        self.table = self.data.setdefault("patches", {})           # "17": {"name": "...", "mods": {"48": true}}
        self.ctl_ch = clamp(int(self.settings.get("ctl_ch", 15)), 0, 15)
        self.delay = clamp(int(self.settings.get("delay", 300)), 0, 2000)
        self.listen = clamp(int(self.settings.get("listen", -1)), -1, 15)
        self.gap = clamp(int(self.settings.get("gap", 30)), 0, 300)
        self.auto = bool(self.settings.get("auto", True))
        self.learn = bool(self.settings.get("learn", True))
        self.jump = self.settings.get("jump")                       # optional Captain page to jump to after a patch change
        self.restore = bool(self.settings.get("restore", True))      # send the last-known stomp states to the GP-200 after a patch change
        self.reset_cc = clamp(int(self.settings.get("reset_cc", 40)), -1, 127)   # Captain CC (encoder push) that sends the patch back to its default; -1 = off
        self.gp_ch = clamp(int(self.settings.get("gp_ch", DEFAULT_CH)), 0, 15)    # channel the module CCs are sent to the GP-200 on
        self.echo_until = 0.0                                       # ignore our own restore messages coming back round for a moment
        self.last_reset_t = 0.0
        self.recall_note = ""
        self.confirm = None
        self.layout_path = self.settings.get("layout", "")
        self.layout_name, self.layout, self.layout_notes = "", {}, []
        self.reload_layout(quiet=True)
        self.bank = 0
        self.cur = 0                                                # current GP-200 patch 0-255
        self.sel = 0                                                # patch selected in the PATCHES tab
        self.bank_scroll = 0
        self.raw_count, self.last_raw, self.in_error = 0, "", ""
        self.pending = None                                         # (time, patch) for delayed auto sync
        self.last_sync = ""
        self.live_mods = {}                                         # module CC -> bool, from what the Captain has sent
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
        self.popup = Popup("MIDI OUT (to the Captain's MIDI IN)" if kind == "out" else "MIDI IN (Captain MIDI OUT / GP-200 traffic)",
                           opts, self.settings.get(kind + "_name"), pick)

    def save_settings(self):
        self.settings.update(ctl_ch=self.ctl_ch, delay=self.delay, listen=self.listen, gap=self.gap, auto=self.auto, learn=self.learn, layout=self.layout_path, jump=self.jump, restore=self.restore, reset_cc=self.reset_cc, gp_ch=self.gp_ch)
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

    # ---- layout ------------------------------------------------------------------------------
    def reload_layout(self, quiet=False):
        if not self.layout_path:
            return
        try:
            self.layout_name, self.layout, self.layout_notes = load_layout(self.layout_path)
            if not quiet:
                self.set_status(f"Loaded layout '{self.layout_name}': {len(self.layout)} key(s) can be synced.")
        except Exception as ex:
            self.layout, self.layout_notes = {}, [f"Could not read {self.layout_path}: {ex}"]
            if not quiet:
                self.set_status(self.layout_notes[0])

    def ask_layout(self, _b=1):
        def done(path):
            path = path.strip().strip('"')
            if path:
                self.layout_path = path
                self.reload_layout()
                self.save_settings()
        self.popup = TextPrompt("Load layout", "Path to the Captain page file (FW5 pageN.txt):", self.layout_path, done)

    # ---- sending -----------------------------------------------------------------------------
    def cc_ctl(self, num, val, group=None):
        self.send([[0xB0 | self.ctl_ch, num, val]], group=group)

    def sync_msgs(self, p):
        e = self.table.get(str(p)) or {}
        out = []
        mods = {int(k): v for k, v in e.get("mods", {}).items()}
        for k, info in self.layout.items():
            st = bool(mods.get(info["cc"], False))          # a module that is not in the table counts as OFF
            out.append((k, info["cc"], st, info["on_next"] if st else info["off_next"]))
        return out

    def sync_now(self, p=None, _b=1):
        p = self.cur if p is None else p
        msgs = self.sync_msgs(p)
        if not self.layout:
            self.set_status("Load a layout first (LOAD LAYOUT).")
            return
        skipped = []
        if not msgs:
            self.last_sync = f"{patch_label(p)}: nothing to send - the layout has no keys that can be synced"
            self.set_status(self.last_sync)
            return
        gap = self.gap / 1000.0
        if self.jump is not None:
            self.cc_ctl(20, int(self.jump))
            self.send([max(gap, 0.08)])
        for k, cc, st, val in msgs:
            self.cc_ctl(KEY_CC[k], val)
            self.send([gap])
        self.last_sync = f"{patch_label(p)}: " + "  ".join(f"{KEY_LABEL[KEYS.index(k)]}={MOD_NAME[cc]} {'ON' if st else 'OFF'}" for k, cc, st, _v in msgs)
        if skipped:
            self.last_sync += "   |  skipped: " + ", ".join(skipped)
        self.set_status("Synced " + self.last_sync)

    def jump_page(self, n, _b=1):
        self.cc_ctl(20, n)

    def set_keytimes(self, key_idx, v, _b=1):
        self.cc_ctl(21 + key_idx, v)

    def press_key(self, key_idx, kind, _b=1):
        self.cc_ctl(1 + key_idx, kind)

    def set_ctl(self, d, _b=1):
        self.ctl_ch = clamp(self.ctl_ch + d, 0, 15)
        self.save_settings()

    def set_gap(self, d, _b=1):
        self.gap = clamp(self.gap + d, 0, 300)
        self.save_settings()

    def set_listen(self, d, _b=1):
        v = clamp(self.listen + 1 + d, 0, 16)          # 0 = omni, 1-16 = channel
        self.listen = v - 1
        self.save_settings()

    def mod_style(self, cc):
        """-> (fill, ring) as tuples, or None for the default look.  Fixed styles first, then the layout key's colour."""
        if cc in MOD_STYLE:
            f, r = MOD_STYLE[cc]
            return tuple(f), tuple(r)
        for info in self.layout.values():
            if info["cc"] == cc and info.get("color"):
                return info["color"], info["color"]
        return None

    def set_delay(self, d, _b=1):
        self.delay = clamp(self.delay + d, 0, 2000)
        self.save_settings()

    def toggle(self, attr, _b=1):
        setattr(self, attr, not getattr(self, attr))
        self.save_settings()

    def sound_off(self, _b=1):
        self.send([[0xB0 | self.ctl_ch, 120, 0]])

    # ---- incoming MIDI --------------------------------------------------------------------------
    def poll_in(self):
        if self.pending and time.time() >= self.pending[0]:
            p = self.pending[1]
            self.pending = None
            if self.restore:
                self.restore_to_unit(p, "restored")
            if self.auto:
                self.sync_now(p)
        if not self.inp:
            return
        try:
            if not self.inp.poll():
                return
            events = self.inp.read(256)
        except Exception as ex:
            self.set_status(f"MIDI IN read error: {ex}")
            self.in_error = str(ex)
            return
        for data, _ts in events:
            self.raw_count += 1
            self.last_raw = " ".join(f"{x:02X}" for x in data)
            self.feed(data)

    def feed(self, data):
        b0 = data[0]
        if b0 < 0x80 or b0 >= 0xF0:
            return
        if (b0 & 0xF0) == 0xB0 and len(data) >= 3 and self.reset_cc >= 0 and data[1] == self.reset_cc and data[1] not in MOD_NAME:
            # the encoder push: accepted on ANY channel, whatever LISTEN CHANNEL says (it is usually sent on the control channel)
            now = time.time()
            self.flash_in = now
            self.counts["in"] += 1
            self.log("IN ", describe([b0, data[1], data[2]]))
            if now < self.echo_until:
                return
            if now - self.last_reset_t > 0.5:               # the push sends 127 then 0 alternately, so act on every press
                self.last_reset_t = now
                self.reset_patch(self.cur)
            return
        if self.listen >= 0 and (b0 & 0x0F) != self.listen:
            return                                           # not the channel we were told to listen to
        kind = b0 & 0xF0
        m = [b0, data[1]] if kind in (0xC0, 0xD0) else [b0, data[1], data[2]]
        if (b0 & 0x0F) == self.ctl_ch and kind == 0xB0 and 20 <= m[1] <= 30:
            return                                           # our own control traffic coming back around
        self.flash_in = time.time()
        self.counts["in"] += 1
        self.log("IN ", describe(m))
        now = time.time()
        if kind == 0xB0 and m[1] == 0:
            self.bank = m[2]
        elif kind == 0xC0 and m[1] < 128:
            self.cur = BANK_CC0.get(self.bank, 0) + m[1]
            self.sel = self.cur
            self.live_mods = {}
            self.pending = (time.time() + self.delay / 1000.0, self.cur)
        elif kind == 0xB0 and m[1] in MOD_NAME and now >= self.echo_until:
            on = m[2] >= 64
            self.live_mods[m[1]] = on
            if (b0 & 0x0F) != self.gp_ch:
                self.gp_ch = b0 & 0x0F                       # follow the channel the Captain really sends stomp CCs on
                self.save_settings()
            if self.learn:
                e = self.table.setdefault(str(self.cur), {"name": "", "mods": {}})
                e.setdefault("mods", {})[str(m[1])] = on
                self.save_data()

    # ---- recall: last-known stomp states and per-patch defaults -------------------------------------
    def restore_to_unit(self, p, why="restored", mods=None):
        """Send the module CCs we know for patch p to the GP-200 (through the Captain's MIDI Through)."""
        if mods is None:
            mods = (self.table.get(str(p)) or {}).get("mods", {})
        items = sorted((int(k), bool(v)) for k, v in mods.items() if int(k) in MOD_NAME)
        if not items:
            self.recall_note = f"{patch_label(p)}: nothing known yet, so nothing was sent to the GP-200."
            return 0
        gap = max(self.gap, 20) / 1000.0
        for cc, on in items:
            self.send([[0xB0 | self.gp_ch, cc, 127 if on else 0]])
            self.send([gap])
        self.echo_until = time.time() + len(items) * (gap + 0.01) + 0.6
        self.live_mods = {cc: on for cc, on in items}
        self.recall_note = f"{patch_label(p)} {why}: " + "  ".join(f"{MOD_NAME[c]} {'ON' if v else 'OFF'}" for c, v in items)
        self.set_status("Sent to GP-200 - " + self.recall_note)
        return len(items)

    def restore_now(self, _b=1):
        self.restore_to_unit(self.cur, "restored")

    def reset_patch(self, p, _b=1):
        """Put patch p back to its saved default: update the table, send it to the GP-200 and re-sync the Captain's LEDs."""
        d = self.defaults.get("patches", {}).get(str(p))
        if not d:
            self.recall_note = f"{patch_label(p)}: no default saved for this patch yet (SAVE THIS AS DEFAULT)."
            self.set_status(self.recall_note)
            return False
        cur = self.table.get(str(p)) or {}
        mods = {str(k): bool(v) for k, v in d.get("mods", {}).items()}
        for k in cur.get("mods", {}):
            mods.setdefault(str(k), False)                   # a module we know nothing about counts as OFF, as everywhere else in this app
        self.table[str(p)] = {"name": d.get("name") or cur.get("name", ""), "mods": mods}
        self.save_data()
        self.restore_to_unit(p, "reset to default", mods)
        if self.layout and self.auto:
            self.sync_now(p)
        return True

    def reset_now(self, _b=1):
        self.reset_patch(self.cur)

    def save_defaults_file(self):
        self.defaults["saved"] = time.strftime("%Y-%m-%d %H:%M")
        try:
            DEFAULTS_FILE.write_text(json.dumps(self.defaults, indent=1))
        except Exception as ex:
            self.set_status(f"Could not save defaults: {ex}")

    def save_default_one(self, _b=1):
        e = self.table.get(str(self.cur)) or {"name": "", "mods": {}}
        self.defaults["patches"][str(self.cur)] = json.loads(json.dumps(e))
        self.save_defaults_file()
        self.set_status(f"{patch_label(self.cur)}: its present state is now its default.")

    def save_defaults_all(self):
        self.defaults["patches"] = json.loads(json.dumps(self.table))
        self.save_defaults_file()
        self.set_status(f"Saved defaults for {len(self.defaults['patches'])} patches.")

    def ask(self, key, msg, fn, _b=1):
        if self.confirm and self.confirm[0] == key and time.time() - self.confirm[1] < 5:
            self.confirm = None
            fn()
        else:
            self.confirm = (key, time.time())
            self.set_status(msg + "  Click again within 5 seconds to confirm.")

    def set_reset_cc(self, d, _b=1):
        self.reset_cc = clamp(self.reset_cc + d, -1, 127)
        self.save_settings()

    def set_gp_ch(self, d, _b=1):
        self.gp_ch = clamp(self.gp_ch + d, 0, 15)
        self.save_settings()

    # ---- table ----------------------------------------------------------------------------------
    def save_data(self):
        try:
            DATA_FILE.write_text(json.dumps(self.data, indent=1))
        except Exception as ex:
            self.set_status(f"Could not save: {ex}")

    def entry(self, p, create=False):
        e = self.table.get(str(p))
        if e is None and create:
            e = self.table[str(p)] = {"name": "", "mods": {}}
        return e

    def cycle_mod(self, p, cc, _b=1):
        e = self.entry(p, True)
        e["mods"][str(cc)] = not bool(e["mods"].get(str(cc), False))
        self.save_data()

    def clear_patch(self, _b=1):
        self.table.pop(str(self.sel), None)
        self.save_data()

    def ask_name(self, _b=1):
        def done(name):
            name = re.sub(r"[^\w .+#&'-]", "", name).strip()[:20]
            self.entry(self.sel, True)["name"] = name
            self.save_data()
        self.popup = TextPrompt(f"Name for {patch_label(self.sel)}", "Name:", (self.entry(self.sel) or {}).get("name", ""), done)

    def copy_from(self, _b=1):
        opts = [(str(p), f"{patch_label(p)}  {self.table[str(p)].get('name', '')}") for p in sorted(int(k) for k in self.table) if p != self.sel]
        if not opts:
            self.set_status("No other patch has a table entry yet.")
            return

        def pick(key):
            src = self.table[key]
            self.table[str(self.sel)] = {"name": (self.entry(self.sel) or {}).get("name", ""), "mods": dict(src.get("mods", {}))}
            self.save_data()
        self.popup = Popup("Copy effect states from", opts, None, pick)

    def goto_patch(self, p):
        self.sel = clamp(p, 0, N_PATCH - 1)
        self.bank_scroll = clamp(self.sel // 4 - 7, 0, 64 - 15)
    # ---- immediate-mode widgets ------------------------------------------------------------------
    def add_hit(self, rect, kind, a=None, b=None):
        self._hits.append((pygame.Rect(rect), kind, a, b))

    def btn(self, rect, label, cb, active=False, enabled=True, size=14):
        r = pygame.Rect(rect)
        hover = enabled and r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, (30, 23, 23), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(self.screen, (18, 163, 90) if active else (BTN_HOT if hover else BTN), r, border_radius=6)
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

    def draw_sync(self, s):
        panel(s, (30, 130, 700, 660), "Captain layout")
        self.btn((46, 168, 200, 38), "LOAD LAYOUT...", self.ask_layout, size=14)
        self.btn((254, 168, 130, 38), "RELOAD", lambda b: self.reload_layout(), enabled=bool(self.layout_path), size=14)
        lp = self.layout_path or "(no file chosen)"
        txt(s, lp[-52:], 12, DIM, (396, 180))
        if self.layout_name:
            txt(s, f"Page '{self.layout_name}' - keys that can be synced:", 14, AMBER, (50, 226), bold=True)
        for n, k in enumerate(KEYS):
            info = self.layout.get(k)
            r = pygame.Rect(46 + (n % 5) * 134, 256 + (n // 5) * 78, 128, 70)
            sty = self.mod_style(info["cc"]) if info else None
            kc = sty[0] if sty else None
            fixed = bool(info) and info["cc"] in MOD_STYLE
            pygame.draw.rect(s, (kc if fixed else tuple(int(c * 0.45) for c in kc)) if kc else ((18, 163, 90) if info else BTN), r, border_radius=6)
            pygame.draw.rect(s, sty[1] if sty else (AMBER if info else BTN_EDGE), r, 3 if fixed else (2 if kc else 1), border_radius=6)
            txt(s, KEY_LABEL[n], 18, (255, 255, 255) if info else DIM, (r.x + 10, r.y + 6), bold=True)
            if info:
                cur = bool((self.table.get(str(self.cur)) or {}).get("mods", {}).get(str(info["cc"]), False))
                txt(s, MOD_NAME[info["cc"]], 16, (255, 255, 255), (r.x + 44, r.y + 8), bold=True)
                txt(s, f"x{info['times']}  on>{info['on_next']}  off>{info['off_next']}", 11, TEXT, (r.x + 10, r.y + 34))
                txt(s, "table: " + ("ON" if cur else "OFF"), 12, (255, 255, 255) if cur else DIM, (r.x + 10, r.y + 50))
            else:
                txt(s, "not synced", 11, DIM, (r.x + 10, r.y + 40))
        yy = 426
        for ln in self.layout_notes[:6]:
            for part in wrap(ln, 12, 660)[:2]:
                txt(s, part, 12, ORANGE, (50, yy))
                yy += 17
        if not self.layout_path:
            txt(s, "LOAD LAYOUT and type the path to your Captain page file (FW5),", 13, TEXT, (50, 440))
            txt(s, "for example   C:/captain/supersetup/page2.txt", 13, DIM, (50, 462))
        txt(s, "x2  on>2  off>1  =  key has 2 presses; send value 2 to show ON, 1 to show OFF.", 11, DIM, (50, 740))
        txt(s, "Keys not listed keep whatever the Captain shows.", 11, DIM, (50, 758))

        panel(s, (750, 130, 620, 660), "Live")
        pygame.draw.rect(s, LCD_BG, (770, 168, 580, 120), border_radius=8)
        pygame.draw.rect(s, BTN_EDGE, (770, 168, 580, 120), 1, border_radius=8)
        e = self.table.get(str(self.cur))
        txt(s, patch_label(self.cur), 58, LCD_FG, (790, 180), bold=True)
        nm = (e or {}).get("name") or ("(no table entry)" if not e else "")
        txt(s, nm, fit_size(nm, 300, (24, 20, 16), bold=True), LCD_FG if e else DIM, (1000, 200), bold=True)
        txt(s, f"GP-200 bank CC0 = {self.bank}", 11, DIM, (1000, 250))
        self.btn((770, 304, 280, 52), "AUTO SYNC:  " + ("ON" if self.auto else "OFF"), lambda b: self.toggle("auto"), active=self.auto, size=18)
        self.btn((1070, 304, 280, 52), "SYNC NOW", lambda b: self.sync_now(), enabled=bool(self.out), size=18)
        self.btn((770, 366, 280, 40), "LEARN FROM CAPTAIN:  " + ("ON" if self.learn else "OFF"), lambda b: self.toggle("learn"), active=self.learn, size=13)
        txt(s, "Writes module CCs the Captain sends", 11, DIM, (1066, 378))
        txt(s, "into the current patch's table.", 11, DIM, (1066, 392))
        txt(s, "CONTROL CHANNEL", 12, DIM, (770, 432), bold=True)
        self.btn((920, 420, 36, 32), "<", lambda b: self.set_ctl(-1), size=15)
        txt(s, str(self.ctl_ch + 1), 18, LCD_FG, (976, 436), "center", bold=True)
        self.btn((996, 420, 36, 32), ">", lambda b: self.set_ctl(1), size=15)
        txt(s, "= externalmidicontrolch on the Captain.", 11, DIM, (1050, 420))
        txt(s, "Use one the GP-200 ignores.", 11, DIM, (1050, 436))
        txt(s, "SYNC DELAY (ms)", 12, DIM, (770, 482), bold=True)
        self.btn((920, 470, 36, 32), "-", lambda b: self.set_delay(-50), size=15)
        txt(s, str(self.delay), 18, LCD_FG, (990, 486), "center", bold=True)
        self.btn((1024, 470, 36, 32), "+", lambda b: self.set_delay(50), size=15)
        txt(s, "Wait after the patch change before sending.", 11, DIM, (1076, 480))
        txt(s, "PAGE JUMP", 12, DIM, (770, 522), bold=True)
        self.btn((920, 510, 36, 32), "-", lambda b: self.set_jump(-1), size=15)
        txt(s, "off" if self.jump is None else str(self.jump), 18, LCD_FG, (990, 526), "center", bold=True)
        self.btn((1024, 510, 36, 32), "+", lambda b: self.set_jump(1), size=15)
        txt(s, "Send CC20 first to jump the Captain here.", 11, DIM, (1076, 520))
        txt(s, "KEY SPACING (ms)", 12, DIM, (770, 562), bold=True)
        self.btn((920, 550, 36, 32), "-", lambda b: self.set_gap(-10), size=15)
        txt(s, str(self.gap), 18, LCD_FG, (990, 566), "center", bold=True)
        self.btn((1024, 550, 36, 32), "+", lambda b: self.set_gap(10), size=15)
        txt(s, "Pause between key messages; raise it if keys are missed.", 11, DIM, (1076, 560))
        txt(s, "LISTEN CHANNEL", 12, DIM, (770, 602), bold=True)
        self.btn((920, 590, 36, 32), "<", lambda b: self.set_listen(-1), size=15)
        txt(s, "OMNI" if self.listen < 0 else str(self.listen + 1), 18, LCD_FG, (990, 606), "center", bold=True)
        self.btn((1024, 590, 36, 32), ">", lambda b: self.set_listen(1), size=15)
        txt(s, "Only act on messages on this channel (OMNI = any).", 11, DIM, (1076, 600))
        txt(s, "LAST SYNC", 12, DIM, (770, 640), bold=True)
        for i, ln in enumerate(wrap(self.last_sync or "none yet", 12, 580)[:3]):
            txt(s, ln, 12, TEXT, (770, 658 + i * 16))
        mods = "  ".join(f"{MOD_NAME[c]} {'on' if v else 'off'}" for c, v in sorted(self.live_mods.items()))
        txt(s, "Seen from the Captain since the last patch change:", 11, DIM, (770, 716))
        for i, ln in enumerate(wrap(mods or "-", 12, 580)[:2]):
            txt(s, ln, 12, TEXT, (770, 732 + i * 16))

    def draw_recall(self, s):
        p = self.cur
        e = self.table.get(str(p)) or {}
        d = self.defaults.get("patches", {}).get(str(p)) or {}
        panel(s, (30, 130, 700, 660), "This patch: last known vs default")
        pygame.draw.rect(s, LCD_BG, (50, 168, 660, 90), border_radius=8)
        pygame.draw.rect(s, BTN_EDGE, (50, 168, 660, 90), 1, border_radius=8)
        txt(s, patch_label(p), 52, LCD_FG, (66, 178), bold=True)
        nm = e.get("name") or d.get("name") or ("(no table entry)" if not e else "")
        txt(s, nm, fit_size(nm, 360, (24, 20, 16), bold=True), LCD_FG if e else DIM, (270, 196), bold=True)
        txt(s, "MODULE", 12, DIM, (70, 278), bold=True)
        txt(s, "LAST KNOWN (what we restore)", 12, DIM, (210, 278), bold=True)
        txt(s, "DEFAULT (encoder push)", 12, DIM, (470, 278), bold=True)
        for i, (name, cc) in enumerate(MODULES):
            y = 300 + i * 34
            pygame.draw.rect(s, (30, 23, 23) if i % 2 else (38, 30, 30), (50, y, 660, 32), border_radius=4)
            lk = e.get("mods", {}).get(str(cc))
            df = d.get("mods", {}).get(str(cc))
            txt(s, f"{name}   CC{cc}", 15, TEXT, (70, y + 7), bold=True)
            diff = (lk is not None or df is not None) and bool(lk) != bool(df)
            for x, v in ((230, lk), (490, df)):
                label = "-" if v is None else ("ON" if v else "OFF")
                col = DIM if v is None else (GREEN if v else RED)
                txt(s, label, 16, col, (x, y + 6), bold=True)
            if diff:
                txt(s, "differs", 12, ORANGE, (640, y + 9), bold=True)
        txt(s, "-  = not known yet (counts as OFF for the Captain's LEDs, and nothing is sent for it on a restore).", 11, DIM, (50, 646))
        self.btn((50, 676, 210, 44), "RESTORE NOW", self.restore_now, enabled=bool(self.out), size=15)
        self.btn((274, 676, 210, 44), "RESET TO DEFAULT NOW", self.reset_now, enabled=bool(self.out), size=14)
        self.btn((498, 676, 212, 44), "SAVE THIS AS DEFAULT", self.save_default_one, size=14)
        txt(s, "Restore = send the last-known states. Reset = replace them with the default first.", 11, DIM, (50, 734))
        txt(s, "Both go to the GP-200 through the Captain (its MIDI Through must be on).", 11, DIM, (50, 750))

        panel(s, (750, 130, 620, 660), "Recall settings")
        self.btn((770, 168, 580, 44), "RESTORE AFTER EVERY PATCH CHANGE:  " + ("ON" if self.restore else "OFF"), lambda b: self.toggle("restore"), active=self.restore, size=15)
        txt(s, "After the sync delay, the last-known stomp states are sent to the GP-200", 11, DIM, (772, 220))
        txt(s, "so the unit matches what the Captain remembers.", 11, DIM, (772, 235))
        txt(s, "ENCODER RESET CC", 12, DIM, (770, 270), bold=True)
        self.btn((940, 258, 36, 32), "-", lambda b: self.set_reset_cc(-1), size=15)
        txt(s, "off" if self.reset_cc < 0 else str(self.reset_cc), 18, LCD_FG, (1012, 274), "center", bold=True)
        self.btn((1048, 258, 36, 32), "+", lambda b: self.set_reset_cc(1), size=15)
        txt(s, "= encoderkeyCCtoggle (40).", 11, DIM, (1094, 260))
        txt(s, "Use a CC the GP-200 ignores.", 11, DIM, (1094, 276))
        txt(s, "GP-200 CHANNEL", 12, DIM, (770, 316), bold=True)
        self.btn((940, 304, 36, 32), "<", lambda b: self.set_gp_ch(-1), size=15)
        txt(s, str(self.gp_ch + 1), 18, LCD_FG, (1012, 320), "center", bold=True)
        self.btn((1048, 304, 36, 32), ">", lambda b: self.set_gp_ch(1), size=15)
        txt(s, "Stomp CC channel; follows the Captain.", 11, DIM, (1094, 314))
        sv = self.confirm and self.confirm[0] == "all" and time.time() - self.confirm[1] < 5
        self.btn((770, 360, 580, 44), "CLICK AGAIN TO CONFIRM" if sv else "SAVE ALL AS DEFAULTS  (start of the evening)", lambda b: self.ask("all", "This replaces every saved default with the present table.", self.save_defaults_all), active=bool(sv), size=15)
        n = len(self.defaults.get("patches", {}))
        txt(s, f"Defaults file: {n} patches saved" + (f"   ({self.defaults.get('saved')})" if self.defaults.get("saved") else "   (none saved yet)"), 12, TEXT if n else ORANGE, (772, 414))
        txt(s, "LAST ACTION", 12, DIM, (770, 452), bold=True)
        for i, ln in enumerate(wrap(self.recall_note or "none yet", 12, 580)[:4]):
            txt(s, ln, 12, TEXT, (770, 472 + i * 16))
        txt(s, "HOW IT WORKS", 12, DIM, (770, 556), bold=True)
        for i, ln in enumerate((
                "1. Set the GP-200 up, then SAVE ALL AS DEFAULTS.",
                "2. During the night the table follows the Captain's key presses (LEARN).",
                "3. Change patch and come back: the last-known states are sent to the unit.",
                "4. Push the encoder: the patch goes back to its saved default.",
                "Needs MIDI IN from the Captain, and the Captain's midithrough = on.")):
            txt(s, ln, 12, TEXT if i < 4 else ORANGE, (770, 576 + i * 18))

    def set_jump(self, d, _b=1):
        v = -1 if self.jump is None else self.jump
        v = clamp(v + d, -1, 98)
        self.jump = None if v < 0 else v
        self.save_settings()

    def draw_patches(self, s):
        panel(s, (30, 130, 720, 660), "Patches   (wheel scrolls)")
        for n in range(15):
            b = self.bank_scroll + n
            if b >= 64:
                break
            y = 170 + n * 40
            txt(s, f"{b + 1:02d}", 16, DIM, (50, y + 8), bold=True)
            for k in range(4):
                p = b * 4 + k
                rect = pygame.Rect(96 + k * 160, y, 154, 36)
                e = self.table.get(str(p))
                sel = p == self.sel
                hover = rect.collidepoint(pygame.mouse.get_pos())
                pygame.draw.rect(s, (18, 163, 90) if sel else (BTN_HOT if hover else BTN), rect, border_radius=5)
                pygame.draw.rect(s, AMBER if (sel or p == self.cur) else BTN_EDGE, rect, 2 if p == self.cur else 1, border_radius=5)
                txt(s, "ABCD"[k], 14, (255, 255, 255) if sel else TEXT, (rect.x + 8, rect.centery), "midleft", bold=True)
                if e:
                    led(s, (rect.right - 14, rect.centery), True, AMBER)
                    nm = e.get("name", "")
                    if nm:
                        txt(s, nm, fit_size(nm, 100, (13, 12, 11, 10)), (255, 255, 255) if sel else TEXT, (rect.x + 28, rect.centery), "midleft")
                self.add_hit(rect, "patch", p, None)
        panel(s, (770, 130, 600, 660), "Patch")
        e = self.table.get(str(self.sel))
        txt(s, patch_label(self.sel), 44, LCD_FG, (790, 170), bold=True)
        nm = (e or {}).get("name") or "(no name)"
        txt(s, nm, fit_size(nm, 300, (22, 18, 14), bold=True), TEXT, (950, 186), bold=True)
        self.btn((1220, 170, 130, 40), "NAME...", self.ask_name, size=14)
        txt(s, "Click a module to switch it ON or OFF  (a patch starts with everything OFF).", 12, DIM, (790, 236))
        for i, (name, cc) in enumerate(MODULES):
            c, r = i % 5, i // 5
            rect = pygame.Rect(790 + c * 112, 262 + r * 114, 106, 106)
            st = bool((e or {}).get("mods", {}).get(str(cc), False))
            hov = rect.collidepoint(pygame.mouse.get_pos())
            sty = self.mod_style(cc)
            tint = sty[0] if (sty and cc not in MOD_STYLE and sum(sty[0]) > 60) else None
            pygame.draw.rect(s, (14, 127, 70) if st else ((46, 40, 36) if hov else (30, 27, 24)), rect, border_radius=8)
            pygame.draw.rect(s, AMBER if st else (DIM if hov else BTN_EDGE), rect, 2 if st else 1, border_radius=8)
            draw_module_icon(s, name, rect.centerx, rect.y + 46, 1.0 if st else 0.32, tint)
            led(s, (rect.x + 13, rect.y + 12), st, AMBER)
            txt(s, "ON" if st else "OFF", 13, AMBER if st else DIM, (rect.centerx, rect.bottom - 12), "center", bold=True)
            self.add_hit(rect, "btn", lambda b, cc=cc: self.cycle_mod(self.sel, cc))
        self.btn((790, 540, 160, 38), "COPY FROM...", self.copy_from, size=13)
        self.btn((960, 540, 160, 38), "CLEAR", self.clear_patch, enabled=e is not None, size=13)
        self.btn((1130, 540, 220, 38), "SYNC THIS TO CAPTAIN", lambda b: self.sync_now(self.sel), enabled=bool(self.out) and e is not None, size=12)
        self.btn((790, 590, 160, 38), "< PATCH", lambda b: self.goto_patch(self.sel - 1), size=13)
        self.btn((960, 590, 160, 38), "PATCH >", lambda b: self.goto_patch(self.sel + 1), size=13)
        self.btn((1130, 590, 220, 38), "GO TO LIVE PATCH", lambda b: self.goto_patch(self.cur), size=12)
        txt(s, "Dot = patch has a table entry.  Outlined = the patch the GP-200 is on now.", 11, DIM, (790, 650))
        txt(s, "Colours come from the loaded layout page.", 11, DIM, (790, 704))
        txt(s, "Table file:", 11, DIM, (790, 722))
        for i, ln in enumerate(wrap(str(DATA_FILE), 11, 560)[:2]):
            txt(s, ln, 11, TEXT, (790, 738 + i * 14))
        txt(s, "LEARN (SYNC tab) fills the table while you press keys on the Captain.", 11, DIM, (790, 668))

    def draw_test(self, s):
        panel(s, (30, 130, 1340, 660), "Captain external control  (CC on the control channel)")
        txt(s, f"Sending on channel {self.ctl_ch + 1}.  The Captain page must have externalmidicontrol = [on] and externalmidicontrolch = [{self.ctl_ch + 1}].", 13, TEXT, (50, 170))
        txt(s, "PAGE JUMP  (CC20 = page number)", 13, AMBER, (50, 214), bold=True)
        for n in range(30):
            self.btn((50 + (n % 15) * 86, 240 + (n // 15) * 40, 80, 34), f"{n}", lambda b, n=n: self.jump_page(n), size=14)
        txt(s, "KEY PRESS-COUNT  (CC21-30, value 1-10: LED and next press follow it)", 13, AMBER, (50, 340), bold=True)
        for k in range(10):
            y = 368 + k * 32
            txt(s, f"KEY {KEY_LABEL[k]}", 13, TEXT, (50, y + 7), bold=True)
            for v in range(1, 11):
                self.btn((130 + (v - 1) * 52, y, 48, 28), str(v), lambda b, k=k, v=v: self.set_keytimes(k, v), size=13)
        txt(s, "PRESS A KEY REMOTELY  (CC1-10: 1 down, 2 long, 3 up, 4 long up)", 13, AMBER, (700, 340), bold=True)
        for k in range(10):
            y = 368 + k * 32
            txt(s, f"KEY {KEY_LABEL[k]}", 13, TEXT, (700, y + 7), bold=True)
            for i, (lab, v) in enumerate((("DOWN", 1), ("LONG", 2), ("UP", 3), ("LONG UP", 4))):
                self.btn((780 + i * 100, y, 94, 28), lab, lambda b, k=k, v=v: self.press_key(k, v), size=12)
        txt(s, "These do not need a layout. Use them to see how your Captain reacts.", 12, DIM, (50, 700))
        txt(s, "Remember: the GP-200 reads CC20-30 as Quick Access / bank / patch controls, so never use the GP-200's own channel here.", 12, ORANGE, (50, 722))

    # ---- STAGE display: big, readable from across the stage ----------------------------------------------
    def stage_mod_on(self, cc):
        if cc in self.live_mods:                       # what the Captain has just sent wins
            return bool(self.live_mods[cc])
        return bool((self.table.get(str(self.cur)) or {}).get("mods", {}).get(str(cc), False))

    def draw_stage(self, s):
        e = self.table.get(str(self.cur)) or {}
        nm = e.get("name") or ""
        # patch number, name, next / previous
        txt(s, patch_label(self.cur), 130, LCD_FG, (40, 28), bold=True)
        txt(s, nm if nm else "", fit_size(nm or " ", 540, (80, 72, 64, 56, 48, 40), bold=True), TEXT, (450, 52), bold=True)
        if not nm:
            txt(s, "(no name)", 40, DIM, (450, 62), bold=True)
        p = self.cur + 1
        if p < 256:
            pn = (self.table.get(str(p)) or {}).get("name", "")
            txt(s, f"NEXT  {patch_label(p)}", 26, DIM, (1030, 52), bold=True)
            if pn:
                txt(s, pn, fit_size(pn, 320, (34, 30, 26, 22, 18), bold=True), DIM, (1030, 90))
        p = self.cur - 1
        if p >= 0:
            pn = (self.table.get(str(p)) or {}).get("name", "")
            txt(s, f"PREV  {patch_label(p)}  {pn}"[:40], 20, DIM, (450, 150))
        # pedals
        order = [("MOD", 54), ("EQ", 53), ("NR", 51), ("WAH", 57), ("PRE", 48), ("DST", 49), ("DLY", 55), ("RVB", 56)]
        for i, (name, cc) in enumerate(order):
            rect = pygame.Rect(30 + (i % 4) * 336, 225 + (i // 4) * 262, 326, 250)
            st = self.stage_mod_on(cc)
            sty = self.mod_style(cc)
            tint = sty[0] if (sty and cc not in MOD_STYLE and sum(sty[0]) > 60) else None
            if tint and name in STAGE_BRIGHTEN:           # lift a dull layout colour: full brightness, a touch toward white
                mx = max(tint) or 1
                tint = [min(255, int(c * 255 / mx * 0.88 + 255 * 0.12)) for c in tint]
            pc = tuple(tint) if tint else tuple(STAGE_COL.get(name, AMBER))
            pygame.draw.rect(s, _mix(pc, 0.22) if st else (26, 24, 22), rect, border_radius=12)
            pygame.draw.rect(s, pc if st else _mix(pc, 0.35), rect, 5 if st else 2, border_radius=12)
            tile = pygame.Surface((70, 80), pygame.SRCALPHA)
            draw_module_icon(tile, name, 35, 40, 1.0 if st else 0.3, tint)
            big = pygame.transform.smoothscale(tile, (154, 176))
            s.blit(big, big.get_rect(center=(rect.centerx, rect.y + 112)))
            led(s, (rect.x + 30, rect.y + 30), st, pc)
            txt(s, "ON" if st else "OFF", 46, pc if st else _mix(pc, 0.45), (rect.centerx, rect.bottom - 34), "center", bold=True)
        self.btn((1290, 12, 90, 34), "EXIT", lambda b: setattr(self, "tab", 0), size=13)
        txt(s, "F11 = full screen      ESC = leave      Shows what the Captain sends, and the saved table for this patch.", 14, DIM, (30, 780))
        txt(s, "MIDI IN: " + ("open" if self.inp else "NOT OPEN - choose it on the SYNC screen"), 14, AMBER if self.inp else ORANGE, (30, 806))
    def draw_monitor(self, s):
        panel(s, (30, 130, 1340, 660), "MIDI monitor  (newest at the bottom)")
        self.btn((1230, 126, 120, 26), "CLEAR", lambda b: self.events.clear(), size=12)
        evs = list(self.events)[-27:]
        if not evs:
            txt(s, "Nothing yet. Change patch, or choose MIDI OUT at the top.", 14, DIM, (54, 170))
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
        txt(s, "GP-200 SYNC", 36, AMBER, (30, 6), bold=True)
        txt(s, "C A P T A I N   K E Y   S T A T E S", 11, AMBER_DIM, (32, 52))
        out = (self.settings.get("out_name") or "none  (click to choose)").replace("#0", "")
        inn = (self.settings.get("in_name") or "none  (optional)").replace("#0", "")
        self.btn((310, 8, 560, 30), f"MIDI OUT:  {out}"[:68], lambda b: self.choose_device("out"), size=13)
        self.btn((310, 42, 560, 30), f"MIDI IN:   {inn}"[:68], lambda b: self.choose_device("in"), size=13)
        self.btn((890, 8, 170, 64), "SYNC NOW", lambda b: self.sync_now(), enabled=bool(self.out), size=16)
        now = time.time()
        for i, (lab, t) in enumerate((("OUT", self.flash_out), ("IN", self.flash_in))):
            on = now - t < 0.15
            led(s, (1100 + i * 70, 28), on, AMBER)
            txt(s, lab, 12, TEXT if on else DIM, (1114 + i * 70, 21), bold=True)
        txt(s, f"{self.counts['out']} sent   {self.counts['in']} received", 11, DIM, (1092, 48))
        st = "MIDI IN: open" if self.inp else "MIDI IN: NOT OPEN"
        txt(s, f"{st}   raw {self.raw_count}   last {self.last_raw or '-'}", 10, AMBER if self.inp else ORANGE, (1092, 64))
        txt(s, patch_label(self.cur), 22, LCD_FG, (1262, 20), bold=True)
        txt(s, f"ctl ch {self.ctl_ch + 1}", 12, DIM, (1262, 48))
        for i, name in enumerate(TABS):
            self.btn((30 + i * 150, 88, 140, 32), name, lambda b, i=i: setattr(self, "tab", i), active=(i == self.tab), size=14)

    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        self._hits = []
        if self.tab != 5:
            self.draw_header(s)
        [self.draw_sync, self.draw_patches, self.draw_test, self.draw_monitor, self.draw_help, self.draw_stage, self.draw_recall][self.tab](s)
        if self.tab != 5:
            panel(s, (30, 800, 1340, 50), "")
            age = time.time() - self.status_t
            for i, ln in enumerate(wrap(self.status, 13, 1300)[:2]):
                txt(s, ln, 13, TEXT if age < 40 else DIM, (46, 808 + i * 17))
        self.hits = self._hits
        if self.popup:
            self.popup.draw(s, self)

    # ---- events ------------------------------------------------------------------------------------------
    def handle(self, e):
        if e.type == pygame.QUIT:
            self.running = False
            return
        if self.popup:
            self.popup.handle(e, self)
            return
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_F11:
                try:
                    pygame.display.toggle_fullscreen()
                except Exception:
                    pass
            elif e.key == pygame.K_ESCAPE and self.tab == 5:
                self.tab = 0
            elif e.key == pygame.K_ESCAPE:
                self.running = False
            elif self.tab == 1 and e.key in (pygame.K_RIGHT, pygame.K_DOWN):
                self.goto_patch(self.sel + 1)
            elif self.tab == 1 and e.key in (pygame.K_LEFT, pygame.K_UP):
                self.goto_patch(self.sel - 1)
            return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            for r, kind, a, b in reversed(self.hits):
                if not r.collidepoint(e.pos):
                    continue
                if e.button == 3:
                    continue
                if kind == "btn":
                    a(1)
                elif kind == "patch":
                    self.sel = a
                return
        elif e.type == pygame.MOUSEWHEEL and self.tab == 1:
            self.bank_scroll = clamp(self.bank_scroll - e.y * 2, 0, 64 - 15)
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
