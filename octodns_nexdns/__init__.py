"""OctoDNS provider for NexDNS."""

from logging import getLogger
from time import sleep

from requests import Session

from octodns.provider.base import BaseProvider
from octodns.record import Record


# requests applies no timeout of its own, so a half-open connection would hang
# octodns-sync forever - in CI, a stuck job holding a half-finished apply.
DEFAULT_TIMEOUT = 30

# An apply sends one request per record, so a zone of any size outruns the
# account's per-minute budget and starts meeting 429s. Failing there is the
# worst outcome available: an apply is not a transaction, so the zone is left
# holding some of the intended changes and none of the rest, and the operator
# has to work out which.
#
# Retry-After is honoured as a lower bound rather than as the answer. It is the
# right number from a current deployment, but the value used to understate the
# wait, and a plugin released today will be talking to installations that have
# not been updated - a client that cannot make progress against them is the
# same broken apply.
INITIAL_RATE_LIMIT_BACKOFF = 2
MAX_RATE_LIMIT_BACKOFF = 60
MAX_RATE_LIMIT_WAIT = 180

class NexdnsProvider(BaseProvider):
    """NexDNS DNS provider for OctoDNS.

    Supports both source (reading records) and target (writing records).

    Config example (config.yaml):
        providers:
          nexdns:
            class: octodns_nexdns.NexdnsProvider
            token: env/NEXDNS_API_TOKEN
            api_url: https://api.nexdns.tech/v1  # optional
    """

    SUPPORTS_GEO = False
    # The apex NS set is the delegation to the platform's nameservers and is
    # managed there, so this provider neither reads nor writes it. False is the
    # inherited default; it is stated here because the README documents it as
    # part of the provider's contract and octodns-sync fails loudly on a desired
    # zone that carries an apex NS record.
    SUPPORTS_ROOT_NS = False
    SUPPORTS = {
        'A', 'AAAA', 'ALIAS', 'CAA', 'CNAME', 'DNAME', 'DS',
        'MX', 'NS', 'PTR', 'SRV', 'TLSA', 'TXT',
    }

    def __init__(self, id, token, api_url='https://api.nexdns.tech/v1',
                 timeout=DEFAULT_TIMEOUT, ns_group=None, *args, **kwargs):
        self.log = getLogger(f'NexdnsProvider[{id}]')
        self.log.debug('__init__: id=%s', id)
        super().__init__(id, *args, **kwargs)
        self._token = token
        self._api_url = api_url.rstrip('/')
        self._timeout = timeout
        # Documented as a provider option. Without this it fell through
        # **kwargs into BaseProvider.__init__ and raised TypeError, so
        # setting the documented option stopped octodns-sync at startup.
        self._ns_group = ns_group
        self._session = Session()
        self._session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'octodns-nexdns',
        })
        self._zone_cache = {}

    def list_zones(self):
        """Return sorted list of zone names (with trailing dots)."""
        all_zones = []
        page = 1
        while True:
            resp = self._request('/zones', params={'per_page': 100, 'page': page})
            data = resp.get('data', [])
            all_zones.extend(data)
            meta = resp.get('meta', {})
            if page >= meta.get('last_page', 1):
                break
            page += 1
        return sorted(f"{z['name']}." for z in all_zones)

    def populate(self, zone, target=False, lenient=False):
        """Load records from NexDNS into the zone object."""
        zone_name = zone.name[:-1]  # strip trailing dot
        zone_data = self._find_zone(zone_name)
        if not zone_data:
            self.log.debug('populate: zone %s not found', zone_name)
            return False

        zone_id = zone_data['id']
        resp = self._request(f'/zones/{zone_id}/records')
        api_records = resp.get('data', [])

        # Group records by (name, type)
        grouped = {}
        for r in api_records:
            name = self._octodns_name(r['name'])
            # SOA is not an OctoDNS concern, and the apex NS set belongs to the
            # platform: surfacing it made every plan propose deleting the zone's
            # own nameservers.
            if r['type'] == 'SOA':
                continue
            if r['type'] == 'NS' and name == '':
                continue
            key = (name, r['type'])
            grouped.setdefault(key, []).append(r)

        for (name, rtype), records in grouped.items():
            if rtype not in self.SUPPORTS:
                continue

            data = self._data_for_records(rtype, records)
            if data is None:
                continue

            record = Record.new(zone, name, data, source=self, lenient=lenient)
            zone.add_record(record, lenient=lenient)

        self.log.info('populate: found %d records for %s', len(grouped), zone_name)
        return True

    def _apply(self, plan):
        """Apply changes to NexDNS."""
        zone_name = plan.desired.name[:-1]
        zone_data = self._find_zone(zone_name)
        if not zone_data:
            # Create zone
            payload = {'name': zone_name}
            if self._ns_group:
                payload['ns_group'] = self._ns_group
            self._request('/zones', method='POST', json=payload)
            zone_data = self._find_zone(zone_name)
            if not zone_data:
                raise Exception(f'Failed to create zone {zone_name}')

        zone_id = zone_data['id']

        for change in plan.changes:
            class_name = change.__class__.__name__

            if class_name == 'Create':
                self._apply_create(zone_id, change.new)
            elif class_name == 'Update':
                self._apply_update(zone_id, change.existing, change.new)
            elif class_name == 'Delete':
                self._apply_delete(zone_id, change.existing)

    def _apply_create(self, zone_id, record):
        """Create records for a new OctoDNS record."""
        for params in self._params_for_record(record):
            self._request(f'/zones/{zone_id}/records', method='POST', json=params)

    def _apply_update(self, zone_id, existing, new):
        """Update records: delete old, create new."""
        self._apply_delete(zone_id, existing)
        self._apply_create(zone_id, new)

    def _apply_delete(self, zone_id, record):
        """Delete all API records matching this OctoDNS record."""
        name = self._api_name(record.name)
        resp = self._request(
            f'/zones/{zone_id}/records', params={'type': record._type, 'name': name}
        )
        for r in resp.get('data', []):
            # The name filter matches substrings, so verify before deleting.
            if r['name'] != name or r['type'] != record._type:
                continue
            resp = self._session.delete(
                f'{self._api_url}/zones/{zone_id}/records/{r["id"]}',
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                raise Exception(
                    f'NexDNS API error deleting {record._type} {name}: '
                    f'{resp.status_code} {resp.text}'
                )

    # --- Data conversion ---

    def _data_for_records(self, rtype, records):
        """Convert API records to OctoDNS data dict."""
        ttl = records[0]['ttl']

        if rtype in ('A', 'AAAA'):
            values = [self._clean_content(r['content']) for r in records]
            return {'type': rtype, 'ttl': ttl, 'values': values}

        if rtype == 'NS':
            # A delegation target is a hostname, so octoDNS expects it absolute.
            # Stripping the dot made the read value never equal the configured
            # one, so every sync re-planned the delegation and _apply_update
            # tore it down and rebuilt it.
            values = [
                self._ensure_trailing_dot(self._clean_content(r['content']))
                for r in records
            ]
            return {'type': 'NS', 'ttl': ttl, 'values': values}

        if rtype in ('CNAME', 'PTR', 'ALIAS', 'DNAME'):
            content = self._ensure_trailing_dot(self._clean_content(records[0]['content']))
            return {'type': rtype, 'ttl': ttl, 'value': content}

        if rtype == 'MX':
            values = []
            for r in records:
                fields = r.get('fields', {})
                priority = fields.get('priority', r.get('priority', 10))
                host = fields.get('host', r['content'])
                values.append({
                    'preference': int(priority),
                    'exchange': self._ensure_trailing_dot(self._clean_content(host)),
                })
            return {'type': 'MX', 'ttl': ttl, 'values': values}

        if rtype == 'TXT':
            values = []
            for r in records:
                fields = r.get('fields', {})
                value = fields.get('value', r['content'].strip('"'))
                values.append(self._escape_txt(value))
            return {'type': 'TXT', 'ttl': ttl, 'values': values}

        if rtype == 'SRV':
            values = []
            for r in records:
                fields = r.get('fields', {})
                values.append({
                    'priority': int(fields.get('priority', r.get('priority', 0))),
                    'weight': int(fields.get('weight', r.get('weight', 0))),
                    'port': int(fields.get('port', r.get('port', 0))),
                    'target': self._ensure_trailing_dot(fields.get('target', r['content'])),
                })
            return {'type': 'SRV', 'ttl': ttl, 'values': values}

        if rtype == 'CAA':
            values = []
            for r in records:
                fields = r.get('fields', {})
                values.append({
                    'flags': int(fields.get('flags', r.get('flags', 0))),
                    'tag': fields.get('tag', r.get('tag', 'issue')),
                    'value': fields.get('value', r['content']),
                })
            return {'type': 'CAA', 'ttl': ttl, 'values': values}

        if rtype == 'DS':
            values = []
            for r in records:
                fields = r.get('fields', {})
                values.append({
                    'key_tag': int(fields.get('keytag', 0)),
                    'algorithm': int(fields.get('algorithm', 0)),
                    'digest_type': int(fields.get('digest_type', 0)),
                    'digest': fields.get('digest', r['content']),
                })
            return {'type': 'DS', 'ttl': ttl, 'values': values}

        if rtype == 'TLSA':
            values = []
            for r in records:
                fields = r.get('fields', {})
                values.append({
                    'certificate_usage': int(fields.get('usage', 0)),
                    'selector': int(fields.get('selector', 0)),
                    'matching_type': int(fields.get('matching_type', 0)),
                    'certificate_association_data': fields.get('certificate', r['content']),
                })
            return {'type': 'TLSA', 'ttl': ttl, 'values': values}

        return None

    def _params_for_record(self, record):
        """Convert OctoDNS record to API request params."""
        rtype = record._type
        params_list = []

        if rtype in ('A', 'AAAA'):
            for value in record.values:
                params_list.append({
                    'name': self._api_name(record.name), 'type': rtype,
                    'content': value, 'ttl': record.ttl,
                })

        elif rtype == 'NS':
            # Hostname, so the trailing dot comes off on the way in, as it does
            # for CNAME and MX. The read path puts it back.
            for value in record.values:
                params_list.append({
                    'name': self._api_name(record.name), 'type': 'NS',
                    'content': value.rstrip('.'), 'ttl': record.ttl,
                })

        elif rtype in ('CNAME', 'PTR', 'ALIAS', 'DNAME'):
            params_list.append({
                'name': self._api_name(record.name), 'type': rtype,
                'content': record.value.rstrip('.'), 'ttl': record.ttl,
            })

        elif rtype == 'MX':
            for value in record.values:
                params_list.append({
                    'name': self._api_name(record.name), 'type': 'MX',
                    'content': value.exchange.rstrip('.'), 'ttl': record.ttl,
                    'priority': value.preference,
                })

        elif rtype == 'TXT':
            for value in record.values:
                params_list.append({
                    'name': self._api_name(record.name), 'type': 'TXT',
                    'content': self._unescape_txt(value), 'ttl': record.ttl,
                })

        elif rtype == 'SRV':
            for value in record.values:
                params_list.append({
                    'name': self._api_name(record.name), 'type': 'SRV',
                    'content': value.target.rstrip('.'), 'ttl': record.ttl,
                    'priority': value.priority,
                    'weight': value.weight,
                    'port': value.port,
                })

        elif rtype == 'CAA':
            for value in record.values:
                params_list.append({
                    'name': self._api_name(record.name), 'type': 'CAA',
                    'content': value.value, 'ttl': record.ttl,
                    'flags': value.flags,
                    'tag': value.tag,
                })

        elif rtype == 'DS':
            for value in record.values:
                params_list.append({
                    'name': self._api_name(record.name), 'type': 'DS',
                    'content': value.digest, 'ttl': record.ttl,
                    'keytag': value.key_tag,
                    'algorithm': value.algorithm,
                    'digest_type': value.digest_type,
                })

        elif rtype == 'TLSA':
            for value in record.values:
                params_list.append({
                    'name': self._api_name(record.name), 'type': 'TLSA',
                    'content': value.certificate_association_data, 'ttl': record.ttl,
                    'usage': value.certificate_usage,
                    'selector': value.selector,
                    'matching_type': value.matching_type,
                })

        return params_list

    # --- Helpers ---

    def _find_zone(self, name):
        """Find zone by exact name, with caching."""
        if name in self._zone_cache:
            return self._zone_cache[name]

        resp = self._request(f'/zones?search={name}&per_page=100')
        for z in resp.get('data', []):
            if z['name'] == name:
                self._zone_cache[name] = z
                return z
        return None

    def _request(self, path, method='GET', **kwargs):
        """Make an API request, waiting out the account's rate limit.

        A 429 is retried for every method, not only for reads: the budget is
        checked before the request is executed, so a refused write had no
        effect and replaying it cannot apply anything twice.
        """
        url = f'{self._api_url}{path}'
        kwargs.setdefault('timeout', self._timeout)

        waited = 0
        backoff = INITIAL_RATE_LIMIT_BACKOFF

        while True:
            resp = self._session.request(method, url, **kwargs)

            if resp.status_code != 429:
                break

            wait = max(self._retry_after(resp), backoff)
            if waited + wait > MAX_RATE_LIMIT_WAIT:
                break

            self.log.warning(
                '_request: rate limited, waiting %ds before retrying %s %s',
                wait, method, path,
            )
            sleep(wait)
            waited += wait
            backoff = min(backoff * 2, MAX_RATE_LIMIT_BACKOFF)

        if resp.status_code == 204:
            return {}

        try:
            data = resp.json()
        except ValueError:
            data = {}

        if resp.status_code >= 400:
            error = data.get('error', {})
            message = error.get('message', f'HTTP {resp.status_code}')
            # Include the per-field detail and the request that failed: a bare
            # "Validation failed." gave no clue which record octodns-sync died on.
            details = error.get('details') or {}
            if details:
                rendered = '; '.join(
                    f'{field}: {"; ".join(msgs) if isinstance(msgs, list) else msgs}'
                    for field, msgs in details.items()
                )
                message = f'{message} ({rendered})'
            payload = kwargs.get('json')
            if payload:
                message = f'{message} [{method} {path} {payload}]'
            raise Exception(f'NexDNS API error: {message}')

        return data

    @staticmethod
    def _retry_after(resp):
        """Seconds the response asks the caller to wait, 0 when it does not say.

        Retry-After may also be an HTTP date by RFC 9110. This API sends
        seconds, and a date form is left to the backoff rather than parsed,
        because guessing wrong about a date is worse than waiting a known
        interval.
        """
        try:
            return max(0, int(resp.headers.get('Retry-After', '')))
        except ValueError:
            return 0

    @staticmethod
    def _api_name(name):
        """Translate an OctoDNS record name to the API's.

        OctoDNS addresses the zone apex as an empty string; the API names it
        '@'. Passing the empty string through made every apex record fail
        validation, which killed octodns-sync on the first change of any plan.
        """
        return name if name else '@'

    @staticmethod
    def _octodns_name(name):
        """Translate an API record name to OctoDNS's (the inverse of _api_name)."""
        return '' if name == '@' else name

    @staticmethod
    def _escape_txt(value):
        """Escape semicolons for octoDNS, which requires them as `\\;`.

        The API stores and returns TXT values verbatim, so a DMARC or SPF record
        arrives with bare semicolons and octoDNS rejects it as an unescaped `;`.
        Any already-escaped semicolon is normalised first so the value cannot
        pick up a second backslash on each pass.
        """
        return str(value).replace('\\;', ';').replace(';', '\\;')

    @staticmethod
    def _unescape_txt(value):
        """Undo `_escape_txt` on the way back to the API.

        The API escapes what it stores, so sending octoDNS's `\\;` had the
        backslash escaped again and a literal backslash was served on the wire -
        an invalid DMARC or SPF value. It round-tripped self-consistently, so no
        plan ever showed the drift.
        """
        return str(value).replace('\\;', ';')

    @staticmethod
    def _clean_content(content):
        """Strip trailing dot and surrounding quotes."""
        content = content.rstrip('.')
        if content.startswith('"') and content.endswith('"'):
            content = content[1:-1]
        return content

    @staticmethod
    def _ensure_trailing_dot(value):
        """Ensure hostname has trailing dot (OctoDNS convention)."""
        if value and not value.endswith('.'):
            return value + '.'
        return value
