# Attribution

Adapted agent under test: **PolicyEngine/policybench**, MIT licensed.

- repo: https://github.com/PolicyEngine/policybench
- commit: `ad1583fe6deb6ee70a8b542894edac043b9f4c1e` ("Add the tool_choice sensitivity escape
  hatch and the Claude thinking note (#138)")
- issue this case follows: https://github.com/PolicyEngine/policybench/issues/139
- license: MIT (`LICENSE` at the pinned commit)

Every prompt in `cases/cases.json` and the whole of `tools.json` are byte-for-byte the output
of policybench's own request builder
(`policybench/eval_no_tools.py::_chat_completion_request_kwargs`) at that commit, executed
inside the lab container by `../home/gen/gen_requests.py`. No prompt text was written by hand.
