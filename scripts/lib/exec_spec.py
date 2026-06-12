#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared execution spec helpers for ssh-skill."""

from __future__ import annotations

import shlex
from typing import Dict, Optional

PROTOCOL_VERSION = 2
SCRIPT_MARKER = '__CLAUDE_SSH_SKILL_EOF__'


def quote_cmd_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def quote_powershell_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_inline_exec_spec(command: str, cwd: Optional[str] = None, shell: Optional[str] = None) -> Dict:
    return {
        'protocol_version': PROTOCOL_VERSION,
        'transport': 'inline',
        'command': command,
        'cwd': cwd,
        'shell': shell,
    }


def build_script_exec_spec(script: str, cwd: Optional[str] = None, shell: Optional[str] = None) -> Dict:
    return {
        'protocol_version': PROTOCOL_VERSION,
        'transport': 'stdin-script',
        'script': script,
        'cwd': cwd,
        'shell': shell,
    }


def render_exec_spec(exec_spec: Dict) -> str:
    transport = exec_spec.get('transport', 'inline')
    shell = exec_spec.get('shell')
    cwd = exec_spec.get('cwd')

    if transport == 'inline':
        return _build_runtime_command(exec_spec.get('command', ''), cwd=cwd, shell=shell)
    if transport == 'stdin-script':
        return _build_script_runtime_command(exec_spec.get('script', ''), cwd=cwd, shell=shell)
    raise ValueError(f"Unsupported transport: {transport}")


def _build_runtime_command(command: str, cwd: Optional[str] = None, shell: Optional[str] = None) -> str:
    runtime_command = command

    if shell in (None, 'sh', 'bash'):
        if cwd:
            runtime_command = f"cd {shlex.quote(cwd)} && {runtime_command}"
        if shell == 'bash':
            return f"bash -lc {shlex.quote(runtime_command)}"
        if shell == 'sh':
            return f"sh -c {shlex.quote(runtime_command)}"
        return runtime_command

    if shell == 'cmd':
        if cwd:
            runtime_command = f'cd /d {quote_cmd_string(cwd)} && {runtime_command}'
        return f'cmd /c {quote_cmd_string(runtime_command)}'

    if shell == 'powershell':
        if cwd:
            runtime_command = f"Set-Location -LiteralPath {quote_powershell_string(cwd)}; {runtime_command}"
        return f"powershell -NoProfile -Command {shlex.quote(runtime_command)}"

    raise ValueError(f'Unsupported shell: {shell}')


def _build_script_runtime_command(script: str, cwd: Optional[str] = None, shell: Optional[str] = None) -> str:
    body = script.rstrip('\n')

    if shell in (None, 'sh', 'bash'):
        runner = 'sh' if shell in (None, 'sh') else 'bash'
        prefix = f"cd {shlex.quote(cwd)} && " if cwd else ''
        return (
            f"{prefix}{runner} <<'{SCRIPT_MARKER}'\n"
            f"{body}\n"
            f"{SCRIPT_MARKER}"
        )

    if shell == 'cmd':
        prefix = f'cd /d {quote_cmd_string(cwd)} && ' if cwd else ''
        return (
            f'{prefix}powershell -NoProfile -Command '
            f'"$script = @\'\n{body}\n\'@; '
            f'$tmp = [System.IO.Path]::GetTempFileName() + ".cmd"; '
            f'Set-Content -LiteralPath $tmp -Value $script -Encoding ASCII; '
            f'cmd /c $tmp; '
            f'$code = $LASTEXITCODE; '
            f'Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue; '
            f'exit $code"'
        )

    if shell == 'powershell':
        set_location = f"Set-Location -LiteralPath {quote_powershell_string(cwd)}; " if cwd else ''
        return (
            f"powershell -NoProfile -Command \"{set_location}"
            f"$script = @'\n{body}\n'@; "
            f"$tmp = [System.IO.Path]::GetTempFileName() + '.ps1'; "
            f"Set-Content -LiteralPath $tmp -Value $script -Encoding UTF8; "
            f"& $tmp; $code = $LASTEXITCODE; "
            f"Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue; "
            f"exit $code\""
        )

    raise ValueError(f'Unsupported shell: {shell}')
