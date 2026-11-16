#!/usr/bin/env python3
# Copyright (c) 2026 C.GhostGalleon. Free to use, copy, modify and share under the MIT licence (see LICENSE.txt).
# Please keep this notice. Unofficial program: not affiliated with any equipment maker.
"""
CAPTAIN FW4 -> FW5 PAGE CONVERTER
=================================
Turns MIDI Captain (STD / Blue / Gold) Super Mode page files written for firmware 4.x into the firmware 5.x format.

Use:     python captain_fw5_convert.py  <folder with your old pageN.txt files>  [output folder]
         (or drag the folder onto this script)
Result:  a new folder  <folder>_FW5  holding page0.txt, page1.txt ...  Copy those into the Captain's  supersetup  folder.
         Your old files are never changed.  A short report lists anything that needs a look by hand.

What changes between the two firmwares (from the FW5.0 update log and command specification):
  * every page now has one [GlobalSetup] block (screen, LED, exp pedals, encoder, MIDI IN, ...) instead of [globalsetup] on page 0 + [PAGE]
  * keys are called key1-key5 and keyA-keyE  (old key0-key9)
  * every key has an extra  ledcolor0  = the colour shown before the first press
  * auto PC:  [1][PC][auto][bank_inc] -> [1][PC][bank_inc][-],   [1][PC][auto][2] -> [1][PC][auto2][-]
  * the two end keys no longer flip pages by themselves: a long press now needs  [preset][value_inc] / [preset][value_dec]
  * HID:  [send][HID][Ctrl][A] -> [HID][send][ctrl][A][END]
Needs only Python 3.
"""
import re
import sys
from pathlib import Path

OLD_KEYS = {"key0": "key1", "key1": "key2", "key2": "key3", "key3": "key4", "key4": "key5",
            "key5": "keyA", "key6": "keyB", "key7": "keyC", "key8": "keyD", "key9": "keyE"}
NEW_ORDER = ["key1", "key2", "key3", "key4", "key5", "keyA", "keyB", "keyC", "keyD", "keyE"]
PRESS_RE = re.compile(r"^(ledcolor|short_dw|short_up|long_up|long)(\d+)(_name)?$")
RANK = {"ledcolor": 0, "short_dw": 1, "short_up": 2, "long": 3, "long_up": 4}


def groups(raw):
    return re.findall(r"\[([^\]]*)\]", raw or "")


def parse(text):
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


def conv_cmd(c, notes, where):
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


def conv_raw(raw, notes, where):
    g = groups(raw)
    if not g:
        return raw
    # variable-length forms (HID) start with a keyword, not a channel number; commands are 4 groups otherwise
    out, i = [], 0
    while i < len(g):
        chunk = g[i:i + 4]
        cc = conv_cmd(chunk, notes, where)
        out.append("".join(f"[{x}]" for x in cc))
        i += 4
    return " ".join(out)


def convert(text, pageno, notes, base=None):
    gl, secs = parse(text)
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
        new = OLD_KEYS.get(oldname.lower())
        if new:
            byname[new] = items
        else:
            notes.append(f"page{pageno}: unknown section [{oldname}] skipped")
    for k in NEW_ORDER:
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
            mm = PRESS_RE.match(key)
            if mm:
                pressed.append(((int(mm.group(2)), RANK[mm.group(1)], 1 if mm.group(3) else 0), key, raw))
            else:
                plain.append((key, raw))
        pressed.sort(key=lambda t: t[0])
        for key, raw in plain:
            L.append(f"{key} = {raw}")
        last_led = None
        for _o, key, raw in pressed:
            if key.startswith("ledcolor"):
                if int(PRESS_RE.match(key).group(2)) == times:
                    last_led = raw
        cols = {int(PRESS_RE.match(key).group(2)): raw for _o, key, raw in pressed if key.startswith("ledcolor")}
        L.append(f"ledcolor0 = {last_led or cols.get(1) or '[0x000000][0x000000][0x000000]'}")
        # nav keys: the old firmware turned pages by itself on the two end keys, FW5 needs a command
        nav = {"key5": "[preset][value_inc]", "keyE": "[preset][value_dec]"}.get(k)
        has_long = any(key == "long1" for _o, key, _r in pressed)
        for _o, key, raw in pressed:
            if PRESS_RE.match(key).group(3):
                L.append(f"{key} = {raw}")
            elif key.startswith("ledcolor"):
                L.append(f"{key} = {raw}")
            else:
                L.append(f"{key} = {conv_raw(raw, notes, f'page{pageno} {k} {key}')}")
        if nav and not has_long:
            L.append(f"long1 = {nav}")
    return "\n".join(L) + "\n"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        input("Press Enter to close")
        return
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.name + "_FW5")
    dst.mkdir(parents=True, exist_ok=True)
    notes, n = [], 0
    base = {}
    p0 = src / "page0.txt"
    if p0.exists():
        base = {k: v for k, v in parse(p0.read_text(encoding="utf-8", errors="replace"))[0].items()
                if k in ("ledbright", "screenbright", "long_press_timing", "WIRELESS_2.4G", "WIRELESS_ID", "WIRELESS_dB")}
    files = sorted(src.glob("page*.txt"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    for f in files:
        m = re.fullmatch(r"page(\d+)\.txt", f.name)
        if not m:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if "[GlobalSetup]" in text and "[key1]" in text:
            notes.append(f"{f.name}: already FW5 format - copied as it is")
            (dst / f.name).write_text(text, encoding="utf-8", newline="\n")
            continue
        (dst / f.name).write_text(convert(text, int(m.group(1)), notes, base), encoding="utf-8", newline="\n")
        n += 1
    print(f"Converted {n} page file(s) into  {dst}")
    for t in notes:
        print("  note:", t)
    if len(sys.argv) < 2 or getattr(sys, "frozen", False):
        input("Press Enter to close")


if __name__ == "__main__":
    main()
