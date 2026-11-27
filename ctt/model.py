import dataclasses
import enum

import oci.model
import oci.digest
import ocm


@dataclasses.dataclass(frozen=True)
class AppendFilesEntry:
    src_path: str
    tgt_path: str


class AppendMode(enum.StrEnum):
    REPLACE_REMOVED = 'replace_removed'
    INSERT_INTO_LAYER = 'insert_into_layer'
    NEW_LAYER = 'new_layer'


@dataclasses.dataclass
class ReplicationResourceOptions:
    extra_tags: list[str] = dataclasses.field(default_factory=list)
    remove_files: list[str] = dataclasses.field(default_factory=list)
    # glob patterns matched against in-image file paths; entries matching any of these are
    # removed alongside `remove_files`. Patterns are evaluated with `fnmatch.fnmatchcase`
    # after stripping a single leading `/`. Example: `/etc/ssl/certs/*.pem`.
    remove_file_globs: list[str] = dataclasses.field(default_factory=list)
    # attributes to strip from the top-level OCI manifest document, as dotted paths
    # (`[]` traverses into each element of a list), e.g. 'manifests[].platform.features'
    strip_manifest_attributes: list[str] = dataclasses.field(default_factory=list)
    append_files: list[AppendFilesEntry] = dataclasses.field(default_factory=list)
    append_mode: AppendMode = AppendMode.REPLACE_REMOVED
    # only used if append_mode is INSERT_INTO_LAYER; Python-style index (negative allowed)
    append_layer_idx: int | None = None
    digest: str | None = None
    reference_by_digest: bool = False
    retain_symbolic_tag: bool = False
    convert_to_relative_ref: bool = False
    # if set, this ref is written into the component-descriptor instead of the actual push target
    descriptor_ref_override: str | None = None
    inject_sboms: bool = False
    # if True, 409 responses when pushing extra tags (e.g. from immutable tag policies) are
    # downgraded to a warning instead of raising
    ignore_extra_tag_policy_violations: bool = False


@dataclasses.dataclass(kw_only=True)
class ReplicationResourceElement(ReplicationResourceOptions):
    source: ocm.Resource
    target: ocm.Resource
    component_id: ocm.ComponentIdentity
    src_ocm_repo: ocm.OciOcmRepository

    @property
    def src_ref(self) -> oci.model.OciImageReference:
        '''The original access reference, used for uploader naming and metadata.'''
        if self.source.access.type is ocm.AccessType.OCI_REGISTRY:
            return oci.model.OciImageReference(self.source.access.imageReference)
        elif self.source.access.type is ocm.AccessType.RELATIVE_OCI_REFERENCE:
            src_ocm_repo = oci.model.OciImageReference(self.src_ocm_repo.oci_ref)
            full_ref = '/'.join((
                src_ocm_repo.netloc,
                self.source.access.reference.lstrip('/'),
            ))
            return oci.model.OciImageReference(full_ref, normalise=False)
        else:
            raise ValueError(self.source.access.type)

    @property
    def replication_src_ref(self) -> oci.model.OciImageReference:
        '''Select the source content using its qualified OCI resource digest.'''
        reference = self.src_ref
        digest = self.source.digest
        if not isinstance(digest, ocm.DigestSpec):
            raise ValueError('CTT replication requires a resource digest')
        algorithm = {'SHA-256': 'sha256', 'SHA-512': 'sha512'}.get(digest.hashAlgorithm)
        normalisation = digest.normalisationAlgorithm
        if not algorithm or normalisation != ocm.NormalisationAlgorithm.OCI_ARTIFACT_DIGEST:
            raise ValueError('CTT replication requires a supported OCI artifact digest')
        approved = oci.digest.DigestVerifier(
            f'{algorithm}:{digest.value}', reference,
        ).expected_digest
        if reference.has_digest_tag:
            access_digest = oci.digest.DigestVerifier(reference.tag, reference).expected_digest
            # Different hash algorithms cannot be compared without content. In that
            # case the qualified resource digest remains the authoritative address.
            if access_digest.startswith(f'{algorithm}:') and access_digest != approved:
                raise oci.digest.DigestMismatchError(approved, access_digest, reference)
        return oci.model.OciImageReference(
            f'{reference.ref_without_tag}@{approved}', normalise=False,
        )

    @property
    def tgt_ref(self) -> oci.model.OciImageReference:
        if self.target.access.type is ocm.AccessType.OCI_REGISTRY:
            return oci.model.OciImageReference(self.target.access.imageReference)
        elif self.target.access.type is ocm.AccessType.RELATIVE_OCI_REFERENCE:
            return oci.model.OciImageReference(self.target.access.reference, normalise=False)
        else:
            raise ValueError(self.source.access.type)

    @property
    def preliminary_tgt_ref(self) -> oci.model.OciImageReference:
        '''
        In case the resource does not exist in the target yet, we don't know the digest for sure
        (for example, there might be filtering). To show a reasonable output in the replication plan
        nonetheless, use a placeholder digest in case no digest can be determined to show that the
        replicated resource will contain a digest tag.
        '''
        if not self.reference_by_digest:
            return self.tgt_ref

        tgt_ref = self.tgt_ref

        if self.digest:
            digest = self.digest
        elif tgt_ref.has_digest_tag:
            digest = self.tgt_ref.tag
        else:
            digest = 'sha256:<placeholder-digest>'

        if (
            self.retain_symbolic_tag
            and (tgt_ref.has_symbolical_tag or tgt_ref.has_mixed_tag)
        ):
            return oci.model.OciImageReference(f'{tgt_ref.with_symbolical_tag}@{digest}')
        else:
            return oci.model.OciImageReference(f'{tgt_ref.ref_without_tag}@{digest}')

    def __str__(self) -> str:
        return (
            f'{self.source.name}:{self.source.version} '
            f'[{self.replication_src_ref} -> {self.preliminary_tgt_ref}]'
        )


