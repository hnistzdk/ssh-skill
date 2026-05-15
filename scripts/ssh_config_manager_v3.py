#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH Config 管理工具 v3.1

基于标准 OpenSSH config 格式的配置管理工具

新特性：
1. 使用标准 ~/.ssh/config 文件
2. 支持别名（Host）管理
3. 元数据存储在注释中（无需单独的 metadata 文件）
4. 支持 ProxyJump（跳板机）配置
5. 支持在注释中持久化密码，并兼容运行时环境变量覆盖

注释元数据格式：
# ===== 服务器名称 =====
# description: 详细描述
# environment: production|development|staging
# tags: tag1,tag2,tag3
# location: 物理位置
# password: your-password
# created_at: 2026-03-01
# updated_at: 2026-03-01
Host alias
    HostName 192.168.1.100
    User root

用法：
    # 列出所有服务器
    python ssh_config_manager_v3.py list-servers

    # 按环境过滤
    python ssh_config_manager_v3.py list-servers --environment production

    # 查找服务器
    python ssh_config_manager_v3.py find "web"

    # 创建配置
    python ssh_config_manager_v3.py create \\
      --alias prod-web-01 \\
      --host 192.168.1.100 \\
      --user root \\
      --key ~/.ssh/id_rsa \\
      --environment production \\
      --description "生产环境 Web 服务器" \\
      --tags web,nginx,production \\
      --location "阿里云华北"

    # 删除配置
    python ssh_config_manager_v3.py delete prod-web-01

    # 导出配置
    python ssh_config_manager_v3.py export --output backup.json
