"""Everything is in pyproject.toml. This only puts CHANGELOG.md inside the built package, so
``proofloop update`` and *More > What's new* can read it from a pip or pipx install too."""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        changelog = Path("CHANGELOG.md")
        if changelog.is_file() and not self.dry_run:
            shutil.copyfile(changelog, Path(self.build_lib) / "proofloop" / "CHANGELOG.md")


setup(cmdclass={"build_py": BuildPy})
