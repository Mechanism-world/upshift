"""Security regressions: hostile CLI arguments, hostile target repositories, hostile models.

Every test here pins a guard that a specific attack would otherwise walk through. No test
makes a network call, spawns git or docker, or reads an API key.

The threats, in the order they appear:

1. `upshift adapt <source>` where the source is an option, not a URL (argument injection into
   `git clone`).
2. A cloned repository that symlinks out of itself, trying to get `~/.ssh/id_rsa` into the
   evidence that is sent to the model.
3. A repository whose text steers the extraction model into writing Python out of a
   docstring in the `backend.py` that `upshift upgrade` later imports and runs.
4. A run id or case id that is a path, trying to make upshift write (and `rmtree`) outside
   its runs root.
5. A shell command that tries to take the host down with it, rather than the container.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upshift import recorder
from upshift.adapt import inventory
from upshift.adapt.generate import build_backend, docstring_safe

# ---------------------------------------------------------------------------
# 1. git clone argument injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "--upload-pack=touch /tmp/pwned/x.git",
        "--separate-git-dir=/tmp/elsewhere/x.git",
        "--template=/tmp/hooks/x.git",
        "-u/tmp/evil/x.git",
        "--config=core.fsmonitor=id/x.git",
    ],
)
def test_option_shaped_source_is_refused_before_git_runs(source):
    """These all satisfy `is_git_url` (they end in .git and contain a '/'), so without the
    check they would reach `git clone` as argv and be parsed as git's own flags."""
    assert inventory.is_git_url(source)  # this is exactly why the check has to exist
    with pytest.raises(ValueError, match="would be read by git as an option"):
        inventory.check_clone_url(source)


def test_resolve_source_refuses_option_shaped_source(tmp_path):
    def never_called(url, dest):  # pragma: no cover - the point is that it is not called
        raise AssertionError(f"clone must not be attempted for {url!r}")

    with pytest.raises(ValueError, match="would be read by git as an option"):
        inventory.resolve_source(
            "--upload-pack=evil/x.git", tmp_path, clone_fn=never_called
        )


@pytest.mark.parametrize(
    "url",
    ["https://github.com/o/r", "git@github.com:o/r.git", "ssh://git@h/o/r.git",
     "git://h/o/r.git", "https://example.com/o/r.git"],
)
def test_real_clone_urls_still_pass(url):
    inventory.check_clone_url(url)


def test_clone_url_with_newline_is_refused():
    with pytest.raises(ValueError, match="control character"):
        inventory.check_clone_url("https://example.com/r.git\n--upload-pack=x")


def test_clone_argv_terminates_option_parsing(monkeypatch, tmp_path):
    """Second guard: even for an allowed URL, `--` must separate options from operands."""
    seen: dict[str, list[str]] = {}

    class Result:
        returncode = 0
        stdout = "deadbeef\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen.setdefault("argv", list(argv))
        return Result()

    monkeypatch.setattr(inventory.subprocess, "run", fake_run)
    inventory._default_clone("https://example.com/o/r.git", tmp_path / "clone")
    argv = seen["argv"]
    assert argv[:5] == ["git", "clone", "--depth", "1", "--"]
    assert argv[5] == "https://example.com/o/r.git"


def test_only_clone_and_rev_parse_are_ever_run():
    """No `git submodule`, no `git checkout`: nothing that would run a hostile repo's hooks
    or fetch its submodules. `clone --depth 1` does neither."""
    source = (ROOT / "src" / "upshift" / "adapt" / "inventory.py").read_text()
    tree = ast.parse(source)
    subcommands = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "_git" or not node.args:
            continue
        arg = node.args[0]
        assert isinstance(arg, ast.List) and isinstance(arg.elts[0], ast.Constant)
        subcommands.add(arg.elts[0].value)
    assert subcommands == {"clone", "rev-parse"}


# ---------------------------------------------------------------------------
# 2. symlinks out of the target repository
# ---------------------------------------------------------------------------


