import dataclasses
import hashlib
import typing
import urllib.parse

import ocm

import ioutil
import oci.client
import oci.digest
import oci.model


if typing.TYPE_CHECKING:
    import botocore.client


def s3_access_as_blob_descriptor(
    s3_client: 'botocore.client.S3',
    s3_access: ocm.S3Access | ocm.LegacyS3Access,
    chunk_size: int=8192,
    name: str=None,
) -> ioutil.BlobDescriptor:
    if not s3_client:
        raise ValueError('must pass-in s3-client')

    if isinstance(s3_access, ocm.LegacyS3Access):
        bucket = s3_access.bucketName
        key = s3_access.objectKey
    else:
        bucket = s3_access.bucket
        key = s3_access.key

    blob = s3_client.get_object(Bucket=bucket, Key=key)

    size = blob['ContentLength']
    body = blob['Body']

    return ioutil.BlobDescriptor(
        content=body.iter_chunks(chunk_size=chunk_size),
        size=size,
        name=name or f's3://{bucket}/{key}',
    )


def resolve_access(
    access: ocm.Access,
    component: ocm.Component=None,
) -> ocm.Access:
    '''Resolve component-relative access without changing the descriptor's signed representation.'''
    try:
        access_type = ocm.AccessType(access.type)
    except (ValueError, TypeError) as e:
        raise ValueError(f'Unsupported access type: {access.type!r}') from e

    if isinstance(access, dict):
        access_class = {
            ocm.AccessType.OCI_REGISTRY: ocm.OciAccess,
            ocm.AccessType.OCI_BLOB: ocm.OciBlobAccess,
            ocm.AccessType.LOCAL_BLOB: ocm.LocalBlobAccess,
            ocm.AccessType.RELATIVE_OCI_REFERENCE: ocm.RelativeOciAccess,
            ocm.AccessType.S3: ocm.S3Access,
            ocm.AccessType.S3_V2: ocm.S3Access,
        }.get(access_type)
        if not access_class:
            raise ValueError(f'Unsupported access type: {access.type!r}')
        if access_type in (ocm.AccessType.S3, ocm.AccessType.S3_V2) and 'bucketName' in access:
            access_class = ocm.LegacyS3Access
        try:
            access = access_class(**(dict(access) | {'type': access_type}))
        except TypeError as e:
            raise ValueError('Invalid resource access specification') from e
    elif dataclasses.is_dataclass(access):
        access = dataclasses.replace(access, type=access_type)

    if access_type is ocm.AccessType.RELATIVE_OCI_REFERENCE:
        if not component or not component.current_ocm_repo:
            raise ValueError('Relative OCI access requires the owning component repository')
        return to_absolute_oci_access(access, component.current_ocm_repo)

    if access_type is not ocm.AccessType.LOCAL_BLOB:
        return access

    # Both the local and global access paths must identify the same blob. A global
    # access may refer back to another repository after copy-by-reference replication.
    local_digest = oci.digest.DigestVerifier(
        access.localReference, 'localBlob',
    ).expected_digest
    if global_access := access.globalAccess:
        if isinstance(global_access, dict):
            try:
                global_access = ocm.LocalBlobGlobalAccess(**global_access)
            except TypeError as e:
                raise ValueError('Invalid local blob global access') from e
        try:
            global_type = ocm.AccessType(global_access.type)
        except (ValueError, TypeError) as e:
            raise ValueError('Unsupported local blob global access type') from e
        if global_type is not ocm.AccessType.OCI_BLOB:
            raise ValueError('Unsupported local blob global access type')
        global_digest = oci.digest.DigestVerifier(
            global_access.digest, global_access.ref,
        ).expected_digest
        if local_digest != global_digest:
            raise ValueError('Local and global blob references must identify the same content')
        return ocm.OciBlobAccess(
            imageReference=global_access.ref,
            digest=global_digest,
            size=global_access.size,
            mediaType=global_access.mediaType,
        )

    if not component or not component.current_ocm_repo:
        raise ValueError('Local blob access requires the owning component repository')
    return ocm.OciBlobAccess(
        imageReference=component.current_ocm_repo.component_version_oci_ref(component),
        digest=local_digest,
        size=access.size,
        mediaType=access.mediaType,
    )


