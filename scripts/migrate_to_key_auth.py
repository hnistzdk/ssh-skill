#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迁移服务器从密码认证到密钥认证

更新 SSH config：
1. 移除 password 注释字段
2. 添加 IdentityFile 配置
3. 不再持久化密码信息
"""

import sys
import os
import re
import argparse

# 修复 Windows 终端 UTF-8 输出
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_script_dir, 'lib'))

from reporting import add_reporting_arguments, emit_json, verbose_details


def _build_error(code, message, details=None, cause=None, retriable=False):
    return {
        'code': code,
        'message': message,
        'details': details or {},
        'cause': cause,
        'retriable': retriable,
    }


def _build_failure(operation, target, code, message, details=None, cause=None, retriable=False, mode='local'):
    return {
        'schema_version': '1.0',
        'success': False,
        'operation': operation,
        'target': target,
        'mode': mode,
        'error': _build_error(code, message, details=details, cause=cause, retriable=retriable),
    }


def _build_success(operation, target, result, mode='local', args=None, **details):
    payload = dict(result)
    reporting = verbose_details(args, **details)
    if reporting:
        payload['reporting'] = reporting
    return {
        'schema_version': '1.0',
        'success': True,
        'operation': operation,
        'target': target,
        'mode': mode,
        'result': payload,
        'error': None,
    }


def migrate_to_key_auth(alias, key_file):
    """
    迁移服务器配置从密码认证到密钥认证

    Args:
        alias: 服务器别名
        key_file: 密钥文件名（如 id_rsa_sa_legacy）

    Returns:
        dict: 执行结果
    """
    config_path = os.path.expanduser("~/.ssh/config")

    if not os.path.exists(config_path):
        return {
            'success': False,
            'code': 'target_resolution_error',
            'message': f'SSH config 文件不存在: {config_path}',
            'details': {'config_path': config_path},
        }

    with open(config_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    host_index = -1
    for i, line in enumerate(lines):
        if line.strip().startswith('Host ') and not line.strip().startswith('Host *'):
            match = re.match(r'Host\s+(.+)', line.strip())
            if match and match.group(1).strip() == alias:
                host_index = i
                break

    if host_index == -1:
        return {
            'success': False,
            'code': 'target_resolution_error',
            'message': f'找不到服务器 {alias}',
            'details': {'alias': alias, 'config_path': config_path},
        }

    comment_start = host_index
    for i in range(host_index - 1, max(0, host_index - 20), -1):
        line = lines[i].strip()
        if line.startswith('# ====='):
            comment_start = i
            break
        if not line.startswith('#') and line:
            break

    password_index = -1
    had_password_annotation = False

    for i in range(comment_start, host_index):
        line = lines[i].strip()
        if line.startswith('# password:'):
            password_index = i
            had_password_annotation = True
            break

    if password_index == -1:
        return {
            'success': False,
            'code': 'auth_error',
            'message': f'{alias} 没有配置密码注释，可能已经是密钥认证',
            'details': {'alias': alias, 'config_path': config_path},
        }

    lines[password_index] = ''

    host_end = host_index + 1
    for i in range(host_index + 1, len(lines)):
        line = lines[i].strip()
        if line.startswith('Host ') and not line.startswith('Host *'):
            break
        if line.startswith('# ====='):
            break
        host_end = i + 1

    has_identity_file = False
    for i in range(host_index, host_end):
        if 'IdentityFile' in lines[i]:
            has_identity_file = True
            break

    if not has_identity_file:
        indent = '    '
        lines.insert(host_end, f"{indent}IdentityFile ~/.ssh/{key_file}\n")

    with open(config_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    return {
        'success': True,
        'message': f'现在可以使用密钥连接到 {alias}',
        'alias': alias,
        'key_file': key_file,
        'config_path': config_path,
        'password_annotation_removed': had_password_annotation,
        'identity_file_added': not has_identity_file,
    }


def main():
    parser = argparse.ArgumentParser(description='迁移服务器从密码认证到密钥认证')
    add_reporting_arguments(parser)
    parser.add_argument('alias', help='服务器别名')
    parser.add_argument('--key-file', required=True, help='密钥文件名（如 id_rsa_sa_legacy）')

    args = parser.parse_args()
    operation = 'migrate_to_key_auth'

    result = migrate_to_key_auth(args.alias, args.key_file)

    if result.get('success'):
        emit_json(_build_success(
            operation=operation,
            target=args.alias,
            args=args,
            result=result,
            alias=args.alias,
            key_file=args.key_file,
            config_path=result.get('config_path'),
        ), args=args, ensure_ascii=False)
        return 0

    emit_json(_build_failure(
        operation=operation,
        target=args.alias,
        code=result.get('code', 'internal_error'),
        message=result.get('message', f'无法迁移 {args.alias}'),
        details={
            'alias': args.alias,
            'key_file': args.key_file,
            **result.get('details', {}),
        },
        cause=result.get('cause'),
        retriable=False,
    ), args=args, stream=sys.stderr, ensure_ascii=False)
    return 1


if __name__ == '__main__':
    sys.exit(main())
