import pytest

import oci.digest
import ocm
import ocm.gardener as og
from test._test_utils import content_digest


OCI_REPO = ocm.OciOcmRepository(baseUrl='acme.org/ocm')


def test_eval_version_template():
    assert og.eval_version_template(
        version_template=ocm.gardener.VersionTemplate.from_dict({
            'type': 'jq',
            'expr': '."attr-1"',
        }),
        image_dict={
            'attr-1': 'foo',
            'attr-2': 'ignoreme',
        },
    ) == 'foo'


def res(
    name='resource-name',
    version='1.2.3',
    type=ocm.ArtefactType.OCI_IMAGE,
    access=ocm.OciAccess(
        imageReference='acme.org/image-ref:tag',
    ),
    labels=(
        ocm.Label(
            name='imagevector.gardener.cloud/name',
            value='name-from-label',
        ),
        ocm.Label(
            name='imagevector.gardener.cloud/target-version',
            value='version-from-label',
        ),
    ),
    digest=None,
) -> ocm.Resource:
    return ocm.Resource(
        name=name,
        version=version,
        type=type,
        access=access,
        labels=labels,
        digest=digest,
    )


def test_oci_image_dict_from_resource():
    # using defaults
    assert og.oci_image_dict_from_resource(
        resource=res(),
        # resource_names_from_label=True,
        # fallback_to_target_version_from_resource=False,
        # resource_names=None,
    ) == {
        'name': 'name-from-label',
        'repository': 'acme.org/image-ref',
        'tag': 'tag',
        'targetVersion': 'version-from-label',
    }

    # should not fallback if label is present
    assert og.oci_image_dict_from_resource(
        resource=res(),
        # resource_names_from_label=True,
        fallback_to_target_version_from_resource=True,
        # resource_names=None,
    ) == {
        'name': 'name-from-label',
        'repository': 'acme.org/image-ref',
        'tag': 'tag',
        'targetVersion': 'version-from-label',
    }

    assert og.oci_image_dict_from_resource(
        resource=res(
            labels=(
                ocm.Label(
                    name='imagevector.gardener.cloud/name',
                    value='name-from-label',
                ),
            ),
        ),
        # resource_names_from_label=True,
        fallback_to_target_version_from_resource=True,
        # resource_names=None,
    ) == {
        'name': 'name-from-label',
        'repository': 'acme.org/image-ref',
        'tag': 'tag',
        'targetVersion': '1.2.3',
    }

    # retain resource-name (ignore label)
    assert og.oci_image_dict_from_resource(
        resource=res(),
        resource_names_from_label=False,
        # fallback_to_target_version_from_resource=False,
        # resource_names=None,
    ) == {
        'name': 'resource-name',
        'repository': 'acme.org/image-ref',
        'tag': 'tag',
        'targetVersion': 'version-from-label',
    }

    # RelativeOciAccess without ocm_repo → skip (return None)
    assert og.oci_image_dict_from_resource(
        resource=res(access=ocm.RelativeOciAccess(reference='relative/image-ref:tag')),
    ) is None

    # RelativeOciAccess with ocm_repo → resolved to absolute reference
    assert og.oci_image_dict_from_resource(
        resource=res(access=ocm.RelativeOciAccess(reference='relative/image-ref:tag')),
        ocm_repo=OCI_REPO,
    ) == {
        'name': 'name-from-label',
        'repository': 'acme.org/relative/image-ref',
        'tag': 'tag',
        'targetVersion': 'version-from-label',
    }


def test_oci_image_dict_from_resource_regardless_of_label():
    # name-label is not required
    assert og.oci_image_dict_from_resource_regardless_of_label(
        resource=res(name='resource-name', labels=()),
    ) == {
        'name': 'resource-name',
        'repository': 'acme.org/image-ref',
        'tag': 'tag',
    }

    # non-OCI-image resources are skipped
    assert og.oci_image_dict_from_resource_regardless_of_label(
        resource=res(type=ocm.ArtefactType.BLOB),
    ) is None

    # RelativeOciAccess without ocm_repo → skip (return None)
    assert og.oci_image_dict_from_resource_regardless_of_label(
        resource=res(access=ocm.RelativeOciAccess(reference='relative/image-ref:tag')),
    ) is None

    # RelativeOciAccess with ocm_repo → resolved to absolute reference
    assert og.oci_image_dict_from_resource_regardless_of_label(
        resource=res(
            name='resource-name',
            access=ocm.RelativeOciAccess(reference='relative/image-ref:tag'),
        ),
        ocm_repo=OCI_REPO,
    ) == {
        'name': 'resource-name',
        'repository': 'acme.org/relative/image-ref',
        'tag': 'tag',
    }


