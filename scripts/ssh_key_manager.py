#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH 密钥管理工具 v1.0

功能：
- 自动检测服务器类型（ESXi、Linux、FIPS模式等）
- 安全添加公钥（备份、去重、格式验证）
- 智能适配不同系统
- 批量操作支持
- 错误处理和回滚

用法：
    # 单台服务器添加密钥
    python ssh_key_manager.py add --host esxi-01 --key ~/.ssh/id_ed25519.pub

    # 批量添加
    python ssh_key_manager.py add --hosts "esxi-01,mgmt-01,dev-001" --key ~/.ssh/id_ed25519.pub

    # 所有服务器
    python ssh_key_manager.py add --all --key ~/.ssh/id_ed25519.pub

    # 验证密钥
    python ssh_key_manager.py verify --host esxi-01 --key ~/.ssh/id_ed25519.pub

    # 回滚操作
    python ssh_key_manager.py rollback --host esxi-01

作者：张阳 (zhangyang@bjued.cn)
日期：2026-03-04
"""

import sys
import os
import json
import argparse
import time
import re
import subprocess
from datetime import datetime
from typing import List, Optional, Tuple
from dataclasses import dataclass, asdict

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


@dataclass
class ServerInfo:
    """服务器信息"""
    alias: str
    server_type: str
    auth_keys_path: str
    supports_ed25519: bool
    os_info: str


@dataclass
class SSHResult:
    """SSH命令执行结果"""
    success: bool
    stdout: str
    stderr: str
    exit_code: int


@dataclass
class OperationResult:
    """操作结果"""
    alias: str
    success: bool
    action: str
    message: str
    backup_file: Optional[str] = None
    error: Optional[str] = None


class SSHKeyManager:
    """SSH 密钥管理器"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_loader = SSHConfigLoaderV3(config_path)
        self.progress_file = os.path.expanduser("~/.ssh_key_manager_progress.json")
        self.ssh_execute_script = os.path.join(_script_dir, "ssh_execute.py")

    def _log(self, message: str, quiet: bool = False):
        if not quiet:
            print(message, file=sys.stderr)

    def detect_server_type(self, alias: str) -> ServerInfo:
        """检测服务器类型"""
        result = self._execute_command(alias, "uname -a 2>/dev/null || echo 'Unknown'")
        os_info = result.stdout.strip() if result.success else "Unknown"

        is_esxi = "VMware ESXi" in os_info or "vmkernel" in os_info.lower()

        fips_result = self._execute_command(
            alias,
            "cat /proc/sys/crypto/fips_enabled 2>/dev/null || echo '0'"
        )
        is_fips = fips_result.stdout.strip() == "1" if fips_result.success else False

        if is_esxi:
            server_type = "esxi"
            user_result = self._execute_command(alias, "whoami 2>/dev/null || echo 'root'")
            user = user_result.stdout.strip() if user_result.success else "root"
            auth_keys_path = f"/etc/ssh/keys-{user}/authorized_keys"
            supports_ed25519 = False
        elif is_fips:
            server_type = "fips"
            auth_keys_path = "~/.ssh/authorized_keys"
            supports_ed25519 = False
        else:
            server_type = "standard"
            auth_keys_path = "~/.ssh/authorized_keys"
            supports_ed25519 = True

        return ServerInfo(
            alias=alias,
            server_type=server_type,
            auth_keys_path=auth_keys_path,
            supports_ed25519=supports_ed25519,
            os_info=os_info
        )

    def _execute_command(self, alias: str, command: str, timeout: int = 30) -> SSHResult:
        """执行 SSH 命令"""
        try:
            cmd = [
                "python",
                self.ssh_execute_script,
                alias,
                command,
                "--timeout",
                str(timeout)
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                encoding='utf-8',
                errors='replace'
            )

            try:
                payload = json.loads(result.stdout)
                if isinstance(payload, dict):
                    result_payload = payload.get('result')
                    if isinstance(result_payload, dict):
                        stderr = result_payload.get('stderr', '')
                        if not payload.get('success') and not stderr and isinstance(payload.get('error'), dict):
                            stderr = payload['error'].get('message') or payload['error'].get('cause') or ''
                        return SSHResult(
                            success=payload.get('success', False),
                            stdout=result_payload.get('stdout', ''),
                            stderr=stderr,
                            exit_code=result_payload.get('exit_code', result.returncode)
                        )

                    return SSHResult(
                        success=payload.get('success', result.returncode == 0),
                        stdout=payload.get('stdout', ''),
                        stderr=payload.get('stderr', ''),
                        exit_code=payload.get('exit_code', result.returncode)
                    )
            except json.JSONDecodeError:
                pass

            return SSHResult(
                success=result.returncode == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode
            )

        except Exception as e:
            return SSHResult(
                success=False,
                stdout='',
                stderr=str(e),
                exit_code=255
            )

    def backup_authorized_keys(self, alias: str, server_info: ServerInfo) -> Optional[str]:
        """备份 authorized_keys 文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{server_info.auth_keys_path}.backup_{timestamp}"

        result = self._execute_command(
            alias,
            f"cp {server_info.auth_keys_path} {backup_path} 2>/dev/null && echo 'OK' || echo 'FAIL'"
        )

        if result.success and "OK" in result.stdout:
            return backup_path
        return None

    def check_key_exists(self, alias: str, server_info: ServerInfo, public_key: str) -> bool:
        """检查密钥是否已存在"""
        key_parts = public_key.strip().split()
        if len(key_parts) < 2:
            return False

        key_signature = key_parts[1][:50]
        result = self._execute_command(
            alias,
            f"grep -F '{key_signature}' {server_info.auth_keys_path} 2>/dev/null"
        )

        return result.success and result.exit_code == 0

    def ensure_newline(self, alias: str, server_info: ServerInfo) -> bool:
        """确保 authorized_keys 文件末尾有换行符"""
        result = self._execute_command(
            alias,
            f"sed -i -e '$a\\' {server_info.auth_keys_path} 2>/dev/null && echo 'OK' || echo 'FAIL'"
        )

        return result.success and "OK" in result.stdout

    def verify_format(self, alias: str, server_info: ServerInfo) -> Tuple[bool, str]:
        """验证 authorized_keys 文件格式"""
        result = self._execute_command(
            alias,
            f"grep '@.*ssh-' {server_info.auth_keys_path} 2>/dev/null"
        )

        if result.success and result.exit_code == 0:
            return False, "发现密钥格式错误：密钥连在一起"

        return True, ""

    def add_key(self, alias: str, public_key_content: str, quiet: bool = False) -> OperationResult:
        """添加公钥到服务器"""
        try:
            self._log("  检测服务器类型...", quiet=quiet)
            server_info = self.detect_server_type(alias)
            self._log(f"  服务器类型: {server_info.server_type}", quiet=quiet)

            key_type = public_key_content.split()[0] if public_key_content.strip() else ""
            if key_type == "ssh-ed25519" and not server_info.supports_ed25519:
                return OperationResult(
                    alias=alias,
                    success=False,
                    action="skipped",
                    message=f"服务器不支持 ED25519 密钥 ({server_info.server_type} 模式)",
                    error="密钥类型不兼容"
                )

            self._log("  检查密钥是否已存在...", quiet=quiet)
            if self.check_key_exists(alias, server_info, public_key_content):
                return OperationResult(
                    alias=alias,
                    success=True,
                    action="exists",
                    message="密钥已存在"
                )

            self._log("  备份 authorized_keys...", quiet=quiet)
            backup_file = self.backup_authorized_keys(alias, server_info)
            if not backup_file:
                return OperationResult(
                    alias=alias,
                    success=False,
                    action="failed",
                    message="备份失败",
                    error="无法创建备份文件"
                )

            self._log("  确保文件格式正确...", quiet=quiet)
            self.ensure_newline(alias, server_info)

            self._log("  添加公钥...", quiet=quiet)
            escaped_key = public_key_content.strip().replace("'", "'\\''")
            result = self._execute_command(
                alias,
                f"printf '%s\\n' '{escaped_key}' >> {server_info.auth_keys_path} && echo 'OK' || echo 'FAIL'"
            )

            if not result.success or "FAIL" in result.stdout:
                self._execute_command(alias, f"cp {backup_file} {server_info.auth_keys_path}")
                return OperationResult(
                    alias=alias,
                    success=False,
                    action="failed",
                    message="添加密钥失败",
                    error=result.stderr,
                    backup_file=backup_file
                )

            self._log("  验证格式...", quiet=quiet)
            is_valid, error_msg = self.verify_format(alias, server_info)
            if not is_valid:
                self._execute_command(alias, f"cp {backup_file} {server_info.auth_keys_path}")
                return OperationResult(
                    alias=alias,
                    success=False,
                    action="failed",
                    message="格式验证失败",
                    error=error_msg,
                    backup_file=backup_file
                )

            self._execute_command(alias, f"chmod 600 {server_info.auth_keys_path}")

            return OperationResult(
                alias=alias,
                success=True,
                action="added",
                message=f"成功添加密钥 ({key_type})",
                backup_file=backup_file
            )

        except Exception as e:
            return OperationResult(
                alias=alias,
                success=False,
                action="failed",
                message="操作失败",
                error=str(e)
            )

    def verify_key(self, alias: str, public_key_content: str) -> OperationResult:
        """验证密钥是否存在"""
        try:
            server_info = self.detect_server_type(alias)
            exists = self.check_key_exists(alias, server_info, public_key_content)

            if exists:
                return OperationResult(
                    alias=alias,
                    success=True,
                    action="verified",
                    message="密钥存在"
                )

            return OperationResult(
                alias=alias,
                success=False,
                action="not_found",
                message="密钥不存在"
            )

        except Exception as e:
            return OperationResult(
                alias=alias,
                success=False,
                action="failed",
                message="验证失败",
                error=str(e)
            )

    def rollback(self, alias: str, backup_file: Optional[str] = None) -> OperationResult:
        """回滚到备份文件"""
        try:
            server_info = self.detect_server_type(alias)

            if not backup_file:
                result = self._execute_command(
                    alias,
                    f"ls -t {server_info.auth_keys_path}.backup_* 2>/dev/null | head -1"
                )
                if not result.success or not result.stdout.strip():
                    return OperationResult(
                        alias=alias,
                        success=False,
                        action="failed",
                        message="未找到备份文件"
                    )
                backup_file = result.stdout.strip()

            result = self._execute_command(
                alias,
                f"cp {backup_file} {server_info.auth_keys_path} && echo 'OK' || echo 'FAIL'"
            )

            if result.success and "OK" in result.stdout:
                return OperationResult(
                    alias=alias,
                    success=True,
                    action="rollback",
                    message=f"已回滚到 {backup_file}"
                )

            return OperationResult(
                alias=alias,
                success=False,
                action="failed",
                message="回滚失败",
                error=result.stderr
            )

        except Exception as e:
            return OperationResult(
                alias=alias,
                success=False,
                action="failed",
                message="回滚失败",
                error=str(e)
            )

    def batch_add_keys(
        self,
        hosts: List[str],
        public_key_content: str,
        on_error: str = "continue",
        quiet: bool = False
    ) -> List[OperationResult]:
        """批量添加密钥"""
        results = []
        total = len(hosts)

        for i, host in enumerate(hosts):
            if not quiet:
                self._log(f"\n[{i + 1}/{total}] 处理 {host}...", quiet=False)
            else:
                self._log(f"[{i + 1}/{total}] {host}...", quiet=False)

            result = self.add_key(host, public_key_content, quiet=quiet)
            results.append(result)
            self._save_progress(host)

            if quiet:
                if result.success:
                    if result.action == "exists":
                        self._log("[OK] 已存在", quiet=False)
                    elif result.action == "skipped":
                        self._log(f"[SKIP] 跳过 ({result.message})", quiet=False)
                    else:
                        self._log("[OK] 成功", quiet=False)
                else:
                    self._log("[FAIL] 失败", quiet=False)
            else:
                if result.success:
                    self._log(f"  [OK] {result.message}", quiet=False)
                else:
                    self._log(f"  [FAIL] {result.message}", quiet=False)
                    if result.error:
                        self._log(f"     错误: {result.error}", quiet=False)

            if not result.success and on_error == "stop":
                self._log("\n遇到错误，停止执行", quiet=False)
                break
            if not result.success and on_error == "ask":
                response = input("\n继续处理剩余服务器? (y/n): ")
                if response.lower() != 'y':
                    break

        return results

    def _save_progress(self, host: str):
        """保存进度"""
        try:
            progress = self._load_progress()
            progress.append(host)
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_progress(self) -> List[str]:
        """加载进度"""
        try:
            if os.path.exists(self.progress_file):
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _clear_progress(self):
        """清除进度"""
        try:
            if os.path.exists(self.progress_file):
                os.remove(self.progress_file)
        except Exception:
            pass

    def get_all_hosts(self) -> List[str]:
        """获取所有服务器别名"""
        try:
            config_path = os.path.expanduser("~/.ssh/config")
            if not os.path.exists(config_path):
                return []

            hosts = []
            with open(config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('Host ') and not line.startswith('Host *'):
                        match = re.match(r'Host\s+(.+)', line)
                        if match:
                            hosts.append(match.group(1).strip())
            return hosts
        except Exception:
            return []


def _operation_to_dict(result: OperationResult) -> dict:
    return asdict(result)


def generate_summary(results: List[OperationResult]) -> dict:
    """生成操作汇总报告"""
    total = len(results)
    success_count = sum(1 for r in results if r.success)
    failed_count = total - success_count

    added = [r.alias for r in results if r.action == "added"]
    exists = [r.alias for r in results if r.action == "exists"]
    skipped = [{
        'alias': r.alias,
        'message': r.message,
        'error': r.error,
    } for r in results if r.action == "skipped"]
    failed = [{
        'alias': r.alias,
        'message': r.message,
        'error': r.error,
    } for r in results if not r.success]

    return {
        'total': total,
        'successful': success_count,
        'failed': failed_count,
        'added': added,
        'exists': exists,
        'skipped': skipped,
        'failures': failed,
    }


def _load_public_key(key_path: str) -> str:
    expanded = os.path.expanduser(key_path)
    if not os.path.exists(expanded):
        raise FileNotFoundError(expanded)
    with open(expanded, 'r', encoding='utf-8') as f:
        content = f.read().strip()
    if not content:
        raise ValueError('公钥文件为空')
    return content


def _build_add_failure(results: List[OperationResult]) -> dict:
    failed = [r for r in results if not r.success]
    primary = failed[0] if failed else None
    return _build_failure(
        operation='ssh_key_manager_add',
        target='batch',
        code='transport_error',
        message='批量添加公钥存在失败项',
        details={
            'summary': generate_summary(results),
            'results': [_operation_to_dict(r) for r in results],
        },
        cause=primary.error if primary else None,
        retriable=False,
    )


def main():
    parser = argparse.ArgumentParser(
        description="SSH 密钥管理工具 - 安全、智能地管理服务器公钥",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单台服务器添加密钥
  %(prog)s add --host esxi-01 --key ~/.ssh/id_ed25519.pub

  # 批量添加
  %(prog)s add --hosts "esxi-01,mgmt-01,dev-001" --key ~/.ssh/id_ed25519.pub

  # 所有服务器
  %(prog)s add --all --key ~/.ssh/id_ed25519.pub

  # 验证密钥
  %(prog)s verify --host esxi-01 --key ~/.ssh/id_ed25519.pub

  # 回滚操作
  %(prog)s rollback --host esxi-01
        """
    )
    add_reporting_arguments(parser)

    subparsers = parser.add_subparsers(dest='command', help='命令')

    add_parser = subparsers.add_parser('add', help='添加公钥')
    add_parser.add_argument('--host', help='服务器别名')
    add_parser.add_argument('--hosts', help='服务器别名列表（逗号分隔）')
    add_parser.add_argument('--all', action='store_true', help='所有服务器')
    add_parser.add_argument('--key', required=True, help='公钥文件路径')
    add_parser.add_argument('--on-error', choices=['continue', 'stop', 'ask'],
                            default='continue', help='错误处理策略')
    add_parser.add_argument('--quiet', action='store_true', help='简洁模式')
    add_parser.add_argument('--resume', action='store_true', help='从上次中断处继续')

    verify_parser = subparsers.add_parser('verify', help='验证密钥')
    verify_parser.add_argument('--host', required=True, help='服务器别名')
    verify_parser.add_argument('--key', required=True, help='公钥文件路径')

    rollback_parser = subparsers.add_parser('rollback', help='回滚操作')
    rollback_parser.add_argument('--host', required=True, help='服务器别名')
    rollback_parser.add_argument('--backup', help='备份文件路径')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    manager = SSHKeyManager()

    try:
        if args.command == 'add':
            operation = 'ssh_key_manager_add'
            key_path = os.path.expanduser(args.key)
            try:
                public_key = _load_public_key(args.key)
            except FileNotFoundError:
                emit_json(_build_failure(
                    operation=operation,
                    target=args.host or args.hosts or 'all',
                    code='target_resolution_error',
                    message=f'公钥文件不存在: {key_path}',
                    details={'key_path': key_path},
                    retriable=False,
                ), args=args, stream=sys.stderr, ensure_ascii=False)
                return 1
            except ValueError as e:
                emit_json(_build_failure(
                    operation=operation,
                    target=args.host or args.hosts or 'all',
                    code='cli_argument_error',
                    message=str(e),
                    details={'key_path': key_path},
                    cause=str(e),
                    retriable=False,
                ), args=args, stream=sys.stderr, ensure_ascii=False)
                return 1

            if args.all:
                hosts = manager.get_all_hosts()
                if not hosts:
                    emit_json(_build_failure(
                        operation=operation,
                        target='all',
                        code='target_resolution_error',
                        message='未找到任何服务器配置',
                        details={'config_path': os.path.expanduser('~/.ssh/config')},
                        retriable=False,
                    ), args=args, stream=sys.stderr, ensure_ascii=False)
                    return 1
            elif args.hosts:
                hosts = [h.strip() for h in args.hosts.split(',') if h.strip()]
            elif args.host:
                hosts = [args.host]
            else:
                emit_json(_build_failure(
                    operation=operation,
                    target='add',
                    code='cli_argument_error',
                    message='必须指定 --host, --hosts 或 --all',
                    details={'key_path': key_path},
                    retriable=False,
                ), args=args, stream=sys.stderr, ensure_ascii=False)
                return 1

            if args.resume:
                completed = manager._load_progress()
                if completed:
                    hosts = [h for h in hosts if h not in completed]
                    if not hosts:
                        manager._clear_progress()
                        emit_json(_build_success(
                            operation=operation,
                            target='batch',
                            args=args,
                            result={
                                'message': '所有服务器已处理完成',
                                'key_path': key_path,
                                'resumed': True,
                                'remaining_hosts': [],
                                'completed_hosts': completed,
                                'summary': {
                                    'total': 0,
                                    'successful': 0,
                                    'failed': 0,
                                    'added': [],
                                    'exists': [],
                                    'skipped': [],
                                    'failures': [],
                                },
                                'results': [],
                            },
                            key_path=key_path,
                            resumed=True,
                        ), args=args, ensure_ascii=False)
                        return 0

            start_time = time.time()
            if len(hosts) == 1:
                result = manager.add_key(hosts[0], public_key, quiet=args.quiet)
                elapsed_ms = int((time.time() - start_time) * 1000)

                if result.success:
                    emit_json(_build_success(
                        operation=operation,
                        target=hosts[0],
                        args=args,
                        result={
                            'message': result.message,
                            'key_path': key_path,
                            'elapsed_ms': elapsed_ms,
                            'summary': generate_summary([result]),
                            'results': [_operation_to_dict(result)],
                        },
                        key_path=key_path,
                        host=hosts[0],
                    ), args=args, ensure_ascii=False)
                    return 0

                emit_json(_build_failure(
                    operation=operation,
                    target=hosts[0],
                    code='transport_error',
                    message=result.message,
                    details={
                        'key_path': key_path,
                        'result': _operation_to_dict(result),
                        'elapsed_ms': elapsed_ms,
                    },
                    cause=result.error,
                    retriable=False,
                ), args=args, stream=sys.stderr, ensure_ascii=False)
                return 1

            results = manager.batch_add_keys(hosts, public_key, args.on_error, args.quiet)
            elapsed_ms = int((time.time() - start_time) * 1000)
            manager._clear_progress()
            summary = generate_summary(results)
            payload = {
                'message': '批量公钥添加完成' if all(r.success for r in results) else '批量公钥添加存在失败项',
                'key_path': key_path,
                'elapsed_ms': elapsed_ms,
                'host_count': len(hosts),
                'summary': summary,
                'results': [_operation_to_dict(r) for r in results],
            }

            if all(r.success for r in results):
                emit_json(_build_success(
                    operation=operation,
                    target='batch',
                    args=args,
                    result=payload,
                    key_path=key_path,
                    host_count=len(hosts),
                ), args=args, ensure_ascii=False)
                return 0

            failure = _build_add_failure(results)
            failure['target'] = 'batch'
            failure['error']['details']['key_path'] = key_path
            failure['error']['details']['elapsed_ms'] = elapsed_ms
            emit_json(failure, args=args, stream=sys.stderr, ensure_ascii=False)
            return 1

        if args.command == 'verify':
            operation = 'ssh_key_manager_verify'
            key_path = os.path.expanduser(args.key)
            try:
                public_key = _load_public_key(args.key)
            except FileNotFoundError:
                emit_json(_build_failure(
                    operation=operation,
                    target=args.host,
                    code='target_resolution_error',
                    message=f'公钥文件不存在: {key_path}',
                    details={'key_path': key_path, 'host': args.host},
                    retriable=False,
                ), args=args, stream=sys.stderr, ensure_ascii=False)
                return 1
            except ValueError as e:
                emit_json(_build_failure(
                    operation=operation,
                    target=args.host,
                    code='cli_argument_error',
                    message=str(e),
                    details={'key_path': key_path, 'host': args.host},
                    cause=str(e),
                    retriable=False,
                ), args=args, stream=sys.stderr, ensure_ascii=False)
                return 1

            result = manager.verify_key(args.host, public_key)
            if result.success:
                emit_json(_build_success(
                    operation=operation,
                    target=args.host,
                    args=args,
                    result={
                        'message': result.message,
                        'key_path': key_path,
                        'result': _operation_to_dict(result),
                    },
                    key_path=key_path,
                    host=args.host,
                ), args=args, ensure_ascii=False)
                return 0

            emit_json(_build_failure(
                operation=operation,
                target=args.host,
                code='transport_error',
                message=result.message,
                details={
                    'key_path': key_path,
                    'result': _operation_to_dict(result),
                },
                cause=result.error,
                retriable=False,
            ), args=args, stream=sys.stderr, ensure_ascii=False)
            return 1

        if args.command == 'rollback':
            operation = 'ssh_key_manager_rollback'
            result = manager.rollback(args.host, args.backup)
            if result.success:
                emit_json(_build_success(
                    operation=operation,
                    target=args.host,
                    args=args,
                    result={
                        'message': result.message,
                        'backup': args.backup,
                        'result': _operation_to_dict(result),
                    },
                    host=args.host,
                    backup=args.backup,
                ), args=args, ensure_ascii=False)
                return 0

            emit_json(_build_failure(
                operation=operation,
                target=args.host,
                code='transport_error',
                message=result.message,
                details={
                    'backup': args.backup,
                    'result': _operation_to_dict(result),
                },
                cause=result.error,
                retriable=False,
            ), args=args, stream=sys.stderr, ensure_ascii=False)
            return 1

    except KeyboardInterrupt:
        emit_json(_build_failure(
            operation=f'ssh_key_manager_{args.command}',
            target=getattr(args, 'host', None) or getattr(args, 'hosts', None) or 'batch',
            code='internal_error',
            message='操作已取消',
            details={'command': args.command},
            cause='KeyboardInterrupt',
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 130
    except Exception as e:
        emit_json(_build_failure(
            operation=f'ssh_key_manager_{args.command}',
            target=getattr(args, 'host', None) or getattr(args, 'hosts', None) or 'batch',
            code='internal_error',
            message=f'执行失败: {e}',
            details={'command': args.command},
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1


if __name__ == '__main__':
    sys.exit(main())
