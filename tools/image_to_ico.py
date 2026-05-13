"""image_to_ico.py — turn a PNG/JPG/source image into a real multi-size .ico.

Windows picks the best resolution at render time (16×16 in tiny tray slots,
256×256 on the desktop). A single-size ICO looks awful when scaled, so we
bake every common Windows icon size into ONE .ico file.

Usage:
    python tools/image_to_ico.py SOURCE OUT.ico
    python tools/image_to_ico.py favicon_io/android-chrome-512x512.png icon.ico

You can also pass a directory of PNGs and the tool picks the highest-resolution
PNG it finds as the source.

Requires: Pillow (PIL.Image.save with format='ICO' handles multi-size since 9.x).
"""
from __future__ import annotations
import sys
import argparse
from pathlib import Path


# Standard Windows .ico sizes. The Explorer/Taskbar/Add-Remove dialog all
# pick the closest match. 256 must come last in the ICO directory to avoid
# old XP-era loaders barfing.
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _resolveSourceImage(src: Path) -> Path:
    """If src is a directory, pick the highest-resolution PNG inside it."""
    if src.is_file():
        return src
    if not src.is_dir():
        raise FileNotFoundError(f"Source does not exist: {src}")
    candidates = sorted(
        [p for p in src.iterdir() if p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'}],
        key=lambda p: (p.stat().st_size, p.name),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No image files found in {src}")
    return candidates[0]


def imageToIco(src: Path, dst: Path, sizes: list[tuple[int, int]] | None = None) -> Path:
    """Convert `src` (file or dir) into a multi-size .ico at `dst`. Returns dst."""
    try:
        from PIL import Image
    except ImportError as e:
        raise SystemExit(
            "Pillow is not installed. Install it with: pip install Pillow\n"
            f"Underlying error: {e}"
        )
    actualSrc = _resolveSourceImage(src)
    sizes = sizes or ICO_SIZES
    img = Image.open(actualSrc)
    if img.mode not in ('RGBA', 'LA'):
        img = img.convert('RGBA')
    dst = Path(dst).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, format='ICO', sizes=sizes)
    return dst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Convert an image (or folder of images) to a multi-size .ico")
    p.add_argument('src', help='Source image file OR directory of images (highest-res wins)')
    p.add_argument('dst', help='Output .ico path')
    p.add_argument('--sizes', default=None,
                   help='Comma-separated list of side lengths, e.g. "16,32,48,256". '
                        'Default: 16,24,32,48,64,128,256')
    args = p.parse_args(argv)
    sizes = None
    if args.sizes:
        try:
            sizes = [(int(s), int(s)) for s in args.sizes.split(',') if s.strip()]
        except ValueError:
            p.error(f"--sizes must be comma-separated integers, got: {args.sizes}")
    out = imageToIco(Path(args.src), Path(args.dst), sizes=sizes)
    print(f"Wrote {out} ({out.stat().st_size:,} bytes, sizes={[s[0] for s in (sizes or ICO_SIZES)]})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
