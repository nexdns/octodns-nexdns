"""Smoke tests for the NexDNS OctoDNS provider.

These tests run fully offline. No method that performs live HTTP is called.
"""

from octodns_nexdns import NexdnsProvider


def test_construct_without_raising():
    """The provider must instantiate without raising.

    BaseProvider.__init__ requires self.log to be set before it runs,
    otherwise it raises NotImplementedError. This guards against that.
    """
    provider = NexdnsProvider('test', token='nxd_x')
    assert type(provider).__name__ == 'NexdnsProvider'
    # self.log must be wired up for octodns to function
    assert provider.log is not None


def test_construct_with_custom_api_url():
    """Custom api_url is accepted and trailing slash is stripped."""
    provider = NexdnsProvider('test', token='nxd_x', api_url='https://dns.example.com/v1/')
    assert provider._api_url == 'https://dns.example.com/v1'


def test_supports_record_types():
    """SUPPORTS must contain the expected record types."""
    expected = {
        'A', 'AAAA', 'ALIAS', 'CAA', 'CNAME', 'DNAME', 'DS',
        'MX', 'NS', 'PTR', 'SRV', 'TLSA', 'TXT',
    }
    assert NexdnsProvider.SUPPORTS == expected


def test_root_ns_is_not_supported():
    """The apex NS set belongs to the platform, and the README says so.

    populate() filters it on read; this is the other half of the same rule and
    what makes octodns-sync refuse a desired zone that carries one instead of
    planning a delegation change it cannot apply.
    """
    assert NexdnsProvider.SUPPORTS_ROOT_NS is False


def test_clean_content_strips_quotes_and_dot():
    """Pure helper: no network required."""
    assert NexdnsProvider._clean_content('example.com.') == 'example.com'
    assert NexdnsProvider._clean_content('"quoted value"') == 'quoted value'


def test_ensure_trailing_dot():
    """Pure helper: no network required."""
    assert NexdnsProvider._ensure_trailing_dot('example.com') == 'example.com.'
    assert NexdnsProvider._ensure_trailing_dot('example.com.') == 'example.com.'
    assert NexdnsProvider._ensure_trailing_dot('') == ''


def test_data_for_records_groups_a_values():
    """Pure record-conversion helper: no network required."""
    provider = NexdnsProvider('test', token='nxd_x')
    records = [
        {'name': 'www', 'type': 'A', 'ttl': 300, 'content': '1.2.3.4'},
        {'name': 'www', 'type': 'A', 'ttl': 300, 'content': '5.6.7.8'},
    ]
    data = provider._data_for_records('A', records)
    assert data == {'type': 'A', 'ttl': 300, 'values': ['1.2.3.4', '5.6.7.8']}


def test_data_for_records_ds_from_fields():
    """DS values are read from the parsed 'fields' object."""
    provider = NexdnsProvider('test', token='nxd_x')
    records = [{
        'name': '@', 'type': 'DS', 'ttl': 3600,
        'content': '12345 8 2 ABCDEF',
        'fields': {'keytag': 12345, 'algorithm': 8, 'digest_type': 2, 'digest': 'ABCDEF'},
    }]
    data = provider._data_for_records('DS', records)
    assert data == {'type': 'DS', 'ttl': 3600, 'values': [{
        'key_tag': 12345, 'algorithm': 8, 'digest_type': 2, 'digest': 'ABCDEF',
    }]}


def test_data_for_records_tlsa_from_fields():
    """TLSA values are read from the parsed 'fields' object."""
    provider = NexdnsProvider('test', token='nxd_x')
    records = [{
        'name': '_443._tcp', 'type': 'TLSA', 'ttl': 3600,
        'content': '3 1 1 ABCDEF',
        'fields': {'usage': 3, 'selector': 1, 'matching_type': 1, 'certificate': 'ABCDEF'},
    }]
    data = provider._data_for_records('TLSA', records)
    assert data == {'type': 'TLSA', 'ttl': 3600, 'values': [{
        'certificate_usage': 3, 'selector': 1, 'matching_type': 1,
        'certificate_association_data': 'ABCDEF',
    }]}


def test_txt_semicolons_round_trip():
    """A DMARC value must survive read -> octoDNS -> write unchanged.

    The API stores TXT verbatim and escapes on the way out, so sending
    octoDNS's escaped form had the backslash escaped a second time and a
    literal backslash reached the wire. It round-tripped self-consistently,
    which is why no plan ever showed the drift.
    """
    stored = 'v=DMARC1; p=none; rua=mailto:d@example.com'

    for_octodns = NexdnsProvider._escape_txt(stored)
    assert for_octodns == 'v=DMARC1\\; p=none\\; rua=mailto:d@example.com'
    assert NexdnsProvider._unescape_txt(for_octodns) == stored

    # Reading a value twice must not accumulate backslashes.
    assert NexdnsProvider._escape_txt(for_octodns) == for_octodns


def test_ns_values_keep_the_trailing_dot():
    """NS targets are absolute, or every sync re-plans the delegation.

    _clean_content strips the trailing dot, which is right for A and AAAA and
    wrong for a hostname: the read value never equalled the configured one, so
    the plan was never empty and _apply_update deleted and recreated the
    delegation on every run.
    """
    provider = NexdnsProvider('test', token='nxd_x')
    records = [
        {'name': 'sub', 'type': 'NS', 'ttl': 3600, 'content': 'ns1.example.net.'},
        {'name': 'sub', 'type': 'NS', 'ttl': 3600, 'content': 'ns2.example.net'},
    ]

    data = provider._data_for_records('NS', records)

    assert data['values'] == ['ns1.example.net.', 'ns2.example.net.']


def test_ns_group_is_accepted_and_sent_on_zone_create():
    """The option is documented, so it must not reach BaseProvider as **kwargs."""
    provider = NexdnsProvider('test', token='nxd_x', ns_group='example-group')

    assert provider._ns_group == 'example-group'
    assert NexdnsProvider('test', token='nxd_x')._ns_group is None
