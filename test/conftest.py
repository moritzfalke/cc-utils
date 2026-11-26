# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import socket
import sys

import pytest
import requests


@pytest.fixture
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Unit tests must not perform network I/O')

    async def denied_async(*args, **kwargs):
        denied(*args, **kwargs)

    monkeypatch.setattr(requests.Session, 'request', denied)
    if aiohttp := sys.modules.get('aiohttp'):
        monkeypatch.setattr(aiohttp.ClientSession, '_request', denied_async)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
