# Security policy

## Credential boundary

This repository must never contain production `.env` files, API keys, access tokens,
cookies, SSH/private keys, database dumps, uploaded reports, OCR output, or backups.
Use `.env.example` only as a variable-name template and inject real credentials through
the deployment environment or the application's encrypted settings store.

Before publishing a copy of this project, inspect the complete Git history as
well as the current files for secrets and organization-only information. A
clean working tree alone does not prove that earlier commits are safe.

If a real credential is committed, deleting the line in a later commit is not enough.
Revoke or rotate the credential first, remove it from Git history, and only then push.

## Reporting a vulnerability

Do not open a public issue containing credentials, report files, company data, internal
network details, or reproducible deployment access information. Use the repository
owner's private security contact instead.
