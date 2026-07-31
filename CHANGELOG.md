# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A sync of a zone larger than the account's per-minute request budget no longer
  stops partway with the zone half applied. A rate-limited request now waits and
  is retried, for up to three minutes per request, instead of raising.

## [1.0.0] - 2026-07-31

Initial public release.