def access_to_digest_lookup(
    access: ocm.Access,
    oci_client: oci.client.Client=None,
    s3_client: 'botocore.client.S3'=None,
    chunk_size: int=8192,
    component: ocm.Component=None,
) -> ocm.DigestSpec:
    '''Calculate a digest from fetched content, never from access metadata alone.'''
    access = resolve_access(access, component)
    access_type = ocm.AccessType(access.type)
    if access_type in (ocm.AccessType.OCI_REGISTRY, ocm.AccessType.OCI_BLOB):
        if not oci_client:
            raise ValueError('OCI access requires an OCI client')
        digest = hashlib.sha256()
        if access_type is ocm.AccessType.OCI_REGISTRY:
            response = oci_client.manifest_raw(
                image_reference=access.imageReference,
                accept=oci.model.MimeTypes.prefer_multiarch,
            )
            normalisation = ocm.NormalisationAlgorithm.OCI_ARTIFACT_DIGEST
        else:
            response = oci_client.blob(
                image_reference=access.imageReference,
                digest=access.digest,
            )
            normalisation = ocm.NormalisationAlgorithm.GENERIC_BLOB_DIGEST
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                digest.update(chunk)
        finally:
            response.close()
        return ocm.DigestSpec(
            hashAlgorithm='SHA-256',
            normalisationAlgorithm=normalisation,
            value=digest.hexdigest(),
        )

    elif access_type in (
        ocm.AccessType.S3,
        ocm.AccessType.S3_V2,
    ):
        if not s3_client:
            raise ValueError('S3 access requires an S3 client')
        if isinstance(access, ocm.LegacyS3Access):
            bucket = access.bucketName
            key = access.objectKey
        else:
            bucket = access.bucket
            key = access.key

        blob = s3_client.get_object(Bucket=bucket, Key=key)['Body']

        digest = hashlib.sha256()
        try:
            for chunk in blob.iter_chunks(chunk_size=chunk_size):
                digest.update(chunk)
        finally:
            blob.close()

        return ocm.DigestSpec(
            hashAlgorithm='SHA-256',
            normalisationAlgorithm=ocm.NormalisationAlgorithm.GENERIC_BLOB_DIGEST,
            value=digest.hexdigest(),
        )

    raise ValueError(f'Unsupported access type: {access.type!r}')


def artifact_url(
    component: ocm.Component,
    artifact: ocm.Resource | ocm.Source,
) -> str:
    access = artifact.access

    if isinstance(access, ocm.GithubAccess):
        return access.repoUrl

    elif isinstance(access, ocm.LocalBlobAccess):
        image_reference = component.current_ocm_repo.component_oci_ref(component.name)
        return f'{image_reference}@{access.localReference}'

    elif isinstance(access, ocm.OciAccess):
        return access.imageReference

    elif isinstance(access, ocm.RelativeOciAccess):
        return access.reference

    elif isinstance(access, ocm.S3Access):
        return f'http://{access.bucket}.s3.amazonaws.com/{access.key}'

    elif isinstance(access, ocm.LegacyS3Access):
        return f'http://{access.bucketName}.s3.amazonaws.com/{access.objectKey}'

    elif isinstance(access, ocm.LocalBlobGlobalAccess):
        return access.ref

    else:
        raise ValueError(access)


def to_absolute_oci_access(
    access: ocm.OciAccess | ocm.RelativeOciAccess,
    ocm_repo: ocm.OciOcmRepository | None=None,
) -> ocm.OciAccess:
    if access.type is ocm.AccessType.OCI_REGISTRY:
        pass

    elif access.type is ocm.AccessType.RELATIVE_OCI_REFERENCE:
        if not '://' in ocm_repo.baseUrl:
            base_url = urllib.parse.urlparse(f'x://{ocm_repo.baseUrl}').netloc
        else:
            base_url = urllib.parse.urlparse(ocm_repo.baseUrl).netloc

        access = ocm.OciAccess(
            imageReference=f'{base_url.rstrip('/')}/{access.reference.lstrip('/')}',
        )

    else:
        raise ValueError(f'Unsupported access type: {access.type}')

    return access


