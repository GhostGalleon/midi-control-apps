#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
CAPTAIN CHORDSTOMP  -  note and chord editor for the Paint Audio MIDI Captain STD (firmware 5, Super Mode pages).
Based on the MIDI Captain Editor: click a note or a chord on the chart and it is programmed onto a footswitch.

Run:      python captain_app.py
Needs:    pip install pygame

How to use
  1. Hold key 1 (top-left) of the Captain while plugging in USB: a drive called MIDICAPTAIN appears.
  2. Start this program, click the FOLDER button and pick the "supersetup" folder on that drive
     (it looks for the drive by itself at start-up).
  3. Click a page, click a key on the picture, edit what it does, press SAVE.
     Your original pageN.txt files are copied to captain_backups/ before the first save.

The program only edits the plain-text pageN.txt files; comments and any keywords it does not know
about are kept exactly as they were.
Pages written for firmware 4 are converted to the firmware 5 format when they are opened.
"""
import datetime
import json
import math
import re
import shutil
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


if getattr(sys, "frozen", False):          # running as a PyInstaller .exe: keep settings beside the exe
    BASE = _data_root(Path(sys.executable).resolve().parent)
else:
    BASE = _data_root(Path(__file__).resolve().parent)
SETTINGS_FILE = BASE / "chordstomp_settings.json"
BACKUP_DIR = BASE / "captain_backups"

W, H = 1400, 860
GAP = 0.004

# ---- palette (graphite + mint, re-coloured to gold at build time) -----------------------
AMBER = (245, 205, 61)
AMBER_DIM = (244, 199, 38)
ORANGE = (255, 140, 30)
SOLO_RED = (235, 40, 40)
TEXT = (247, 244, 233)
DIM = (172, 166, 150)
PANEL = (52, 48, 36)
EDGE = (152, 143, 120)
LCD_BG = (13, 11, 7)
LCD_FG = (255, 211, 51)
LCD_GLOW = (157, 125, 8)
GREEN = (245, 205, 61)
RED = (255, 90, 80)
BTN = (70, 63, 46)
BTN_HOT = (110, 99, 70)
BTN_EDGE = (152, 142, 116)
WELL = (26, 23, 14)

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


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


def metal(w, h, top=(94, 87, 70), bot=(32, 29, 20)):
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


def rounded_metal(surf, rect, radius=10, top=(94, 87, 70), bot=(32, 29, 20)):
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
    pygame.draw.rect(surf, (40, 36, 27), r, 1, border_radius=10)
    pygame.draw.rect(surf, EDGE, r.inflate(-2, -2), 1, border_radius=9)
    pygame.draw.line(surf, (248, 246, 238), (r.x + 12, r.y + 2), (r.right - 12, r.y + 2))
    spaced = " ".join(title.upper())
    tr = txt(surf, spaced, 12, (249, 247, 239), (r.x + 16, r.y + 8), bold=True)
    if title:
        pygame.draw.line(surf, (76, 69, 52), (tr.right + 12, r.y + 15), (r.right - 16, r.y + 15))
        pygame.draw.line(surf, (245, 205, 61), (tr.right + 12, r.y + 16), (r.right - 16, r.y + 16))
    for sx in (r.x + 12, r.right - 12):
        pygame.draw.circle(surf, (30, 27, 20), (sx, r.bottom - 12), 4)
        pygame.draw.circle(surf, (234, 227, 200), (sx, r.bottom - 12), 4, 1)
        pygame.draw.line(surf, (176, 168, 148), (sx - 2, r.bottom - 14), (sx + 2, r.bottom - 10))


def lcd(surf, rect, text, max_size=24):
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, (30, 27, 20), r.inflate(6, 6), border_radius=8)
    pygame.draw.rect(surf, (214, 206, 187), r.inflate(6, 6), 1, border_radius=8)
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
        pygame.draw.circle(surf, (64, 59, 47), pos, 4)
        pygame.draw.circle(surf, (190, 180, 156), pos, 4, 1)


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
        pygame.draw.rect(surf, (32, 29, 21), r.inflate(4, 4), border_radius=8)
        pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
        pygame.draw.line(surf, (245, 242, 229), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
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
                pygame.draw.rect(surf, (170, 156, 119), r, border_radius=5)
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




# ---- the 20 factory pages that ship with the MIDI Captain STD (firmware 5)
FACTORY = [
    '[GlobalSetup]\npage_name = [Kemper]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [5]\ndisplay_number_ABC = [abc5]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [1]\nPC_group_0 = \nPC_group_1 = \nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [256]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [43]\nencoderkeyCCtoggle = [40]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [7]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n \n[key1]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0x00FF0F][0x00FF0F][0x00FF0F]\n\nledcolor1 = [0x00FF0F][0x00FF0F][0x00FF0F]\nshort_dw1_name =[TAP]\nshort_dw1 = [1][CC][30][1]\nlong1_name = [Tuner]\nlong1 = [1][CCT][31][127]\n\n[key2]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0x8A2BE2][0x8A2BE2][0x8A2BE2]\nshort_dw1_name = [StompA_On]\nshort_dw1 = [1][CC][17][127]\n\nledcolor2 = [0x000000][0x000000][0x000000] \nshort_dw2_name = [StompA_Off]\nshort_dw2 = [1][CC][17][0]\n\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0x8A2BE2][0x8A2BE2][0x8A2BE2]\nshort_dw1_name = [Delay_On]\nshort_dw1 = [1][CC][27][127]\n\nledcolor2 = [0x000000][0x000000][0x000000] \nshort_dw2_name = [Delay_Off]\nshort_dw2 = [1][CC][27][0]\n\n\n[key4]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nshort_dw1 = [1][PC][auto4][-] \n\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_+]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1 = [preset][value_inc]\n\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto0][-]\n\n\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto1][-]\n\n \n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto3][-]\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1 = [preset][value_dec]\n\n\n\n\n\n',
    '[GlobalSetup]\npage_name = [DSPn]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [4]\ndisplay_number_ABC = [abc4]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [1]\nPC_group_0 = \nPC_group_1 = \nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [30]\ntapcountMAX = [250]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [43]\nencoderkeyCCtoggle = [40]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [2]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n \n[key1]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xFF00FF][0xFF00FF][0xFF00FF]\n\nledcolor1 = [0xFF00FF][0xFF00FF][0xFF00FF]\nshort_dw1_name =[TAP]\nshort_dw1 = [1][CC][42][127]\nlong1_name = [Tuner]\nlong1 = [1][CCT][43][127]\n\n[key2]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [FX_1_On]\nshort_dw1 = [1][CC][37][127]\n\nledcolor2 = [0x000000][0x000000][0x000000] \nshort_dw2_name = [FX_1_Off]\nshort_dw2 = [1][CC][37][0]\n\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [FX_2_On]\nshort_dw1 = [1][CC][38][127]\n\nledcolor2 = [0x000000][0x000000][0x000000] \nshort_dw2_name = [FX_2_Off]\nshort_dw2 = [1][CC][38][0]\n\n\n[key4]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [FX_3_On]\nshort_dw1 = [1][CC][39][127]\n\nledcolor2 = [0x000000][0x000000][0x000000] \nshort_dw2_name = [FX_3_Off]\nshort_dw2 = [1][CC][39][0]\n\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_+]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1 = [preset][value_inc]\n\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto0][-]\n\n\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto1][-]\n\n \n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto3][-]\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1 = [preset][value_dec]\n\n\n\n\n\n',
    '[GlobalSetup]\npage_name = [DSPm]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [4]\ndisplay_number_ABC = [abc4]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [2]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [30]\ntapcountMAX = [250]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [43]\nencoderkeyCCtoggle = [40]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [2]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n\n \n[key1]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xFF00FF][0xFF00FF][0xFF00FF]\n\nledcolor1 = [0xFF00FF][0xFF00FF][0xFF00FF]\nshort_dw1_name =[TAP]\nshort_dw1 = [1][CC][44][127]\nlong1_name = [Tuner]\nlong1 = [1][CCT][45][127]\n\n[key2]\nkeytimes = [3]\nledmode = [normal]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\nledcolor1 = [0xFFA500][0x000000][0x000000]\nshort_dw1_name = [Preset]\nshort_dw1 = [1][CC][47][0]\n\nledcolor2 = [0x000000][0xFFA500][0x000000] \nshort_dw2_name = [Scene]\nshort_dw2 = [1][CC][47][1]\n\nledcolor3 = [0x000000][0x000000][0xFFA500] \nshort_dw3_name = [Stomp]\nshort_dw3 = [1][CC][47][2]\n\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x7FFF00][0x7FFF00][0x7FFF00]\n\nledcolor1 = [0x000000][0x000000][0x7FFF00]\nshort_dw1_name = [Swap_1]\nshort_dw1 = [1][CC][64][0]\n\nledcolor2 = [0x7FFF00][0x7FFF00][0x000000] \nshort_dw2_name = [Swap_2]\nshort_dw2 = [1][CC][64][127]\n\n\n[key4]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0xFF7F50][0xFF7F50][0xFF7F50]\n\nledcolor1 = [0x000000][0x000000][0xFF7F50]\nshort_dw1_name = [GigView_On]\nshort_dw1 = [1][CC][46][127]\n\nledcolor2 = [0xFF7F50][0xFF7F50][0x000000] \nshort_dw2_name = [GigView_Off]\nshort_dw2 = [1][CC][46][0]\n\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_+]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1 = [preset][value_inc]\n\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto0][-]\n\n\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto1][-]\n\n \n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto3][-]\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1 = [preset][value_dec]\n\n\n\n\n\n',
    '[GlobalSetup]\npage_name = [DSP]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [4]\ndisplay_number_ABC = [abc4]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [2]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [30]\ntapcountMAX = [250]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [43]\nencoderkeyCCtoggle = [40]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [2]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n\n \n[key1]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xD2691E][0xD2691E][0xD2691E]\n\nledcolor1 = [0xD2691E][0xD2691E][0xD2691E]\nshort_dw1_name =[Scene_A]\nshort_dw1 = [1][CC][43][0]\n\n\n[key2]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xD2691E][0xD2691E][0xD2691E]\n\nledcolor1 = [0xD2691E][0xD2691E][0xD2691E]\nshort_dw1_name =[Scene_B]\nshort_dw1 = [1][CC][43][1]\n\n\n\n[key3]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xD2691E][0xD2691E][0xD2691E]\n\nledcolor1 = [0xD2691E][0xD2691E][0xD2691E]\nshort_dw1_name =[Scene_C]\nshort_dw1 = [1][CC][43][2]\n\n\n\n[key4]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xD2691E][0xD2691E][0xD2691E]\n\nledcolor1 = [0xD2691E][0xD2691E][0xD2691E]\nshort_dw1_name =[Scene_D]\nshort_dw1 = [1][CC][43][3]\n\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_+]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1 = [preset][value_inc]\n\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto0][-]\n\n\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto1][-]\n\n \n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1 = [1][PC][auto3][-]\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1 = [preset][value_dec]\n\n\n\n\n\n',
    '[GlobalSetup]\npage_name = [DSPsc]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [4]\ndisplay_number_ABC = [abc4]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [2]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [30]\ntapcountMAX = [250]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [43]\nencoderkeyCCtoggle = [40]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [2]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n \n[key1]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name =[Scene_A]\nshort_dw1 = [1][CC][43][0]\n\n\n[key2]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name =[Scene_B]\nshort_dw1 = [1][CC][43][1]\n\n\n\n[key3]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name =[Scene_C]\nshort_dw1 = [1][CC][43][2]\n\n\n\n[key4]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name =[Scene_D]\nshort_dw1 = [1][CC][43][3]\n\n\n[key5]\nkeytimes = [1]\nledmode = [select2]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_up1_name = [PC_+]\nshort_up1 = [1][PC][value_inc][-] \nlong1 = [preset][value_inc]\n\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name =[Scene_E]\nshort_dw1 = [1][CC][43][4]\n\n\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name =[Scene_F]\nshort_dw1 = [1][CC][43][5]\n\n \n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name =[Scene_G]\nshort_dw1 = [1][CC][43][6]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name =[Scene_H]\nshort_dw1 = [1][CC][43][7]\n\n\n[keyE]\nkeytimes = [1]\nledmode = [select2]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_up1_name = [PC_-]\nshort_up1 = [1][PC][value_dec][-] \nlong1 = [preset][value_dec]\n\n\n\n\n\n',
    '[GlobalSetup]\npage_name = [HXLP]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [3]\ndisplay_number_ABC = [abc3]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [1]\nPC_group_0 = \nPC_group_1 = \nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [40]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [82]\nencoderkeyCCtoggle = [73]\nexp1_CH = [1]\nexp1_CC = [0]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [2]\nexp2_CC = [1]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n\n[key1]\nkeytimes = [3]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0x000000][0x000000][0xFF0000]\nshort_dw1_name = [Snap1]\nshort_dw1 = [1][CC][69][0]\n\nledcolor2 = [0xFF0000][0x000000][0x000000]\nshort_dw2_name = [Snap2]\nshort_dw2 = [1][CC][69][1]\n\nledcolor3 = [0x000000][0xFF0000][0x000000]\nshort_dw3_name = [Snap3]\nshort_dw3 = [1][CC][69][2]\n\n[key2]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0x00FF00][0x00FF00][0x00FF00]\nshort_dw1_name = [Play]\nshort_dw1 = [1][CC][61][127]\n\nledcolor2 = [0xFF0000][0xFF0000][0xFF0000]\nshort_dw2_name = [Pause]\nshort_dw2 = [1][CC][61][00]\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0x000FFF][0x000FFF][0x000FFF]\nshort_dw1_name = [Record]\nshort_dw1 = [1][CC][60][127]\n\nledcolor2 = [0x00FF00][0x00FF00][0x00FF00]\nshort_dw2_name = [Play]\nshort_dw2 = [1][CC][61][127]\n\n[key4]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0x0000FF][0x0000FF][0x0000FF]\nshort_dw1_name = [OVD]\nshort_dw1 = [1][CC][60][0]\nlong1_name = [Undo]\nlong1 = [1][CC][63][127]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [Bank_+]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0x0xff00][0x0xff00][0x0xff00]\n\n\nledcolor1 = [0x0xff00][0x0xff00][0x0xff00]\nshort_dw1_name = [Tap]\nshort_dw1 = [1][CC][64][64]\nlong1_name = [Tuner]\nlong1 = [1][CC][68][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [Bank_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [HXSN]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [3]\ndisplay_number_ABC = [abc3]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [1]\nPC_group_0 = \nPC_group_1 = \nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [40]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [20]\nencoderkeyCCtoggle = [10]\nexp1_CH = [1]\nexp1_CC = [0]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [2]\nexp2_CC = [1]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Snap1]\nshort_dw1 = [1][CC][69][0]\n\n[key2]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Snap2]\nshort_dw1 = [1][CC][69][1]\n\n[key3]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Snap3]\nshort_dw1 = [1][CC][69][2]\n\n[key4]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Snap4]\nshort_dw1 = [1][CC][69][3]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [Bank_+]\nshort_dw1 = [1][PC][bank_inc][-] \nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0x00FF00][0x00FF00][0x00FF00]\n\n\nledcolor1 = [0x00FF00][0x00FF00][0x00FF00]\nshort_dw1_name = [Tap]\nshort_dw1 = [1][CC][64][64]\nlong1_name = [Tuner]\nlong1 = [1][CC][68][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [Bank_-]\nshort_dw1 = [1][PC][bank_dec][-] \nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [FM3S]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [4]\ndisplay_number_ABC = [123]\ndisplay_bank_offset = [0]\npc_open_num = [0]\nPC_Bank_Select = [4]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = [1][CC][0][2]\nPC_group_3 = [1][CC][0][3]\nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [30]\ntapcountMAX = [250]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [20]\nencoderkeyCCtoggle = [46]\nexp1_CH = [1]\nexp1_CC = [0]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [2]\nexp2_CC = [1]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence5]\nshort_dw1 = [1][CC][43][4]\n\n[key2]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence6]\nshort_dw1 = [1][CC][43][5]\n\n[key3]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Secce7]\nshort_dw1 = [1][CC][43][6]\n\n[key4]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence8]\nshort_dw1 = [1][CC][43][7]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [PC_ +]\nshort_dw1 = [1][PC][value_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence1]\nshort_dw1 = [1][CC][43][0]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence2]\nshort_dw1 = [1][CC][43][1]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence3]\nshort_dw1 = [1][CC][43][2]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence4]\nshort_dw1 = [1][CC][43][3]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [PC_-]\nshort_dw1 = [1][PC][value_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [FM3M]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [3]\ndisplay_number_ABC = [123]\ndisplay_bank_offset = [0]\npc_open_num = [0]\nPC_Bank_Select = [4]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = [1][CC][0][2]\nPC_group_3 = [1][CC][0][3]\nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [30]\ntapcountMAX = [250]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [20]\nencoderkeyCCtoggle = [46]\nexp1_CH = [1]\nexp1_CC = [0]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [2]\nexp2_CC = [1]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence1]\nshort_dw1 = [1][CC][43][0]\n\n[key2]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence2]\nshort_dw1 = [1][CC][43][1]\n\n[key3]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Secce3]\nshort_dw1 = [1][CC][43][2]\n\n[key4]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sence4]\nshort_dw1 = [1][CC][43][3]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [PC_ +]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xFF1493][0xFF1493][0xFF1493]\n\n\nledcolor1 = [0xFF1493][0xFF1493][0xFF1493]\nshort_dw1_name = [TAP]\nshort_dw1 = [1][CC][15][1]\nlong1_name = [Tuner]\nlong1 = [1][CCT][46][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [PC_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [FMLP]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [4]\ndisplay_number_ABC = [123]\ndisplay_bank_offset = [0]\npc_open_num = [0]\nPC_Bank_Select = [4]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = [1][CC][0][2]\nPC_group_3 = [1][CC][0][3]\nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [30]\ntapcountMAX = [250]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [20]\nencoderkeyCCtoggle = [46]\nexp1_CH = [1]\nexp1_CC = [0]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [2]\nexp2_CC = [1]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FF00][0x00FF00][0x00FF00]\n\nledcolor1 = [0x00FF00][0x00FF00][0x00FF00]\nshort_dw1_name = [Play/Stop]\nshort_dw1 = [1][CC][20][1]\n\n[key2]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x0000FF][0x0000FF][0x0000FF]\n\n\nledcolor1 = [0x0000FF][0x0000FF][0x0000FF]\nshort_dw1_name = [Undo]\nshort_dw1 = [1][CC][21][1]\n\n[key3]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF0000][0xFF0000][0xFF0000]\n\n\nledcolor1 = [0xFF0000][0xFF0000][0xFF0000]\nshort_dw1_name = [Record]\nshort_dw1 = [1][CC][22][1]\n\n[key4]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [Ovd]\nshort_dw1 = [1][CC][23][1]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [PC_ +]\nshort_dw1 = [1][PC][value_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sense1]\nshort_dw1 = [1][CC][43][0]\n\n[keyB]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sense2]\nshort_dw1 = [1][CC][43][1]\n\n[keyC]\nkeytimes = [1]\nledmode = [select1]\nledcolor0 = [0xFFA500][0xFFA500][0xFFA500]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Sense3]\nshort_dw1 = [1][CC][43][2]\n\n[keyD]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xFF1493][0xFF1493][0xFF1493]\n\n\nledcolor1 = [0xFF1493][0xFF1493][0xFF1493]\nshort_dw1_name = [TAP]\nshort_dw1 = [1][CC][15][1]\nlong1_name = [Tuner]\nlong1 = [1][CCT][46][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [PC_-]\nshort_dw1 = [1][PC][value_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [AMPO]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [3]\ndisplay_number_ABC = [abc3]\ndisplay_bank_offset = [0]\npc_open_num = [0]\nPC_Bank_Select = [3]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = [1][CC][0][2]\nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [40]\ntapcountMAX = [300]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [16]\nencoderkeyCCtoggle = [49]\nexp1_CH = [1]\nexp1_CC = [11]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [7]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0x00FA9A][0x00FA9A][0x00FA9A]\nshort_dw1_name = [FS2_ON]\nshort_dw1 = [1][CC][49][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [FS2_OFF]\nshort_dw2 = [1][CC][49][00]\n\n[key2]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x0000FF][0x0000FF][0x0000FF]\n\n\nledcolor1 = [0x0000FF][0x0000FF][0x0000FF]\nshort_dw1_name = [FS4_ON]\nshort_dw1 = [1][CC][51][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [FS4_OFF]\nshort_dw2 = [1][CC][51][0]\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [FS7_ON]\nshort_dw1 = [1][CC][54][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [FS7_OFF]\nshort_dw2 = [1][CC][54][0]\n\n[key4]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0x7D26CD][0x7D26CD][0x7D26CD]\nshort_dw1_name = [FS8_ON]\nshort_dw1 = [1][CC][55][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [FS8_OFF]\nshort_dw2 = [1][CC][55][0]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [BANK_ +]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xFF1413][0xFF1413][0xFF1413]\n\n\nledcolor1 = [0xFF1413][0xFF1413][0xFF1413]\nshort_dw1_name = [TAP]\nshort_dw1 = [1][CC][76][1]\nlong1_name = [Tuner]\nlong1 = [1][CCT][60][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [AMPL]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [3]\ndisplay_number_ABC = [abc3]\ndisplay_bank_offset = [0]\npc_open_num = [0]\nPC_Bank_Select = [3]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = [1][CC][0][2]\nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [40]\ntapcountMAX = [300]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [16]\nencoderkeyCCtoggle = [49]\nexp1_CH = [1]\nexp1_CC = [11]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [7]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x00FF7F][0x00FF7F][0x00FF7F]\n\nledcolor1 = [0x00FF7F][0x00FF7F][0x00FF7F]\nshort_dw1_name = [Play]\nshort_dw1 = [1][CC][64][127]\n\nledcolor2 = [0x0000CD][0x0000CD][0x0000CD]\nshort_dw2_name = [Pause]\nshort_dw2 = [1][CC][64][0]\n\n[key2]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xFF4500][0xFF4500][0xFF4500]\n\n\nledcolor1 = [0xFF4500][0xFF4500][0xFF4500]\nshort_dw1_name = [Record]\nshort_dw1 = [1][CC][63][1]\n\n[key3]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nlong1_name = [Undo]\nlong1 = [1][CC][68][1]\nshort_up1_name = [OVD]\nshort_up1 = [1][CC][67][1]\n\n[key4]\nkeytimes = [3]\nledmode = [normal]\nledcolor0 = [0xAB82FF][0xAB82FF][0xAB82FF]\n\n\nledcolor1 = [0x000000][0x000000][0xAB82FF]\nshort_dw1_name = [Scene1]\nshort_dw1 = [1][CC][25][1]\n\nledcolor2 = [0xAB82FF][0x000000][0x000000]\nshort_dw2_name = [Scene2]\nshort_dw2 = [1][CC][25][2]\n\nledcolor3 = [0x000000][0xAB82FF][0x000000]\nshort_dw3_name = [Scene3]\nshort_dw3 = [1][CC][25][3]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [BANK_ +]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xFF1413][0xFF1413][0xFF1413]\n\n\nledcolor1 = [0xFF1413][0xFF1413][0xFF1413]\nshort_dw1_name = [TAP]\nshort_dw1 = [1][CC][76][1]\nlong1_name = [Tuner]\nlong1 = [1][CCT][60][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [GT1K]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [5]\ndisplay_number_ABC = [abc5]\ndisplay_bank_offset = [0]\npc_open_num = [0]\nPC_Bank_Select = [4]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = [1][CC][0][2]\nPC_group_3 = [1][CC][0][3]\nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [250]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [16]\nencoderkeyCCtoggle = [49]\nexp1_CH = [1]\nexp1_CC = [11]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [7]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xFF1413][0xFF1413][0xFF1413]\n\nledcolor1 = [0xFF1413][0xFF1413][0xFF1413]\nshort_dw1_name = [Tap]\nshort_dw1 = [1][CC][26][autotap]\nlong1_name = [Tuner]\nlong1 = [1][CCT][22][127]\n\n[key2]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0x00FF7F][0x00FF7F][0x00FF7F]\nshort_dw1_name = []\nshort_dw1 = [1][CC][20][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = []\nshort_dw2 = [1][CC][20][0]\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = []\nshort_dw1 = [1][CC][21][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = []\nshort_dw2 = [1][CC][21][0]\n\n[key4]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto4][-]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [BANK_ +]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto3][-]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x666666][0x666666][0x666666]\n\n\nledcolor1 = [0x666666][0x666666][0x666666]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [HRSc]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [5]\ndisplay_number_ABC = [abc5]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [1]\nPC_group_0 = \nPC_group_1 = \nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [62]\nencoderkeyCCtoggle = [76]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [2]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Scene5]\nshort_dw1 = [1][CC][25][127]\n\n[key2]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Scene6]\nshort_dw1 = [1][CC][26][127]\n\n[key3]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Scene7]\nshort_dw1 = [1][CC][27][127]\n\n[key4]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Scene8]\nshort_dw1 = [1][CC][28][127]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [PC_+]\nshort_dw1 = [1][PC][value_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Scene1]\nshort_dw1 = [1][CC][21][127]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Scene2]\nshort_dw1 = [1][CC][22][127]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Scene3]\nshort_dw1 = [1][CC][23][127]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Scene4]\nshort_dw1 = [1][CC][24][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [PC_-]\nshort_dw1 = [1][PC][value_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [HRFP]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [5]\ndisplay_number_ABC = [abc5]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [1]\nPC_group_0 = \nPC_group_1 = \nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [62]\nencoderkeyCCtoggle = [76]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [2]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0x00FF00][0x00FF00][0x00FF00]\n\nledcolor1 = [0x00FF00][0x00FF00][0x00FF00]\nshort_dw1_name = [Tap]\nshort_dw1 = [1][CC][64][127]\nlong1_name = [Tuner]\nlong1 = [1][CCT][92][127]\n\n[key2]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0xFF6A6A][0xFF6A6A][0xFF6A6A]\nshort_dw1_name = [Block2_On]\nshort_dw1 = [1][CC][76][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [Block2_Off]\nshort_dw2 = [1][CC][76][0]\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = [Block4_On]\nshort_dw1 = [1][CC][78][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [Block4_Off]\nshort_dw2 = [1][CC][78][0]\n\n[key4]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto4][-]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_+]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto3][-]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [ToM1]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [6]\ndisplay_number_ABC = [abc6]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [4]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = [1][CC][0][2]\nPC_group_3 = [1][CC][0][3]\nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [7]\nencoderkeyCCtoggle = [65]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [2]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto3][-]\n\n[key2]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto4][-]\n\n[key3]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto5][-]\n\n[key4]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0x0000FF][0x0000FF][0x0000FF]\n\n\nledcolor1 = [0x0000FF][0x0000FF][0x0000FF]\nshort_dw1_name = [FS_Mode]\nshort_dw1 = [1][CC][20][127]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_+]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xFF6347][0xFF6347][0xFF6347]\n\n\nledcolor1 = [0xFF6347][0xFF6347][0xFF6347]\nshort_dw1_name = [TAP]\nshort_dw1 = [1][CC][64][127]\nlong1_name = [Tuner]\nlong1 = [1][CCT][68][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [ToM2]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [6]\ndisplay_number_ABC = [abc6]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [4]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = [1][CC][0][2]\nPC_group_3 = [1][CC][0][3]\nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [7]\nencoderkeyCCtoggle = [65]\nexp1_CH = [1]\nexp1_CC = [1]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [2]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [FS_1]\nshort_dw1 = [1][CC][21][127]\n\n[key2]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [FS_2]\nshort_dw1 = [1][CC][22][127]\n\n[key3]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [FS_3]\nshort_dw1 = [1][CC][23][127]\n\n[key4]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [FS_4]\nshort_dw1 = [1][CC][24][127]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [PC_+]\nshort_dw1 = [1][PC][value_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [FS_5]\nshort_dw1 = [1][CC][25][127]\n\n[keyB]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [FS_6]\nshort_dw1 = [1][CC][26][127]\n\n[keyC]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [FS_7]\nshort_dw1 = [1][CC][27][127]\n\n[keyD]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [FS_8]\nshort_dw1 = [1][CC][28][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [PC_-]\nshort_dw1 = [1][PC][value_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [IKTX]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [3]\ndisplay_number_ABC = [abc3]\ndisplay_bank_offset = [0]\npc_open_num = [0]\nPC_Bank_Select = [2]\nPC_group_0 = [1][CC][0][0]\nPC_group_1 = [1][CC][0][1]\nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [23]\nencoderkeyCCtoggle = [12]\nexp1_CH = [1]\nexp1_CC = [102]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [11]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0x90EE90][0x90EE90][0x90EE90]\nshort_dw1_name = [COMP_On]\nshort_dw1 = [1][CC][19][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [COMP_Off]\nshort_dw2 = [1][CC][19][0]\n\n[key2]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0x90EE90][0x90EE90][0x90EE90]\nshort_dw1_name = [Reverb_On]\nshort_dw1 = [1][CC][79][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [Reverb_Off]\nshort_dw2 = [1][CC][79][0]\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0x90EE90][0x90EE90][0x90EE90]\nshort_dw1_name = [Delay_On]\nshort_dw1 = [1][CC][80][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [Delay_Off]\nshort_dw2 = [1][CC][80][0]\n\n[key4]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0x90EE90][0x90EE90][0x90EE90]\nshort_dw1_name = [Chorus_On]\nshort_dw1 = [1][CC][31][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = [Chorus_Off]\nshort_dw2 = [1][CC][31][0]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_+]\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [tap]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = [TAP]\nshort_dw1 = [1][CC][90][127]\nlong1_name = [Tuner]\nlong1 = [1][CCT][9][127]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = [BANK_-]\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [JAMP]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [4]\ndisplay_number_ABC = [abc4]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [1]\nPC_group_0 = \nPC_group_1 = \nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [20]\nencoderkeyCCtoggle = [10]\nexp1_CH = [1]\nexp1_CC = [0]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [1]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = []\nshort_dw1 = [1][CC][21][127]\n\n[key2]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = []\nshort_dw1 = [1][CC][22][127]\n\n[key3]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = []\nshort_dw1 = [1][CC][23][127]\n\n[key4]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xA020F0][0xA020F0][0xA020F0]\n\n\nledcolor1 = [0xA020F0][0xA020F0][0xA020F0]\nshort_dw1_name = []\nshort_dw1 = [1][CC][24][127]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = []\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto3][-]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = []\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
    '[GlobalSetup]\npage_name = [BIAS]\nledbright = [80]\nscreenbright = [80]\nlong_press_timing = [1]\nWIRELESS_2.4G = [on]\nWIRELESS_ID   = [8]\nWIRELESS_dB   = [6]\nBATTERY_CHARGE= [off]\ngroup_number = [4]\ndisplay_number_ABC = [abc4]\ndisplay_bank_offset = [1]\npc_open_num = [0]\nPC_Bank_Select = [1]\nPC_group_0 = \nPC_group_1 = \nPC_group_2 = \nPC_group_3 = \nPC_group_4 = \nPC_group_5 = \nPC_group_6 = \nPC_group_7 = \nPC_group_8 = \nPC_group_9 = \nCC_value_gropnumber = \ntapcountMIN = [20]\ntapcountMAX = [200]\nmidithrough = [on]\nexternalmidicontrol = [on]\nexternalmidicontrolch = [16]\nencoderCH = [1]\nencoderCCnum = [20]\nencoderkeyCCtoggle = [10]\nexp1_CH = [1]\nexp1_CC = [0]\nexp1_min = [0]\nexp1_max = [127]\nexp2_CH = [1]\nexp2_CC = [1]\nexp2_min = [0]\nexp2_max = [127]\nLEDSelectLowLight = [on]\nLowLightLevel = [10]\n\n[key1]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = []\nshort_dw1 = [1][CC][80][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = []\nshort_dw2 = [1][CC][80][0]\n\n[key2]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = []\nshort_dw1 = [1][CC][81][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = []\nshort_dw2 = [1][CC][81][0]\n\n[key3]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = []\nshort_dw1 = [1][CC][82][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = []\nshort_dw2 = [1][CC][82][0]\n\n[key4]\nkeytimes = [2]\nledmode = [normal]\nledcolor0 = [0x000000][0x000000][0x000000]\n\n\nledcolor1 = [0xFFA500][0xFFA500][0xFFA500]\nshort_dw1_name = []\nshort_dw1 = [1][CC][83][127]\n\nledcolor2 = [0x000000][0x000000][0x000000]\nshort_dw2_name = []\nshort_dw2 = [1][CC][83][0]\n\n[key5]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = []\nshort_dw1 = [1][PC][bank_inc][-]\nlong1_name = []\nlong1 = [preset][value_inc]\n\n[keyA]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto0][-]\n\n[keyB]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto1][-]\n\n[keyC]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto2][-]\n\n[keyD]\nkeytimes = [1]\nledmode = [select]\nledcolor0 = [0x00FFFF][0x00FFFF][0x00FFFF]\n\n\nledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]\nshort_dw1_name = []\nshort_dw1 = [1][PC][auto3][-]\n\n[keyE]\nkeytimes = [1]\nledmode = [normal]\nledcolor0 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\n\n\nledcolor1 = [0xCCCCCC][0xCCCCCC][0xCCCCCC]\nshort_dw1_name = []\nshort_dw1 = [1][PC][bank_dec][-]\nlong1_name = []\nlong1 = [preset][value_dec]',
]


def _norm(text, n):
    return [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def factory_name(n):
    m = re.search(r"page_name\s*=\s*\[([^\]]*)\]", FACTORY[n])
    return m.group(1) if m else "?"


# =============================================================================
# PAGE FILE MODEL  (lossless: comments, blank lines and unknown keywords survive)
# =============================================================================
class Item:
    def __init__(self, comments, key, raw, line):
        self.comments, self.key, self.raw, self.line = comments, key, raw, line


class Section:
    def __init__(self, name, pre):
        self.name, self.pre, self.items = name, pre, []
        self.header = f"[{name}]" if name else None

    def find(self, key):
        for it in self.items:
            if it.key == key:
                return it
        return None

    def get(self, key, default=None):
        it = self.find(key)
        return it.raw if it else default

    def set(self, key, raw, comments=None):
        it = self.find(key)
        if it:
            if it.raw != raw:
                it.raw, it.line = raw, None
        else:
            self.items.append(Item(list(comments or []), key, raw, None))

    def delete(self, key):
        self.items = [i for i in self.items if i.key != key]


class PageDoc:
    def __init__(self, sections=None, tail=None, nl="\r\n"):
        self.sections, self.tail, self.nl = sections or [], tail or [], nl

    def sec(self, name, create=False):
        for s in self.sections:
            if s.name.lower() == name.lower():
                return s
        if create:
            s = Section(name, [""])
            self.sections.append(s)
            return s
        return None

    def to_text(self):
        out = []
        for s in self.sections:
            out += s.pre
            if s.header:
                out.append(s.header)
            for it in s.items:
                out += it.comments
                out.append(it.line if it.line is not None else f"{it.key} = {it.raw}")
        out += self.tail
        return self.nl.join(out) + self.nl

    def clone(self):
        return parse_page(self.to_text())


def parse_page(text):
    nl = "\n"          # the Captain wants plain LF line endings (CRLF pages were not loaded)
    text = text.replace("\r\n", "\n")
    doc = PageDoc(nl=nl)
    pending, cur = [], None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^\[([A-Za-z0-9_.]+)\]$", s)
        if s == "" or s.startswith("#"):
            pending.append(line)
        elif m:
            cur = Section(m.group(1), pending)
            cur.header = line
            doc.sections.append(cur)
            pending = []
        elif "=" in s:
            if cur is None:
                cur = Section("", pending)
                cur.header = None
                doc.sections.append(cur)
                pending = []
            k, v = s.split("=", 1)
            cur.items.append(Item(pending, k.strip(), v.strip(), line))
            pending = []
        else:
            pending.append(line)
    doc.tail = pending
    return doc


# ---- value helpers ----------------------------------------------------------
KEYSEC = ["key1", "key2", "key3", "key4", "key5", "keyA", "keyB", "keyC", "keyD", "keyE"]     # FW5 section names, key index 0..9
GSEC = "GlobalSetup"


def groups(raw):
    return re.findall(r"\[([^\]]*)\]", raw or "")


def parse_cmds(raw):
    """FW5 commands have different lengths: [1][PC][0][-] (4), [preset][value_inc] (2), [HID][send][A][END] (to END) ..."""
    g = groups(raw)
    out, i = [], 0
    while i < len(g):
        low = g[i].lower()
        if g[i].isdigit():
            n = 4
        elif low in ("hid", "sysex"):
            n = 1
            while i + n < len(g) and g[i + n].upper() != "END":
                n += 1
            n += 1
        elif low == "syscmn":
            n = 3
        elif low == "expccchange":
            n = 6
        else:
            n = 2
        c = g[i:i + n]
        if c and c[0].isdigit():
            c = (c + ["-"] * 4)[:4]
        out.append(c)
        i += n
    return out


def fmt_cmds(cmds):
    return " ".join("".join(f"[{x}]" for x in c) for c in cmds)


def is_std(c):
    """[channel][PC|CC|CCT|NT][a][b]"""
    return len(c) == 4 and c[0].isdigit()


def ctype(c):
    if not c:
        return ""
    if c[0].isdigit():
        return c[1].upper() if len(c) > 1 else ""
    return {"hid": "HID", "sysex": "SYSEX", "syscmn": "SYSCMN", "preset": "PRESET", "timeengine": "TIME", "realtime": "REALTIME",
            "expccchange": "EXP"}.get(c[0].lower(), c[0].upper())


def parse_led(raw):
    out = []
    for g in groups(raw)[:3]:
        try:
            out.append(int(g, 16) & 0xFFFFFF)
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(out[-1] if out else 0)
    return out


def fmt_led(cols):
    return "".join(f"[0x{c:06X}]" for c in cols)


def rgb(c):
    return ((c >> 16) & 255, (c >> 8) & 255, c & 255)


def dim_hex(c, f):
    r, g, b = rgb(c)
    return (int(r * f) << 16) | (int(g * f) << 8) | int(b * f)


TRIGGERS = [("short_dw", "PRESS DOWN"), ("short_up", "PRESS UP"), ("long", "LONG PRESS"), ("long_up", "LONG PRESS UP")]
KEY_NAMES = ["1", "2", "3", "4", "5", "A", "B", "C", "D", "E"]      # key1..key5, keyA..keyE as printed on the STD
KEY_NAMES_ASCII = KEY_NAMES
LED_MODES = [("normal", "normal - shows this press' colour"), ("select", "select - only one key lit"),
             ("select1", "select1 - exclusive group 1"), ("select2", "select2 - exclusive group 2"),
             ("select3", "select3 - exclusive group 3"), ("select4", "select4 - exclusive group 4"),
             ("select5", "select5 - exclusive group 5"), ("tap", "tap - flashes (tempo)")]


_PRESS_RE = re.compile(r"^(ledcolor|short_dw|short_up|long_up|long)(\d+)(_name)?$")
_RANK = {"ledcolor": 0, "short_dw": 1, "short_up": 2, "long": 3, "long_up": 4}


def tidy_key_section(sec):
    """Group each press' keywords together the way the Captain files do:
         ledcolor0, ledcolor1, short_dw1_name, short_dw1, short_up1 ..., ledcolor2, ...
    Anything else (keytimes, ledmode, unknown lines) stays first, in its original order."""
    plain, pressed = [], []
    for it in sec.items:
        mm = _PRESS_RE.match(it.key)
        if mm:
            pressed.append(((int(mm.group(2)), _RANK[mm.group(1)], 0 if mm.group(3) else 1), it))
        else:
            plain.append(it)
    pressed.sort(key=lambda t: t[0])          # stable; a name goes just before its command
    sec.items = plain + [it for _, it in pressed]


# ---- Captain ChordStomp: chord chart + piano ------------------------------------------------------------------------
CHORD_TYPES = [("maj", [0, 4, 7]), ("min", [0, 3, 7]), ("7", [0, 4, 7, 10]), ("maj7", [0, 4, 7, 11]), ("m7", [0, 3, 7, 10]), ("6", [0, 4, 7, 9]),
               ("m6", [0, 3, 7, 9]), ("sus2", [0, 2, 7]), ("sus4", [0, 5, 7]), ("dim", [0, 3, 6]), ("aug", [0, 4, 8]), ("add9", [0, 4, 7, 14]),
               ("9", [0, 4, 7, 10, 14]), ("m9", [0, 3, 7, 10, 14]), ("dim7", [0, 3, 6, 9]), ("m7b5", [0, 3, 6, 10]), ("5", [0, 7, 12]), ("maj9", [0, 4, 7, 11, 14])]
PIANO_LO, PIANO_HI = 36, 96        # C2 .. B6


def chord_notes(root, ctype, octave, inv):
    iv = dict(CHORD_TYPES)[ctype]
    base = 12 * (octave + 1) + root
    notes = [base + i for i in iv]
    for _ in range(inv % len(notes)):
        notes = notes[1:] + [notes[0] + 12]
    return [n for n in notes if 0 <= n <= 127]


def chord_label(root, ctype):
    return NOTE_NAMES[root] + ctype



class KeyView:
    """Reads / writes one [key1]..[keyE] section."""

    def __init__(self, doc, n):
        self.doc, self.n = doc, n

    @property
    def sec(self):
        return self.doc.sec(KEYSEC[self.n], create=True)

    def times(self):
        try:
            return clamp(int(groups(self.sec.get("keytimes", "[1]"))[0]), 1, 10)
        except Exception:
            return 1

    def set_times(self, t):
        t = clamp(t, 1, 10)
        old = self.times()
        self.sec.set("keytimes", f"[{t}]")
        for n in range(old + 1, t + 1):                 # give new presses a sensible LED colour
            if self.sec.find(f"ledcolor{n}") is None:
                self.set_led(n, self.led(n - 1))
        tidy_key_section(self.sec)

    def ledmode(self):
        g = groups(self.sec.get("ledmode", "[normal]"))
        return g[0] if g else "normal"

    def set_ledmode(self, m):
        self.sec.set("ledmode", f"[{m}]")
        tidy_key_section(self.sec)

    def led(self, n):
        raw = self.sec.get(f"ledcolor{n}")
        if raw:
            return parse_led(raw)
        if n == 0:                                      # no start colour written: the key starts in press 1's colour
            return self.led(1)
        return [0x666666] * 3

    def set_led(self, n, cols):
        self.sec.set(f"ledcolor{n}", fmt_led(cols))
        tidy_key_section(self.sec)

    def cmds(self, trig, n):
        return parse_cmds(self.sec.get(f"{trig}{n}"))

    def set_cmds(self, trig, n, cmds):
        k = f"{trig}{n}"
        if cmds:
            self.sec.set(k, fmt_cmds(cmds))
        else:
            self.sec.delete(k)
        tidy_key_section(self.sec)

    def name(self, trig, n):
        g = groups(self.sec.get(f"{trig}{n}_name", ""))
        return g[0] if g else ""

    def set_name(self, trig, n, v):
        k = f"{trig}{n}_name"
        if v:
            self.sec.set(k, f"[{v}]")
        else:
            self.sec.delete(k)
        tidy_key_section(self.sec)

    def clear(self):
        self.sec.items = []
        self.sec.set("keytimes", "[1]")
        self.sec.set("ledmode", "[normal]")
        self.sec.set("ledcolor0", fmt_led([0x666666] * 3))
        self.sec.set("ledcolor1", fmt_led([0x666666] * 3))
        tidy_key_section(self.sec)


PC_NAMES = {"bank_inc": "BANK+", "bank_dec": "BANK-", "value_inc": "PC+", "value_dec": "PC-"}


def cmd_short(c):
    ty = ctype(c)
    if is_std(c):
        ch, _t, a, b = c
        if ty == "PC":
            if a in PC_NAMES:
                return PC_NAMES[a]
            return f"slot{a[4:]}" if a.startswith("auto") else f"PC {a}"
        if ty == "CC":
            return f"CC{a} {b}" if not b.isdigit() else f"CC{a}={b}"
        if ty == "CCT":
            return f"CC{a} tgl"
        if ty == "NT":
            return f"NOTE {a}"
    if ty == "PRESET":
        return "PAGE+" if c[1] == "value_inc" else "PAGE-" if c[1] == "value_dec" else f"PAGE {c[1]}"
    if ty == "HID":
        return " ".join(x for x in c[1:] if x.upper() != "END")[:14]
    if ty == "TIME":
        return "TIME " + (c[1] if len(c) > 1 else "")
    return " ".join(c[:2])[:14]


# =============================================================================
# DEVICE PROFILES  (names for CC numbers, so the editor can say what a key does)
# kinds: toggle = 127 on / 0 off, trigger = single 127, step = 127 up / 0 down, value = 0..100 or 0..127
# =============================================================================
GP200 = {
    "name": "Valeton GP-200",
    "note": ("Patches: Bank Select CC0 (0 = banks 01-32, 1 = banks 33-64 - checked on a real unit, the manual says the opposite) then Program Change 0-127. "
             "The GP-200 listens on the channel set in its MIDI menu."),
    "items": [
        (0, "Bank select (0 = 01-32, 1 = 33-64)", "value"),
        (7, "Patch volume", "value"), (11, "EXP 1 pedal", "value"), (13, "EXP 1 A/B", "toggle"),
        (16, "Quick Access knob 1", "value"), (17, "Quick Access 1 step", "step"),
        (18, "Quick Access knob 2", "value"), (19, "Quick Access 2 step", "step"),
        (20, "Quick Access knob 3", "value"), (21, "Quick Access 3 step", "step"),
        (22, "BANK - (initial)", "trigger"), (23, "BANK + (initial)", "trigger"),
        (24, "PATCH -", "trigger"), (25, "PATCH +", "trigger"),
        (26, "BANK - (wait)", "trigger"), (27, "BANK + (wait)", "trigger"), (28, "BANK (wait)", "trigger"),
        (48, "PRE", "toggle"), (49, "DST", "toggle"), (50, "AMP", "toggle"), (51, "NR", "toggle"),
        (52, "CAB", "toggle"), (53, "EQ", "toggle"), (54, "MOD", "toggle"), (55, "DLY", "toggle"),
        (56, "RVB", "toggle"), (57, "WAH", "toggle"), (58, "TUNER", "toggle"), (59, "LOOPER", "toggle"),
        (60, "Looper record", "trigger"), (61, "Looper auto record", "trigger"),
        (62, "Looper play / stop", "toggle"), (63, "Looper half speed", "toggle"),
        (64, "Looper reverse", "toggle"), (65, "Looper delete", "trigger"),
        (66, "Looper rec volume", "value"), (67, "Looper play volume", "value"), (68, "Looper placement", "toggle"),
        (69, "CTRL 1", "toggle"), (70, "CTRL 2", "toggle"), (71, "CTRL 3", "toggle"), (72, "CTRL 4", "toggle"),
        (75, "TAP tempo", "trigger"),
        (76, "CTRL 5", "toggle"), (77, "CTRL 6", "toggle"), (78, "CTRL 7", "toggle"), (79, "CTRL 8", "toggle"),
        (92, "Drum machine menu", "toggle"), (93, "Drum play / stop", "toggle"),
        (94, "Drum type", "value"), (95, "Drum volume", "value"),
    ],
}
GENERIC = {
    "name": "Generic MIDI",
    "note": "Common controller numbers. Use for any synth, effect unit or DAW.",
    "items": [
        (1, "Mod wheel", "value"), (7, "Volume", "value"), (10, "Pan", "value"), (11, "Expression", "value"),
        (64, "Sustain pedal", "toggle"), (65, "Portamento", "toggle"), (66, "Sostenuto", "toggle"),
        (91, "Reverb level", "value"), (93, "Chorus level", "value"),
        (120, "All sound off", "trigger"), (123, "All notes off", "trigger"),
    ],
}
PODPRO = {
    "name": 'Line 6 POD Pro',
    "note": ('Switches: 0 = off, 127 = on. Amp model (CC12), cab (CC71) and effect (CC19) are selected by value; use the POD app to see the names. Program Change loads a stored program.'),
    "items": [
        (1, 'Tweak', 'value'), (4, 'Wah Pos', 'value'), (7, 'Volume', 'value'),
        (9, 'Dig Out Gain', 'value'), (12, 'Amp model', 'value'), (13, 'Drive', 'value'),
        (14, 'Bass', 'value'), (15, 'Mid', 'value'), (16, 'Treble', 'value'),
        (17, 'Ch Volume', 'value'), (18, 'Rev Level', 'value'), (19, 'Effect', 'value'),
        (20, 'Drive 2', 'value'), (21, 'Presence', 'value'), (22, 'Noise Gate', 'toggle'),
        (23, 'Gate Thresh', 'value'), (24, 'Gate Decay', 'value'), (25, 'Distortion', 'toggle'),
        (26, 'Drive Boost', 'toggle'), (27, 'Presence On', 'toggle'), (28, 'Delay', 'toggle'),
        (30, 'Delay Time', 'value'), (32, 'Delay Fdbk', 'value'), (34, 'Delay Level', 'value'),
        (36, 'Reverb', 'toggle'), (37, 'Reverb Type', 'toggle'), (38, 'Rev Decay', 'value'),
        (39, 'Rev Tone', 'value'), (40, 'Rev Diffusion', 'value'), (41, 'Rev Density', 'value'),
        (42, 'Compressor', 'value'), (43, 'Wah', 'toggle'), (44, 'Wah Low', 'value'),
        (45, 'Wah High', 'value'), (46, 'Vol Min', 'value'), (47, 'Vol Pedal', 'toggle'),
        (48, 'Vol Swell', 'toggle'), (49, 'Swell Ramp', 'value'), (50, 'Mod FX', 'toggle'),
        (51, 'Mod Speed', 'value'), (52, 'Mod Depth', 'value'), (53, 'Feedback', 'toggle'),
        (54, 'PreDelay', 'value'), (55, 'Rotary', 'toggle'), (56, 'Rotary Max', 'value'),
        (57, 'Rotary Min', 'value'), (58, 'Trem Speed', 'value'), (59, 'Trem Depth', 'value'),
        (62, 'Delay Fine', 'value'), (71, 'Cabinet', 'value'), (73, 'Bright', 'toggle'),
    ],
}
VAMP = {
    "slots": 5,
    "name": 'Behringer V-AMP (Mk1)',
    "note": ('Switches: 0 = off, 127 = on. Amp (CC19), effect type (CC20), cabinet (CC23) and reverb type (CC24) are selected by number 0-15; use the V-AMP app to see the names. Program Change loads a preset.'),
    "items": [
        (7, 'Vol Pedal', 'value'), (12, 'Gain', 'value'), (13, 'Treble', 'value'),
        (14, 'Mid', 'value'), (15, 'Bass', 'value'), (16, 'Volume', 'value'),
        (18, 'Reverb Mix', 'value'), (19, 'Amp', 'value'), (20, 'Effect type', 'value'),
        (21, 'Effects', 'toggle'), (22, 'Reverb', 'toggle'), (23, 'Cabinet', 'value'),
        (24, 'Reverb type', 'value'), (25, 'Gate', 'value'), (26, 'Drive', 'toggle'),
        (27, 'Wah Pedal', 'value'), (44, 'Pre Fx Type', 'value'), (45, 'Pre 1', 'value'),
        (46, 'Pre 2', 'value'), (47, 'Pre 3', 'value'), (48, 'Pre 4', 'value'),
        (49, 'Delay Type', 'value'), (50, 'Time Hi', 'value'), (51, 'Time Lo', 'value'),
        (52, 'Spread', 'value'), (53, 'Feedback', 'value'), (54, 'Delay Mix', 'value'),
        (55, 'Post Fx Mode', 'value'), (56, 'Post 1', 'value'), (57, 'Post 2', 'value'),
        (58, 'Post 3', 'value'), (59, 'FX Mix', 'value'),
    ],
}

PROFILES = [GP200, PODPRO, VAMP, GENERIC]
TOGGLE_COLOURS = [0x0000ff, 0x00ff00, 0xff8200, 0xaa0000, 0x00ffff, 0x7b0050, 0xffd000, 0xff3080, 0x60c000, 0x04c3ff]


def profile_lookup(profile, cc):
    for n, name, kind in profile["items"]:
        if str(n) == str(cc):
            return name, kind
    return None, None


def describe_cmd(c, profile):
    ty = ctype(c)
    if is_std(c):
        ch, _t, a, b = c
        if ty == "PC":
            if a == "bank_inc":
                return "next group of patches"
            if a == "bank_dec":
                return "previous group of patches"
            if a == "value_inc":
                return "program change up by one"
            if a == "value_dec":
                return "program change down by one"
            if a.startswith("auto"):
                return f"patch slot {a[4:]} of the current group (auto)"
            return f"program change {a}"
        if ty in ("CC", "CCT"):
            name, kind = profile_lookup(profile, a)
            v = int(b) if b.isdigit() else None
            pre = "toggle: " if ty == "CCT" else ""
            if b in ("value_inc", "value_dec", "bank_inc", "bank_dec", "autotap") or b.startswith("auto"):
                return f"{pre}{name or 'controller ' + a}  ({b})"
            if name is None:
                return f"{pre}controller {a} = {b}"
            if kind == "toggle" and v is not None:
                return f"{pre}{name}  {'ON' if v >= 64 else 'OFF'}"
            if kind == "step" and v is not None:
                return f"{pre}{name}  {'up' if v >= 64 else 'down'}"
            if kind == "trigger":
                return pre + name
            return f"{pre}{name} = {b}"
        if ty == "NT":
            try:
                n = int(a)
                nm = f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"
            except ValueError:
                nm = a
            return f"note {nm}, velocity {b}"
    if ty == "PRESET":
        return {"value_inc": "go to the next page", "value_dec": "go to the previous page"}.get(c[1], f"page command {c[1]}")
    if ty == "HID":
        return "keyboard / mouse: " + " ".join(c[1:-1] if c[-1].upper() == "END" else c[1:])
    if ty == "TIME":
        return "time engine: " + (c[1] if len(c) > 1 else "")
    if ty == "SYSEX":
        return "system exclusive"
    if ty == "SYSCMN":
        return "system common: " + " ".join(c[1:])
    if ty == "REALTIME":
        return "realtime: " + (c[1] if len(c) > 1 else "")
    if ty == "EXP":
        return "sets what the expression pedal sends"
    return ""
# ---- FW4 -> FW5 conversion (same as captain_fw5_convert.py), used when old pages are opened
FW4_KEYS = {"key0": "key1", "key1": "key2", "key2": "key3", "key3": "key4", "key4": "key5",
            "key5": "keyA", "key6": "keyB", "key7": "keyC", "key8": "keyD", "key9": "keyE"}
FW4_PRESS_RE = re.compile(r"^(ledcolor|short_dw|short_up|long_up|long)(\d+)(_name)?$")
FW4_RANK = {"ledcolor": 0, "short_dw": 1, "short_up": 2, "long": 3, "long_up": 4}


def fw4_parse(text):
    """-> (globals dict, {section: [(key, raw)]}) ; section names lower-cased, order kept in dict"""
    gl, secs, cur = {}, {}, None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^\[([A-Za-z0-9_.]+)\]$", s)
        if m:
            cur = m.group(1)
            if cur.lower() not in ("globalsetup", "page"):
                secs.setdefault(cur, [])
            continue
        if "=" in s and cur is not None:
            k, v = s.split("=", 1)
            if cur.lower() in ("globalsetup", "page"):
                gl[k.strip()] = v.strip()
            else:
                secs[cur].append((k.strip(), v.strip()))
    return gl, secs


def fw4_conv_cmd(c, notes, where):
    """c = list of up to 4 strings.  Returns a converted command (list of strings)."""
    c = (c + ["-"] * 4)[:4]
    a, b, d, e = c
    if b == "PC":
        if d == "auto" and e in ("bank_inc", "bank_dec"):
            return [a, "PC", e, "-"]
        if d == "auto" and re.fullmatch(r"\d", e):
            return [a, "PC", f"auto{e}", "-"]
        if d in ("inc1", "dec1"):
            return [a, "PC", "value_inc" if d == "inc1" else "value_dec", "-"]
        if re.fullmatch(r"(inc|dec)[2-5]", d) or d == "random":
            notes.append(f"{where}: PC '{d}' has no FW5 equivalent - left as it was, please check")
    if b == "CC" and e == "auto":
        notes.append(f"{where}: CC auto - check against the FW5 CC auto commands")
    if a in ("send", "press", "release", "delay") and b == "HID":
        mod, key = d, e
        if a == "delay":
            return ["HID", "delay", key, "END"]
        if key.lower() == "all":
            notes.append(f"{where}: HID release all has no FW5 equivalent - please check")
            return c
        parts = ["HID", a]
        if mod not in ("-", ""):
            parts.append(mod.lower())
        parts += [key, "END"]
        return parts
    return [a, b, d, e]


def fw4_conv_raw(raw, notes, where):
    g = groups(raw)
    if not g:
        return raw
    # variable-length forms (HID) start with a keyword, not a channel number; commands are 4 groups otherwise
    out, i = [], 0
    while i < len(g):
        chunk = g[i:i + 4]
        cc = fw4_conv_cmd(chunk, notes, where)
        out.append("".join(f"[{x}]" for x in cc))
        i += 4
    return " ".join(out)


def fw4_convert(text, pageno, notes, base=None):
    gl, secs = fw4_parse(text)
    for k, v in (base or {}).items():
        gl.setdefault(k, v)
    name = (gl.get("page_name") or f"PG{pageno}").strip("[]")
    first = pageno == 0

    def val(k, default):
        v = gl.get(k)
        return v if v is not None and v != "" else default

    L = ["[GlobalSetup]", f"page_name = [{name}]"]
    L += [f"ledbright = {val('ledbright', '[80]')}", f"screenbright = {val('screenbright', '[80]')}",
          f"long_press_timing = {val('long_press_timing', '[1]')}",
          f"WIRELESS_2.4G = {val('WIRELESS_2.4G', '[on]')}", f"WIRELESS_ID   = {val('WIRELESS_ID', '[8]')}",
          f"WIRELESS_dB   = {val('WIRELESS_dB', '[6]')}", "BATTERY_CHARGE= [off]",
          f"group_number = {val('group_number', '[4]')}", f"display_number_ABC = {val('display_number_ABC', '[abc4]')}",
          f"display_bank_offset = {val('display_bank_offset', '[1]')}", "pc_open_num = [0]", "PC_Bank_Select = [1]"]
    L += [f"PC_group_{i} = " for i in range(10)]
    L += ["CC_value_gropnumber = ", "tapcountMIN = [20]", "tapcountMAX = [256]",
          f"midithrough = {val('midithrough', '[on]')}", "externalmidicontrol = [on]", "externalmidicontrolch = [16]",
          "encoderCH = [1]", f"encoderCCnum = {val('encoder_CC', '[43]')}", "encoderkeyCCtoggle = [40]",
          f"exp1_CH = {val('exp1_CH', '[1]')}", f"exp1_CC = {val('exp1_CC', '[1]')}", "exp1_min = [0]", "exp1_max = [127]",
          f"exp2_CH = {val('exp2_CH', '[1]')}", f"exp2_CC = {val('exp2_CC', '[2]')}", "exp2_min = [0]", "exp2_max = [127]",
          "LEDSelectLowLight = [on]", "LowLightLevel = [10]", ""]
    if gl.get("display_pc_offset") == "[0]":
        notes.append(f"page{pageno}: display_pc_offset = 0 (PC numbers shown from 0) has no FW5 setting - the display may now start from 1")
    if gl.get("encoder_NAME"):
        pass
    byname = {}
    for oldname, items in secs.items():
        new = FW4_KEYS.get(oldname.lower())
        if new:
            byname[new] = items
        else:
            notes.append(f"page{pageno}: unknown section [{oldname}] skipped")
    for k in KEYSEC:
        L += ["", f"[{k}]"]
        items = byname.get(k)
        if items is None:
            L += ["keytimes = [1]", "ledmode = [normal]"]
            continue
        plain, pressed = [], []
        times = 1
        for key, raw in items:
            if key == "keytimes":
                m = re.search(r"\d+", raw)
                times = int(m.group()) if m else 1
            mm = FW4_PRESS_RE.match(key)
            if mm:
                pressed.append(((int(mm.group(2)), FW4_RANK[mm.group(1)], 1 if mm.group(3) else 0), key, raw))
            else:
                plain.append((key, raw))
        pressed.sort(key=lambda t: t[0])
        for key, raw in plain:
            L.append(f"{key} = {raw}")
        last_led = None
        for _o, key, raw in pressed:
            if key.startswith("ledcolor"):
                if int(FW4_PRESS_RE.match(key).group(2)) == times:
                    last_led = raw
        cols = {int(FW4_PRESS_RE.match(key).group(2)): raw for _o, key, raw in pressed if key.startswith("ledcolor")}
        L.append(f"ledcolor0 = {last_led or cols.get(1) or '[0x000000][0x000000][0x000000]'}")
        # nav keys: the old firmware turned pages by itself on the two end keys, FW5 needs a command
        nav = {"key5": "[preset][value_inc]", "keyE": "[preset][value_dec]"}.get(k)
        has_long = any(key == "long1" for _o, key, _r in pressed)
        for _o, key, raw in pressed:
            if FW4_PRESS_RE.match(key).group(3):
                L.append(f"{key} = {raw}")
            elif key.startswith("ledcolor"):
                L.append(f"{key} = {raw}")
            else:
                L.append(f"{key} = {fw4_conv_raw(raw, notes, f'page{pageno} {k} {key}')}")
        if nav and not has_long:
            L.append(f"long1 = {nav}")
    return "\n".join(L) + "\n"




# =============================================================================
# EXTRA MODAL WIDGETS
# =============================================================================
class BigPopup(Popup):
    ROW_H, ROWS = 26, 18

    def __init__(self, title, options, current, on_pick):
        Popup.__init__(self, title, options, current, on_pick)


SWATCHES = [0xffffff, 0xcccccc, 0x666666, 0x2d2d2d, 0xff0000, 0xaa0000, 0x460000, 0xff4000,
            0xff8200, 0xffb000, 0xffd000, 0xffff00, 0xc0ff00, 0x60c000, 0x00ff00, 0x006e00,
            0x00ff90, 0x00ffff, 0x04c3ff, 0x0070ff, 0x0000ff, 0x00002d, 0x4000ff, 0x8000ff,
            0xff00ff, 0xff3080, 0x7b0050, 0x420050, 0x000000, 0x002d00, 0x606d00, 0x2d2d46]


class ColorPopup:
    """Pick one LED colour from swatches, or type a 6-digit hex value."""

    def __init__(self, title, current, on_pick):
        self.title, self.current, self.on_pick = title, current, on_pick
        self.panel = pygame.Rect(0, 0, 560, 360)
        self.panel.center = (W // 2, H // 2 - 20)
        self.hexbuf = f"{current:06x}"
        self.focus = False
        self.rects = []
        for i, c in enumerate(SWATCHES):
            r, c_ = divmod(i, 8)
            self.rects.append((pygame.Rect(self.panel.x + 24 + c_ * 64, self.panel.y + 56 + r * 56, 56, 48), c))
        self.hexrect = pygame.Rect(self.panel.x + 120, self.panel.bottom - 82, 130, 34)
        self.okrect = pygame.Rect(self.panel.x + 270, self.panel.bottom - 82, 90, 34)
        self.cancel = pygame.Rect(self.panel.x + 372, self.panel.bottom - 82, 90, 34)

    def value(self):
        try:
            return int(self.hexbuf, 16) & 0xFFFFFF
        except ValueError:
            return None

    def handle(self, e, app):
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_ESCAPE:
                app.popup = None
            elif self.focus:
                if e.key == pygame.K_BACKSPACE:
                    self.hexbuf = self.hexbuf[:-1]
                elif e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    self._ok(app)
                elif e.unicode and e.unicode in "0123456789abcdefABCDEF" and len(self.hexbuf) < 6:
                    self.hexbuf += e.unicode.lower()
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for r, c in self.rects:
                if r.collidepoint(e.pos):
                    app.popup = None
                    self.on_pick(c)
                    return True
            self.focus = self.hexrect.collidepoint(e.pos)
            if self.okrect.collidepoint(e.pos):
                self._ok(app)
            elif self.cancel.collidepoint(e.pos) or not self.panel.collidepoint(e.pos):
                app.popup = None
        return True

    def _ok(self, app):
        v = self.value()
        if v is not None:
            app.popup = None
            self.on_pick(v)

    def draw(self, surf, app):
        dim = pygame.Surface((W, H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 170))
        surf.blit(dim, (0, 0))
        rounded_metal(surf, self.panel, 12)
        pygame.draw.rect(surf, AMBER_DIM, self.panel, 1, border_radius=12)
        txt(surf, " ".join(self.title.upper()), 13, AMBER, (self.panel.x + 18, self.panel.y + 16), bold=True)
        mouse = pygame.mouse.get_pos()
        for r, c in self.rects:
            pygame.draw.rect(surf, (8, 8, 8), r.inflate(4, 4), border_radius=6)
            pygame.draw.rect(surf, rgb(c), r, border_radius=5)
            if c == self.current:
                pygame.draw.rect(surf, (255, 255, 255), r.inflate(4, 4), 2, border_radius=6)
            if r.collidepoint(mouse):
                pygame.draw.rect(surf, AMBER, r.inflate(6, 6), 2, border_radius=7)
        txt(surf, "HEX  0x", 14, DIM, (self.panel.x + 30, self.hexrect.centery), "midleft")
        pygame.draw.rect(surf, WELL, self.hexrect, border_radius=5)
        pygame.draw.rect(surf, AMBER if self.focus else BTN_EDGE, self.hexrect, 1, border_radius=5)
        shown = self.hexbuf + ("_" if self.focus and int(time.time() * 2) % 2 == 0 else "")
        txt(surf, shown, 16, TEXT, (self.hexrect.x + 8, self.hexrect.centery), "midleft", bold=True)
        v = self.value()
        if v is not None:
            pygame.draw.rect(surf, rgb(v), (self.hexrect.x, self.hexrect.bottom + 6, 130, 12), border_radius=3)
        for r, lab in ((self.okrect, "OK"), (self.cancel, "CANCEL")):
            hover = r.collidepoint(mouse)
            pygame.draw.rect(surf, BTN_HOT if hover else BTN, r, border_radius=6)
            pygame.draw.rect(surf, AMBER if hover else BTN_EDGE, r, 1, border_radius=6)
            txt(surf, lab, 14, (255, 255, 255), r.center, "center", bold=True)


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


GLOBAL_KEYS = ["page_name", "ledbright", "screenbright", "long_press_timing", "WIRELESS_2.4G", "WIRELESS_ID", "WIRELESS_dB",
               "BATTERY_CHARGE", "group_number", "display_number_ABC", "display_bank_offset", "pc_open_num", "PC_Bank_Select",
               "PC_group_0", "PC_group_1", "PC_group_2", "PC_group_3", "PC_group_4", "PC_group_5", "PC_group_6", "PC_group_7",
               "PC_group_8", "PC_group_9", "CC_value_gropnumber", "tapcountMIN", "tapcountMAX", "midithrough",
               "externalmidicontrol", "externalmidicontrolch", "encoderCH", "encoderCCnum", "encoderkeyCCtoggle",
               "exp1_CH", "exp1_CC", "exp1_min", "exp1_max", "exp2_CH", "exp2_CC", "exp2_min", "exp2_max",
               "LEDSelectLowLight", "LowLightLevel"]
GLOBAL_SHARED = ["ledbright", "screenbright", "long_press_timing", "WIRELESS_2.4G", "WIRELESS_ID", "WIRELESS_dB", "BATTERY_CHARGE"]


def new_page_text(name="NEW", base=None):
    """A fresh FW5 page: global block (copying the shared values from `base`, a PageDoc) + ten plain keys, PC+/PC- on the end keys."""
    g = base.sec(GSEC) if base is not None else None

    def sh(k, d):
        v = g.get(k) if g else None
        return v if v else d
    L = [f"[{GSEC}]", f"page_name = [{name}]", f"ledbright = {sh('ledbright', '[80]')}", f"screenbright = {sh('screenbright', '[80]')}",
         f"long_press_timing = {sh('long_press_timing', '[1]')}", f"WIRELESS_2.4G = {sh('WIRELESS_2.4G', '[on]')}",
         f"WIRELESS_ID   = {sh('WIRELESS_ID', '[8]')}", f"WIRELESS_dB   = {sh('WIRELESS_dB', '[6]')}",
         f"BATTERY_CHARGE= {sh('BATTERY_CHARGE', '[off]')}", "group_number = [4]", "display_number_ABC = [abc4]",
         "display_bank_offset = [1]", "pc_open_num = [0]", "PC_Bank_Select = [1]"]
    L += [f"PC_group_{i} = " for i in range(10)]
    L += ["CC_value_gropnumber = ", "tapcountMIN = [20]", "tapcountMAX = [256]", "midithrough = [on]",
          "externalmidicontrol = [on]", "externalmidicontrolch = [16]", "encoderCH = [1]", "encoderCCnum = [43]",
          "encoderkeyCCtoggle = [40]", "exp1_CH = [1]", "exp1_CC = [1]", "exp1_min = [0]", "exp1_max = [127]",
          "exp2_CH = [1]", "exp2_CC = [2]", "exp2_min = [0]", "exp2_max = [127]", "LEDSelectLowLight = [on]", "LowLightLevel = [10]", ""]
    grey = "[0x666666][0x666666][0x666666]"
    for k in KEYSEC:
        L += ["", f"[{k}]", "keytimes = [1]", "ledmode = [normal]"]
        if k == "key5":
            L += ["ledcolor0 = [0xFFFFFF][0xFFFFFF][0xFFFFFF]", "ledcolor1 = [0xFFFFFF][0xFFFFFF][0xFFFFFF]", "short_up1_name = [PC_+]",
                  "short_up1 = [1][PC][value_inc][-]", "long1 = [preset][value_dec]"]
        elif k == "keyE":
            L += ["ledcolor0 = [0xFFFFFF][0xFFFFFF][0xFFFFFF]", "ledcolor1 = [0xFFFFFF][0xFFFFFF][0xFFFFFF]", "short_up1_name = [PC_-]",
                  "short_up1 = [1][PC][value_dec][-]", "long1 = [preset][value_inc]"]
        else:
            L += [f"ledcolor0 = {grey}", f"ledcolor1 = {grey}"]
    return "\n".join(L) + "\n"
# the GP-200 starter page (your page0.txt, long-press patch slots on both toggle states, CC0 groups as the real unit wants them)
GP200_PAGE = r'''[GlobalSetup]
page_name = [GP2001]
ledbright = [50]
screenbright = [80]
long_press_timing = [1]
WIRELESS_2.4G = [on]
WIRELESS_ID   = [8]
WIRELESS_dB   = [6]
BATTERY_CHARGE= [off]
group_number = [4]
display_number_ABC = [abc4]
display_bank_offset = [1]
pc_open_num = [0]
PC_Bank_Select = [2]
PC_group_0 = [1][CC][0][0]
PC_group_1 = [1][CC][0][1]
PC_group_2 = 
PC_group_3 = 
PC_group_4 = 
PC_group_5 = 
PC_group_6 = 
PC_group_7 = 
PC_group_8 = 
PC_group_9 = 
CC_value_gropnumber = 
tapcountMIN = [20]
tapcountMAX = [256]
midithrough = [on]
externalmidicontrol = [on]
externalmidicontrolch = [16]
encoderCH = [1]
encoderCCnum = [43]
encoderkeyCCtoggle = [40]
exp1_CH = [1]
exp1_CC = [1]
exp1_min = [0]
exp1_max = [127]
exp2_CH = [1]
exp2_CC = [2]
exp2_min = [0]
exp2_max = [127]
LEDSelectLowLight = [on]
LowLightLevel = [10]


[key1]
keytimes = [2]
ledmode = [normal]
ledcolor0 = [0x000000][0x000000][0x420050]
ledcolor1 = [0x4000FF][0x4000FF][0x4000FF]
short_up1_name = [MOD]
short_up1 = [1][CC][54][127]
ledcolor2 = [0x000000][0x000000][0x420050]
short_up2_name = [MOD]
short_up2 = [1][CC][54][0]

[key2]
keytimes = [2]
ledmode = [normal]
ledcolor0 = [0x000000][0x000000][0x002D00]
ledcolor1 = [0x60C000][0x60C000][0x60C000]
short_up1_name = [EQ]
short_up1 = [1][CC][53][127]
ledcolor2 = [0x000000][0x000000][0x002D00]
short_up2_name = [EQ]
short_up2 = [1][CC][53][0]

[key3]
keytimes = [1]
ledmode = [tap]
ledcolor0 = [0x04C3FF][0x04C3FF][0x04C3FF]
ledcolor1 = [0x04C3FF][0x04C3FF][0x04C3FF]
short_dw1_name = [TAPtem]
short_dw1 = [1][CC][75][127]

[key4]
keytimes = [2]
ledmode = [normal]
ledcolor0 = [0x000000][0x000000][0x112200]
ledcolor1 = [0x8000FF][0x8000FF][0x8000FF]
short_up1_name = [TUNER]
short_up1 = [1][CC][58][127]
ledcolor2 = [0x000000][0x000000][0x112200]
short_up2_name = [TUNER]
short_up2 = [1][CC][58][0]

[key5]
keytimes = [1]
ledmode = [normal]
ledcolor0 = [0xFFFFFF][0xFFFFFF][0xFFFFFF]
ledcolor1 = [0xFFFFFF][0xFFFFFF][0xFFFFFF]
short_dw1_name = [BANK+]
short_dw1 = [1][PC][bank_inc][-]
long1_name = [INC]
long1 = [preset][value_inc]

[keyA]
keytimes = [2]
ledmode = [normal]
ledcolor0 = [0x000000][0x2D2D46][0x000000]
ledcolor1 = [0x00FFFF][0x00FFFF][0x00FFFF]
short_up1_name = [PRE]
short_up1 = [1][CC][48][127]
long1 = [1][PC][auto0][-]
ledcolor2 = [0x000000][0x2D2D46][0x000000]
short_up2_name = [PRE]
short_up2 = [1][CC][48][0]
long2 = [1][PC][auto0][-]

[keyB]
keytimes = [2]
ledmode = [normal]
ledcolor0 = [0x000000][0x460000][0x000000]
ledcolor1 = [0xFF0000][0xFF0000][0xFF0000]
short_up1_name = [DST]
short_up1 = [1][CC][49][127]
long1 = [1][PC][auto1][-]
ledcolor2 = [0x000000][0x460000][0x000000]
short_up2_name = [DST]
short_up2 = [1][CC][49][0]
long2 = [1][PC][auto1][-]

[keyC]
keytimes = [2]
ledmode = [normal]
ledcolor0 = [0x000000][0x460000][0x000000]
ledcolor1 = [0x7B0050][0x7B0050][0x7B0050]
short_up1_name = [DLY]
short_up1 = [1][CC][55][127]
long1 = [1][PC][auto2][-]
ledcolor2 = [0x000000][0x460000][0x000000]
short_up2_name = [DLY]
short_up2 = [1][CC][55][0]
long2 = [1][PC][auto2][-]

[keyD]
keytimes = [2]
ledmode = [normal]
ledcolor0 = [0x000000][0x606D00][0x000000]
ledcolor1 = [0xFFD000][0xFFD000][0xFFD000]
short_up1_name = [RVB]
short_up1 = [1][CC][56][127]
long1 = [1][PC][auto3][-]
ledcolor2 = [0x000000][0x606D00][0x000000]
short_up2_name = [RVB]
short_up2 = [1][CC][56][0]
long2 = [1][PC][auto3][-]

[keyE]
keytimes = [1]
ledmode = [normal]
ledcolor0 = [0xFFFFFF][0xFFFFFF][0xFFFFFF]
ledcolor1 = [0xFFFFFF][0xFFFFFF][0xFFFFFF]
short_up1_name = [BANK-]
short_up1 = [1][PC][bank_dec][-]
long1_name = [DEC]
long1 = [preset][value_dec]
'''




# =============================================================================
# THE APP
# =============================================================================
# ring segment centres (degrees, screen coordinates: 0 = right, 90 = down, 180 = left, 270 = up)
SEG_ANGLES_TOP = [210, -30, 90]     # keys 1-4, bank+  : left, right, bottom (three equal arcs)
SEG_ANGLES_BOT = [150, 270, 30]     # keys A-D, bank- : left, top, right (the ring turned 180 degrees)
PC_WORDS = ["value_inc", "value_dec", "bank_inc", "bank_dec"] + [f"auto{i}" for i in range(10)]
CC_WORDS = ["value_inc", "value_dec", "bank_inc", "bank_dec", "autotap"] + [f"auto{i}" for i in range(10)]
HID_VERBS = ["send", "press", "release", "delay"]
HID_MODS = ["ctrl", "shift", "alt", "option", "windows", "command"]
TYPE_DEFAULTS = {"PC": ["1", "PC", "0", "-"], "CC": ["1", "CC", "0", "127"], "CCT": ["1", "CCT", "12", "127"], "NT": ["1", "NT", "60", "127"],
                 "PRESET": ["preset", "value_inc"], "HID": ["HID", "send", "A", "END"], "SYSEX": ["SysEX", "00", "END"],
                 "TIME": ["timeengine", "begin"], "SYSCMN": ["SysCmn", "SongSelect", "1"], "REALTIME": ["realtime", "start"],
                 "EXP": ["EXPCCchange", "EXP1", "1", "43", "0", "127"]}
CMD_TYPES = ["PC", "CC", "CCT", "NT", "PRESET", "HID", "SYSEX", "TIME", "SYSCMN", "REALTIME", "EXP"]
RAW_TEMPLATES = {
    "PRESET": ["preset value_inc", "preset value_dec"],
    "TIME": ["timeengine begin", "timeengine stop", "timeengine record", "timeengine value_inc", "timeengine value_dec"],
    "REALTIME": ["realtime start", "realtime continue", "realtime stop", "realtime systemreset"],
    "SYSCMN": ["SysCmn SongSelect 1", "SysCmn SongSelectInc -", "SysCmn SongSelectDec -", "SysCmn SongPP 100", "SysCmn SongPPinc -",
               "SysCmn SongPPdec -", "SysCmn SongPPreset -"],
    "HID": ["HID send ctrl C END", "HID send ctrl V END", "HID send ctrl Z END", "HID send space END", "HID send enter END",
            "HID send play_pause END", "HID send vol_up END", "HID send vol_down END", "HID send next_track END",
            "HID send prev_track END", "HID send F5 END", "HID press shift END", "HID release shift END", "HID delay 50 END",
            "HID send mouse_L END"],
    "SYSEX": ["SysEX 00 END"],
    "EXP": ["EXPCCchange EXP1 1 43 0 127", "EXPCCchange EXP2 1 11 0 127"],
}


# starter pages.  Top row = patch slots (then any spare keys), bottom row = stomp toggles, D / E = bank down / up.
# toggles: (CC, label, colour or None, off-look or None, extra commands for the ON press)
OFF_GREEN = [0x000000, 0x002D00, 0x000000]
STARTERS = {
    "Valeton GP-200": dict(page="GP200", ch=1, slots=4, slotcol=None,
                           toggles=[(49, "DST", None, None, []), (54, "MOD", None, None, []), (55, "DLY", None, None, []), (56, "RVB", None, None, [])],
                           extra={"PC_Bank_Select": "[2]", "PC_group_0": "[1][CC][0][0]", "PC_group_1": "[1][CC][0][1]"}, long_map=None),
    "Behringer V-AMP (Mk1)": dict(page="VAMP", ch=2, slots=5, slotcol=0xFF0000,
                                  toggles=[(21, "Effects", 0x00FF00, OFF_GREEN, []), (22, "Reverb", 0xFF8200, OFF_GREEN, []),
                                           (26, "Drive", 0xFFD000, OFF_GREEN, [["1", "CC", "16", "100"]])],
                                  extra={"display_number_ABC": "[abc5]", "encoderCCnum": "[12]"},
                                  long_map=None),
    "Line 6 POD Pro": dict(page="PODPRO", ch=3, slots=4, slotcol=None,
                           toggles=[(25, "Dist", None, None, []), (50, "ModFX", None, None, []), (28, "Delay", None, None, []), (36, "Reverb", None, None, [])],
                           extra={}, long_map=None),
}


# long press page turn on every page: key5 = previous page, keyE = next page  (key index 4 and 9)
PAGE_TURN = {4: "value_dec", 9: "value_inc"}


class App:
    def __init__(self, headless=False, folder=None, page=0, key=0):
        pygame.init()
        pygame.font.init()
        flags = 0 if headless else (pygame.SCALED | pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("CAPTAIN CHORDSTOMP")
        self.running, self.popup, self.edit = True, None, None
        self._hits, self.hits, self.clip = [], [], None
        self.pages, self.folder, self.dirty = [], None, False
        self.sel_page, self.sel_key, self.press_view = 0, key, {}
        self.page_scroll, self.kscroll, self.kmax = 0, 0, 0
        self.show_global = False
        self.show_factory = False
        self.show_chords = False
        self.cs = dict(sel=[], root=0, ctype=None, inv=0, oct=3, vel=100, ch=1, release=True, playing=[])
        self.bk = {}
        self.backed_up = False
        self.status, self.status_t = "Click FOLDER and pick the supersetup folder of your MIDI Captain.", time.time()
        self.out = None
        self.settings = {}
        try:
            self.settings = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
        self.profile = next((p for p in PROFILES if p["name"] == self.settings.get("profile")), PROFILES[0])
        self.out_devs = []
        if pgmidi and not headless:
            try:
                pgmidi.init()
                self.refresh_devices()
                if self.settings.get("out_name"):
                    self.open_out(self.settings["out_name"])
            except Exception:
                pass
        path = folder or self.settings.get("folder") or self.auto_detect()
        if path:
            self.load_folder(path, quiet=True)
        self.sel_page = clamp(page, 0, max(0, len(self.pages) - 1))
        self.bg = self.make_bg()

    # ---- background ------------------------------------------------------------
    def make_bg(self):
        bg = pygame.Surface((W, H))
        for y in range(H):
            t = y / H
            pygame.draw.line(bg, (int(14 + 18 * (1 - t)), int(20 + 26 * (1 - t)), int(22 + 28 * (1 - t))), (0, y), (W, y))
        return bg

    # ---- settings / midi ----------------------------------------------------------
    def save_settings(self):
        self.settings["profile"] = self.profile["name"]
        if self.folder:
            self.settings["folder"] = str(self.folder)
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    def set_status(self, s):
        self.status, self.status_t = s, time.time()

    def refresh_devices(self):
        self.out_devs = []
        if not pgmidi:
            return
        seen = {}
        for i in range(pgmidi.get_count()):
            _, name, is_in, is_out, _ = pgmidi.get_device_info(i)
            if is_out:
                name = name.decode(errors="replace")
                n = seen.get(name, 0)
                seen[name] = n + 1
                self.out_devs.append((i, f"{name}#{n}"))

    def open_out(self, key):
        try:
            if self.out:
                self.out.close()
        except Exception:
            pass
        self.out = None
        if not key or not pgmidi:
            return
        for i, k in self.out_devs:
            if k == key:
                try:
                    self.out = pgmidi.Output(i)
                except Exception as ex:
                    self.set_status(f"Could not open {key}: {ex}")
                return

    def choose_out(self, _b=1):
        if pgmidi:
            try:
                if self.out:
                    self.out.close()
                    self.out = None
                pgmidi.quit()
                pgmidi.init()
                self.refresh_devices()
            except Exception:
                pass
        opts = [(None, "(none)")] + [(k, k[:-2] if k.endswith("#0") else k) for _, k in self.out_devs]

        def pick(k):
            self.settings["out_name"] = k
            self.open_out(k)
            self.save_settings()
        self.popup = Popup("MIDI OUT used by the TEST buttons (to your GP-200 etc.)", opts, self.settings.get("out_name"), pick)

    def test_cmd(self, c):
        if not is_std(c):
            self.set_status("This command can only run on the Captain itself.")
            return
        ch, ty, a, b = c
        if not self.out:
            self.set_status("Pick a MIDI OUT (top) first - TEST sends the command to your device.")
            return
        try:
            chn = clamp(int(ch), 1, 16) - 1
            ty = ty.upper()
            if ty == "PC" and a.isdigit():
                self.out.write_short(0xC0 | chn, int(a))
            elif ty in ("CC", "CCT") and a.isdigit() and b.isdigit():
                self.out.write_short(0xB0 | chn, int(a), int(b))
            elif ty == "NT" and a.isdigit() and b.isdigit():
                self.out.write_short(0x90 | chn, int(a), int(b))
            else:
                self.set_status("This command can only run on the Captain itself (it needs the pedal's own counters or keyboard).")
                return
            self.set_status(f"Sent {cmd_short(c)} on channel {chn + 1}.")
        except Exception as ex:
            self.set_status(f"Send failed: {ex}")

    # ---- files ---------------------------------------------------------------------
    def auto_detect(self):
        if not sys.platform.startswith("win"):
            return None
        for L in "DEFGHIJKLMNOPQRSTUVWXYZC":
            p = Path(f"{L}:/supersetup")
            try:
                if (p / "page0.txt").exists():
                    return str(p)
            except Exception:
                pass
        return None

    def pick_folder(self, _b=1):
        path = None
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title="Pick the supersetup folder (on the MIDICAPTAIN drive)",
                                           initialdir=self.settings.get("folder") or str(Path.home()))
            root.destroy()
        except Exception:
            self.popup = TextPrompt("Folder", "Type or paste the path of the supersetup folder (e.g. E:\\supersetup):",
                                    self.settings.get("folder", ""), self.load_folder)
            return
        if path:
            self.load_folder(path)

    def load_folder(self, path, quiet=False):
        p = Path(path)
        if not (p / "page0.txt").exists() and (p / "supersetup" / "page0.txt").exists():
            p = p / "supersetup"
        pages, disk, raws = [], [], []
        n = 0
        while (p / f"page{n}.txt").exists():
            try:
                raws.append((p / f"page{n}.txt").read_text(encoding="utf-8", errors="replace"))
            except Exception as ex:
                if not quiet:
                    self.set_status(f"Could not read page{n}.txt: {ex}")
                return
            n += 1
        notes = []
        old_fmt = [r for r in raws if "[keyE]" not in r]
        base = {}
        if old_fmt and raws:
            base = {k: v for k, v in fw4_parse(raws[0])[0].items()
                    if k in ("ledbright", "screenbright", "long_press_timing", "WIRELESS_2.4G", "WIRELESS_ID", "WIRELESS_dB")}
        for i, r in enumerate(raws):
            disk.append(parse_page(r).to_text())
            pages.append(parse_page(r if "[keyE]" in r else fw4_convert(r, i, notes, base)))
        if not pages:
            if not quiet:
                self.set_status(f"No page0.txt found in {p}. Pick the folder called supersetup.")
            return
        self.pages, self.folder, self.dirty, self.disk = pages, p, False, disk
        self.backed = set()
        self.sel_page = clamp(self.sel_page, 0, len(pages) - 1)
        self.backed_up = False
        self.save_settings()
        self.set_status(f"Loaded {len(pages)} page(s) from {p}.")
        if old_fmt:
            self.dirty = True
            self.set_status(f"{len(old_fmt)} page(s) were in the old FW4 format and were converted to FW5 in the editor. "
                            "Nothing is written until you SAVE (originals are backed up first).")
            self.popup = Info("Old (FW4) pages converted", [
                f"{len(old_fmt)} of {len(pages)} pages were in the firmware-4 format. They are shown converted to firmware 5.",
                "Check them, then press SAVE ALL; your original files are copied to captain_backups first."]
                + [n_[:150] for n_ in notes[:8]], [("OK", None)])

    def reload(self, _b=1):
        if not self.folder:
            return
        if self.dirty:
            self.popup = Info("Discard changes?", ["Reloading throws away everything you changed since the last save."],
                              [("RELOAD", lambda: self.load_folder(self.folder)), ("CANCEL", None)])
        else:
            self.load_folder(self.folder)

    def page_changed(self, i):
        return i >= len(self.disk) or self.pages[i].to_text() != self.disk[i]

    def any_changed(self):
        return len(self.pages) != len(self.disk) or any(self.page_changed(i) for i in range(len(self.pages)))

    def save(self, _b=1):
        """Save only the page you are working on (fast)."""
        if not self.pages:
            return False
        if len(self.pages) != len(self.disk) or self.sel_page >= len(self.disk):
            self.set_status("Pages were added, deleted or moved - saving all of them so the numbering stays unbroken.")
            return self.save_all()
        return self._write([self.sel_page], f"PAGE {self.sel_page} SAVED!", f"only page {self.sel_page} was written")

    def save_all(self, _b=1):
        if not self.pages:
            return False
        idx = [i for i in range(len(self.pages)) if self.page_changed(i)]
        return self._write(idx, "ALL SAVED!", f"{len(idx)} changed page(s) written")

    def _write(self, idx, title, detail):
        if not self.folder:
            self.set_status("Nothing to save - pick the supersetup folder first.")
            return False
        try:
            folder = Path(self.folder)
            if not hasattr(self, "backup_dest"):
                self.backup_dest = BACKUP_DIR / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            for i in idx:                                   # back up just the files we are about to change
                f = folder / f"page{i}.txt"
                if i not in self.backed and f.exists():
                    self.backup_dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, self.backup_dest / f.name)
                self.backed.add(i)
            for i in idx:
                text = self.pages[i].to_text()
                with open(folder / f"page{i}.txt", "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
                while len(self.disk) <= i:
                    self.disk.append("")
                self.disk[i] = text
            n = len(self.pages)
            if len(self.disk) > n:                          # pages were deleted: remove the leftover files
                for k in range(n, len(self.disk)):
                    f = folder / f"page{k}.txt"
                    if f.exists():
                        if k not in self.backed and f.exists():
                            self.backup_dest.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(f, self.backup_dest / f.name)
                        f.unlink()
                del self.disk[n:]
            self.dirty = self.any_changed()
            self.saved_t, self.saved_title, self.saved_detail = time.time(), title, detail
            self.set_status(f"{title.title()} Originals backed up in {self.backup_dest}. Eject the drive and restart the Captain."
                            + ("  (Other pages still have unsaved changes.)" if self.dirty else ""))
            return True
        except Exception as ex:
            self.set_status(f"SAVE FAILED: {ex}")
            return False

    def request_quit(self):
        if self.dirty:
            self.popup = Info("Unsaved changes", ["You have changed pages and not saved them."],
                              [("SAVE ALL & QUIT", lambda: self.save_all() and setattr(self, "running", False)),
                               ("QUIT, DON'T SAVE", lambda: setattr(self, "running", False)), ("CANCEL", None)])
        else:
            self.running = False

    # ---- page model ------------------------------------------------------------------
    @property
    def doc(self):
        return self.pages[self.sel_page] if self.pages else None

    def page_sec(self, doc=None):
        return (doc or self.doc).sec(GSEC, create=True)

    def page_name(self, doc):
        s = doc.sec(GSEC)
        g = groups(s.get("page_name", "")) if s else []
        return g[0] if g else "----"

    def pval(self, key, default=""):
        g = groups(self.page_sec().get(key, ""))
        return g[0] if g else default

    def pset(self, key, val):
        self.page_sec().set(key, f"[{val}]")
        self.dirty = True

    def gsec(self):
        return self.pages[0].sec(GSEC) if self.pages else None

    def gval(self, key, default=""):
        s = self.gsec()
        g = groups(s.get(key, "")) if s else []
        return g[0] if g else default

    def gset(self, key, val):
        """FW5 keeps a full global block on every page; shared settings (brightness ...) are written to all pages."""
        for d in self.pages:
            d.sec(GSEC, create=True).set(key, f"[{val}]")
        self.dirty = True

    def apply_page_turn(self, _b=1):
        """key5 long press = previous page, keyE long press = next page, on every page."""
        for d in self.pages:
            for k, step in PAGE_TURN.items():
                KeyView(d, k).set_cmds("long", 1, [["preset", step]])
        self.dirty = True
        self.set_status("Every page now turns back with a long press of key 5 and forward with a long press of key E.")

    def pset_raw(self, key, raw):
        self.page_sec().set(key, raw)
        self.dirty = True

    def new_page(self, _b=1):
        if not self.pages:
            return
        opts = [("blank", "Blank page  (PC up/down on the end keys, long press turns the page)")]
        opts += [(k, f"Starter: {k}  -  patch slots, stomp toggles, bank up/down") for k in STARTERS]
        self.popup = BigPopup("New page", opts, None, self.make_page)

    def make_page(self, v):
        sp = STARTERS.get(v)
        if sp:
            opts = [(i, f"channel {i}" + ("   (suggested)" if i == sp["ch"] else "")) for i in range(1, 17)]
            self.popup = BigPopup(f"MIDI channel of the {v}", opts, sp["ch"], lambda ch, v=v: self.build_page(v, ch))
        else:
            self.build_page(v, 1)

    def page_channel(self, doc=None):
        """The MIDI channel most of this page's channel commands use."""
        d = doc or self.doc
        seen = {}
        for k in range(10):
            kv = KeyView(d, k)
            for tr, _ in TRIGGERS:
                for n in range(1, kv.times() + 1):
                    for c in kv.cmds(tr, n):
                        if is_std(c):
                            seen[c[0]] = seen.get(c[0], 0) + 1
        if seen:
            return int(max(seen, key=seen.get))
        g = groups(d.sec(GSEC).get("exp1_CH", "")) if d.sec(GSEC) else []
        return int(g[0]) if g and g[0].isdigit() else 1

    def set_page_channel(self, ch, doc=None):
        """Move every channel command of the page (keys, PC groups, EXP pedals, encoder) to MIDI channel ch."""
        d = doc or self.doc
        for k in range(10):
            kv = KeyView(d, k)
            for tr, _ in TRIGGERS:
                for n in range(1, 11):
                    cmds = kv.cmds(tr, n)
                    if cmds and any(is_std(c) for c in cmds):
                        for c in cmds:
                            if is_std(c):
                                c[0] = str(ch)
                        kv.set_cmds(tr, n, cmds)
        g = d.sec(GSEC, create=True)
        for i in range(10):
            raw = g.get(f"PC_group_{i}")
            if raw:
                cmds = parse_cmds(raw)
                for c in cmds:
                    if is_std(c):
                        c[0] = str(ch)
                g.set(f"PC_group_{i}", fmt_cmds(cmds))
        for key in ("exp1_CH", "exp2_CH", "encoderCH"):
            g.set(key, f"[{ch}]")
        self.dirty = True

    def build_gp200_page(self, ch):
        """The GP-200 starter is a complete page (colours, names, bank-select groups); only shared values, page turn and channel are applied."""
        d = parse_page(GP200_PAGE)
        d.nl = "\n"
        base = self.pages[0].sec(GSEC)
        g = d.sec(GSEC)
        for k_ in GLOBAL_SHARED:
            v = base.get(k_) if base else None
            if v:
                g.set(k_, v)
        for k in range(10):                             # a toggle's OFF look (last press) must equal its start colour
            kk = KeyView(d, k)
            if kk.times() > 1:
                kk.set_led(kk.times(), kk.led(0))
        kv = KeyView(d, 9)                              # keyE was bank UP like key 5: it is bank DOWN
        kv.set_name("short_up", 1, "BANK-")
        kv.set_cmds("short_up", 1, [["1", "PC", "bank_dec", "-"]])
        for k, step in PAGE_TURN.items():
            KeyView(d, k).set_cmds("long", 1, [["preset", step]])
            KeyView(d, k).set_name("long", 1, "INC" if step == "value_inc" else "DEC")
        if ch != 1:
            self.set_page_channel(ch, d)
        return d

    def build_page(self, v, ch):
        sp = STARTERS.get(v)
        if v == "Valeton GP-200":
            self.pages.append(self.build_gp200_page(ch))
            self.sel_page = len(self.pages) - 1
            self.press_view = {}
            self.dirty = True
            self.set_status(f"New GP-200 page added on MIDI channel {ch}.")
            return
        d = parse_page(new_page_text(sp["page"] if sp else "NEW", self.pages[0]))
        d.nl = "\n"
        self.pages.append(d)
        self.sel_page = len(self.pages) - 1
        self.press_view = {}
        if sp:
            keep = self.sel_key
            order = list(range(sp["slots"])) + [k for k in range(8) if k >= sp["slots"]]       # slots first, then toggles
            for i in range(sp["slots"]):
                self.sel_key = i
                self.apply_quick(("slot", i, ""))
                if sp["slotcol"] is not None:
                    self.kv().set_led(0, [sp["slotcol"]] * 3)
                    self.kv().set_led(1, [sp["slotcol"]] * 3)
            for j, (cc, lab, col, off, extra) in enumerate(sp["toggles"]):
                self.sel_key = order[sp["slots"] + j]
                self.apply_quick(("toggle", cc, lab))
                kv = self.kv()
                if col is not None:
                    kv.set_led(1, [col] * 3)
                if off is not None:
                    kv.set_led(0, list(off))
                    kv.set_led(2, list(off))
                if extra:
                    kv.set_cmds("short_up", 1, extra + kv.cmds("short_up", 1))
            for k, kind in ((8, "bank_dec"), (9, "bank_inc")):
                self.sel_key = k
                self.apply_quick(("bank", kind, ""))
                kv = self.kv()                                  # bank keys act on release, like the factory pages
                cm = kv.cmds("short_dw", 1)
                nm = kv.name("short_dw", 1)
                kv.set_cmds("short_dw", 1, [])
                kv.set_name("short_dw", 1, "")
                kv.set_cmds("short_up", 1, cm)
                kv.set_name("short_up", 1, nm)
            for k, step in (sp["long_map"] or PAGE_TURN).items():
                self.sel_key = k
                self.kv().set_cmds("long", 1, [["preset", step]])
            g = d.sec(GSEC)
            for k_, raw in sp["extra"].items():
                g.set(k_, raw)
            self.sel_key = keep
            if ch != 1:
                self.set_page_channel(ch, d)
        self.press_view = {}
        self.dirty = True
        self.set_status(("New " + v + f" starter page added on MIDI channel {ch}. " if sp else "New page added. ")
                        + "Top row: patch slots, bottom row: stomp toggles, D / E: bank down / up.")

    def copy_page(self, _b=1):
        if not self.doc:
            return
        d = self.doc.clone()
        self.pages.insert(self.sel_page + 1, d)
        self.sel_page += 1
        self.dirty = True

    def delete_page(self, _b=1):
        if len(self.pages) <= 1:
            self.set_status("The Captain needs at least one page.")
            return
        i = self.sel_page

        def go():
            self.pages.pop(i)
            self.sel_page = clamp(i, 0, len(self.pages) - 1)
            self.dirty = True
        self.popup = Info("Delete page?", [f"Page {i} ({self.page_name(self.pages[i])}) will be removed and the pages after it renumbered."],
                          [("DELETE", go), ("CANCEL", None)])

    def move_page(self, d):
        i, j = self.sel_page, self.sel_page + d
        if j < 0 or j >= len(self.pages):
            return
        self.pages[i], self.pages[j] = self.pages[j], self.pages[i]
        self.sel_page = j
        self.dirty = True

    # ---- immediate-mode widgets ------------------------------------------------------------
    def hit(self, rect, cb, wheel=None):
        r = pygame.Rect(rect)
        if self.clip:
            r = r.clip(self.clip)
        if r.w > 0 and r.h > 0:
            self._hits.append((r, cb, wheel))

    def btn(self, rect, label, cb, active=False, enabled=True, size=14, color=None):
        r = pygame.Rect(rect)
        surf = self.screen
        mp = pygame.mouse.get_pos()
        hover = enabled and r.collidepoint(mp) and (not self.clip or self.clip.collidepoint(mp))
        pygame.draw.rect(surf, (32, 29, 21), r.inflate(4, 4), border_radius=8)
        fill = (color or AMBER_DIM) if active else (BTN_HOT if hover else BTN)
        pygame.draw.rect(surf, fill, r, border_radius=6)
        if not active:
            pygame.draw.line(surf, (245, 242, 229), (r.x + 6, r.y + 2), (r.right - 6, r.y + 2))
        pygame.draw.rect(surf, AMBER if (hover or active) else BTN_EDGE, r, 1, border_radius=6)
        col = (255, 255, 255) if (hover or active) else (TEXT if enabled else (116, 112, 100))
        sizes = tuple(s for s in (16, 15, 14, 13, 12, 11, 10) if s <= size)
        txt(surf, label, fit_size(label, r.w - 10, sizes, bold=True), col, r.center, "center", bold=True)
        if enabled:
            self.hit(r, lambda b, cb=cb: cb(b))

    def field(self, rect, fid, text, commit, wheel=None, menu=None, align="left"):
        r = pygame.Rect(rect)
        editing = self.edit and self.edit["id"] == fid
        s = self.screen
        pygame.draw.rect(s, WELL, r, border_radius=5)
        pygame.draw.rect(s, AMBER if editing else BTN_EDGE, r, 1, border_radius=5)
        shown = (self.edit["buf"] + ("_" if int(time.time() * 2) % 2 == 0 else "")) if editing else text
        sz = fit_size(shown, r.w - 10, (14, 13, 12, 11, 10))
        if align == "center":
            txt(s, shown, sz, TEXT, r.center, "center", bold=True)
        else:
            txt(s, shown, sz, TEXT, (r.x + 6, r.centery), "midleft", bold=True)

        def click(b, fid=fid, text=text, commit=commit, menu=menu):
            if b == 3 and menu:
                menu()
            elif b == 1:
                self.edit = {"id": fid, "buf": text, "commit": commit}
        self.hit(r, click, wheel)

    def choice(self, rect, text, options, current, pick, title, size=13):
        r = pygame.Rect(rect)
        s = self.screen
        hover = r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(s, BTN_HOT if hover else BTN, r, border_radius=5)
        pygame.draw.rect(s, AMBER if hover else BTN_EDGE, r, 1, border_radius=5)
        txt(s, text, fit_size(text, r.w - 24, tuple(x for x in (14, 13, 12, 11, 10) if x <= size), bold=True), TEXT,
            (r.x + 7, r.centery), "midleft", bold=True)
        pygame.draw.polygon(s, DIM, [(r.right - 14, r.centery - 3), (r.right - 6, r.centery - 3), (r.right - 10, r.centery + 3)])
        self.hit(r, lambda b: setattr(self, "popup", BigPopup(title, options, current, pick)))

    def commit_edit(self):
        if not self.edit:
            return
        e, self.edit = self.edit, None
        try:
            err = e["commit"](e["buf"].strip())
        except Exception as ex:
            err = str(ex)
        if err:
            self.set_status(err)

    # ---- editing helpers -------------------------------------------------------------------------
    def kv(self, n=None):
        return KeyView(self.doc, self.sel_key if n is None else n)

    def press_of(self, n):
        return clamp(self.press_view.get(n, 1), 1, KeyView(self.doc, n).times())

    def edit_cmds(self, trig, mutate):
        kv, p = self.kv(), self.press_of(self.sel_key)
        cmds = kv.cmds(trig, p)
        mutate(cmds)
        kv.set_cmds(trig, p, cmds)
        self.dirty = True

    def change_type(self, trig, i, ty):
        def m(c):
            new = list(TYPE_DEFAULTS[ty])
            if is_std(c) and new[0].isdigit():
                new[0] = c[0]
            c[i] = new
        self.edit_cmds(trig, m)

    def set_field(self, trig, i, j, val):
        """Validate + store field j of a channel command (PC / CC / CCT / NT). Returns an error string or None."""
        kv, p = self.kv(), self.press_of(self.sel_key)
        cmds = kv.cmds(trig, p)
        c = cmds[i]
        if not is_std(c):
            return "Edit this command in the text box."
        ty = ctype(c)
        v = val.strip()
        if v == "":
            return "Empty value ignored."
        if "[" in v or "]" in v or " " in v:
            return "No spaces or brackets, please."
        if j == 0:
            if not v.isdigit() or not 1 <= int(v) <= 16:
                return "MIDI channel must be 1 - 16."
        elif j == 2:
            if ty in ("CC", "CCT", "NT") or (ty == "PC" and v.isdigit()):
                if not v.isdigit() or not 0 <= int(v) <= 127:
                    return "Value must be 0 - 127."
            elif ty == "PC" and v not in PC_WORDS:
                return "PC value: 0-127, or one of " + " ".join(PC_WORDS)
            if ty == "PC":
                c[3] = "-"
        elif j == 3:
            if ty in ("CC", "CCT", "NT"):
                if v.isdigit():
                    if not 0 <= int(v) <= 127:
                        return "Value must be 0 - 127."
                elif not (ty == "CC" and v in CC_WORDS):
                    return "Value 0 - 127" + (", or " + " ".join(CC_WORDS) if ty == "CC" else ".")
            elif ty == "PC":
                v = "-"
        c[j] = v
        kv.set_cmds(trig, p, cmds)
        self.dirty = True
        return None

    def set_raw(self, trig, i, text):
        """Other command kinds (preset, HID, SysEx, time engine ...) are typed as words: preset value_inc"""
        words = [w for w in re.split(r"[\s\[\]]+", text.strip()) if w]
        if not words:
            return "Empty value ignored."
        if words[0].lower() in ("hid", "sysex") and words[-1].upper() != "END":
            words.append("END")
        kv, p = self.kv(), self.press_of(self.sel_key)
        cmds = kv.cmds(trig, p)
        cmds[i] = words
        kv.set_cmds(trig, p, cmds)
        self.dirty = True
        return None

    def bump(self, trig, i, j, dy):
        kv, p = self.kv(), self.press_of(self.sel_key)
        c = kv.cmds(trig, p)[i]
        if is_std(c) and c[j].isdigit():
            hi = 16 if j == 0 else 127
            lo = 1 if j == 0 else 0
            self.set_field(trig, i, j, str(clamp(int(c[j]) + dy, lo, hi)))

    def pick_color(self, seg, n=None):
        kv = self.kv()
        p = self.press_of(self.sel_key) if n is None else n
        cols = kv.led(p)

        def done(v):
            if seg == 3:
                cols[:] = [v] * 3
            else:
                cols[seg] = v
            kv.set_led(p, cols)
            self.dirty = True
        self.popup = ColorPopup("LED colour - " + ("start colour" if p == 0 else f"press {p}") + " - ring segment " + ("ALL" if seg == 3 else str(seg + 1)),
                                cols[0] if seg == 3 else cols[seg], done)

    # ---- quick set (wizards) ------------------------------------------------------------------------
    def quick_set(self, _b=1):
        opts = []
        for n, name, kind in self.profile["items"]:
            if kind == "toggle":
                opts.append((("toggle", n, name), f"Toggle  {name}  (CC{n})"))
        for n, name, kind in self.profile["items"]:
            if kind in ("trigger", "step"):
                opts.append((("trigger", n, name), f"Press  {name}  (CC{n})"))
        opts += [(("slot", i, ""), f"Patch slot {i + 1} ({'ABCDEFGH'[i]}) of the group  (PC auto{i})") for i in range(self.profile.get("slots", 4))]
        opts += [(("bank", "bank_inc", ""), "Bank / group UP"), (("bank", "bank_dec", ""), "Bank / group DOWN"),
                 (("pcstep", "value_inc", ""), "PC UP (short press), next page (long press)"),
                 (("pcstep", "value_dec", ""), "PC DOWN (short press), previous page (long press)"),
                 (("page", "value_inc", ""), "Next page (short press)"), (("page", "value_dec", ""), "Previous page (short press)")]
        if self.profile is GP200:
            opts.append((("direct", 0, ""), "Direct GP-200 patch (type 12B)..."))
        opts.append((("clear", 0, ""), "Clear this key"))
        self.popup = BigPopup("Quick set for key " + KEY_NAMES_ASCII[self.sel_key] + "  -  " + self.profile["name"],
                              opts, None, self.apply_quick)

    def apply_quick(self, v):
        kind, n, name = v
        kv = self.kv()
        short = re.sub(r"[^A-Za-z0-9]", "", name)[:6]
        if kind == "clear":
            kv.clear()
        elif kind == "toggle":
            col = TOGGLE_COLOURS[n % len(TOGGLE_COLOURS)]
            off = [0, 0, dim_hex(col, 0.18)]
            kv.sec.items = []
            kv.sec.set("keytimes", "[2]")
            kv.sec.set("ledmode", "[normal]")
            kv.set_led(0, off)                                  # key starts looking OFF; press 1 sends ON
            kv.set_led(1, [col] * 3)
            kv.set_name("short_up", 1, short)
            kv.set_cmds("short_up", 1, [["1", "CC", str(n), "127"]])
            kv.set_led(2, off)
            kv.set_name("short_up", 2, short)
            kv.set_cmds("short_up", 2, [["1", "CC", str(n), "0"]])
            self.press_view[self.sel_key] = 1
        elif kind == "trigger":
            kv.sec.items = []
            kv.sec.set("keytimes", "[1]")
            kv.sec.set("ledmode", "[tap]" if "TAP" in name.upper() else "[normal]")
            kv.set_led(0, [0x04C3FF] * 3)
            kv.set_led(1, [0x04C3FF] * 3)
            kv.set_name("short_dw", 1, short)
            kv.set_cmds("short_dw", 1, [["1", "CC", str(n), "127"]])
        elif kind == "slot":
            col = [0x00FFFF, 0xAA0000, 0x7B0050, 0xFF8200, 0x60C000, 0xFFD000, 0xFF3080, 0x04C3FF][n]
            kv.sec.items = []
            kv.sec.set("keytimes", "[1]")
            kv.sec.set("ledmode", "[select]")
            kv.set_led(0, [col] * 3)
            kv.set_led(1, [col] * 3)
            kv.set_name("short_dw", 1, "SLOT_" + "ABCDEFGH"[n])
            kv.set_cmds("short_dw", 1, [["1", "PC", f"auto{n}", "-"]])
        elif kind == "bank":
            kv.sec.items = []
            kv.sec.set("keytimes", "[1]")
            kv.sec.set("ledmode", "[normal]")
            kv.set_led(0, [0xFFFFFF] * 3)
            kv.set_led(1, [0xFFFFFF] * 3)
            kv.set_name("short_dw", 1, "BANK+" if n == "bank_inc" else "BANK-")
            kv.set_cmds("short_dw", 1, [["1", "PC", n, "-"]])
        elif kind in ("pcstep", "page"):
            kv.sec.items = []
            kv.sec.set("keytimes", "[1]")
            kv.sec.set("ledmode", "[normal]")
            kv.set_led(0, [0xFFFFFF] * 3)
            kv.set_led(1, [0xFFFFFF] * 3)
            sign = "+" if n == "value_inc" else "-"
            if kind == "pcstep":
                kv.set_name("short_up", 1, "PC_" + sign)
                kv.set_cmds("short_up", 1, [["1", "PC", n, "-"]])
                kv.set_cmds("long", 1, [["preset", n]])
            else:
                kv.set_name("short_up", 1, "PAGE" + sign)
                kv.set_cmds("short_up", 1, [["preset", n]])
        elif kind == "direct":
            self.popup = TextPrompt("Direct GP-200 patch", "Patch as shown on the GP-200: a number 1-256, or bank+letter like 12B:",
                                    "1", self.apply_direct)
            return
        tidy_key_section(kv.sec)
        self.dirty = True

    def apply_direct(self, s):
        m = re.fullmatch(r"(\d+)\s*([A-Da-d])?", s.strip())
        if not m:
            self.set_status("Type a patch like 12B (bank 12, slot B) or just a number 1-256.")
            return
        if m.group(2):
            bank, slot = int(m.group(1)), "ABCD".index(m.group(2).upper())
            idx = (bank - 1) * 4 + slot
        else:
            idx = int(m.group(1)) - 1
        if not 0 <= idx <= 255:
            self.set_status("The GP-200 has patches 1-256 (banks 01A - 64D).")
            return
        kv = self.kv()
        kv.sec.items = []
        kv.sec.set("keytimes", "[1]")
        kv.sec.set("ledmode", "[select]")
        kv.set_led(0, [0x00FF90] * 3)
        kv.set_led(1, [0x00FF90] * 3)
        kv.set_name("short_dw", 1, f"{idx // 4 + 1:02d}{'ABCD'[idx % 4]}")
        kv.set_cmds("short_dw", 1, [["1", "CC", "0", "0" if idx < 128 else "1"], ["1", "PC", str(idx % 128), "-"]])
        self.dirty = True
        self.set_status(f"Key set to patch {idx // 4 + 1:02d}{'ABCD'[idx % 4]} (bank select CC0 + program {idx % 128}).")

    # ---- drawing -----------------------------------------------------------------------------------------
    def draw(self):
        s = self.screen
        s.blit(self.bg, (0, 0))
        self._hits = []
        self.clip = None
        self.draw_header(s)
        if not self.show_factory:
            self.draw_pages(s)
        if self.show_factory:
            self.draw_factory(s)
        elif self.pages:
            self.draw_pedal(s)
            if self.show_global:
                self.draw_global(s)
            else:
                self.draw_page_settings(s)
            self.draw_key_panel(s)
            if self.show_chords:
                self.draw_chords(s)
        else:
            panel(s, (290, 90, 1080, 700), "No pages loaded")
            for i, ln in enumerate(wrap("Hold key 1 (top-left) on the MIDI Captain while plugging in the USB cable. A drive called "
                                        "MIDICAPTAIN appears. Then click FOLDER (top) and pick the folder called supersetup on that "
                                        "drive. Your pages appear here as a picture of the pedal.", 16, 900)):
                txt(s, ln, 16, TEXT, (330, 150 + i * 26))
        panel(s, (30, 800, 1340, 50), "")
        age = time.time() - self.status_t
        for i, ln in enumerate(wrap(self.status, 13, 1020)[:2]):
            txt(s, ln, 13, TEXT if age < 40 else DIM, (46, 808 + i * 17))
        txt(s, "Pages are turned by [preset] commands", 11, DIM, (1354, 810), "topright")
        txt(s, "(PC key long press = next / previous page).", 11, DIM, (1354, 826), "topright")
        self.hits = self._hits
        if time.time() - getattr(self, "saved_t", 0) < 3.5:
            r = pygame.Rect(0, 0, 560, 150)
            r.center = (W // 2, H // 2)
            pygame.draw.rect(s, (31, 27, 17), r, border_radius=16)
            pygame.draw.rect(s, GREEN, r, 4, border_radius=16)
            txt(s, getattr(self, "saved_title", "SAVED!"), 52 if len(getattr(self, "saved_title", "")) > 9 else 64, GREEN, (r.centerx, r.y + 14), "midtop", bold=True)
            txt(s, getattr(self, "saved_detail", ""), 15, TEXT, (r.centerx, r.bottom - 52), "midtop")
            txt(s, "Eject the drive, then restart the Captain.", 14, DIM, (r.centerx, r.bottom - 30), "midtop")
        if self.popup:
            self.popup.draw(s, self)

    def draw_header(self, s):
        txt(s, "CAPTAIN CHORDSTOMP", 25, ORANGE, (30, 14), bold=True)
        txt(s, "N O T E   &   C H O R D   P A D S   -   F W 5", 10, AMBER, (32, 52))
        fold = str(self.folder) if self.folder else "(click to choose the supersetup folder)"
        if len(fold) > 52:
            fold = "..." + fold[-49:]
        self.btn((310, 8, 480, 30), f"FOLDER: {fold}", self.pick_folder, size=13)
        out = (self.settings.get("out_name") or "none").replace("#0", "")
        self.btn((310, 42, 480, 30), f"MIDI OUT for TEST: {out}"[:60], self.choose_out, size=13)
        opts = [(p["name"], p["name"]) for p in PROFILES]

        def pick(name):
            self.profile = next(p for p in PROFILES if p["name"] == name)
            self.save_settings()
        self.choice((810, 8, 210, 30), "DEVICE: " + self.profile["name"], opts, self.profile["name"], pick, "Device profile (names the CC numbers)")
        self.btn((810, 42, 100, 30), "RELOAD", self.reload, enabled=bool(self.folder), size=13)
        self.btn((920, 42, 100, 30), "HELP", self.show_help, size=13)
        pc = bool(self.pages) and self.sel_page < len(self.pages) and self.page_changed(self.sel_page)
        self.btn((1040, 8, 160, 64), f"SAVE PAGE {self.sel_page}" + ("  *" if pc else ""), self.save, active=pc,
                 enabled=bool(self.pages), size=15, color=(150, 90, 10))
        self.btn((1210, 8, 160, 64), "SAVE ALL" + ("  *" if self.dirty else ""), self.save_all, active=self.dirty,
                 enabled=bool(self.pages), size=15, color=(150, 90, 10))

    def show_help(self, _b=1):
        self.popup = Info("How the editor works", [
            "# Getting the files",
            "Hold key 1 on the Captain, plug in USB: a drive MIDICAPTAIN appears. Pick its supersetup folder with FOLDER. "
            "When you have saved, eject the drive in Windows and restart the Captain.",
            "# Notes and chords",
            "Press NOTES / CHORDS (top of the key panel). Click piano keys for single notes or build a chord from the chart (root + type, with inversion and octave), then PROGRAM KEY writes the notes onto the selected key. RELEASE = NOTE OFF also writes the same notes with velocity 0 on PRESS UP so they stop when you let go. PLAY sends the notes to your MIDI OUT to hear them first.",
            "# Editing",
            "Pick a page on the left, click a key on the picture, then change what it does on the right. "
            "QUICK SET builds a whole key for you from the DEVICE list (toggle a GP-200 block, patch slot, bank up/down).",
            "Fields: click and type, or use the mouse wheel on a number. Right-click a value field for a list of names.",
            "# Presses",
            "PRESSES is how many times a key cycles (1 = plain, 2 = toggle on/off ... up to 10). Each press has its own LED colour and commands. "
            "START is the colour the key shows before its first press (for a toggle: the OFF look).",
            "# Names",
            "The small box next to each trigger names that action; the name is shown on the Captain's screen. + PC adds a program/bank command, "
            "+ CMD a CC. Other command kinds (preset = page turn, HID keyboard, time engine ...) are changed with the type box and typed as words, "
            "e.g. preset value_inc - right-click the text for ready-made ones.",
            "# Pages and old files",
            "Firmware 5 stores every setting on every page. Old FW4 pages are converted when you open them. The GLOBAL SETTINGS button changes the "
            "shared values (brightness, long press) on all pages at once. To let an app control the key LEDs, see Page settings > PAGE: external control.",
            "# Restore pages",
            "RESTORE PAGE(S) (bottom of the page list) opens its own screen. For each page choose LAST BACKUP (the copy saved before the "
            "last overwrite) or FACTORY, or restore all factory pages (you must type RESTORE). It only changes the editor until you SAVE.",
            "# Safety",
            "Before the first SAVE your original pageN.txt files are copied to captain_backups. Comments and unknown lines in "
            "your files are kept exactly as they were.",
        ], [("OK", None)])

    def draw_pages(self, s):
        panel(s, (30, 90, 250, 700), "Pages")
        rows = 14
        n = len(self.pages)
        self.page_scroll = clamp(self.page_scroll, 0, max(0, n - rows))
        if self.sel_page < self.page_scroll:
            self.page_scroll = self.sel_page
        if self.sel_page >= self.page_scroll + rows:
            self.page_scroll = self.sel_page - rows + 1
        for r in range(rows):
            i = r + self.page_scroll
            if i >= n:
                break
            rect = pygame.Rect(42, 128 + r * 34, 226, 31)
            sel = i == self.sel_page and not self.show_global
            hover = rect.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, AMBER_DIM if sel else (BTN_HOT if hover else BTN), rect, border_radius=5)
            pygame.draw.rect(s, AMBER if sel else BTN_EDGE, rect, 1, border_radius=5)
            txt(s, f"{i:>2}", 15, (255, 255, 255) if sel else DIM, (rect.x + 10, rect.centery), "midleft", bold=True)
            txt(s, self.page_name(self.pages[i]), 16, (255, 255, 255) if sel else TEXT, (rect.x + 48, rect.centery), "midleft", bold=True)
            if self.page_changed(i):
                txt(s, "*", 18, ORANGE, (rect.right - 14, rect.centery), "center", bold=True)
            self.hit(rect, lambda b, i=i: self.select_page(i), lambda dy: setattr(self, "page_scroll", self.page_scroll - dy))
        if n > rows:
            txt(s, f"{self.page_scroll + 1}-{min(n, self.page_scroll + rows)} of {n}  (wheel)", 11, DIM, (155, 606), "center")
        ok = bool(self.pages)
        self.btn((42, 624, 70, 32), "NEW", self.new_page, enabled=ok, size=13)
        self.btn((118, 624, 70, 32), "COPY", self.copy_page, enabled=ok, size=13)
        self.btn((194, 624, 74, 32), "DELETE", self.delete_page, enabled=ok and len(self.pages) > 1, size=13)
        self.btn((42, 662, 108, 32), "MOVE UP", lambda b: self.move_page(-1), enabled=ok and self.sel_page > 0, size=13)
        self.btn((160, 662, 108, 32), "MOVE DOWN", lambda b: self.move_page(1), enabled=ok and self.sel_page < len(self.pages) - 1, size=13)
        self.btn((42, 702, 226, 34), "GLOBAL SETTINGS", lambda b: setattr(self, "show_global", not self.show_global),
                 active=self.show_global, enabled=ok, size=13)
        self.btn((42, 744, 226, 32), "RESTORE PAGE(S)...", self.open_restore, enabled=ok, size=13)

    # ---- factory pages (its own screen, nothing happens by accident) ---------------------------------------------------------
    def factory_state(self, i):
        if i >= len(self.pages):
            return "not on the card"
        return "same as factory" if _norm(self.pages[i].to_text(), i) == _norm(FACTORY[i], i) else "changed"

    def restore_page(self, i):
        d = parse_page(FACTORY[i])
        if i < len(self.pages):
            self.pages[i] = d
        elif i == len(self.pages):
            self.pages.append(d)
        else:
            return
        self.press_view = {}
        self.dirty = True
        self.set_status(f"Page {i} is back to factory ({factory_name(i)}) in the editor. Nothing is written until you press SAVE; "
                        "your old file is backed up when you do.")

    def open_restore(self, _b=1):
        self.bk = self.find_backups()
        self.show_factory = True

    def find_backups(self):
        """Newest saved copy of each page, from the captain_backups folders: {page: (path, 'date time')}."""
        out = {}
        try:
            dirs = sorted([d for d in Path(BACKUP_DIR).iterdir() if d.is_dir()], reverse=True)
        except Exception:
            dirs = []
        for d in dirs:
            for f in d.glob("page*.txt"):
                m = re.fullmatch(r"page(\d+)\.txt", f.name)
                if m and int(m.group(1)) not in out:
                    n = d.name
                    stamp = f"{n[6:8]}/{n[4:6]}/{n[0:4]} {n[9:11]}:{n[11:13]}" if re.fullmatch(r"\d{8}_\d{6}", n) else n
                    out[int(m.group(1))] = (f, stamp)
        return out

    def backup_differs(self, i):
        if i not in self.bk:
            return False
        try:
            t = self.bk[i][0].read_text(encoding="utf-8", errors="replace")
            return i >= len(self.pages) or parse_page(t).to_text() != self.pages[i].to_text()
        except Exception:
            return False

    def restore_from_backup(self, i):
        d = parse_page(self.bk[i][0].read_text(encoding="utf-8", errors="replace"))
        if i < len(self.pages):
            self.pages[i] = d
        elif i == len(self.pages):
            self.pages.append(d)
        self.press_view, self.dirty = {}, True
        self.set_status(f"Page {i} is back to your backup from {self.bk[i][1]} in the editor. Press SAVE to write it to the Captain.")

    def ask_restore_page(self, i, src="factory"):
        if i > len(self.pages):
            self.set_status(f"Restore page {len(self.pages)} first - pages must run without gaps.")
            return
        cur = self.page_name(self.pages[i]) if i < len(self.pages) else "not on the card"
        if src == "backup":
            self.popup = Info(f"Restore page {i} from your last backup?", [
                f"Page {i} ({cur}) will be replaced by the copy saved on {self.bk[i][1]}. Your current version of it is lost in the editor.",
                "Nothing is written to the Captain until you press SAVE."],
                [("RESTORE FROM BACKUP", lambda: self.restore_from_backup(i)), ("CANCEL", None)])
            return
        extra = ""
        self.popup = Info(f"Restore page {i} to factory?", [
            f"Page {i} ({cur}) will be replaced by the factory page {factory_name(i)}." + extra,
            "Your own changes to this page are lost in the editor. Nothing is written to the Captain until you press SAVE, and the old file is backed up first."],
            [("RESTORE FROM FACTORY", lambda: self.restore_page(i)), ("CANCEL", None)])

    def ask_restore_all_backup(self):
        idx = [i for i in sorted(self.bk) if i <= len(self.pages) and self.backup_differs(i)]
        if not idx:
            self.set_status("No backups to restore - nothing differs from your latest backups.")
            return

        def go():
            for i in idx:
                self.restore_from_backup(i)
            self.set_status(f"{len(idx)} page(s) are back to their last backup in the editor. Press SAVE ALL to write them.")
        self.popup = Info("Restore all pages from backup?", [
            f"{len(idx)} page(s) have a backup that differs from what you have now. Each is replaced by its newest backup copy.",
            "Nothing is written to the Captain until you press SAVE ALL."], [("RESTORE FROM BACKUPS", go), ("CANCEL", None)])

    def ask_restore_all(self):
        def done(t):
            if t.strip().upper() != "RESTORE":
                self.set_status("Nothing restored - the word RESTORE was not typed.")
                return
            self.pages = [parse_page(f) for f in FACTORY]
            self.sel_page, self.press_view, self.dirty = 0, {}, True
            self.set_status("All 20 pages are back to factory in the editor. Press SAVE ALL to write them to the Captain "
                            "(your current files are backed up first).")
        self.popup = TextPrompt("Restore ALL factory pages", "This replaces every page with the factory set. Type RESTORE and press Enter:", "", done)

    def draw_factory(self, s):
        panel(s, (30, 90, 1340, 700), "Restore pages - from your last backup or from factory")
        for j, ln in enumerate(wrap("Nothing here changes your Captain by itself. A restore only changes the pages in this editor; it reaches the "
                                    "Captain when you press SAVE PAGE / SAVE ALL, and your old files are backed up first. LAST BACKUP is the copy "
                                    "this editor saved just before it last overwrote that page.", 13, 1270)[:2]):
            txt(s, ln, 13, TEXT, (52, 126 + j * 17))
        for i in range(20):
            col, row = divmod(i, 10)
            x, y = 52 + col * 655, 168 + row * 46
            r = pygame.Rect(x, y, 630, 40)
            st = self.factory_state(i)
            pygame.draw.rect(s, BTN, r, border_radius=6)
            pygame.draw.rect(s, BTN_EDGE, r, 1, border_radius=6)
            txt(s, f"{i:>2}", 15, DIM, (x + 12, r.centery), "midleft", bold=True)
            txt(s, factory_name(i), 17, TEXT, (x + 46, r.centery), "midleft", bold=True)
            now = self.page_name(self.pages[i]) if i < len(self.pages) else "-"
            txt(s, f"now: {now}", 12, DIM, (x + 112, y + 6))
            txt(s, f"backup: {self.bk[i][1]}" if i in self.bk else "no backup yet", 10, DIM, (x + 112, y + 22))
            txt(s, st, 13, GREEN if st == "same as factory" else (ORANGE if st == "changed" else DIM), (x + 262, r.centery), "midleft", bold=True)
            self.btn((x + 395, y + 4, 112, 32), "LAST BACKUP", lambda b, i=i: self.ask_restore_page(i, "backup"),
                     enabled=self.backup_differs(i) and i <= len(self.pages), size=12)
            self.btn((x + 512, y + 4, 112, 32), "FACTORY", lambda b, i=i: self.ask_restore_page(i, "factory"),
                     enabled=st != "same as factory" and i <= len(self.pages), size=12)
        self.btn((52, 650, 300, 44), "BACK TO THE EDITOR", lambda b: setattr(self, "show_factory", False), size=15)
        self.btn((370, 650, 330, 44), "RESTORE ALL FROM LAST BACKUPS...", lambda b: self.ask_restore_all_backup(), size=14)
        self.btn((720, 650, 590, 44), "RESTORE ALL 20 PAGES TO FACTORY...", lambda b: self.ask_restore_all(), size=15, color=(140, 30, 20))
        txt(s, "Restoring all to factory asks you to type the word RESTORE first.", 12, DIM, (720, 702))

    def select_page(self, i):
        self.sel_page = i
        self.show_factory = False
        self.show_global = False
        self.press_view = {}
        self.kscroll = 0

    # ---- pedal picture -----------------------------------------------------------------------------------------
    PAD_X = [370, 510, 650, 790, 930]
    PAD_Y = [180, 412]

    def pad_pos(self, k):
        return self.PAD_X[k % 5], self.PAD_Y[k // 5]

    def summary(self, kv):
        p = self.press_of(kv.n)
        first = None
        for tr, _ in TRIGGERS[:2]:
            c = kv.cmds(tr, p)
            if c:
                first = kv.name(tr, p) or " ".join(cmd_short(x) for x in c[:2])
                break
        lng = kv.cmds("long", p)
        l2 = ("L: " + (kv.name("long", p) or " ".join(cmd_short(x) for x in lng[:1]))) if lng else ""
        return first or "-", l2

    def draw_pedal(self, s):
        d = self.doc
        panel(s, (290, 90, 720, 450), f"Page {self.sel_page}  -  {self.page_name(d)}")
        for k in range(10):
            kv = KeyView(d, k)
            cx, cy = self.pad_pos(k)
            p = self.press_of(k)
            cols = kv.led(p)
            sel = k == self.sel_key
            pygame.draw.circle(s, (9, 8, 6), (cx, cy), 49)
            for seg in range(3):
                mid = (SEG_ANGLES_TOP if k < 5 else SEG_ANGLES_BOT)[seg]       # where this ring segment sits on the real pedal
                pts = arc_pts(cx, cy, 43, mid - 52, mid + 52, 20)
                pygame.draw.lines(s, rgb(cols[seg]), False, pts, 8)
            pygame.draw.circle(s, (78, 76, 70), (cx, cy), 33)
            pygame.draw.circle(s, (128, 125, 118), (cx, cy), 31)
            pygame.draw.circle(s, (170, 167, 160), (cx - 3, cy - 4), 22)
            txt(s, KEY_NAMES[k] if KEY_NAMES[k] != "Λ" else "^", 26, (30, 28, 24), (cx, cy), "center", bold=True)
            if sel:
                pygame.draw.circle(s, (255, 255, 255), (cx, cy), 53, 2)
            t = kv.times()
            if t > 1:
                pygame.draw.circle(s, (28, 26, 20), (cx + 38, cy - 38), 12)
                pygame.draw.circle(s, AMBER, (cx + 38, cy - 38), 12, 1)
                txt(s, f"{p}/{t}", 10, AMBER, (cx + 38, cy - 38), "center", bold=True)
            l1, l2 = self.summary(kv)
            txt(s, l1, fit_size(l1, 132, (13, 12, 11, 10), bold=True), TEXT if sel else DIM, (cx, cy + 62), "center", bold=True)
            if l2:
                txt(s, l2, fit_size(l2, 132, (11, 10), bold=False), DIM, (cx, cy + 78), "center")
            self.hit((cx - 56, cy - 56, 112, 112), lambda b, k=k: self.select_key(k))
        for k in range(10):                       # key labels last, so no ring paints over them
            cx, cy = self.pad_pos(k)
            txt(s, KEYSEC[k], 10, TEXT, (cx - 51, cy - 25), "midright")
        # screen + encoder + expression pedals
        scr = pygame.Rect(455, 268, 130, 88)
        pygame.draw.rect(s, (9, 8, 6), scr.inflate(8, 8), border_radius=8)
        pygame.draw.rect(s, (23, 20, 13), scr, border_radius=6)
        txt(s, self.page_name(d), 30, AMBER, (scr.centerx, scr.y + 28), "center", bold=True)
        txt(s, "PC " + self.pval("pc_open_num", "0"), 14, DIM, (scr.centerx, scr.y + 56), "center")
        pygame.draw.circle(s, (9, 8, 6), (780, 306), 38)
        pygame.draw.circle(s, (98, 96, 90), (780, 306), 32)
        pygame.draw.circle(s, (160, 157, 150), (780, 306), 26)
        pygame.draw.line(s, (36, 34, 30), (780, 306), (780, 284), 3)
        txt(s, f"ENCODER  ch{self.pval('encoderCH', '-')}  CC{self.pval('encoderCCnum', '-')}", 11, DIM, (780, 352), "center")
        txt(s, f"EXP 1   ch{self.pval('exp1_CH', '-')}  CC{self.pval('exp1_CC', '-')}", 12, DIM, (312, 296))
        txt(s, f"EXP 2   ch{self.pval('exp2_CH', '-')}  CC{self.pval('exp2_CC', '-')}", 12, DIM, (312, 316))

    def select_key(self, k):
        self.sel_key = k
        self.kscroll = 0
        self.commit_edit()

    # ---- page settings ---------------------------------------------------------------------------------------------
    def numfield(self, rect, fid, getter, setter, lo, hi):
        def commit(v):
            if not v.isdigit() or not lo <= int(v) <= hi:
                return f"Enter a number {lo} - {hi}."
            setter(str(int(v)))

        def wheel(dy):
            try:
                setter(str(clamp(int(getter()) + dy, lo, hi)))
            except ValueError:
                pass
        self.field(rect, fid, getter(), commit, wheel, align="center")

    def tog(self, rect, label, key, default="off", size=13):
        on = self.pval(key, default) == "on"
        self.btn(rect, f"{label}:  " + ("ON" if on else "OFF"), lambda b: self.pset(key, "off" if on else "on"), active=on, size=size)

    def rawfield(self, rect, fid, key):
        """Edits a whole command-like value (e.g. PC_group_0 = [1][CC][0][1]) as words."""
        txt_ = " ".join(groups(self.page_sec().get(key, "")))

        def commit(v):
            words = [w for w in re.split(r"[\s\[\]]+", v.strip()) if w]
            self.pset_raw(key, "".join(f"[{w}]" for w in words))
        self.field(rect, fid, txt_, commit)

    def draw_page_settings(self, s):
        panel(s, (290, 550, 720, 240), f"Page {self.sel_page} settings")
        tabs = ["PAGE", "PEDALS", "PC BANK", "LED / TAP"]
        cur = getattr(self, "set_tab", 0)
        for i, nm in enumerate(tabs):
            self.btn((520 + i * 120, 553, 114, 26), nm, lambda b, i=i: setattr(self, "set_tab", i), active=(i == cur), size=12)
        L, R = 310, 665
        y = 592
        row = 36
        if cur == 0:
            txt(s, "PAGE NAME", 13, DIM, (L, y + 8))

            def setname(v):
                if not v or len(v) > 6 or not re.fullmatch(r"[A-Za-z0-9_\-]+", v):
                    return "Page name: 1 - 6 letters / digits."
                self.pset("page_name", v)
            self.field((L + 120, y, 90, 30), "pname", self.page_name(self.doc), setname, align="center")
            txt(s, "GROUP SIZE", 13, DIM, (L, y + row + 8))
            gs = self.pval("group_number", "4")
            self.choice((L + 120, y + row, 110, 30), gs + " patches", [(str(g), f"{g} patches") for g in range(2, 11)], gs,
                        lambda v: self.pset("group_number", v), "Patches per group (used by PC auto keys)")
            modes = [("123", "123  plain numbers"), ("abc3", "1A 1B 1C  2A ...  (3 per bank)"), ("abc4", "1A-1D  2A ...  (4 per bank)"),
                     ("abc5", "1A-1E  2A ...  (5 per bank)"), ("abc8", "1A-1H  2A ...  (8 per bank)")]
            cm = self.pval("display_number_ABC", "abc4")
            txt(s, "PC DISPLAY", 13, DIM, (L, y + 2 * row + 8))
            self.choice((L + 120, y + 2 * row, 230, 30), dict(modes).get(cm, cm), modes, cm, lambda v: self.pset("display_number_ABC", v), "How patch numbers are shown")
            txt(s, "FIRST BANK", 13, DIM, (L, y + 3 * row + 8))
            bo = self.pval("display_bank_offset", "1")
            self.choice((L + 120, y + 3 * row, 110, 30), bo, [("0", "0  (0A 0B ...)"), ("1", "1  (1A 1B ...)")], bo,
                        lambda v: self.pset("display_bank_offset", v), "First bank number")
            txt(s, "PC AT START", 13, DIM, (L, y + 4 * row + 8))
            self.numfield((L + 120, y + 4 * row, 70, 30), "pcopen", lambda: self.pval("pc_open_num", "0"), lambda v: self.pset("pc_open_num", v), 0, 127)
            txt(s, "PAGE CHANNEL", 13, DIM, (R, y + 4 * row + 8))
            pc_ = self.page_channel()
            self.choice((R + 130, y + 4 * row, 150, 30), f"channel {pc_}", [(i, f"channel {i}") for i in range(1, 17)], pc_,
                        lambda v: self.set_page_channel(v), "MIDI channel for every command on this page")
            self.tog((R, y, 330, 30), "MIDI THROUGH (IN -> OUT)", "midithrough", "on")
            self.tog((R, y + row, 230, 30), "EXTERNAL CONTROL", "externalmidicontrol", "on")
            txt(s, "ON CH", 12, DIM, (R + 244, y + row + 8))
            self.numfield((R + 290, y + row, 40, 30), "extch", lambda: self.pval("externalmidicontrolch", "16"), lambda v: self.pset("externalmidicontrolch", v), 1, 16)
            for i, ln in enumerate(wrap("External control lets another device (such as the GP-200 Sync app) press keys, jump pages (CC20) and "
                                        "set key states (CC21-30) over MIDI IN. Use a channel your other gear ignores (default 16).", 11, 330)):
                txt(s, ln, 11, DIM, (R, y + 2 * row + 4 + i * 15))
        elif cur == 1:
            for n, lab in ((1, "EXP 1"), (2, "EXP 2")):
                yy = y + (n - 1) * row
                txt(s, lab, 13, DIM, (L, yy + 8))
                for k, (key, cap, w, lo, hi, dx) in enumerate((("CH", "ch", 46, 1, 16, 60), ("CC", "CC", 52, 0, 127, 148), ("min", "min", 52, 0, 127, 252), ("max", "max", 52, 0, 127, 364))):
                    kk = f"exp{n}_{key}"
                    txt(s, cap, 12, DIM, (L + dx, yy + 8))
                    self.numfield((L + dx + 30, yy, w, 30), f"e{n}{key}", lambda kk=kk: self.pval(kk, "0"), lambda v, kk=kk: self.pset(kk, v), lo, hi)
            yy = y + 2 * row
            txt(s, "ENCODER", 13, DIM, (L, yy + 8))
            for key, cap, w, lo, hi, dx in (("encoderCH", "ch", 46, 1, 16, 90), ("encoderCCnum", "turn CC", 52, 0, 127, 180), ("encoderkeyCCtoggle", "push CC", 52, 0, 127, 330)):
                txt(s, cap, 12, DIM, (L + dx, yy + 8))
                self.numfield((L + dx + (30 if cap == "ch" else 62), yy, w, 30), key, lambda key=key: self.pval(key, "0"), lambda v, key=key: self.pset(key, v), lo, hi)
            for i, ln in enumerate(["Expression pedals 1 and 2 send their CC on the channel shown, scaled between min and max.",
                                    "The encoder (knob) sends 'turn CC' when turned and 'push CC' when pressed in."]):
                txt(s, ln, 11, DIM, (L, y + 3 * row + 12 + i * 16))
        elif cur == 2:
            txt(s, "PC BANK SELECT MODE", 13, DIM, (L, y + 8))
            self.numfield((L + 190, y, 56, 30), "pcbs", lambda: self.pval("PC_Bank_Select", "1"), lambda v: self.pset("PC_Bank_Select", v), 0, 10)
            txt(s, "sent before a PC to reach patches above 127", 11, DIM, (L + 262, y + 9))
            for g in range(10):
                cc_, rr = divmod(g, 5)
                xx = L + cc_ * 345
                yy = y + 38 + rr * 31
                txt(s, f"GROUP {g}", 12, DIM, (xx, yy + 7))
                self.rawfield((xx + 70, yy, 255, 27), f"pcg{g}", f"PC_group_{g}")
        else:
            self.tog((L, y, 300, 30), "LOW LIGHT FOR UNSELECTED KEYS", "LEDSelectLowLight", "on", size=12)
            txt(s, "LOW LIGHT LEVEL", 13, DIM, (L, y + row + 8))
            self.numfield((L + 170, y + row, 64, 30), "lowl", lambda: self.pval("LowLightLevel", "10"), lambda v: self.pset("LowLightLevel", v), 0, 100)
            txt(s, "TAP TEMPO MIN", 13, DIM, (L, y + 2 * row + 8))
            self.numfield((L + 170, y + 2 * row, 64, 30), "tapmin", lambda: self.pval("tapcountMIN", "20"), lambda v: self.pset("tapcountMIN", v), 0, 999)
            txt(s, "TAP TEMPO MAX", 13, DIM, (L, y + 3 * row + 8))
            self.numfield((L + 170, y + 3 * row, 64, 30), "tapmax", lambda: self.pval("tapcountMAX", "256"), lambda v: self.pset("tapcountMAX", v), 0, 999)
            txt(s, "CC VALUE GROUPS", 13, DIM, (L, y + 4 * row + 8))
            self.rawfield((L + 170, y + 4 * row, 300, 30), "ccvg", "CC_value_gropnumber")
            for i, ln in enumerate(["Select-mode keys (select, select1-5) dim the other keys of their group.",
                                    "Tap tempo keys send values between the tap MIN and MAX tempo.",
                                    "CC value groups set the sizes used by [CC][n][bank_inc] ..."]):
                txt(s, ln, 11, DIM, (R, y + 8 + i * 18))

    def draw_global(self, s):
        panel(s, (290, 550, 720, 240), "Global settings  (written to every page)")
        L = 310
        y = 592
        row = 38
        for i, (key, lab, lo, hi) in enumerate((("ledbright", "LED BRIGHTNESS", 0, 100), ("screenbright", "SCREEN BRIGHTNESS", 0, 100))):
            txt(s, lab, 13, DIM, (L, y + i * row + 8))
            self.numfield((L + 190, y + i * row, 70, 30), key, lambda key=key: self.gval(key, "80"), lambda v, key=key: self.gset(key, v), lo, hi)
            txt(s, "0 - 100", 11, DIM, (L + 270, y + i * row + 9))
        txt(s, "LONG PRESS", 13, DIM, (L, y + 2 * row + 8))
        lp = self.gval("long_press_timing", "1")
        self.choice((L + 190, y + 2 * row, 110, 30), lp + " s", [(v, v + " seconds") for v in ("0.5", "1", "1.5", "2", "2.5")], lp,
                    lambda v: self.gset("long_press_timing", v), "How long a long press takes")
        bc = self.gval("BATTERY_CHARGE", "off") == "on"
        self.btn((L, y + 3 * row, 300, 30), "BATTERY CHARGE:  " + ("ON" if bc else "OFF"),
                 lambda b: self.gset("BATTERY_CHARGE", "off" if bc else "on"), active=bc, size=13)
        self.btn((L, y + 4 * row, 400, 30), "SET PAGE TURN ON ALL PAGES:  key 5 long = back, key E long = forward",
                 self.apply_page_turn, size=12)
        txt(s, "WIRELESS", 13, DIM, (L + 400, y + 8))
        w = self.gval("WIRELESS_2.4G", "on") == "on"
        self.btn((L + 500, y, 150, 30), "2.4G:  " + ("ON" if w else "OFF"), lambda b: self.gset("WIRELESS_2.4G", "off" if w else "on"), active=w, size=13)
        txt(s, "ID", 12, DIM, (L + 400, y + row + 8))
        self.numfield((L + 440, y + row, 60, 30), "wid", lambda: self.gval("WIRELESS_ID", "8"), lambda v: self.gset("WIRELESS_ID", v), 0, 99)
        txt(s, "dB", 12, DIM, (L + 520, y + row + 8))
        self.numfield((L + 550, y + row, 60, 30), "wdb", lambda: self.gval("WIRELESS_dB", "6"), lambda v: self.gset("WIRELESS_dB", v), 0, 99)
        for i, ln in enumerate(wrap("Firmware 5 stores these on every page; changing one here updates all pages. Wireless is for the Blue/Gold models.", 11, 290)):
            txt(s, ln, 11, DIM, (L + 400, y + 2 * row + 12 + i * 15))

    # ---- key panel --------------------------------------------------------------------------------------------------------
    # ---- ChordStomp ----------------------------------------------------------------------------------------------
    def open_chords(self, _b=1):
        self.show_chords = not self.show_chords
        if not self.show_chords:
            self.cs_stop()
        else:
            self.cs_load_key()

    def cs_key_notes(self, k):
        kv, p = self.kv(k), self.press_of(k)
        dn = [x for x in kv.cmds("short_dw", p) if ctype(x) == "NT" and len(x) > 3 and x[2].isdigit()]
        up = [x for x in kv.cmds("short_up", p) if ctype(x) == "NT"]
        return dn, up

    def cs_load_key(self):
        """Show what is already programmed on the selected key (notes, chord, channel, velocity, release)."""
        c = self.cs
        self.cs_stop()
        dn, up = self.cs_key_notes(self.sel_key)
        notes = sorted({int(x[2]) for x in dn})
        c["sel"], c["ctype"] = notes, None
        if not notes:
            return
        try:
            c["ch"] = clamp(int(dn[0][0]), 1, 16)
            c["vel"] = clamp(int(dn[0][3]), 1, 127)
        except ValueError:
            pass
        c["release"] = bool(up)
        target = set(notes)
        for ct, iv in CHORD_TYPES:
            if len(iv) != len(notes):
                continue
            for root in range(12):
                for octv in range(0, 7):
                    for inv in range(len(iv)):
                        if set(chord_notes(root, ct, octv, inv)) == target:
                            c.update(root=root, ctype=ct, oct=clamp(octv, 1, 5), inv=min(inv, 3))
                            return

    def cs_pick_key(self, i):
        self.select_key(i)
        self.cs_load_key()

    def cs_apply_chord(self):
        c = self.cs
        if c["ctype"]:
            c["sel"] = chord_notes(c["root"], c["ctype"], c["oct"], c["inv"])

    def cs_toggle_note(self, n):
        c = self.cs
        c["ctype"] = None
        if n in c["sel"]:
            c["sel"].remove(n)
        else:
            c["sel"] = sorted(c["sel"] + [n])

    def cs_name(self):
        c = self.cs
        if c["ctype"]:
            nm = chord_label(c["root"], c["ctype"])
            return nm + ("/%d" % c["inv"] if c["inv"] else "")
        return " ".join(note_name(n) for n in c["sel"][:3])

    def cs_play(self, _b=1):
        c = self.cs
        if c["playing"]:
            return self.cs_stop()
        if not c["sel"]:
            return self.set_status("Pick some notes or a chord first.")
        if not self.out:
            return self.set_status("Pick a MIDI OUT (top) first - PLAY sends the notes to your device.")
        try:
            for n in c["sel"]:
                self.out.write_short(0x90 | (c["ch"] - 1), n, c["vel"])
            c["playing"] = list(c["sel"])
            self.set_status("Playing - press STOP (or PLAY) to release the notes.")
        except Exception as ex:
            self.set_status(f"Send failed: {ex}")

    def cs_stop(self, _b=1):
        c = self.cs
        if c["playing"] and self.out:
            try:
                for n in c["playing"]:
                    self.out.write_short(0x80 | (c["ch"] - 1), n, 0)
            except Exception:
                pass
        c["playing"] = []

    def cs_program(self, _b=1):
        c = self.cs
        if not c["sel"]:
            return self.set_status("Pick some notes or a chord first.")
        k = self.sel_key
        kv, p = self.kv(), self.press_of(k)
        chs = str(c["ch"])
        down = [x for x in kv.cmds("short_dw", p) if ctype(x) != "NT"] + [[chs, "NT", str(n), str(c["vel"])] for n in c["sel"]]
        up = [x for x in kv.cmds("short_up", p) if ctype(x) != "NT"]
        if c["release"]:
            up += [[chs, "NT", str(n), "0"] for n in c["sel"]]
        kv.set_cmds("short_dw", p, down)
        kv.set_cmds("short_up", p, up)
        nm = self.cs_name().replace(" ", "_").replace("[", "").replace("]", "")[:10]
        if nm:
            kv.set_name("short_dw", p, nm)
        self.dirty = True
        self.set_status(f"Key {KEY_NAMES_ASCII[k]} press {p}: {len(c['sel'])} notes on PRESS DOWN" + (" and note-off on PRESS UP." if c["release"] else ".") +
                        "  Press SAVE PAGE to write it.")

    def cs_blank_menu(self, _b=1):
        if not self.pages:
            return
        opts = [("empty", "Every key empty  (nothing sent, LEDs grey)"),
                ("ends", "Every key empty, except key 5 / key E: PC up / PC down, long press turns the page")]
        self.popup = BigPopup(f"Blank page {self.sel_page}", opts, None, self.cs_blank_page)

    def cs_blank_page(self, v):
        ch = str(self.page_channel())
        for n in range(10):
            kv = self.kv(n)
            kv.clear()
            if v == "ends" and KEYSEC[n] in ("key5", "keyE"):
                up = KEYSEC[n] == "key5"
                kv.set_cmds("short_up", 1, [[ch, "PC", "value_inc" if up else "value_dec", "-"]])
                kv.set_name("short_up", 1, "PC_+" if up else "PC_-")
                kv.set_cmds("long", 1, [["preset", "value_dec" if up else "value_inc"]])
                kv.set_led(0, [0xFFFFFF] * 3)
                kv.set_led(1, [0xFFFFFF] * 3)
        self.press_view = {}
        self.dirty = True
        self.cs_load_key()
        self.set_status(f"Page {self.sel_page} is blank in the editor (page name and global settings kept). Nothing is written until you press SAVE PAGE.")

    def cs_blank_key(self, _b=1):
        kv = self.kv()
        kv.clear()
        self.press_view.pop(self.sel_key, None)
        self.dirty = True
        self.cs_load_key()
        self.set_status(f"Key {KEY_NAMES_ASCII[self.sel_key]} is blank (all presses, LEDs and commands).")

    def cs_clear_key(self, _b=1):
        k = self.sel_key
        kv, p = self.kv(), self.press_of(k)
        for trig in ("short_dw", "short_up"):
            kv.set_cmds(trig, p, [x for x in kv.cmds(trig, p) if ctype(x) != "NT"])
        self.dirty = True
        self.set_status(f"Key {KEY_NAMES_ASCII[k]} press {p}: note commands removed (other commands kept).")

    def cs_step(self, field, d, lo, hi, step=1):
        c = self.cs
        c[field] = clamp(c[field] + d * step, lo, hi)
        if field in ("oct", "inv"):
            self.cs_apply_chord()

    def draw_chords(self, s):
        c = self.cs
        R = pygame.Rect(30, 90, 980, 700)
        panel(s, tuple(R), "")
        self.hit(R, lambda b: None)                                   # swallow clicks on the empty overlay
        k = self.sel_key
        p = self.press_of(k)
        txt(s, "NOTES & CHORDS", 20, AMBER, (46, 100), bold=True)
        if getattr(self, "cs_last", None) != (k, p):             # key or press changed: show what is on it
            self.cs_last = (k, p)
            self.cs_load_key()
            c = self.cs
        txt(s, f"programs  KEY {KEY_NAMES_ASCII[k]}  press {p}", 14, TEXT, (240, 106))
        tn = self.kv().times()
        if tn > 1:
            txt(s, "PRESS", 11, DIM, (440, 108))
            for i in range(tn):
                self.btn((486 + i * 24, 98, 22, 30), str(i + 1), lambda b, i=i: self.press_view.__setitem__(k, i + 1), active=(i + 1 == p), size=11)
        self.btn((880, 98, 116, 30), "CLOSE", self.open_chords, size=13)
        self.btn((740, 98, 128, 30), "BLANK PAGE...", self.cs_blank_menu, size=12)
        for i in range(10):
            dn, _up = self.cs_key_notes(i)
            nm = self.kv(i).name("short_dw", self.press_of(i))[:7] if dn else ""
            self.btn((46 + i * 95, 138, 91, 28), (KEY_NAMES_ASCII[i] + " " + nm).strip(), lambda b, i=i: self.cs_pick_key(i), active=(i == k), size=12 if not nm else 11)
        # piano
        wk = [n for n in range(PIANO_LO, PIANO_HI) if n % 12 in (0, 2, 4, 5, 7, 9, 11)]
        ww = 26
        x0, y0, wh, bh = 46, 182, 150, 92
        pos = {}
        mp = pygame.mouse.get_pos()
        for i, n in enumerate(wk):
            r = pygame.Rect(x0 + i * ww, y0, ww - 1, wh)
            pos[n] = r
        for n, r in pos.items():
            on = n in c["sel"]
            pygame.draw.rect(s, (255, 211, 51) if on else (236, 230, 214), r, border_radius=3)
            if on:
                pygame.draw.rect(s, ORANGE, r, 2, border_radius=3)
            if n % 12 == 0:
                txt(s, note_name(n), 10, (60, 52, 30), (r.centerx, r.bottom - 16), "center")
            self.hit(r, lambda b, n=n: (self.cs_toggle_note(n)))
        for n in range(PIANO_LO, PIANO_HI):
            if n % 12 in (1, 3, 6, 8, 10) and (n - 1) in pos:
                r = pygame.Rect(pos[n - 1].right - ww // 3 - 1, y0, ww * 2 // 3, bh)
                on = n in c["sel"]
                pygame.draw.rect(s, (255, 150, 30) if on else (30, 27, 20), r, border_radius=3)
                self.hit(r, lambda b, n=n: (self.cs_toggle_note(n)))
        txt(s, "click keys to choose notes (C2 - B6)", 10, DIM, (x0, y0 + wh + 4))
        # readout
        sel = c["sel"]
        nm = self.cs_name() if sel else "-"
        txt(s, "SELECTED", 12, DIM, (46, 352))
        txt(s, nm, 22, LCD_FG, (130, 346), bold=True)
        txt(s, "  ".join(f"{note_name(n)}({n})" for n in sel) or "no notes yet", 12, TEXT, (46, 378))
        self.btn((880, 346, 116, 28), "CLEAR", lambda b: (c.update(sel=[], ctype=None), self.cs_stop()), size=12)
        # chord chart
        txt(s, "CHORD CHART", 12, AMBER, (46, 404), bold=True)
        for i in range(12):
            self.btn((46 + i * 78, 424, 74, 32), NOTE_NAMES[i], lambda b, i=i: (c.update(root=i), self.cs_apply_chord()), active=(i == c["root"]), size=14)
        for j, (lab, _iv) in enumerate(CHORD_TYPES):
            self.btn((46 + (j % 6) * 156, 462 + (j // 6) * 38, 150, 32), lab, lambda b, lab=lab: (c.update(ctype=lab), self.cs_apply_chord()),
                     active=(c["ctype"] == lab), size=14)
        # options
        y = 584
        txt(s, "CHANNEL", 11, DIM, (46, y + 8))
        self.btn((120, y, 28, 28), "-", lambda b: self.cs_step("ch", -1, 1, 16), size=14)
        txt(s, str(c["ch"]), 16, TEXT, (166, y + 6), "midtop", bold=True)
        self.btn((184, y, 28, 28), "+", lambda b: self.cs_step("ch", 1, 1, 16), size=14)
        txt(s, "VELOCITY", 11, DIM, (240, y + 8))
        self.btn((320, y, 28, 28), "-", lambda b: self.cs_step("vel", -1, 1, 127, 5), size=14)
        txt(s, str(c["vel"]), 16, TEXT, (372, y + 6), "midtop", bold=True)
        self.btn((396, y, 28, 28), "+", lambda b: self.cs_step("vel", 1, 1, 127, 5), size=14)
        txt(s, "OCTAVE", 11, DIM, (456, y + 8))
        self.btn((516, y, 28, 28), "-", lambda b: self.cs_step("oct", -1, 1, 5), size=14)
        txt(s, str(c["oct"]), 16, TEXT, (562, y + 6), "midtop", bold=True)
        self.btn((580, y, 28, 28), "+", lambda b: self.cs_step("oct", 1, 1, 5), size=14)
        txt(s, "INVERSION", 11, DIM, (636, y + 8))
        for i in range(4):
            self.btn((716 + i * 34, y, 30, 28), str(i), lambda b, i=i: (c.update(inv=i), self.cs_apply_chord()), active=(c["inv"] == i), size=13)
        self.btn((866, y, 130, 28), "RELEASE = NOTE OFF" if c["release"] else "RELEASE: NONE",
                 lambda b: c.update(release=not c["release"]), active=c["release"], size=11)
        # what is on the key now
        kv = self.kv()
        have = [x for x in kv.cmds("short_dw", p) if ctype(x) == "NT"]
        txt(s, "ON THIS KEY NOW", 11, DIM, (46, 634))
        cur = "  ".join(note_name(int(x[2])) for x in have if x[2].isdigit()) or "no notes"
        txt(s, cur, 13, TEXT, (180, 632))
        # actions
        self.btn((46, 666, 300, 48), "PROGRAM KEY " + KEY_NAMES_ASCII[k], self.cs_program, enabled=bool(self.pages and sel), size=17, color=(150, 90, 10))
        self.btn((360, 666, 150, 48), "STOP" if c["playing"] else "PLAY", self.cs_play, active=bool(c["playing"]), size=15)
        self.btn((524, 666, 220, 48), "REMOVE NOTES FROM KEY", self.cs_clear_key, size=12)
        self.btn((758, 666, 238, 48), "BLANK KEY (everything)", self.cs_blank_key, size=12)
        txt(s, "PLAY needs MIDI OUT (top). PROGRAM KEY writes to the page in the editor; press SAVE PAGE afterwards.", 11, DIM, (46, 726))
        txt(s, "Several notes on one key = a chord, all sent together on PRESS DOWN.", 11, DIM, (46, 744))

    def draw_key_panel(self, s):
        panel(s, (1020, 90, 350, 700), "")
        kv = self.kv()
        k = self.sel_key
        p = self.press_of(k)
        t = kv.times()
        txt(s, f"KEY  {KEY_NAMES_ASCII[k]}", 20, AMBER, (1036, 104), bold=True)
        txt(s, f"[{KEYSEC[k]}]", 11, DIM, (1036, 130))
        self.btn((1190, 100, 164, 32), "QUICK SET...", self.quick_set, size=13)
        self.btn((1190, 138, 164, 26), "TIDY ORDER", self.tidy_key, size=11)
        self.btn((1036, 148, 146, 28), "NOTES / CHORDS", self.open_chords, active=self.show_chords, size=12, color=(150, 90, 10))
        txt(s, "PRESSES", 12, DIM, (1036, 190))
        self.btn((1110, 184, 30, 28), "-", lambda b: (kv.set_times(t - 1), setattr(self, "dirty", True)) if t > 1 else None, size=15)
        txt(s, str(t), 18, TEXT, (1160, 198), "center", bold=True)
        self.btn((1180, 184, 30, 28), "+", lambda b: (kv.set_times(t + 1), setattr(self, "dirty", True)) if t < 10 else None, size=15)
        lm = kv.ledmode()
        self.choice((1226, 184, 128, 28), "LED: " + lm, LED_MODES, lm,
                    lambda v: (kv.set_ledmode(v), setattr(self, "dirty", True)), "LED mode", size=12)
        if t > 1:
            txt(s, "PRESS", 12, DIM, (1036, 236))
            for i in range(t):
                self.btn((1096 + i * 25, 230, 23, 26), str(i + 1), lambda b, i=i: self.press_view.__setitem__(k, i + 1),
                         active=(i + 1 == p), size=11)
        names = ("L", "R", "B") if k < 5 else ("L", "T", "R")
        for row_, (lab, n_) in enumerate((("LED RING", p), ("START", 0))):
            yy = 282 + row_ * 50
            txt(s, lab, 12, DIM, (1036, yy + 6))
            for i in range(3):
                txt(s, names[i], 10, DIM, (1138 + i * 62, yy + 32), "center")
            cols = kv.led(n_)
            for i in range(3):
                r = pygame.Rect(1110 + i * 62, yy, 56, 28)
                pygame.draw.rect(s, (8, 8, 8), r.inflate(4, 4), border_radius=6)
                pygame.draw.rect(s, rgb(cols[i]), r, border_radius=5)
                if r.collidepoint(pygame.mouse.get_pos()):
                    pygame.draw.rect(s, AMBER, r.inflate(4, 4), 2, border_radius=6)
                self.hit(r, lambda b, i=i, n_=n_: self.pick_color(i, n_))
            self.btn((1298, yy, 56, 28), "ALL", lambda b, n_=n_: self.pick_color(3, n_), size=12)
            if n_ == 0 and t > 1:
                self.btn((1034, yy + 26, 70, 22), "LAST=START", lambda b: (kv.set_led(t, kv.led(0)), setattr(self, "dirty", True)), size=10)
        # triggers (scrolling area)
        top = 392
        area = pygame.Rect(1024, top, 342, 786 - top)
        self.clip = area
        s.set_clip(area)
        y = top - self.kscroll
        for trig, label in TRIGGERS:
            cmds = kv.cmds(trig, p)
            txt(s, f"{label}", 12, AMBER, (1036, y + 7), bold=True)
            nm = kv.name(trig, p)

            def setnm(v, trig=trig, p=p, kv=kv):
                v = v.replace(" ", "_")
                if len(v) > 10 or "[" in v or "]" in v:
                    return "Action name: up to 10 characters, no brackets."
                kv.set_name(trig, p, v)
                self.dirty = True
            self.field((1140, y + 1, 76, 26), f"nm{trig}{p}{k}", nm, setnm)
            if not nm and not (self.edit and self.edit["id"] == f"nm{trig}{p}{k}"):
                txt(s, "name", 11, (116, 110, 96), (1148, y + 8))
            self.btn((1220, y + 1, 52, 26), "+ PC", lambda b, trig=trig: self.auto_menu(trig), size=11)
            self.btn((1276, y + 1, 78, 26), "+ CMD", lambda b, trig=trig: self.edit_cmds(trig, lambda c: c.append(list(TYPE_DEFAULTS["CC"]))), size=11)
            y += 32
            for i, c in enumerate(cmds):
                self.draw_cmd_row(s, trig, i, c, y)
                y += 50
            if not cmds:
                txt(s, "(nothing sent)", 11, (116, 110, 96), (1050, y))
                y += 20
            y += 8
        self.kmax = max(0, y + self.kscroll - 786)
        s.set_clip(None)
        self.clip = None
        if self.kmax > 0:
            txt(s, "wheel = scroll   Ctrl+wheel = change a value", 10, DIM, (1340, 776), "midright")
            gx, gy, gh = 1357, 392, 380
            pygame.draw.rect(s, (30, 27, 20), (gx, gy, 10, gh), border_radius=4)
            th = max(30, int(gh * gh / (gh + self.kmax)))
            ty = gy + int((gh - th) * (self.kscroll / max(1, self.kmax)))
            pygame.draw.rect(s, AMBER, (gx, ty, 10, th), border_radius=4)
            self.hit((gx - 3, gy, 16, gh), lambda b: self.set_kscroll_from_mouse(gy, gh), lambda dy: setattr(self, "kscroll", clamp(self.kscroll - dy * 40, 0, self.kmax)))

    def set_kscroll_from_mouse(self, gy, gh):
        y = pygame.mouse.get_pos()[1]
        self.kscroll = clamp(int((y - gy) / gh * (self.kmax + gh)) - gh // 2, 0, self.kmax)

    def tidy_key(self, _b=1):
        before = [i.key for i in self.kv().sec.items]
        tidy_key_section(self.kv().sec)
        if before != [i.key for i in self.kv().sec.items]:
            self.dirty = True
            self.set_status("Key tidied: each press' lines are now grouped together (LED colour, press down, press up, long ...).")
        else:
            self.set_status("This key's lines are already in tidy order.")

    def draw_cmd_row(self, s, trig, i, c, y):
        ty = ctype(c)
        x = 1034
        if is_std(c):
            self.field((x, y, 36, 28), f"{trig}{i}ch", c[0], lambda v: self.set_field(trig, i, 0, v),
                       lambda dy: self.bump(trig, i, 0, dy), align="center")
            self.choice((x + 40, y, 56, 28), ty, [(q, q) for q in CMD_TYPES], ty, lambda v: self.change_type(trig, i, v), "Command type", size=12)
            self.field((x + 100, y, 74, 28), f"{trig}{i}a", c[2], lambda v: self.set_field(trig, i, 2, v),
                       lambda dy: self.bump(trig, i, 2, dy), menu=lambda: self.value_menu(trig, i, 2), align="center")
            self.field((x + 178, y, 74, 28), f"{trig}{i}b", c[3], lambda v: self.set_field(trig, i, 3, v),
                       lambda dy: self.bump(trig, i, 3, dy), menu=lambda: self.value_menu(trig, i, 3), align="center")
            self.btn((x + 256, y, 30, 28), "T", lambda b: self.test_cmd(c), size=12)
        else:
            self.choice((x, y, 92, 28), ty, [(q, q) for q in CMD_TYPES], ty, lambda v: self.change_type(trig, i, v), "Command type", size=12)
            self.field((x + 96, y, 190, 28), f"{trig}{i}raw", " ".join(c), lambda v: self.set_raw(trig, i, v), menu=lambda: self.raw_menu(trig, i))
        self.btn((x + 290, y, 30, 28), "x", lambda b: self.edit_cmds(trig, lambda cc: cc.pop(i)), size=12)
        d = describe_cmd(c, self.profile)
        txt(s, d, fit_size(d, 300, (11, 10), bold=False), DIM, (x + 2, y + 31))

    def auto_menu(self, trig):
        """+ PC: add a program change command (PC up/down, bank up/down, patch slot of the group)."""
        opts = [("value_inc", "PC UP   (value_inc)"), ("value_dec", "PC DOWN   (value_dec)"),
                ("bank_inc", "BANK UP   (bank_inc)"), ("bank_dec", "BANK DOWN   (bank_dec)")] + [(f"auto{n}", f"patch slot {n}   (auto{n})") for n in range(10)]
        self.popup = BigPopup("Add PC command", opts, None,
                              lambda v: self.edit_cmds(trig, lambda c: c.append(["1", "PC", v, "-"])))

    def raw_menu(self, trig, i):
        kv, p = self.kv(), self.press_of(self.sel_key)
        c = kv.cmds(trig, p)[i]
        opts = [(w, w) for w in RAW_TEMPLATES.get(ctype(c), [])]
        if opts:
            self.popup = BigPopup("Ready-made " + ctype(c) + " commands", opts, None, lambda v: self.set_raw(trig, i, v))

    def value_menu(self, trig, i, j):
        kv, p = self.kv(), self.press_of(self.sel_key)
        c = kv.cmds(trig, p)[i]
        if not is_std(c):
            return
        ty = ctype(c)
        if ty == "PC" and j == 2:
            self.popup = BigPopup("PC value", [(w, w) for w in PC_WORDS], None, lambda v: self.set_field(trig, i, j, v))
        elif ty in ("CC", "CCT") and j == 2:
            opts = [(str(n), f"{n:>3}  {name}") for n, name, _ in self.profile["items"]]
            self.popup = BigPopup(f"{self.profile['name']} controllers", opts, c[2], lambda v: self.set_field(trig, i, 2, v))
        elif ty in ("CC", "CCT") and j == 3:
            opts = [("127", "127  (on / press)"), ("0", "0  (off)"), ("64", "64"), ("100", "100")]
            if ty == "CC":
                opts += [(w, w) for w in CC_WORDS]
            self.popup = BigPopup("CC value", opts, c[3], lambda v: self.set_field(trig, i, 3, v))

    # ---- events --------------------------------------------------------------------------------------------------------------
    def handle(self, e):
        if e.type == pygame.QUIT:
            self.request_quit()
            return
        if self.popup:
            self.popup.handle(e, self)
            return
        if e.type == pygame.KEYDOWN:
            if self.edit:
                ed = self.edit
                if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_TAB):
                    self.commit_edit()
                elif e.key == pygame.K_ESCAPE:
                    self.edit = None
                elif e.key == pygame.K_BACKSPACE:
                    ed["buf"] = ed["buf"][:-1]
                elif e.unicode and e.unicode.isprintable() and len(ed["buf"]) < 48:
                    ed["buf"] += e.unicode
                return
            if e.key == pygame.K_ESCAPE:
                self.request_quit()
            elif e.key == pygame.K_s and (e.mod & pygame.KMOD_CTRL):
                self.save()
            return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            was = self.edit
            if was:
                self.commit_edit()
            for r, cb, _w in reversed(self.hits):
                if r.collidepoint(e.pos):
                    cb(e.button)
                    return
        if e.type == pygame.MOUSEWHEEL:
            pos = pygame.mouse.get_pos()
            ctrl = bool(pygame.key.get_mods() & pygame.KMOD_CTRL)
            if self.show_chords and pygame.Rect(30, 90, 980, 700).collidepoint(pos):
                return
            if not ctrl and pygame.Rect(1020, 392, 350, 394).collidepoint(pos):
                self.kscroll = clamp(self.kscroll - e.y * 40, 0, self.kmax)
                return
            if not ctrl and not self.show_factory and pygame.Rect(290, 540, 720, 250).collidepoint(pos):
                self.set_status("Hold Ctrl while turning the wheel to change a value.")
                return
            for r, _cb, w in reversed(self.hits):
                if w and r.collidepoint(pos):
                    w(e.y)
                    return
            if pygame.Rect(1024, 392, 342, 394).collidepoint(pos):
                self.kscroll = clamp(self.kscroll - e.y * 40, 0, self.kmax)

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
        try:
            if self.out:
                self.out.close()
            if pgmidi:
                pgmidi.quit()
        except Exception:
            pass
        pygame.quit()


def main():
    shot = None
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]

    def arg(name, default=None):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
    app = App(headless=bool(shot), folder=arg("--folder"), page=int(arg("--page", 0)), key=int(arg("--key", 0)))
    app.set_tab = int(arg("--tab", 0))
    app.show_global = "--global" in sys.argv
    if "--chords" in sys.argv:
        app.show_chords = True
        if "--demo" in sys.argv:
            app.cs.update(root=2, ctype="m7", sel=chord_notes(2, "m7", 3, 0))
    if "--factory" in sys.argv:
        app.open_restore()
    app.run(shot)


if __name__ == "__main__":
    main()
