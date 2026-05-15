#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON 配置迁移到 SSH Config 工具

功能：
1. 扫描 ~/.ssh/server_config/ 目录下的所有 JSON 配置
2. 转换为标准 SSH config 格式
3. 将元数据内嵌到 SSH config 注释中
4. 备份原有配置

用法：
    python migrate_to_ssh_config.py \
      --source ~/.ssh/server_config \
      --output ~/.ssh/config \
      --backup ~/.ssh/server_config.backup
"""

import sys
import os
import json
import argparse
import shutil
from datetime import datetime
from typing import List, Optional

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


class ConfigMigrator:
    """配置迁移器"""

    def __init__(self, source_dir: str, output_config: str,
                 backup_dir: Optional[str] = None,
                 force: bool = False):
        self.source_dir = os.path.expanduser(source_dir)
        self.output_config = os.path.expanduser(output_config)
        self.backup_dir = os.path.expanduser(backup_dir) if backup_dir else None
        self.force = force

        self.migration_report = {
            'started_at': datetime.now().isoformat(),
            'source_dir': self.source_dir,
            'output_config': self.output_config,
            'metadata_embedded': True,
            'backup_dir': self.backup_dir,
            'total_files': 0,
            'migrated': 0,
            'skipped': 0,
            'errors': [],
            'warnings': [],
            'backup': {
                'requested': bool(self.backup_dir),
                'created': False,
                'path': self.backup_dir,
                'existing_output_backup': None,
                'skipped_after_failure': False,
            },
            'steps': [],
        }

    def backup_source(self) -> dict:
        """备份源配置目录"""
        if not self.backup_dir:
            return {
                'success': True,
                'skipped': True,
                'message': '未指定备份目录，跳过源目录备份',
            }

        try:
            if os.path.exists(self.backup_dir):
                return {
                    'success': False,
                    'code': 'target_resolution_error',
                    'message': f'备份目录已存在: {self.backup_dir}',
                    'details': {'backup_dir': self.backup_dir},
                }

            shutil.copytree(self.source_dir, self.backup_dir)
            self.migration_report['backup']['created'] = True
            return {
                'success': True,
                'message': f'已备份到: {self.backup_dir}',
                'backup_dir': self.backup_dir,
            }

        except Exception as e:
            return {
                'success': False,
                'code': 'internal_error',
                'message': f'备份失败: {e}',
                'details': {'backup_dir': self.backup_dir},
                'cause': str(e),
            }

    def scan_json_configs(self) -> List[str]:
        """扫描 JSON 配置文件"""
        json_files = []

        if not os.path.exists(self.source_dir):
            return json_files

        for filename in os.listdir(self.source_dir):
            if filename.endswith('.json') and not filename.startswith('.'):
                if filename == 'servers.json':
                    continue
                json_files.append(os.path.join(self.source_dir, filename))

        return json_files

    def load_json_config(self, file_path: str) -> Optional[dict]:
        """加载 JSON 配置文件"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.migration_report['errors'].append({
                'file': file_path,
                'error': str(e)
            })
            return None

    def generate_alias(self, config: dict, filename: str) -> str:
        """生成主机别名"""
        if 'name' in config:
            return config['name']
        return os.path.splitext(os.path.basename(filename))[0]

    def convert_to_ssh_config(self, config: dict, alias: str) -> str:
        """转换为 SSH config 格式（带注释元数据）"""
        lines = []
        lines.append(f"\n# ===== {alias} =====")

        description = config.get('description', config.get('notes', ''))
        if description:
            lines.append(f"# description: {description}")

        if 'metadata' in config and 'environment' in config['metadata']:
            environment = config['metadata']['environment']
            lines.append(f"# environment: {environment}")

        if 'metadata' in config and 'tags' in config['metadata']:
            tags = config['metadata']['tags']
            if tags:
                lines.append(f"# tags: {','.join(tags)}")

        if 'metadata' in config and 'location' in config['metadata']:
            location = config['metadata']['location']
            lines.append(f"# location: {location}")

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if 'metadata' in config and 'created_at' in config['metadata']:
            lines.append(f"# created_at: {config['metadata']['created_at']}")
        else:
            lines.append(f"# created_at: {now}")
        lines.append(f"# updated_at: {now}")

        lines.append(f"Host {alias}")

        if 'host' in config:
            lines.append(f"    HostName {config['host']}")

        if 'user' in config:
            lines.append(f"    User {config['user']}")

        if 'port' in config and config['port'] != 22:
            lines.append(f"    Port {config['port']}")

        if 'key_file' in config:
            lines.append(f"    IdentityFile {config['key_file']}")

        if 'jump_hosts' in config and config['jump_hosts']:
            jump_hosts = config['jump_hosts']
            if isinstance(jump_hosts, list) and len(jump_hosts) > 0:
                jump_host = jump_hosts[0]
                if isinstance(jump_host, dict):
                    jump_alias = jump_host.get('name', jump_host.get('host'))
                else:
                    jump_alias = str(jump_host)

                lines.append(f"    ProxyJump {jump_alias}")
                self.migration_report['warnings'].append({
                    'alias': alias,
                    'warning': f'跳板机配置已转换为 ProxyJump: {jump_alias}'
                })

        if 'password' in config and config['password']:
            self.migration_report['warnings'].append({
                'alias': alias,
                'warning': '检测到 JSON 中包含密码字段；迁移后不会写入 SSH config，请改用运行时环境变量'
            })

        return '\n'.join(lines)

    def extract_metadata(self, config: dict, alias: str) -> dict:
        """提取元数据（用于报告）"""
        metadata = {}

        if 'metadata' in config:
            meta = config['metadata']
            metadata['environment'] = meta.get('environment', 'unknown')
            metadata['tags'] = meta.get('tags', [])
            metadata['location'] = meta.get('location', '')
        else:
            metadata['environment'] = 'unknown'
            metadata['tags'] = []
            metadata['location'] = ''

        if 'description' in config:
            metadata['description'] = config['description']
        elif 'notes' in config:
            metadata['description'] = config['notes']
        else:
            metadata['description'] = ''

        metadata['migrated_at'] = datetime.now().isoformat()
        metadata['original_file'] = alias + '.json'
        return metadata

    def migrate(self) -> dict:
        """执行迁移"""
        if not os.path.exists(self.source_dir):
            return {
                'success': False,
                'code': 'target_resolution_error',
                'message': f'源目录不存在: {self.source_dir}',
                'details': {
                    'source_dir': self.source_dir,
                    'output_config': self.output_config,
                    'backup_dir': self.backup_dir,
                },
            }

        if self.backup_dir:
            backup_result = self.backup_source()
            self.migration_report['steps'].append({
                'step': 'backup_source',
                'result': backup_result,
            })
            if not backup_result.get('success'):
                if not self.force:
                    return {
                        'success': False,
                        'code': backup_result.get('code', 'internal_error'),
                        'message': backup_result.get('message', '源目录备份失败'),
                        'details': {
                            'source_dir': self.source_dir,
                            'output_config': self.output_config,
                            'backup_dir': self.backup_dir,
                            'backup_result': backup_result,
                        },
                        'cause': backup_result.get('cause'),
                    }
                self.migration_report['backup']['skipped_after_failure'] = True
                self.migration_report['warnings'].append({
                    'alias': 'backup',
                    'warning': backup_result.get('message', '源目录备份失败，但已使用 --force 继续执行'),
                })

        json_files = self.scan_json_configs()
        self.migration_report['total_files'] = len(json_files)
        self.migration_report['steps'].append({
            'step': 'scan_json_configs',
            'result': {
                'count': len(json_files),
                'files': json_files,
            },
        })

        if len(json_files) == 0:
            self.migration_report['completed_at'] = datetime.now().isoformat()
            return {
                'success': True,
                'message': '没有找到需要迁移的配置文件',
                **self.migration_report,
            }

        ssh_config_lines = []
        migrated_aliases = []
        skipped_files = []

        for json_file in json_files:
            config = self.load_json_config(json_file)
            if config is None:
                self.migration_report['skipped'] += 1
                skipped_files.append(json_file)
                continue

            alias = self.generate_alias(config, json_file)
            ssh_config_text = self.convert_to_ssh_config(config, alias)
            ssh_config_lines.append(ssh_config_text)
            self.extract_metadata(config, alias)

            self.migration_report['migrated'] += 1
            migrated_aliases.append(alias)

        existing_output_backup = None
        if os.path.exists(self.output_config):
            existing_output_backup = self.output_config + '.backup.' + datetime.now().strftime('%Y%m%d_%H%M%S')
            shutil.copy2(self.output_config, existing_output_backup)
            self.migration_report['backup']['existing_output_backup'] = existing_output_backup

        with open(self.output_config, 'a', encoding='utf-8') as f:
            f.write('\n# ===== 从 JSON 配置迁移 =====\n')
            f.write(f'# 迁移时间: {datetime.now().isoformat()}\n')
            for config_text in ssh_config_lines:
                f.write(config_text + '\n')

        self.migration_report['completed_at'] = datetime.now().isoformat()

        return {
            'success': True,
            'message': 'JSON 配置已迁移到 SSH config',
            **self.migration_report,
            'migrated_aliases': migrated_aliases,
            'skipped_files': skipped_files,
            'report_file_written': False,
            'output_appended': True,
            'existing_output_backup': existing_output_backup,
        }


