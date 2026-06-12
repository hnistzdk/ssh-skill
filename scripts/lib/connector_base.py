#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Connector extension points for ssh-skill."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

from execution_result import ConnectorCapabilities, ExecutionResult


class RemoteConnector(ABC):
    name = 'unknown'
    capabilities = ConnectorCapabilities()

    @abstractmethod
    def execute(self, exec_spec: Dict, timeout: int) -> ExecutionResult:
        raise NotImplementedError


class ConnectorRegistry:
    def __init__(self):
        self._connectors = {}

    def register(self, connector: RemoteConnector):
        self._connectors[connector.name] = connector

    def get(self, name: str) -> RemoteConnector:
        try:
            return self._connectors[name]
        except KeyError:
            raise ValueError(f'Unsupported connector: {name}')

    def names(self):
        return sorted(self._connectors.keys())
