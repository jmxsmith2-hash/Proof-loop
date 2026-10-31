# PyInstaller spec: ProofLoop as a folder you can run without Python installed.
#   pip install ".[all]" -c constraints.txt pyinstaller
#   pyinstaller packaging/windows/proofloop.spec --noconfirm --distpath dist --workpath build/pyinstaller
# Gives dist/ProofLoop/ with proofloop.exe (command line) and ProofLoopApp.exe (opens the window).
from pathlib import Path

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)

# The window's pages, templates, data, migrations and so on are files inside the package.
datas = collect_data_files("proofloop")
if not any(Path(src).name == "CHANGELOG.md" for src, _ in datas):  # installed from a wheel, it is there already
    datas += [(str(HERE.parents[1] / "CHANGELOG.md"), "proofloop")]  # "What's new" reads it from the package
datas += collect_data_files("trimesh")  # trimesh keeps schemas and templates beside its code
hiddenimports = (
    collect_submodules("proofloop")  # many are imported inside functions, only when a command runs
    + collect_submodules("uvicorn")  # the server picks its loops and protocols by name
    + collect_submodules("paho.mqtt")
    + collect_submodules("pystray")  # one backend per platform, chosen at run time
    + collect_submodules("trimesh.resolvers")
    + ["cascadio", "multipart", "PIL.ImageDraw", "PIL.ImageFont", "PIL.ImageFilter"]
)
excludes = ["tkinter", "matplotlib", "pytest", "mypy", "ruff", "IPython", "notebook"]

# The icon is drawn by ProofLoop itself (the one next to the clock): save it as the .exe's icon.
icon = Path(CONF["workpath"]) / "proofloop.ico"
icon.parent.mkdir(parents=True, exist_ok=True)
from proofloop.app.tray import icon_image  # noqa: E402

icon_image(256).save(icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def analysis(script):
    return Analysis([str(HERE / script)], pathex=[str(HERE)], datas=datas, hiddenimports=hiddenimports,
                    excludes=excludes, noarchive=False)


cli = analysis("proofloop_cli.py")
app = analysis("proofloop_app.py")
MERGE((cli, "proofloop_cli", "proofloop"), (app, "proofloop_app", "ProofLoopApp"))  # share the libraries

cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="proofloop", console=True,
              icon=str(icon), upx=False)
app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="ProofLoopApp", console=False,
              icon=str(icon), upx=False)
COLLECT(cli_exe, cli.binaries, cli.datas, app_exe, app.binaries, app.datas, upx=False, name="ProofLoop")
