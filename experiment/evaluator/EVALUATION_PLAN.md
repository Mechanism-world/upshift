# EVALUATION PLAN — Arm C (independent evaluator)

**Written 2026-09-14, Phase 1, BEFORE either candidate patch exists or was seen.**
Frozen on write. Departures are recorded in `DEPARTURES.md`, never by editing this file.

Repository under test: `gptme/gptme` @ `3411afec50c9e1124441bec1b00c0831400c8592`
(verified present at `/Users/atilavahedian/Desktop/exp914/upstream/gptme`, `git log -1` → `3411afec5`).
Migration: `gpt-5.6-sol` → `gpt-6-astra`.

Budget: **$2.00 USD hard cap**, authorized 2026-09-14 (supersedes the $0.00 line in the
standing brief). Sub-ceilings in §4.6. Nothing outside §4 may spend money.

---

## 0. What I read, and one contamination incident to record

**Read (allowed):** `experiment/PROTOCOL_ARMS.md`, `experiment/ASTRA_FACTS.md`,
`experiment/docs/EVALUATOR_BRIEF_COMMON.md`, and the pristine upstream checkout.
Nothing else. I did not open `w1/`, `w2/`, `upshift-src/`, `quarantine/`, `Upshift/runs`,
`Upshift/reports`, `Upshift/CLAUDE.md`, rescue-ops, or any bench directory.

**Incident (self-reported).** While diagnosing why a local pytest run was taking >15
minutes, I ran `ps -ax -o command`. The output included the full command lines of both
arms' processes. From that I learned: (a) the two arm directories are
`…/exp914/w1/gptme` and `…/exp914/w2/gptme`, (b) both were running pytest at that moment,
(c) one was running `pytest tests/ -m "not slow and not requires_api and not integration"
-q -n 16 --timeout 120`, the other a per-file loop over nine Tier-1 files. I learned **no
patch content, no diff, no source, no arm identity, and nothing about which workflow
either arm used.** The two arms will reach me as X and Y in an order I do not control, so
this cannot be mapped onto either label. I record it rather than conceal it. Mitigation
adopted for the rest of the experiment: I will not run bare `ps` again; process checks are
filtered to my own PIDs.

**Second incident, non-contaminating but material:** my own test run was competing for CPU
with both time-limited arms. I killed it (`pkill` on my probe venv only, confirmed gone)
and **commit to running no heavy local job until the coordinator tells me both arms have
finished.** All timings in this plan are therefore stated as procedure, not as measured
wall-clock.

---

## 1. Application procedure — patch → clean checkout → build from scratch

### 1.1 Layout

```
/Users/atilavahedian/Desktop/exp914/eval/
├── EVALUATION_PLAN.md          # this file, frozen
├── DEPARTURES.md               # any deviation, with reason and timestamp
├── work/
│   ├── base/gptme/             # pinned SHA, unpatched  → BASELINE
│   ├── candX/gptme/            # pinned SHA + Candidate X patch
│   └── candY/gptme/            # pinned SHA + Candidate Y patch
├── results/
│   ├── baseline/ candX/ candY/ # raw pytest output, wire captures, live logs
│   ├── FLAKY_LIST.md           # written before any candidate is scored
│   └── LEDGER.md               # every paid call, running total
└── bin/                        # the scripts below; ONE script per job, run for all three trees
```

### 1.2 Clean checkout (run once per tree; identical for base, X, Y)

```bash
PIN=3411afec50c9e1124441bec1b00c0831400c8592
UPSTREAM=/Users/atilavahedian/Desktop/exp914/upstream/gptme
TREE=/Users/atilavahedian/Desktop/exp914/eval/work/<base|candX|candY>

rm -rf "$TREE" && mkdir -p "$TREE"
git clone --quiet --no-hardlinks "$UPSTREAM" "$TREE/gptme"
cd "$TREE/gptme"
git checkout --quiet --detach "$PIN"
git rev-parse HEAD            # MUST print $PIN — recorded
git status --porcelain        # MUST be empty — recorded
```

The clone source is the local pristine mirror, which is itself verified at `$PIN`. The
upstream directory is never written to.

### 1.3 Applying a candidate

A candidate arrives either as a patch file or as a worktree.

**If a patch file** (`candX.patch`):
```bash
cd "$TREE/gptme"
git apply --check --verbose ../candX.patch     # must succeed; failure = recorded, see below
git apply            --verbose ../candX.patch
git status --porcelain > ../applied_files.txt  # exact file list, recorded
git diff --stat       > ../applied_stat.txt
```

**If a worktree** (the arm hands me a directory): I do **not** copy the arm's build
artifacts. I derive the patch myself and apply it to my own clean checkout, so both
candidates are built by the same procedure:
```bash
git -C <arm-worktree> diff "$PIN" -- . \
    ':(exclude).venv' ':(exclude)*.lock' ':(exclude)**/__pycache__' \
  > ../candX.patch
```
Untracked files added by the arm are collected separately with
`git -C <arm-worktree> ls-files --others --exclude-standard` and copied in explicitly;
the list is recorded. If an arm's change exists only as an uncommitted, untracked,
non-source artifact (a venv, a log, a scratch file), it is **not** part of the patch and
is not applied — and that fact is recorded, not silently dropped.

**If `git apply --check` fails**, I try `git apply --3way`, then `patch -p1`. If all fail,
that candidate is scored **FAILURE — patch does not apply to the pinned SHA**, the exact
error is recorded, and I do not hand-repair it. Repairing a patch would make me a third
migration arm.

### 1.4 Build from scratch — solving the `uv sync` / pytest problem NOW

`pyproject.toml` declares dev dependencies under **`[tool.poetry.group.dev.dependencies]`**
(pyproject.toml:145–163), which is Poetry's own group syntax. `uv` reads PEP 735
`[dependency-groups]` and `[tool.uv.dev-dependencies]`, neither of which exists here, so
`uv sync` installs the runtime dependencies only and **pytest is absent**. There is no
`uv.lock`; only `poetry.lock` (which uv does not consume). Poetry itself is not installed
on this machine (`which poetry` → not found). The build backend is `poetry-core>=2.0`
(pyproject.toml:448–450) with `dynamic = ["dependencies"]` (pyproject.toml:11), which uv
builds correctly.

