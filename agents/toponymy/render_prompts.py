"""Render toponymy's own layer prompts for the opus47-001 adapter.

Mechanical: the jinja template SOURCE is lifted out of toponymy/templates.py by AST
(no target module is imported or executed); the render params are toponymy's own
`topic_name_prompt` render_params dict; the data is toponymy's own committed test
fixture subtopic_objects.json + conftest.py's cluster_tree.
"""
import ast, json, pathlib, sys
import jinja2

WS = pathlib.Path(sys.argv[1])          # .../workspace
OUT = pathlib.Path(sys.argv[2])         # .../agent

def template_sources(path):
    """PROMPT_TEMPLATES['layer'] -> {rendering: template source string}, via AST."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PROMPT_TEMPLATES" for t in node.targets
        ):
            d = node.value
            for k, v in zip(d.keys, d.values):
                if getattr(k, "value", None) == "layer":
                    out = {}
                    for kk, vv in zip(v.keys, v.values):
                        key = getattr(kk, "value", None)
                        if key in ("system", "user", "combined"):
                            # jinja2.Template("<literal>")
                            out[key] = vv.args[0].value
                    return out
    raise SystemExit("PROMPT_TEMPLATES['layer'] not found")

src = template_sources(WS / "toponymy" / "templates.py")
env_sys = jinja2.Template(src["system"])
env_usr = jinja2.Template(src["user"])

fixture = json.loads((WS / "toponymy" / "tests" / "subtopic_objects.json").read_text())

# toponymy/toponymy.py: detail_levels = linspace(lowest, highest, n_layers); layer i -> [i]
# toponymy/tests/test_toponymy.py: lowest_detail_level=0.8, highest_detail_level=1.0
# toponymy/cluster_layer.py:214  summary_level = int(round(detail_level * (len(SUMMARY_KINDS)-1)))
SUMMARY_KINDS = [
    "domain expert level (8 to 15 word)",
    "very specific and detailed (6 to 12 word)",
    "specific and detailed (4 to 8 word)",
    "clear and concise (3 to 6 word)",
    "focussed and brief (2 to 5 word)",
    "essential and core (1 to 4 word)",
    "simple (1 or 2 word)",
]
DETAIL_LEVEL = 1.0                                   # top layer (layer 1) of a 2-layer fit
summary_kind = SUMMARY_KINDS[int(round(DETAIL_LEVEL * (len(SUMMARY_KINDS) - 1)))]

OBJECT_DESCRIPTION = "sentences"                     # test_toponymy.py:48
CORPUS_DESCRIPTION = "collection of sentences"       # test_toponymy.py:49

common = dict(
    document_type=OBJECT_DESCRIPTION,
    corpus_description=CORPUS_DESCRIPTION,
    summary_kind=summary_kind,
    is_very_specific_summary="very specific" in summary_kind,
    is_general_summary="general" in summary_kind,
    has_major_subtopics=True,
    exemplar_start_delimiter='    * "',              # prompt_construction.py:317
    exemplar_end_delimiter='"\n',                    # prompt_construction.py:318
)

system_prompt = env_sys.render(
    cluster_keywords=[],
    cluster_subtopics={"major": [], "minor": [], "misc": []},
    cluster_sentences=[],
    **common,
)

cases = []
for topic in fixture:
    subtopic_names = [s["subtopic"] for s in topic["subtopics"]]           # conftest.py:236
    sentences = [x for s in topic["subtopics"] for x in s["sentences"]]    # conftest.py:180
    user = env_usr.render(
        cluster_keywords=[],
        cluster_subtopics={"major": subtopic_names, "minor": [], "misc": []},
        cluster_sentences=sentences,
        **common,
    )
    slug = "name_topic_" + topic["topic"].lower()
    cases.append({
        "id": slug,
        "description": (
            f"Name the layer-1 cluster whose five layer-0 subtopics are "
            f"{', '.join(subtopic_names)} (toponymy/tests/subtopic_objects.json, "
            f"cluster {len(cases)} of conftest.py's cluster_tree)."
        ),
        "initial_state": {},
        "user_messages": [user],
        "checks": [
            {"type": "no_api_error"},
            # toponymy/templates.py GET_TOPIC_NAME_REGEX, verbatim. llm_wrappers.py:285
            # does re.findall(regex, output, re.DOTALL)[0]; no match -> IndexError -> retry
            # exhaustion -> the topic is left unnamed.
            {"type": "response_matches",
             "regex": r'\{\s*"topic_name":\s*.*?,\s*"topic_specificity":\s*[\w.]+\s*\}'},
        ],
        "sim": {"oracle_plan": [
            {"final_message": '{"topic_name":"%s","topic_specificity":0.8}' % topic["topic"]}
        ]},
    })

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "system_prompt.txt").write_text(system_prompt)
(OUT / "cases").mkdir(exist_ok=True)
(OUT / "cases" / "cases.json").write_text(json.dumps(cases, indent=2) + "\n")
print("system prompt chars:", len(system_prompt))
for c in cases:
    print(c["id"], "user chars:", len(c["user_messages"][0]))
