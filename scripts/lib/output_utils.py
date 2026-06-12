#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded output helpers for ssh-skill."""

from __future__ import annotations

import subprocess
import threading
import time

DEFAULT_OUTPUT_LIMIT_BYTES = 1024 * 1024
READ_CHUNK_SIZE = 65536


def _capture_chunk(captured: bytearray, chunk: bytes, limit_bytes: int) -> int:
    total = len(chunk)
    remaining = limit_bytes - len(captured)
    if remaining > 0:
        captured.extend(chunk[:remaining])
    return total


def read_bounded_stream(stream, limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES, encoding: str = 'utf-8'):
    captured = bytearray()
    total_bytes = 0

    while True:
        chunk = stream.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        total_bytes += _capture_chunk(captured, chunk, limit_bytes)

    return {
        'text': bytes(captured).decode(encoding, errors='replace'),
        'truncated': total_bytes > len(captured),
        'captured_bytes': len(captured),
        'total_bytes': total_bytes,
        'output_limit_bytes': limit_bytes,
    }


def read_bounded_channel(channel, limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES, encoding: str = 'utf-8'):
    stdout = bytearray()
    stderr = bytearray()
    stdout_total = 0
    stderr_total = 0

    while True:
        progressed = False

        while channel.recv_ready():
            chunk = channel.recv(READ_CHUNK_SIZE)
            if not chunk:
                break
            stdout_total += _capture_chunk(stdout, chunk, limit_bytes)
            progressed = True

        while channel.recv_stderr_ready():
            chunk = channel.recv_stderr(READ_CHUNK_SIZE)
            if not chunk:
                break
            stderr_total += _capture_chunk(stderr, chunk, limit_bytes)
            progressed = True

        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break

        if not progressed:
            time.sleep(0.01)

    exit_code = channel.recv_exit_status()
    return {
        'stdout': bytes(stdout).decode(encoding, errors='replace'),
        'stderr': bytes(stderr).decode(encoding, errors='replace'),
        'stdout_truncated': stdout_total > len(stdout),
        'stderr_truncated': stderr_total > len(stderr),
        'stdout_bytes': stdout_total,
        'stderr_bytes': stderr_total,
        'output_limit_bytes': limit_bytes,
        'exit_code': exit_code,
    }


def run_bounded_process(command, timeout: int, limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES, encoding: str = 'utf-8'):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()
    totals = {'stdout': 0, 'stderr': 0}

    def drain(stream, target: bytearray, key: str):
        while True:
            chunk = stream.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            totals[key] += _capture_chunk(target, chunk, limit_bytes)

    threads = []
    for stream, target, key in ((process.stdout, stdout, 'stdout'), (process.stderr, stderr, 'stderr')):
        if stream is None:
            continue
        thread = threading.Thread(target=drain, args=(stream, target, key), daemon=True)
        thread.start()
        threads.append(thread)

    timed_out = False
    try:
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            exit_code = -1
            process.wait()

        for thread in threads:
            thread.join(timeout=1)

        stderr_text = bytes(stderr).decode(encoding, errors='replace')
        if timed_out:
            if stderr_text:
                stderr_text += '\n'
            stderr_text += f'Command timed out after {timeout} seconds'

        return {
            'stdout': bytes(stdout).decode(encoding, errors='replace'),
            'stderr': stderr_text,
            'stdout_truncated': totals['stdout'] > len(stdout),
            'stderr_truncated': totals['stderr'] > len(stderr),
            'stdout_bytes': totals['stdout'],
            'stderr_bytes': totals['stderr'],
            'output_limit_bytes': limit_bytes,
            'exit_code': exit_code,
            'timed_out': timed_out,
        }
    finally:
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
