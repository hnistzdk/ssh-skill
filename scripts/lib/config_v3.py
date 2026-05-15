"""
配置管理模块 v3.0

基于标准 OpenSSH config 格式的配置加载器

新特性：
1. 从 ~/.ssh/config 加载配置
2. 支持别名（Host）
3. 元数据从注释中解析
4. 完全兼容 ProxyJump（跳板机）
5. 支持从注释持久化密码，并允许环境变量覆盖
"""

import os
import re
from typing import Optional, List

try:
    import paramiko
except ImportError:
    raise ImportError("需要安装 paramiko 库: pip install paramiko")


class SSHConfigLoaderV3:
    """SSH Config 加载器 v3.0

    从标准 OpenSSH config 文件加载配置
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化加载器

        Args:
            config_path: SSH config 文件路径，默认 ~/.ssh/config
        """
        if config_path is None:
            config_path = os.path.expanduser("~/.ssh/config")

        self.config_path = config_path

    def load_ssh_config(self, alias: str) -> dict:
        """
        从 SSH config 加载指定别名的配置

        Args:
            alias: 主机别名

        Returns:
            配置字典

        Raises:
            FileNotFoundError: 配置文件不存在
            ValueError: 别名不存在
        """
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"SSH config 文件不存在: {self.config_path}")

        ssh_config = paramiko.SSHConfig()
        with open(self.config_path, 'r', encoding='utf-8') as f:
            ssh_config.parse(f)

        try:
            host_config = ssh_config.lookup(alias)
        except Exception as e:
            raise ValueError(f"无法解析别名 '{alias}': {e}")

        if host_config.get('hostname') == alias and not self._alias_exists(alias):
            raise ValueError(f"别名 '{alias}' 不存在于 SSH config 中")

        return host_config

    @staticmethod
    def _extract_host_aliases(host_line: str) -> List[str]:
        """从 Host 行提取别名列表，忽略通配符模式"""
        match = re.match(r'Host\s+(.+)', host_line.strip())
        if not match:
            return []

        aliases = []
        for host_alias in match.group(1).split():
            host_alias = host_alias.strip()
            if host_alias and '*' not in host_alias and '?' not in host_alias:
                aliases.append(host_alias)
        return aliases

    @staticmethod
    def _normalize_alias_for_env(alias: str) -> str:
        """将别名转换为环境变量后缀"""
        return re.sub(r'[^A-Za-z0-9]+', '_', alias).strip('_').upper()

    def _load_runtime_password(self, alias: str) -> str:
        """从环境变量加载运行时密码"""
        candidates = []
        normalized_alias = self._normalize_alias_for_env(alias)
        if normalized_alias:
            candidates.extend([
                f'SSH_SKILL_PASSWORD_{normalized_alias}',
                f'SSH_PASSWORD_{normalized_alias}',
            ])

        candidates.extend([
            'SSH_SKILL_PASSWORD',
            'SSH_PASSWORD',
        ])

        for env_name in candidates:
            value = os.environ.get(env_name)
            if value:
                return value

        return ''

    def _alias_exists(self, alias: str) -> bool:
        """检查别名是否存在于 SSH config 中"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('Host ') and not line.startswith('Host *'):
                        if alias in self._extract_host_aliases(line):
                            return True
            return False
        except Exception:
            return False

    def _resolve_password(self, alias: str, metadata: dict) -> str:
        """解析密码：环境变量优先，其次使用注释中的持久化密码"""
        runtime_password = self._load_runtime_password(alias)
        if runtime_password:
            return runtime_password

        persisted_password = metadata.get('password')
        if persisted_password:
            return persisted_password

        return ''

    def _build_jump_hosts(self, proxy_jump: str) -> List[dict]:
        """将 ProxyJump 配置解析为 Paramiko 可用的跳板机链"""
        jump_hosts: List[dict] = []
        seen = set()

        def append_alias(alias: str):
            if alias in seen:
                raise ValueError(f"检测到循环 ProxyJump 配置: {alias}")
            seen.add(alias)

            config = self.load_ssh_config(alias)
            metadata = self.load_metadata(alias)

            nested_proxy_jump = config.get('proxyjump')
            if nested_proxy_jump:
                for nested_alias in nested_proxy_jump.split(','):
                    nested_alias = nested_alias.strip()
                    if nested_alias:
                        append_alias(nested_alias)

            jump_host = {
                'host': config.get('hostname'),
                'user': config.get('user'),
                'port': int(config.get('port', 22)),
            }

            identity_files = config.get('identityfile')
            if identity_files:
                if isinstance(identity_files, list):
                    jump_host['key_file'] = identity_files[0]
                else:
                    jump_host['key_file'] = identity_files

            password = self._resolve_password(alias, metadata)
            if password:
                jump_host['password'] = password

            jump_hosts.append(jump_host)

        for jump_alias in proxy_jump.split(','):
            jump_alias = jump_alias.strip()
            if jump_alias:
                append_alias(jump_alias)

        return jump_hosts

    def load_metadata(self, alias: str) -> dict:
        """
        从注释中加载元数据

        Args:
            alias: 主机别名

        Returns:
            元数据字典
        """
        metadata = {
            'description': '',
            'environment': 'unknown',
            'tags': [],
            'location': '',
            'password': '',
        }

        if not os.path.exists(self.config_path):
            return metadata

        with open(self.config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        host_line_index = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('Host ') and not stripped.startswith('Host *'):
                if alias in self._extract_host_aliases(stripped):
                    host_line_index = i
                    break

        if host_line_index == -1:
            return metadata

        comment_lines = []
        i = host_line_index - 1
        while i >= 0:
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith('#') or not stripped:
                comment_lines.insert(0, line)
                i -= 1
            else:
                break

        for line in comment_lines:
            line = line.strip()
            if not line.startswith('#'):
                continue

            line = line[1:].strip()

            if line.startswith('=====') or line == '':
                continue

            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()

                if key == 'description':
                    metadata['description'] = value
                elif key == 'environment':
                    metadata['environment'] = value
                elif key == 'tags':
                    metadata['tags'] = [t.strip() for t in value.split(',') if t.strip()]
                elif key == 'location':
                    metadata['location'] = value
                elif key == 'password':
                    metadata['password'] = value

        return metadata

    def get_connection_params(self, alias: str) -> dict:
        """
        获取连接参数（用于创建 SSH 客户端）

        Args:
            alias: 主机别名

        Returns:
            连接参数字典（包括密码）
        """
        config = self.load_ssh_config(alias)
        metadata = self.load_metadata(alias)

        params = {
            'hostname': config.get('hostname'),
            'user': config.get('user'),
            'port': int(config.get('port', 22)),
            'timeout': 30,
        }

        identity_files = config.get('identityfile')
        if identity_files:
            if isinstance(identity_files, list):
                params['key_file'] = identity_files[0]
            else:
                params['key_file'] = identity_files

        password = self._resolve_password(alias, metadata)
        if password:
            params['password'] = password

        proxy_jump = config.get('proxyjump')
        if proxy_jump:
            params['proxy_jump'] = proxy_jump
            params['jump_hosts'] = self._build_jump_hosts(proxy_jump)

        forward_agent = config.get('forwardagent', 'no').lower()
        params['forward_agent'] = forward_agent in ('yes', 'true', '1')

        params['metadata'] = metadata
        params['alias'] = alias

        return params

    def from_alias(self, alias: str):
        """
        通过别名创建 SSH 客户端（智能选择）

        策略：
        - 有密钥文件且无密码 → 使用 NativeSSHClient（原生 SSH）
        - 有密码（环境变量或持久化注释） → 使用 ParamikoClient（Paramiko）

        Args:
            alias: 主机别名

        Returns:
            NativeSSHClient 或 ParamikoClient 实例
        """
        params = self.get_connection_params(alias)

        has_key = bool(params.get('key_file'))
        has_password = bool(params.get('password'))

        if has_key and not has_password:
            try:
                from .native_ssh_client import NativeSSHClient
            except ImportError:
                from native_ssh_client import NativeSSHClient

            client = NativeSSHClient(
                host=params['hostname'],
                user=params['user'],
                port=params['port'],
                key_file=params.get('key_file'),
                timeout=params['timeout'],
                proxy_jump=params.get('proxy_jump'),
                forward_agent=params.get('forward_agent', False),
                alias=alias
            )
        else:
            try:
                from .paramiko_client import ParamikoClient
            except ImportError:
                from paramiko_client import ParamikoClient

            client = ParamikoClient(
                host=params['hostname'],
                user=params['user'],
                port=params['port'],
                password=params.get('password'),
                key_file=params.get('key_file'),
                timeout=params['timeout'],
                jump_hosts=params.get('jump_hosts'),
                forward_agent=params.get('forward_agent', False)
            )

        client.alias = alias

        return client

    @staticmethod
    def get_default_config_path() -> str:
        """获取默认 SSH config 路径"""
        return os.path.expanduser("~/.ssh/config")


def get_config_loader_v3(config_path: Optional[str] = None) -> SSHConfigLoaderV3:
    """
    获取配置加载器实例

    Args:
        config_path: SSH config 文件路径

    Returns:
        SSHConfigLoaderV3 实例
    """
    return SSHConfigLoaderV3(config_path)
