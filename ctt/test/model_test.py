# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import copy

import pytest

import ctt.uploaders
import oci.digest
import ocm
from test._test_utils import content_digest


pytestmark = pytest.mark.usefixtures('no_network')


@pytest.mark.parametrize('relative', [False, True])
@pytest.mark.parametrize('algorithm', ['sha256', 'sha512'])
def test_replication_source_reference(relative, algorithm, resource_element_factory):
    content = b'example payload'
    element = resource_element_factory(content, relative=relative, algorithm=algorithm)
    source = copy.deepcopy(element.source)
    assert str(element.src_ref) == 'registry.example/source:v1'
    assert str(element.replication_src_ref) == (
        f'registry.example/source@{content_digest(content, algorithm)}'
    )
    assert element.source == source


@pytest.mark.parametrize('shape', ['digest', 'mixed'])
def test_existing_source_reference_consistency(shape, resource_element_factory):
    expected = content_digest(b'example payload')
    tag = ':v1' if shape == 'mixed' else ''
    element = resource_element_factory(reference=f'registry.example/source{tag}@{expected}')
    assert element.replication_src_ref.tag == expected
    other = content_digest(b'other payload')
    element.source.access.imageReference = f'registry.example/source{tag}@{other}'
    with pytest.raises(oci.digest.DigestMismatchError) as error:
        element.replication_src_ref
    assert error.value.expected_digest == expected
    assert error.value.actual_digest == other


@pytest.mark.parametrize('reference', [
    'registry.example/source:v1',
    f'registry.example/source@{content_digest(b"example payload")}',
])
def test_source_resource_digest_is_required(reference, resource_element_factory):
    element = resource_element_factory(reference=reference)
    element.source.digest = None
    with pytest.raises(ValueError, match='requires a resource digest'):
        element.replication_src_ref


@pytest.mark.parametrize('field,value', [
    ('hashAlgorithm', 'SHA-1'), ('value', 'abcd'),
    ('normalisationAlgorithm', ocm.NormalisationAlgorithm.GENERIC_BLOB_DIGEST),
    ('normalisationAlgorithm', ocm.EXCLUDE_FROM_SIGNATURE),
])
def test_replication_source_digest_spec(field, value, resource_element_factory):
    element = resource_element_factory()
    setattr(element.source.digest, field, value)
    with pytest.raises(ValueError):
        element.replication_src_ref


def test_uploader_tag_naming_uses_metadata_reference(resource_element_factory):
    element = resource_element_factory()
    result = ctt.uploaders.TagSuffixUploader('suffix').process(element)
    assert result.target.access.imageReference == 'registry.example/source:v1-suffix'
    assert result.replication_src_ref.tag == content_digest(b'example payload')


def test_source_reference_with_registry_port(resource_element_factory):
    element = resource_element_factory(reference='registry.example:5000/source:v1')
    assert str(element.replication_src_ref) == (
        f'registry.example:5000/source@{content_digest(b"example payload")}'
    )


def test_resource_digest_selects_the_address_algorithm(resource_element_factory):
    content = b'example payload'
    element = resource_element_factory(
        reference=f'registry.example/source@{content_digest(content, "sha512")}',
    )
    assert element.replication_src_ref.tag == content_digest(content)