**Resolution (verified working on a scratch clone of the pinned SHA before this plan was
frozen):** create an explicit venv, install the project editable, then install the exact
test dependencies named in the dev group by hand.

```bash
cd "$TREE/gptme"
uv venv --python 3.12 .venv
export VIRTUAL_ENV="$PWD/.venv"
uv pip install -e .
uv pip install \
  "pytest>=9.1,<10" pytest-asyncio pytest-cov "pytest-xdist>=3.8" \
  "pytest-profiling>=1.7" "pytest-dotenv>=0.5.2" "pytest-timeout>=2.4" \
  "pytest-retry>=1.7" pytest-mock greenlet
.venv/bin/python -c "import pytest, gptme; print(pytest.__version__, gptme.__file__)"
uv pip freeze > ../pip-freeze.txt     # recorded per tree and DIFFED across the three
```

Version list mirrors pyproject.toml:154–164 exactly. `pip-freeze.txt` is diffed across
base/X/Y; any dependency difference not caused by a candidate's own dependency change is
a confound and is recorded before scoring.

Verified on the pinned SHA: this produces a working environment and
`pytest --collect-only` over all ten Tier-1 files collects **651 tests with zero collection
errors**. Extras (`-E all`) and `playwright install` are **not** installed: Tier 2 is run
with `-m "not slow …"`, collection succeeds without them, and installing a browser engine
adds a large machine-specific confound. If any Tier-2 collection error traces to a missing
extra, I install that single extra **in all three trees** and record it in `DEPARTURES.md`.

### 1.5 Mandatory environment hygiene for every offline run

`tests/conftest.py` does **not** unconditionally force offline mode. The protocol's §5
claim is conditional: `OPENAI_BASE_URL=http://localhost:666` is set only inside
`if not has_api_key()` (conftest.py:254–262), and `has_api_key()` (conftest.py:92–101)
consults `Config.get_env`, which falls back to the **user config file** as well as the
environment (gptme/config/core.py:168–184). Worse, `pytest_sessionstart` →
`_check_anthropic_quota_exhausted` (conftest.py:104–133, called at :289) makes a **real
billable Anthropic API call** whenever an Anthropic key is visible.

Therefore every offline invocation in this plan runs through one wrapper:

```bash
# bin/offline_env.sh — sourced by every pytest job
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u OPENROUTER_API_KEY -u DEEPSEEK_API_KEY \
    -u XAI_API_KEY -u GEMINI_API_KEY -u GROQ_API_KEY -u LLM_PROXY_URL \
    -u LLM_PROXY_API_KEY -u GPTME_THINKING_EFFORT \
    MODEL=local/test OPENAI_BASE_URL=http://localhost:666 "$@"
```

Preflight assertion, run once per tree and recorded, aborting the job if it fails:
```bash
.venv/bin/python -c "
from gptme.config import get_config
c = get_config()
assert not any(c.get_env(k, '') for k in
  ('OPENAI_API_KEY','ANTHROPIC_API_KEY','OPENROUTER_API_KEY','DEEPSEEK_API_KEY')), 'KEY VISIBLE'
print('offline-safe')"
```
(Verified on the scratch clone: prints `offline-safe`; this machine's gptme user config
dir is `~/Library/Application Support/gptme` and carries no keys.)

---

## 2. Frozen existing suite — baseline first, then candidates

### 2.1 Exact invocations

**Tier 1** (the ten files of PROTOCOL §5, in that exact order):
```bash
cd "$TREE/gptme" && bin/offline_env.sh .venv/bin/python -m pytest -q \
  --timeout 120 -n 8 -p no:cacheprovider \
  --junitxml=<results>/tier1.xml -rA \
  tests/test_llm_openai.py tests/test_llm_openai_reasoning_effort.py \
  tests/test_llm_openai_sampling.py tests/test_llm_models.py \
  tests/test_llm_models_resolution.py tests/test_llm_validate.py \
  tests/test_tool_use.py tests/test_tools_choice.py \
  tests/test_prompt_tools.py tests/test_util_cost.py \
  > <results>/tier1.txt 2>&1
```

**Tier 2** (PROTOCOL §5 verbatim in marker and target):
```bash
cd "$TREE/gptme" && bin/offline_env.sh .venv/bin/python -m pytest \
  tests/ -m "not slow and not requires_api and not integration" -q \
  --timeout 120 -n 8 -p no:cacheprovider \
  --junitxml=<results>/tier2.xml -rA \
  > <results>/tier2.txt 2>&1
```

Deviations from the literal §5 text, both forced and both recorded here in advance:
* `uv run pytest …` is replaced by `.venv/bin/python -m pytest …`, because `uv run` cannot
  see a Poetry dev group and therefore cannot run pytest at all (§1.4). The marker
  expression, the target (`tests/`) and `-q` are unchanged.
* `--timeout 120 -n 8 -p no:cacheprovider --junitxml -rA` are added for determinism,
  machine-readable results, and hang protection. They are identical for baseline, X and Y.
  `-n` is pinned to `8` (not `auto`) so xdist distributes work identically on every run.
* **No `--retries`.** pytest-retry is installed (it is in the dev group) but is inert
  unless `--retries` is passed. Passing it would mask exactly the instability this plan
  measures. Flakiness is handled explicitly in §2.3 instead.

### 2.2 Pinned-SHA baseline, established first

Before any candidate is touched, the baseline tree is built and **Tier 1 and Tier 2 are
each run three times** (offline, $0). This produces:

* `results/baseline/tier{1,2}.run{1,2,3}.xml`
* `results/baseline/PASSING_SET.txt` — every test id that passed in **all three** runs.
  This is the only set against which regressions are counted.
* `results/FLAKY_LIST.md` — every test id whose outcome was **not unanimous** across the
  three baseline runs, plus every test id that errored or was skipped non-deterministically.

Both artifacts are written to disk and their SHA-256 recorded **before any candidate tree
is built**, so the definition of "regression" is fixed before I can know what either
candidate breaks.

### 2.3 Flaky-test rule — decided now

1. A test on `FLAKY_LIST.md` is **excluded from regression accounting for both candidates**.
   Its per-candidate outcome is still recorded, and if a candidate fails a flaky test in
   all three re-runs while the baseline passed it in ≥1 of 3, that is reported as a
   *signal*, never as a counted regression.