def _secret(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "id_rsa"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nSUPERSECRET\n")
    return secret


def test_walk_repo_skips_a_symlink_pointing_outside_the_repo(tmp_path):
    secret = _secret(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.py").write_text("import openai\nclient.chat.completions.create()\n")
    (repo / "config.py").symlink_to(secret)

    found = [p.name for p in inventory.walk_repo(repo)]
    assert found == ["agent.py"]


def test_walk_repo_skips_files_under_a_symlinked_directory(tmp_path):
    secret = _secret(tmp_path)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "agent.py").write_text("import openai\n")
    (repo / "vendored").symlink_to(secret.parent, target_is_directory=True)

    for path in inventory.walk_repo(repo):
        assert "id_rsa" not in path.name


def test_walk_repo_keeps_a_symlink_that_stays_inside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "real.py").write_text("import openai\n")
    (repo / "alias.py").symlink_to(repo / "pkg" / "real.py")

    assert sorted(p.name for p in inventory.walk_repo(repo)) == ["alias.py", "real.py"]


def test_evidence_never_contains_the_escaping_symlink_target(tmp_path):
    secret = _secret(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.py").write_text(
        "import openai\n"
        "client.chat.completions.create(model='gpt-5.5', messages=messages, tools=TOOLS)\n"
    )
    (repo / "prompt.py").symlink_to(secret)

    source = inventory.SourceRepo(root=repo, origin=str(repo), commit=None, is_clone=True)
    evidence = inventory.render_evidence(inventory.take_inventory(source))
    assert "SUPERSECRET" not in evidence


def test_pointer_cannot_reach_outside_the_repo(tmp_path):
    """The round-2 path is model output; it was already guarded, and stays guarded."""
    secret = _secret(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.py").write_text("import openai\n")
    from upshift.adapt.extract import resolve_pointer_path

    assert resolve_pointer_path(repo, "agent.py") == "agent.py"
    assert resolve_pointer_path(repo, "../outside/id_rsa") is None
    assert resolve_pointer_path(repo, str(secret)) is None


# ---------------------------------------------------------------------------
# 3. code injection into the generated backend.py
# ---------------------------------------------------------------------------

BREAKOUT = '"""\nimport os\nos.system("touch /tmp/pwned")\n"""'


def _generated(origin="https://example.com/r.git", commit=None, tools=None):
    source, _implemented, _stubs, _prov = build_backend(
        {"tools": tools or []}, origin, commit
    )
    return source


def test_origin_cannot_escape_the_generated_docstring():
    source = _generated(origin=BREAKOUT)
    ast.parse(source)  # would raise, or silently execute, if the docstring were closed
    assert "os.system" not in _executable_code(source)


def test_a_hostile_citation_cannot_escape_the_generated_docstring():
    tools = [
        {
            "name": "search",
            "citation": 'a.py:1\n"""\nimport os\nos.system("touch /tmp/pwned")\n"""',
            "backend": {"kind": "lookup", "citation": "a.py:1", "match_fields": ["q"]},
        }
    ]
    source = _generated(tools=tools)
    ast.parse(source)
    assert "os.system" not in _executable_code(source)


def test_a_hostile_tool_name_cannot_escape_the_tool_specs_literal():
    tools = [
        {
            "name": '", "x": __import__("os").system("touch /tmp/pwned"), "y": "',
            "citation": "a.py:1",
            "backend": {"kind": "list", "citation": "a.py:1"},
        }
    ]
    source = _generated(tools=tools)
    ast.parse(source)
    specs = _tool_specs(source)
    # the hostile name survived as a dict KEY — data, not code
    assert list(specs) == ['", "x": __import__("os").system("touch /tmp/pwned"), "y": "']


def test_a_placeholder_in_the_origin_is_not_re_expanded():
    source = _generated(origin="__TOOL_SPECS__")
    assert "__TOOL_SPECS__" not in source.split('"""')[1]


def test_generated_backend_still_works_after_sanitising():
    tools = [
        {
            "name": "list_orders",
            "citation": "a.py:1",
            "backend": {"kind": "list", "citation": "a.py:1", "state_key": "orders"},
        }
    ]
    source = _generated(tools=tools)
    namespace: dict = {}
    exec(compile(source, "backend.py", "exec"), namespace)  # noqa: S102 - our own output
    backend = namespace["create_backend"]({"orders": [{"id": "o1"}]})
    assert backend.execute("list_orders", {}) == {"results": [{"id": "o1"}]}


def test_docstring_safe_leaves_ordinary_text_alone():
    assert docstring_safe("https://github.com/o/r.git") == "https://github.com/o/r.git"
    assert docstring_safe("tool <- src/agent.py:12-40  [lookup] REVIEW") == (
        "tool <- src/agent.py:12-40  [lookup] REVIEW"
    )


def _tool_specs(source: str) -> dict:
    """The generated TOOL_SPECS, read as a literal. `literal_eval` refuses anything that is
    not a plain literal, so this passing is itself the proof that nothing executable got in."""
    for node in ast.parse(source).body:
        target = getattr(node, "target", None)
        if isinstance(node, ast.AnnAssign) and getattr(target, "id", "") == "TOOL_SPECS":
            return ast.literal_eval(node.value)
    raise AssertionError("no TOOL_SPECS assignment in the generated backend")


def _executable_code(source: str) -> str:
    """`source` with its module docstring removed — what actually runs on import."""
    tree = ast.parse(source)
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


# ---------------------------------------------------------------------------
# 4. run ids and case ids are directory names, not paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["../evil", "..", "a/b", "/abs", "x/../../y", ".hidden", "", "a\x00b"]
)
def test_run_dir_refuses_a_run_id_that_is_a_path(bad):
    with pytest.raises(ValueError, match="invalid run id"):
        recorder.run_dir("runs", bad)


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "/abs"])
def test_rep_path_refuses_a_case_id_that_is_a_path(bad, tmp_path):
    with pytest.raises(ValueError, match="invalid case id"):
        recorder.rep_path(tmp_path, bad, 1)


