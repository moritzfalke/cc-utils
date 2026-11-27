# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import ctt.model
import ctt.process_dependencies as processing
import oci.client
import oci.digest
import oci.model as om
import ocm
from test._test_utils import content_digest, http_response


pytestmark = pytest.mark.usefixtures('no_network', 'upload_caches')
CONFIG = b'{}'


def manifest_bytes(name='example'):
    return json.dumps({
        'schemaVersion': 2, 'mediaType': om.OCI_MANIFEST_SCHEMA_V2_MIME,
        'config': {
            'mediaType': 'application/vnd.oci.image.config.v1+json',
            'digest': content_digest(CONFIG), 'size': len(CONFIG),
        },
        'layers': [], 'annotations': {'name': name},
    }, separators=(',', ':')).encode()


@pytest.fixture
def upload_client(monkeypatch):
    def make(element, content):
        documents = {
            str(element.src_ref): content,
            str(element.replication_src_ref): content,
        }
        published = []
        client = oci.client.Client()

        def request(url, image_reference, method='GET', **kwargs):
            if method == 'HEAD':
                return http_response(b'', status=404)
            if '/blobs/' in url:
                assert url.endswith(content_digest(CONFIG))
                return http_response(CONFIG)
            return http_response(
                documents[str(image_reference)], content_type=om.OCI_MANIFEST_SCHEMA_V2_MIME,
            )

        def publish(image_reference, manifest):
            raw = manifest.encode() if isinstance(manifest, str) else manifest
            published.append(raw)
            documents[str(image_reference)] = raw
            reference = om.OciImageReference.to_image_ref(image_reference)
            documents[f'{reference.ref_without_tag}@{content_digest(raw)}'] = raw
            return http_response(b'', status=201)

        monkeypatch.setattr(client, '_request', Mock(side_effect=request))
        monkeypatch.setattr(client, 'put_manifest', Mock(side_effect=publish))
        monkeypatch.setattr(client, 'put_blob', Mock(return_value=http_response(b'', status=201)))
        return SimpleNamespace(client=client, documents=documents, published=published)
    return make


@pytest.mark.parametrize('relative', [False, True])
def test_upload_uses_qualified_source(relative, resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content, relative=relative)
    state = upload_client(element, content)
    state.documents[str(element.src_ref)] = manifest_bytes('updated')
    result = processing.process_upload_request(element, state.client)
    assert result == content_digest(content)
    assert state.published == [content]


@pytest.mark.parametrize('operation', ['copy', 'annotations', 'strip'])
def test_upload_input_consistency(operation, resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content)
    state = upload_client(element, content)
    state.documents[str(element.replication_src_ref)] = manifest_bytes('updated')
    if operation == 'strip':
        element.strip_manifest_attributes = ['annotations']
    with pytest.raises(oci.digest.DigestMismatchError):
        processing.process_upload_request(
            element, state.client,
            inject_ocm_coordinates_into_oci_manifests=operation == 'annotations',
        )
    state.client.put_blob.assert_not_called()
    state.client.put_manifest.assert_not_called()
    assert not processing.uploaded_image_refs_to_digests
    assert not processing.uploaded_image_refs_to_ready_events


def test_existing_target_hint_is_not_a_source_identity(resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content, digest=content_digest(manifest_bytes('updated')))
    state = upload_client(element, content)
    assert processing.process_upload_request(element, state.client) == content_digest(content)
    assert state.published == [content]


@pytest.mark.parametrize('matching', [False, True])
def test_existing_target_content_check(matching, resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content, digest=content_digest(content))
    state = upload_client(element, content)
    state.documents[str(element.tgt_ref)] = content if matching else manifest_bytes('updated')
    if matching:
        assert processing.process_upload_request(element, state.client) == content_digest(content)
    else:
        with pytest.raises(oci.digest.DigestMismatchError):
            processing.process_upload_request(element, state.client)
    assert not state.published


def test_completed_cache_is_bound_to_source(resource_element_factory, upload_client):
    first = manifest_bytes()
    second = manifest_bytes('updated')
    element = resource_element_factory(first)
    state = upload_client(element, first)
    assert processing.process_upload_request(element, state.client) == content_digest(first)
    state.client._request.reset_mock()
    state.client.put_manifest.reset_mock()
    assert processing.process_upload_request(element, state.client) == content_digest(first)
    state.client._request.assert_not_called()
    state.client.put_manifest.assert_not_called()

    other = resource_element_factory(second)
    state.documents[str(other.replication_src_ref)] = second
    assert processing.process_upload_request(other, state.client) == content_digest(second)
    assert state.published == [first, second]


def test_cache_without_source_metadata_is_not_reused(resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content)
    state = upload_client(element, content)
    processing.uploaded_image_refs_to_digests[element.tgt_ref] = content_digest(
        manifest_bytes('updated'),
    )
    assert processing.process_upload_request(element, state.client) == content_digest(content)
    assert state.published == [content]


def test_transformed_output_is_not_reused_for_plain_copy(resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content)
    state = upload_client(element, content)
    transformed = processing.process_upload_request(
        element, state.client, inject_ocm_coordinates_into_oci_manifests=True,
    )
    assert transformed != content_digest(content)
    assert not processing.uploaded_image_refs_to_digests
    assert processing.process_upload_request(element, state.client) == content_digest(content)
    assert state.published[-1] == content


