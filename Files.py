"""
Files.py — Collection class for batch file operations.

Wraps list[File] with chainable batch methods: zip, tar, filter, map,
totalSize, allExist, deleteAll, copyAllTo, moveAllTo.

Usage:
    from File import File
    from Files import Files

    bundle = Files([
        File('C:/project/main.py'),
        File('C:/project/data.py'),
        File('C:/project/README.md'),
    ])

    bundle.zip('C:/Desktop/bundle.zip', compression=5)
    bundle.tar('C:/Desktop/bundle.tar.gz', mode='gz')

    # Filter to only existing files then copy
    bundle.filter(lambda f: f.exists).copyAllTo('C:/backup/')

    # Map to get all sizes
    sizes = bundle.map(lambda f: f.size)

    print(bundle.totalSize())   # bytes across all files
    print(bundle.allExist())    # True if every file exists

Glob constructor:
    bundle = Files.glob('C:/project', '**/*.py')

Directory constructor:
    bundle = Files.fromDir('C:/project/src', recursive=True)
"""

from __future__ import annotations

import os
import sys
import zipfile
import tarfile
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    from File import File  # standalone usage
except ImportError:
    from data import File  # type: ignore  # inside PyEncoder project

EMPTY_STRING = ''

try:
    from data import recordException as _recordException  # type: ignore
except ImportError:
    def _recordException(context: str, error=None, *, handled: bool = True, source: str = 'Files.py') -> int:  # type: ignore
        print(f'[EXCEPTION:{context}] {type(error).__name__}: {error}', file=sys.stderr)
        return 0


def recordException(context: str, error=None) -> int:
    return _recordException(context, error, source='Files.py')


def _run_process(command, *, timeout: float, capture_output: bool = False, text: bool = False, check: bool = False, phase_name: str = 'Files.process'):
    kwargs = {'timeout': timeout, 'check': check}
    if capture_output:
        kwargs['capture_output'] = True
    if text:
        kwargs['text'] = True
    if _PhaseProcess is not None:
        return _PhaseProcess.run(command, phase_name=phase_name, **kwargs)
    return subprocess.run(command, **kwargs)  # lifecycle-bypass-ok phase-ownership-ok: final fallback when PhaseProcess is unavailable


