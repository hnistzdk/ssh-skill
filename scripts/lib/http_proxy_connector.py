#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP proxy connector placeholder.

Concrete deployments can implement this connector against their proxy protocol
while preserving the same ExecutionResult contract as SSH connectors.
"""

from __future__ import annotations

from typing import Dict

from connector_base import RemoteConnector
from execution_result import ConnectorCapabilities, ExecutionResult


class HttpProxyConnector(RemoteConnector):
    name = 'http-proxy'
    capabilities = ConnectorCapabilities(
        stream_stdout=False,
        stream_stderr=False,
        separate_stderr=False,
        exit_code=False,
        stdin_script=False,
        persistent=False,
    )

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def execute(self, exec_spec: Dict, timeout: int) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            exit_code=-1,
            stderr='HTTP proxy connector is not configured for this proxy protocol',
            error_type='unsupported_connector',
            error_message='HTTP proxy connector requires a concrete proxy protocol adapter',
            connector=self.name,
            method=self.name,
        )
