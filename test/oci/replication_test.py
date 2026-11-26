# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace
from unittest.mock import Mock

import dacite
import pytest

import oci
import oci.client
import oci.digest
import oci.model as om
from test._test_utils import content_digest, http_response


pytestmark = pytest.mark.usefixtures('no_network')
SOURCE = 'registry.example/source'
TARGET = 'registry.example/target:v1'


def manifest_bytes(config):
    return json.dumps({
        'schemaVersion': 2, 'mediaType': om.OCI_MANIFEST_SCHEMA_V2_MIME,
        'config': {
            'digest': content_digest(config), 'size': len(config),
            'mediaType': 'application/vnd.oci.image.config.v1+json',
        },
        'layers': [], 'annotations': {'name': 'example'},
    }, separators=(',', ':')).encode()


def index_bytes(child, media_type=om.OCI_MANIFEST_SCHEMA_V2_MIME, algorithm='sha256'):
    return json.dumps({
        'schemaVersion': 2, 'mediaType': om.OCI_IMAGE_INDEX_MIME,
        'manifests': [{
            'digest': content_digest(child, algorithm), 'size': len(child), 'mediaType': media_type,
            'platform': {'architecture': 'amd64', 'os': 'linux'},
        }],
    }, separators=(',', ':')).encode()


@pytest.fixture
def replication_client(monkeypatch):
    config = b'{"architecture":"amd64","os":"linux"}'
    child = manifest_bytes(config)
    index = index_bytes(child)
    child_ref = f'{SOURCE}@{content_digest(child)}'
    index_ref = f'{SOURCE}@{content_digest(index)}'
    documents = {child_ref: child, index_ref: index}
    published = {}
    client = oci.client.Client()

    def fetch(image_reference, accept=None, absent_ok=False):
        content = documents[str(image_reference)]
        media_type = json.loads(content).get('mediaType', 'application/json')
        return http_response(content, content_type=media_type)

    def request(url, method='GET', **kwargs):
        if method == 'HEAD':
            return http_response(b'', status=404)
        assert method == 'GET' and url.endswith(f'/blobs/{content_digest(config)}')
        return http_response(config)

    def put_manifest(image_reference, manifest):
        content = manifest.encode() if isinstance(manifest, str) else manifest
        reference = om.OciImageReference.to_image_ref(image_reference)
        if reference.has_digest_tag:
            assert reference.tag == content_digest(content)
        published[f'{reference.ref_without_tag}@{content_digest(content)}'] = content
        return http_response(b'', status=201)

    def put_blob(image_reference, digest, octets_count, data):
        content = data if isinstance(data, bytes) else b''.join(data.iter_content(8192))
        assert digest == content_digest(content)
        assert len(content) == octets_count
        return http_response(b'', status=201)

    monkeypatch.setattr(client, 'manifest_raw', Mock(side_effect=fetch))
    monkeypatch.setattr(client, '_request', Mock(side_effect=request))
    monkeypatch.setattr(client, 'put_manifest', Mock(side_effect=put_manifest))
    monkeypatch.setattr(client, 'put_blob', Mock(side_effect=put_blob))
    return SimpleNamespace(
        client=client, documents=documents, published=published,
        config=config, child=child, index=index, child_ref=child_ref, index_ref=index_ref,
    )


def test_verbatim_replication_and_result_shape(replication_client):
    state = replication_client
    result = oci.replicate_artifact(
        state.index_ref, TARGET, oci_client=state.client, mode=oci.ReplicationMode.PREFER_MULTIARCH,
    )
    assert len(result) == 3
    assert result[2] == state.index
    assert state.child in state.published.values()


@pytest.mark.parametrize('annotations,modified', [
    (None, False), ({'name': 'example'}, False), ({'name': 'updated'}, True),
])
def test_annotation_processing(annotations, modified, replication_client):
    state = replication_client
    result = oci._replicate_artifact(
        state.child_ref, TARGET, oci_client=state.client, annotations=annotations,
    )
    assert result[3] is modified
    assert (result[2] != state.child) is modified


@pytest.mark.parametrize('annotations', [None, {'name': 'updated'}])
def test_input_manifest_consistency(annotations, replication_client):
    state = replication_client
    state.documents[state.index_ref] = index_bytes(b'other document')
    with pytest.raises(oci.digest.DigestMismatchError):
        oci.replicate_artifact(
            state.index_ref, TARGET, oci_client=state.client, annotations=annotations,
        )
    state.client.put_manifest.assert_not_called()
    state.client.put_blob.assert_not_called()


