# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import copy
from unittest.mock import Mock

import pytest

import oci.client
import ocm
from test._test_utils import content_digest, http_response


@pytest.fixture
def resource_factory():
    def make(content=b'example payload', access=None, **kwargs):
        access = access or ocm.OciAccess(imageReference='registry.example/image:v1')
        normalisation = (
            ocm.NormalisationAlgorithm.GENERIC_BLOB_DIGEST
            if isinstance(access, (
                ocm.LocalBlobAccess, ocm.OciBlobAccess, ocm.S3Access, ocm.LegacyS3Access,
            ))
            else ocm.NormalisationAlgorithm.OCI_ARTIFACT_DIGEST
        )
        values = {
            'name': 'example', 'version': '1.0.0', 'type': ocm.ArtefactType.OCI_IMAGE,
            'access': copy.deepcopy(access),
            'digest': ocm.DigestSpec(
                hashAlgorithm='SHA-256', normalisationAlgorithm=normalisation,
                value=content_digest(content).removeprefix('sha256:'),
            ),
        }
        return ocm.Resource(**(values | kwargs))
    return make


@pytest.fixture
def component_descriptor_factory():
    def make(resources=(), name='example.org/component', repository='registry.example/ocm'):
        return ocm.ComponentDescriptor(
            meta=ocm.Metadata(),
            component=ocm.Component(
                name=name, version='1.0.0', provider='example', sources=[],
                componentReferences=[], resources=list(resources),
                repositoryContexts=[ocm.OciOcmRepository(baseUrl=repository)],
            ),
        )
    return make


@pytest.fixture
def content_client(monkeypatch):
    def make(content):
        client = oci.client.Client()
        request = Mock(side_effect=lambda **kwargs: http_response(content))
        monkeypatch.setattr(client, '_request', request)
        return client, request
    return make
