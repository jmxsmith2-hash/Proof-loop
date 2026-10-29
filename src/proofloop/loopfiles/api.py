"""Make a loop file the way Make a loop file would, with your saved settings.

Check's "Make it now" button and ``proofloop check prepare --loops`` use this, so a loop file made
there gets the same hardware profile, sweep floor, push depth, cooldown and first-sweep safety pause.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from proofloop.core import printers
from proofloop.core.paths import PRINTERS_DB, home
from proofloop.core.settings import SettingsStore
from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.processor import ProcessError, Result, analyze, process
from proofloop.loopfiles.gcode.routines import LoopSettings
from proofloop.loopfiles.profiles import HardwareProfile, ProfileStore
from proofloop.monitor.journal import PrintJournal
from proofloop.monitor.service import loop_settings


def pick_profile(store: ProfileStore, model_id: str, preferred: str) -> HardwareProfile | None:
    """The saved default profile if it fits this printer, otherwise the first one that does."""
    matching = store.for_printer(model_id)
    if not matching:
        return None
    return {p.id: p for p in matching}.get(preferred, matching[0])


def swept_before(data_dir: str | Path) -> bool:
    db = Path(data_dir) / PRINTERS_DB
    if not db.is_file():
        return False
    from proofloop.monitor.store import Store

    return Store(db).has_good_loop()


def make_loop_file(
    data: bytes, name: str, loops: int | None = None, data_dir: str | Path | None = None
) -> tuple[Result, HardwareProfile, LoopSettings]:
    """A loop file from a sliced plate, with your saved loop-file settings. Raises ProcessError."""
    data_dir = Path(data_dir or home())
    sf = threemf.loads(data, name=name)
    defaults = SettingsStore(data_dir).load().defaults
    profile = pick_profile(ProfileStore(data_dir / "profiles"), sf.info.printer_model_id, defaults.profile_id)
    if profile is None:
        raise ProcessError(
            f"no hardware profile for the {printers.model_name(sf.info.printer_model_id)} yet: add your kit in "
            "Loop files › Hardware"
        )
    st = loop_settings(defaults, None, swept_before(data_dir))
    if loops:
        st = replace(st, loops=loops)
    a = analyze(sf, profile, st)
    if not a.ok:
        raise ProcessError("; ".join(a.problems))
    result = process(sf, profile, st)
    PrintJournal(None, data_dir).keep_file(result.filename, result.data)  # so the journal can find its G-code
    return result, profile, st
