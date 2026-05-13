# Prompt

Desktop prompt, workflow, and doctype engine.

**Author:** Trenton Tompkins · <trenttompkins@gmail.com> · (724) 431-5207
**Website:** <https://trentontompkins.com>
**GitHub:** <https://github.com/tibberous>
**License:** MIT — see [LICENSE](LICENSE).

---

## What it is

Prompt is a self-contained Python+Qt6 desktop application for managing LLM
prompts, multi-step workflows, and document templates (doctypes). It ships as
a stand-alone Windows executable, with no Python install required on the
target machine.

## Quick start (from source)

```powershell
# Run directly
python start.py

# One-shot build of all 5 executable backends
.\auto_build_exes.ps1

# Build all installers (NSIS / Inno / WiX) from a tested executable
.\auto_build_installers.ps1

# Full pipeline (deploy artifacts to release_upload/)
.\auto_deploy.ps1
```

## Build pipeline

Prompt is packaged through **five** executable backends so the right binary
exists for every flavor of target system:

| Backend | Output | Notes |
|---|---|---|
| **PyInstaller (onefile)** | `dist/Prompt-PyInstaller.exe` | Single executable, easy to ship |
| **PyInstaller (onedir)** | `dist/Prompt-PyInstallerDir-bundle.zip` | Faster startup, exposes `_internal/` |
| **Nuitka** | `dist/Prompt-Nuitka.exe` | Compiled C; smaller, faster startup |
| **cx_Freeze** | `dist/Prompt-cx_Freeze-bundle.zip` | Stable across Python versions |
| **PyApp** | `dist/Prompt-PyApp.exe` | Rust launcher, downloads Python on demand |

Each is wrapped in installers via `auto_build_installers.ps1`:

| Installer | Tool | Output |
|---|---|---|
| **NSIS** | `makensis.exe` | `PromptSetup-<backend>-NSIS.exe` |
| **Inno Setup** | `iscc.exe` | `PromptSetup-<backend>-Inno.exe` |
| **WiX (MSI)** | `wix.exe` | `PromptSetup-<backend>-WiX.msi` |

## Configuration

| File | Purpose | Pushed? |
|---|---|---|
| `config.ini` | Live API keys + local paths | **NO** — gitignored |
| `config.sample.ini` | Push-safe template | yes |
| `build_info.ini` | EXE + installer metadata (author, copyright, URL, phone, license, ...) | yes |

Edit `build_info.ini` to change every string baked into the
built executables and installers — see comments inside the file. Re-run the
build to apply.

## Icon

A single multi-size `icon.ico` (16 / 24 / 32 / 48 / 64 / 128 / 256 px) is
embedded in every built `.exe`. Regenerate it from the PNG masters in
`favicon_io/`:

```powershell
python tools/image_to_ico.py favicon_io icon.ico
```

## CLI

```text
start.py                       Run the desktop app
start.py --build               Build all 5 EXE backends, no installers
start.py --build --pyinstaller Build only the PyInstaller EXE
start.py --build package       Build NSIS + Inno + WiX installers
start.py --doctor              Diagnose missing deps / tools
start.py --version             Print version + build metadata
```

## Phone

If you need to discuss a custom build or commercial license:
**(724) 431-5207**.

---

(c) 2026 Trenton Tompkins. Released under the MIT License.
