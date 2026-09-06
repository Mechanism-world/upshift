# ADAPT_EDITS — hand-written adapter, no `upshift adapt` run

The target is `TutteInstitute/toponymy` @ `bcc46b86fc8ef1b791098497556f85678ec8cac0`. The
agent is a **single-call, tool-less** namer: one system message, one user message, one text
answer parsed with a regex. Everything it sends is fully determined by four files at the pin,
so the five adapter files were transcribed by hand and **$0.00 of the case budget went to
adaptation**. No target code was installed or executed — the install phase was declined
(`--no-install`), see §"Install".

| file | origin |
| --- | --- |
| `system_prompt.txt` | `toponymy/templates.py` `PROMPT_TEMPLATES["layer"]["system"]`, rendered with `topic_name_prompt`'s own `render_params` (`toponymy/prompt_construction.py:426-441`) |
| `cases/cases.json` | five user messages = the same `["layer"]["user"]` template rendered over the repo's own committed fixture `toponymy/tests/subtopic_objects.json` |
| `agent.json` | `AnthropicNamer` defaults (`llm_wrappers.py:1865-1956`) + `LLMWrapper.generate_topic_name` defaults (`llm_wrappers.py:496-506`) |
| `tools.json` | `[]` — the namer defines no tools (see §2) |
| `backend.py` | trivial: no tools to execute |
| `render_prompts.py` | the transcription script itself, kept so the prompts are reproducible |

## 0. Edit ledger

| when | file | edit | why |
| --- | --- | --- | --- |
| 2026-09-05 (attempt 1) | all five | written by hand from the pinned source | see the table above |
| 2026-09-05 (attempt 1) | `agent.json` | `temperature` declared under `params.extra_body` | upshift could not put a top-level sampling param on the wire; workaround, §3.1 |
| 2026-09-06 (attempt 2) | `agent.json` | `temperature` moved back to a plain `params` key | product fix `a9676be`; `ADAPTER.md`'s contract. **A transport declaration only** — the wire request, the cases, `backend.py`, the prompt and `tools.json` are unchanged, §3.1 |

## 1. How the prompts were produced

`render_prompts.py` **AST-parses** `toponymy/templates.py` and lifts the jinja template
*source strings* out of `PROMPT_TEMPLATES["layer"]` without importing or executing any target
module, then renders them with jinja2 3.1.6 at its defaults — which is what
`jinja2.Template(...)` in `templates.py` uses. The render params are copied field-for-field
from `prompt_construction.topic_name_prompt`:

| render param | value here | source |
| --- | --- | --- |
| `document_type` | `sentences` | `toponymy/tests/test_toponymy.py:48` — the repo's own value for exactly these fixtures |
| `corpus_description` | `collection of sentences` | `test_toponymy.py:49` |
| `summary_kind` | `simple (1 or 2 word)` | `cluster_layer.py:214` `SUMMARY_KINDS[int(round(detail_level * 6))]` with `detail_level = 1.0`, which `toponymy.py:255-258` assigns to the **top** layer of a 2-layer fit at the repo's own `highest_detail_level=1.0` (`test_toponymy.py:51`) |
| `is_very_specific_summary` / `is_general_summary` | `False` / `False` | `prompt_construction.py:423-424`, computed from that `summary_kind` |
| `has_major_subtopics` | `True` | `prompt_construction.py:441`, `bool(major_subtopics)` |
| `cluster_subtopics.major` | the five subtopic names of the topic | `prompt_construction.py:388-390` at layer 1 over `conftest.py:249-252`'s `cluster_tree = {(1, i): [(0, i*5+j) for j in range(5)]}`, whose layer-0 names are `conftest.py:229-233`'s `subtopics` fixture |
| `cluster_subtopics.minor` / `.misc` | `[]` / `[]` | `prompt_construction.py:391-406`: at `layer_id == 1` there is no layer below the children, and `other_subtopics` is empty unless `layer_id > 1` |
| `cluster_sentences` | the topic's 25 sentences | `conftest.py:167-187` (`subtopic_objects` → `all_sentences`) grouped by `conftest.py:189-192`'s `cluster_label_vector = np.arange(5).repeat(25)` |
| `exemplar_start_delimiter` / `exemplar_end_delimiter` | `    * "` / `"\n` | `prompt_construction.py:317-318`, the defaults `cluster_layer` never overrides |

The system prompt is identical for all five cases **because it is identical in toponymy**: at
one layer, `document_type`, `corpus_description` and `summary_kind` are fixed, and only the
user rendering varies per cluster. This adapter is therefore one real layer-1 naming pass over
five clusters, not five unrelated prompts.

## 2. The request this adapter reproduces

`AnthropicNamer()` returns a `LiteLLMNamer` with `model="anthropic/claude-haiku-4-5-20251001"`,
`use_json_object=True`, `disable_system_prompts=False` (`llm_wrappers.py:1944-1956`). With
system prompts supported, `_call_llm_for_prompt` takes the system/user path
(`llm_wrappers.py:464-478`) and `_provider_kwargs` (`:1444-1468`) builds the litellm call.