2. A test in `PASSING_SET.txt` that fails on a candidate is re-run **3 times in isolation**
   on that candidate (`pytest -q --timeout 120 -p no:xdist <nodeid>`), and the **same 3
   isolated re-runs are performed on the other candidate and on the baseline**, whatever
   their headline result was. Then:
   * fails 3/3 → **REGRESSION** (counted).
   * fails 2/3 → **REGRESSION** (counted).
   * fails 1/3 → added to `FLAKY_LIST.md` as a late discovery, excluded for **both**
     candidates, and the addition is timestamped in `DEPARTURES.md`.
   * fails 0/3 (i.e. only failed under `-n 8`) → recorded as **order/parallelism-sensitive**,
     excluded for both, and noted as a finding about the suite, not the candidate.
3. A test that **errors at collection** is never treated as flaky. Collection errors are
   counted as regressions immediately.
4. `PROTOCOL §11(a)` says "no worse pass rate than the pinned-SHA baseline". I read this
   **per-test, not in aggregate**: no test in `PASSING_SET.txt` may fail. An aggregate
   reading would let a candidate break one test and fix another and still "not regress".
   I commit to the per-test reading now, before I can know which candidate it binds.
5. A newly *passing* test (failed at baseline, passes on a candidate) is recorded as a
   **repair**, listed, and reported — it is never netted against a regression.

---

## 3. Migration acceptance checks M1–M5 — concrete mechanisms

### 3.0 The instrument: a loopback wire recorder (prototype built and verified)

M1 and M2 are wire assertions. Both are observable **without a paid call**, because of a
property of this application I verified on the pinned SHA:

* For `provider == "openai"`, `init()` passes `base_url = proxy_url or None`
  (llm_openai.py:549–552) — gptme itself never sets an OpenAI base URL. The openai SDK
  then reads `OPENAI_BASE_URL` from the environment (openai 2.54.0,
  `OpenAI.__init__`). This is the same hook upstream's own conftest uses (conftest.py:259).
* `_is_proxy()` (llm_openai.py:780–791) keys **only** off `LLM_PROXY_URL`, so pointing
  `OPENAI_BASE_URL` at a local recorder does **not** flip
  `_should_use_responses_api` (llm_openai.py:357–364). The endpoint decision under test is
  left intact. This is the crucial property that makes the instrument valid.

`bin/recorder.py` (prototype already written and exercised) is a stdlib
`ThreadingHTTPServer` bound to `127.0.0.1`, in one of two modes:

* **`record-and-400`** — appends `{path, method, body}` to a JSONL file and replies
  `400 {"error": {"code": "recorder_stub"}}`. Nothing leaves the machine; **$0**.
  A 400 is not transient, so gptme's retry ladder
  (`_handle_openai_transient_error`, llm_openai.py:793) does not re-send.
* **`forward`** — records the request, forwards the raw bytes to `https://api.openai.com`,
  and streams the response back verbatim (chunked, so SSE survives). Used **only** inside
  §4, so the paid live runs also produce full wire evidence for M1/M2/M5 at no extra cost.

Verification already performed on the pinned SHA (mode `record-and-400`, fake key
`sk-test-fake-key`, $0): gptme emitted exactly one request, recorded as
`POST /v1/responses`, `stream: true`, `model: "gpt-6-astra"`, `store: false`, 3 tools
(`complete`, `save`, `shell`), and **no** `temperature`, `top_p`, `top_logprobs`,
`logprobs`, `reasoning`, `prompt_cache_retention` or `prompt_cache_options` key. That is
the baseline wire fact; it is recorded, not interpreted, and it is not a verdict about any
patch.

The driver command (identical for base, X, Y — only `$TREE` changes):
```bash
cd "$TREE/gptme"                     # cwd matters: the repo's own gptme.toml is project config
env OPENAI_API_KEY=sk-test-fake-key \
    OPENAI_BASE_URL=http://127.0.0.1:$PORT/v1 \
    GPTME_MAX_STEPS=5 TERM=dumb \
    [GPTME_THINKING_EFFORT=$L] [GPTME_OPENAI_RESPONSES_API=$R] \
  .venv/bin/gptme --non-interactive --system short -t save,shell \
    --tool-format tool --model openai/gpt-6-astra --workspace "$WS" "$TASK"
```
`--system short` (cli/main.py:644–649) is used for every wire probe and every live run.
Rationale and its cost consequence are in §4.2; it is applied identically to the baseline
and both candidates.

### 3.1 M1 — endpoint

*Contract cited:* `_should_use_responses_api` (llm_openai.py:357–364) and the
`supports_responses_api` registry flag (llm_openai_models.py:16–27 for `gpt-6-astra`);
upstream's own test `test_should_use_responses_api_enabled_by_default`
(tests/test_llm_openai.py:1064–1088). Doc: "Chat Completions does not support function
calling with GPT-6 Astra" (ASTRA_FACTS §BREAKING — endpoint / tool calling).

**Mechanism (offline, $0).** Run the driver with tools present and assert every recorded
request satisfies `path == "/v1/responses"` and `"tools" in body`. Run it in **four
configurations**, all offline, all identical across candidates:

| id | config | expectation |
|---|---|---|
| M1-a | default (streaming) | `/v1/responses` |
| M1-b | `--no-stream` | `/v1/responses` |
| M1-c | `GPTME_OPENAI_RESPONSES_API=0` | see below |
| M1-d | `-t none` (no tools) | either endpoint is legal; recorded only |

M1-c is the app's own documented escape hatch (`_responses_api_enabled`,
llm_openai.py:345–355). Forcing Chat Completions *with tools* on this model is a
documented 400. M1-c is **not** part of the pass/fail for M1 — it is a §6 tie-breaker
input, because the protocol does not require an arm to fix a debug flag.

**M1 PASSES** iff M1-a and M1-b both show `/v1/responses` for every tool-bearing request.
**Live confirmation** comes free from §4's `forward` mode: the same assertion is re-applied
to every request of every live rep.

### 3.2 M2 — sampling parameters

