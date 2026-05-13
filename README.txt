Prompt - Desktop prompt, workflow, and doctype engine
=====================================================

Author:   Trenton Tompkins <trenttompkins@gmail.com>
Phone:    (724) 431-5207
Website:  https://trentontompkins.com
GitHub:   https://github.com/tibberous
License:  MIT  (see LICENSE)


What it is
----------
Prompt is a self-contained Python + Qt6 desktop application for managing LLM
prompts, multi-step workflows, and document templates (doctypes). It ships
as a stand-alone Windows executable; no Python install required on target.


Quick start (from source)
-------------------------
    python start.py                          Run directly
    .\auto_build_exes.ps1                    Build all 5 EXE backends
    .\auto_build_installers.ps1              Build all installers
    .\auto_deploy.ps1                        Full pipeline -> release_upload/


Build pipeline (5 executable backends x 3 installer types = 15 artifacts)
-------------------------------------------------------------------------
Executables (dist/):
    Prompt-PyInstaller.exe           PyInstaller onefile
    Prompt-PyInstallerDir-bundle.zip PyInstaller onedir
    Prompt-Nuitka.exe                Nuitka compiled
    Prompt-cx_Freeze-bundle.zip      cx_Freeze frozen
    Prompt-PyApp.exe                 PyApp Rust launcher

Installers (built per-backend):
    PromptSetup-<backend>-NSIS.exe   NSIS (makensis)
    PromptSetup-<backend>-Inno.exe   Inno Setup (iscc)
    PromptSetup-<backend>-WiX.msi    WiX MSI


Configuration
-------------
    config.ini          Live API keys + local paths.  GITIGNORED.
    config.sample.ini   Push-safe template (placeholders only).
    build_info.ini      EXE + installer metadata (author, copyright,
                        URL, phone, license).  Edit + rebuild to
                        propagate everywhere.


Icon
----
A single multi-size icon.ico (16/24/32/48/64/128/256 px) is embedded in
every built .exe.  Regenerate from PNG masters in favicon_io/:

    python tools/image_to_ico.py favicon_io icon.ico


CLI
---
    start.py                       Run the desktop app
    start.py --build               Build all 5 EXE backends
    start.py --build --pyinstaller Build only the PyInstaller EXE
    start.py --build package       Build NSIS + Inno + WiX installers
    start.py --doctor              Diagnose missing deps / tools
    start.py --version             Print version + build metadata


--------------------------------------------------------------
(c) 2026 Trenton Tompkins.  Released under the MIT License.
