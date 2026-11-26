# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import asyncio
import hashlib
import hmac
import re
import tempfile


# Bound memory usage while withholding downloaded bytes until their digest is verified.
SPOOL_MAX_SIZE = 8 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


class DigestMismatchError(ValueError):
    def __init__(self, expected_digest: str, actual_digest: str, image_reference):
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        self.image_reference = str(image_reference)
        super().__init__(
            f'Digest mismatch for {self.image_reference}: '
            f'expected {expected_digest}, received {actual_digest}'
        )


class DigestVerifier:
    '''Validate the requested digest, then compare it with the hash of actual response bytes.'''

    def __init__(self, digest: str, image_reference):
        if not isinstance(digest, str):
            raise ValueError(f'Malformed OCI digest: {digest!r}')
        algorithm, separator, value = digest.partition(':')
        if not separator or algorithm not in ('sha256', 'sha512'):
            raise ValueError(f'Unsupported OCI digest: {digest!r}')
        self.hasher = hashlib.new(algorithm)
        if len(value) != self.hasher.digest_size * 2 or not re.fullmatch('[0-9a-fA-F]+', value):
            raise ValueError(f'Malformed OCI digest: {digest!r}')
        self.expected_digest = f'{algorithm}:{value.lower()}'
        self.image_reference = image_reference

    def update(self, content: bytes):
        self.hasher.update(content)

    def verify(self):
        actual_digest = f'{self.hasher.name}:{self.hasher.hexdigest()}'
        if not hmac.compare_digest(self.expected_digest, actual_digest):
            raise DigestMismatchError(self.expected_digest, actual_digest, self.image_reference)


class VerifiedRaw:
    '''File-backed, verified replacement for a requests response's raw download stream.'''

    def __init__(self, buffer):
        self.buffer = buffer

    def __getattr__(self, name):
        return getattr(self.buffer, name)

    def read(self, amt=None, decode_content=None, **kwargs):
        return self.buffer.read(-1 if amt is None else amt)

    def stream(self, amt=CHUNK_SIZE, decode_content=None):
        while chunk := self.read(amt):
            yield chunk

    def close(self):
        self.buffer.close()

    def release_conn(self):
        # requests.Response.close() calls release_conn even after .content was consumed.
        self.close()


def verify_response(response, verifier: DigestVerifier):
    '''Consume and verify before exposing any bytes, preserving requests.Response's API.'''
    buffer = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_SIZE)
    try:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            verifier.update(chunk)
            buffer.write(chunk)
        verifier.verify()
        buffer.seek(0)
        response.close()
        response.raw.close()
        response.raw = VerifiedRaw(buffer)
        response._content = False
        response._content_consumed = False
        return response
    except BaseException:
        buffer.close()
        response.close()
        response.raw.close()
        raise


class VerifiedAsyncContent:
    '''aiohttp-compatible local content stream. Only previously verified bytes are readable.'''

    def __init__(self, buffer, size):
        self.buffer = buffer
        self.total_bytes = size
        self._exception = None

    def exception(self):
        return self._exception

    def set_exception(self, exception, exc_cause=None):
        self._exception = exception
        self.buffer.close()

    def is_eof(self):
        return True  # All network input has already been consumed.

    def at_eof(self):
        return self.buffer.closed or self.buffer.tell() == self.total_bytes

    def read_nowait(self, n=-1):
        if self._exception:
            raise self._exception
        return self.buffer.read(n)

    async def read(self, n=-1):
        return self.read_nowait(n)

    async def readany(self):
        return await self.read(CHUNK_SIZE)

    async def readexactly(self, n):
        if n < 0:
            raise ValueError('Read size must not be negative')
        content = await self.read(n)
        if len(content) != n:
            raise asyncio.IncompleteReadError(content, n)
        return content

    async def readuntil(self, separator=b'\n'):
        if not separator:
            raise ValueError('Separator must not be empty')
        content = bytearray()
        while chunk := await self.read(8192):
            start = max(0, len(content) - len(separator) + 1)
            content.extend(chunk)
            position = content.find(separator, start)
            if position >= 0:
                end = position + len(separator)
                self.buffer.seek(end - len(content), 1)
                return bytes(content[:end])
        return bytes(content)

    async def readline(self):
        return await self.readuntil()

    async def readchunk(self):
        return await self.readany(), False  # HTTP chunk boundaries are no longer relevant.

    async def iter_chunked(self, n):
        if n <= 0:
            raise ValueError('Chunk size must be positive')
        while chunk := await self.read(n):
            yield chunk

    def iter_any(self):
        return self.iter_chunked(CHUNK_SIZE)

    async def iter_chunks(self):
        async for chunk in self.iter_any():
            yield chunk, False

    def __aiter__(self):
        async def lines():
            while line := await self.readline():
                yield line
        return lines()


async def verify_response_async(response, verifier: DigestVerifier):
    '''Consume and verify before exposing any bytes, preserving aiohttp.ClientResponse's API.'''
    buffer = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_SIZE)
    try:
        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
            verifier.update(chunk)
            buffer.write(chunk)
        verifier.verify()
        size = buffer.tell()
        buffer.seek(0)
        # Keep the response itself: consumers (including put_blob) rely on its type and metadata.
        # aiohttp's close/release machinery calls content.set_exception(), closing the spool.
        response.content = VerifiedAsyncContent(buffer, size)
        return response
    except BaseException:
        buffer.close()
        response.close()
        raise
