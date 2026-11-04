# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-04

First public release.

### Added

- `softsynk-server` engine: manifest-driven install, configure, status, update and rollback of modules, with a gum terminal UI and a non-interactive mode.
- `backup` module (`synk-backup`): PostgreSQL dumps into an encrypted restic repository with an S3-compatible off-site copy, tiered retention, integrity checks, restore tests, restores and failure e-mail.
- `redis` module: single or cache-and-queue Redis setup and tuning.
- `auto-updates` module: unattended-upgrades setup with third-party repos, scheduled reboots and verification.
