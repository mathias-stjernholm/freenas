import datetime
import dateutil
import dateutil.parser
import josepy as jose
import json
import os
import pytz
import random
import re
import socket
import ssl

from middlewared.async_validators import validate_country
from middlewared.schema import accepts, Bool, Dict, Int, List, Patch, Ref, Str
from middlewared.service import CRUDService, job, periodic, private, skip_arg, ValidationErrors
from middlewared.validators import Email, IpAddress, Range, ShouldBe

from acme import client, errors, messages
from OpenSSL import crypto, SSL


CA_TYPE_EXISTING = 0x01
CA_TYPE_INTERNAL = 0x02
CA_TYPE_INTERMEDIATE = 0x04
CERT_TYPE_EXISTING = 0x08
CERT_TYPE_INTERNAL = 0x10
CERT_TYPE_CSR = 0x20

CERT_ROOT_PATH = '/etc/certificates'
CERT_CA_ROOT_PATH = '/etc/certificates/CA'
RE_CERTIFICATE = re.compile(r"(-{5}BEGIN[\s\w]+-{5}[^-]+-{5}END[\s\w]+-{5})+", re.M | re.S)


def get_context_object():
    # BEING USED IN VCENTERPLUGIN SERVICE
    try:
        ssl._create_default_https_context = ssl._create_unverified_context
    except AttributeError:
        pass
    context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
    context.verify_mode = ssl.CERT_NONE
    return context


def get_cert_info_from_data(data):
    cert_info_keys = ['key_length', 'country', 'state', 'city', 'organization', 'common',
                      'san', 'serial', 'email', 'lifetime', 'digest_algorithm']
    return {key: data.get(key) for key in cert_info_keys}


async def validate_cert_name(middleware, cert_name, datastore, verrors, name):
    certs = await middleware.call(
        'datastore.query',
        datastore,
        [('cert_name', '=', cert_name)]
    )
    if certs:
        verrors.add(
            name,
            'A certificate with this name already exists'
        )

    if cert_name in ("external", "self-signed", "external - signature pending"):
        verrors.add(
            name,
            f'{cert_name} is a reserved internal keyword for Certificate Management'
        )
    reg = re.search(r'^[a-z0-9_\-]+$', cert_name or '', re.I)
    if not reg:
        verrors.add(
            name,
            'Use alphanumeric characters, "_" and "-".'
        )


def _set_required(name):
    def set_r(attr):
        attr.required = True
    return {'name': name, 'method': set_r}


def load_private_key(buffer, passphrase=None):
    try:
        return crypto.load_privatekey(
            crypto.FILETYPE_PEM,
            buffer,
            passphrase=passphrase.encode() if passphrase else None
        )
    except crypto.Error:
        return None


def export_private_key(buffer, passphrase=None):
    key = load_private_key(buffer, passphrase)
    if key:
        return crypto.dump_privatekey(
            crypto.FILETYPE_PEM,
            key,
            passphrase=passphrase.encode() if passphrase else None
        ).decode()


def generate_key(key_length):
    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, key_length)
    return k


async def _validate_common_attributes(middleware, data, verrors, schema_name):

    def _validate_certificate_with_key(certificate, private_key, schema_name, verrors):
        if (
                (certificate and private_key) and
                all(k not in verrors for k in (f'{schema_name}.certificate', f'{schema_name}.privatekey'))
        ):
            public_key_obj = crypto.load_certificate(crypto.FILETYPE_PEM, certificate)
            private_key_obj = load_private_key(private_key, passphrase)

            try:
                context = SSL.Context(SSL.TLSv1_2_METHOD)
                context.use_certificate(public_key_obj)
                context.use_privatekey(private_key_obj)
                context.check_privatekey()
            except SSL.Error as e:
                verrors.add(
                    f'{schema_name}.privatekey',
                    f'Private key does not match certificate: {e}'
                )

    country = data.get('country')
    if country:
        await validate_country(middleware, country, verrors, f'{schema_name}.country')

    certificate = data.get('certificate')
    if certificate:
        matches = RE_CERTIFICATE.findall(certificate)

        nmatches = len(matches)
        if not nmatches:
            verrors.add(
                f'{schema_name}.certificate',
                'Not a valid certificate'
            )
        else:
            cert_info = await middleware.call('certificate.load_certificate', certificate)
            if not cert_info:
                verrors.add(
                    f'{schema_name}.certificate',
                    'Certificate not in PEM format'
                )

    private_key = data.get('privatekey')
    passphrase = data.get('passphrase')
    if private_key:
        if not load_private_key(private_key, passphrase):
            verrors.add(
                f'{schema_name}.privatekey',
                'Please provide a valid private key with matching passphrase ( if any )'
            )

    key_length = data.get('key_length')
    if key_length:
        if key_length not in [1024, 2048, 4096]:
            verrors.add(
                f'{schema_name}.key_length',
                'Key length must be a valid value ( 1024, 2048, 4096 )'
            )

    signedby = data.get('signedby')
    if signedby:
        valid_signing_ca = await middleware.call(
            'certificateauthority.query',
            [
                ('certificate', '!=', None),
                ('privatekey', '!=', None),
                ('certificate', '!=', ''),
                ('privatekey', '!=', ''),
                ('id', '=', signedby)
            ],
        )

        if not valid_signing_ca:
            verrors.add(
                f'{schema_name}.signedby',
                'Please provide a valid signing authority'
            )

    csr = data.get('CSR')
    if csr:
        if not await middleware.call('certificate.load_certificate_request', csr):
            verrors.add(
                f'{schema_name}.CSR',
                'Please provide a valid CSR'
            )

    csr_id = data.get('csr_id')
    if csr_id and not await middleware.call('certificate.query', [['id', '=', csr_id]]):
        verrors.add(
            f'{schema_name}.csr_id',
            'Please provide a valid csr_id'
        )

    await middleware.run_in_io_thread(
        _validate_certificate_with_key, certificate, private_key, schema_name, verrors
    )


