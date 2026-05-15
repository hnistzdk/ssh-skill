#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 SSH Config 文件

1. 从原始 JSON 配置中提取元数据填充注释
2. 统一证书文件路径格式为 ~/.ssh/keyfile
3. 不再在注释中写入密码字段
"""

import sys
import os
import json
import re
import argparse
from datetime import datetime

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


def load_json_config(json_path):
    """加载 JSON 配置文件"""
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def find_json_config_by_alias(alias, json_dir):
    """根据别名查找对应的 JSON 配置文件"""
    json_path = os.path.join(json_dir, f"{alias}.json")
    if os.path.exists(json_path):
        return load_json_config(json_path)

    for filename in os.listdir(json_dir):
        if not filename.endswith('.json'):
            continue

        name_without_ext = os.path.splitext(filename)[0]
        if name_without_ext.upper() == alias.upper():
            json_path = os.path.join(json_dir, filename)
            return load_json_config(json_path)

    for filename in os.listdir(json_dir):
        if not filename.endswith('.json'):
            continue

        json_path = os.path.join(json_dir, filename)
        config = load_json_config(json_path)
        if config and config.get('name') == alias:
            return config

    return None


def normalize_key_path(key_path):
    """
    统一证书文件路径格式为 ~/.ssh/keyfile

    Args:
        key_path: 原始路径

    Returns:
        标准化后的路径
    """
    if not key_path:
        return key_path

    if key_path.startswith('~/.ssh/'):
        return key_path

    normalized = key_path.replace('\\', '/')

    if '/.ssh/' in normalized:
        parts = normalized.split('/.ssh/')
        if len(parts) == 2:
            return f"~/.ssh/{parts[1]}"

    return key_path


def extract_metadata_from_json(config):
    """从 JSON 配置中提取元数据"""
    metadata = {
        'description': '',
        'environment': 'unknown',
        'tags': [],
        'location': '',
        'has_password': False,
    }

    if 'description' in config:
        metadata['description'] = config['description']
    elif 'notes' in config:
        metadata['description'] = config['notes']

    if 'metadata' in config:
        meta = config['metadata']
        metadata['environment'] = meta.get('environment', 'unknown')
        metadata['tags'] = meta.get('tags', [])
        metadata['location'] = meta.get('location', '')

    metadata['has_password'] = bool(config.get('password'))

    return metadata


def parse_ssh_config(config_path):
    """
    解析 SSH config 文件

    Returns:
        List of blocks, each block is a dict with:
        - comments: list of comment lines
        - host_line: the Host line
        - config_lines: list of config lines
        - alias: extracted alias
    """
    if not os.path.exists(config_path):
        return []

    with open(config_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    blocks = []
    current_comments = []
    current_host_line = None
    current_config = []
    in_host_block = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith('Host ') and not stripped.startswith('Host *'):
            if current_host_line:
                alias = extract_alias_from_host_line(current_host_line)
                blocks.append({
                    'comments': current_comments,
                    'host_line': current_host_line,
                    'config_lines': current_config,
                    'alias': alias
                })

            current_host_line = line
            current_config = []
            in_host_block = True

        elif in_host_block:
            if stripped and not stripped.startswith('#'):
                if line.startswith((' ', '\t')):
                    current_config.append(line)
                else:
                    in_host_block = False
                    current_comments = []
                    if stripped.startswith('#'):
                        current_comments.append(line)
            elif stripped.startswith('#'):
                current_config.append(line)
            elif not stripped:
                current_config.append(line)
                in_host_block = False
                current_comments = []
        else:
            if stripped.startswith('#') or not stripped:
                current_comments.append(line)
            else:
                current_comments = []

        i += 1

    if current_host_line:
        alias = extract_alias_from_host_line(current_host_line)
        blocks.append({
            'comments': current_comments,
            'host_line': current_host_line,
            'config_lines': current_config,
            'alias': alias
        })

    return blocks


def extract_alias_from_host_line(host_line):
    """从 Host 行提取别名"""
    match = re.match(r'Host\s+(.+)', host_line.strip())
    if match:
        return match.group(1).strip()
    return None


def generate_updated_comments(alias, metadata):
    """生成更新后的注释"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    comments = [
        f"\n# ===== {alias} =====\n",
        f"# description: {metadata.get('description', '')}\n",
        f"# environment: {metadata.get('environment', 'unknown')}\n",
    ]

    tags = metadata.get('tags', [])
    if tags:
        comments.append(f"# tags: {','.join(tags)}\n")
    else:
        comments.append("# tags: \n")

    location = metadata.get('location', '')
    comments.append(f"# location: {location}\n")
    comments.append(f"# created_at: {now}\n")
    comments.append(f"# updated_at: {now}\n")

    return comments