def test_ordinary_ids_are_untouched(tmp_path):
    assert recorder.run_dir("runs", "demo-baseline").name == "demo-baseline"
    assert recorder.rep_path(tmp_path, "todo_add_one", 3).parent.name == "todo_add_one"


def test_every_shipped_case_id_is_a_safe_component():
    from upshift.schemas import Case

    for cases_file in sorted(ROOT.glob("agents/*/cases/cases.json")) + [
        ROOT / "src" / "upshift" / "example_agent" / "cases" / "cases.json"
    ]:
        for case in Case.load_all(cases_file):
            recorder.safe_component(case.id, "case id")


# ---------------------------------------------------------------------------
# 5. the shell_gpt sandbox
# ---------------------------------------------------------------------------


def _shell_backend_module():
    path = ROOT / "agents" / "shell_gpt" / "backend.py"
    spec = importlib.util.spec_from_file_location("upshift_shellbox_security", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_container_argv_is_bounded_and_never_a_shell_string(monkeypatch, tmp_path):
    module = _shell_backend_module()
    monkeypatch.setattr(module, "WORKROOT", tmp_path / "work")
    seen: dict = {}

    class Completed:
        returncode = 0
        stdout = b""

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    backend = module.create_backend({"files": {"a.txt": "hi\n"}})
    backend.execute("execute_shell_command", {"shell_command": "cat a.txt; rm -rf /"})

    argv, kwargs = seen["argv"], seen["kwargs"]
    assert kwargs.get("shell") is not True
    assert kwargs["timeout"] == module.TIMEOUT_S
    # the model's command is one argv element, after `bash -c` — it can never become a
    # docker flag however it is written
    assert argv[-3:] == ["bash", "-c", "cat a.txt; rm -rf /"]
    for flag in ("--rm", "--network", "--pids-limit", "--memory", "--security-opt"):
        assert flag in argv, f"{flag} missing from the sandbox argv"
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == module.MEMORY_LIMIT
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    # the only host path mounted is this episode's throwaway tree
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert mounts == [f"{backend.root}:/work"]
    assert str(backend.root).startswith(str(tmp_path))


def test_no_host_fallback_when_docker_is_missing(monkeypatch, tmp_path):
    module = _shell_backend_module()
    monkeypatch.setattr(module, "WORKROOT", tmp_path / "work")

    def no_docker(argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(module.subprocess, "run", no_docker)
    backend = module.create_backend({})
    result = backend.execute("execute_shell_command", {"shell_command": "echo hi"})
    assert "sandbox unavailable" in result["error"]
    assert "output" not in result


@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/passwd", "a/../../b"])
def test_initial_state_cannot_write_outside_the_episode_tree(monkeypatch, tmp_path, bad):
    module = _shell_backend_module()
    monkeypatch.setattr(module, "WORKROOT", tmp_path / "work")
    with pytest.raises(ValueError, match="must stay inside the tree"):
        module.create_backend({"files": {bad: "x"}})


# ---------------------------------------------------------------------------
# 6. hostile literals in a target repo must not crash the walk
# ---------------------------------------------------------------------------


def test_a_huge_int_literal_does_not_abort_the_analysis(tmp_path):
    """`repr()` of a big enough int raises ValueError (CPython's digit limit). It is a
    keyword argument in a file we merely read, so it must not end the run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.py").write_text(
        "import openai\n"
        "client.chat.completions.create(model=0x" + "f" * 20_000 + ", messages=m)\n"
    )
    source = inventory.SourceRepo(root=repo, origin=str(repo), commit=None, is_clone=False)
    got = inventory.take_inventory(source)
    assert got.files
    for call in got.call_sites:
        for value in call.kwargs.values():
            assert len(value) <= inventory.MAX_KWARG_CHARS + len("…<truncated>")


# ---------------------------------------------------------------------------
# 7. secrets never reach disk or a log line
# ---------------------------------------------------------------------------


def test_dotenv_loader_never_overrides_the_environment_and_returns_nothing(
    monkeypatch, tmp_path
):
    from upshift.cli import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "OPENAI_API_KEY=sk-from-file\n"
        "ANTHROPIC_API_KEY=sk-ant-a=b=c\n"
        "MALFORMED\n"
        "\n"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _load_dotenv(env_file) is None
    assert __import__("os").environ["OPENAI_API_KEY"] == "sk-from-env"  # environment wins
    assert __import__("os").environ["ANTHROPIC_API_KEY"] == "sk-ant-a=b=c"  # '=' in value


def test_api_call_record_has_no_room_for_a_header():
    """An `APICall` is {endpoint, request, response, error}. The key lives on the SDK client
    object, so a run record structurally cannot carry it."""
    import dataclasses

    from upshift.schemas import APICall

    assert [f.name for f in dataclasses.fields(APICall)] == [
        "endpoint",
        "request",
        "response",
        "error",
    ]


@pytest.mark.parametrize(
    "rel", ["openai_provider.py", "anthropic_provider.py", "openai_batch.py"]
)
def test_providers_only_read_keys_into_the_client_constructor(rel):
    """Every read of `api_key` is either the environment lookup, the `if not api_key` guard,
    or the SDK client's own `api_key=` kwarg. Nothing puts it in a body, a header dict, a
    format string or a log line."""
    source = (ROOT / "src" / "upshift" / "providers" / rel).read_text()
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    reads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "api_key" and isinstance(n.ctx, ast.Load)
    ]
    assert reads, f"{rel} does not read an api_key at all"
    for name in reads:
        parent = parents[name]
        if isinstance(parent, ast.keyword):
            assert parent.arg == "api_key", f"{rel}: api_key passed as {parent.arg!r}"
        elif isinstance(parent, ast.Dict):
            # the client kwargs dict, expanded into the SDK constructor as `**kwargs`
            key = parent.keys[parent.values.index(name)]
            assert isinstance(key, ast.Constant) and key.value == "api_key", (
                f"{rel}: api_key stored under {getattr(key, 'value', key)!r}"
            )
        else:
            assert isinstance(parent, ast.UnaryOp | ast.If | ast.Assign | ast.Compare), (
                f"{rel}: api_key reaches a {type(parent).__name__}"
            )

    writes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "api_key" and isinstance(n.ctx, ast.Store)
    ]
    for name in writes:
        assign = parents[name]
        assert isinstance(assign, ast.Assign)
        call = assign.value
        assert isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "get", (
            f"{rel}: api_key is assigned from something other than os.environ.get"
        )


def test_cli_refuses_a_tag_that_is_a_path(tmp_path, monkeypatch):
    """The guard has to fire before the baseline run, not after it has written records."""
    from upshift import cli

    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "demo"]) == 0
    runs = tmp_path / "runs"
    assert cli.main(
        [
            "upgrade", "--agent", "demo", "--provider", "sim",
            "--baseline-model", "sim-5.5", "--candidate-model", "sim-5.6-sol",
            "--tag", "../escape", "--n", "1", "--quiet",
        ]
    ) == 2
    assert cli.main(
        ["run", "--agent", "demo", "--provider", "sim", "--model", "sim-5.5",
         "--run-id", "a/b", "--n", "1"]
    ) == 2
    assert not runs.exists()
    assert not (tmp_path.parent / "escape-baseline").exists()
