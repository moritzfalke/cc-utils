# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import copy
import socket
import sys
from unittest.mock import Mock

import pytest
import requests

import ctt.model
import ctt.process_dependencies as processing
import ocm
from test._test_utils import content_digest


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


@pytest.fixture
def resource_element_factory():
    def make(
        content=b'example payload', relative=False, reference=None, algorithm='sha256', **options,
    ):
        access = (
            ocm.RelativeOciAccess(reference=reference or 'source:v1') if relative
            else ocm.OciAccess(imageReference=reference or 'registry.example/source:v1')
        )
        source = ocm.Resource(
            name='example', version='1.0.0', type=ocm.ArtefactType.OCI_IMAGE, access=access,
            digest=ocm.DigestSpec.from_oci_artifact_digest(content_digest(content, algorithm)),
        )
        target = copy.deepcopy(source)
        target.access = ocm.OciAccess(imageReference='registry.example/target:v1')
        return ctt.model.ReplicationResourceElement(
            source=source, target=target,
            component_id=ocm.ComponentIdentity('example.org/component', '1.0.0'),
            src_ocm_repo=ocm.OciOcmRepository(baseUrl='registry.example/ocm'),
            **options,
        )
    return make


@pytest.fixture
def upload_caches(monkeypatch):
    monkeypatch.setattr(processing, 'uploaded_image_refs_to_digests', {})
    monkeypatch.setattr(processing, 'uploaded_image_refs_to_source_refs', {})
    monkeypatch.setattr(processing, 'uploaded_image_refs_to_ready_events', {})


@pytest.fixture
def capture_descriptor_publication(monkeypatch):
    import ctt.replicate

    publish = Mock()
    monkeypatch.setattr(
        ctt.replicate, 'replicate_oci_artifact_with_patched_component_descriptor', publish,
    )
    return publish
