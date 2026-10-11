# Security

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub's private vulnerability reporting](https://github.com/bradnemetski/fauxcall/security/advisories/new),
not in a public issue. Include what you found, how to reproduce it, and what an attacker could do
with it. You'll get an answer within a week.

## What matters most

FauxCall handles other people's credentials and data, so these are the most important areas:

- **Credentials leaking** into recordings, logs, the call journal, generated files, reports or
  error messages.
- **Personal data** that should have been pseudonymized but wasn't.
- **Mutating calls** reaching a real service without the user's up-front approval.
- **Generated fakes** exposing more than intended. The admin API is meant for local evaluation
  setups; set `admin_token` when a fake is reachable by anything you don't control.

## Supported versions

Security fixes go into the latest release.
