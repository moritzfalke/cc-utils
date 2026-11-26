import base64
import threading
import time
import unittest.mock

import requests
import pytest

import oci.auth
import oci.client as co
import oci.digest
from test._test_utils import content_digest, http_response


def test_append_b64_padding_if_missing():
    def encode_and_decode(octets: bytes):
        encoded = base64.b64encode(octets).decode('utf-8')
        encoded_wo_padding = encoded.strip('=')

        assert encoded == co._append_b64_padding_if_missing(encoded_wo_padding)

    encode_and_decode(b'a')
    encode_and_decode(b'ab')
    encode_and_decode(b'abc')
    encode_and_decode(b'abcd')


def test_per_host_throttle_aimd():
    t = co._PerHostThrottle(host='test.example.com', initial_limit=8)
    assert t._limit == 8

    t.on_429()
    assert t._limit == 4

    t.on_429()
    assert t._limit == 2

    t.on_429()
    assert t._limit == 1

    # minimum is 1
    t.on_429()
    assert t._limit == 1

    # additive increase
    t.on_success()
    assert t._limit == 2

    # does not exceed initial
    t._limit = 7
    t.on_success()
    assert t._limit == 8
    t.on_success()
    assert t._limit == 8  # capped


def test_per_host_throttle_blocks_at_limit():
    t = co._PerHostThrottle(host='test.example.com', initial_limit=1)

    results = []

    with t:
        # throttle slot is held; a second acquire should block
        def try_acquire():
            with t:
                results.append('acquired')

        thread = threading.Thread(target=try_acquire)
        thread.start()
        # give thread a moment to block
        thread.join(timeout=0.1)
        assert not results, 'second thread should not have acquired while slot is held'

    thread.join(timeout=1)
    assert results == ['acquired'], 'second thread should acquire after first releases'


def test_per_host_throttle_resume_at_blocks():
    '''on_429 with retry_after > 0 prevents new __enter__ calls until the window elapses.'''
    t = co._PerHostThrottle(host='test.example.com', initial_limit=4)
    t.on_429(retry_after=0.15)

    acquired_at = []

    def acquire():
        with t:
            acquired_at.append(time.monotonic())

    before = time.monotonic()
    thread = threading.Thread(target=acquire)
    thread.start()
    thread.join(timeout=2.0)

    assert len(acquired_at) == 1
    assert acquired_at[0] - before >= 0.10, 'thread should have been blocked for at least ~0.1s'


def _mock_response(status_code, headers=None):
    r = unittest.mock.Mock(spec=requests.Response)
    r.status_code = status_code
    r.ok = (status_code < 400)
    r.reason = 'mock'
    r.content = b''
    r.headers = headers or {}
    return r


def test_client_calls_throttle_on_429():
    '''on_429() halves the limit on 429; on_success() recovers it after the retry succeeds.'''
    client = co.Client(max_write_concurrency_per_host=4)

    # pre-populate auth cache so _authenticate() exits early without network I/O
    client.token_cache.set_auth_method(
        image_reference='registry.example.com/repo:tag',
        auth_method=co.AuthMethod.BASIC,
    )

    call_count = 0

    def fake_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _mock_response(429, headers={'Retry-After': '0'})
        return _mock_response(200)

    with unittest.mock.patch.object(client.session, 'request', side_effect=fake_request):
        client._request(
            url='https://registry.example.com/v2/repo/blobs/uploads/',
            image_reference='registry.example.com/repo:tag',
            scope='repository:repo:push',
            method='POST',
        )

    throttle = client._host_write_throttles['registry.example.com']
    # 4 → 2 from on_429, then 2 → 3 from on_success on the successful retry
    assert throttle._limit == 3
    assert call_count == 2


# ---------------------------------------------------------------------------
# batch_delete_manifests
# ---------------------------------------------------------------------------

ECR_REF = '123456789012.dkr.ecr.eu-west-1.amazonaws.com/my-repo:latest'
ECR_REFS = [
    '123456789012.dkr.ecr.eu-west-1.amazonaws.com/my-repo:v1',
    '123456789012.dkr.ecr.eu-west-1.amazonaws.com/my-repo:v2',
    '123456789012.dkr.ecr.eu-west-1.amazonaws.com/my-repo@sha256:abcdef',
]


def test_batch_delete_manifests_rejects_non_aws():
    client = co.Client()
    raised = False
    try:
        client.batch_delete_manifests(
            image_reference='gcr.io/project/repo:latest',
            refs=[],
        )
    except ValueError:
        raised = True
    assert raised