class CertificateService(CRUDService):

    class Config:
        datastore = 'system.certificate'
        datastore_extend = 'certificate.cert_extend'
        datastore_prefix = 'cert_'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.map_functions = {
            'CERTIFICATE_CREATE_INTERNAL': self.__create_internal,
            'CERTIFICATE_CREATE_IMPORTED': self.__create_imported_certificate,
            'CERTIFICATE_CREATE_IMPORTED_CSR': self.__create_imported_csr,
            'CERTIFICATE_CREATE': self.__create_certificate,
            'CERTIFICATE_CREATE_CSR': self.__create_csr,
            'CERTIFICATE_CREATE_ACME': self.__create_acme_certificate
        }

    @private
    async def cert_extend(self, cert):
        """Extend certificate with some useful attributes."""

        if cert.get('signedby'):

            # We query for signedby again to make sure it's keys do not have the "cert_" prefix and it has gone through
            # the cert_extend method

            cert['signedby'] = await self.middleware.call(
                'datastore.query',
                'system.certificateauthority',
                [('id', '=', cert['signedby']['id'])],
                {
                    'prefix': 'cert_',
                    'extend': 'certificate.cert_extend',
                    'get': True
                }
            )

        # Remove ACME related keys if cert is not an ACME based cert
        if not cert.get('acme'):
            for key in ['acme', 'acme_uri', 'domains_authenticators', 'renew_days']:
                cert.pop(key)

        # convert san to list
        cert['san'] = (cert.pop('san', '') or '').split()
        if cert['serial'] is not None:
            cert['serial'] = int(cert['serial'])

        if cert['type'] in (
                CA_TYPE_EXISTING, CA_TYPE_INTERNAL, CA_TYPE_INTERMEDIATE
        ):
            root_path = CERT_CA_ROOT_PATH
        else:
            root_path = CERT_ROOT_PATH
        cert['root_path'] = root_path
        cert['certificate_path'] = os.path.join(
            root_path, '{0}.crt'.format(cert['name'])
        )
        cert['privatekey_path'] = os.path.join(
            root_path, '{0}.key'.format(cert['name'])
        )
        cert['csr_path'] = os.path.join(
            root_path, '{0}.csr'.format(cert['name'])
        )

        def cert_issuer(cert):
            issuer = None
            if cert['type'] in (CA_TYPE_EXISTING, CERT_TYPE_EXISTING):
                issuer = "external"
            elif cert['type'] == CA_TYPE_INTERNAL:
                issuer = "self-signed"
            elif cert['type'] in (CERT_TYPE_INTERNAL, CA_TYPE_INTERMEDIATE):
                issuer = cert['signedby']
            elif cert['type'] == CERT_TYPE_CSR:
                issuer = "external - signature pending"
            return issuer

        cert['issuer'] = cert_issuer(cert)

        cert['chain_list'] = []
        if cert['chain']:
            certs = RE_CERTIFICATE.findall(cert['certificate'])
        else:
            certs = [cert['certificate']]
            signing_CA = cert['issuer']
            # Recursively get all internal/intermediate certificates
            # FIXME: NONE HAS BEEN ADDED IN THE FOLLOWING CHECK FOR CSR'S WHICH HAVE BEEN SIGNED BY A CA
            while signing_CA not in ["external", "self-signed", "external - signature pending", None]:
                certs.append(signing_CA['certificate'])
                signing_CA['issuer'] = cert_issuer(signing_CA)
                signing_CA = signing_CA['issuer']

        cert_obj = None
        try:
            for c in certs:
                # XXX Why load certificate if we are going to dump it right after?
                # Maybe just to verify its integrity?
                # Logic copied from freenasUI
                if c:
                    cert_obj = crypto.load_certificate(crypto.FILETYPE_PEM, c)
                    cert['chain_list'].append(
                        crypto.dump_certificate(crypto.FILETYPE_PEM, cert_obj).decode()
                    )
        except Exception:
            self.logger.debug('Failed to load certificate {0}'.format(cert['name']), exc_info=True)

        try:
            if cert['privatekey']:
                key_obj = crypto.load_privatekey(crypto.FILETYPE_PEM, cert['privatekey'])
                cert['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, key_obj).decode()
        except Exception:
            self.logger.debug('Failed to load privatekey {0}'.format(cert['name']), exc_info=True)

        try:
            if cert['CSR']:
                csr_obj = crypto.load_certificate_request(crypto.FILETYPE_PEM, cert['CSR'])
                cert['CSR'] = crypto.dump_certificate_request(crypto.FILETYPE_PEM, csr_obj).decode()
        except Exception:
            self.logger.debug('Failed to load csr {0}'.format(cert['name']), exc_info=True)

        cert['internal'] = 'NO' if cert['type'] in (CA_TYPE_EXISTING, CERT_TYPE_EXISTING) else 'YES'

        obj = None
        # date not applicable for CSR
        cert['from'] = None
        cert['until'] = None
        if cert['type'] == CERT_TYPE_CSR:
            obj = csr_obj
        elif cert_obj:
            obj = crypto.load_certificate(crypto.FILETYPE_PEM, cert['certificate'])
            notBefore = obj.get_notBefore()
            t1 = dateutil.parser.parse(notBefore)
            t2 = t1.astimezone(dateutil.tz.tzutc())
            cert['from'] = t2.ctime()

            notAfter = obj.get_notAfter()
            t1 = dateutil.parser.parse(notAfter)
            t2 = t1.astimezone(dateutil.tz.tzutc())
            cert['until'] = t2.ctime()

        if obj:
            cert['DN'] = '/' + '/'.join([
                '%s=%s' % (c[0].decode(), c[1].decode())
                for c in obj.get_subject().get_components()
            ])

        return cert

    # HELPER METHODS

    @private
    @accepts(
        Str('hostname', required=True),
        Int('port', required=True)
    )
    def get_host_certificates_thumbprint(self, hostname, port):
        try:
            conn = ssl.create_connection((hostname, port))
            context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
            sock = context.wrap_socket(conn, server_hostname=hostname)
            certificate = ssl.DER_cert_to_PEM_cert(sock.getpeercert(True))
            return self.fingerprint(certificate)
        except ConnectionRefusedError:
            return ''
        except socket.gaierror:
            return ''

    @private
    @accepts(
        Str('certificate', required=True)
    )
    def load_certificate(self, certificate):
        try:
            cert = crypto.load_certificate(
                crypto.FILETYPE_PEM,
                certificate
            )
        except crypto.Error:
            return {}
        else:
            cert_info = self.get_x509_subject(cert)
            cert_info['serial'] = cert.get_serial_number()

            signature_algorithm = cert.get_signature_algorithm().decode()
            m = re.match('^(.+)[Ww]ith', signature_algorithm)
            if m:
                cert_info['digest_algorithm'] = m.group(1).upper()

            return cert_info

    @private
    def get_x509_subject(self, obj):
        return {
            'country': obj.get_subject().C,
            'state': obj.get_subject().ST,
            'city': obj.get_subject().L,
            'organization': obj.get_subject().O,
            'common': obj.get_subject().CN,
            'san': obj.get_subject().subjectAltName,
            'email': obj.get_subject().emailAddress,
        }

    @private
    @accepts(
        Str('csr', required=True)
    )
    def load_certificate_request(self, csr):
        try:
            csr = crypto.load_certificate_request(crypto.FILETYPE_PEM, csr)
        except crypto.Error:
            return {}
        else:
            return self.get_x509_subject(csr)

    @private
    async def get_fingerprint_of_cert(self, certificate_id):
        certificate_list = await self.query(filters=[('id', '=', certificate_id)])
        if len(certificate_list) == 0:
            return None
        else:
            return await self.middleware.run_in_io_thread(
                self.fingerprint,
                certificate_list[0]['certificate']
            )

    @private
    @accepts(
        Str('cert_certificate', required=True)
    )
    def fingerprint(self, cert_certificate):
        # getting fingerprint of certificate
        try:
            certificate = crypto.load_certificate(
                crypto.FILETYPE_PEM,
                cert_certificate
            )
        except Exception:
            return None
        else:
            return certificate.digest('sha1').decode()

    @private
    async def san_to_string(self, san_list):
        # TODO: ADD MORE TYPES WRT RFC'S
        san_string = ''
        ip_validator = IpAddress()
        for count, san in enumerate(san_list or []):
            try:
                ip_validator(san)
            except ShouldBe:
                san_string += f'DNS: {san}, '
            else:
                san_string += f'IP: {san}, '
        return san_string[:-2] if san_list else ''

    @private
    @accepts(
        Dict(
            'certificate_cert_info',
            Int('key_length'),
            Int('serial', required=False, null=True),
            Int('lifetime', required=True),
            Str('country', required=True),
            Str('state', required=True),
            Str('city', required=True),
            Str('organization', required=True),
            Str('common', required=True),
            Str('email', validators=[Email()], required=True),
            Str('digest_algorithm', enum=['SHA1', 'SHA224', 'SHA256', 'SHA384', 'SHA512']),
            List('san', items=[Str('san')], null=True),
            register=True
        )
    )
    def create_certificate(self, cert_info):

        cert_info['san'] = self.middleware.call_sync(
            'certificate.san_to_string',
            cert_info.pop('san', [])
        )

        cert = crypto.X509()
        cert.get_subject().C = cert_info['country']
        cert.get_subject().ST = cert_info['state']
        cert.get_subject().L = cert_info['city']
        cert.get_subject().O = cert_info['organization']
        cert.get_subject().CN = cert_info['common']
        # Add subject alternate name in addition to CN

        if cert_info['san']:
            cert.add_extensions([crypto.X509Extension(
                b"subjectAltName", False, cert_info['san'].encode()
            )])
            cert.get_subject().subjectAltName = cert_info['san']
        cert.get_subject().emailAddress = cert_info['email']

        serial = cert_info.get('serial')
        if serial is not None:
            cert.set_serial_number(serial)

        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(cert_info['lifetime'] * (60 * 60 * 24))

        cert.set_issuer(cert.get_subject())
        # Setting it to '2' actually results in a v3 cert
        # openssl's cert x509 versions are zero-indexed!
        # see: https://www.ietf.org/rfc/rfc3280.txt
        cert.set_version(2)
        return cert

    @private
    @accepts(
        Patch(
            'certificate_cert_info', 'certificate_signing_request',
            ('rm', {'name': 'lifetime'})
        )
    )
    def create_certificate_signing_request(self, cert_info):

        cert_info['san'] = self.middleware.call_sync(
            'certificate.san_to_string',
            cert_info.pop('san', [])
        )

        key = generate_key(cert_info['key_length'])

        req = crypto.X509Req()
        req.get_subject().C = cert_info['country']
        req.get_subject().ST = cert_info['state']
        req.get_subject().L = cert_info['city']
        req.get_subject().O = cert_info['organization']
        req.get_subject().CN = cert_info['common']

        if cert_info['san']:
            req.add_extensions(
                [crypto.X509Extension(b"subjectAltName", False, cert_info['san'].encode())])
            req.get_subject().subjectAltName = cert_info['san']
        req.get_subject().emailAddress = cert_info['email']

        req.set_pubkey(key)
        req.sign(key, cert_info['digest_algorithm'])

        return (req, key)

    @private
    async def validate_common_attributes(self, data, schema_name):
        verrors = ValidationErrors()

        await _validate_common_attributes(self.middleware, data, verrors, schema_name)

        return verrors

    @private
    async def get_domain_names(self, data):
        names = [data['common']]
        names.extend(data['san'])
        return names

    @private
    def get_acme_client_and_key(self, acme_directory_uri, tos=False):
        data = self.middleware.call_sync('acme.registration.query', [['directory', '=', acme_directory_uri]])
        if not data:
            data = self.middleware.call_sync(
                'acme.registration.create',
                {'tos': tos, 'acme_directory_uri': acme_directory_uri}
            )
        else:
            data = data[0]
        # Making key now
        key = jose.JWKRSA.fields_from_json(json.loads(data['body']['key']))
        key_dict = key.fields_to_partial_json()
        # Making registration resource now
        registration = messages.RegistrationResource.from_json({
            'uri': data['uri'],
            'terms_of_service': data['tos'],
            'body': {
                'contact': [data['body']['contact']],
                'status': data['body']['status'],
                'key': {
                    'e': key_dict['e'],
                    'kty': 'RSA',
                    'n': key_dict['n']
                }
            }
        })

        return client.ClientV2(
            messages.Directory({
                'newAccount': data['new_account_uri'],
                'newNonce': data['new_nonce_uri'],
                'newOrder': data['new_order_uri'],
                'revokeCert': data['revoke_cert_uri']
            }),
            client.ClientNetwork(key, account=registration)
        ), key

    @private
    def issue_certificate(self, job, progress, data, csr_data):
        verrors = ValidationErrors()

        # TODO: Add ability to complete DNS validation challenge manually

        # Validate domain dns mapping for handling DNS challenges
        # Ensure that there is an authenticator for each domain in the CSR
        domains = self.middleware.call_sync('certificate.get_domain_names', csr_data)
        dns_authenticator_ids = [o['id'] for o in self.middleware.call_sync('dns.authenticator.query')]
        for domain in domains:
            if domain not in data['dns_mapping']:
                verrors.add(
                    'acme_create.dns_mapping',
                    f'Please provide DNS authenticator id for {domain}'
                )
            elif data['dns_mapping'][domain] not in dns_authenticator_ids:
                verrors.add(
                    'acme_create.dns_mapping',
                    f'Provided DNS Authenticator id for {domain} does not exist'
                )
        for domain in data['dns_mapping']:
            if domain not in domains:
                verrors.add(
                    'acme_create.dns_mapping',
                    f'{domain} not specified in the CSR'
                )

        if verrors:
            raise verrors

        acme_client, key = self.get_acme_client_and_key(data['acme_directory_uri'], data['tos'])
        try:
            # perform operations and have a cert issued
            order = acme_client.new_order(csr_data['CSR'])
        except messages.Error as e:
            verrors.add(
                'acme_create.new_order',
                f'Failed to issue a new order for Certificate : {e}'
            )
        else:
            job.set_progress(progress, 'New order for certificate issuance placed')

            self.handle_authorizations(job, progress, order, data['dns_mapping'], acme_client, key)

            try:
                # Polling for a maximum of 10 minutes while trying to finalize order
                # Should we try .poll() instead first ? research please
                return acme_client.poll_and_finalize(order, datetime.datetime.now() + datetime.timedelta(minutes=10))
            except errors.TimeoutError:
                verrors.add(
                    'acme_create.final_order',
                    'Certificate request for final order timed out'
                )
        finally:
            if verrors:
                raise verrors

    @private
    def handle_authorizations(self, job, progress, order, domain_names_dns_mapping, acme_client, key):
        # When this is called, it should be ensured by the function calling this function that for all authorization
        # resource, a domain name dns mapping is available ? Ideal ?
        # For multiple domain providers in domain names, I think we should ask the end user to specify which domain
        # provider is used for which domain so authorizations can be handled gracefully
        # https://serverfault.com/questions/906407/lets-encrypt-dns-challenge-with-multiple-public-dns-providers
        verrors = ValidationErrors()

        max_progress = (progress * 4) - progress - (progress * 4 / 5)  # TODO: Refine this

        for authorization_resource in order.authorizations:
            try:
                status = False
                progress += (max_progress / len(order.authorizations))
                domain = authorization_resource.body.identifier.value  # TODO: handle wildcards
                # BOULDER DOES NOT RETURN WILDCARDS FOR NOW - WILDCARDS SHOULD BE GRACEFULLY HANDLED IN THE DOMAIN DNS
                # MAPPING DICT
                # OTHER IMPLEMENTATIONS RIGHT NOW ASSUME THAT EVERY DOMAIN HAS A WILD CARD IN CASE OF DNS CHALLENGE
                challenge = None
                for chg in authorization_resource.body.challenges:
                    if chg.typ == 'dns-01':
                        challenge = chg

                if not challenge:
                    verrors.add(
                        'acme_authorization.domain',
                        f'DNS Challenge not found for domain {authorization_resource.body.identifier.value}'
                    )
                    continue

                try:
                    status = self.middleware.call_sync(
                        'dns.authenticator.update_txt_record', {
                            'authenticator': domain_names_dns_mapping[domain],
                            'challenge': challenge.json_dumps(),
                            'domain': domain,
                            'key': key.json_dumps()
                        }
                    )
                except ValidationErrors as v:
                    verrors.extend(v)
                else:
                    try:
                        acme_client.answer_challenge(challenge, challenge.response(key))
                    except errors.UnexpectedUpdate as e:
                        verrors.add(
                            'acme_authorization.domain',
                            f'Error answering challenge for {domain} : {e}'
                        )

            finally:
                job.set_progress(
                    progress,
                    f'DNS challenge {"completed" if status else "failed"} for {domain}'
                )

        if verrors:
            raise verrors

    @periodic(86400, run_on_start=True)
    @job(lock='acme_cert_renewal')
    def renew_certs(self, job):
        certs = self.middleware.call_sync(
            'certificate.query',
            [['acme', '!=', None]]
        )
        progress = 0
        for cert in certs:
            progress += (100 / len(certs))

            if (
                    pytz.utc.localize(cert['expire']) - datetime.datetime.now(datetime.timezone.utc)
            ).days < cert['renew_days']:
                # renew cert
                self.logger.debug(f'Renewing certificate {cert["name"]}')
                final_order = self.issue_certificate(
                    job, progress / 4, {
                        'tos': True,
                        'acme_directory_uri': cert['acme']['directory'],
                        'dns_mapping': cert['domains_authenticators']
                    },
                    cert
                )

                self.middleware.call_sync(
                    'datastore.update',
                    self._config.datastore,
                    cert['id'],
                    {
                        'certificate': final_order.fullchain_pem,
                        'acme_uri': final_order.uri,
                        'expire': final_order.body.expires.astimezone(pytz.utc).strftime('%Y-%m-%d'),
                        'chain': True if len(RE_CERTIFICATE.findall(final_order.fullchain_pem)) > 1 else False,
                    },
                    {'prefix': self._config.datastore_prefix}
                )

            job.set_progress(progress)

    # CREATE METHODS FOR CREATING CERTIFICATES
    # "do_create" IS CALLED FIRST AND THEN BASED ON THE TYPE OF THE CERTIFICATE WHICH IS TO BE CREATED THE
    # APPROPRIATE METHOD IS CALLED
    # FOLLOWING TYPES ARE SUPPORTED
    # CREATE_TYPE ( STRING )          - METHOD CALLED
    # CERTIFICATE_CREATE_INTERNAL     - __create_internal
    # CERTIFICATE_CREATE_IMPORTED     - __create_imported_certificate
    # CERTIFICATE_CREATE_IMPORTED_CSR - __create_imported_csr
    # CERTIFICATE_CREATE              - __create_certificate
    # CERTIFICATE_CREATE_CSR          - __create_csr
    # CERTIFICATE_CREATE_ACME         - __create_acme_certificate

    @accepts(
        Dict(
            'certificate_create',
            Bool('tos'),
            Dict('dns_mapping', additional_attrs=True),
            Int('csr_id'),
            Int('signedby'),
            Int('key_length'),
            Int('renew_days'),
            Int('type'),
            Int('lifetime'),
            Int('serial', validators=[Range(min=1)]),
            Str('acme_directory_uri'),
            Str('certificate'),
            Str('city'),
            Str('common'),
            Str('country'),
            Str('CSR'),
            Str('email', validators=[Email()]),
            Str('name', required=True),
            Str('organization'),
            Str('passphrase'),
            Str('privatekey'),
            Str('state'),
            Str('create_type', enum=[
                'CERTIFICATE_CREATE_INTERNAL', 'CERTIFICATE_CREATE_IMPORTED',
                'CERTIFICATE_CREATE', 'CERTIFICATE_CREATE_CSR',
                'CERTIFICATE_CREATE_IMPORTED_CSR', 'CERTIFICATE_CREATE_ACME'], required=True),
            Str('digest_algorithm', enum=['SHA1', 'SHA224', 'SHA256', 'SHA384', 'SHA512']),
            List('san', items=[Str('san')]),
            register=True
        )
    )
    @job(lock='cert_create')
    async def do_create(self, job, data):

        if not data.get('dns_mapping'):
            data.pop('dns_mapping')  # Default dict added

        verrors = await self.validate_common_attributes(data, 'certificate_create')

        await validate_cert_name(
            self.middleware, data['name'], self._config.datastore,
            verrors, 'certificate_create.name'
        )

        if verrors:
            raise verrors

        job.set_progress(10, 'Initial validation complete')

        data = await self.middleware.run_in_io_thread(
            self.map_functions[data.pop('create_type')],
            job, data
        )

        data['san'] = ' '.join(data.pop('san', []) or [])

        # Patch creates another copy of dns_mapping
        data.pop('dns_mapping', None)

        pk = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        await self.middleware.call(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )

        job.set_progress(100, 'Certificate created successfully')

        return await self._get_instance(pk)

    @accepts(
        Dict(
            'acme_create',
            Bool('tos', default=False),
            Int('csr_id', required=True),
            Int('renew_days', default=10),
            Str('acme_directory_uri', required=True),
            Str('name', required=True),
            Dict('dns_mapping', additional_attrs=True, required=True)
        )
    )
    @skip_arg(count=1)
    def __create_acme_certificate(self, job, data):

        csr_data = self.middleware.call_sync(
            'certificate._get_instance', data['csr_id']
        )

        final_order = self.issue_certificate(job, 25, data, csr_data)

        job.set_progress(95, 'Final order received from ACME server')

        cert_dict = {
            'acme': self.middleware.call_sync(
                'acme.registration.query',
                [['directory', '=', data['acme_directory_uri']]]
            )[0]['id'],
            'acme_uri': final_order.uri,
            'certificate': final_order.fullchain_pem,
            'CSR': csr_data['CSR'],
            'privatekey': csr_data['privatekey'],
            'name': data['name'],
            'expire': final_order.body.expires.astimezone(pytz.utc).strftime('%Y-%m-%d'),
            'chain': True if len(RE_CERTIFICATE.findall(final_order.fullchain_pem)) > 1 else False,
            'type': CERT_TYPE_EXISTING,
            'domains_authenticators': data['dns_mapping'],
            'renew_days': data['renew_days']
        }

        cert_dict.update(self.load_certificate(final_order.fullchain_pem))

        return cert_dict

    @accepts(
        Patch(
            'certificate_create_internal', 'certificate_create_csr',
            ('rm', {'name': 'signedby'}),
            ('rm', {'name': 'lifetime'})
        )
    )
    @skip_arg(count=1)
    def __create_csr(self, job, data):
        # no signedby, lifetime attributes required
        cert_info = get_cert_info_from_data(data)
        cert_info.pop('lifetime')

        data['type'] = CERT_TYPE_CSR

        req, key = self.create_certificate_signing_request(cert_info)

        data['CSR'] = crypto.dump_certificate_request(crypto.FILETYPE_PEM, req)
        data['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, key)

        return data

    @accepts(
        Dict(
            'create_imported_csr',
            Str('CSR', required=True),
            Str('name'),
            Str('privatekey', required=True),
            Str('passphrase')
        )
    )
    @skip_arg(count=1)
    def __create_imported_csr(self, job, data):

        data['type'] = CERT_TYPE_CSR

        for k, v in self.load_certificate_request(data['CSR']).items():
            data[k] = v

        if 'passphrase' in data:
            data['privatekey'] = export_private_key(
                data['privatekey'],
                data['passphrase']
            )

        data.pop('passphrase', None)

        return data

    @accepts(
        Patch(
            'certificate_create', 'create_certificate',
            ('edit', _set_required('certificate')),
            ('edit', _set_required('privatekey')),
            ('edit', _set_required('type')),
            ('rm', {'name': 'create_type'})
        )
    )
    @skip_arg(count=1)
    def __create_certificate(self, job, data):

        for k, v in self.load_certificate(data['certificate']).items():
            data[k] = v

        return data

    @accepts(
        Dict(
            'certificate_create_imported',
            Int('csr_id'),
            Str('certificate', required=True),
            Str('name'),
            Str('passphrase'),
            Str('privatekey')
        )
    )
    @skip_arg(count=1)
    def __create_imported_certificate(self, job, data):
        verrors = ValidationErrors()

        csr_id = data.pop('csr_id', None)
        if csr_id:
            csr_obj = self.middleware.call_sync(
                'certificate.query',
                [
                    ['id', '=', csr_id],
                    ['CSR', '!=', None]
                ]
            )
            if not csr_obj:
                verrors.add(
                    'certificate_create.csr_id',
                    f'No CSR exists with id {csr_id}'
                )
            else:
                data['privatekey'] = csr_obj[0]['privatekey']
                data.pop('passphrase', None)
        elif not data.get('privatekey'):
            verrors.add(
                'certificate_create.privatekey',
                'Private key is required when importing a certificate'
            )

        if verrors:
            raise verrors

        data['type'] = CERT_TYPE_EXISTING

        data = self.__create_certificate(job, data)

        data['chain'] = True if len(RE_CERTIFICATE.findall(data['certificate'])) > 1 else False

        if 'passphrase' in data:
            data['privatekey'] = export_private_key(
                data['privatekey'],
                data['passphrase']
            )

        data.pop('passphrase', None)

        return data

    @accepts(
        Patch(
            'certificate_create', 'certificate_create_internal',
            ('edit', _set_required('key_length')),
            ('edit', _set_required('digest_algorithm')),
            ('edit', _set_required('lifetime')),
            ('edit', _set_required('country')),
            ('edit', _set_required('state')),
            ('edit', _set_required('city')),
            ('edit', _set_required('organization')),
            ('edit', _set_required('email')),
            ('edit', _set_required('common')),
            ('edit', _set_required('signedby')),
            ('rm', {'name': 'create_type'}),
            register=True
        )
    )
    @skip_arg(count=1)
    def __create_internal(self, job, data):

        cert_info = get_cert_info_from_data(data)
        data['type'] = CERT_TYPE_INTERNAL

        signing_cert = self.middleware.call_sync(
            'certificateauthority.query',
            [('id', '=', data['signedby'])],
            {'get': True}
        )

        public_key = generate_key(data['key_length'])
        signkey = load_private_key(signing_cert['privatekey'])

        cert = self.middleware.call_sync('certificate.create_certificate', cert_info)
        cert.set_pubkey(public_key)
        cacert = crypto.load_certificate(crypto.FILETYPE_PEM, signing_cert['certificate'])
        cert.set_issuer(cacert.get_subject())
        cert.add_extensions([
            crypto.X509Extension(b"subjectKeyIdentifier", False, b"hash", subject=cert),
        ])

        cert_serial = self.middleware.call_sync(
            'certificateauthority.get_serial_for_certificate',
            data['signedby']
        )

        cert.set_serial_number(cert_serial)
        cert.sign(signkey, data['digest_algorithm'])

        data['certificate'] = crypto.dump_certificate(crypto.FILETYPE_PEM, cert)
        data['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, public_key)
        data['serial'] = cert_serial

        return data

    @accepts(
        Int('id', required=True),
        Dict(
            'certificate_update',
            Str('name')
        )
    )
    @job(lock='cert_update')
    async def do_update(self, job, id, data):
        old = await self._get_instance(id)
        # signedby is changed back to integer from a dict
        old['signedby'] = old['signedby']['id'] if old.get('signedby') else None

        new = old.copy()

        new.update(data)

        if new['name'] != old['name']:

            verrors = ValidationErrors()

            await validate_cert_name(self.middleware, data['name'], self._config.datastore, verrors,
                                     'certificate_update.name')

            if verrors:
                raise verrors

            new['san'] = ' '.join(new.pop('san', []) or [])

            await self.middleware.call(
                'datastore.update',
                self._config.datastore,
                id,
                new,
                {'prefix': self._config.datastore_prefix}
            )

            await self.middleware.call(
                'service.start',
                'ix-ssl',
                {'onetime': False}
            )

        return await self._get_instance(id)

    @accepts(
        Int('id'),
        Bool('force', default=False)
    )
    @job(lock='cert_delete')
    def do_delete(self, job, id, force=False):

        cert = self.middleware.call_sync(
            'certificate.query',
            [
                ['id', '=', id],
                ['acme', '!=', None]
            ]
        )

        if cert:
            cert = cert[0]

            verrors = ValidationErrors()

            client, key = self.get_acme_client_and_key(cert['acme']['directory'], True)

            job.set_progress(60)

            try:
                client.revoke(
                    jose.ComparableX509(crypto.load_certificate(crypto.FILETYPE_PEM, cert['certificate'])),
                    0
                )
            except errors.ClientError as e:
                verrors.add(
                    'acme_revoke.id',
                    f'Unable to revoke certificate :{e}'
                )
            else:
                job.set_progress(80, 'Certificate successfully revoked')

            if verrors and not force:
                raise verrors

        response = self.middleware.call_sync(
            'datastore.delete',
            self._config.datastore,
            id
        )

        self.middleware.call_sync(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )

        job.set_progress(100)
        return response


class CertificateAuthorityService(CRUDService):

    class Config:
        datastore = 'system.certificateauthority'
        datastore_extend = 'certificate.cert_extend'
        datastore_prefix = 'cert_'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.map_create_functions = {
            'CA_CREATE_INTERNAL': self.__create_internal,
            'CA_CREATE_IMPORTED': self.__create_imported_ca,
            'CA_CREATE_INTERMEDIATE': self.__create_intermediate_ca,
        }

    # HELPER METHODS

    @private
    async def validate_common_attributes(self, data, schema_name):
        verrors = ValidationErrors()

        await _validate_common_attributes(self.middleware, data, verrors, schema_name)

        return verrors

    @private
    async def get_serial_for_certificate(self, ca_id):

        ca_data = await self._get_instance(ca_id)

        if ca_data.get('signedby'):
            # Recursively call the same function for it's parent and let the function gather all serials in a chain
            return await self.get_serial_for_certificate(ca_data['signedby']['id'])
        else:

            async def cert_serials(ca_id):
                return [
                    data['serial'] for data in
                    await self.middleware.call(
                        'datastore.query',
                        'system.certificate',
                        [('signedby', '=', ca_id)],
                        {
                            'prefix': self._config.datastore_prefix,
                            'extend': self._config.datastore_extend
                        }
                    )
                ]

            ca_signed_certs = await cert_serials(ca_id)

            async def child_serials(ca_id):
                serials = []
                children = await self.query(
                    [('signedby', '=', ca_id)]
                )

                for child in children:
                    serials.extend((await child_serials(child['id'])))

                serials.extend((await cert_serials(ca_id)))
                serials.append((await self._get_instance(ca_id))['serial'])

                return serials

            ca_signed_certs.extend((await child_serials(ca_id)))

            if not ca_signed_certs:
                return int((await self._get_instance(ca_id))['serial']) + 1
            else:
                return max(ca_signed_certs) + 1

    @private
    @accepts(
        Ref('certificate_cert_info')
    )
    def create_self_signed_CA(self, cert_info):

        key = generate_key(cert_info['key_length'])
        cert = self.middleware.call_sync('certificate.create_certificate', cert_info)
        cert.set_pubkey(key)
        cert.add_extensions([
            crypto.X509Extension(b"basicConstraints", True, b"CA:TRUE"),
            crypto.X509Extension(b"keyUsage", True, b"keyCertSign, cRLSign"),
            crypto.X509Extension(b"subjectKeyIdentifier", False, b"hash", subject=cert),
        ])
        serial = cert_info.get('serial')
        cert.set_serial_number(serial or 0o1)
        cert.sign(key, cert_info['digest_algorithm'])
        return (cert, key)

    def _set_enum(name):
        def set_enum(attr):
            attr.enum = ['CA_CREATE_INTERNAL', 'CA_CREATE_IMPORTED', 'CA_CREATE_INTERMEDIATE']
        return {'name': name, 'method': set_enum}

    # CREATE METHODS FOR CREATING CERTIFICATE AUTHORITIES
    # "do_create" IS CALLED FIRST AND THEN BASED ON THE TYPE OF CA WHICH IS TO BE CREATED, THE
    # APPROPRIATE METHOD IS CALLED
    # FOLLOWING TYPES ARE SUPPORTED
    # CREATE_TYPE ( STRING )      - METHOD CALLED
    # CA_CREATE_INTERNAL          - __create_internal
    # CA_CREATE_IMPORTED          - __create_imported_ca
    # CA_CREATE_INTERMEDIATE      - __create_intermediate_ca

    @accepts(
        Patch(
            'certificate_create', 'ca_create',
            ('edit', _set_enum('create_type')),
            ('rm', {'name': 'dns_mapping'}),
            register=True
        )
    )
    async def do_create(self, data):
        verrors = await self.validate_common_attributes(data, 'certificate_authority_create')

        await validate_cert_name(
            self.middleware, data['name'], self._config.datastore,
            verrors, 'certificate_authority_create.name'
        )

        if verrors:
            raise verrors

        data = await self.middleware.run_in_io_thread(
            self.map_create_functions[data.pop('create_type')],
            data
        )

        data['san'] = ' '.join(data.pop('san', []) or [])

        pk = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        await self.middleware.call(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )

        return await self._get_instance(pk)

    @accepts(
        Dict(
            'ca_sign_csr',
            Int('ca_id', required=True),
            Int('csr_cert_id', required=True),
            Str('name', required=True),
            register=True
        )
    )
    def ca_sign_csr(self, data):
        return self.__ca_sign_csr(data)

    @accepts(
        Ref('ca_sign_csr'),
        Str('schema_name', default='certificate_authority_update')
    )
    def __ca_sign_csr(self, data, schema_name):
        verrors = ValidationErrors()

        ca_data = self.middleware.call_sync(
            'certificateauthority.query',
            ([('id', '=', data['ca_id'])])
        )
        csr_cert_data = self.middleware.call_sync('certificate.query', [('id', '=', data['csr_cert_id'])])

        if not ca_data:
            verrors.add(
                f'{schema_name}.ca_id',
                f'No Certificate Authority found for id {data["ca_id"]}'
            )
        else:
            ca_data = ca_data[0]
            if not ca_data.get('privatekey'):
                verrors.add(
                    f'{schema_name}.ca_id',
                    'Please use a CA which has a private key assigned'
                )

        if not csr_cert_data:
            verrors.add(
                f'{schema_name}.csr_cert_id',
                f'No Certificate found for id {data["csr_cert_id"]}'
            )
        else:
            csr_cert_data = csr_cert_data[0]
            if not csr_cert_data.get('CSR'):
                verrors.add(
                    f'{schema_name}.csr_cert_id',
                    'No CSR has been filed by this certificate'
                )
            else:
                try:
                    csr = crypto.load_certificate_request(crypto.FILETYPE_PEM, csr_cert_data['CSR'])
                except crypto.Error:
                    verrors.add(
                        f'{schema_name}.csr_cert_id',
                        'CSR not valid'
                    )

        if verrors:
            raise verrors

        cert_info = crypto.load_certificate(crypto.FILETYPE_PEM, ca_data['certificate'])
        PKey = load_private_key(ca_data['privatekey'])

        serial = self.middleware.call_sync(
            'certificateauthority.get_serial_for_certificate',
            ca_data['id']
        )

        cert = crypto.X509()
        cert.set_serial_number(serial)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(86400 * 365 * 10)
        cert.set_issuer(cert_info.get_subject())
        cert.set_subject(csr.get_subject())
        cert.set_pubkey(csr.get_pubkey())
        cert.sign(PKey, ca_data['digest_algorithm'])

        new_cert = crypto.dump_certificate(crypto.FILETYPE_PEM, cert).decode()

        new_csr = {
            'type': CERT_TYPE_INTERNAL,
            'name': data['name'],
            'certificate': new_cert,
            'privatekey': csr_cert_data['privatekey'],
            'create_type': 'CERTIFICATE_CREATE',
            'signedby': ca_data['id']
        }

        new_csr_job_id = self.middleware.call_sync(
            'certificate.create',
            new_csr
        )

        return new_csr_job_id

    @accepts(
        Patch(
            'ca_create_interal', 'ca_create_intermediate',
            ('add', {'name': 'signedby', 'type': 'int', 'required': True}),
        ),
    )
    def __create_intermediate_ca(self, data):

        signing_cert = self.middleware.call_sync(
            'certificateauthority._get_instance',
            data['signedby']
        )

        serial = self.middleware.call_sync(
            'certificateauthority.get_serial_for_certificate',
            signing_cert['id']
        )

        data['type'] = CA_TYPE_INTERMEDIATE
        cert_info = get_cert_info_from_data(data)

        publickey = generate_key(data['key_length'])
        signkey = load_private_key(signing_cert['privatekey'])

        cert = self.middleware.call_sync('certificate.create_certificate', cert_info)
        cert.set_pubkey(publickey)
        cacert = crypto.load_certificate(crypto.FILETYPE_PEM, signing_cert['certificate'])
        cert.set_issuer(cacert.get_subject())
        cert.add_extensions([
            crypto.X509Extension(b"basicConstraints", True, b"CA:TRUE, pathlen:0"),
            crypto.X509Extension(b"keyUsage", True, b"keyCertSign, cRLSign"),
            crypto.X509Extension(b"subjectKeyIdentifier", False, b"hash", subject=cert),
        ])

        cert.set_serial_number(serial)
        data['serial'] = serial
        cert.sign(signkey, data['digest_algorithm'])

        data['certificate'] = crypto.dump_certificate(crypto.FILETYPE_PEM, cert)
        data['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, publickey)

        return data

    @accepts(
        Patch(
            'ca_create', 'ca_create_imported',
            ('edit', _set_required('certificate')),
            ('rm', {'name': 'create_type'}),
        )
    )
    def __create_imported_ca(self, data):
        data['type'] = CA_TYPE_EXISTING
        data['chain'] = True if len(RE_CERTIFICATE.findall(data['certificate'])) > 1 else False

        for k, v in self.middleware.call_sync('certificate.load_certificate', data['certificate']).items():
            data[k] = v

        if all(k in data for k in ('passphrase', 'privatekey')):
            data['privatekey'] = export_private_key(
                data['privatekey'],
                data['passphrase']
            )

        data.pop('passphrase', None)

        return data

    @accepts(
        Patch(
            'ca_create', 'ca_create_interal',
            ('edit', _set_required('key_length')),
            ('edit', _set_required('digest_algorithm')),
            ('edit', _set_required('lifetime')),
            ('edit', _set_required('country')),
            ('edit', _set_required('state')),
            ('edit', _set_required('city')),
            ('edit', _set_required('organization')),
            ('edit', _set_required('email')),
            ('edit', _set_required('common')),
            ('rm', {'name': 'create_type'}),
            register=True
        )
    )
    def __create_internal(self, data):
        cert_info = get_cert_info_from_data(data)
        cert_info['serial'] = random.getrandbits(24)
        (cert, key) = self.create_self_signed_CA(cert_info)

        data['type'] = CA_TYPE_INTERNAL
        data['certificate'] = crypto.dump_certificate(crypto.FILETYPE_PEM, cert)
        data['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, key)
        data['serial'] = cert_info['serial']

        return data

    @accepts(
        Int('id', required=True),
        Dict(
            'ca_update',
            Int('ca_id'),
            Int('csr_cert_id'),
            Str('create_type', enum=['CA_SIGN_CSR']),
            Str('name'),
        )
    )
    async def do_update(self, id, data):

        if data.pop('create_type', '') == 'CA_SIGN_CSR':
            data['ca_id'] = id
            return await self.middleware.run_in_io_thread(
                self.__ca_sign_csr,
                data,
                'certificate_authority_update'
            )

        old = await self._get_instance(id)
        # signedby is changed back to integer from a dict
        old['signedby'] = old['signedby']['id'] if old.get('signedby') else None

        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()

        if new['name'] != old['name']:
            await validate_cert_name(self.middleware, data['name'], self._config.datastore, verrors,
                                     'certificate_authority_update.name')

            if verrors:
                raise verrors

            new['san'] = ' '.join(new.pop('san', []) or [])

            await self.middleware.call(
                'datastore.update',
                self._config.datastore,
                id,
                new,
                {'prefix': self._config.datastore_prefix}
            )

            await self.middleware.call(
                'service.start',
                'ix-ssl',
                {'onetime': False}
            )

        return await self._get_instance(id)

    @accepts(
        Int('id')
    )
    async def do_delete(self, id):
        response = await self.middleware.call(
            'datastore.delete',
            self._config.datastore,
            id
        )

        await self.middleware.call(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )
        return response
