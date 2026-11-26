'''
Functionality for normalising and hashing OCM-Component-Descriptors.
'''

import collections.abc
import copy
import dataclasses
import datetime
import hashlib
import json

import ocm
import ocm.access


class DigestMismatchException(Exception):
    pass


def verify_digest_spec(expected: ocm.DigestSpec, actual: ocm.DigestSpec):
    '''Check the entire digest specification, including the algorithm's provenance.'''
    if not isinstance(expected, ocm.DigestSpec) or not isinstance(actual, ocm.DigestSpec):
        raise ValueError('Expected an OCM digest specification')
    if dataclasses.asdict(expected) != dataclasses.asdict(actual):
        raise DigestMismatchException(f'Calculated digest {actual} mismatches existing {expected}')


def _check_exclusion(digest: ocm.DigestSpec | None, allow_excluded_resources: bool) -> bool:
    if not digest:
        return False
    if not isinstance(digest, ocm.DigestSpec):
        raise ValueError('Expected an OCM digest specification')
    if (
        digest.hashAlgorithm == ocm.NO_DIGEST
        or digest.normalisationAlgorithm == ocm.EXCLUDE_FROM_SIGNATURE
        or digest.value == ocm.NO_DIGEST
    ):
        if dataclasses.asdict(digest) != dataclasses.asdict(ocm.ExcludeFromSignatureDigest()):
            raise ValueError('Invalid resource digest exclusion')
        if not allow_excluded_resources:
            raise ValueError('Excluded resource content requires explicit opt-in')
        return True
    return False


def _check_normalisation(normalisation):
    if normalisation != ocm.NormalisationAlgorithm.JSON_NORMALISATION:
        raise ValueError(f'Unsupported component normalisation algorithm: {normalisation!r}')


def normalise_obj(
    obj: dict,
) -> list[dict]:
    '''
    Recursively converts a dictionary `obj` to a list of objects with a single key/value-pair,
    sorted by the keys, to create a stable representation of `obj`.
    '''
    return [
        {
            key: normalise_obj(value) if isinstance(value, dict) else value,
        }
        for key, value in sorted(
            obj.items(),
            key=lambda item: item[0],
        )
    ]


def normalise_label(
    label: ocm.Label,
) -> list[dict]:
    label_raw = dataclasses.asdict(label)

    if not label.version:
        del label_raw['version']

    return normalise_obj(label_raw)


def normalise_resource(
    resource: ocm.Resource,
    access_to_digest_lookup: collections.abc.Callable[[ocm.Access], ocm.DigestSpec],
    verify_digests: bool=True,
    allow_excluded_resources: bool=False,
    component: ocm.Component=None,
) -> list[dict]:
    resource_raw = dataclasses.asdict(resource)

    # drop properties not relevant for signing
    del resource_raw['access']
    del resource_raw['srcRefs']

    if labels := [normalise_label(l) for l in resource.labels if l.signing]:
        resource_raw['labels'] = labels
    else:
        del resource_raw['labels']

    excluded = _check_exclusion(resource.digest, allow_excluded_resources)
    has_access = resource.access and resource.access.type not in (ocm.AccessType.NONE, 'none', None)
    if not has_access:
        if resource.digest and verify_digests and not excluded:
            raise ValueError('Cannot verify a resource digest without content access')
        del resource_raw['digest']
    elif not excluded and (verify_digests or not resource.digest):
        # Resolve only the lookup input. Access itself remains outside the signed form,
        # and existing single-argument digest callbacks continue to work.
        access = ocm.access.resolve_access(resource.access, component)
        digest = access_to_digest_lookup(access)
        if not isinstance(digest, ocm.DigestSpec):
            raise ValueError('Expected an OCM digest specification from the content lookup')
        calculated_exclusion = _check_exclusion(digest, allow_excluded_resources)
        if not calculated_exclusion and (
            digest.hashAlgorithm != 'SHA-256'
            or digest.normalisationAlgorithm not in (
                ocm.NormalisationAlgorithm.OCI_ARTIFACT_DIGEST,
                ocm.NormalisationAlgorithm.GENERIC_BLOB_DIGEST,
            )
        ):
            raise ValueError('Unsupported resource digest specification')
        if resource.digest:
            # Do not rewrite signed algorithm metadata using values from the lookup.
            verify_digest_spec(resource.digest, digest)
        else:
            resource_raw['digest'] = dataclasses.asdict(digest)

    # extra-identity is expected to be null instead of an empty object by OCM-cli in case it does
    # not contain any elements
    if not resource.extraIdentity:
        resource_raw['extraIdentity'] = None

    return normalise_obj(resource_raw)