def test_batch_delete_manifests_aws():
    mock_creds = oci.auth.OciAccessKeyCredentials(
        access_key_id='AKIATEST',
        secret_access_key='secret',
        session_token=None,
    )

    def mock_credentials_lookup(image_reference, privileges, absent_ok):
        return mock_creds

    client = co.Client(credentials_lookup=mock_credentials_lookup)

    with unittest.mock.patch('oci.aws.batch_delete_images', return_value=[]) as mock_batch:
        client.batch_delete_manifests(image_reference=ECR_REF, refs=ECR_REFS)

    mock_batch.assert_called_once()
    image_ids = mock_batch.call_args.kwargs['image_ids']
    assert {'imageTag': 'v1'} in image_ids
    assert {'imageTag': 'v2'} in image_ids
    assert {'imageDigest': 'sha256:abcdef'} in image_ids


@pytest.mark.parametrize('method', ['manifest', 'blob'])
@pytest.mark.parametrize('matching', [False, True])
def test_download_content_checks(method, matching, monkeypatch, no_network):
    content = b'{"schemaVersion":2,"config":{},"layers":[]}'
    received = content if matching else b'{"schemaVersion":1}'
    expected = content_digest(content)
    client = co.Client()
    response = http_response(received)
    monkeypatch.setattr(client, '_request', unittest.mock.Mock(return_value=response))

    def download():
        if method == 'manifest':
            return client.manifest_raw(f'registry.example/image@{expected}')
        return client.blob('registry.example/image:v1', expected)

    if matching:
        with download() as result:
            assert isinstance(result, requests.Response)
            assert result.json()['schemaVersion'] == 2
            assert result.content == content
            assert result.text == content.decode()
    else:
        with pytest.raises(oci.digest.DigestMismatchError):
            download()
    assert response.raw.closed


@pytest.mark.parametrize('shape', ['symbolic', 'digest', 'mixed'])
def test_manifest_reference_resolution(shape, monkeypatch, no_network):
    content = b'{"schemaVersion":2}'
    expected = content_digest(content)
    reference = {
        'symbolic': 'registry.example/image:v1',
        'digest': f'registry.example/image@{expected}',
        'mixed': f'registry.example/image:v1@{expected}',
    }[shape]
    result_reference = f'registry.example/image@{expected}' if shape == 'symbolic' else reference
    response = http_response(content)
    response.headers['Docker-Content-Digest'] = content_digest(b'header value')
    client = co.Client()
    request = unittest.mock.Mock(return_value=response)
    monkeypatch.setattr(client, '_request', request)
    assert client.to_digest_hash(reference) == result_reference
    request.assert_called_once()


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('algorithm', ['sha256', 'sha512'])
def test_blob_streaming_interfaces(stream, algorithm, monkeypatch, no_network):
    content = b'example payload'
    client = co.Client()
    monkeypatch.setattr(client, '_request', unittest.mock.Mock(return_value=http_response(content)))
    with client.blob(
        'registry.example/image:v1', content_digest(content, algorithm), stream=stream,
    ) as r:
        assert r.raw.read(3, decode_content=True) == content[:3]
        assert b''.join(r.iter_content(4)) == content[3:]


def test_invalid_blob_digest_does_not_issue_a_request(monkeypatch, no_network):
    client = co.Client()
    request = unittest.mock.Mock()
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(ValueError):
        client.blob('registry.example/image:v1', 'sha256:abcd')
    request.assert_not_called()


def test_missing_blob_response(monkeypatch, no_network):
    client = co.Client()
    response = http_response(b'', status=404)
    monkeypatch.setattr(client, '_request', unittest.mock.Mock(return_value=response))
    assert client.blob('registry.example/image:v1', content_digest(b''), absent_ok=True) is None
    assert response.raw.closed


@pytest.mark.parametrize('shape', ['digest', 'mixed'])
@pytest.mark.parametrize('algorithm', ['sha256', 'sha512'])
def test_reference_resolution_checks_content(shape, algorithm, monkeypatch, no_network):
    expected = content_digest(b'example payload', algorithm)
    tag = ':v1' if shape == 'mixed' else ''
    reference = f'registry.example/image{tag}@{expected}'
    response = http_response(b'other payload')
    client = co.Client()
    monkeypatch.setattr(client, '_request', unittest.mock.Mock(return_value=response))
    with pytest.raises(oci.digest.DigestMismatchError) as error:
        client.to_digest_hash(reference)
    assert error.value.expected_digest == expected
    assert error.value.actual_digest == content_digest(b'other payload', algorithm)
    assert response.raw.closed