| wire field | value here | source |
| --- | --- | --- |
| `model` | `claude-haiku-4-5-20251001` | `AnthropicNamer`'s default (`llm_wrappers.py:1866`); `_anthropic_model` (`:1227`) only adds the `anthropic/` routing prefix litellm strips |
| `system` | the rendered layer system template | `_call_llm_with_system_prompt:1504-1523` sends it as `{"role": "system", ...}`; litellm hoists that to the Messages `system` field |
| `messages` | one user turn | same, `{"role": "user", "content": user_prompt + self.extra_prompting}`; `extra_prompting` is `""` (no `llm_specific_instructions`) |
| `temperature` | **`0.4`** | `LLMWrapper.generate_topic_name`'s default (`llm_wrappers.py:500`), passed through `_call_llm_for_prompt`; `temperature_override` is `None` by default. **This is the field the case is about.** |
| `max_tokens` | `128` | `max_tokens_topic_name` default (`llm_wrappers.py:1870`, `:505-506`) |
| `tools` / `tool_choice` | **absent** | see §2.1 |
| no `top_p` / `top_k` / `stop` | — | `_provider_kwargs` sets none |

### 2.1 `response_format: {"type": "json_object"}` is a no-op on this path — verified

`_provider_kwargs:1465-1466` adds `response_format={"type": "json_object"}` because
`use_json_object=True`. It matters whether litellm turns that into a **forced tool call**,
because a forced `tool_choice` is a *second* thing the newer models reject and would confound
this case entirely.

It does not, for this value. Read at toponymy's own locked version (`uv.lock:2398-2399`,
`litellm 1.85.0`, wheel read from PyPI, never installed):
`litellm/llms/anthropic/chat/transformation.py:1458-1497` routes a non-4.5/4.6/4.7 model to
`map_response_format_to_anthropic_tool(...)`, which at `:1281-1300` calls
`_extract_json_schema_from_response_format` — that returns `None` unless the value carries
`response_schema` or `json_schema` (`:1238-1250`) — and **returns `None` when the schema is
`None`**. The caller then does `if _tool is None: continue`, so no tool is added, no
`tool_choice` is set, and even `json_mode` is skipped.

So toponymy's Anthropic request is a plain, tool-less Messages request whose only offending
field is `temperature`. `tools.json` is `[]` and `max_turns` is `1`.

## 3. The deliberate deviations

Each is a place where toponymy cannot be expressed exactly in the five files. Listed so a
reader can disagree.

### 3.1 `temperature` is declared plainly; upshift decides how it travels

**Current (attempt 2).** `agent.json` carries `params: {"max_tokens": 128, "temperature":
0.4}` — a plain `params` key, which is what `ADAPTER.md` now asks for:

> Declare sampling params (`temperature`, `top_p`, `top_k`) as plain `params` keys on every
> endpoint — how they TRAVEL is upshift's job: on `/v1/messages` they are moved into
> `extra_body` when the installed `anthropic` SDK no longer takes them as keywords (>= 1.1.0),
> which puts the same field on the wire and lets the API, not the client, decide.

So the adapter now states toponymy's request faithfully and says nothing about transport. The
wire is unchanged and the run records prove it: every rep of `runs/opus47-001-r2-baseline/`
shows `"request": {"extra_body": {"temperature": 0.4}, "max_tokens": 128, ...}` going out —
upshift moved it, because the SDK in the lab image (`anthropic` 1.4.0) has no such keyword
(`messages_create_accepts("temperature") is False`, checked in-container before the run).

**This is a transport declaration, not a case or backend change.** `cases/cases.json`,
`backend.py`, `system_prompt.txt` and `tools.json` are byte-identical to attempt 1; the
request that reaches `api.anthropic.com` is byte-identical too. What changed is which of
upshift and the adapter is responsible for the `extra_body` spelling.

**Previously (attempt 1), and why.** The same field was declared as
`params: {"extra_body": {"temperature": 0.4}}`, because upshift then passed sampling params
to `client.messages.create(**request)` as top-level kwargs and the installed SDK — `anthropic`
>= 1.1.0 — had removed `temperature` from that signature. A plain `params.temperature`
therefore never reached the wire: the SDK raised `TypeError: Messages.create() got an
unexpected keyword argument 'temperature'`, which the provider recorded as a synthetic 400
**on every model equally**, so the pair could not be told apart. Measured, at $0:
`runs/opus47-001-naive-params-baseline/` is that naive form against the **baseline** model at
N=5 — 0/5 cases, 25/25 reps, every one the SDK's `TypeError`, `input_tokens: 0`.

The `extra_body` spelling reached the wire but cost the case its repair: upshift's
`drop-sampling-params` candidate inspected **top-level** `params` keys only, produced no edit
against `params.extra_body.temperature`, and the loop gave up having generated zero
candidates.