def normalise_component_reference(
    component_reference: ocm.ComponentReference,
    component_descriptor_lookup: collections.abc.Callable[[ocm.ComponentIdentity], ocm.ComponentDescriptor], # noqa: E501
    access_to_digest_lookup: collections.abc.Callable[[ocm.Access], ocm.DigestSpec],
    verify_digests: bool=True,
    normalisation: ocm.NormalisationAlgorithm=ocm.NormalisationAlgorithm.JSON_NORMALISATION,
    allow_excluded_resources: bool=False,
) -> list[dict]:
    _check_normalisation(normalisation)
    component_reference_raw = dataclasses.asdict(component_reference)

    if labels := [normalise_label(l) for l in component_reference.labels if l.signing]:
        component_reference_raw['labels'] = labels
    else:
        del component_reference_raw['labels']

    if not component_reference.digest or verify_digests:
        component_descriptor = component_descriptor_lookup(ocm.ComponentIdentity(
            name=component_reference.componentName,
            version=component_reference.version,
        ))

        digest = component_descriptor_digest(
            component_descriptor=component_descriptor,
            component_descriptor_lookup=component_descriptor_lookup,
            access_to_digest_lookup=access_to_digest_lookup,
            verify_digests=verify_digests,
            normalisation=normalisation,
            allow_excluded_resources=allow_excluded_resources,
        )

        calculated_digest = ocm.DigestSpec(
            hashAlgorithm='SHA-256',
            normalisationAlgorithm=normalisation,
            value=digest,
        )
        if component_reference.digest:
            verify_digest_spec(component_reference.digest, calculated_digest)
        else:
            component_reference_raw['digest'] = dataclasses.asdict(calculated_digest)

    # extra-identity is expected to be null instead of an empty object by OCM-cli in case it does
    # not contain any elements
    if not component_reference.extraIdentity:
        component_reference_raw['extraIdentity'] = None

    return normalise_obj(component_reference_raw)


def normalise_component(
    component: ocm.Component,
    component_descriptor_lookup: collections.abc.Callable[[ocm.ComponentIdentity], ocm.ComponentDescriptor], # noqa: E501
    access_to_digest_lookup: collections.abc.Callable[[ocm.Access], ocm.DigestSpec],
    verify_digests: bool=True,
    normalisation: ocm.NormalisationAlgorithm=ocm.NormalisationAlgorithm.JSON_NORMALISATION,
    allow_excluded_resources: bool=False,
) -> list[dict]:
    _check_normalisation(normalisation)
    component_raw = dataclasses.asdict(component)

    # drop properties not relevant for signing
    del component_raw['repositoryContexts']
    del component_raw['sources']

    if labels := [normalise_label(l) for l in component.labels if l.signing]:
        component_raw['labels'] = labels
    else:
        del component_raw['labels']

    # calculate missing digests for component references
    component_raw['componentReferences'] = [
        normalise_component_reference(
            component_reference=cref,
            component_descriptor_lookup=component_descriptor_lookup,
            access_to_digest_lookup=access_to_digest_lookup,
            verify_digests=verify_digests,
            normalisation=normalisation,
            allow_excluded_resources=allow_excluded_resources,
        ) for cref in component.componentReferences
    ]

    # calculate missing digests for resources; also, match OCM-cli's extra-identity handling by
    # implicitly adding the version to the extra-identity if the resource is not unique by its name
    # + existing extra-identity yet (not for the last resource as this is unique already if all
    # other resources have the version added to their extra-identity)
    resources = []
    for idx, resource in enumerate(component.resources):
        for peer in component.resources[idx+1:]:
            if (
                peer.identity(peers=()) == resource.identity(peers=())
                and 'version' not in resource.extraIdentity
            ):
                resource = dataclasses.replace(resource) # create copy
                resource.extraIdentity['version'] = resource.version

        resources.append(normalise_resource(
            resource=resource,
            access_to_digest_lookup=access_to_digest_lookup,
            verify_digests=verify_digests,
            allow_excluded_resources=allow_excluded_resources,
            component=component,
        ))
    component_raw['resources'] = resources

    # match OCM-cli's creation time normalisation by dropping the microseconds (round up if
    # necessary) and match expected format %Y-%m-%dT%H:%M:%SZ
    if component.creationTime:
        creation_time = datetime.datetime.fromisoformat(component.creationTime)
        if creation_time.microsecond >= 500000:
            creation_time += datetime.timedelta(seconds=1)
        component_raw['creationTime'] = creation_time.strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        del component_raw['creationTime']

    return normalise_obj(component_raw)


