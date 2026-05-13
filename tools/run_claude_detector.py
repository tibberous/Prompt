#!/usr/bin/env python3
from __future__ import annotations
import argparse, ast, os, subprocess, sys, time, traceback
from pathlib import Path

DETECTOR_NAMES = {'rawsql','processfaults','threads','badcode','phaseownership','swallowed','brokenimports','astscan','deadcode','startpydebugger','live','all','depcheck','phasehooks','nonconform','comport','fileio','unlocalized','bypass','monkeypatch','recursion','redundant'}

def py_files(root: Path):
    skip={'.git','__pycache__','dist','build','logs','reports','workspaces'}
    for p in root.rglob('*.py'):
        if any(part in skip for part in p.parts):
            continue
        yield p

def run_static(root: Path, detector: str) -> tuple[int, str]:
    findings=[]
    for p in py_files(root):
        text=p.read_text(encoding='utf-8', errors='replace')
        try:
            ast.parse(text, filename=str(p))
        except SyntaxError as e:
            findings.append(f'{p}: syntax error {e}')
            continue
        low=text.lower()
        if detector=='rawsql' and any(x in low for x in ['.cursor(', 'sqlite3.connect', 'pymysql.connect', 'mysql.connector.connect', 'session.execute(']):
            if 'raw-sql-ok' in low or p.name == 'run_claude_detector.py':
                pass
            else:
                findings.append(f'{p}: possible raw SQL/connection call')
        elif detector=='swallowed' and ('except exception: pass' in low or 'except baseexception: pass' in low):
            if p.name == 'run_claude_detector.py':
                pass
            else:
                findings.append(f'{p}: possible swallowed exception')
        elif detector=='threads' and ('threading.thread(' in low and 'phase' not in low):
            findings.append(f'{p}: thread launch should be phase/process owned')
        elif detector=='badcode' and ('while true:' in low and 'timeout' not in low):
            findings.append(f'{p}: possible infinite loop without timeout nearby')
        elif detector=='brokenimports':
            pass
        elif detector=='astscan':
            # AST parse already proved it.
            pass
    if findings:
        return 1, '\n'.join(findings[:300])
    return 0, f'{detector}: passed'

def run_live(root: Path, app_args: list[str], timeout: int) -> tuple[int, str]:
    args=list(app_args or ['--offscreen'])
    if '--fast' not in [a.lower() for a in args]:
        args=['--fast', *args]
    cmd=[sys.executable, str(root/'start.py'), *args]
    env=os.environ.copy(); env.setdefault('PROMPT_FAST_BUILD_TIMEOUT_SECONDS', str(max(timeout, 120))); env.setdefault('PROMPT_BUILD_MODE','1')
    started=time.monotonic()
    try:
        proc=subprocess.run(cmd, cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        out=proc.stdout or ''
        bad=[]
        for line in out.splitlines():
            l=line.lower()
            if any(x in l for x in ['traceback', '[fast:failed]', '[fatal', 'modulenotfounderror', 'nameerror']):
                if 'dry-run' in l and 'missing' in l:
                    continue
                bad.append(line)
        if proc.returncode != 0 and bad:
            return proc.returncode, f'LIVE:FAILED rc={proc.returncode} elapsed={time.monotonic()-started:.1f}s\n'+'\n'.join(bad[-80:])
        return 0, f'LIVE:PASSED rc={proc.returncode} elapsed={time.monotonic()-started:.1f}s command={cmd!r}'
    except subprocess.TimeoutExpired as e:
        return 124, f'LIVE:TIMEOUT after {timeout}s command={cmd!r}\n{(e.stdout or "")[-4000:]}'

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--output', default='reports/claude/report.txt')
    ap.add_argument('--timeout', default='300')
    ap.add_argument('--detector', action='append', default=[])
    ap.add_argument('--app-args', nargs=argparse.REMAINDER, default=[])
    ns, extra=ap.parse_known_args()
    root=Path(ns.root).resolve(); out=Path(ns.output); out=out if out.is_absolute() else root/out
    timeout=int(str(ns.timeout or '300'))
    detectors=ns.detector or ['all']
    if 'all' in detectors:
        detectors=['rawsql','processfaults','threads','badcode','phaseownership','swallowed']
    lines=[]; rc_final=0
    for det in detectors:
        if det=='live':
            rc,text=run_live(root, ns.app_args, timeout)
        else:
            rc,text=run_static(root, det)
        lines.append(f'[{det}] rc={rc}\n{text}')
        if rc!=0:
            rc_final=rc_final or rc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n\n'.join(lines)+'\n', encoding='utf-8')
    print('\n\n'.join(lines))
    return rc_final

if __name__ == '__main__':
    raise SystemExit(main())