def test_image_dict_from_image_dict_and_resource():
    assert og.image_dict_from_image_dict_and_resource(
        component_name='cname',
        image={
            'name': 'iname',
        },
        resource=res(),
    ) == {
        'name': 'iname',
        'repository': 'acme.org/image-ref',
        'sourceRepository': 'cname',
        'tag': 'tag',
    }

    # labels should be retained
    assert og.image_dict_from_image_dict_and_resource(
        component_name='cname',
        image={
            'name': 'iname',
            'labels': [{'name': 'some-label', 'value': 42}],
        },
        resource=res(),
    ) == {
        'name': 'iname',
        'repository': 'acme.org/image-ref',
        'sourceRepository': 'cname',
        'tag': 'tag',
        'labels': [{'name': 'some-label', 'value': 42}],
    }

    # target-version should be honoured
    assert og.image_dict_from_image_dict_and_resource(
        component_name='cname',
        image={
            'name': 'iname',
            'targetVersion': 'tversion',
        },
        resource=res(),
    ) == {
        'name': 'iname',
        'repository': 'acme.org/image-ref',
        'sourceRepository': 'cname',
        'tag': 'tag',
        'targetVersion': 'tversion',
    }


def _image_dict(builder, resource):
    if builder == 'reference':
        return og.image_dict_from_image_dict_and_resource(
            component_name='example.org/component', image={'name': 'example'}, resource=resource,
        )
    arguments = {
        'resource': resource, 'ocm_repo': ocm.OciOcmRepository(baseUrl='registry.example/ocm'),
    }
    if builder == 'labelled':
        return og.oci_image_dict_from_resource(**arguments)
    return og.oci_image_dict_from_resource_regardless_of_label(**arguments)


@pytest.mark.parametrize('builder', ['reference', 'labelled', 'unlabelled'])
@pytest.mark.parametrize('shape', ['symbolic', 'mixed', 'digest', 'untagged', 'port'])
def test_image_dict_resource_digest(builder, shape, no_network):
    expected = content_digest(b'example image manifest')
    reference = {
        'symbolic': 'registry.example/image:v1',
        'mixed': f'registry.example/image:v1@{expected}',
        'digest': f'registry.example/image@{expected}',
        'untagged': 'registry.example/image',
        'port': f'registry.example:5000/image@{expected}',
    }[shape]
    resource = res(
        access=ocm.OciAccess(imageReference=reference),
        digest=ocm.DigestSpec.from_oci_artifact_digest(expected),
    )
    output = _image_dict(builder, resource)
    assert output['tag'] == (f'v1@{expected}' if shape in ('symbolic', 'mixed') else expected)
    assert output['repository'] == (
        'registry.example:5000/image' if shape == 'port' else 'registry.example/image'
    )
    assert resource.access.imageReference == reference


@pytest.mark.parametrize('builder', ['reference', 'labelled', 'unlabelled'])
@pytest.mark.parametrize('shape', ['mixed', 'digest'])
def test_image_dict_reference_consistency(builder, shape, no_network):
    expected = content_digest(b'example image manifest')
    other = content_digest(b'other image manifest')
    tag = ':v1' if shape == 'mixed' else ''
    resource = res(
        access=ocm.OciAccess(imageReference=f'registry.example/image{tag}@{other}'),
        digest=ocm.DigestSpec.from_oci_artifact_digest(expected),
    )
    with pytest.raises(oci.digest.DigestMismatchError) as error:
        _image_dict(builder, resource)
    assert error.value.expected_digest == expected
    assert error.value.actual_digest == other


@pytest.mark.parametrize('builder', ['reference', 'labelled', 'unlabelled'])
@pytest.mark.parametrize('field,value', [
    ('hashAlgorithm', 'SHA-1'),
    ('normalisationAlgorithm', ocm.NormalisationAlgorithm.GENERIC_BLOB_DIGEST),
    ('value', 'abcd'),
])
def test_image_dict_digest_spec_validation(builder, field, value, no_network):
    resource = res(digest=ocm.DigestSpec.from_oci_artifact_digest(content_digest(b'example')))
    setattr(resource.digest, field, value)
    with pytest.raises(ValueError):
        _image_dict(builder, resource)


@pytest.mark.parametrize('builder', ['reference', 'labelled', 'unlabelled'])
def test_image_dict_excluded_digest(builder, no_network):
    resource = res(digest=ocm.ExcludeFromSignatureDigest())
    with pytest.raises(ValueError, match='supported OCI artifact digest'):
        _image_dict(builder, resource)


