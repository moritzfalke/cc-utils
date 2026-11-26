# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import asyncio
import io

import aiohttp
import pytest
import requests

import oci.digest as digest
from test._test_utils import async_http_response, content_digest, http_response


pytestmark = pytest.mark.usefixtures('no_network')
REFERENCE = 'registry.example/image:v1'


@pytest.mark.parametrize('algorithm', ['sha256', 'sha512'])
@pytest.mark.parametrize('content', [b'', b'example payload'])
def test_digest_verifier_accepts_matching_chunks(algorithm, content):
    verifier = digest.DigestVerifier(content_digest(content, algorithm), REFERENCE)
    verifier.update(content[:3])
    verifier.update(content[3:])
    verifier.verify()


@pytest.mark.parametrize('value', [
    None, 'sha256', 'sha256:abcd', 'sha256:' + 'g' * 64,
    'sha1:' + '0' * 40, 'unknown:' + '0' * 64,
])
def test_digest_verifier_rejects_invalid_specs(value):
    with pytest.raises(ValueError):
        digest.DigestVerifier(value, REFERENCE)


def test_digest_mismatch_reports_context():
    expected = content_digest(b'expected payload')
    verifier = digest.DigestVerifier(expected, REFERENCE)
    verifier.update(b'other payload')
    with pytest.raises(digest.DigestMismatchError) as error:
        verifier.verify()
    assert error.value.expected_digest == expected
    assert error.value.actual_digest == content_digest(b'other payload')
    assert error.value.image_reference == REFERENCE


@pytest.mark.parametrize('matching', [False, True])
def test_sync_verification_spools_and_closes_buffers(matching, monkeypatch, tmp_path):
    content = b'example payload' * 20
    received = content if matching else b'other payload' * 20
    buffers = []
    original = digest.tempfile.SpooledTemporaryFile

    def spool(**kwargs):
        buffer = original(**kwargs, dir=tmp_path)
        buffers.append(buffer)
        return buffer

    monkeypatch.setattr(digest, 'SPOOL_MAX_SIZE', 16)
    monkeypatch.setattr(digest.tempfile, 'SpooledTemporaryFile', spool)
    response = http_response(received)
    verifier = digest.DigestVerifier(content_digest(content), REFERENCE)
    if matching:
        with digest.verify_response(response, verifier) as verified:
            assert verified.raw.read(3) == content[:3]
            assert b''.join(verified.iter_content(7)) == content[3:]
    else:
        with pytest.raises(digest.DigestMismatchError):
            digest.verify_response(response, verifier)
    assert buffers[0]._rolled
    assert buffers[0].closed
    assert response.raw.closed


def test_sync_verification_closes_an_interrupted_response(monkeypatch):
    response = http_response(b'example payload')

    def chunks(chunk_size):
        yield b'example '
        raise requests.exceptions.ChunkedEncodingError('Incomplete response')

    monkeypatch.setattr(response, 'iter_content', chunks)
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        digest.verify_response(
            response, digest.DigestVerifier(content_digest(b'example payload'), REFERENCE),
        )
    assert response.raw.closed


@pytest.mark.parametrize('error_type', [aiohttp.ClientPayloadError, asyncio.CancelledError])
def test_async_verification_closes_on_interruption(error_type, monkeypatch, tmp_path):
    buffers = []
    original = digest.tempfile.SpooledTemporaryFile

    def spool(**kwargs):
        buffer = original(**kwargs, dir=tmp_path)
        buffers.append(buffer)
        return buffer

    monkeypatch.setattr(digest.tempfile, 'SpooledTemporaryFile', spool)

    async def run():
        response = await async_http_response(b'example payload')

        async def chunks(stream, n):
            yield b'example '
            raise error_type('Incomplete response')

        monkeypatch.setattr(aiohttp.StreamReader, 'iter_chunked', chunks)
        with pytest.raises(error_type):
            await digest.verify_response_async(
                response, digest.DigestVerifier(content_digest(b'example payload'), REFERENCE),
            )
        assert buffers[0].closed
        assert response.closed

    asyncio.run(run())


def test_verified_async_content_read_interfaces():
    async def run():
        content = digest.VerifiedAsyncContent(io.BytesIO(b'first\nsecond\nlast'), 17)
        assert content.is_eof()
        assert not content.at_eof()
        assert await content.readline() == b'first\n'
        assert [line async for line in content] == [b'second\n', b'last']
        assert content.at_eof()
        content.buffer.seek(0)
        assert await content.readexactly(5) == b'first'
        with pytest.raises(asyncio.IncompleteReadError):
            await content.readexactly(100)
        with pytest.raises(ValueError):
            await content.readuntil(b'')
        with pytest.raises(ValueError):
            await content.readexactly(-1)
        error = aiohttp.ClientConnectionError('Response closed')
        content.set_exception(error)
        assert content.buffer.closed
        assert content.exception() is error
        with pytest.raises(aiohttp.ClientConnectionError):
            await content.read()

    asyncio.run(run())
