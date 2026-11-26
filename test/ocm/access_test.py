# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import dataclasses
import hashlib
from unittest.mock import Mock

import pytest

import oci.digest
import ocm
import ocm.access as access_module
from test._test_utils import content_digest


pytestmark = pytest.mark.usefixtures('no_network')


@pytest.mark.parametrize('algorithm', ['sha256', 'sha512'])
def test_oci_digest_lookup(algorithm, content_client):
    content = b'{"schemaVersion":2}'
    client, request = content_client(content)
    access = ocm.OciAccess(
        imageReference=f'registry.example/image@{content_digest(content, algorithm)}',
    )
    result = access_module.access_to_digest_lookup(access, oci_client=client)
    assert result == ocm.DigestSpec(
        hashAlgorithm='SHA-256',
        normalisationAlgorithm=ocm.NormalisationAlgorithm.OCI_ARTIFACT_DIGEST,
        value=hashlib.sha256(content).hexdigest(),
    )
    request.assert_called_once()


def test_local_blob_lookup_uses_component_repository(component_descriptor_factory, content_client):
    content = b'example blob'
    client, request = content_client(content)
    component = component_descriptor_factory().component
    access = ocm.LocalBlobAccess(localReference=content_digest(content), size=len(content))
    result = access_module.access_to_digest_lookup(access, oci_client=client, component=component)
    assert result.value == hashlib.sha256(content).hexdigest()
    assert result.normalisationAlgorithm == ocm.NormalisationAlgorithm.GENERIC_BLOB_DIGEST
    assert str(request.call_args.kwargs['image_reference']) == (
        'registry.example/ocm/component-descriptors/example.org/component:1.0.0'
    )


@pytest.mark.parametrize('as_dict', [False, True])
def test_global_blob_access_lookup(as_dict, content_client):
    content = b'example blob'
    client, request = content_client(content)
    global_access = ocm.LocalBlobGlobalAccess(
        ref='mirror.example/blobs:v1', digest=content_digest(content), size=len(content),
        mediaType='application/octet-stream', type='ociBlob',
    )
    access = ocm.LocalBlobAccess(
        localReference=content_digest(content),
        globalAccess=dataclasses.asdict(global_access) if as_dict else global_access,
    )
    result = access_module.access_to_digest_lookup(access, oci_client=client)
    assert result.value == hashlib.sha256(content).hexdigest()
    assert str(request.call_args.kwargs['image_reference']) == global_access.ref


def test_global_access_requires_consistent_references():
    access = ocm.LocalBlobAccess(
        localReference=content_digest(b'example'),
        globalAccess=ocm.LocalBlobGlobalAccess(
            ref='registry.example/blobs:v1', digest=content_digest(b'other'), size=5,
            mediaType='application/octet-stream', type='ociBlob',
        ),
    )
    with pytest.raises(ValueError, match='same content'):
        access_module.resolve_access(access)


def test_relative_access_lookup(component_descriptor_factory, content_client):
    content = b'{"schemaVersion":2}'
    client, request = content_client(content)
    result = access_module.access_to_digest_lookup(
        ocm.RelativeOciAccess(reference='image:v1'), oci_client=client,
        component=component_descriptor_factory().component,
    )
    assert result.value == hashlib.sha256(content).hexdigest()
    assert str(request.call_args.kwargs['image_reference']) == 'registry.example/image:v1'


@pytest.mark.parametrize('access', [
    ocm.LocalBlobAccess(localReference=content_digest(b'example')),
    ocm.RelativeOciAccess(reference='image:v1'),
])
def test_relative_access_requires_component_context(access):
    with pytest.raises(ValueError, match='owning component repository'):
        access_module.access_to_digest_lookup(access)


def test_oci_blob_content_check(content_client):
    expected = content_digest(b'example')
    client, _ = content_client(b'other')
    access = ocm.OciBlobAccess(
        imageReference='registry.example/blobs:v1', digest=expected, size=7,
        mediaType='application/octet-stream',
    )
    with pytest.raises(oci.digest.DigestMismatchError) as error:
        access_module.access_to_digest_lookup(access, oci_client=client)
    assert error.value.expected_digest == expected


@pytest.mark.parametrize('access', [
    ocm.AccessDict({'type': 'unsupported'}), ocm.HTTPAccess(url='https://example.org/artifact'),
])
def test_unsupported_access_type(access):
    with pytest.raises(ValueError, match='Unsupported access type'):
        access_module.access_to_digest_lookup(access)


@pytest.mark.parametrize('access', [
    ocm.S3Access(bucket='example', key='object'),
    ocm.LegacyS3Access(bucketName='example', objectKey='object'),
])
def test_s3_lookup_and_body_cleanup(access):
    body = Mock(iter_chunks=Mock(return_value=iter([b'example ', b'payload'])))
    client = Mock()
    client.get_object.return_value = {'Body': body}
    result = access_module.access_to_digest_lookup(access, s3_client=client)
    assert result.value == hashlib.sha256(b'example payload').hexdigest()
    client.get_object.assert_called_once_with(Bucket='example', Key='object')
    body.close.assert_called_once()