@dataclasses.dataclass
class ReplicationComponentElement:
    source: ocm.ComponentDescriptor
    target: ocm.ComponentDescriptor

    @property
    def src_ref(self) -> str:
        component = self.source.component
        return component.repositoryContexts[-1].component_version_oci_ref(component)

    @property
    def tgt_ref(self) -> str:
        component = self.target.component
        return component.repositoryContexts[-1].component_version_oci_ref(component)

    def __str__(self) -> str:
        return (
            f'{self.source.component.name}:{self.target.component.version} '
            f'[{self.src_ref} -> {self.tgt_ref}]'
        )


@dataclasses.dataclass
class ReplicationPlanStep:
    '''
    To each target registry, _all_ OCM component descriptors are replicated (independent of
    configuration) as well as their respective OCI resources (based on the `processing.cfg`).
    The replication of the `resources` can be done in parallel, however, the OCM component
    descriptors must be replicated sequentially to allow early-exiting in case a root component
    descriptor already exists in the target registry.
    '''
    target_ocm_repository: str
    resources: tuple[ReplicationResourceElement]
    components: tuple[ReplicationComponentElement]

    def __str__(self) -> str:
        resources = (
            resource
            for resource in sorted(
                self.resources,
                key=lambda resource: (resource.source.name, resource.source.version),
            )
        )

        return f'''\
- **{self.target_ocm_repository}**

  1. Replication of OCI resources (processed in-parallel)

{
    '\n'.join(
        f'    - {resource}'
        for resource in resources
    ) or '    None'
}

  2. Replication of OCM component descriptors (processed sequentially)

{
    '\n'.join(
        f'    {idx + 1}. {component}'
        for idx, component in enumerate(self.components)
    ) or '    None'
}
'''


@dataclasses.dataclass
class ReplicationPlan:
    '''
    Contains all information required for replication. The described `steps` can be processed in
    parallel. Each `step` describes the replication to a different target registry.
    '''
    steps: list[ReplicationPlanStep] = dataclasses.field(default_factory=list)

    def __str__(self) -> str:
        return f'''\
**Replication Plan**

Targets: {[step.target_ocm_repository for step in self.steps]}

{'\n'.join(str(step) for step in self.steps)}
'''
