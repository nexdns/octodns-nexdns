# octodns-nexdns

[![PyPI](https://img.shields.io/pypi/v/octodns-nexdns)](https://pypi.org/project/octodns-nexdns/)
[![Python versions](https://img.shields.io/pypi/pyversions/octodns-nexdns)](https://pypi.org/project/octodns-nexdns/)
[![License](https://img.shields.io/pypi/l/octodns-nexdns)](LICENSE)

Official [octoDNS](https://github.com/octodns/octodns) provider for [NexDNS](https://nexdns.tech) – managed authoritative DNS with a REST API, DNSSEC and a control panel.

It works as a **target**, so octoDNS applies your zone files to NexDNS, and as a **source**, so octoDNS reads what is already there. That covers the three things people come here for: keeping zones in git and applying them from CI, moving zones in from another DNS provider, and exporting what you have back out to YAML.

If you manage only NexDNS and never need a second DNS provider in the picture, the [`nexdns` CLI](https://github.com/nexdns/cli) or the [Terraform provider](https://github.com/nexdns/terraform-provider-nexdns) may suit you better – see [choosing between the tools](#choosing-between-the-tools).

- [Requirements](#requirements)
- [Installation](#installation)
- [API token](#api-token)
- [Configuration](#configuration)
- [Zone files](#zone-files)
- [The sync workflow](#the-sync-workflow)
- [Record types](#record-types)
- [Migrating a zone in](#migrating-a-zone-in)
- [Exporting zones back out](#exporting-zones-back-out)
- [Provider options](#provider-options)
- [Continuous integration](#continuous-integration)
- [Choosing between the tools](#choosing-between-the-tools)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Requirements

- Python 3.9 or newer. The test matrix covers 3.9 through 3.13
- octoDNS 1.5 or newer and `requests` 2.25 or newer, both installed with this package
- A NexDNS account on a plan that includes API access, and an API token. See [pricing](https://nexdns.tech/pricing)

## Installation

```bash
pip install octodns-nexdns
```

octoDNS is a dependency of this package, so that one command is enough. Providers are named by class path in your config, which means a broken install shows up only when `octodns-sync` starts – check it up front:

```bash
python -c "from octodns_nexdns import NexdnsProvider; print(NexdnsProvider)"
```

## API token

Create a token under [Settings → API keys](https://nexdns.tech/settings/api-keys) and give it the scopes this provider uses:

| Scope | Used for |
| --- | --- |
| `zones.read` | finding the zone, and listing your zones when the config discovers them dynamically |
| `records.read` | reading the current records of a zone |
| `records.write` | creating and deleting records when a plan is applied |
| `zones.write` | only if you want the provider to create a zone that is not in the account yet |

Then put it in the environment:

```bash
export NEXDNS_API_TOKEN=nxd_xxxxxxxxxxxxxxxxxxxx
```

`env/NEXDNS_API_TOKEN` in the configuration below is octoDNS's syntax for reading a value from the environment, so the token stays out of the config file and out of git.

## Configuration

The minimum useful setup is two providers: a YAML source that holds your zone files, and this provider as the target.

```yaml
# config.yaml
providers:
  config:
    class: octodns.provider.yaml.YamlProvider
    directory: ./zones
  nexdns:
    class: octodns_nexdns.NexdnsProvider
    token: env/NEXDNS_API_TOKEN
    # api_url: https://api.nexdns.tech/v1   # this is the default

zones:
  example.com.:
    sources:
      - config
    targets:
      - nexdns
```

Every name under `sources:` and `targets:` has to be a key under `providers:` – that is the entire wiring, and getting it wrong is the most common first error. Zone names carry a trailing dot.

## Zone files

The YAML provider reads one file per zone from its `directory`, named after the zone: `./zones/example.com.yaml` for the configuration above. Keys are enforced in sorted order at every level, including inside a record.

```yaml
# zones/example.com.yaml
'':
  - ttl: 300
    type: A
    values:
      - 203.0.113.10
      - 203.0.113.11
  - ttl: 3600
    type: MX
    values:
      - exchange: mail.example.com.
        preference: 10
  - ttl: 3600
    type: TXT
    values:
      - v=spf1 include:_spf.example.net -all
_dmarc:
  - ttl: 3600
    type: TXT
    value: v=DMARC1\; p=none\; rua=mailto:dmarc@example.com
api:
  - ttl: 300
    type: A
    value: 203.0.113.20
www:
  - ttl: 300
    type: CNAME
    value: example.com.
```

Two things worth knowing about that file:

- **A TTL belongs to the whole record set.** Every value under one name and type shares it. Reading a zone, the provider takes the TTL of the first record in the set, so values that were given different TTLs outside octoDNS show up as a change that normalises them.
- **Semicolons in TXT values are escaped as `\;`.** That is octoDNS's requirement, not the API's: the provider unescapes them on the way out and escapes them again on the way back, so a DMARC or SPF value round-trips unchanged.

The `''` key is the zone apex. Sorted order puts it first, then `_dmarc`, `api`, `www`.

## The sync workflow

```bash
# 1. plan – prints what would change and touches nothing
octodns-sync --config-file config.yaml

# 2. apply
octodns-sync --config-file config.yaml --doit
```

Read the plan before you pass `--doit`; that separation is the point of octoDNS. Three things worth knowing about the run:

- In a zone that already holds ten records or more, a plan that updates or deletes more than 30% of them is refused with `force required`. Re-run it with `--force` once you have read what it wants to do.
- A zone name as a positional argument limits the run to that zone: `octodns-sync --config-file config.yaml --doit example.com.`
- A plan line that reads as one update is carried out as a delete of the record set's current records followed by a create of the new ones.

## Record types

A, AAAA, ALIAS, CAA, CNAME, DNAME, DS, MX, NS, PTR, SRV, TLSA, TXT.

SOA is deliberately absent: octoDNS models the zone's SOA itself, and no provider writes it.

DNSSEC signing is not part of an octoDNS plan and this provider does not manage it. `DS` here is the ordinary record type, used to secure a delegation to a child zone. Signing your own zone is a zone-level action in the panel or with the CLI (`nexdns dnssec enable example.com`).

### Apex NS records

The NS record set at the apex of a zone is the delegation to the NexDNS nameservers, and the platform manages it. The provider declares `SUPPORTS_ROOT_NS = False`, filters the apex NS set out when it reads a zone, and never writes one. NS records *below* the apex – delegating a subdomain elsewhere – are ordinary records and are supported.

Two consequences:

- Do not put an apex NS record in your zone file. octoDNS's `strict_supports` is on by default, so a desired zone containing one stops the run with `nexdns: root NS record not supported for example.com.`
- When the source is another DNS provider, its apex NS records arrive with everything else. Set `strict_supports: false` on this provider for that run, and octoDNS logs the record and drops it instead of failing. That is why the migration example below sets it.

## Migrating a zone in

Any octoDNS provider can be the source. Cloudflare, as an example:

```yaml
# migrate.yaml
providers:
  cloudflare:
    class: octodns_cloudflare.CloudflareProvider
    token: env/CLOUDFLARE_API_TOKEN
  nexdns:
    class: octodns_nexdns.NexdnsProvider
    strict_supports: false
    token: env/NEXDNS_API_TOKEN

zones:
  example.com.:
    sources:
      - cloudflare
    targets:
      - nexdns
```

```bash
pip install octodns-cloudflare
octodns-sync --config-file migrate.yaml           # what would be created
octodns-sync --config-file migrate.yaml --doit    # create it
```

Add a block under `zones:` per domain, and the same run moves all of them.

If the zone is not in your NexDNS account yet, the provider creates it during apply, which needs a token with `zones.write`; [`ns_group`](#provider-options) decides which nameserver group it gets. Records are then live on the NexDNS nameservers, but resolvers keep answering from the old provider until you change the nameservers at your registrar – so apply first, compare the two, delegate, and leave the old zone in place until the change has propagated.

## Exporting zones back out

To write one zone from NexDNS into YAML files:

```bash
octodns-dump --config-file config.yaml --output-dir ./zones example.com. nexdns
```

The zone has to be one of the `zones:` in the config, `nexdns` is the source to read it from, and files already in `--output-dir` are overwritten.

To back up every zone in the account, let octoDNS discover them:

```yaml
# backup.yaml
providers:
  backup:
    class: octodns.provider.yaml.YamlProvider
    directory: ./backup
  nexdns:
    class: octodns_nexdns.NexdnsProvider
    token: env/NEXDNS_API_TOKEN

zones:
  '*':
    sources:
      - nexdns
    targets:
      - backup
```

```bash
mkdir -p backup
octodns-sync --config-file backup.yaml --doit
```

`'*'` tells octoDNS to ask the source which zones exist; the provider pages through the account's zone list, so a zone added in the panel is picked up on the next run. The dumped files carry no apex NS record, because the provider filters it – see [apex NS records](#apex-ns-records).

## Provider options

| Option | Required | Default | Description |
| --- | --- | --- | --- |
| `class` | yes | – | `octodns_nexdns.NexdnsProvider` |
| `token` | yes | – | API token. `env/NEXDNS_API_TOKEN` reads it from the environment |
| `api_url` | no | `https://api.nexdns.tech/v1` | API base URL. A trailing slash is trimmed |
| `timeout` | no | `30` | Seconds one API request may take before it is abandoned. Without a limit a half-open connection would hang the whole run |
| `ns_group` | no | account default | Slug of the nameserver group for zones the provider creates. `GET /v1/ns-groups` lists the slugs your account may use; left out, the account's first available group is used |

The key you give the provider under `providers:` is its id – the name you then use in `sources:` and `targets:`, and the name it logs under.

Anything octoDNS's own `BaseProvider` accepts is passed through, including `strict_supports`, `apply_disabled`, `update_pcent_threshold` and `delete_pcent_threshold`.

## Continuous integration

The plan/apply split maps onto pull request and merge. Plan on every push and pull request, apply only from `main`:

```yaml
# .github/workflows/dns.yml
name: DNS
on: [push, pull_request]
jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install octodns-nexdns
      - run: octodns-sync --config-file dns/config.yaml
        env:
          NEXDNS_API_TOKEN: ${{ secrets.NEXDNS_API_TOKEN }}
  apply:
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    needs: plan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install octodns-nexdns
      - run: octodns-sync --config-file dns/config.yaml --doit
        env:
          NEXDNS_API_TOKEN: ${{ secrets.NEXDNS_API_TOKEN }}
```

The plan job needs a token too, because planning reads the current state from the API. A read-only token with `zones.read` and `records.read` is enough for it.

## Choosing between the tools

| | Config format | More than one DNS provider | Suits |
| --- | --- | --- | --- |
| **octoDNS + `octodns-nexdns`** | YAML | yes | many zones under one workflow, migrations between providers |
| [`nexdns` CLI](https://github.com/nexdns/cli) | YAML, flags | no | one account, one-off changes, shell and CI scripting |
| [Terraform provider](https://github.com/nexdns/terraform-provider-nexdns) | HCL | yes | DNS as part of a larger infrastructure state |
| [REST API](https://nexdns.tech/docs/api) | JSON | no | your own tooling |

They are not exclusive – a zone applied by octoDNS is an ordinary zone, visible in the panel and reachable by the CLI and the API.

## Troubleshooting

**`Zone example.com., unknown source: config`**
The name in `sources:` (or `targets:`) does not match any key under `providers:`. Both examples use `config` for the YAML provider and `nexdns` for this one; if you renamed either, rename it in both places.

**`keys out of order: expected api got www`**
A zone file breaks the sorted-key rule. The order applies at every level, so it covers the record names in the file and the keys inside a record (`ttl`, `type`, `value`/`values`) alike.

**`nexdns: root NS record not supported for example.com.`**
The desired zone contains an apex NS record. Remove it from the zone file, or, when the records come from another provider, set `strict_supports: false` on this provider – see [apex NS records](#apex-ns-records).

**`NexDNS API error: API access is not included in your plan.`**
The token is valid, but the REST API is not part of the current plan. See [pricing](https://nexdns.tech/pricing).

**`NexDNS API error: API key lacks the "records.write" permission.`**
The token exists but was created without that scope. Scopes are fixed when the key is created, so create a new key with the [scopes above](#api-token) and revoke the old one.

**`NexDNS API error: Authentication is required...`**
The token is wrong, revoked, or not reaching the process. `env/NEXDNS_API_TOKEN` reads the variable of that name at startup; in CI, check that the secret is exposed to the step that runs `octodns-sync`.

**`[example.com.] Too many deletes, 50.00% is over 30.00% (6/12), force required`**
octoDNS's safety threshold, not an error from this provider. Read the plan; if it is what you meant, re-run with `--force`.

**Every record shows as a create, on a zone that already exists**
The provider found no zone of that name in the account, so it treated the zone as empty. Check that the name matches the zone in your account and that the token belongs to the account that holds it.

**Errors mention the request that failed**
API errors carry the field-level detail and the request that produced them, e.g. `NexDNS API error: Validation failed. (content: ...) [POST /zones/.../records {...}]`. The payload in brackets is the record octoDNS was writing when it stopped.

## Development

```bash
git clone https://github.com/nexdns/octodns-nexdns
cd octodns-nexdns
pip install -e '.[dev]'
pytest
```

The test suite runs offline: it covers construction and the record conversion helpers and never calls the API. CI runs it on every Python version the package advertises, and additionally imports the provider class, since a config only names it by path.

## Links

- **PyPI**: [pypi.org/project/octodns-nexdns](https://pypi.org/project/octodns-nexdns/)
- **octoDNS**: [github.com/octodns/octodns](https://github.com/octodns/octodns)
- **NexDNS**: [nexdns.tech](https://nexdns.tech)
- **Integration docs**: [nexdns.tech/docs/integrations](https://nexdns.tech/docs/integrations)
- **API reference**: [nexdns.tech/docs/api](https://nexdns.tech/docs/api)
- **CLI**: [github.com/nexdns/cli](https://github.com/nexdns/cli)
- **Terraform provider**: [github.com/nexdns/terraform-provider-nexdns](https://github.com/nexdns/terraform-provider-nexdns)
- **Certbot plugin**: [github.com/nexdns/certbot-dns-nexdns](https://github.com/nexdns/certbot-dns-nexdns)
- **Issues**: [github.com/nexdns/octodns-nexdns/issues](https://github.com/nexdns/octodns-nexdns/issues)
- **Changes**: [CHANGELOG.md](CHANGELOG.md)

## License

[Apache-2.0](LICENSE)
