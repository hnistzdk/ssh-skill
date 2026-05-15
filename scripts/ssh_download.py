#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH文件下载CLI工具 v3.1

支持通过别名下载文件，SFTP 高级功能：断点续传、目录递归下载、进度显示

用法：
    python ssh_download.py <alias> <remote_path> <local_path> [options]

示例：
    # 下载单个文件
    python ssh_download.py prod-web-01 /var/log/app.log ./app.log

    # 断点续传
    python ssh_download.py prod-web-01 /tmp/large-file.iso ./large-file.iso --resume

    # 下载整个目录
    python ssh_download.py prod-web-01 /var/log/ ./logs/ --recursive

    # 下载目录 + 断点续传
    python ssh_download.py prod-web-01 /opt/data/ ./data/ --recursive --resume
"""

import sys
import os
import json
import argparse

# 添加lib到路径
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_script_dir, 'lib'))

from path_normalization import normalize_remote_path, PathNormalizationError
from reporting import add_reporting_arguments, emit_json, progress_enabled, verbose_details


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
        'operation': 'download',
        'target': alias,
        'mode': mode,
        'error': _build_error(code, message, details=details, cause=cause, retriable=retriable),
    }


def progress_callback(progress):
    """进度回调：输出 JSON 进度到 stderr"""
    info = progress.to_dict()
    try:
        sys.stderr.write(json.dumps(info, ensure_ascii=True) + '\n')
        sys.stderr.flush()
    except Exception:
        pass


def _build_result_payload(raw_result, extra_details=None):
    if not isinstance(raw_result, dict):
        payload = {'value': raw_result}
    else:
        payload = {}
        for key, value in raw_result.items():
            if key in ('success', 'error'):
                continue
            payload[key] = value

    if extra_details:
        payload['reporting'] = extra_details

    return payload


def _build_transfer_error(output, remote_path, local_path):
    errors = output.get('errors') or []
    messages = []
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                message = item.get('message') or item.get('cause') or str(item)
            else:
                message = str(item)
            if message:
                messages.append(message)
        message = '; '.join(messages)
    else:
        message = str(errors)

    return _build_error(
        'transport_error',
        message or 'Download failed',
        details={'remote_path': remote_path, 'local_path': local_path, 'errors': errors},
        cause=message or None,
        retriable=False,
    )


def main():
    parser = argparse.ArgumentParser(description='SSH file download tool v3.1')
    parser.add_argument('alias', help='SSH host alias from ~/.ssh/config')
    parser.add_argument('remote_path', help='Remote file or directory path')
    parser.add_argument('local_path', help='Local file or directory path')
    parser.add_argument('--resume', action='store_true',
                        help='Enable resume for interrupted transfers')
    parser.add_argument('--recursive', action='store_true',
                        help='Download directory recursively')
    parser.add_argument('--no-progress', action='store_true',
                        help='Disable progress output')
    add_reporting_arguments(parser)

    args = parser.parse_args()

    try:
        remote_path = normalize_remote_path(args.remote_path, role='remote_path')
    except PathNormalizationError as e:
        emit_json(_build_failure(
            alias=args.alias,
            code=e.code,
            message=str(e),
            details=e.to_error().get('details'),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)

    try:
        # 加载配置
        from config_v3 import SSHConfigLoaderV3
        loader = SSHConfigLoaderV3()
        params = loader.get_connection_params(args.alias)

        has_key = params.get('key_file') is not None
        has_password = params.get('password') is not None

        # 对于下载，无法提前知道远程文件大小，所以不做大文件判断
        # 如果用户明确需要进度显示，可以使用 --resume 参数强制使用 Paramiko SFTP
        # 智能选择：密钥认证且不需要高级功能时，使用原生 SSH
        # 断点续传、递归下载、密码认证时使用 Paramiko SFTP
        use_native = has_key and not has_password and not args.resume and not args.recursive
        reporting = verbose_details(
            args,
            transport='native' if use_native else 'paramiko',
            progress_enabled=progress_enabled(args),
            resume=args.resume,
            recursive=args.recursive,
        )

        if use_native:
            # 使用原生 SSH（简单下载，性能更好）
            client = loader.from_alias(args.alias)

            result = client.download(remote_path, args.local_path, show_progress=progress_enabled(args))
            emit_json({
                'schema_version': '1.0',
                'success': result.success,
                'operation': 'download',
                'target': args.alias,
                'mode': 'native',
                'result': {
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'exit_code': result.exit_code,
                    **({'reporting': reporting} if reporting else {}),
                },
                'error': None if result.success else _build_error(
                    'transport_error',
                    result.stderr or 'Download failed',
                    details={'remote_path': remote_path, 'local_path': args.local_path},
                    cause=result.stderr,
                    retriable=False,
                )
            }, args=args, ensure_ascii=True)
            sys.exit(0 if result.success else 1)
        else:
            # 使用 Paramiko SFTP（支持断点续传、递归下载等高级功能）
            from paramiko_client import ParamikoClient
            client = ParamikoClient(
                host=params['hostname'],
                user=params['user'],
                port=params['port'],
                password=params.get('password'),
                key_file=params.get('key_file'),
                timeout=30,
                jump_hosts=params.get('jump_hosts'),
                forward_agent=params.get('forward_agent', False),
                transfer_timeout=None  # 大文件传输不设超时限制
            )

            # 获取 SSH 连接和 SFTP
            ssh_client = client._get_connection()
            sftp = ssh_client.open_sftp()

            # 设置 SFTP 超时（大文件传输使用无限制）
            sftp.get_channel().settimeout(None)

            # 创建传输器
            from sftp_transfer import SFTPTransfer, _remote_isdir
            cb = None if not progress_enabled(args) else progress_callback
            transfer = SFTPTransfer(sftp, progress_callback=cb)

            # 判断远程路径是文件还是目录
            is_remote_dir = _remote_isdir(sftp, remote_path)

        if is_remote_dir:
            if not args.recursive:
                emit_json(_build_failure(
                    alias=args.alias,
                    code='cli_argument_error',
                    message=f'"{remote_path}" is a directory. Use --recursive to download directories.',
                    details={'remote_path': remote_path},
                    retriable=False,
                    mode='paramiko',
                ), args=args, stream=sys.stderr, ensure_ascii=True)
                sys.exit(1)
            result = transfer.download_directory(remote_path, args.local_path,
                                                  resume=args.resume)
        else:
            result = transfer.download_file(remote_path, args.local_path,
                                            resume=args.resume)

        # 关闭 SFTP
        sftp.close()

        # 输出结果
        output = result.to_dict()
        success = bool(output.get('success'))
        emit_json({
            'schema_version': '1.0',
            'success': success,
            'operation': 'download',
            'target': args.alias,
            'mode': 'paramiko',
            'result': _build_result_payload(output, extra_details=reporting),
            'error': None if success else _build_transfer_error(output, remote_path, args.local_path)
        }, args=args, ensure_ascii=True)
        sys.exit(0 if success else 1)

    except FileNotFoundError as e:
        emit_json(_build_failure(
            alias=args.alias,
            code='target_resolution_error',
            message=f'File not found: {e}',
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)
    except ValueError as e:
        emit_json(_build_failure(
            alias=args.alias,
            code='cli_argument_error',
            message=f'Invalid alias: {e}',
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)
    except Exception as e:
        emit_json(_build_failure(
            alias=args.alias,
            code='internal_error',
            message=f'Download error: {e}',
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
