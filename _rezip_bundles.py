import zipfile, os
from pathlib import Path

dist = Path('C:/prompt/dist')
targets = [
    ('Prompt-PyInstallerDir', 'Prompt-PyInstallerDir-bundle.zip'),
    ('Prompt-cx_Freeze', 'Prompt-cx_Freeze-bundle.zip'),
]
for backend_dir, zip_name in targets:
    src = dist / backend_dir
    zip_path = dist / zip_name
    if not src.exists():
        print(f'SKIP: {src} not present')
        continue
    if zip_path.exists():
        print(f'EXISTS: {zip_path} ({zip_path.stat().st_size} bytes) — leaving in place')
        continue
    print(f'Zipping {src} -> {zip_path}')
    n = 0
    bytes_total = 0
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(src):
            for f in files:
                full = Path(root) / f
                rel = backend_dir + '/' + str(full.relative_to(src)).replace('\\', '/')
                zf.write(full, rel)
                n += 1
                bytes_total += full.stat().st_size
    print(f'  wrote {n} files, raw={bytes_total/1024/1024:.1f}MB, zip={zip_path.stat().st_size/1024/1024:.1f}MB')
print('done')
