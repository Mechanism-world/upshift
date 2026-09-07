# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| The latest release | Yes — fixes land on `main` and ship in the next release |
| Anything older | No |

upshift is pre-1.0. There is no long-term support branch: the fix for a reported issue is
the next release.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use GitHub private vulnerability reporting on the repository —
<https://github.com/Mechanism-world/upshift/security/advisories/new>. It is private
between you and the maintainer until an advisory is published.

Please include what you did, what happened, and what you expected — a reproducer is worth
more than a description. Expect an acknowledgement within a few days; this is a solo-
maintained project, so please allow reasonable time before public disclosure.

## Scope

In scope:

- The `upshift` CLI itself (`upshift upgrade`, `upshift adapt`, `upshift init`, reporting and
  patch generation) — anything in `src/upshift/`.
- `upshift adapt`'s handling of a target repository it is pointed at, including a repository
  cloned from a URL you do not control.
- The sandboxed shell backend in `agents/shell_gpt/backend.py`, which executes
  model-generated shell commands.
- Anything that would cause credentials, or data from outside the repository being analysed,
  to leak into a run record or a generated patch.

Out of scope:

- Vulnerabilities in the upstream agents adapted under `agents/` — report those to their own
  projects (each `agents/*/ATTRIBUTION.md` names the upstream and commit).
- Vulnerabilities in OpenAI's or Anthropic's APIs or SDKs — report those to those vendors.
- The behaviour of a *model* (jailbreaks, harmful output). upshift measures model behaviour;
  it is not a filter for it.
- The content of the committed evidence under `runs/`, which is deliberately public. If you
  believe it contains something it should not, that **is** in scope — see below.

## Threat model

These are properties of this codebase, stated so you can check them rather than trust them.

### What leaves your machine

upshift is a local CLI with no backend of its own. It is **not** true that nothing leaves
your machine — testing a model means sending it your agent — so here is the actual data
flow, in full.

**Data that leaves your machine, every run, by design:**

- **To the model provider you selected.** Every API call carries your system prompt, your
  tool schemas, every eval-case user message, every tool result, and your **API key** as a
  transport credential. The destination is `api.openai.com` or `api.anthropic.com`, or the
  base URL you set via `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` — including a URL that points
  at your own gateway, or at `upshift capture` on loopback.
- **To the extraction model, when you run `upshift adapt`.** `adapt` ranks the target
  repository's files and puts cited slices of them in the prompt. If you point it at a private
  repository, that repository's source is sent to whichever model you configured, under your
  key. The `ADAPT_REPORT.md` and the run record both show exactly which slices went.
- **To the git remote, when you pass `adapt` a URL.** `git clone --depth 1 -- <url>`. The URL
  must start with `https://`, `git://`, `ssh://` or `git@` (or end in `.git`); a source
  beginning with `-` is refused rather than handed to git, where it would be read as one of
  git's own options.

**Data that does not leave your machine:**

- Nothing goes to Mechanism, or to any service operated by this project. There is no
  telemetry, no analytics, no crash reporting, no license check, no account, and no upload of
  your agent, your prompts, your patches or your results.
- Run records, diffs, verdicts and patches are written under your runs root and stay there
  unless you commit and push them yourself.

**Consequences worth stating plainly:**

- Your **local transcripts can contain sensitive data**. A run record is a full transcript by
  design (see below); an `adapt` record additionally quotes source it read. Both live in
  plaintext JSON in your working tree.
- Your **provider sees everything the run sends**, under their retention and training policy,
  not ours. If your prompt embeds customer data, the provider receives it.
- `upshift capture` forwards **the caller's own credentials** to the `--upstream` you name.
  That upstream is not restricted to a provider allowlist — it defaults to
  `https://api.anthropic.com`, accepts any `http(s)` URL you type, and refuses anything else
  (a non-http scheme, or a URL with no host). The destination is never inferred from a
  recorded request's headers or body, so a hostile agent cannot redirect the recorder; the
  operator chooses it, and a wrong choice sends that agent's key to the host they named.
  Verified in `tests/test_security.py`
  (`test_the_capture_upstream_is_operator_supplied_and_must_be_http`,
  `test_nothing_in_the_recorder_chooses_an_upstream_from_request_content`).

### API keys

Verified in the source, not assumed:

- Keys are read from the environment only: `os.environ.get("OPENAI_API_KEY")` in
  `src/upshift/providers/openai_provider.py` and `src/upshift/providers/openai_batch.py`,
  `os.environ.get("ANTHROPIC_API_KEY")` in `src/upshift/providers/anthropic_provider.py`.
- A `.env` file in the working directory is loaded by a minimal `KEY=VALUE` reader in
  `src/upshift/cli.py`; the existing environment always wins and the loader never logs a
  value. `.env` is gitignored.
- The key is passed straight into the vendor SDK client constructor
  (`Anthropic(api_key=…)` / `OpenAI(api_key=…)`) and is never placed in a request body.
- **Run records cannot contain a key or an auth header.** A recorded API call is the
  `APICall` dataclass in `src/upshift/schemas.py`: `{endpoint, request, response, error}`.
  `request` is the request *body* the agent loop built — `model`, `system`, `messages`,
  `tools`, and mapped params — and nothing else. Transport concerns (the API key, the
  `x-api-key` / `Authorization` header, the `anthropic-workspace-id` header set from
  `ANTHROPIC_WORKSPACE_ID`, base URL, timeout, retries) live on the SDK client object and
  never enter that dict. Grepping the committed `runs/` tree for `sk-ant-`/`sk-proj-` finds
  only placeholder strings that appear in the *source code of target repositories* that
  `adapt` read and quoted as evidence — no real credential.

### Run records contain everything you sent and everything you got back

