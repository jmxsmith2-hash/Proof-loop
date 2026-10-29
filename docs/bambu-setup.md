# Printer setup

## Making and sending loop files

No printer setup is needed to make loop files. ProofLoop only writes them. Send them with Bambu
Studio, Bambu Handy or Bambuddy, in whatever network mode you use now.

- Slice with the printer's normal Bambu Studio profile. Object labels must be
  on: they are by default in Bambu Studio. In OrcaSlicer, turn on *Label
  objects*.
- Export with *File › Export › Export plate sliced file*. That writes a
  `.gcode.3mf`.
- Keep the output's `FL_S2_` / `FL_S1_` name.

## FarmBoard (Stage 2)

Set the FarmBoard up as 3D Farmers describe. For digital (MQTT) mode, put the
printer in LAN-only mode with Developer Mode on. In the board's OTA page enter
your Wi-Fi, the printer IP, LAN access code and serial number, and tick
*Enable MQTT auto-trigger*. ProofLoop's loop files also contain FarmLoop's
switch-tap gestures, so a board in mechanical mode responds the same way it
does to FarmLoop's own files.

## Watching printers and the plate check (optional)

1. On the printer, turn on **LAN-only mode** and **Developer Mode**. Note the
   IP address, serial number and access code. The access code changes every
   time you toggle either mode.
2. In ProofLoop, open *Printers › Monitor › Add a printer* (or *Find printers*).
3. With the plate **empty**, press *Save empty-plate picture*. Add a few:
   door open and closed, light on and off.
4. Put a part on the plate and press *Check plate*. It should report parts left.
5. Make loop files with *Wait for the plate check* on.

If the printer reports HMS 0500-0500-0001-0007 ("MQTT command verification
failed"), Developer Mode is off and the printer will refuse `resume`.

X1, X2D, H2 and P2S cameras need `ffmpeg` on the computer running ProofLoop.
The Docker image includes it.
