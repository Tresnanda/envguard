# Security Policy

## Supported Versions

Security fixes target the latest released version of envguard.

## Reporting a Vulnerability

Please do not open a public issue for vulnerabilities. Email the maintainer or use the repository's private security advisory flow when available.

Include:

- A description of the issue and impact.
- Steps to reproduce with a minimal example.
- Whether secret values, files, or external services are exposed.

## Security Expectations

envguard scans source files and configuration names. It must not print secret values, send project data to third-party services, or mutate files unless `--fix` is explicitly used.