By design: an inspectable diff requires the full transcript. A run record holds your system
prompt, your tool schemas, every user message, every model response, every tool result, and
token/cost accounting. `upshift adapt` records additionally contain **verbatim source
excerpts from the repository you pointed it at**.

So: **review a run directory before you publish it.** If your agent's prompt embeds secrets,
customer data, or internal API details, those are in the record. If you ran `adapt` against
a private repository, that repository's source is quoted in the record. Nothing redacts it
for you.

### `upshift adapt` does not execute the target repository

Verified: `adapt` treats a target repo as text. It walks the tree, reads files that pass a
size/extension filter, renders notebooks as cell text, parses Python with the standard
library `ast` module, and sends selected slices to the model as evidence. It does not
`import` the repo, does not run its tests, does not run setup or build steps, and does not
`exec`/`eval` anything it read (`ast.literal_eval` on a literal node is the only evaluation,
and it does not execute code). The only subprocess it spawns is `git`, with a fixed argv
(`["git", "clone", "--depth", "1", "--", <url>, <dest>]` and `["git", "rev-parse", "HEAD"]` —
never a shell string). `clone --depth 1` neither runs the remote repository's hooks nor
initialises its submodules, and no `git` command that would is ever run.

Two things a hostile repository might try, and what stops them:

- **Reading a file that is not in the repository.** A checked-in symlink named `config.py`
  that points at `~/.ssh/id_rsa` would otherwise be read and quoted to the model as evidence.
  The walk resolves every candidate path and skips anything that lands outside the repository
  root; the same check already applied to a file path the *model* asks to see in the
  pointer-following round.
- **Getting code into the `backend.py` that `adapt` writes.** The generated file's header
  carries the origin, the commit and one provenance line per tool, and tool names and
  citations are model output influenced by repo text. Every one of those is escaped before
  it is pasted into the docstring, `TOOL_SPECS` is written with `json.dumps` rather than
  string substitution, and the result is parsed before it is written.

**But the agent it produces is still code you will run.** The output of `adapt` includes a
`backend.py`, and every `upshift upgrade` run imports and calls that file's `Backend` class
in your own process, unsandboxed. That is unavoidable — executing the agent's tools is the
measurement. `adapt`-generated backends are scaffolding meant to be read and edited; read
`backend.py` before you run it, exactly as you would any generated code.

### The shell_gpt backend runs model-generated commands in Docker

`agents/shell_gpt/backend.py` reproduces ShellGPT's shell tool, so the model's output is a
shell command that gets executed. It never runs on the host. Each command runs as:

```
docker run --rm --network none --hostname shellbox --pids-limit 512 \
    --memory 512m --security-opt no-new-privileges \
    -v <per-episode tmpdir>:/work -w /work -e TZ=UTC -e LC_ALL=C \
    upshift-shellbox:latest bash -c <command>
```

The container's environment is `TZ=UTC` and `LC_ALL=C` and nothing else. Docker forwards no
host variable into a container unless it is told to, and this argv contains no `--env-file`,
no `--env-host`, and no bare `-e NAME` (the form that would copy the host's value of `NAME`
across) — so `OPENAI_API_KEY` and everything else in your shell is unreachable from inside.
The `docker` client process itself does inherit your environment, because it needs
`DOCKER_HOST` and friends to reach the daemon at all; the boundary that matters is the
container's environment, and that one is minimal. Pinned by
`tests/test_security.py::test_the_container_receives_a_minimal_environment`.

`--network none` means no network from inside the container, `--rm` and a fresh per-episode
temporary directory mean no state survives, `--pids-limit 512` bounds fork bombs,
`--memory 512m` bounds the other half of that problem (a command that allocates until the
host swaps is OOM-killed inside its own container), and the only host path mounted is that
throwaway directory — writable, but nothing outside it is reachable. There is a wall-clock
timeout, after which the container is force-removed. The command is passed as a single argv
element after `bash -c`, so however it is written it cannot become an argument to `docker`
itself; `subprocess` is never called with `shell=True`. If Docker is not available the
backend returns a `sandbox unavailable` error — it does **not** fall back to running the
command on your host.

This is a container, not a security boundary against a determined attacker: a Docker escape
is a Docker escape. Do not point this agent at a model or prompt you actively distrust.

### What upshift writes

`upshift upgrade` writes everything under its runs root (`runs/` by default, `--runs-root` to
move it): the per-rep records, the behavioural diff, and — when repairs are tried — a **copy**
of your agent directory at `runs/<tag>/patched_agent/` that the repair loop edits, plus the
resulting `upgrade.patch`. **Your own agent directory is not modified**; applying the patch is
your decision. Nothing is written outside the runs root, your git history is not touched, and
nothing is ever committed or pushed for you. `upshift init` and `upshift adapt --out` each
create one new directory and refuse to write into a non-empty one, and neither ever deletes
anything.

The runs root is the boundary, and it is enforced rather than assumed: a `--tag` and a case
id each become one directory name under it, so both are checked to be plain names — letters,
digits, `.`, `_`, `-` — and a value containing `/` or `..` is rejected before any directory is
created. This matters because the repair loop replaces `runs/<tag>/patched_agent/` with a
fresh copy of your agent each round, and a case id can come from a `cases.json` that `adapt`
drafted from a repository you have not read.

### Known, accepted

- A `response_matches` check compiles a regular expression from your own `cases.json`. A
  pathological pattern can make matching take exponential time — on your machine, against
  your own case file. It is a footgun, not a boundary, and it is not sandboxed. Note that a
  `cases.json` drafted by `upshift adapt` contains patterns *the model* wrote from the target
  repository's text; that file is one of the ones the report tells you to review.
- The container is a container: a Docker escape is a Docker escape (see above).
