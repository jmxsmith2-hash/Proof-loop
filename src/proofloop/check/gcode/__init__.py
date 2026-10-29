"""Check sliced G-code (plain .gcode or Bambu Studio .gcode.3mf) before it goes to the printer.

From other parts of ProofLoop (Loop files › Check a file uses this):

    from proofloop.check.gcode import check_file
    result = check_file("plate.gcode.3mf", write_report=False)
    for f in result["findings"]:
        print(f["severity_name"], f["title"], f["detail"], f["fix"])
"""


def check_file(path, *, printer=None, material=None, plate=None, out=None, write_report=True, config_dir=None):
    """Check one file; printer and filament are read from it when not given. Returns the result dict."""
    from proofloop.check.gcode.pipeline import check_gcode

    return check_gcode(config_dir, path, printer=printer, material=material, plate=plate, out=out,
                       write_report=write_report)
