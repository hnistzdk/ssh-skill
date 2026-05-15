#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH批量操作CLI工具 v3.0

从 SSH config 读取服务器列表，支持按环境/别名过滤

用法：
    python ssh_cluster.py <command> [--parallel] [--hosts HOSTS] [--environment ENV]

示例：
    # 对所有服务器执行命令
    python ssh_cluster.py "uptime" --parallel

    # 对指定别名列表执行
    python ssh_cluster.py "df -h" --hosts "DEV-002,DEV-003" --parallel

    # 按环境过滤
    python ssh_cluster.py "uptime" --environment production --parallel

    # 健康检查
    python ssh_cluster.py "systemctl status nginx" --parallel --health-check
"""

import sys
import os
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))

from cluster import SSHCluster
from reporting import add_reporting_arguments, emit_json, verbose_details


def _build_error(code, message, details=None, cause=None, retriable=False):
    return {
        'code': code,
        'message': message,
        'details': details or {},
        'cause': cause,
        'retriable': retriable,
    }


def _build_failure(operation, target, code, message, details=None, cause=None, retriable=False):
    return {
        'schema_version': '1.0',
        'success': False,
        'operation': operation,
        'target': target,
        'mode': 'cluster',
        'error': _build_error(code, message, details=details, cause=cause, retriable=retriable),
    }


def _build_result_payload(result, extra_details=None):
    payload = dict(result)
    if extra_details:
        payload['reporting'] = extra_details
    return payload


def main():
    parser = argparse.ArgumentParser(description='SSH批量操作工具 v3.0')
    parser.add_argument('command', help='要执行的命令')
    parser.add_argument('--hosts', help='指定别名列表（逗号分隔）')
    parser.add_argument('--environment', help='按环境过滤')
    parser.add_argument('--tags', help='按标签过滤（逗号分隔）')
    parser.add_argument('--parallel', action='store_true', help='并发执行')
    parser.add_argument('--timeout', type=int, help='超时时间（秒）')
    parser.add_argument('--health-check', action='store_true', help='健康检查模式')
    parser.add_argument('--max-workers', type=int, default=10, help='最大并发数')
    add_reporting_arguments(parser)

    args = parser.parse_args()
    target = args.hosts or args.environment or 'all'
    reporting = verbose_details(
        args,
        command=args.command,
        hosts=args.hosts,
        environment=args.environment,
        tags=args.tags,
        parallel=args.parallel,
        timeout=args.timeout,
        health_check=args.health_check,
        max_workers=args.max_workers,
    )

    try:
        aliases = args.hosts.split(',') if args.hosts else None
        tags = args.tags.split(',') if args.tags else None

        cluster = SSHCluster.from_ssh_config(
            aliases=aliases,
            environment=args.environment,
            tags=tags,
            max_workers=args.max_workers
        )

        if not cluster.clients:
            emit_json(_build_failure(
                operation='cluster_execute',
                target=target,
                code='target_resolution_error',
                message='No servers matched the filter criteria',
                details={
                    'hosts': aliases,
                    'environment': args.environment,
                    'tags': tags,
                },
                retriable=False,
            ), args=args, stream=sys.stderr, ensure_ascii=True)
            sys.exit(1)

        if args.health_check:
            health = cluster.health_check_all(
                check_command=args.command,
                parallel=args.parallel,
                timeout=args.timeout
            )

            output = {
                'success': True,
                'total': len(health),
                'healthy': sum(1 for v in health.values() if v),
                'unhealthy': sum(1 for v in health.values() if not v),
                'results': {name: {'healthy': status} for name, status in health.items()}
            }
            emit_json({
                'schema_version': '1.0',
                'success': all(health.values()),
                'operation': 'cluster_health_check',
                'target': target,
                'mode': 'cluster',
                'result': _build_result_payload(output, extra_details=reporting),
                'error': None,
            }, args=args, ensure_ascii=True)
            sys.exit(0 if all(health.values()) else 1)

        results = cluster.execute_all(
            args.command,
            parallel=args.parallel,
            timeout=args.timeout
        )

        output = {
            'success': all(r.success for r in results.values()),
            'total': len(results),
            'successful': sum(1 for r in results.values() if r.success),
            'failed': sum(1 for r in results.values() if not r.success),
            'results': {
                name: {
                    'success': result.success,
                    'exit_code': result.exit_code,
                    'stdout': result.stdout,
                    'stderr': result.stderr
                }
                for name, result in results.items()
            }
        }

        emit_json({
            'schema_version': '1.0',
            'success': output['success'],
            'operation': 'cluster_execute',
            'target': target,
            'mode': 'cluster',
            'result': _build_result_payload(output, extra_details=reporting),
            'error': None,
        }, args=args, ensure_ascii=True)
        sys.exit(0 if output['success'] else 1)

    except FileNotFoundError as e:
        emit_json(_build_failure(
            operation='cluster_execute',
            target=target,
            code='target_resolution_error',
            message=f'Config file not found: {e}',
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)
    except ValueError as e:
        emit_json(_build_failure(
            operation='cluster_execute',
            target=target,
            code='cli_argument_error',
            message=str(e),
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)
    except Exception as e:
        emit_json(_build_failure(
            operation='cluster_execute',
            target=target,
            code='internal_error',
            message=str(e),
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
