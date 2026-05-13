#!/usr/bin/env python3
from __future__ import annotations
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ['auto.ps1','auto_deploy.ps1','auto_run.ps1','auto_build_exes.ps1','auto_build_installers.ps1']
BAD_VAR_COLON = re.compile(r'"[^"]*\$(?!script:|env:|global:|local:|private:|using:)[A-Za-z_][A-Za-z0-9_]*:')

def main() -> int:
    failures=[]
    for name in SCRIPTS:
        path=ROOT/name
        if not path.exists():
            failures.append(f'{name}: missing')
            continue
        text=path.read_text(encoding='utf-8', errors='replace')
        if 'DataReceivedEventHandler' in text:
            failures.append(f'{name}: uses PowerShell DataReceivedEventHandler runspace-crash path')
        if BAD_VAR_COLON.search(text):
            failures.append(f'{name}: unsafe $var: interpolation remains')
        if 'Expand-Archive' not in text and 'ExtractToDirectory' not in text:
            failures.append(f'{name}: no zip extraction path found')
        if 'Remove-Item $ArchiveFile.FullName' not in text and 'Deleted archive' not in text:
            failures.append(f'{name}: no archive delete path found')
        if 'start.py' not in text:
            failures.append(f'{name}: does not launch root start.py')
        if name == 'auto_build_exes.ps1':
            for token in ('--build','--offscreen','--force-rebuild'):
                if token not in text:
                    failures.append(f'{name}: missing launch arg {token}')
        if name == 'auto_build_installers.ps1':
            for token in ('--build','package','--offscreen','--force-rebuild'):
                if token not in text:
                    failures.append(f'{name}: missing launch arg {token}')
        if 'CreateNoWindow = $true' not in text:
            failures.append(f'{name}: helper launch is not marked CreateNoWindow')
        for required in ('Ensure-PromptSourcesAvailable', 'Test-ZipLooksLikePromptPayload', 'Get-NewestPromptArchive'):
            if required not in text:
                failures.append(f'{name}: missing self-contained zip bootstrap helper {required}')
        if 'Ensure-PromptSourcesAvailable -ThrowOnFailure | Out-Null' not in text:
            failures.append(f'{name}: does not bootstrap/extract before preflight/launch')
        for required_path in ('frozen_prompt_entry.py', 'prompt_app.py', 'tools/run_prompt_release.py'):
            if required_path not in text.replace('\\','/'):
                failures.append(f'{name}: required Prompt payload check missing {required_path}')
    if failures:
        print('AUTO_DEPLOYER_CHECK:FAILED')
        for f in failures:
            print(' - '+f)
        return 1
    print('AUTO_DEPLOYER_CHECK:PASSED scripts=' + ','.join(SCRIPTS))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
