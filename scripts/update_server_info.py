#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量更新服务器系统信息到 environment 字段

获取每台服务器的：操作系统/CPU核心数/内存/磁盘总空间
并更新到 SSH config 的 environment 注释字段
"""

import sys
import os
import re
import argparse

# 修复 Windows 终端 UTF-8 输出
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 添加 lib 到路径
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_script_dir, 'lib'))

from config_v3 import SSHConfigLoaderV3
from reporting import add_reporting_arguments, emit_json, verbose_details


def _build_error(code, message, details=None, cause=None, retriable=False):
    return {
        'code': code,
        'message': message,
        'details': details or {},
        'cause': cause,
        'retriable': retriable,
    }


def _build_success(result, target, args=None, **details):
    payload = dict(result)
    reporting = verbose_details(args, **details)
    if reporting:
        payload['reporting'] = reporting
    return {
        'schema_version': '1.0',
        'success': True,
        'operation': 'update_server_info',
        'target': target,
        'mode': 'local',
        'result': payload,
        'error': None,
    }


def _build_failure(target, code, message, details=None, cause=None, retriable=False):
    return {
        'schema_version': '1.0',
        'success': False,
        'operation': 'update_server_info',
        'target': target,
        'mode': 'local',
        'error': _build_error(code, message, details=details, cause=cause, retriable=retriable),
    }


def _log(message, enabled=True, end='\n'):
    if not enabled:
        return
    sys.stderr.write(message + end)
    sys.stderr.flush()


def _extract_host_aliases(host_line):
    """从 Host 行提取具体别名列表，忽略通配符模式"""
    match = re.match(r'Host\s+(.+)', host_line.strip())
    if not match:
        return []

    aliases = []
    for alias in match.group(1).split():
        alias = alias.strip()
        if alias and '*' not in alias and '?' not in alias:
            aliases.append(alias)
    return aliases


def _find_host_index(lines, alias):
    """查找别名对应的 Host 行索引，支持多别名 Host"""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('Host ') and not stripped.startswith('Host *'):
            aliases = _extract_host_aliases(stripped)
            if alias in aliases:
                return i
    return -1

def get_system_info(alias):
    """获取服务器系统信息"""
    try:
        # 使用智能选择创建客户端
        loader = SSHConfigLoaderV3()
        client = loader.from_alias(alias)

        # 获取操作系统
        result = client.execute("cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'\"' -f2 || uname -s")
        os_name = result.stdout.strip() if result.success else "Unknown"

        # 获取 CPU 核心数
        result = client.execute("nproc 2>/dev/null || grep -c processor /proc/cpuinfo 2>/dev/null || echo '?'")
        cpu_cores = result.stdout.strip() if result.success else "?"

        # 获取内存（GB）
        result = client.execute("grep MemTotal /proc/meminfo 2>/dev/null | grep -o '[0-9]*' | head -1")
        if result.success and result.stdout.strip():
            mem_kb = int(result.stdout.strip())
            mem_gb = round(mem_kb / 1024 / 1024, 1)
            memory = f"{mem_gb}G"
        else:
            memory = "?"

        # 获取磁盘总空间
        result = client.execute("df -h / 2>/dev/null | tail -1 | tr -s ' ' | cut -d' ' -f2")
        disk = result.stdout.strip() if result.success else "?"

        # 格式化信息
        info = f"{os_name}/{cpu_cores}核/{memory}内存/{disk}磁盘"
        return {"success": True, "info": info}

    except Exception as e:
        return {"success": False, "error": str(e)}


def update_environment_field(alias, system_info):
    """更新 SSH config 中的 environment 字段"""
    config_path = os.path.expanduser("~/.ssh/config")

    if not os.path.exists(config_path):
        return {
            'success': False,
            'code': 'target_resolution_error',
            'message': f'SSH config 文件不存在: {config_path}',
            'details': {
                'alias': alias,
                'config_path': config_path,
            },
        }

    with open(config_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    host_index = _find_host_index(lines, alias)

    if host_index == -1:
        return {
            'success': False,
            'code': 'target_resolution_error',
            'message': f'找不到服务器 {alias}',
            'details': {
                'alias': alias,
                'config_path': config_path,
            },
        }

    env_index = -1
    for i in range(host_index - 1, max(0, host_index - 20), -1):
        line = lines[i].strip()
        if line.startswith('# environment:'):
            env_index = i
            break
        if line.startswith('# ====='):
            break

    if env_index != -1:
        new_env = system_info
        lines[env_index] = f"# environment: {new_env}\n"
    else:
        new_env = system_info
        lines.insert(host_index, f"# environment: {new_env}\n")

    with open(config_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    return {
        'success': True,
        'alias': alias,
        'environment': new_env,
        'config_path': config_path,
        'updated_existing_field': env_index != -1,
    }


def main():
    parser = argparse.ArgumentParser(description='批量更新服务器系统信息到 environment 字段')
    add_reporting_arguments(parser)
    parser.add_argument('--no-progress', action='store_true', help='禁用进度输出')
    args = parser.parse_args()

    loader = SSHConfigLoaderV3()
    config_path = loader.config_path
    if not os.path.exists(config_path):
        emit_json(_build_failure(
            target=config_path,
            code='target_resolution_error',
            message='SSH config 文件不存在',
            details={'config_path': config_path},
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1

    hosts = []
    seen_hosts = set()
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith('Host ') and not stripped.startswith('Host *'):
                for alias in _extract_host_aliases(stripped):
                    if alias not in seen_hosts:
                        hosts.append(alias)
                        seen_hosts.add(alias)

    show_progress = not args.quiet and not args.no_progress
    _log(f"找到 {len(hosts)} 台服务器，开始获取系统信息...\n", enabled=show_progress)

    results = []
    success_count = 0
    for i, alias in enumerate(hosts, 1):
        _log(f"[{i}/{len(hosts)}] 正在处理 {alias}... ", enabled=show_progress, end='')

        info_result = get_system_info(alias)
        if info_result.get('success'):
            info = info_result['info']
            update_result = update_environment_field(alias, info)
            if update_result.get('success'):
                _log(f"✓ {info}", enabled=show_progress)
                results.append({
                    'alias': alias,
                    'success': True,
                    'info': info,
                    'environment': update_result.get('environment'),
                    'updated_existing_field': update_result.get('updated_existing_field', False),
                })
                success_count += 1
            else:
                error_message = update_result.get('message', '更新配置失败')
                _log(f"✗ {error_message}", enabled=show_progress)
                results.append({
                    'alias': alias,
                    'success': False,
                    'code': update_result.get('code', 'internal_error'),
                    'error': error_message,
                    'details': update_result.get('details', {}),
                })
        else:
            error_message = info_result.get('error', '未知错误')
            _log(f"✗ {error_message}", enabled=show_progress)
            results.append({
                'alias': alias,
                'success': False,
                'code': info_result.get('code', 'connection_error'),
                'error': error_message,
            })

    failed_count = len(hosts) - success_count
    _log(f"\n完成！成功: {success_count}/{len(hosts)}", enabled=show_progress)

    summary = {
        'total': len(hosts),
        'successful': success_count,
        'failed': failed_count,
        'results': results,
    }

    if failed_count == 0:
        emit_json(_build_success(
            result=summary,
            target=config_path,
            args=args,
            config_path=config_path,
            hosts=len(hosts),
        ), args=args, ensure_ascii=False)
        return 0

    emit_json(_build_failure(
        target=config_path,
        code='verification_error',
        message='部分服务器系统信息更新失败',
        details={
            'config_path': config_path,
            'total': len(hosts),
            'successful': success_count,
            'failed': failed_count,
            'results': results,
        },
        retriable=False,
    ), args=args, stream=sys.stderr, ensure_ascii=False)
    return 1


if __name__ == '__main__':
    sys.exit(main())
