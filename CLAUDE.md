# Working with Nico

This repo's own working rules are in AGENTS.md; read it too.

## Keys
- Never ask Nico for an API key or to edit a cloud environment.
- On his Mac, use `key list`, then `key run NAME -- <command>`.
- In a cloud session, this repo's GitHub Actions secrets are: OPENAI_KEY. Run anything that needs them in a `workflow_dispatch` workflow (add one if it's missing), upload results as an artifact, and never echo a secret.

## Pre-approved by Nico (2026-10-03)
- Push, and merge your own PRs once CI is green.
- Deploy this app once tests pass, with the previous version ready to roll back.
- Paid API test runs, up to about $5 a day.

Still ask first: emails and messages, payments, App Store submissions and releases, anything public, force-pushes.

Make reversible calls yourself and finish the whole job: build, test, check the result, then report the outcome in a few plain lines.