def normalise_component_name(component_name: str) -> str:
    return component_name.lower()  # oci-spec demands lowercase


def to_component_id_and_repository_url(
    component: ocm.Component | ocm.ComponentDescriptor | ocm.ComponentIdentity | str,
    repository: ocm.OciOcmRepository | str=None,
) -> tuple[ocm.Component | ocm.ComponentIdentity, str]:
    if isinstance(component, str):
        name, version = component.rsplit(':', 1)
        component = ocm.ComponentIdentity(
            name=name,
            version=version,
        )

    if isinstance(component, ocm.ComponentDescriptor):
        component = component.component
    elif isinstance(component, ocm.ComponentIdentity) and not repository:
        raise ValueError('repository must be passed if calling w/ component-identity')

    if not repository:  # component is sure to be of type ocm.Component by now (checked above)
        repository = component.current_ocm_repo

    if isinstance(repository, ocm.OciOcmRepository):
        repo_base_url = repository.baseUrl
    elif isinstance(repository, str):
        repo_base_url = repository
    else:
        raise ValueError(f'only OciOcmRepository is supported - got: {repository=}')

    return component, repo_base_url


def oci_ref(
    component: ocm.Component | ocm.ComponentDescriptor | ocm.ComponentIdentity | str,
    repository: ocm.OciOcmRepository | str=None,
) -> oci.model.OciImageReference:
    component, repo_base_url = to_component_id_and_repository_url(
        component=component,
        repository=repository,
    )

    return oci.model.OciImageReference(
        '/'.join((
            repo_base_url.rstrip('/'),
            'component-descriptors',
            f'{component.name.lower()}:{component.version}',
        )),
    )


def target_oci_ref(
    component: ocm.Component,
    component_ref: ocm.ComponentReference=None,
    component_version: str=None,
) -> str:
    if not component_ref:
        component_ref = component
        component_name = component_ref.name
    else:
        component_name = component_ref.componentName

    component_name = normalise_component_name(component_name)
    component_version = component_ref.version

    last_ocm_repo = component.current_ocm_repo

    return last_ocm_repo.component_version_oci_ref(
        name=component_name,
        version=component_version,
    )


def oci_artefact_reference(
    component: (
        ocm.Component
        | ocm.ComponentIdentity
        | ocm.ComponentReference
        | str  # 'name:version'
        | tuple[str, str]  # (name, version)
    ),
    ocm_repository: str | ocm.OciOcmRepository=None,
) -> str:
    if isinstance(component, ocm.Component):
        if not ocm_repository:
            ocm_repository = component.current_ocm_repo
        component_name = component.name
        component_version = component.version
    elif isinstance(component, ocm.ComponentIdentity):
        component_name = component.name
        component_version = component.version
    elif isinstance(component, ocm.ComponentReference):
        component_name = component.componentName
        component_version = component.version
    elif isinstance(component, str):
        component_name, component_version = component.split(':')
    elif isinstance(component, tuple):
        if not len(component) == 2 or not all(isinstance(x, str) for x in component):
            raise TypeError('if a tuple is given as component, it must contain two strings')
        component_name, component_version = component
    else:
        raise ValueError(component)

    if not ocm_repository:
        raise ValueError('ocm_repository must be given unless a Component is passed.')
    if isinstance(ocm_repository, str):
        ocm_repository = ocm.OciOcmRepository(baseUrl=ocm_repository)
    elif not isinstance(ocm_repository, ocm.OciOcmRepository):
        raise TypeError(type(ocm_repository))

    return ocm_repository.component_version_oci_ref(
        name=component_name,
        version=component_version,
    )
