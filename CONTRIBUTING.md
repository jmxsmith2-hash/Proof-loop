# Contributing to ProofLoop

Thanks for helping. ProofLoop runs on people's own computers and printers, so test a change on the
real thing where you can, or with the pretend printers where you can't.

## Set up

On Windows, `setup.bat` makes `.venv` here and also adds a desktop shortcut and starts ProofLoop
at login. For development, these commands are enough.

```bash
py -3.12 -m venv .venv                     # Windows (macOS/Linux: python3.12 -m venv .venv)
.venv\Scripts\python -m pip install -e ".[dev,all]" -c constraints.txt
```

Keep `constraints.txt` as it is. It pins the versions CI tested (see
[docs/development.md](docs/development.md#versions)).

## Run the checks

```bash
ruff check src tests tools examples
python -m mypy src
python -m pytest -q                 # a few minutes; add --ignore=tests/e2e for a quicker run
```

To try a change without printers, run `proofloop try`. It starts three pretend printers that
print about 40 times faster than real ones, and it never touches your own printers or data.

[docs/development.md](docs/development.md) has the rest: the test folders, coverage, the evidence
check against the public FarmLoop files, and how CI is set up.

## Style

Match the existing docs and window text: plain, short words, in the terms a person sees on screen.
Say what happens, not how it works inside. Write menu paths as *Tab › Section* and button names in
bold, as the README does.

## CHANGELOG.md

`CHANGELOG.md` has one section per released version, newest first, headed `## 0.4.0 · 2026-10-30`,
with `### New`, `### Changed` and `### Fixed`. The release workflow uses the section for the tag
as the release notes, and the window shows new sections under *More › What's new*. Write each
line for the user, not the code. Don't edit sections for versions that are already released. The
steps to release are in [docs/development.md](docs/development.md#releases).

## Pull requests

- Keep CI green. Every pull request runs the tests on Python 3.11 to 3.13, ruff, mypy, the
  Windows and Linux runs and the `setup.bat` checks. See [CI](docs/development.md#ci).
- Fill in the pull request template.
- Don't report a security problem in an issue or pull request. See [SECURITY.md](SECURITY.md).

By contributing you agree your changes are licensed under AGPL-3.0-or-later, as the rest of
ProofLoop is ([LICENSE](LICENSE)).