@pytest.mark.parametrize('annotations', [None, {'name': 'updated'}])
def test_child_manifest_consistency(annotations, replication_client):
    state = replication_client
    document = json.loads(state.child)
    document['annotations']['name'] = 'updated'
    state.documents[state.child_ref] = json.dumps(document).encode()
    with pytest.raises(oci.digest.DigestMismatchError):
        oci.replicate_artifact(
            state.index_ref, TARGET, oci_client=state.client,
            mode=oci.ReplicationMode.PREFER_MULTIARCH, annotations=annotations,
        )
    state.client.put_manifest.assert_not_called()
    state.client.put_blob.assert_not_called()


def test_unmodified_child_result_consistency(replication_client, monkeypatch):
    state = replication_client
    monkeypatch.setattr(oci, '_replicate_artifact', Mock(return_value=(
        http_response(b'', status=201), TARGET, b'other document', False,
    )))
    manifest = dacite.from_dict(data_class=om.OciImageManifestList, data=json.loads(state.index))
    with pytest.raises(oci.digest.DigestMismatchError):
        oci._replicate_manifest_list(
            state.index.decode(), manifest, om.OciImageReference(state.index_ref),
            om.OciImageReference(TARGET), state.client, oci.ReplicationMode.PREFER_MULTIARCH,
            platform_filter=None, annotations=None,
        )
    state.client.put_manifest.assert_not_called()


def test_nested_index_annotation_processing(replication_client):
    state = replication_client
    outer = index_bytes(state.index, media_type=om.OCI_IMAGE_INDEX_MIME)
    source_ref = f'{SOURCE}@{content_digest(outer)}'
    state.documents[source_ref] = outer
    _, _, output = oci.replicate_artifact(
        source_ref, TARGET, oci_client=state.client, mode=oci.ReplicationMode.PREFER_MULTIARCH,
        annotations={'name': 'updated'},
    )
    inner_digest = json.loads(output)['manifests'][0]['digest']
    inner = state.published[f'registry.example/target@{inner_digest}']
    child_digest = json.loads(inner)['manifests'][0]['digest']
    child = state.published[f'registry.example/target@{child_digest}']
    assert json.loads(child)['annotations'] == {'name': 'updated'}


@pytest.mark.parametrize('keep', [False, True])
def test_platform_filtering(keep, replication_client):
    state = replication_client
    _, _, output = oci.replicate_artifact(
        state.index_ref, TARGET, oci_client=state.client, mode=oci.ReplicationMode.PREFER_MULTIARCH,
        platform_filter=lambda platform: keep,
    )
    assert len(json.loads(output)['manifests']) == int(keep)


@pytest.mark.parametrize('indexed', [False, True])
def test_legacy_manifest_conversion(indexed, replication_client):
    state = replication_client
    config = {
        'config': {'Image': 'example'}, 'container_config': {'Image': 'example'},
        'container': 'example', 'created': '2020-01-01T00:00:00Z',
    }
    legacy = json.dumps({
        'schemaVersion': 1, 'name': 'example', 'tag': 'v1', 'architecture': 'amd64',
        'fsLayers': [], 'history': [{'v1Compatibility': json.dumps(config)}],
    }).encode()
    legacy_ref = f'{SOURCE}@{content_digest(legacy)}'
    state.documents[legacy_ref] = legacy
    source_ref = legacy_ref
    if indexed:
        index = index_bytes(
            legacy, media_type='application/vnd.docker.distribution.manifest.v1+json',
        )
        source_ref = f'{SOURCE}@{content_digest(index)}'
        state.documents[source_ref] = index
    _, _, output = oci.replicate_artifact(
        source_ref, TARGET, oci_client=state.client, mode=oci.ReplicationMode.PREFER_MULTIARCH,
    )
    assert json.loads(output)['schemaVersion'] == 2
    assert state.client.manifest_raw.call_count == (2 if indexed else 1)
    if indexed:
        assert json.loads(output)['manifests'][0]['mediaType'] == om.OCI_MANIFEST_SCHEMA_V2_MIME


def test_sha512_source_address(replication_client):
    state = replication_client
    child_digest = content_digest(state.child, 'sha512')
    state.documents[f'{SOURCE}@{child_digest}'] = state.child
    index = index_bytes(state.child, algorithm='sha512')
    source_ref = f'{SOURCE}@{content_digest(index)}'
    state.documents[source_ref] = index
    _, _, output = oci.replicate_artifact(
        source_ref, TARGET, oci_client=state.client, mode=oci.ReplicationMode.PREFER_MULTIARCH,
    )
    assert json.loads(output)['manifests'][0]['digest'] == content_digest(state.child)


def test_single_manifest_normalisation(replication_client):
    state = replication_client
    _, _, output = oci.replicate_artifact(
        state.child_ref, TARGET, oci_client=state.client,
        mode=oci.ReplicationMode.NORMALISE_TO_MULTIARCH,
    )
    index = json.loads(output)
    assert index['manifests'][0]['digest'] == content_digest(state.child)
    assert index['manifests'][0]['platform'] == {'architecture': 'amd64', 'os': 'linux'}