*Contract cited:* `tests/test_llm_openai_sampling.py`; `_get_temperature`
(llm_openai.py:136–152) and `_get_top_p` (llm_openai.py:155–171), whose model-specific
branch is the substring test `"gpt-5" in model_meta.model` — which does not match
`gpt-6-astra`; and the call sites' `if not is_reasoner:` guards
(llm_openai.py:1124–1131 responses, :1167–1174 chat completions, :1523–1529 stream).
Doc: remove `temperature`, `top_p`, `top_logprobs`; on Chat Completions also `logprobs`
(ASTRA_FACTS §BREAKING — removed sampling / logprob parameters).

**Mechanism (offline, $0).** Over every request captured in M1-a…M1-d, assert the
recorded JSON body contains **none** of `temperature`, `top_p`, `top_logprobs`,
`logprobs`, and that `include` (Responses) does not contain
`message.output_text.logprobs`. Applied recursively so a value hidden inside `extra_body`
is caught: the check walks the whole body object, not just its top level.

Two additional offline sub-checks, because a request that never gets built proves nothing:
* **M2-callerforced**: call the library directly with explicit caller values —
  `llm_openai.chat(msgs, "openai/gpt-6-astra", tools, temperature=0.37, top_p=0.82)` and
  the `stream()` equivalent — against the recorder, and assert the banned keys are still
  absent. This closes the hole where the parameters are omitted only because nothing
  supplied them. `tests/test_llm_openai_sampling.py:87–121` establishes that callers
  *can* supply them, so this is a real reachable path.
* **M2-nonreasoner**: assert the patched code does not achieve M2 by flipping
  `supports_reasoning` to `False` for this model — that would satisfy M2 while breaking M3
  and the reasoning metadata contract (`_record_usage`, llm_openai.py:224–242). Checked by
  asserting `get_model("openai/gpt-6-astra").supports_reasoning is True`. If a candidate
  deliberately sets it False, that is not an automatic failure but is a §5 source-review
  finding of the highest severity and is reported.

**M2 PASSES** iff no banned key appears in any recorded request in any configuration,
offline and live, and M2-callerforced also shows none.

*Note for the record, not a verdict:* the pinned SHA already sends none of these on the
default path, because `gpt-6-astra` is registered with `supports_reasoning: True`
(llm_openai_models.py:21). A candidate that "fixes" M2 by adding a `gpt-6` branch to
`_get_temperature` has added an unnecessary change (§5.2), not a repair. A candidate that
changed nothing here is correct, not lazy. I commit to both readings now.

### 3.3 M3 — reasoning effort validity

*Contract cited:* `_OPENAI_REASONING_EFFORTS` (llm_openai.py:1279–1287),
`_resolve_reasoning_effort` (llm_openai.py:1293–1322), `extra_body`
(llm_openai.py:1368–1372), and upstream's own
`test_openai_effort_set_matches_sdk_literal` / `test_openai_chat_sends_reasoning_effort`
(tests/test_llm_openai_reasoning_effort.py:81–105). Doc: `none` returns HTTP 400 on this
model; valid values `low|medium|high|xhigh|max`; if previously on `none`/`minimal`, start
at `low` (ASTRA_FACTS §BREAKING — reasoning effort). Trap: the endpoint 400 advertises
`none` as the fix and `none` is itself rejected.

**Step 1 — what does the app claim to accept? (offline, $0, patch-agnostic.)**
For each level `L ∈ {none, minimal, low, medium, high, xhigh, max}` run the driver with
`GPTME_THINKING_EFFORT=L` against the recorder and classify from observed behaviour, not
from reading the patch:
* the app raises before any request (no line in the JSONL, `ValueError: Invalid … reasoning
  effort` on stderr) → **app REJECTS L**;
* a request is recorded → **app ACCEPTS L**; record where the level travelled
  (`reasoning.effort` on Responses, `reasoning_effort` on Chat Completions, or elsewhere).

This deliberately does not depend on the patch's internal structure: a candidate may
rename or restructure the effort set and still be measured correctly.

**Step 2 — does Astra accept it? (live, §4.3.)** For each `L` the app ACCEPTS, issue the
minimal live probe of §4.3 at N=5 and record HTTP status.

**M3 PASSES** iff, for every `L` the app accepts, no live probe returns HTTP 400 naming
that parameter. **M3 FAILS** if any accepted level 400s. If the app accepts **no** level
(i.e. it rejects everything including `low`), that is also a FAIL: `low|medium|high|xhigh|max`
are documented as valid and the application must be able to use at least `low`. That
condition is stated now so it cannot be tailored later.

Additional offline sub-check, **M3-default**: with `GPTME_THINKING_EFFORT` **unset**,
record whether any effort is sent at all. The pinned SHA sends none (verified). If a
candidate introduces a default effort for this model, it is measured under the same rule.

### 3.4 M4 — tool loop completes

*Contract cited:* `tests/test_tool_use.py`, and the eval harness's pass/fail contract
`EvalSpec.expect: dict[str, Callable[[ResultContext], bool]]` (gptme/eval/types.py:182–191).
**Citation correction, recorded now:** PROTOCOL §6 cites `gptme/eval/pass_rate_gate.py` as
the harness's pass/fail contract. It is not — that module is a lesson-injection gate
(pass_rate_gate.py:1–26, `GateDecision = Literal["inject","suppress","default"]`) and has
no bearing on whether an eval passes. The real contract is `EvalSpec.expect`. I use the
real one. Doc risk being tested: Astra is "more likely to ask the user a question when
additional input could materially change the result" (ASTRA_FACTS §BEHAVIORAL 1).

**Mechanism (live only — this one genuinely cannot be faked offline).** §4.4.

**Terminal-state definition, fixed now.** A rep is a **PASS** iff **all** hold:
1. process exit code is 0;
2. the tool actually ran — the expected side effect is present (workspace file / literal
   output);
3. the final assistant message contains the required literal (defined per case in §4.4);
4. the conversation log contains **no** `"Stopped: reached max steps limit"` system message
   (chat.py:606–613) — that is the stall/loop signature;
5. the run contains ≥1 request whose response carried a tool call **and** ≥1 subsequent
   request whose input carried that tool's result — i.e. the loop actually round-tripped,
   read off the `forward`-mode wire capture, not inferred from the transcript.

