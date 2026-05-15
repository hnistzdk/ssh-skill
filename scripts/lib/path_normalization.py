"""
路径规范化工具。

用于统一处理 SSH 脚本中的远程路径输入校验，避免各入口脚本重复实现。
"""

import re
from typing import Dict


class PathNormalizationError(ValueError):
    """远程路径规范化错误。"""

    def __init__(self, message: str, path: str, role: str = "remote_path"):
        super().__init__(message)
        self.code = "path_normalization_error"
        self.path = path
        self.role = role

    def to_error(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "details": {
                "path": self.path,
                "role": self.role,
            },
            "cause": None,
            "retriable": False,
        }


def normalize_remote_path(path: str, role: str = "remote_path") -> str:
    """
    校验远程路径输入，识别明显的 Windows/MSYS 误转换。

    规则：
    - 拒绝形如 C:/... 或 C:\... 的路径（通常是 MSYS 对远程 Unix 路径的错误转换）。
    """
    if path is None:
        raise PathNormalizationError(
            "Remote path is required",
            path="",
            role=role,
        )

    normalized = path.strip()
    if not normalized:
        raise PathNormalizationError(
            "Remote path cannot be empty",
            path=path,
            role=role,
        )

    if re.match(r"^[A-Za-z]:[/\\]", normalized):
        raise PathNormalizationError(
            f"Remote path looks like a Windows path (MSYS conversion): {normalized}. Use MSYS_NO_PATHCONV=1 prefix or quote the path.",
            path=normalized,
            role=role,
        )

    return normalized
