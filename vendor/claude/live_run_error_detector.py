#!/usr/bin/env python3
from __future__ import annotations
import subprocess, sys, os, argparse
from pathlib import Path

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--repo-root','--root', default='.')
    ap.add_argument('--timeout', type=int, default=300)
    ap.add_argument('args', nargs='*')
    ns=ap.parse_args()
    root=Path(ns.repo_root).resolve()
    app_args=ns.args or ['--build','--offscreen','--dry-run','--force-rebuild']
    if '--fast' not in [x.lower() for x in app_args]:
        app_args=['--fast', *app_args]
    cmd=[sys.executable, str(root/'start.py'), *app_args]
    env=os.environ.copy(); env.setdefault('PROMPT_BUILD_MODE','1')
    p=subprocess.run(cmd, cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', timeout=ns.timeout)
    print(p.stdout)
    bad=[line for line in (p.stdout or '').splitlines() if any(x in line.lower() for x in ['traceback','[fast:failed]','modulenotfounderror','nameerror','[fatal'])]
    if p.returncode != 0 and bad:
        print('LIVE_RUN_ERROR_DETECTOR:FAILED')
        print('\n'.join(bad[-80:]))
        return p.returncode or 1
    print('LIVE_RUN_ERROR_DETECTOR:PASSED')
    return 0
if __name__=='__main__':
    raise SystemExit(main())
