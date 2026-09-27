# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Deprecated

- `pulsar publish --yes`: deprecated alias for `--confirm`. Stays accepted with a warning for one release; will become a usage error in the next release.
- `pulsar auth status --offline`: deprecated no-op flag (`pulsar auth status` is offline by default). Stays accepted with a warning for one release; will become a usage error in the next release.
