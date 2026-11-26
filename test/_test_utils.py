# SPDX-FileCopyrightText: Contributors to the Gardener project
#
# SPDX-License-Identifier: Apache-2.0


from contextlib import contextmanager
from io import StringIO
import hashlib
import io
import sys
import typing


@contextmanager
def capture_out():
    new_stdout, new_stderr = StringIO(), StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = new_stdout, new_stderr
        yield sys.stdout, sys.stderr
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr


@contextmanager
def replace_modules(modules:dict):
    '''
    ctx manager that will replace the given modules in sys.modules and restore them
    afterwards
    '''
    original_modules = {
        name: sys.modules[name] for name in modules.keys()
    }
    try:
        for name, module in modules.items():
            sys.modules[name] = module
        yield None
    finally:
        for name, module in original_modules.items():
            sys.modules[name] = module


class AssertMixin:
    def assertEmpty(self, iterable, msg=None):
        if issubclass(type(iterable), typing.Sequence):
            leng = len(iterable)
            if leng == 0:
                return
            raise self.failureException('iterable was not empty')
        try:
            next(iterable)
            raise self.failureException('iterable was not empty')
        except StopIteration:
            return # ok - iterable was empty
        except Exception as e:
            raise self.failureException(str(e))


def content_digest(content: bytes, algorithm: str='sha256') -> str:
    return f'{algorithm}:{hashlib.new(algorithm, content).hexdigest()}'


def http_response(content: bytes, status: int=200, content_type: str='application/json'):
    import requests
    import urllib3

    response = requests.Response()
    response.status_code = status
    response.encoding = 'utf-8'
    response.raw = urllib3.response.HTTPResponse(body=io.BytesIO(content), preload_content=False)
    response.headers['Content-Type'] = content_type
    response.headers['Content-Length'] = str(len(content))
    return response


async def async_http_response(content: bytes, status: int=200):
    import asyncio
    import inspect
    from unittest.mock import Mock

    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    loop = asyncio.get_running_loop()
    stream = aiohttp.StreamReader(protocol=Mock(_reading_paused=False), limit=65536, loop=loop)
    stream.feed_data(content)
    stream.feed_eof()
    url = URL('https://registry.example/v2/image/manifests/v1')
    headers = CIMultiDictProxy(CIMultiDict({
        'Content-Type': 'application/json', 'Content-Length': str(len(content)),
    }))
    arguments = {
        'writer': None,
        'continue100': None,
        'timer': aiohttp.helpers.TimerNoop(),
        'request_info': aiohttp.RequestInfo(url, headers, 'GET', url),
        'traces': [],
        'loop': loop,
        'session': Mock(_resolve_charset=lambda response, body: 'utf-8'),
        'stream_writer': Mock(output_size=0),
    }
    parameters = inspect.signature(aiohttp.ClientResponse).parameters
    response = aiohttp.ClientResponse('GET', url, **{
        name: value for name, value in arguments.items() if name in parameters
    })
    response.status = status
    response.reason = 'Example response'
    response._headers = headers
    response._closed = False
    response.content = stream
    return response