def test_target_hint_does_not_skip_configured_processing(resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content, digest=content_digest(content))
    state = upload_client(element, content)
    state.documents[str(element.tgt_ref)] = content
    result = processing.process_upload_request(
        element, state.client, inject_ocm_coordinates_into_oci_manifests=True,
    )
    assert result != content_digest(content)
    assert len(state.published) == 1


def test_completed_cache_includes_replication_mode(resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content)
    state = upload_client(element, content)
    processing.process_upload_request(element, state.client)
    state.client.put_manifest.reset_mock()
    processing.process_upload_request(
        element, state.client, replication_mode=processing.oci.ReplicationMode.REGISTRY_DEFAULTS,
    )
    state.client.put_manifest.assert_called_once()


def test_dry_run_does_not_change_completed_cache(resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content)
    state = upload_client(element, content)
    processing.process_upload_request(element, state.client)
    cached = dict(processing.uploaded_image_refs_to_digests)
    state.client._request.reset_mock()
    processing.process_upload_request(
        element, state.client, processing_mode=processing.ProcessingMode.DRY_RUN,
    )
    assert processing.uploaded_image_refs_to_digests == cached
    state.client._request.assert_not_called()


def test_failed_upload_releases_pending_state(resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content)
    state = upload_client(element, content)
    state.documents[str(element.replication_src_ref)] = manifest_bytes('updated')
    with pytest.raises(oci.digest.DigestMismatchError):
        processing.process_upload_request(element, state.client)
    assert not processing.uploaded_image_refs_to_ready_events
    state.documents[str(element.replication_src_ref)] = content
    assert processing.process_upload_request(element, state.client) == content_digest(content)


def test_request_setup_error_releases_pending_state(resource_element_factory, upload_client):
    element = resource_element_factory(manifest_bytes())
    state = upload_client(element, manifest_bytes())
    element.component_id = None
    with pytest.raises(AttributeError):
        processing.process_upload_request(
            element, state.client, inject_ocm_coordinates_into_oci_manifests=True,
        )
    assert not processing.uploaded_image_refs_to_ready_events


def test_missing_digest_is_checked_before_cached_results(resource_element_factory, upload_client):
    content = manifest_bytes()
    element = resource_element_factory(content)
    state = upload_client(element, content)
    processing.process_upload_request(element, state.client)
    element.source.digest = None
    state.client._request.reset_mock()
    with pytest.raises(ValueError, match='requires a resource digest'):
        processing.process_upload_request(element, state.client)
    state.client._request.assert_not_called()


def test_concurrent_identical_uploads_share_a_completed_result(
    resource_element_factory, upload_client, monkeypatch,
):
    content = manifest_bytes()
    element = resource_element_factory(content)
    state = upload_client(element, content)
    started, finish = threading.Event(), threading.Event()
    results, errors = [], []

    def filter_image(**kwargs):
        started.set()
        assert finish.wait(2)
        return http_response(b''), element.tgt_ref, content

    monkeypatch.setattr(processing.ctt.oci_util, 'filter_image', Mock(side_effect=filter_image))

    def run():
        try:
            results.append(processing.process_upload_request(element, state.client))
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=run, daemon=True)
    second = threading.Thread(target=run, daemon=True)
    first.start()
    assert started.wait(2)
    second.start()
    finish.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert results == [content_digest(content)] * 2
    processing.ctt.oci_util.filter_image.assert_called_once()


@pytest.mark.parametrize('annotations', [False, True])
@pytest.mark.parametrize('matching', [False, True])
@pytest.mark.parametrize('algorithm', ['sha256', 'sha512'])
def test_replication_step_resource_metadata(
    annotations, matching, algorithm,
    resource_element_factory, upload_client, capture_descriptor_publication,
):
    content = manifest_bytes()
    element = resource_element_factory(
        content, algorithm=algorithm, reference_by_digest=True, retain_symbolic_tag=True,
    )
    state = upload_client(element, content)
    source = ocm.ComponentDescriptor(meta=ocm.Metadata(), component=ocm.Component(
        name='example.org/component', version='1.0.0', provider='example', sources=[],
        resources=[element.source], componentReferences=[],
        repositoryContexts=[element.src_ocm_repo],
    ))
    target = copy.deepcopy(source)
    target.component.repositoryContexts.append(
        ocm.OciOcmRepository(baseUrl='registry.example/target-ocm'),
    )
    target.component.resources = [element.target]
    original_digest = copy.deepcopy(element.source.digest)
    step = ctt.model.ReplicationPlanStep(
        target_ocm_repository='registry.example/target-ocm', resources=(element,),
        components=(ctt.model.ReplicationComponentElement(source=source, target=target),),
    )
    if not matching:
        state.documents[str(element.replication_src_ref)] = manifest_bytes('updated')

    def run():
        return list(processing.process_replication_plan_step(
            step, source, state.client, Mock(return_value=None),
            inject_ocm_coordinates_into_oci_manifests=annotations,
            skip_cd_validation=True, max_workers=1,
        ))

    if matching:
        assert run()
        expected = content_digest(state.published[-1])
        assert element.target.digest == ocm.DigestSpec.from_oci_artifact_digest(expected)
        assert element.target.access.imageReference == f'registry.example/target:v1@{expected}'
        capture_descriptor_publication.assert_called_once()
    else:
        with pytest.raises(oci.digest.DigestMismatchError):
            run()
        assert not state.published
        assert element.target.digest == original_digest
        capture_descriptor_publication.assert_not_called()
    assert element.source.digest == original_digest
