MIDI control programs (unofficial)
==================================

Free software.  Copyright (c) 2026 C.GhostGalleon.  MIT licence (LICENSE.txt): anyone may use, copy,
modify and share these programs for free, as long as the copyright notice stays with them.
Unofficial programs: not affiliated with any equipment maker. They send MIDI SysEx that can overwrite sounds
in your equipment: back up your device first.

PROGRAMS
--------
POD Pro Control, SC-88Pro Control, V-AMP Control, PSR-E223 Control, VM200 Control, MIDI Captain Editor
(firmware 5), MIDI Captain Editor FW4, Pro VS Mini Control, TD-3 Control, TD-6V Control, GP-5 Control, GP-200 Sync.

INSTALL AND RUN
---------------
1. Install Python for Windows, then double-click install_requirements.bat (once).
2. Open a program's folder and double-click run_python.bat.

FOLDERS
-------
    <Program name>\
        python\    the program (Python source)
        data\      YOUR settings, sound libraries and patch tables. Created and filled by the program.
        defaults\  starter files (only some programs); copied into data\ the first time if missing.
    tools\         captain_fw5_convert.py turns MIDI Captain firmware 4 pages into firmware 5 pages.

THE ONE RULE
------------
To update, copy over the python\ and defaults\ folders. Never replace your data\ folder.

FILES THE PROGRAMS KEEP IN data\  (plain text; best left to the programs)
--------------------------------------------------------------------------
GP-200 Sync:   gp200_table.json (patch table), gp200_settings.json, gp200_defaults.json (start-of-evening snapshot)
V-AMP, POD Pro, SC-88Pro, PSR:  <name>_data.json (names and presets), plus *_settings.json
MIDI Captain Editor:  works on your page files (page0.txt, page1.txt ...), which are plain text, not JSON.
