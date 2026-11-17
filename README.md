# midi-control-apps
Here are descriptions for all twelve apps, plus the Midi Captain pages FW4 to FW5 converter. 

## Captain ChordStomp (firmware 5)
Visual editor for the PaintAudio MIDI Captain STD’s SuperMode page files.
Like the MIDI Captain Editor below but now has the ablility to program Chords and Notes too in a very visual GUI interface.

## MIDI Captain Editor (firmware 5)
Visual editor for the PaintAudio MIDI Captain STD’s SuperMode page files.
Edit what each of the ten footswitches sends on a short press, a release and a long press. It covers program changes, CC messages, toggle CCs, bank changes and page turns. It also has the page and global settings, such as patch grouping, bank select, tap tempo, MIDI Thru, the encoder, the expression pedals and the LEDs. It can send a key’s messages to check them first.

## MIDI Captain Editor FW4
The same kind of editor for the older firmware 4 page format.
For Captains still on firmware 4. A separate tool converts firmware 4 page files to firmware 5.

## GP-200 Sync
Keeps the MIDI Captain’s key LEDs in step with a Valeton GP-200.
The GP-200 doesn’t report which effects a patch has on. This app keeps a table of the GP-200’s 256 patches and which modules are on in each, learned from the Captain’s key presses. It sends the states back to the Captain’s LEDs. It can also restore the last-known states to the GP-200 after a patch change, reset a patch to a saved default from the encoder button, and show a stage view of the current patch.

## GP-5 Control
Control surface for the Valeton GP-5 multi-effects pedal.
Drives the MIDI controls in the GP-5 manual: patch, bank and volume, the module on/off switches, the tuner and the footswitch. It also keeps patch names, set lists and scenes.

## POD Pro Control 
Control surface for the Line 6 POD Pro (1999).
Sends the POD Pro’s amp, cabinet, effect and knob settings. It also works with presets and the unit’s SysEx dumps. It handles the unit’s quirks, such as an amp change making it resend a burst of settings.

## V-AMP Control
Control surface and preset library for the Behringer V-AMP (Mk1, 2001).
Control of amp, cabinet, effects and reverb settings, plus a library of the unit’s 125 presets. It receives and sends full dumps, saves them, and starts with blank presets until you load your own unit’s.

## SC-88Pro Control
Control surface for the Roland SC-88Pro sound module (1996).
Port A and B part strips, effects and EQ, the insertion effect, part and drum editing, and reading the unit’s state back. It saves setups and exports MIDI files so a DAW can recall them.

## PSR-E223 Control
Control surface for the Yamaha PSR-E223 / YPT-220 keyboard.
Picks any of the 375 voices and 13 drum kits by bank select and program change. It uses the keyboard as a 16-part sound module with volume, pan, reverb and chorus per channel, saves setups, and exports a MIDI clip. It also shows what the keyboard sends.

## VM200 Control
Control surface for the Fostex VM200 digital mixer (1999).
Faders, mutes, solos, pan, phase, aux and effect sends, the 4-band EQ, the effects library and scene recall. It reads the mixer back, does MMC transport and full bulk-dump backups, and has a monitor that explains each message in plain English.

## TD-3 Control
Control surface for the Roland TD-3 V-Drums sound module (2004).
Plays the pads on screen and runs a step sequencer through the module. It changes drum kits, and saves and restores the module’s data with a bulk dump.

## TD-6V Control
The same for the Roland TD-6V V-Drums sound module.
Plays the pads and runs a step sequencer on the module’s drum channel. It changes between the 99 kits and saves and restores the module’s data with a bulk dump.

## Pro VS Mini Control
Control surface for the Behringer Pro VS Mini synthesizer.
Every control sends a MIDI CC on the channel you pick. It has a sound library, and SEND ALL makes the synth match the screen. It follows the synth’s own knobs if they send CCs.

## Captain FW4 to FW5 converter (in tools)
Turns firmware 4 Super Mode page files into the firmware 5 format.

Trademarks
----------
These are independent, unofficial control programs. Names of products such as
POD, V-AMP, SC-88Pro, PSR-E223, VM200, TD-3, TD-6V, Pro VS Mini, GP-5, GP-200, MIDI Captain,
Line 6, Behringer, Roland, Yamaha, Fostex, Valeton and Paint Audio belong
to their respective owners and are used only to say which equipment the
programs work with. They are not affiliated with or endorsed by those companies.
