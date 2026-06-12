#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified execution result contract for ssh-skill."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional


@dataclass
class ExecutionResult:
    success: bool
    exit_code: int
    stdout: str = ''
    stderr: str = ''
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    output_limit_bytes: int = 0
    error_type: str = ''
    error_message: str = ''
    connector: str = ''
    method: str = ''

    def to_dict(self) -> Dict:
        data = asdict(self)
        if not data['method'] and data['connector']:
            data['method'] = data['connector']
        return data


@dataclass(frozen=True)
class ConnectorCapabilities:
    stream_stdout: bool = True
    stream_stderr: bool = True
    separate_stderr: bool = True
    exit_code: bool = True
    stdin_script: bool = True
    persistent: bool = False


def execution_result_from_dict(data: Dict, connector: Optional[str] = None) -> ExecutionResult:
    return ExecutionResult(
        success=bool(data.get('success')),
        exit_code=data.get('exit_code', -1),
        stdout=data.get('stdout', '') or '',
        stderr=data.get('stderr', '') or '',
        stdout_truncated=bool(data.get('stdout_truncated', False)),
        stderr_truncated=bool(data.get('stderr_truncated', False)),
        stdout_bytes=int(data.get('stdout_bytes') or len((data.get('stdout') or '').encode('utf-8', errors='replace'))),
        stderr_bytes=int(data.get('stderr_bytes') or len((data.get('stderr') or '').encode('utf-8', errors='replace'))),
        output_limit_bytes=int(data.get('output_limit_bytes') or 0),
        error_type=data.get('error_type', '') or '',
        error_message=data.get('error_message', '') or '',
        connector=connector or data.get('connector') or data.get('mode') or data.get('method') or '',
        method=data.get('method', '') or '',
    )