class Files:
    """
    A collection of File objects with batch operations.

    Constructors:
        Files([File(...), File(...)])        from a list
        Files.glob(root, pattern)           from a glob pattern
        Files.fromDir(directory, recursive) from a directory

    All batch methods return self or a new Files so calls can be chained:
        Files.glob('.', '**/*.py').filter(lambda f: f.size > 0).zip('out.zip')
    """

    def __init__(self, files: list[File] | None = None) -> None:
        self._files: list[File] = []
        self.extend(files or [])

    # ------------------------------------------------------------------
    # Alternate constructors
    # ------------------------------------------------------------------

    @classmethod
    def glob(cls, root: str | Path, pattern: str) -> 'Files':
        """Build a Files collection from a glob pattern under *root*."""
        base = Path(root)
        return cls([File(p) for p in sorted(base.glob(pattern)) if p.is_file()])

    @classmethod
    def fromDir(cls, directory: str | Path, *, recursive: bool = False, pattern: str = '*') -> 'Files':  # noqa: N802
        """
        Build a Files collection from all files in a directory.
        recursive=True uses rglob instead of glob.
        """
        base = Path(directory)
        method = base.rglob if recursive else base.glob
        return cls([File(p) for p in sorted(method(pattern)) if p.is_file()])

    # ------------------------------------------------------------------
    # Collection interface
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._files)

    def __iter__(self) -> Iterator[File]:
        return iter(self._files)

    def __getitem__(self, index: int) -> File:
        return self._files[index]

    def __repr__(self) -> str:
        return f'Files([{", ".join(repr(f) for f in self._files)}])'

    def append(self, file: File | str | Path) -> 'Files':
        self._files.append(file if isinstance(file, File) else File(file))
        return self

    def extend(self, files) -> 'Files':
        for file in list(files or []):
            self.append(file)
        return self

    def toList(self) -> list[File]:  # noqa: N802
        return list(self._files)

    def toPaths(self) -> list[Path]:  # noqa: N802
        return [f.path for f in self._files]

    # ------------------------------------------------------------------
    # Filtering / mapping
    # ------------------------------------------------------------------

    def filter(self, predicate: Callable[[File], bool]) -> 'Files':
        """Return a new Files containing only files where predicate(file) is True."""
        return Files([f for f in self._files if predicate(f)])

    def map(self, fn: Callable[[File], Any]) -> list[Any]:
        """Apply fn to each file and return a plain list of results."""
        return [fn(f) for f in self._files]

    def existing(self) -> 'Files':
        """Convenience filter: return only files that exist on disk."""
        return self.filter(lambda f: f.exists)

    def withSuffix(self, *suffixes: str) -> 'Files':  # noqa: N802
        """Return only files matching one or more suffixes, e.g. '.py'."""
        wanted = {
            (str(s or '').lower() if str(s or '').startswith('.') else f'.{str(s or '').lower()}')
            for s in suffixes
            if str(s or '').strip()
        }
        return self.filter(lambda f: f.suffix in wanted)

    # ------------------------------------------------------------------
    # Aggregate stats
    # ------------------------------------------------------------------

    def totalSize(self) -> int:  # noqa: N802
        """Total size in bytes across all files."""
        return sum(f.size for f in self._files)

    def allExist(self) -> bool:  # noqa: N802
        """True if every file in the collection exists on disk."""
        return all(f.exists for f in self._files)

    def anyExist(self) -> bool:  # noqa: N802
        """True if at least one file exists."""
        return any(f.exists for f in self._files)

    def missing(self) -> 'Files':
        """Return files that do NOT exist on disk."""
        return self.filter(lambda f: not f.exists)

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    def deleteAll(self) -> int:  # noqa: N802
        """Delete all files. Returns count of successfully deleted files."""
        return sum(1 for f in self._files if f.delete())

    def copyAllTo(self, directory: str | Path) -> int:  # noqa: N802
        """
        Copy all files into *directory*, preserving only the filename (not subdirs).
        Returns count of successfully copied files.
        """
        dest = Path(directory)
        dest.mkdir(parents=True, exist_ok=True)
        return sum(1 for f in self._files if f.copyTo(dest / f.name))

    def moveAllTo(self, directory: str | Path) -> int:  # noqa: N802
        """Move all files into *directory*. Returns count of successful moves."""
        dest = Path(directory)
        dest.mkdir(parents=True, exist_ok=True)
        return sum(1 for f in self._files if f.moveTo(dest / f.name))

    def touchAll(self) -> int:  # noqa: N802
        """Set mtime to now for every file. Returns count of successes."""
        return sum(1 for f in self._files if f.setMtime())

    # ------------------------------------------------------------------
    # Archive: zip
    # ------------------------------------------------------------------

    def zip(
        self,
        dest: str | Path,
        *,
        compression: int = 5,
        flatten: bool = True,
        root: str | Path | None = None,
    ) -> bool:
        """
        Zip all files into *dest*.

        compression: 0-9 (0=store, 9=maximum). Default 5.
        flatten=True:  all files go into the zip root with their filename only.
        flatten=False: preserve relative paths from *root* (or cwd if root=None).
        root:          base path for relative-path preservation when flatten=False.

        Returns True on success.

        Example:
            Files([File('a.py'), File('b.py')]).zip('C:/Desktop/bundle.zip', compression=6)
        """
        try:
            destination = Path(dest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            base = Path(root) if root else Path.cwd()

            compress_type = zipfile.ZIP_DEFLATED if compression > 0 else zipfile.ZIP_STORED
            level = max(0, min(9, int(compression)))

            with zipfile.ZipFile(  # file-io-ok: canonical Files archive surface
                destination,
                mode='w',
                compression=compress_type,
                compresslevel=level,
            ) as zf:
                for f in self._files:
                    if not f.path.exists():
                        print(f'[WARN:Files.zip] skipping missing file: {f.path}')
                        continue
                    if flatten:
                        arcname = f.name
                    else:
                        try:
                            arcname = str(f.path.relative_to(base))
                        except ValueError:
                            arcname = f.name
                    zf.write(str(f.path), arcname=arcname)  # file-io-ok: canonical Files archive surface
            return True
        except Exception as exc:
            recordException('Files.zip', exc)
            return False

    def zipWithStructure(self, dest: str | Path, root: str | Path, *, compression: int = 5) -> bool:  # noqa: N802
        """Convenience: zip preserving relative paths from *root*."""
        return self.zip(dest, compression=compression, flatten=False, root=root)

    # ------------------------------------------------------------------
    # Archive: tar
    # ------------------------------------------------------------------

    def tar(
        self,
        dest: str | Path,
        *,
        mode: str = 'gz',
        flatten: bool = True,
        root: str | Path | None = None,
    ) -> bool:
        """
        Create a tar archive at *dest*.

        mode: 'gz' (gzip, default), 'bz2' (bzip2), 'xz', or '' (uncompressed).
        flatten / root: same semantics as .zip().

        Returns True on success.

        Example:
            Files.glob('.', '**/*.py').tar('C:/Desktop/src.tar.gz', mode='gz')
        """
        try:
            destination = Path(dest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            base = Path(root) if root else Path.cwd()

            tar_mode = f'w:{mode}' if mode else 'w'

            with tarfile.open(str(destination), tar_mode) as tf:  # file-io-ok: canonical Files archive surface
                for f in self._files:
                    if not f.path.exists():
                        print(f'[WARN:Files.tar] skipping missing file: {f.path}')
                        continue
                    if flatten:
                        arcname = f.name
                    else:
                        try:
                            arcname = str(f.path.relative_to(base))
                        except ValueError:
                            arcname = f.name
                    tf.add(str(f.path), arcname=arcname)
            return True
        except Exception as exc:
            recordException('Files.tar', exc)
            return False

    # ------------------------------------------------------------------
    # Archive: rar (optional — requires rarfile + WinRAR binary)
    # ------------------------------------------------------------------

    def rar(self, dest: str | Path, *, winrar_path: str | None = None) -> bool:
        """
        Create a RAR archive. Requires WinRAR installed and optionally the
        rarfile package (pip install rarfile).

        winrar_path: explicit path to WinRAR.exe. If None, looks in PATH and
        common Windows install locations.

        Returns True on success, False if WinRAR is not available.

        Note: prefer .zip() or .tar() for portable/CI use. .rar() is
        Windows-only and requires a licensed WinRAR binary.
        """
        import subprocess

        rar_exe = winrar_path
        if not rar_exe:
            candidates = [
                r'C:\Program Files\WinRAR\WinRAR.exe',
                r'C:\Program Files (x86)\WinRAR\WinRAR.exe',
                'rar',
                'WinRAR',
            ]
            for candidate in candidates:
                try:
                    result = _run_process([candidate, '--version'], capture_output=True, timeout=3, phase_name='Files.rar.probe')
                    if result.returncode in (0, 1):
                        rar_exe = candidate
                        break
                except (FileNotFoundError, OSError) as exc:
                    recordException('Files.rar.probe', exc)
                    continue

        if not rar_exe:
            print('[WARN:Files.rar] WinRAR not found. Install WinRAR or use .zip()/.tar() instead.')
            return False

        try:
            destination = Path(dest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination.unlink()

            args = [rar_exe, 'a', str(destination)]
            args += [str(f.path) for f in self._files if f.path.exists()]
            result = _run_process(args, capture_output=True, text=True, timeout=120, phase_name='Files.rar.build')
            if result.returncode not in (0, 1):
                recordException('Files.rar', RuntimeError(f'WinRAR exited {result.returncode}: {result.stderr.strip()}'))
                return False
            return True
        except Exception as exc:
            recordException('Files.rar', exc)
            return False

    # ------------------------------------------------------------------
    # Checksum manifest
    # ------------------------------------------------------------------

    def md5Manifest(self) -> dict[str, str]:  # noqa: N802
        """Return {filename: md5hex} for all existing files."""
        return {f.name: f.md5Hex() for f in self._files if f.exists}

    def sha1Manifest(self) -> dict[str, str]:  # noqa: N802
        """Return {filename: sha1hex} for all existing files."""
        return {f.name: f.sha1Hex() for f in self._files if f.exists}
