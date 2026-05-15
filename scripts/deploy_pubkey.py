#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部署公钥到远程服务器

将指定的公钥部署到远程服务器，实现从密码认证迁移到密钥认证。
"""

import sys
import os
import argparse

# 修复 Windows 终端 UTF-8 输出
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 添加 lib 到路径
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


def _build_failure(operation, target, code, message, details=None, cause=None, retriable=False, mode='remote'):
    return {
        'schema_version': '1.0',
        'success': False,
        'operation': operation,
        'target': target,
        'mode': mode,
        'error': _build_error(code, message, details=details, cause=cause, retriable=retriable),
    }


def _build_success(operation, target, result, mode='remote', args=None, **details):
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


def deploy_pubkey(alias, pubkey_content, key_name):
    """
    部署公钥到远程服务器

    Args:
        alias: 服务器别名
        pubkey_content: 公钥内容
        key_name: 密钥名称（用于标识）

    Returns:
        dict: 执行结果
    """
    from config_v3 import SSHConfigLoaderV3

    try:
        loader = SSHConfigLoaderV3()
        params = loader.get_connection_params(alias)

        if not params.get('password'):
            return {
                'success': False,
                'code': 'auth_error',
                'message': f'{alias} 没有配置密码，无法使用密码认证部署公钥',
                'details': {'alias': alias},
            }

        from paramiko_client import ParamikoClient
        client = ParamikoClient(
            host=params['hostname'],
            user=params['user'],
            port=params['port'],
            password=params['password'],
            timeout=30
        )

        steps = [{'step': 'connect', 'message': f'正在连接到 {alias}'}]

        result = client.execute("echo 'Connection OK'")
        if not result.success:
            return {
                'success': False,
                'code': 'connection_error',
                'message': f'无法连接到 {alias}',
                'details': {
                    'alias': alias,
                    'stderr': result.stderr,
                },
                'cause': result.stderr or None,
            }

        steps.append({'step': 'connect', 'message': '连接成功，开始部署公钥...'})

        result = client.execute("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
        if not result.success:
            return {
                'success': False,
                'code': 'transport_error',
                'message': '无法创建 .ssh 目录',
                'details': {
                    'alias': alias,
                    'stderr': result.stderr,
                },
                'cause': result.stderr or None,
            }

        result = client.execute(f"grep -F '{pubkey_content.strip()}' ~/.ssh/authorized_keys 2>/dev/null")
        if result.success and result.stdout.strip():
            return {
                'success': True,
                'message': f'公钥已存在于 {alias}，无需重复添加',
                'alias': alias,
                'key_name': key_name,
                'changed': False,
                'verification': 'skipped',
                'steps': steps,
            }

        escaped_pubkey = pubkey_content.strip().replace("'", "'\\''")
        result = client.execute(
            f"echo '{escaped_pubkey}' >> ~/.ssh/authorized_keys && "
            f"chmod 600 ~/.ssh/authorized_keys"
        )

        if not result.success:
            return {
                'success': False,
                'code': 'transport_error',
                'message': '无法写入公钥到 authorized_keys',
                'details': {
                    'alias': alias,
                    'stderr': result.stderr,
                },
                'cause': result.stderr or None,
            }

        steps.append({'step': 'write_authorized_keys', 'message': f'公钥已成功部署到 {alias}'})
        steps.append({'step': 'verify_key_auth', 'message': '验证步骤已跳过，需用户手动测试'})

        return {
            'success': True,
            'message': f'现在可以使用密钥 {key_name} 连接到 {alias}',
            'alias': alias,
            'key_name': key_name,
            'changed': True,
            'verification': 'skipped',
            'next_step': '使用 migrate_to_key_auth.py 更新 SSH config',
            'steps': steps,
        }

    except Exception as e:
        return {
            'success': False,
            'code': 'internal_error',
            'message': str(e),
            'details': {
                'alias': alias,
                'key_name': key_name,
            },
            'cause': str(e),
        }


def main():
    parser = argparse.ArgumentParser(description='部署公钥到远程服务器')
    add_reporting_arguments(parser)
    parser.add_argument('alias', help='服务器别名')
    parser.add_argument('--pubkey-file', required=True, help='公钥文件路径')
    parser.add_argument('--key-name', required=True, help='密钥名称（如 id_rsa_sa_legacy）')

    args = parser.parse_args()
    operation = 'deploy_pubkey'

    pubkey_file = os.path.expanduser(args.pubkey_file)
    if not os.path.exists(pubkey_file):
        emit_json(_build_failure(
            operation=operation,
            target=args.alias,
            code='target_resolution_error',
            message=f'公钥文件不存在: {pubkey_file}',
            details={'pubkey_file': pubkey_file, 'alias': args.alias},
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1

    with open(pubkey_file, 'r', encoding='utf-8') as f:
        pubkey_content = f.read().strip()

    if not pubkey_content:
        emit_json(_build_failure(
            operation=operation,
            target=args.alias,
            code='cli_argument_error',
            message='公钥文件为空',
            details={'pubkey_file': pubkey_file, 'alias': args.alias},
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1

    result = deploy_pubkey(args.alias, pubkey_content, args.key_name)

    if result.get('success'):
        emit_json(_build_success(
            operation=operation,
            target=args.alias,
            args=args,
            result=result,
            alias=args.alias,
            key_name=args.key_name,
            pubkey_file=pubkey_file,
        ), args=args, ensure_ascii=False)
        return 0

    emit_json(_build_failure(
        operation=operation,
        target=args.alias,
        code=result.get('code', 'internal_error'),
        message=result.get('message', f'无法部署公钥到 {args.alias}'),
        details={
            'alias': args.alias,
            'key_name': args.key_name,
            'pubkey_file': pubkey_file,
            **result.get('details', {}),
        },
        cause=result.get('cause'),
        retriable=False,
    ), args=args, stream=sys.stderr, ensure_ascii=False)
    return 1


if __name__ == '__main__':
    sys.exit(main())