Both halves are fixed in the product at `a9676be` ("anthropic: route sampling params through
extra_body so the wire decides"): `map_params` asks the installed SDK's own signature and
routes what it will not take into `extra_body` while the request is *built* (so the record
shows each param where it was really sent), and `_agent_json_remove(..., also_extra_body=True)`
removes the params from `params` **and** from `params.extra_body`. With this declaration the
repair loop generates the candidate itself and accepts it — `../REPORT.md` §4.

### 3.2 No keyphrases

`cluster_keywords` is `[]`, so the template's `{% if cluster_keywords %}` block is omitted. In
a real run those come from `KeyphraseBuilder` over the corpus (`toponymy.py`, `keyphrases.py`),
which cannot be reproduced without installing and running the target. The template guard is the
repo's own, and `topic_name_prompt` renders exactly this when `keyphrases[topic_index]` is
empty (`prompt_construction.py:412-416`). The omission shortens the prompt; it applies to both
models identically and cannot create a difference between them.

### 3.3 All 25 sentences are used as exemplars

`cluster_sentences` is the cluster's full 25 sentences. A real run selects central exemplars
first; 25 is under the `max_num_exemplars=128` cap, so the shape is right but the selection is
not. Again identical on both models.

### 3.4 Layer-0 names stand in for LLM-generated ones

`cluster_subtopics["major"]` is the fixture's own subtopic names (`Artificial Intelligence`,
`Cybersecurity`, …). In a full `fit()` these would be layer-0 names the LLM produced on the
previous pass. `conftest.py:229-233` uses the fixture names for exactly this role in the
repo's own tests, so this is the repo's stand-in, not ours.

### 3.5 One turn, no tool loop

`max_turns: 1`. toponymy makes one call per cluster and parses the text; there is no loop to
cap. Retries (`tenacity`, `stop_after_attempt(3)`, `llm_wrappers.py:491-496`) are toponymy's,
not upshift's, and are out of view here — a rep is one call, which is the unit the differ
compares.

### 3.6 Prompt-cache breakpoints

upshift marks the system block cacheable (LAB_RUNBOOK §2.1); toponymy does not. It made no
difference at all here: the system block is ~220 tokens, under Anthropic's 1024-token cache
minimum, and every rep records `cached_input_tokens: 0`. Price identical, inputs identical.

## 4. Cases and checks

Five cases, the brief's cap, one per cluster of the repo's own fixture. Deterministic checks
only; no LLM judge.

| id | cluster | subtopics fed to the model |
| --- | --- | --- |
| `name_topic_technology` | fixture topic 0 | Artificial Intelligence, Cybersecurity, Cloud Computing, Internet of Things, Blockchain Technology |
| `name_topic_health` | topic 1 | Mental Health, Nutrition, Exercise, Sleep, Public Health |
| `name_topic_environment` | topic 2 | Climate Change, Sustainability, Conservation, Renewable Energy, Pollution |
| `name_topic_education` | topic 3 | STEM Education, Higher Education, Early Childhood Education, Special Education, Online Learning |
| `name_topic_economics` | topic 4 | Macroeconomics, Microeconomics, International Trade, Economic Development, Behavioral Economics |

Two checks per case:

1. `no_api_error`.
2. `response_matches` with **toponymy's own `GET_TOPIC_NAME_REGEX`**, verbatim from
   `templates.py:14`:
   `\{\s*"topic_name":\s*.*?,\s*"topic_specificity":\s*[\w.]+\s*\}`.
   This is not a check invented for the lab: `llm_output_to_result`
   (`llm_wrappers.py:284-293`) does `re.findall(regex, llm_output, re.DOTALL)[0]` on the raw
   answer, so a response this regex does not match raises `IndexError` inside toponymy, burns
   the three `tenacity` attempts and leaves the topic unnamed. Passing it is the repo's own
   definition of a usable answer.

**No check asserts what the name says.** The output is free-text topic naming; a content match
would be invented, and the brief forbids it. The two checks above are what the repo itself
enforces and nothing more. What that costs in sensitivity is stated in `../REPORT.md` §7.

The `sim.oracle_plan` blocks exist only so the pipeline could be smoke-tested for $0 before any
money was spent (`runs/opus47-001-sim-*`, `SAFE`, machinery only, never evidence).

## 5. Install

**Declined (`--no-install`).** `install_review.txt` lists five files with sha256s — `setup.py`,
`pyproject.toml`, `doc/Makefile`, `doc/installation.rst`, `doc/requirements.txt` — and the one
command the driver would run, `pip install -e /case/workspace`. Nothing in them is unusual: a
setuptools package with ordinary registry dependencies, no custom build step, no `postinstall`,
no script hooks. It is **not unsafe; it is unnecessary.** Every field of the request under test
is determined by the four source files transcribed above, and installing would pull numba,
transformers, sentence-transformers, datasets and litellm to produce nothing this adapter uses.
Nothing from the target repository has been executed at any point in this case.
