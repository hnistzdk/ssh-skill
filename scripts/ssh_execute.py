#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH命令执行CLI工具 v3.0

支持通过别名执行SSH命令，从标准 SSH config 和注释元数据中加载配置。
自动检测守护进程：有则走长连接，无则走直连。

用法：
    python ssh_execute.py <alias> <command> [--timeout TIMEOUT]
    python ssh_execute.py <alias> <command> --no-daemon

示例：
    python ssh_execute.py prod-web-01 "whoami && hostname"
    python ssh_execute.py DEV-002 "df -h" --timeout 60
"""

import sys
import os
import json
import socket
import struct
import argparse
import subprocess
import time
import shlex

# 添加lib到路径
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_script_dir, 'lib'))

from path_normalization import normalize_remote_path, PathNormalizationError
from reporting import add_reporting_arguments, emit_json


def _build_error(code, message, details=None, cause=None, retriable=False):
    return {
        'code': code,
        'message': message,
        'details': details or {},
        'cause': cause,
        'retriable': retriable,
    }


def _build_failure(alias, code, message, details=None, cause=None, retriable=False, mode=None):
    return {
        'schema_version': '1.0',
        'success': False,
        'operation': 'execute',
        'target': alias,
        'mode': mode,
        'error': _build_error(code, message, details=details, cause=cause, retriable=retriable),
    }


def _is_typed_error(value):
    return isinstance(value, dict) and {'code', 'message', 'details', 'cause', 'retriable'}.issubset(value.keys())


def _truncate_text(text, limit=1000):
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + f'\n...<truncated {len(text) - limit} chars>'


def _normalize_runtime_path(path, role, shell=None):
    if path is None:
        return None
    normalized = path.strip()
    if not normalized:
        raise ValueError(f'{role} cannot be empty')
    if shell in ('cmd', 'powershell'):
        return normalized
    return normalize_remote_path(normalized, role=role)


def _quote_cmd_string(value):
    return '"' + value.replace('"', '""') + '"'


def _quote_powershell_string(value):
    return "'" + value.replace("'", "''") + "'"


def _build_runtime_command(command, cwd=None, shell=None):
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
            runtime_command = f'cd /d {_quote_cmd_string(cwd)} && {runtime_command}'
        return f'cmd /c {_quote_cmd_string(runtime_command)}'

    if shell == 'powershell':
        if cwd:
            runtime_command = f"Set-Location -LiteralPath {_quote_powershell_string(cwd)}; {runtime_command}"
        return f"powershell -NoProfile -Command {shlex.quote(runtime_command)}"

    raise ValueError(f'Unsupported shell: {shell}')


def _send_message(sock, data):
    """发送带长度前缀的 JSON 消息"""
    payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
    header = struct.pack('!I', len(payload))
    sock.sendall(header + payload)


def _recv_message(sock, timeout=None):
    """接收带长度前缀的 JSON 消息"""
    if timeout:
        sock.settimeout(timeout)

    header = b''
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("连接已关闭")
        header += chunk

    length = struct.unpack('!I', header)[0]
    if length > 10 * 1024 * 1024:
        raise ValueError(f"消息过大: {length} bytes")

    body = b''
    while len(body) < length:
        chunk = sock.recv(min(65536, length - len(body)))
        if not chunk:
            raise ConnectionError("连接已关闭")
        body += chunk

    return json.loads(body.decode('utf-8'))


def try_daemon_execute(alias, command, timeout):
    """尝试通过守护进程执行命令，返回 None 表示守护进程不可用"""
    from ssh_daemon import read_daemon_info

    info = read_daemon_info(alias)
    if not info:
        return None

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout + 5)
        sock.connect(('127.0.0.1', info['port']))
        _send_message(sock, {
            'action': 'execute',
            'command': command,
            'timeout': timeout
        })
        result = _recv_message(sock, timeout=timeout + 5)
        sock.close()
        if isinstance(result, dict):
            result.setdefault('mode', 'daemon')
            result.setdefault('method', 'daemon')
        return result
    except Exception:
        return None


def start_daemon_background(alias):
    """后台启动守护进程"""
    daemon_script = os.path.join(_script_dir, 'ssh_daemon.py')
    try:
        if os.name == 'nt':
            # Windows: 使用 CREATE_NO_WINDOW
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                [sys.executable, daemon_script, 'start', alias],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW
            )
        else:
            subprocess.Popen(
                [sys.executable, daemon_script, 'start', alias],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        # 等待守护进程启动
        for _ in range(10):
            time.sleep(0.3)
            from ssh_daemon import read_daemon_info
            if read_daemon_info(alias):
                return True
        return False
    except Exception:
        return False


def direct_execute(alias, command, timeout, cwd=None, shell=None, quiet=False):
    """直连执行命令（智能选择客户端类型，支持降级到原生 SSH）"""
    from config_v3 import SSHConfigLoaderV3
    from native_ssh_fallback import should_use_native_ssh, execute_native_ssh, check_ssh_agent

    loader = SSHConfigLoaderV3()

    # 加载 SSH 配置
    ssh_config = loader.load_ssh_config(alias)
    metadata = {}
    try:
        metadata = loader.load_metadata(alias)
    except Exception:
        pass

    params = loader.get_connection_params(alias)
    runtime_command = _build_runtime_command(command, cwd=cwd, shell=shell)

    # 检测是否应该降级到原生 SSH
    should_fallback, reason = should_use_native_ssh(ssh_config, metadata)

    if should_fallback:
        # 检查 ssh-agent 状态（如果涉及密钥认证）
        agent_available, agent_msg = check_ssh_agent()

        # 如果原因包含 passphrase 且 ssh-agent 不可用，给出提示但仍然尝试
        if 'passphrase' in reason.lower() and not agent_available and not quiet:
            print(f"\n⚠️  警告：检测到需要 passphrase 的密钥，但 ssh-agent 未配置", file=sys.stderr)
            print(f"ssh-agent 状态: {agent_msg}", file=sys.stderr)
            print(f"\n建议配置 ssh-agent 以避免每次输入密码：", file=sys.stderr)
            print(f"1. 启动 ssh-agent: eval $(ssh-agent)", file=sys.stderr)
            print(f"2. 添加密钥: ssh-add ~/.ssh/your_key", file=sys.stderr)
            print(f"\n现在将使用原生 SSH（需要交互式输入 passphrase）...\n", file=sys.stderr)

        result = execute_native_ssh(alias, runtime_command, timeout)
        result['fallback_reason'] = reason
        result['mode'] = 'native'
        return result

    # 使用智能选择：密钥认证 → NativeSSHClient，密码认证 → ParamikoClient
    client = loader.from_alias(alias)

    # 设置超时
    client.timeout = timeout

    result = client.execute(runtime_command)
    mode = 'paramiko' if params.get('password') is not None else 'native'
    return {
        'success': result.success,
        'exit_code': result.exit_code,
        'stdout': result.stdout,
        'stderr': result.stderr,
        'method': mode,
        'mode': mode,
    }


def execute_command(alias, command, timeout, no_daemon=False, cwd=None, shell=None, quiet=False):
    """统一执行入口，供主命令和 verification 复用。"""
    from config_v3 import SSHConfigLoaderV3

    loader = SSHConfigLoaderV3()
    params = loader.get_connection_params(alias)

    has_password = params.get('password') is not None
    runtime_command = _build_runtime_command(command, cwd=cwd, shell=shell)
    result = None

    if has_password and not no_daemon:
        result = try_daemon_execute(alias, runtime_command, timeout)
        if result is None and start_daemon_background(alias):
            result = try_daemon_execute(alias, runtime_command, timeout)

    if result is None:
        result = direct_execute(alias, command, timeout, cwd=cwd, shell=shell, quiet=quiet)

    if isinstance(result, dict):
        mode = result.get('mode')
        if mode is None:
            mode = 'paramiko' if has_password else 'native'
            result['mode'] = mode
        if result.get('method') is None:
            result['method'] = 'daemon' if mode == 'daemon' else mode
        result.setdefault('runtime_command', runtime_command)

    return result


def _build_execute_result(raw_result, command, timeout, duration_ms, cwd=None, shell=None, verbose=False):
    result = {
        'command': command,
        'timeout': timeout,
        'duration_ms': duration_ms,
        'exit_code': raw_result.get('exit_code'),
        'stdout': raw_result.get('stdout', ''),
        'stderr': raw_result.get('stderr', ''),
    }

    if cwd is not None:
        result['cwd'] = cwd
    if shell is not None:
        result['shell'] = shell
    if raw_result.get('runtime_command') is not None:
        result['runtime_command'] = raw_result.get('runtime_command')
    if raw_result.get('method') is not None:
        result['method'] = raw_result.get('method')
    if raw_result.get('fallback_reason'):
        result['fallback_reason'] = raw_result.get('fallback_reason')
    if verbose:
        result['reporting'] = {
            'mode': raw_result.get('mode'),
            'success': raw_result.get('success'),
        }

    return result


def _normalize_execute_error(alias, command, timeout, mode, raw_result, cwd=None, shell=None):
    raw_error = raw_result.get('error')
    if _is_typed_error(raw_error):
        return raw_error

    stderr = raw_result.get('stderr') or ''
    exit_code = raw_result.get('exit_code')
    method = raw_result.get('method')
    details = {
        'command': command,
        'timeout': timeout,
        'mode': mode,
        'target': alias,
    }
    if cwd is not None:
        details['cwd'] = cwd
    if shell is not None:
        details['shell'] = shell
    if exit_code is not None:
        details['exit_code'] = exit_code
    if method:
        details['method'] = method

    lower_stderr = stderr.lower()
    if exit_code not in (None, -1):
        return _build_error(
            'remote_command_error',
            stderr or f'Remote command failed with exit code {exit_code}',
            details=details,
            cause=stderr or None,
            retriable=False,
        )

    if 'timeout' in lower_stderr or '超时' in stderr:
        code = 'timeout_error'
        message = stderr or 'Command timed out'
    elif 'permission denied' in lower_stderr or 'passphrase' in lower_stderr or 'auth' in lower_stderr:
        code = 'auth_error'
        message = stderr or 'Authentication failed'
    else:
        code = 'transport_error'
        message = stderr or 'Execution failed'

    return _build_error(
        code,
        message,
        details=details,
        cause=stderr or None,
        retriable=False,
    )


def _parse_verify_grep_specs(specs, shell=None):
    parsed = []
    for spec in specs:
        if '::' not in spec:
            raise ValueError('Invalid --verify-grep. Use <remote-path>::<pattern>.')
        target, pattern = spec.split('::', 1)
        target = _normalize_runtime_path(target, role='verify_grep_path', shell=shell)
        pattern = pattern.strip()
        if not pattern:
            raise ValueError('Invalid --verify-grep. Pattern cannot be empty.')
        parsed.append({'target': target, 'pattern': pattern})
    return parsed


def _normalize_verification_options(args, shell=None):
    return {
        'verify_exists': [_normalize_runtime_path(path, role='verify_exists_path', shell=shell) for path in (args.verify_exists or [])],
        'verify_read': [_normalize_runtime_path(path, role='verify_read_path', shell=shell) for path in (args.verify_read or [])],
        'verify_grep': _parse_verify_grep_specs(args.verify_grep or [], shell=shell),
        'verify_command': [command for command in (args.verify_command or []) if command.strip()],
    }


def _build_verification_command(check_type, shell, target=None, pattern=None):
    if shell == 'cmd':
        if check_type == 'exists':
            return f'if exist {_quote_cmd_string(target)} (exit /b 0) else (exit /b 1)'
        if check_type == 'read':
            return f'type {_quote_cmd_string(target)}'
        if check_type == 'grep':
            return f'findstr /n /c:{_quote_cmd_string(pattern)} {_quote_cmd_string(target)}'
    elif shell == 'powershell':
        if check_type == 'exists':
            return f"if (Test-Path -LiteralPath {_quote_powershell_string(target)}) {{ exit 0 }} else {{ exit 1 }}"
        if check_type == 'read':
            return f"Get-Content -LiteralPath {_quote_powershell_string(target)}"
        if check_type == 'grep':
            return f"Select-String -Path {_quote_powershell_string(target)} -Pattern {_quote_powershell_string(pattern)}"
    else:
        if check_type == 'exists':
            return f"test -e {shlex.quote(target)}"
        if check_type == 'read':
            return f"cat {shlex.quote(target)}"
        if check_type == 'grep':
            return f"grep -n -m 1 -e {shlex.quote(pattern)} {shlex.quote(target)}"

    raise ValueError(f'Unsupported verification command type: {check_type}')


def _execute_verification_command(alias, command, timeout, no_daemon, cwd=None, shell=None, quiet=False):
    try:
        return execute_command(alias, command, timeout, no_daemon=no_daemon, cwd=cwd, shell=shell, quiet=quiet)
    except Exception as e:
        return {
            'success': False,
            'exit_code': None,
            'stdout': '',
            'stderr': str(e),
            'mode': None,
            'method': None,
            'error': _build_error(
                'transport_error',
                'Verification command execution failed',
                details={
                    'target': alias,
                    'command': command,
                    'timeout': timeout,
                    'cwd': cwd,
                    'shell': shell,
                },
                cause=str(e),
                retriable=False,
            ),
        }


def _build_verification_failure(alias, command, timeout, mode, verification, cwd=None, shell=None):
    failed_checks = [check for check in verification.get('checks', []) if not check.get('success')]
    cause = '; '.join(check.get('message') or check.get('type') or 'verification failed' for check in failed_checks)
    return _build_error(
        'verification_error',
        'One or more verification checks failed',
        details={
            'target': alias,
            'command': command,
            'timeout': timeout,
            'mode': mode,
            'cwd': cwd,
            'shell': shell,
            'failed_checks': failed_checks,
        },
        cause=cause or 'verification failed',
        retriable=False,
    )


def _run_verification_checks(alias, timeout, no_daemon, verification_options, cwd=None, shell=None, quiet=False):
    checks = []

    for target in verification_options['verify_exists']:
        verify_command = _build_verification_command('exists', shell, target=target)
        raw = _execute_verification_command(alias, verify_command, timeout, no_daemon, cwd=cwd, shell=shell, quiet=quiet)
        success = bool(raw.get('success'))
        checks.append({
            'type': 'exists',
            'target': target,
            'success': success,
            'exit_code': raw.get('exit_code'),
            'mode': raw.get('mode'),
            'error': raw.get('error') if not success and _is_typed_error(raw.get('error')) else None,
            'message': None if success else (raw.get('stderr') or f'Path does not exist: {target}'),
        })

    for target in verification_options['verify_read']:
        verify_command = _build_verification_command('read', shell, target=target)
        raw = _execute_verification_command(alias, verify_command, timeout, no_daemon, cwd=cwd, shell=shell, quiet=quiet)
        success = bool(raw.get('success'))
        checks.append({
            'type': 'read',
            'target': target,
            'success': success,
            'exit_code': raw.get('exit_code'),
            'mode': raw.get('mode'),
            'output_preview': _truncate_text(raw.get('stdout') or ''),
            'error': raw.get('error') if not success and _is_typed_error(raw.get('error')) else None,
            'message': None if success else (raw.get('stderr') or f'Unable to read remote file: {target}'),
        })

    for item in verification_options['verify_grep']:
        target = item['target']
        pattern = item['pattern']
        verify_command = _build_verification_command('grep', shell, target=target, pattern=pattern)
        raw = _execute_verification_command(alias, verify_command, timeout, no_daemon, cwd=cwd, shell=shell, quiet=quiet)
        success = bool(raw.get('success'))
        checks.append({
            'type': 'grep',
            'target': target,
            'pattern': pattern,
            'success': success,
            'exit_code': raw.get('exit_code'),
            'mode': raw.get('mode'),
            'match_preview': _truncate_text((raw.get('stdout') or '').strip()),
            'error': raw.get('error') if not success and _is_typed_error(raw.get('error')) else None,
            'message': None if success else (raw.get('stderr') or f'Pattern not found in remote file: {target}'),
        })

    for verify_command in verification_options['verify_command']:
        raw = _execute_verification_command(alias, verify_command, timeout, no_daemon, cwd=cwd, shell=shell, quiet=quiet)
        success = bool(raw.get('success'))
        checks.append({
            'type': 'command',
            'command': verify_command,
            'success': success,
            'exit_code': raw.get('exit_code'),
            'mode': raw.get('mode'),
            'stdout_preview': _truncate_text(raw.get('stdout') or ''),
            'stderr_preview': _truncate_text(raw.get('stderr') or ''),
            'error': raw.get('error') if not success and _is_typed_error(raw.get('error')) else None,
            'message': None if success else (raw.get('stderr') or f'Verification command failed: {verify_command}'),
        })

    return {
        'success': all(check.get('success') for check in checks),
        'checks': checks,
    }


def main():
    parser = argparse.ArgumentParser(description='SSH command execution tool v3.0')
    parser.add_argument('alias', help='SSH host alias from ~/.ssh/config')
    parser.add_argument('command', help='Command to execute')
    parser.add_argument('--timeout', type=int, help='Timeout in seconds')
    parser.add_argument('--no-daemon', action='store_true',
                        help='Disable daemon mode, use direct SSH connection')
    parser.add_argument('--cwd', help='Remote working directory')
    parser.add_argument('--shell', choices=['sh', 'bash', 'cmd', 'powershell'],
                        help='Remote shell to use')
    parser.add_argument('--verify-exists', action='append', default=[],
                        help='Verify remote path exists after command succeeds')
    parser.add_argument('--verify-read', action='append', default=[],
                        help='Read remote file after command succeeds')
    parser.add_argument('--verify-grep', action='append', default=[],
                        help='Verify grep pattern with format <remote-path>::<pattern>')
    parser.add_argument('--verify-command', action='append', default=[],
                        help='Run an additional verification command after main command succeeds')
    add_reporting_arguments(parser)

    args = parser.parse_args()
    timeout = args.timeout or 30

    try:
        cwd = _normalize_runtime_path(args.cwd, role='cwd', shell=args.shell)
        verification_options = _normalize_verification_options(args, shell=args.shell)
    except PathNormalizationError as e:
        emit_json(_build_failure(
            alias=args.alias,
            code=e.code,
            message=str(e),
            details=e.to_error().get('details'),
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)
    except ValueError as e:
        emit_json(_build_failure(
            alias=args.alias,
            code='cli_argument_error',
            message=str(e),
            details={
                'target': args.alias,
                'command': args.command,
                'timeout': timeout,
                'cwd': args.cwd,
                'shell': args.shell,
            },
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)

    try:
        start_time = time.time()
        result = execute_command(
            args.alias,
            args.command,
            timeout,
            no_daemon=args.no_daemon,
            cwd=cwd,
            shell=args.shell,
            quiet=args.quiet,
        )
        duration_ms = int((time.time() - start_time) * 1000)
        mode = result.get('mode')
        primary_success = bool(result.get('success'))
        verification = {'success': True, 'checks': []}

        if primary_success:
            verification = _run_verification_checks(
                args.alias,
                timeout,
                args.no_daemon,
                verification_options,
                cwd=cwd,
                shell=args.shell,
                quiet=args.quiet,
            )

        success = primary_success and verification['success']
        error = None
        if not primary_success:
            error = _normalize_execute_error(args.alias, args.command, timeout, mode, result, cwd=cwd, shell=args.shell)
        elif not verification['success']:
            error = _build_verification_failure(args.alias, args.command, timeout, mode, verification, cwd=cwd, shell=args.shell)

        emit_json({
            'schema_version': '1.0',
            'success': success,
            'operation': 'execute',
            'target': args.alias,
            'mode': mode,
            'result': _build_execute_result(result, args.command, timeout, duration_ms, cwd=cwd, shell=args.shell, verbose=args.verbose),
            'verification': verification,
            'error': error,
        }, args=args, ensure_ascii=True)
        sys.exit(0 if success else 1)

    except FileNotFoundError as e:
        emit_json(_build_failure(
            alias=args.alias,
            code='target_resolution_error',
            message=f'Config not found: {e}',
            details={
                'target': args.alias,
                'command': args.command,
                'timeout': timeout,
                'cwd': cwd,
                'shell': args.shell,
            },
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)
    except ValueError as e:
        emit_json(_build_failure(
            alias=args.alias,
            code='cli_argument_error',
            message=f'Invalid alias: {e}',
            details={
                'target': args.alias,
                'command': args.command,
                'timeout': timeout,
                'cwd': cwd,
                'shell': args.shell,
            },
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)
    except Exception as e:
        emit_json(_build_failure(
            alias=args.alias,
            code='internal_error',
            message=f'Execution error: {e}',
            details={
                'target': args.alias,
                'command': args.command,
                'timeout': timeout,
                'cwd': cwd,
                'shell': args.shell,
            },
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