Termination-by-question is caught by (3): a clarifying question does not contain the
literal. I do not attempt to classify *why* a rep failed from prose; the wire capture and
the transcript are attached and the reader can see it.

**Threshold:** a case PASSES at **≥4 of 5** reps (80%). Fixed now, applied identically to
baseline, X and Y. A case is only counted against a candidate if the **baseline passed it
at the same threshold** — per PROTOCOL §11 final paragraph.

### 3.5 M5 — structured output

*Contract cited:* `supports_strict_tools` (llm_openai_models.py:26 for `gpt-6-astra`) and
its use at `_spec2tool` (llm_openai.py:2254–2261, `function_def["strict"] = True`), plus
the json-schema paths at llm_openai.py:341 and :382 (`"strict": True`).

**Ambiguity I must flag now, while I cannot know whom it helps.** On this application M5
may be **unobservable on the path the migration selects**:
* the Responses-API tool converter `_tool_spec_to_responses_tool`
  (gptme/llm/openai_responses.py:174–197) **never emits `strict`** — it emits only
  `{type, name, description, parameters}`. Verified on the live wire capture above: the
  recorded `tools[0]` has exactly those four keys.
* `strict: True` on tools appears **only** on the Chat Completions path
  (llm_openai.py:2260–2261), which is the path Astra rejects for function calling.