def normalise_component_descriptor(
    component_descriptor: ocm.ComponentDescriptor,
    component_descriptor_lookup: collections.abc.Callable[[ocm.ComponentIdentity], ocm.ComponentDescriptor], # noqa: E501
    access_to_digest_lookup: collections.abc.Callable[[ocm.Access], ocm.DigestSpec],
    verify_digests: bool=True,
    normalisation: ocm.NormalisationAlgorithm=ocm.NormalisationAlgorithm.JSON_NORMALISATION,
    allow_excluded_resources: bool=False,
) -> list[dict]:
    '''
    Returns a normalised version of the component descriptor by dropping signing-irrelevant
    properties, calculating missing digests and converting properties to the format expected by the
    OCM-cli (i.e. extra-identity handling and datetime-format).

    @param component_descriptor:
        the component descriptor of which a normalised representation should be created
    @param component_descriptor_lookup:
        lookup for component descriptors by their identity
    @param access_to_digest_lookup:
        used to retrieve the digest of an resource by its access specification
    @param verify_digests:
        if set, verify already existing digests instead of assuming they are correct
    @param normalisation:
        the algorithm used to create a normalised representation of the component descriptor
    @param allow_excluded_resources:
        explicitly permit resources whose content is excluded from the signature
    '''
    component_descriptor_raw = dataclasses.asdict(component_descriptor)

    # drop properties not relevant for signing
    del component_descriptor_raw['signatures']
    del component_descriptor_raw['nestedDigests']

    component_descriptor_raw['component'] = normalise_component(
        component=copy.deepcopy(component_descriptor.component),
        component_descriptor_lookup=component_descriptor_lookup,
        access_to_digest_lookup=access_to_digest_lookup,
        verify_digests=verify_digests,
        normalisation=normalisation,
        allow_excluded_resources=allow_excluded_resources,
    )

    return normalise_obj(component_descriptor_raw)


def component_descriptor_digest(
    component_descriptor: ocm.ComponentDescriptor,
    component_descriptor_lookup: collections.abc.Callable[[ocm.ComponentIdentity], ocm.ComponentDescriptor], # noqa: E501
    access_to_digest_lookup: collections.abc.Callable[[ocm.Access], ocm.DigestSpec],
    verify_digests: bool=True,
    normalisation: ocm.NormalisationAlgorithm=ocm.NormalisationAlgorithm.JSON_NORMALISATION,
    allow_excluded_resources: bool=False,
) -> str:
    '''
    Calculates the hexdigest of the recursively normalised component descriptor.

    @param component_descriptor:
        the component descriptor of which the digest should be calculated
    @param component_descriptor_lookup:
        lookup for component descriptors by their identity
    @param access_to_digest_lookup:
        used to retrieve the digest of an resource by its access specification
    @param verify_digests:
        if set, verify already existing digests instead of assuming they are correct
    @param normalisation:
        the algorithm used to create a normalised representation of the component descriptor as
        input for the digest calculation
    @param allow_excluded_resources:
        explicitly permit resources whose content is excluded from the signature
    '''
    normalised_component_descriptor = normalise_component_descriptor(
        component_descriptor=component_descriptor,
        component_descriptor_lookup=component_descriptor_lookup,
        access_to_digest_lookup=access_to_digest_lookup,
        verify_digests=verify_digests,
        normalisation=normalisation,
        allow_excluded_resources=allow_excluded_resources,
    )

    serialised_component_descriptor = json.dumps(
        obj=normalised_component_descriptor,
        separators=(',', ':'), # remove spaces after separators (match OCM-cli)
    )

    return hashlib.sha256(serialised_component_descriptor.encode()).hexdigest()
