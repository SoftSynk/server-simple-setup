# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository. Include the affected file or command, the impact, and steps to reproduce.

You should get a first reply within 7 days. Fixes are released as a new version, with credit to the reporter unless they prefer otherwise.

## Supported versions

Only the latest release gets security fixes.

## Security model in brief

- All commands run as root on the server they manage. Anyone with root on that server can read the backup password and S3 keys under `/opt/softsynk-server-management/secrets` (mode 0700).
- **Protect your bucket:** turn on versioning or Object Lock at your storage provider, and use S3 keys that cannot change bucket settings. A server compromise must not be able to erase your off-site backups.
- **Keep the restic repository password somewhere else** (a password manager). Without it, backups cannot be decrypted.
- Install and update only from a tarball whose sha256 you verified. See [docs/architecture.md](docs/architecture.md#trust-model-for-distribution).
