# v1 scope

Does: run agent suite on old + new model, diff behavior per case, attempt repairs within allowed repair types, output patch + verdict, readable terminal report.

Does NOT: dashboard, accounts, hosting, other providers, framework integrations, auto-detecting releases, more than one victim agent.

Adding anything requires evidence from a real user, written here first.

## v0.2 addition (2026-08-31)

`upshift adapt <repo>` — generate the five-file adapter directory from an agent codebase
with cited provenance, so onboarding a real agent takes minutes of review instead of hours
of writing. Rationale: adapting shell_gpt by hand cost 12-15 hours; no stranger pays that
to try a tool. Everything else in this file is unchanged; adapt adds no new repair types,
providers, or UI.