"""

import sys
import os
import json
import argparse
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path

import paramiko

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


class SSHConfigManager:
    """SSH Config 管理器（基于注释元数据）"""

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化管理器

        Args:
            config_path: SSH config 文件路径，默认 ~/.ssh/config
        """
        if config_path is None:
            config_path = os.path.expanduser("~/.ssh/config")

        self.config_path = config_path

        # 确保 .ssh 目录存在
        ssh_dir = os.path.dirname(config_path)
        os.makedirs(ssh_dir, exist_ok=True)

        # 确保 config 文件存在
        if not os.path.exists(config_path):
            Path(config_path).touch()

    def parse_metadata_from_comments(self, comment_lines: List[str]) -> dict:
        """
        从注释行中解析元数据

        Args:
            comment_lines: 注释行列表

        Returns:
            元数据字典
        """
        metadata = {}

        for line in comment_lines:
            line = line.strip()
            if not line.startswith('#'):
                continue

            # 移除开头的 #
            line = line[1:].strip()

            # 跳过分隔线
            if line.startswith('=====') or line == '':
                continue

            # 解析 key: value 格式
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()

                if key in ['description', 'environment', 'location', 'created_at', 'updated_at', 'password']:
                    metadata[key] = value
                elif key == 'tags':
                    # 标签用逗号分隔
                    metadata['tags'] = [t.strip() for t in value.split(',') if t.strip()]

        return metadata

    @staticmethod
    def _extract_host_aliases(host_line: str) -> List[str]:
        """从 Host 行提取别名列表，忽略通配符模式"""
        match = re.match(r'Host\s+(.+)', host_line.strip())
        if not match:
            return []

        aliases = []
        for alias in match.group(1).split():
            alias = alias.strip()
            if alias and '*' not in alias and '?' not in alias:
                aliases.append(alias)
        return aliases

    def read_config_with_metadata(self) -> List[Tuple[str, dict, List[str], List[str]]]:
        """
        读取配置文件，解析每个 Host 及其元数据

        Returns:
            [(alias, metadata, comment_lines, config_lines)] 列表
        """
        if not os.path.exists(self.config_path):
            return []

        results = []
        current_comments = []
        current_host_comments = []  # 当前 Host 的注释
        current_config = []
        current_aliases: List[str] = []
        in_host_block = False

        with open(self.config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        for line in lines:
            stripped = line.strip()

            # 检查是否是 Host 行
            if stripped.startswith('Host ') and not stripped.startswith('Host *'):
                # 保存上一个 Host 块
                if current_aliases:
                    metadata = self.parse_metadata_from_comments(current_host_comments)
                    for current_alias in current_aliases:
                        results.append((current_alias, metadata, current_host_comments, current_config))

                # 开始新的 Host 块
                current_aliases = self._extract_host_aliases(stripped)
                if current_aliases:
                    current_config = [line]
                    current_host_comments = current_comments  # 保存当前收集的注释给这个 Host
                    current_comments = []  # 清空，准备收集下一个 Host 的注释
                    in_host_block = True

            elif in_host_block:
                # 在 Host 块中
                if stripped and not stripped.startswith('#'):
                    # 配置行（缩进的）
                    if line.startswith((' ', '\t')):
                        current_config.append(line)
                    else:
                        # 遇到非缩进的非注释行，Host 块结束
                        in_host_block = False
                        if stripped.startswith('#'):
                            current_comments.append(line)
                elif stripped.startswith('#'):
                    # Host 块中的注释（通常不应该有）
                    current_config.append(line)
                elif not stripped:
                    # 空行，Host 块结束
                    current_config.append(line)
                    in_host_block = False

            else:
                # 不在 Host 块中，收集注释
                if stripped.startswith('#') or not stripped:
                    current_comments.append(line)
                else:
                    # 非注释非空行，清空注释缓存
                    current_comments = []

        # 保存最后一个 Host 块
        if current_aliases:
            metadata = self.parse_metadata_from_comments(current_host_comments)
            for current_alias in current_aliases:
                results.append((current_alias, metadata, current_host_comments, current_config))

        return results

    def parse_config(self) -> paramiko.SSHConfig:
        """解析 SSH config 文件"""
        ssh_config = paramiko.SSHConfig()
        with open(self.config_path, 'r', encoding='utf-8') as f:
            ssh_config.parse(f)
        return ssh_config

    def get_host_config(self, alias: str) -> Optional[dict]:
        """
        获取指定别名的配置

        Args:
            alias: 主机别名

        Returns:
            配置字典，未找到返回 None
        """
        try:
            # 先检查别名是否存在
            hosts = self.list_hosts()
            if alias not in hosts:
                return None

            ssh_config = self.parse_config()
            host_config = ssh_config.lookup(alias)

            return host_config
        except Exception:
            return None

    def list_hosts(self) -> List[str]:
        """
        列出所有 Host 别名

        Returns:
            Host 别名列表
        """
        hosts = []
        with open(self.config_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('Host ') and not line.startswith('Host *'):
                    hosts.extend(self._extract_host_aliases(line))
        return hosts

    def create_host(self, alias: str, hostname: str, user: str,
                   identity_file: Optional[str] = None, port: int = 22,
                   proxy_jump: Optional[str] = None,
                   environment: str = "development",
                   description: str = "",
                   tags: Optional[List[str]] = None,
                   location: str = "",
                   password: str = "") -> bool:
        """
        创建新的 Host 配置（带注释元数据）

        Args:
            alias: 主机别名
            hostname: 主机地址
            user: 用户名
            identity_file: 密钥文件路径
            port: 端口号
            proxy_jump: 跳板机别名
            environment: 环境类型
            description: 描述
            tags: 标签列表
            location: 物理位置
            password: 持久化密码

        Returns:
            是否成功创建
        """
        # 检查别名是否已存在
        if self.get_host_config(alias) is not None:
            raise ValueError(f"别名 '{alias}' 已存在")

        # 构建注释元数据块
        comment_lines = [
            f"\n# ===== {alias} =====\n"
        ]

        if description:
            comment_lines.append(f"# description: {description}\n")

        if environment:
            comment_lines.append(f"# environment: {environment}\n")

        if tags:
            comment_lines.append(f"# tags: {','.join(tags)}\n")

        if location:
            comment_lines.append(f"# location: {location}\n")

        if password:
            comment_lines.append(f"# password: {password}\n")

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        comment_lines.append(f"# created_at: {now}\n")
        comment_lines.append(f"# updated_at: {now}\n")

        # 构建配置块
        config_lines = [
            f"Host {alias}\n",
            f"    HostName {hostname}\n",
            f"    User {user}\n",
        ]

        if port != 22:
            config_lines.append(f"    Port {port}\n")

        if identity_file:
            config_lines.append(f"    IdentityFile {identity_file}\n")

        if proxy_jump:
            config_lines.append(f"    ProxyJump {proxy_jump}\n")

        # 添加到 config 文件
        with open(self.config_path, 'a', encoding='utf-8') as f:
            f.writelines(comment_lines)
            f.writelines(config_lines)

        return True

    def update_host(self, alias: str,
                   hostname: Optional[str] = None,
                   user: Optional[str] = None,
                   port: Optional[int] = None,
                   identity_file: Optional[str] = None,
                   proxy_jump: Optional[str] = None,
                   environment: Optional[str] = None,
                   description: Optional[str] = None,
                   tags: Optional[List[str]] = None,
                   location: Optional[str] = None,
                   password: Optional[str] = None) -> bool:
        """
        更新 Host 配置（包括注释元数据）

        Args:
            alias: 主机别名
            hostname: 主机地址（可选）
            user: 用户名（可选）
            port: 端口号（可选）
            identity_file: 密钥文件路径（可选）
            proxy_jump: 跳板机别名（可选）
            environment: 环境类型（可选）
            description: 描述（可选）
            tags: 标签列表（可选）
            location: 物理位置（可选）
            password: 持久化密码（可选）

        Returns:
            是否成功更新
        """
        # 检查别名是否存在
        if self.get_host_config(alias) is None:
            raise ValueError(f"别名 '{alias}' 不存在")

        # 读取配置文件
        with open(self.config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 查找并更新配置块
        new_lines = []
        i = 0
        skip_comments = []
        found = False

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # 收集注释行
            if stripped.startswith('#') or not stripped:
                skip_comments.append(line)
                i += 1
                continue

            # 检查是否是目标 Host 行
            if stripped.startswith('Host '):
                host_aliases = self._extract_host_aliases(stripped)
                if alias in host_aliases:
                    found = True
                    # 找到目标 Host，更新元数据注释
                    updated_comments = self._update_metadata_comments(
                        skip_comments, alias, environment, description, tags, location, password
                    )
                    new_lines.extend(updated_comments)
                    skip_comments = []

                    # 添加 Host 行
                    new_lines.append(line)
                    i += 1

                    # 更新配置行
                    config_lines = []
                    while i < len(lines):
                        next_line = lines[i]
                        next_stripped = next_line.strip()

                        # 如果遇到非缩进的非注释行，配置块结束
                        if next_stripped and not next_line.startswith((' ', '\t', '#')):
                            break

                        # 收集当前配置行
                        if next_line.startswith((' ', '\t')) and next_stripped:
                            config_lines.append(next_line)
                        elif not next_stripped:
                            # 空行，配置块结束
                            break

                        i += 1

                    # 更新配置行
                    updated_config = self._update_config_lines(
                        config_lines, hostname, user, port, identity_file, proxy_jump
                    )
                    new_lines.extend(updated_config)

                    continue
                else:
                    # 不是目标 Host，保留收集的注释和这个 Host
                    new_lines.extend(skip_comments)
                    skip_comments = []
                    new_lines.append(line)
                    i += 1
            else:
                # 其他行，保留收集的注释和这一行
                new_lines.extend(skip_comments)
                skip_comments = []
                new_lines.append(line)
                i += 1

        # 保留最后的注释（如果有）
        new_lines.extend(skip_comments)

        if not found:
            return False

        # 写回文件
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        return True

    def _update_metadata_comments(self, comment_lines: List[str], alias: str,
                                  environment: Optional[str],
                                  description: Optional[str],
                                  tags: Optional[List[str]],
                                  location: Optional[str],
                                  password: Optional[str]) -> List[str]:
        """更新元数据注释"""
        # 解析现有元数据
        metadata = self.parse_metadata_from_comments(comment_lines)

        # 更新元数据（只更新提供的字段）
        if environment is not None:
            metadata['environment'] = environment
        if description is not None:
            metadata['description'] = description
        if tags is not None:
            metadata['tags'] = tags
        if location is not None:
            metadata['location'] = location
        if password is not None:
            metadata['password'] = password

        # 更新时间戳
        metadata['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 保留 created_at（如果存在）
        if 'created_at' not in metadata:
            metadata['created_at'] = metadata['updated_at']

        # 重新构建注释块
        new_comments = [f"\n# ===== {alias} =====\n"]

        if metadata.get('description'):
            new_comments.append(f"# description: {metadata['description']}\n")

        if metadata.get('environment'):
            new_comments.append(f"# environment: {metadata['environment']}\n")

        if metadata.get('tags'):
            tags_str = ','.join(metadata['tags']) if isinstance(metadata['tags'], list) else metadata['tags']
            new_comments.append(f"# tags: {tags_str}\n")

        if metadata.get('location'):
            new_comments.append(f"# location: {metadata['location']}\n")

        if metadata.get('password'):
            new_comments.append(f"# password: {metadata['password']}\n")

        new_comments.append(f"# created_at: {metadata['created_at']}\n")
        new_comments.append(f"# updated_at: {metadata['updated_at']}\n")

        return new_comments

    def _update_config_lines(self, config_lines: List[str],
                            hostname: Optional[str],
                            user: Optional[str],
                            port: Optional[int],
                            identity_file: Optional[str],
                            proxy_jump: Optional[str]) -> List[str]:
        """更新配置行"""
        # 解析现有配置
        config_dict = {}
        for line in config_lines:
            stripped = line.strip()
            if not stripped:
                continue

            parts = stripped.split(None, 1)
            if len(parts) == 2:
                key, value = parts
                config_dict[key.lower()] = value

        # 更新配置（只更新提供的字段）
        if hostname is not None:
            config_dict['hostname'] = hostname
        if user is not None:
            config_dict['user'] = user
        if port is not None:
            config_dict['port'] = str(port)
        if identity_file is not None:
            config_dict['identityfile'] = identity_file
        if proxy_jump is not None:
            config_dict['proxyjump'] = proxy_jump

        # 重新构建配置行（保持标准顺序）
        new_config = []

        # 标准字段顺序
        field_order = ['hostname', 'user', 'port', 'identityfile', 'proxyjump', 'forwardagent']

        for field in field_order:
            if field in config_dict:
                # 首字母大写
                field_name = field.capitalize()
                if field == 'hostname':
                    field_name = 'HostName'
                elif field == 'identityfile':
                    field_name = 'IdentityFile'
                elif field == 'proxyjump':
                    field_name = 'ProxyJump'
                elif field == 'forwardagent':
                    field_name = 'ForwardAgent'

                new_config.append(f"    {field_name} {config_dict[field]}\n")

        # 添加其他未知字段
        for key, value in config_dict.items():
            if key not in field_order:
                field_name = key.capitalize()
                new_config.append(f"    {field_name} {value}\n")

        return new_config

    def delete_host(self, alias: str) -> bool:
        """
        删除 Host 配置（包括注释元数据）

        Args:
            alias: 主机别名

        Returns:
            是否成功删除
        """
        # 检查别名是否存在
        if self.get_host_config(alias) is None:
            return False

        # 读取配置文件
        with open(self.config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 查找并删除配置块（包括前面的注释）
        new_lines = []
        i = 0
        skip_comments = []

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # 收集注释行
            if stripped.startswith('#') or not stripped:
                skip_comments.append(line)
                i += 1
                continue

            # 检查是否是目标 Host 行
            if stripped.startswith('Host '):
                host_aliases = self._extract_host_aliases(stripped)
                if alias in host_aliases:
                    # 找到目标 Host，跳过它和它的配置行
                    i += 1
                    # 跳过缩进的配置行
                    while i < len(lines):
                        next_line = lines[i]
                        if next_line.strip() and not next_line.startswith((' ', '\t', '#')):
                            break
                        i += 1
                    # 清空收集的注释（这些注释属于被删除的 Host）
                    skip_comments = []
                    continue
                else:
                    # 不是目标 Host，保留收集的注释和这个 Host
                    new_lines.extend(skip_comments)
                    skip_comments = []
                    new_lines.append(line)
                    i += 1
            else:
                # 其他行，保留收集的注释和这一行
                new_lines.extend(skip_comments)
                skip_comments = []
                new_lines.append(line)
                i += 1

        # 保留最后的注释（如果有）
        new_lines.extend(skip_comments)

        # 写回文件
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        return True

    def find_host(self, query: str) -> List[Tuple[str, dict, dict]]:
        """
        查找主机（支持别名、描述、标签、位置模糊匹配）

        Args:
            query: 查询字符串

        Returns:
            [(alias, config, metadata)] 列表
        """
        results = []
        query_lower = query.lower()

        hosts_with_metadata = self.read_config_with_metadata()

        for alias, metadata, _, _ in hosts_with_metadata:
            # 精确匹配别名
            if alias.lower() == query_lower:
                config = self.get_host_config(alias)
                results.append((alias, config, metadata))
                continue

            # 模糊匹配别名
            if query_lower in alias.lower():
                config = self.get_host_config(alias)
                results.append((alias, config, metadata))
                continue

            # 模糊匹配描述
            if query_lower in metadata.get('description', '').lower():
                config = self.get_host_config(alias)
                results.append((alias, config, metadata))
                continue

            # 模糊匹配位置
            if query_lower in metadata.get('location', '').lower():
                config = self.get_host_config(alias)
                results.append((alias, config, metadata))
                continue

            # 模糊匹配标签
            if any(query_lower in tag.lower() for tag in metadata.get('tags', [])):
                config = self.get_host_config(alias)
                results.append((alias, config, metadata))

        return results

    def list_servers(self, environment: Optional[str] = None,
                    tags: Optional[List[str]] = None) -> List[Tuple[str, dict, dict]]:
        """
        列出所有服务器

        Args:
            environment: 按环境过滤
            tags: 按标签过滤

        Returns:
            [(alias, config, metadata)] 列表
        """
        results = []
        hosts_with_metadata = self.read_config_with_metadata()

        for alias, metadata, _, _ in hosts_with_metadata:
            # 环境过滤
            if environment and metadata.get('environment') != environment:
                continue

            # 标签过滤
            if tags:
                host_tags = metadata.get('tags', [])
                if not any(tag in host_tags for tag in tags):
                    continue

            config = self.get_host_config(alias)
            results.append((alias, config, metadata))

        return results

    def export_config(self) -> dict:
        """
        导出所有配置（用于备份）

        Returns:
            配置字典
        """
        export_data = {
            "version": "3.1",
            "exported_at": datetime.now().isoformat(),
            "hosts": []
        }

        hosts_with_metadata = self.read_config_with_metadata()

        for alias, metadata, _, _ in hosts_with_metadata:
            config = self.get_host_config(alias)

            host_data = {
                "alias": alias,
                "hostname": config.get('hostname'),
                "user": config.get('user'),
                "port": config.get('port', 22),
                "identity_file": (
                    config.get('identityfile')[0]
                    if isinstance(config.get('identityfile'), list)
                    else config.get('identityfile')
                ) if config.get('identityfile') else None,
                "proxy_jump": config.get('proxyjump'),
                "metadata": metadata
            }

            export_data['hosts'].append(host_data)

        return export_data


def _normalize_alias_for_env(alias: str) -> str:
    """将别名转换为环境变量后缀"""
    return re.sub(r'[^A-Za-z0-9]+', '_', alias).strip('_').upper()


def _has_runtime_password(alias: str) -> bool:
    """判断是否配置了运行时密码环境变量"""
    candidates = []
    normalized_alias = _normalize_alias_for_env(alias)
    if normalized_alias:
        candidates.extend([
            f'SSH_SKILL_PASSWORD_{normalized_alias}',
            f'SSH_PASSWORD_{normalized_alias}',
        ])

    candidates.extend([
        'SSH_SKILL_PASSWORD',
        'SSH_PASSWORD',
    ])

    return any(os.environ.get(env_name) for env_name in candidates)


def _get_auth_method(alias: str, config, metadata: Optional[dict] = None) -> str:
    """判断认证方式"""
    identity_files = (config or {}).get('identityfile')
    if isinstance(identity_files, list):
        has_key = bool(identity_files and identity_files[0])
    else:
        has_key = bool(identity_files)
    has_runtime_password = _has_runtime_password(alias)
    has_persisted_password = bool((metadata or {}).get('password'))

    if has_key and has_runtime_password:
        return "运行时密码+密钥"
    if has_key and has_persisted_password:
        return "持久化密码+密钥"
    if has_runtime_password:
        return "运行时密码"
    if has_persisted_password:
        return "持久化密码"
    if has_key:
        return "密钥"
    return "未配置"


def cmd_list_servers(args):
    """列出所有服务器"""
    operation = 'list_servers'
    target = args.environment or ','.join(args.tags or []) or 'all'
    try:
        manager = SSHConfigManager()
        servers = manager.list_servers(
            environment=args.environment,
            tags=args.tags
        )

        result_list = []
        for alias, config, meta in servers:
            result_list.append({
                'alias': alias,
                'hostname': config.get('hostname'),
                'user': config.get('user'),
                'port': config.get('port', 22),
                'description': meta.get('description', ''),
                'tags': meta.get('tags', []),
                'location': meta.get('location', ''),
                'auth': _get_auth_method(alias, config, meta),
            })

        emit_json(_build_success(
            operation=operation,
            target=target,
            args=args,
            result={
                'count': len(result_list),
                'servers': result_list,
                **({'message': '未找到服务器'} if not result_list else {}),
            },
            environment=args.environment,
            tags=args.tags,
            config_path=manager.config_path,
        ), args=args, ensure_ascii=False)
        return 0

    except Exception as e:
        emit_json(_build_failure(
            operation=operation,
            target=target,
            code='internal_error',
            message='列出服务器失败',
            details={
                'environment': args.environment,
                'tags': args.tags,
            },
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1


def cmd_find(args):
    """查找服务器"""
    operation = 'find_server'
    try:
        manager = SSHConfigManager()
        results = manager.find_host(args.query)

        result_list = []
        for alias, config, meta in results:
            result_list.append({
                'alias': alias,
                'hostname': config.get('hostname'),
                'user': config.get('user'),
                'port': config.get('port', 22),
                'environment': meta.get('environment', 'unknown'),
                'description': meta.get('description', ''),
                'tags': meta.get('tags', []),
                'location': meta.get('location', ''),
                'auth': _get_auth_method(alias, config, meta)
            })

        emit_json(_build_success(
            operation=operation,
            target=args.query,
            args=args,
            result={
                'count': len(result_list),
                'results': result_list,
                **({'message': f'未找到匹配 "{args.query}" 的服务器'} if not result_list else {}),
            },
            query=args.query,
        ), args=args, ensure_ascii=False)
        return 0

    except Exception as e:
        emit_json(_build_failure(
            operation=operation,
            target=args.query,
            code='internal_error',
            message='查找服务器失败',
            details={'query': args.query},
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1


def cmd_create(args):
    """创建服务器配置"""
    operation = 'create_server'
    try:
        manager = SSHConfigManager()

        success = manager.create_host(
            alias=args.alias,
            hostname=args.host,
            user=args.user,
            identity_file=args.key,
            port=args.port,
            proxy_jump=args.jump,
            environment=args.environment,
            description=args.description or "",
            tags=args.tags or [],
            location=args.location or "",
            password=args.password or ""
        )

        if not success:
            emit_json(_build_failure(
                operation=operation,
                target=args.alias,
                code='internal_error',
                message='创建失败',
                details={'alias': args.alias},
                retriable=False,
            ), args=args, stream=sys.stderr, ensure_ascii=False)
            return 1

        emit_json(_build_success(
            operation=operation,
            target=args.alias,
            args=args,
            result={
                'message': f'服务器 {args.alias} 创建成功',
                'alias': args.alias,
                'config_file': manager.config_path,
            },
            alias=args.alias,
            host=args.host,
            user=args.user,
            port=args.port,
            jump=args.jump,
            environment=args.environment,
            tags=args.tags,
            location=args.location,
            password=args.password,
        ), args=args, ensure_ascii=False)
        return 0

    except ValueError as e:
        emit_json(_build_failure(
            operation=operation,
            target=args.alias,
            code='cli_argument_error',
            message=str(e),
            details={'alias': args.alias},
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1
    except Exception as e:
        emit_json(_build_failure(
            operation=operation,
            target=args.alias,
            code='internal_error',
            message='创建服务器配置失败',
            details={'alias': args.alias},
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1


def cmd_update(args):
    """更新服务器配置"""
    operation = 'update_server'
    try:
        manager = SSHConfigManager()

        update_kwargs = {'alias': args.alias}

        if args.host is not None:
            update_kwargs['hostname'] = args.host
        if args.user is not None:
            update_kwargs['user'] = args.user
        if args.port is not None:
            update_kwargs['port'] = args.port
        if args.key is not None:
            update_kwargs['identity_file'] = args.key
        if args.jump is not None:
            update_kwargs['proxy_jump'] = args.jump
        if args.environment is not None:
            update_kwargs['environment'] = args.environment
        if args.description is not None:
            update_kwargs['description'] = args.description
        if args.tags is not None:
            update_kwargs['tags'] = args.tags
        if args.location is not None:
            update_kwargs['location'] = args.location
        if args.password is not None:
            update_kwargs['password'] = args.password

        success = manager.update_host(**update_kwargs)

        if not success:
            emit_json(_build_failure(
                operation=operation,
                target=args.alias,
                code='target_resolution_error',
                message='更新失败',
                details={'alias': args.alias},
                retriable=False,
            ), args=args, stream=sys.stderr, ensure_ascii=False)
            return 1

        emit_json(_build_success(
            operation=operation,
            target=args.alias,
            args=args,
            result={
                'message': f'服务器 {args.alias} 更新成功',
                'alias': args.alias,
                'config_file': manager.config_path,
            },
            alias=args.alias,
            host=args.host,
            user=args.user,
            port=args.port,
            jump=args.jump,
            environment=args.environment,
            tags=args.tags,
            location=args.location,
            password=args.password,
        ), args=args, ensure_ascii=False)
        return 0

    except ValueError as e:
        emit_json(_build_failure(
            operation=operation,
            target=args.alias,
            code='cli_argument_error',
            message=str(e),
            details={'alias': args.alias},
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1
    except Exception as e:
        emit_json(_build_failure(
            operation=operation,
            target=args.alias,
            code='internal_error',
            message='更新服务器配置失败',
            details={'alias': args.alias},
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1


def cmd_delete(args):
    """删除服务器配置"""
    operation = 'delete_server'
    try:
        manager = SSHConfigManager()
        success = manager.delete_host(args.alias)

        if not success:
            emit_json(_build_failure(
                operation=operation,
                target=args.alias,
                code='target_resolution_error',
                message=f'服务器 {args.alias} 不存在',
                details={'alias': args.alias},
                retriable=False,
            ), args=args, stream=sys.stderr, ensure_ascii=False)
            return 1

        emit_json(_build_success(
            operation=operation,
            target=args.alias,
            args=args,
            result={
                'message': f'服务器 {args.alias} 已删除',
                'alias': args.alias,
            },
            alias=args.alias,
        ), args=args, ensure_ascii=False)
        return 0

    except Exception as e:
        emit_json(_build_failure(
            operation=operation,
            target=args.alias,
            code='internal_error',
            message='删除服务器配置失败',
            details={'alias': args.alias},
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1


def cmd_export(args):
    """导出配置"""
    operation = 'export_config'
    target = args.output or 'stdout'
    try:
        manager = SSHConfigManager()
        export_data = manager.export_config()

        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)

        emit_json(_build_success(
            operation=operation,
            target=target,
            args=args,
            result={
                'message': f'配置已导出到 {args.output}' if args.output else '配置导出成功',
                'count': len(export_data['hosts']),
                'export': export_data,
            },
            output=args.output,
            config_path=manager.config_path,
        ), args=args, ensure_ascii=False)
        return 0

    except Exception as e:
        emit_json(_build_failure(
            operation=operation,
            target=target,
            code='internal_error',
            message='导出配置失败',
            details={'output': args.output},
            cause=str(e),
            retriable=False,
        ), args=args, stream=sys.stderr, ensure_ascii=False)
        return 1
def main():
    parser = argparse.ArgumentParser(
        description='SSH Config 管理工具 v3.1（基于注释元数据）',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_reporting_arguments(parser)

    subparsers = parser.add_subparsers(dest='command', help='子命令')

    # list-servers 命令
    list_parser = subparsers.add_parser('list-servers', help='列出所有服务器')
    list_parser.add_argument('--environment', help='按环境过滤')
    list_parser.add_argument('--tags', nargs='+', help='按标签过滤')

    # find 命令
    find_parser = subparsers.add_parser('find', help='查找服务器')
    find_parser.add_argument('query', help='查询字符串')

    # create 命令
    create_parser = subparsers.add_parser('create', help='创建服务器配置')
    create_parser.add_argument('--alias', required=True, help='主机别名')
    create_parser.add_argument('--host', required=True, help='主机地址')
    create_parser.add_argument('--user', required=True, help='用户名')
    create_parser.add_argument('--key', help='密钥文件路径')
    create_parser.add_argument('--port', type=int, default=22, help='端口号')
    create_parser.add_argument('--jump', help='跳板机别名')
    create_parser.add_argument('--environment', default='development', help='环境类型')
    create_parser.add_argument('--description', help='描述')
    create_parser.add_argument('--tags', nargs='+', help='标签列表')
    create_parser.add_argument('--location', help='物理位置')
    create_parser.add_argument('--password', help='持久化密码')

    # update 命令
    update_parser = subparsers.add_parser('update', help='更新服务器配置')
    update_parser.add_argument('alias', help='主机别名')
    update_parser.add_argument('--host', help='主机地址')
    update_parser.add_argument('--user', help='用户名')
    update_parser.add_argument('--key', help='密钥文件路径')
    update_parser.add_argument('--port', type=int, help='端口号')
    update_parser.add_argument('--jump', help='跳板机别名')
    update_parser.add_argument('--environment', help='环境类型')
    update_parser.add_argument('--description', help='描述')
    update_parser.add_argument('--tags', nargs='+', help='标签列表')
    update_parser.add_argument('--location', help='物理位置')
    update_parser.add_argument('--password', help='持久化密码')

    # delete 命令
    delete_parser = subparsers.add_parser('delete', help='删除服务器配置')
    delete_parser.add_argument('alias', help='主机别名')

    # export 命令
    export_parser = subparsers.add_parser('export', help='导出配置')
    export_parser.add_argument('--output', help='输出文件路径')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == 'list-servers':
        return cmd_list_servers(args)
    if args.command == 'find':
        return cmd_find(args)
    if args.command == 'create':
        return cmd_create(args)
    if args.command == 'update':
        return cmd_update(args)
    if args.command == 'delete':
        return cmd_delete(args)
    if args.command == 'export':
        return cmd_export(args)

    return 1


if __name__ == '__main__':
    sys.exit(main())
