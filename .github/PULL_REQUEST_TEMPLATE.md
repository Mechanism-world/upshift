<!--
Thanks for the PR. CONTRIBUTING.md has the full expectations; the short version is below.
-->

## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem, or a link to the issue it closes. -->

## How it was verified

<!--
Say what you actually ran, and be honest about what you could not.
"I could not test this against a real model" is a fine answer and is more useful than a guess.
-->

- [ ] `uv run pytest -q` passes
- [ ] `uv run ruff check src tests` passes
- [ ] New behaviour has a test / the bug fix has a test that failed before it

Anything else you ran (a sim run, a real-API run, a manual check):

## Checklist

- [ ] Docs updated in this PR if this changed something ADAPTER.md, DESIGN.md or README.md
      asserts
- [ ] No API keys, `.env` contents, or other credentials in the diff
- [ ] If this adds third-party material: `ATTRIBUTION.md` written and
      `THIRD_PARTY_NOTICES.md` updated with the license text copied from the upstream
      project's own `LICENSE` file
- [ ] Any committed run records come from **real API runs**, not `--provider sim`, and I have
      reviewed them for anything I would not want published
- [ ] N and pass thresholds were not lowered to make anything pass

By submitting this PR I agree that my contribution is licensed under the project's MIT
License (inbound = outbound).
