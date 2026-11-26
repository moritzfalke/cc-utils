# SPDX-FileCopyrightText: Contributors to the Gardener project
# SPDX-License-Identifier: Apache-2.0

import argparse
import hashlib
import sys
from unittest.mock import Mock

import pytest

import ocm
import ocm.__main__ as cli


pytestmark = pytest.mark.usefixtures('no_network')


@pytest.mark.parametrize('command', ['validate', 'normalise'])
@pytest.mark.parametrize('extra,verify_digests,allow_excluded', [
    ([], True, False), (['--no-validate-digests'], False, False),
    (['--allow-excluded-resources'], True, True),
])
def test_normalisation_options(command, extra, verify_digests, allow_excluded, monkeypatch):
    handler = Mock()
    monkeypatch.setattr(cli, command, handler)
    arguments = [
        'ocm', command, '--ocm-repository', 'registry.example/ocm',
        '--name', 'example.org/component:1',
    ]
    if command == 'validate':
        arguments += ['--ca-file', 'example.pem']
    monkeypatch.setattr(sys, 'argv', arguments + extra)
    cli.main()
    parsed = handler.call_args.args[0]
    assert parsed.validate_digests is verify_digests
    assert parsed.allow_excluded_resources is allow_excluded


@pytest.mark.parametrize('field,value', [
    (None, None), ('hashAlgorithm', 'SHA-512'), ('normalisationAlgorithm', 'unsupported'),
    ('value', '0' * 64),
])
def test_signature_digest_metadata(
    field, value, monkeypatch, tmp_path, component_descriptor_factory,
):
    normalised = '[]'
    spec = ocm.DigestSpec(
        hashAlgorithm='SHA-256',
        normalisationAlgorithm=ocm.NormalisationAlgorithm.JSON_NORMALISATION,
        value=hashlib.sha256(normalised.encode()).hexdigest(),
    )
    if field:
        setattr(spec, field, value)
    descriptor = component_descriptor_factory()
    descriptor.signatures = [ocm.Signature(
        name='example', digest=spec,
        signature=ocm.SignatureSpec(
            algorithm='RSASSA-PSS', mediaType='application/octet-stream',
            value='-----BEGIN SIGNATURE-----\nSignature Algorithm: RSASSA-PSS\n\nZGVtbw==\n'
                  '-----END SIGNATURE-----',
        ),
    )]
    monkeypatch.setattr(cli, '_normalise', Mock(return_value=(descriptor, normalised)))
    certificate = Mock()
    monkeypatch.setattr(
        cli.cryptography.x509, 'load_pem_x509_certificates', Mock(return_value=[certificate]),
    )
    ca_file = tmp_path / 'example.pem'
    ca_file.write_bytes(b'example certificate')
    parsed = argparse.Namespace(ca_file=str(ca_file))
    if field:
        with pytest.raises(SystemExit) as error:
            cli.validate(parsed)
        assert error.value.code == 1
        certificate.public_key.assert_not_called()
    else:
        cli.validate(parsed)
        certificate.public_key.return_value.verify.assert_called_once()


@pytest.mark.parametrize('allow_excluded', [False, True])
def test_normalise_exclusion_option(
    allow_excluded, resource_factory, component_descriptor_factory, monkeypatch,
):
    descriptor = component_descriptor_factory([
        resource_factory(digest=ocm.ExcludeFromSignatureDigest()),
    ])
    client = Mock()
    monkeypatch.setattr(cli.oci.client, 'Client', Mock(return_value=client))
    monkeypatch.setattr(cli.oci.auth, 'docker_credentials_lookup', Mock(return_value=Mock()))
    monkeypatch.setattr(
        cli.ocm.retrieve, 'create_default_component_descriptor_lookup',
        Mock(return_value=lambda identity: descriptor),
    )
    parsed = argparse.Namespace(
        name='example.org/component:1.0.0', ocm_repository='registry.example/ocm', docker_cfg=None,
        validate_digests=True, allow_excluded_resources=allow_excluded,
    )
    if allow_excluded:
        result, normalised = cli._normalise(parsed)
        assert result is descriptor
        assert normalised
    else:
        with pytest.raises(ValueError, match='explicit opt-in'):
            cli._normalise(parsed)
    client.blob.assert_not_called()
    client.manifest_raw.assert_not_called()
