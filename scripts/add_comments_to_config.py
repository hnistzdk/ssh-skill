#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为现有 SSH config 添加标准注释

读取现有的 SSH config 文件，为每个 Host 添加标准注释元数据
"""

import sys
import os
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


def parse_existing_config(config_path):
    """
    解析现有配置，提取每个 Host 块

    Returns:
        List of (comments, host_line, config_lines) tuples
    """
    if not os.path.exists(config_path):
        return []

    with open(config_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    hosts = []
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
                hosts.append((current_comments, current_host_line, current_config))

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
        hosts.append((current_comments, current_host_line, current_config))

    return hosts


def extract_alias_from_host_line(host_line):
    """从 Host 行提取别名"""
    match = re.match(r'Host\s+(.+)', host_line.strip())
    if match:
        return match.group(1).strip()
    return None


def has_standard_comments(comments):
    """检查是否已有标准注释"""
    comment_text = ''.join(comments)
    return '# description:' in comment_text or '# environment:' in comment_text


def generate_standard_comments(alias):
    """生成标准注释"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    return [
        f"\n# ===== {alias} =====\n",
        "# description: \n",
        "# environment: unknown\n",
        "# tags: \n",
        "# location: \n",
        f"# created_at: {now}\n",
        f"# updated_at: {now}\n"
    ]


def add_comments_to_config(config_path, output_path=None):
    """
    为配置文件添加标准注释

    Args:
        config_path: 输入配置文件路径
        output_path: 输出配置文件路径（如果为 None，则覆盖原文件）
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
                'output_path': output_path,
            },
        }

    hosts = parse_existing_config(config_path)

    new_lines = []
    processed_count = 0
    skipped_count = 0
    processed_aliases = []
    skipped_aliases = []
    unnamed_blocks = 0

    for comments, host_line, config_lines in hosts:
        alias = extract_alias_from_host_line(host_line)

        if not alias:
            new_lines.extend(comments)
            new_lines.append(host_line)
            new_lines.extend(config_lines)
            skipped_count += 1
            unnamed_blocks += 1
            continue

        if has_standard_comments(comments):
            new_lines.extend(comments)
            new_lines.append(host_line)
            new_lines.extend(config_lines)
            skipped_count += 1
            skipped_aliases.append(alias)
        else:
            standard_comments = generate_standard_comments(alias)
            new_lines.extend(standard_comments)
            new_lines.append(host_line)
            new_lines.extend(config_lines)
            processed_count += 1
            processed_aliases.append(alias)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    return {
        'success': True,
        'message': '已完成 SSH config 标准注释补全',
        'config_path': config_path,
        'output_path': output_path,
        'total_hosts': len(hosts),
        'processed': processed_count,
        'skipped': skipped_count,
        'unnamed_blocks': unnamed_blocks,
        'processed_aliases': processed_aliases,
        'skipped_aliases': skipped_aliases,
        'overwrote_source': output_path == config_path,
    }


def main():
    parser = argparse.ArgumentParser(description='为现有 SSH config 添加标准注释')
    add_reporting_arguments(parser)
    parser.add_argument('--config', default='~/.ssh/config', help='输入 SSH config 路径（默认: ~/.ssh/config）')
    parser.add_argument('--output', help='输出 SSH config 路径（默认覆盖原文件）')

    args = parser.parse_args()
    operation = 'add_comments_to_config'

    config_path = os.path.expanduser(args.config)
    output_path = os.path.expanduser(args.output) if args.output else None

    result = add_comments_to_config(config_path, output_path)

    if result.get('success'):
        emit_json(_build_success(
            operation=operation,
            target=config_path,
            args=args,
            result=result,
            config_path=config_path,
            output_path=result.get('output_path'),
        ), args=args, ensure_ascii=False)
        return 0

    emit_json(_build_failure(
        operation=operation,
        target=config_path,
        code=result.get('code', 'internal_error'),
        message=result.get('message', '添加注释失败'),
        details=result.get('details', {
            'config_path': config_path,
            'output_path': output_path or config_path,
        }),
        cause=result.get('cause'),
        retriable=False,
    ), args=args, stream=sys.stderr, ensure_ascii=False)
    return 1


if __name__ == '__main__':
    sys.exit(main())