def main():
    parser = argparse.ArgumentParser(
        description='JSON 配置迁移到 SSH Config 工具',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_reporting_arguments(parser)
    parser.add_argument(
        '--source',
        default='~/.ssh/server_config',
        help='JSON 配置源目录（默认: ~/.ssh/server_config）'
    )
    parser.add_argument(
        '--output',
        default='~/.ssh/config',
        help='输出的 SSH config 文件路径（默认: ~/.ssh/config）'
    )
    parser.add_argument(
        '--backup',
        help='备份目录路径（可选）'
    )
    parser.add_argument(
        '--report',
        help='迁移报告输出文件（可选）'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='强制执行，跳过确认（用于非交互环境）'
    )

    args = parser.parse_args()
    operation = 'migrate_to_ssh_config'

    migrator = ConfigMigrator(
        source_dir=args.source,
        output_config=args.output,
        backup_dir=args.backup,
        force=args.force
    )

    result = migrator.migrate()

    if result.get('success') and args.report:
        report_path = os.path.expanduser(args.report)
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        result['report_file_written'] = True
        result['report_path'] = report_path

    target = os.path.expanduser(args.output)
    if result.get('success'):
        emit_json(_build_success(
            operation=operation,
            target=target,
            args=args,
            result=result,
            source_dir=os.path.expanduser(args.source),
            output_config=target,
            backup_dir=os.path.expanduser(args.backup) if args.backup else None,
            report_path=result.get('report_path'),
        ), args=args, ensure_ascii=False)
        return 0

    emit_json(_build_failure(
        operation=operation,
        target=target,
        code=result.get('code', 'internal_error'),
        message=result.get('message', 'JSON 配置迁移失败'),
        details=result.get('details', {
            'source_dir': os.path.expanduser(args.source),
            'output_config': target,
            'backup_dir': os.path.expanduser(args.backup) if args.backup else None,
            'report_path': os.path.expanduser(args.report) if args.report else None,
        }),
        cause=result.get('cause'),
        retriable=False,
    ), args=args, stream=sys.stderr, ensure_ascii=False)
    return 1


if __name__ == '__main__':
    sys.exit(main())
