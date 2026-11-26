# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import asyncio
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

import oci.client_async as client_module
import oci.digest
from test._test_utils import async_http_response, content_digest


pytestmark = pytest.mark.usefixtures('no_network')


@pytest.mark.parametrize('method', ['manifest', 'blob'])
@pytest.mark.parametrize('matching', [False, True])
def test_download_content_checks(method, matching, monkeypatch):
    async def run():
        content = b'{"schemaVersion":2}'
        response = await async_http_response(content if matching else b'{"schemaVersion":1}')
        client = client_module.Client(session=Mock(spec=aiohttp.ClientSession))
        monkeypatch.setattr(client, '_request', AsyncMock(return_value=response))

        async def download():
            if method == 'manifest':
                return await client.manifest_raw(f'registry.example/image@{content_digest(content)}')
            return await client.blob('registry.example/image:v1', content_digest(content))

        if matching:
            async with await download() as result:
                assert isinstance(result, aiohttp.ClientResponse)
                assert await result.read() == content
                assert await result.read() == content
                assert await result.text() == content.decode()
                assert await result.json() == {'schemaVersion': 2}
        else:
            with pytest.raises(oci.digest.DigestMismatchError):
                await download()
        assert response.closed

    asyncio.run(run())


@pytest.mark.parametrize('shape', ['symbolic', 'digest', 'mixed'])
def test_manifest_reference_resolution(shape, monkeypatch):
    async def run():
        content = b'{"schemaVersion":2}'
        expected = content_digest(content)
        reference = {
            'symbolic': 'registry.example/image:v1',
            'digest': f'registry.example/image@{expected}',
            'mixed': f'registry.example/image:v1@{expected}',
        }[shape]
        response = await async_http_response(content)
        client = client_module.Client(session=Mock(spec=aiohttp.ClientSession))
        request = AsyncMock(return_value=response)
        monkeypatch.setattr(client, '_request', request)
        assert await client.to_digest_hash(reference) == (
            f'registry.example/image@{expected}' if shape == 'symbolic' else reference
        )
        request.assert_awaited_once()
        assert response.closed

    asyncio.run(run())


@pytest.mark.parametrize('algorithm', ['sha256', 'sha512'])
def test_blob_chunked_reads_and_cleanup(algorithm, monkeypatch, tmp_path):
    async def run():
        content = b'example payload' * 20
        response = await async_http_response(content)
        original_stream = response.content
        client = client_module.Client(session=Mock(spec=aiohttp.ClientSession))
        monkeypatch.setattr(client, '_request', AsyncMock(return_value=response))
        original = oci.digest.tempfile.SpooledTemporaryFile
        monkeypatch.setattr(oci.digest, 'SPOOL_MAX_SIZE', 16)
        monkeypatch.setattr(
            oci.digest.tempfile, 'SpooledTemporaryFile',
            lambda **kwargs: original(**kwargs, dir=tmp_path),
        )
        async with await client.blob(
            'registry.example/image:v1', content_digest(content, algorithm),
        ) as r:
            assert original_stream.at_eof()
            assert r.content.buffer._rolled
            assert await r.content.readexactly(3) == content[:3]
            assert b''.join([chunk async for chunk in r.content.iter_chunked(4)]) == content[3:]
        assert r.content.buffer.closed

    asyncio.run(run())


def test_missing_blob_response(monkeypatch):
    async def run():
        response = await async_http_response(b'', status=404)
        client = client_module.Client(session=Mock(spec=aiohttp.ClientSession))
        monkeypatch.setattr(client, '_request', AsyncMock(return_value=response))
        result = await client.blob('registry.example/image:v1', content_digest(b''), absent_ok=True)
        assert result is None
        assert response.closed

    asyncio.run(run())


@pytest.mark.parametrize('shape', ['digest', 'mixed'])
@pytest.mark.parametrize('algorithm', ['sha256', 'sha512'])
def test_reference_resolution_checks_content(shape, algorithm, monkeypatch):
    async def run():
        expected = content_digest(b'example payload', algorithm)
        tag = ':v1' if shape == 'mixed' else ''
        response = await async_http_response(b'other payload')
        client = client_module.Client(session=Mock(spec=aiohttp.ClientSession))
        monkeypatch.setattr(client, '_request', AsyncMock(return_value=response))
        with pytest.raises(oci.digest.DigestMismatchError) as error:
            await client.to_digest_hash(f'registry.example/image{tag}@{expected}')
        assert error.value.expected_digest == expected
        assert error.value.actual_digest == content_digest(b'other payload', algorithm)
        assert response.closed

    asyncio.run(run())