* the `response_format` / `text.format` json-schema strict paths require an
  `output_schema`, and gptme's only caller deliberately does not pass one
  (gptme/tools/subagent/execution.py:427–429: "output_schema is intentionally NOT passed
  to chat() here").

So if a candidate correctly routes to `/v1/responses`, **no strict surface is emitted at
all** and M5 has nothing to observe. I therefore commit M5 to a **three-valued** outcome:

* **PASS** — a strict surface is emitted and (a) no live 400 mentions the schema, and
  (b) every tool-call `arguments` object returned live parses as JSON and validates against
  the declared `parameters` schema (checked with `jsonschema` over the `forward` capture).
* **FAIL** — a strict surface is emitted and (a) or (b) fails.
* **NOT EXERCISED** — no strict surface is emitted on the selected path.

**NOT EXERCISED counts for neither candidate and against neither.** It is reported
verbatim for both. Even when M5 is NOT EXERCISED, sub-check (b) — arguments validate
against the declared `parameters` — is still run and reported, because it is free from the
same capture; it is reported as *schema conformance (non-strict)* and, again, is not part
of the M5 verdict.

---

## 4. Live Astra validation plan

### 4.1 Preflight (free)

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://api.openai.com/v1/models \
     -H "Authorization: Bearer $OPENAI_API_KEY"
curl -s https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"];
print(len(d), "gpt-6-astra" in {m["id"] for m in d}, "gpt-5.6-sol" in {m["id"] for m in d})'
```
`GET /v1/models` is free. Both model ids must be present or the live plan does not start.
The key is taken from the environment the coordinator provides; I do not read, print, or
copy its value.

### 4.2 Cost instrument and the one measurement compromise

Every live invocation runs with `GPTME_EXIT_STATS=1`, which makes gptme print a JSON cost
summary to stderr (gptme/util/cost.py:74–97; enabled at cli/main.py:1725):
`{"total_cost", "total_input_tokens", "total_output_tokens", "cache_read_tokens",
"request_count", ...}`. Every line is appended to `results/LEDGER.md` with the tree, case,
rep and running total. gptme prices `gpt-6-astra` at $10/$50 per MTok
(llm_openai_models.py:19–20), matching ASTRA_FACTS, and does **not** discount cache reads —
so the ledger **over-states** spend, which is the safe direction for a cap. The check
before every rep is: `running_total + worst_case_rep_cost ≤ ceiling`; if not, stop.

**The compromise, stated in advance.** All live runs use `--system short`, not the default
`full` prompt. Measured offline on the pinned SHA: the `full` system prompt for
`-t save,shell --tool-format tool` is **30,365 tokens**; `short` is **539**. At $10/MTok
input, one `full`-prompt request costs ~$0.30 before any output, so a single case at N=5
for two candidates plus a baseline would exceed the entire $2.00 cap. `--system short`
is a first-class upstream option (cli/main.py:644–649), not a hack, and it is applied
**identically to the baseline and both candidates**, so the comparison stays symmetric.

**What this does and does not cover, stated now:** wire-level checks (M1, M2, M3, M5) are
prompt-independent and are fully covered. M4's behavioural risk — Astra asking a
clarifying question instead of acting — *is* prompt-sensitive, and a 539-token prompt is
not the 30k-token prompt a real gptme user runs. **I therefore do not claim live coverage
of M4 under the application's default system prompt.** That gap is recorded as a
limitation of this evaluation, with its cause (cost), in the final report. It is not
covered for either candidate, so it cannot favour one.

### 4.3 Case L0 — effort-validity probe (covers M3 live)

Direct call into the application's own provider code (not a mock), one request, no tools,
bounded output:
```python
from gptme.llm import llm_openai
from gptme.message import Message
llm_openai.chat([Message("user", "Reply with OK.")], "openai/gpt-6-astra",
                None, max_tokens=16)      # → max_output_tokens=16 (llm_openai.py:1120)
```
run once per accepted level `L` (from §3.3 step 1) with `GPTME_THINKING_EFFORT=L`,
**N=5 reps per level**, through the `forward` recorder. `max_tokens=16` bounds output at 16
tokens even when the model reasons, so a rep costs ≈ $0.001. An invalid parameter is
rejected as a 400 body error, which bills nothing. Recorded per rep: HTTP status, error
`code`/`message` if any, and cost.

Pass rule: **all 5 reps non-400 → level valid**; **any 400 naming the effort parameter →
level invalid**. (A 400 is deterministic; N=5 is run anyway because the protocol requires
N=5 for every live check and the cost is negligible.)

### 4.4 Case L1 — tool round-trip (covers M4; supplies M1/M2/M5 live evidence)

The mandatory M4 case. Two turns, deterministic, cheap:

```
task:  Use the shell tool to run exactly: echo upshift-eval-ok
       Then reply with the command's exact output and nothing else.
tools: -t save,shell     tool-format: tool     --system short
guard: GPTME_MAX_STEPS=5
PASS:  literal "upshift-eval-ok" appears in the final assistant message
       AND the wire capture shows tool-call → tool-result → final answer
       AND no "Stopped: reached max steps limit"
       AND exit code 0
```
**N=5 reps**, fresh `--workspace` per rep, through the `forward` recorder.
Estimated ≈ $0.06/rep on Astra (measured request-1 size on the pinned SHA: ~1,380 tokens
serialized; ~1.3k in + reasoning/answer out over 2–3 requests).

### 4.5 Case L2 — write-and-run (conditional, covers M4 at greater depth)

```
task:  Write hello.py that prints exactly "Hello, world!", then run it with the
       shell tool and report its output.
PASS:  hello.py exists in the workspace AND its run output "Hello, world!" appears in
       the final assistant message AND the loop conditions of §3.4 hold.
```
**Conditional and symmetric:** L2 runs only if, after **all** mandatory work on baseline,
X and Y is complete, the remaining budget is **≥ $0.80**. It then runs for **baseline, X
and Y, or for none of them.** It is never run for one candidate alone.

### 4.6 Order, ceilings, and the out-of-budget rule

**Order (fixed now).** 1. free preflight → 2. **baseline** (`gpt-5.6-sol`, pinned SHA,
unpatched): L0 + L1 → 3. **first candidate** → 4. **second candidate** → 5. L2 if and only
if §4.5's condition holds. Candidate order is decided by a coin flip
(`python3 -c "import secrets;print(secrets.choice('XY'))"`) recorded in `LEDGER.md`
**before the first paid candidate call**, so order is not chosen by me.

Within each tree the order is fixed and identical: L0 levels in the order
`none, minimal, low, medium, high, xhigh, max` (skipping levels the app rejects), then L1
reps 1→5. Same commands, same reps, same order, driven by one parameterized script
(`bin/live.sh <tree> <model>`), so a divergence in treatment would require editing the
script mid-run — which would be a recorded departure.

**Ceilings (hard, enforced before each call):**

| line | ceiling |
|---|---|
| Baseline (`gpt-5.6-sol`, $5/$30) — L0 + L1 | **$0.35** |
| Candidate A (first drawn) | **$0.55** |
| Candidate B (second drawn) | **$0.55** |
| Reserve (L2, or a re-run applied to both) | **$0.55** |
| **Total** | **$2.00** |

The reserve is **ring-fenced**: it may only fund work performed for *both* candidates. It
can never be spent to finish one candidate alone.

**Out-of-budget rule — decided now, before I can know whom it favours.**
If the second candidate cannot complete the identical battery within its own $0.55
ceiling, I stop at the ceiling and apply **symmetric truncation**:

1. I stop that candidate's live work at the ceiling. I do **not** draw on the reserve to
   finish it, and I do **not** shorten N below 5 — a check is either completed at N=5 or
   it is not counted (PROTOCOL §6: narrow the case set, never the repetitions).
2. Every live check the second candidate did not complete at full N=5 is **removed from
   the verdict for both candidates**. The first candidate's results for those checks stay
   on disk as recorded evidence but are explicitly excluded from scoring, and the
   exclusion is listed by name in the report.
3. The verdict is rendered on the **common completed subset**.
4. If the common completed subset does not include **M3 and M4 each at N=5**, the live
   component is declared **INCONCLUSIVE for both candidates**, and the verdict rests on
   the offline tiers (§2), the offline wire checks (§3.1–3.3, $0, always completable) and
   the source review (§5) — stated as such, with the live gap named.
5. I never re-run one candidate "to be fair" after seeing the other's number. Any re-run
   is applied to both, funded from the reserve, and recorded in `DEPARTURES.md`.

**Abort conditions (any one stops all paid work immediately):** running total ≥ $1.90;
any billing/quota error; a rep whose single-run cost exceeds 3× the running median; an
unexpected model id in a recorded request body.

---

## 5. Source-review checklist

Performed **after** all mechanical results (§2–§4) are written to disk, so no reading of
either patch can influence a measured number. Both candidates get the same checklist in
the same order; per-candidate findings go to separate files (`results/cand{X,Y}/REVIEW.md`)
and the comparison is written only after both exist.

### 5.1 Mechanical test-weakening detection (run first, before reading anything)

Test weakening is checked by script, not by eye. Every check below is run for both
candidates and its raw output archived.

```bash
PIN=3411afec50c9e1124441bec1b00c0831400c8592
cd "$TREE/gptme"

# W1 — no file that existed under tests/ at the pinned SHA may be modified or deleted.
#      (Adding NEW test files is explicitly allowed by PROTOCOL §8.)
git ls-tree -r --name-only "$PIN" -- tests/ > /tmp/pin_tests.txt
git diff --name-status "$PIN" -- tests/ | grep -vE '^A' > /tmp/w1.txt   # must be EMPTY

# W2 — the ten Tier-1 files, byte-identical.
for f in tests/test_llm_openai.py tests/test_llm_openai_reasoning_effort.py \
         tests/test_llm_openai_sampling.py tests/test_llm_models.py \
         tests/test_llm_models_resolution.py tests/test_llm_validate.py \
         tests/test_tool_use.py tests/test_tools_choice.py \
         tests/test_prompt_tools.py tests/test_util_cost.py; do
  git diff --exit-code "$PIN" -- "$f" || echo "MODIFIED: $f"
done

# W3 — harness files unchanged (a weakening can hide in the harness, not the test).
git diff --exit-code "$PIN" -- tests/conftest.py tests/retry_compat.py tests/thread_leak.py

# W4 — pytest configuration unchanged (markers, timeout_func_only, addopts).
git diff "$PIN" -- pyproject.toml | grep -nE '^\+' | grep -iE \
  'pytest|marker|addopts|timeout|filterwarnings|asyncio|retries' || echo "clean"
ls pytest.ini setup.cfg tox.ini conftest.py 2>/dev/null   # new root-level config = finding

# W5 — skip/xfail/deselect/retry introduced anywhere in the diff.
git diff "$PIN" | grep -nE \
  '^\+.*(@pytest\.mark\.(skip|skipif|xfail)|pytest\.skip|pytest\.xfail|pytestmark|--deselect|-p no:|--retries|filterwarnings|# *type: *ignore)'

# W6 — assertion density per changed/added test file: assertions must not fall.
for f in $(git diff --name-only "$PIN" -- tests/); do
  echo "$f  pinned=$(git show "$PIN:$f" 2>/dev/null | grep -c 'assert')  now=$(grep -c 'assert' "$f" 2>/dev/null)"
done

# W7 — Makefile / CI test invocation unchanged (test target, markers, -n, --retries).
git diff --exit-code "$PIN" -- Makefile .github/workflows/test.yml
```

Any non-empty W1/W2/W3 output is a **PROTOCOL §5 violation** and, per §11, makes that
candidate a **FAILURE** regardless of every other result. W4–W7 hits are graded findings.

### 5.2 Do the added tests prove anything? (mechanical, symmetric)

For each **new** test file a candidate adds, I run it against the **unpatched pinned-SHA
tree** (`work/base`, offline, $0):
* new test **fails** at the pinned SHA and passes on the candidate → it genuinely pins the
  migration. Counted as **migration evidence**.
* new test **passes** at the pinned SHA → it asserts something the migration did not
  change. Not a failure, but not evidence either; recorded as such.
* new test **errors** at the pinned SHA (imports a symbol the patch introduces) → recorded
  as inconclusive-by-construction, counted as evidence only if the symbol it imports is
  itself required by a documented Astra change.

### 5.3 Reading checklist (applied to the non-test diff)

For each hunk, I record file:line, what it does, and a verdict from this fixed list:

1. **Justified** — required by a change documented in ASTRA_FACTS, or by an upstream
   contract, with the specific source named.
2. **Unnecessary** — touches behaviour no documented Astra change requires. Specifically
   flagged: edits to `_get_temperature`/`_get_top_p` (llm_openai.py:136–171) when the
   reasoner guard already suppresses those parameters; introduction of
   `prompt_cache_options.ttl` (the pinned SHA contains **no** `prompt_cache_retention` —
   verified by grep across `gptme/` and `tests/`, zero hits); introduction of `logprobs`
   handling (likewise **zero** hits at the pinned SHA). Adding machinery for a parameter
   the application never sends is scope creep, not a migration.
3. **Hardcoded model name / hardcoded behaviour** — any new literal `"gpt-6-astra"`,
   `"gpt-6"`, or a substring test like `"gpt-6" in model`, placed in a behavioural branch
   rather than in the model registry. Grounded in the application's own convention: the
   registry carries capability flags (`supports_responses_api`, `supports_strict_tools`,
   `supports_reasoning`, llm_openai_models.py:16–27) and the code branches on flags
   (llm_openai.py:357–364, :2254). Counted:
   `git diff $PIN -- gptme/ | grep -cE '^\+.*(gpt-6|gpt_6)'` with each hit classified as
   registry-data (fine) or behavioural-branch (finding).
   *Recorded caveat:* the pinned SHA already contains a substring branch of exactly this
   shape (`"gpt-5" in model_meta.model`, llm_openai.py:148/166). A candidate that extends
   an existing upstream pattern is graded more leniently than one that invents a new one,
   and I will say which it did.
4. **Unsupported assumption** — a claim about Astra with no support in ASTRA_FACTS or the
   application's own tests. Each is listed with the claim and the missing evidence.
5. **Maintainability** — dead code, duplicated logic, a flag whose two sources of truth
   can disagree, an error path that swallows the 400 this migration is about, a change
   that must be repeated for the next model.
6. **Correctness risk not caught by M1–M5** — e.g. behaviour on other providers
   (`openrouter`, `openai-subscription`, `moonshot`, `local`) altered as collateral. Checked
   mechanically: `git diff $PIN -- gptme/` restricted to files outside `gptme/llm/`, and a
   targeted re-read of any hunk in a shared helper.

### 5.4 Blast radius (mechanical)

```bash
git diff --stat "$PIN"                       # total
git diff --numstat "$PIN" -- gptme/          # source
git diff --numstat "$PIN" -- ':(exclude)gptme/' ':(exclude)tests/'   # everything else
```
Recorded as a table for both candidates. **Size alone is never a verdict** — a larger
correct patch beats a smaller incomplete one. It is used only at tie-break rung 3 (§6).

---

## 6. Tie-breaking rule — decided now, before any patch exists

If both candidates reach **SUCCESS** under PROTOCOL §11 (Tier 1 + Tier 2 at no worse
per-test pass rate than baseline; M1–M5 passing at N=5 where exercised; no test weakened),
I apply this ladder **in order** and stop at the first rung that separates them. Each rung
is a count I can compute without judgement.

**Rung 1 — robustness across the application's own supported configurations.**
The number of these offline configurations in which the migration holds (correct endpoint,
no banned parameter, no pre-request exception):
`--stream` / `--no-stream`; `GPTME_OPENAI_RESPONSES_API=0`; each accepted
`GPTME_THINKING_EFFORT` level; `-t none` (tool-free); `LLM_PROXY_URL` set (which flips
`_is_proxy`, llm_openai.py:780–791, and forces Chat Completions). All are configurations
upstream itself supports and documents. Higher count wins. **$0, offline, run for both
regardless of whether the tie-break is needed**, so this data exists before I know who
needs it.

**Rung 2 — migration evidence.** Count of added tests that FAIL at the pinned SHA and PASS
on the candidate (§5.2). Higher wins. A migration whose correctness is pinned by a test
survives the next refactor; one that is not, does not.

**Rung 3 — scope discipline.** Count of changed source lines outside `gptme/llm/**`
(docs, `CHANGELOG`, comments and added tests excluded). Fewer wins.

**Rung 4 — model-agnosticism.** Count of new hardcoded model-name literals placed in
behavioural branches rather than registry data (§5.3 item 3). Fewer wins.

**Rung 5 — no winner.** If all rungs tie, the result is
**"EQUIVALENT — they are the same migration"**, reported as a finding in its own right per
the standing brief. I will not manufacture a difference, and I will not fall back to
subjective code taste as a decider.

Two things this ladder deliberately does **not** reward: smaller diffs as such (rung 3 is
narrowly about *out-of-scope* lines), and speed (efficiency data is sealed until Phase 3
and is never part of the correctness verdict).

---

## 7. Symmetry guarantees and anti-anchoring

1. **One script per job.** Every measurement is produced by a script in `eval/bin/`
   parameterized only by tree path and (for live) model id. The same script runs for
   baseline, X and Y. Treating candidates differently would require editing a script
   between runs — which leaves a git trace in `eval/` and must be logged in
   `DEPARTURES.md`.
2. **Identical environments.** Same Python (3.12 via uv), same install recipe (§1.4), same
   pinned test-dependency list. `pip-freeze.txt` is diffed across the three trees and any
   difference is reported before scoring.
3. **Machine quiet.** No other heavy job of mine runs concurrently with a measured run.
   Timing-sensitive results are re-measured if the machine was loaded.
4. **Measure-then-read.** All of §2, §3 and §4 complete for **both** candidates before I
   open either diff (§5). Mechanical numbers cannot be anchored by prose I have not read.
5. **Interleaved, not sequential, scoring.** For each check I run baseline → X → Y before
   moving to the next check, rather than finishing X completely and then starting Y. This
   prevents a drifting standard between candidates. (Exception: live §4, where each tree's
   battery must run contiguously to keep prompt-cache conditions comparable; there the
   coin-flip order and the per-candidate ceiling do the work instead.)
6. **Randomized review order.** §5's reading order is a fresh coin flip, recorded, and
   independent of the X/Y labels and of the live order.
7. **Separate write-ups.** `results/candX/REVIEW.md` and `results/candY/REVIEW.md` are each
   written to completion before the comparison document is started. Neither review may
   reference the other candidate.
8. **Frozen thresholds.** N=5, the ≥4/5 live threshold, the per-test regression reading,
   the flaky rule, the ceilings, and the tie-break ladder are all fixed in this file,
   before any result exists.
9. **Unblinding is last.** Arm identity and `metrics.json` are read only after §2–§6 are
   written to disk and hashed.

---

## 8. Criteria I judge ambiguous or unmeasurable — stated now

Recorded before I can know whom any of these favours.

1. **M5 may be vacuous.** On the Responses API path this application emits no `strict`
   flag for tools at all (openai_responses.py:174–197; confirmed on the live wire capture,
   whose `tools[0]` has keys `{type,name,description,parameters}` only), and its only
   `output_schema` caller deliberately passes none (subagent/execution.py:427–429). If the
   correct migration routes to `/v1/responses`, M5 has no surface to test. Handled by the
   three-valued rule in §3.5 — **NOT EXERCISED counts for and against neither candidate.**
2. **M2 may already be satisfied at the pinned SHA.** The reasoner guards
   (llm_openai.py:1124, :1167, :1523) suppress `temperature`/`top_p` for any model with
   `supports_reasoning: True`, which `gpt-6-astra` already has
   (llm_openai_models.py:21). Verified on the wire: the unpatched pinned SHA sends none of
   the banned parameters. So M2 may measure "nothing needed doing" rather than "this arm
   fixed it". I commit now: a candidate that changed nothing here **passes M2**, and a
   candidate that added a model-specific sampling branch is credited with M2 **and** debited
   one "unnecessary change" (§5.3 item 2).
3. **M1 may already be satisfied at the pinned SHA.** Verified on the wire: unpatched, with
   tools and `--tool-format tool`, `openai/gpt-6-astra` already issues
   `POST /v1/responses`, because the registry entry carries
   `supports_responses_api: True` (llm_openai_models.py:22). Same commitment as (2).
   The consequence — that M1 and M2 may not discriminate between the candidates at all —
   is recorded now so that "they both pass M1 and M2" is understood as a property of the
   application, not a compliment to either arm.
4. **M3's phrase "every effort level the application accepts"** is ambiguous between the
   *declared* set (`_OPENAI_REASONING_EFFORTS`, which is provider-wide, not per-model) and
   the *effective* set for this model. I resolve it operationally in §3.3 step 1: a level is
   "accepted" iff the application actually builds and sends a request carrying it. This is
   patch-structure-agnostic and is the reading closest to the protocol's own gloss ("must
   not produce HTTP 400 from the API").
5. **M4's "must not terminate by asking the user a question"** is not directly observable —
   I cannot reliably classify an arbitrary final message as a question. I substitute the
   operational definition in §3.4 (the required literal is absent), which catches
   question-termination along with every other non-completion, and I attach the transcript
   so a reader can see the actual failure mode. I do not report *why* a rep failed as if it
   were measured.
6. **M4's live coverage is not at the application's default system prompt** (§4.2). This is
   a real gap, caused by the 30,365-token `full` prompt against a $2.00 cap. Symmetric,
   named, and not repairable within budget.
7. **PROTOCOL §5's offline-safety claim is conditional**, not absolute (conftest.py:254–262),
   and `pytest_sessionstart` makes a real billable Anthropic call when a key is visible
   (conftest.py:104–133, :289). I enforce offline safety myself (§1.5) rather than relying
   on the claim. Reported as a finding about the protocol, not about either arm.
8. **PROTOCOL §6 miscites the eval pass/fail contract** (`pass_rate_gate.py` is a
   lesson-injection gate, not a pass/fail contract). Corrected in §3.4; the substituted
   citation is `EvalSpec.expect`, eval/types.py:182–191.
9. **Isolation between arms is by instruction, not by OS boundary** (PROTOCOL §12's own
   honest limitation). My own §0 incident demonstrates the leak channel is real —
   process command lines are world-readable. I report it as a limitation of the
   experimental design, and I have stated exactly what I saw.

---

## 9. Deliverables and order of writing

1. `results/baseline/` + `FLAKY_LIST.md` + `PASSING_SET.txt` (hashed) — before any
   candidate tree is built.
2. `results/cand{X,Y}/tier{1,2}.{txt,xml}`, `wire/*.jsonl`, `live/*.json`, `LEDGER.md`.
3. `results/cand{X,Y}/REVIEW.md` — separate, neither referencing the other.
4. `SCORING.md` — per-candidate verdict (SUCCESS / FAILURE / INCONCLUSIVE) with the
   evidence for each of §2, M1–M5, §5, and the tie-break ladder if reached.
5. Only then: unblinding, and a Phase-3 efficiency note kept strictly separate from the
   correctness verdict.

Any departure from this file is appended to `DEPARTURES.md` with timestamp, what changed,
why, and whether it was applied to both candidates.
