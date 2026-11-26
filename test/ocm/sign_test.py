# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import copy
import dataclasses
import functools
from unittest.mock import Mock

import pytest

import ocm
import ocm.access
import ocm.sign as sign_module
from test._test_utils import content_digest


pytestmark = pytest.mark.usefixtures('no_network')


def test_existing_digest_verification_defaults(resource_factory):
    resource = resource_factory()
    lookup = Mock(return_value=resource.digest)
    result = sign_module.normalise_resource(resource, lookup)
    lookup.assert_called_once()
    assert dict(entry for item in result for entry in item.items())['digest']
    lookup.reset_mock()
    sign_module.normalise_resource(resource, lookup, verify_digests=False)
    lookup.assert_not_called()


@pytest.mark.parametrize('field,value', [
    ('value', '0' * 64), ('hashAlgorithm', 'SHA-512'),
    ('normalisationAlgorithm', ocm.NormalisationAlgorithm.GENERIC_BLOB_DIGEST),
])
def test_existing_digest_spec_is_checked(field, value, resource_factory):
    resource = resource_factory()
    calculated = copy.deepcopy(resource.digest)
    setattr(resource.digest, field, value)
    lookup = Mock(return_value=calculated)
    with pytest.raises(sign_module.DigestMismatchException):
        sign_module.normalise_resource(resource, lookup)


@pytest.mark.parametrize('verify_digests', [False, True])
def test_exclusion_policy(verify_digests, resource_factory):
    resource = resource_factory(digest=ocm.ExcludeFromSignatureDigest())
    lookup = Mock()
    with pytest.raises(ValueError, match='explicit opt-in'):
        sign_module.normalise_resource(resource, lookup, verify_digests=verify_digests)
    sign_module.normalise_resource(
        resource, lookup, verify_digests=verify_digests, allow_excluded_resources=True,
    )
    lookup.assert_not_called()


@pytest.mark.parametrize('field', ['hashAlgorithm', 'normalisationAlgorithm', 'value'])
def test_exclusion_spec_requires_all_fields(field, resource_factory):
    resource = resource_factory()
    setattr(resource.digest, field, getattr(ocm.ExcludeFromSignatureDigest(), field))
    with pytest.raises(ValueError, match='Invalid resource digest exclusion'):
        sign_module.normalise_resource(resource, Mock(), allow_excluded_resources=True)


def test_calculated_digest_policy(resource_factory):
    resource = resource_factory(digest=None)
    with pytest.raises(ValueError, match='explicit opt-in'):
        sign_module.normalise_resource(resource, Mock(return_value=ocm.ExcludeFromSignatureDigest()))
    with pytest.raises(ValueError, match='digest specification'):
        sign_module.normalise_resource(resource, Mock(return_value=None))


def test_component_lookup_keeps_single_argument_callback(
    resource_factory, component_descriptor_factory,
):
    resource = resource_factory(
        access=ocm.LocalBlobAccess(localReference=content_digest(b'example payload')),
    )
    descriptor = component_descriptor_factory([resource])
    lookup = Mock(return_value=resource.digest)
    sign_module.component_descriptor_digest(descriptor, Mock(), lookup)
    lookup.assert_called_once()
    resolved = lookup.call_args.args[0]
    assert isinstance(resolved, ocm.OciBlobAccess)
    assert resolved.imageReference.endswith('/example.org/component:1.0.0')


def test_repository_relocation_preserves_normalized_form(
    resource_factory, component_descriptor_factory, content_client,
):
    content = b'example payload'
    resource = resource_factory(access=ocm.LocalBlobAccess(localReference=content_digest(content)))
    descriptor = component_descriptor_factory([resource])
    client, _ = content_client(content)
    lookup = functools.partial(ocm.access.access_to_digest_lookup, oci_client=client)
    original = sign_module.normalise_component_descriptor(descriptor, Mock(), lookup)
    relocated = copy.deepcopy(descriptor)
    relocated.component.repositoryContexts = [ocm.OciOcmRepository(baseUrl='mirror.example/ocm')]
    assert sign_module.normalise_component_descriptor(relocated, Mock(), lookup) == original
    assert descriptor.component.repositoryContexts[0].baseUrl == 'registry.example/ocm'


def test_child_resources_use_child_repository(
    resource_factory, component_descriptor_factory, content_client,
):
    content = b'example payload'
    child = component_descriptor_factory(
        [resource_factory(access=ocm.LocalBlobAccess(localReference=content_digest(content)))],
        name='example.org/child', repository='child.example/ocm',
    )
    root = component_descriptor_factory()
    root.component.componentReferences = [ocm.ComponentReference(
        name='child', componentName=child.component.name, version=child.component.version,
    )]
    client, request = content_client(content)
    sign_module.component_descriptor_digest(
        root, lambda identity: child,
        functools.partial(ocm.access.access_to_digest_lookup, oci_client=client),
    )
    request.assert_called_once()
    assert str(request.call_args.kwargs['image_reference']) == (
        'child.example/ocm/component-descriptors/example.org/child:1.0.0'
    )


@pytest.mark.parametrize('field,value', [
    ('hashAlgorithm', 'SHA-512'), ('normalisationAlgorithm', 'unsupported'),
])
def test_component_reference_digest_spec(field, value, component_descriptor_factory):
    child = component_descriptor_factory()
    calculated = sign_module.component_descriptor_digest(child, Mock(), Mock())
    spec = ocm.DigestSpec(
        hashAlgorithm='SHA-256',
        normalisationAlgorithm=ocm.NormalisationAlgorithm.JSON_NORMALISATION,
        value=calculated,
    )
    setattr(spec, field, value)
    reference = ocm.ComponentReference(
        name='child', componentName=child.component.name,
        version=child.component.version, digest=spec,
    )
    with pytest.raises(sign_module.DigestMismatchException):
        sign_module.normalise_component_reference(reference, lambda identity: child, Mock())


def test_nested_exclusion_policy(resource_factory, component_descriptor_factory):
    child = component_descriptor_factory([resource_factory(digest=ocm.ExcludeFromSignatureDigest())])
    root = component_descriptor_factory()
    root.component.componentReferences = [ocm.ComponentReference(
        name='child', componentName=child.component.name, version=child.component.version,
    )]
    with pytest.raises(ValueError, match='explicit opt-in'):
        sign_module.component_descriptor_digest(root, lambda identity: child, Mock())
    assert sign_module.component_descriptor_digest(
        root, lambda identity: child, Mock(), allow_excluded_resources=True,
    )


def test_component_normalisation_algorithm(component_descriptor_factory):
    with pytest.raises(ValueError, match='Unsupported component normalisation'):
        sign_module.component_descriptor_digest(
            component_descriptor_factory(), Mock(), Mock(), normalisation='unsupported',
        )


def test_digest_spec_comparison_preserves_input(resource_factory):
    spec = resource_factory().digest
    original = dataclasses.asdict(spec)
    sign_module.verify_digest_spec(spec, copy.deepcopy(spec))
    assert dataclasses.asdict(spec) == original


def test_resource_without_access(resource_factory):
    resource = resource_factory()
    resource.access = None
    with pytest.raises(ValueError, match='without content access'):
        sign_module.normalise_resource(resource, Mock())
    resource.digest = None
    lookup = Mock()
    normalised = sign_module.normalise_resource(resource, lookup)
    assert 'digest' not in dict(entry for item in normalised for entry in item.items())
    lookup.assert_not_called()