@pytest.mark.parametrize('builder', ['labelled', 'unlabelled'])
@pytest.mark.parametrize('mixed', [False, True])
def test_image_dict_relative_resource_digest(builder, mixed, no_network):
    expected = content_digest(b'example image manifest')
    reference = f'image:v1@{expected}' if mixed else 'image:v1'
    resource = res(
        access=ocm.RelativeOciAccess(reference=reference),
        digest=ocm.DigestSpec.from_oci_artifact_digest(expected),
    )
    output = _image_dict(builder, resource)
    assert output['repository'] == 'registry.example/image'
    assert output['tag'] == f'v1@{expected}'


@pytest.mark.parametrize('builder', ['reference', 'labelled', 'unlabelled'])
def test_image_dict_sha512_digest(builder, no_network):
    expected = content_digest(b'example image manifest', 'sha512')
    resource = res(digest=ocm.DigestSpec.from_oci_artifact_digest(expected))
    assert _image_dict(builder, resource)['tag'] == f'tag@{expected}'


@pytest.mark.parametrize('builder', ['reference', 'labelled', 'unlabelled'])
def test_image_dict_mixed_reference_without_resource_digest(builder, no_network):
    expected = content_digest(b'example image manifest')
    resource = res(access=ocm.OciAccess(imageReference=f'registry.example/image:v1@{expected}'))
    assert _image_dict(builder, resource)['tag'] == f'v1@{expected}'


def test_iter_image_dicts_from_image_dicts_and_resources():
    # smoketest only as function mostly delegates
    result = tuple(
        og.iter_image_dicts_from_image_dicts_and_resources(
            images=[{
                'name': 'resource-name',
            }],
            component_name='acme.org/other-component',
            resources=[res()],
        )
    )

    assert len(result) == 1
    result, = result
    assert result == {
        'name': 'resource-name',
        'repository': 'acme.org/image-ref',
        'tag': 'tag',
        'sourceRepository': 'acme.org/other-component',
    }


def test_iter_oci_image_dicts_from_component():
    # smoketest only as function mostly delegates
    # important aspects to check:
    # -> should process component-references
    # -> should process component's resources

    referenced_component = ocm.Component(
        name='acme.org/referenced-component',
        version='dontcare',
        provider='acme.org',
        sources=(),
        repositoryContexts=(),
        componentReferences=(),
        resources=(
            res(name='referenced-component-res-name'),
        ),
    )

    root_component = ocm.Component(
        name='acme.org/c1',
        version='dontcare',
        repositoryContexts=(),
        provider='acme.org',
        sources=(),
        componentReferences=(
            ocm.ComponentReference(
                name='cref',
                componentName=referenced_component.name,
                version=referenced_component.version,
                labels=(
                    ocm.Label(
                        name='imagevector.gardener.cloud/images',
                        value={'images': [{
                            'name': 'referenced-component-res-name',
                        }]}
                    ),
                ),
            ),
        ),
        resources=(
            res(),
        ),
    )

    def lookup(cid):
        if cid != root_component.componentReferences[0]:
            pytest.fail(f'component-descriptor-lookup was called with unexpected {cid=}')

        return referenced_component

    # call as parameterised in first round of iter_oci_image_dicts_from_rooted_component
    result = og.iter_oci_image_dicts_from_component(
        component=root_component,
        resource_names_from_label=True,
        fallback_to_target_version_from_resource=False,
        resource_names=None,
        component_descriptor_lookup=lookup,
    )
    result = tuple(result)

    assert len(result) == 2

    assert result == (
        { # this image-dict is yielded from root_component's sub-component-reference
            'name': 'referenced-component-res-name',
            'repository': 'acme.org/image-ref',
            'tag': 'tag',
            'sourceRepository': 'acme.org/referenced-component',
        },
        { # this image-dict is yielded from root-component's resource
            'name': 'name-from-label',
            'repository': 'acme.org/image-ref',
            'tag': 'tag',
            'targetVersion': 'version-from-label',
        }
    )


def test_iter_oci_image_dicts_from_rooted_component_relative_access():
    # RelativeOciAccess resources with the imagevector label should be resolved,
    # not silently dropped (regression: AttributeError on .imageReference)
    component = ocm.Component(
        name='acme.org/c1',
        version='1.0.0',
        provider='acme.org',
        sources=(),
        repositoryContexts=(OCI_REPO,),
        componentReferences=(),
        resources=(
            res(access=ocm.RelativeOciAccess(reference='relative/image:tag')),
        ),
    )

    result = tuple(og.iter_oci_image_dicts_from_rooted_component(
        component=component,
        root_component=None,
        component_descriptor_lookup=lambda _: pytest.fail('should not be called'),
    ))

    assert len(result) == 1
    assert result[0]['repository'] == 'acme.org/relative/image'
    assert result[0]['tag'] == 'tag'