def normalize_config_lines(config_lines):
    """标准化配置行中的证书路径"""
    normalized = []

    for line in config_lines:
        if 'IdentityFile' in line:
            match = re.match(r'(\s*)IdentityFile\s+(.+)', line)
            if match:
                indent = match.group(1)
                key_path = match.group(2).strip()
                normalized_path = normalize_key_path(key_path)
                normalized.append(f"{indent}IdentityFile {normalized_path}\n")
                continue

        normalized.append(line)

    return normalized


def fix_ssh_config(config_path, json_dir, output_path=None):
    """
    修复 SSH config 文件

    Args:
        config_path: SSH config 文件路径
        json_dir: JSON 配置目录
        output_path: 输出文件路径（如果为 None，则覆盖原文件）
    """
    if output_path is None:
        output_path = config_path

    if not os.path.exists(config_path):
        return {
            'success': False,
            'code': 'target_resolution_error',
            'message': f'SSH config 文件不存在: {config_path}',
            'details': {
                'config_path': config_path,
                'json_dir': json_dir,
                'output_path': output_path,
            },
        }

    if not os.path.exists(json_dir):
        return {
            'success': False,
            'code': 'target_resolution_error',
            'message': f'JSON 配置目录不存在: {json_dir}',
            'details': {
                'config_path': config_path,
                'json_dir': json_dir,
                'output_path': output_path,
            },
        }

    blocks = parse_ssh_config(config_path)

    new_lines = []
    updated_count = 0
    normalized_count = 0
    password_fields_ignored = 0
    updated_aliases = []
    skipped_aliases = []

    for block in blocks:
        alias = block['alias']

        if not alias:
            new_lines.extend(block['comments'])
            new_lines.append(block['host_line'])
            new_lines.extend(block['config_lines'])
            continue

        json_config = find_json_config_by_alias(alias, json_dir)

        if json_config:
            metadata = extract_metadata_from_json(json_config)
            new_comments = generate_updated_comments(alias, metadata)
            new_lines.extend(new_comments)

            updated_count += 1
            updated_aliases.append(alias)

            if metadata.get('has_password'):
                password_fields_ignored += 1
        else:
            new_lines.extend(block['comments'])
            skipped_aliases.append(alias)

        new_lines.append(block['host_line'])

        normalized_config = normalize_config_lines(block['config_lines'])
        if normalized_config != block['config_lines']:
            normalized_count += 1

        new_lines.extend(normalized_config)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    return {
        'success': True,
        'message': 'SSH config 修复完成',
        'config_path': config_path,
        'json_dir': json_dir,
        'output_path': output_path,
        'total_hosts': len(blocks),
        'updated_metadata': updated_count,
        'normalized_paths': normalized_count,
        'password_fields_ignored': password_fields_ignored,
        'updated_aliases': updated_aliases,
        'skipped_aliases': skipped_aliases,
        'overwrote_source': output_path == config_path,
    }


def main():
    parser = argparse.ArgumentParser(description='修复 SSH Config 文件')
    add_reporting_arguments(parser)
    parser.add_argument('--config', default='~/.ssh/config', help='SSH config 文件路径（默认: ~/.ssh/config）')
    parser.add_argument('--json-dir', default='~/.ssh/server_config', help='JSON 配置目录（默认: ~/.ssh/server_config）')
    parser.add_argument('--output', help='输出文件路径（默认覆盖原文件）')

    args = parser.parse_args()
    operation = 'fix_ssh_config'

    config_path = os.path.expanduser(args.config)
    json_dir = os.path.expanduser(args.json_dir)
    output_path = os.path.expanduser(args.output) if args.output else None

    result = fix_ssh_config(config_path, json_dir, output_path)

    if result.get('success'):
        emit_json(_build_success(
            operation=operation,
            target=config_path,
            args=args,
            result=result,
            config_path=config_path,
            json_dir=json_dir,
            output_path=result.get('output_path'),
        ), args=args, ensure_ascii=False)
        return 0

    emit_json(_build_failure(
        operation=operation,
        target=config_path,
        code=result.get('code', 'internal_error'),
        message=result.get('message', '修复 SSH config 失败'),
        details=result.get('details', {
            'config_path': config_path,
            'json_dir': json_dir,
            'output_path': output_path or config_path,
        }),
        cause=result.get('cause'),
        retriable=False,
    ), args=args, stream=sys.stderr, ensure_ascii=False)
    return 1


if __name__ == '__main__':
    sys.exit(main())
