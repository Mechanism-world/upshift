"""Deterministic in-memory port of plastiq's agent tool surface (A-085).

Ported by hand from LayerDynamics/plastiq @ 7f84779f:
  apps/plastiq/src/ai/tools/toolDefs.ts   (buildAgentTools: the four handlers)
  apps/plastiq/src/ai/tools/buildPart.ts  (validate -> convert -> probe -> apply)
  apps/plastiq/src/ai/tools/schema.ts     (authoringDocumentSchema, the zod contract)
  apps/plastiq/src/ai/planning.ts         (validatePlan / summarizePlan)
  apps/plastiq/src/ai/tools/inspectGeometry.ts (the empty-geometry answer)
  apps/plastiq/src/ai/prompt.ts:45-47     (the sketch-before-extrude/revolve/cut rule)

plastiq's real build probe is OCCT in a worker; upshift requires a deterministic,
in-memory backend (ADAPTER.md rule 3), so the probe here is the STRUCTURAL part of
what OCCT enforces: the zod schema, the "assembly" guard, and the documented rule
that extrude/revolve/cut consume the sketch immediately before them. Geometry is
never evaluated. See ADAPT_EDITS.md for every deviation.
"""

FEATURE_TYPES = [
    "box", "sketch", "extrude", "revolve", "loft", "sweep", "cut", "fillet",
    "chamfer", "shell", "draft", "transform", "mirror", "linearPattern",
    "circularPattern", "boolean", "importStep", "placement",
]

# Required `params` keys per feature type (schema.ts:72-119, the non-optional ones).
REQUIRED_PARAMS = {
    "box": ["dx", "dy", "dz"],
    "extrude": ["height"],
    "revolve": ["angle"],
    "cut": ["depth"],
    "fillet": ["radius"],
    "chamfer": ["distance"],
    "shell": ["thickness"],
    "draft": ["angle"],
    "linearPattern": ["spacing", "count"],
    "circularPattern": ["count"],
}
# Required `data` keys per feature type (schema.ts:72-119).
REQUIRED_DATA = {
    "sketch": ["profile"],
    "draft": ["face"],
    "loft": ["sections"],
    "sweep": ["profile", "path"],
    "importStep": ["step"],
}
# prompt.ts:45-47 — these consume the MOST RECENT sketch and must be immediately
# preceded by one; without it the feature "FAILS to build".
NEEDS_SKETCH = ("extrude", "revolve", "cut")


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class Backend:
    def __init__(self, initial_state):
        doc = (initial_state or {}).get("document") or {"features": [], "params": {}}
        self._current = doc
        self._applied = None
        self._applied_count = 0
        self._violations = []
        self._build_results = []
        self._plans = []
        self._final_message = ""

    # ── build_part (buildPart.ts) ──────────────────────────────────────────────
    def _validate(self, document):
        """Return a list of violations: schema issues first, then the build rule."""
        v = []
        if not isinstance(document, dict):
            return [{"rule": "schema", "detail": "the document is not an object"}]

        stray = document.get("assembly")
        if isinstance(stray, dict) and isinstance(stray.get("features"), list) and stray["features"]:
            v.append({"rule": "stray_assembly_features",
                      "detail": '%d feature(s) were under "assembly" and would be ignored'
                                % len(stray["features"])})

        feats = document.get("features")
        if not isinstance(feats, list):
            v.append({"rule": "schema", "detail": "features: expected an array"})
            feats = []
        params = document.get("params")
        if not isinstance(params, dict) or any(not _is_num(x) for x in params.values()):
            v.append({"rule": "schema", "detail": "params: expected a record of numbers"})

        prev_type = None
        for i, f in enumerate(feats):
            where = "features.%d" % i
            if not isinstance(f, dict):
                v.append({"rule": "schema", "detail": "%s: expected an object" % where})
                prev_type = None
                continue
            fid = f.get("id")
            ftype = f.get("type")
            if not isinstance(fid, str) or not fid:
                v.append({"rule": "schema", "detail": "%s.id: expected a non-empty string" % where})
            if ftype not in FEATURE_TYPES:
                v.append({"rule": "schema",
                          "detail": "%s.type: %r is not one of the supported feature types" % (where, ftype)})
                prev_type = None
                continue
            fparams = f.get("params") if isinstance(f.get("params"), dict) else {}
            for key in REQUIRED_PARAMS.get(ftype, []):
                if not _is_num(fparams.get(key)):
                    v.append({"rule": "schema", "detail": "%s.params.%s: expected a number" % (where, key)})
            fdata = f.get("data") if isinstance(f.get("data"), dict) else {}
            for key in REQUIRED_DATA.get(ftype, []):
                if key not in fdata:
                    v.append({"rule": "schema", "detail": "%s.data.%s is required" % (where, key)})
            if ftype in NEEDS_SKETCH and prev_type != "sketch":
                v.append({"rule": "no_sketch_upstream",
                          "detail": "feature '%s' (%s): no sketch profile upstream"
                                    % (fid if isinstance(fid, str) else where, ftype)})
            prev_type = ftype
        return v

    def _build_part(self, arguments):
        document = (arguments or {}).get("document")
        violations = self._validate(document)
        self._violations = violations
        if violations:
            stray = [x for x in violations if x["rule"] == "stray_assembly_features"]
            nosketch = [x for x in violations if x["rule"] == "no_sketch_upstream"]
            if stray:
                message = 'Features must go in the top-level "features" array, not "assembly".'
            elif nosketch:
                message = "The model did not compile."
            else:
                message = "The document did not match the build_part schema."
            errors = "; ".join(x["detail"] for x in violations)
            self._build_results.append({"status": "error", "message": message})
            return {"result": "%s Errors: %s" % (message, errors), "isError": True}

        self._applied = document
        self._current = document
        self._applied_count += 1
        n = len(document["features"])
        message = "Built the part (%d feature%s)." % (n, "" if n == 1 else "s")
        self._build_results.append({"status": "ok", "message": message})
        return {"result": message, "isError": False}

    # ── plan_part (planning.ts) ────────────────────────────────────────────────
    def _plan_part(self, arguments):
        plan = arguments if isinstance(arguments, dict) else {}
        nodes = plan.get("nodes")
        relations = plan.get("relations") if isinstance(plan.get("relations"), list) else []
        if not isinstance(nodes, list) or not nodes:
            return {"result": "Plan rejected: nodes: expected at least 1 element", "isError": True}
        ids = []
        for n in nodes:
            if not isinstance(n, dict) or not isinstance(n.get("id"), str) or not n["id"]:
                return {"result": "Plan rejected: nodes: id: expected a non-empty string", "isError": True}
            if not isinstance(n.get("part"), str) or not n["part"]:
                return {"result": "Plan rejected: nodes: part: expected a non-empty string", "isError": True}
            if n["id"] in ids:
                return {"result": 'Plan rejected: duplicate node id "%s"' % n["id"], "isError": True}
            ids.append(n["id"])
        for n in nodes:
            parent = n.get("parent")
            if parent is not None and parent not in ids:
                return {"result": 'Plan rejected: node "%s" has an unknown parent "%s"' % (n["id"], parent),
                        "isError": True}
        for r in relations:
            if not isinstance(r, dict) or r.get("from") not in ids:
                return {"result": 'Plan rejected: relation references an unknown node "%s"'
                                  % (r.get("from") if isinstance(r, dict) else r), "isError": True}
            if r.get("to") not in ids:
                return {"result": 'Plan rejected: relation references an unknown node "%s"' % r.get("to"),
                        "isError": True}
        # acyclic parent hierarchy
        parent_of = {n["id"]: n.get("parent") for n in nodes}
        for start in ids:
            seen, cur = set(), start
            while cur is not None:
                if cur in seen:
                    return {"result": 'Plan rejected: cycle in the parent hierarchy at "%s"' % cur,
                            "isError": True}
                seen.add(cur)
                cur = parent_of.get(cur)
        roots = [n["id"] for n in nodes if not n.get("parent")]
        self._plans.append({"nodes": len(nodes), "relations": len(relations)})
        return {"result": "plan accepted: %d node(s), %d relation(s); roots: %s"
                          % (len(nodes), len(relations), ", ".join(roots) or "—"),
                "isError": False}

    # ── the tool dispatch (toolDefs.ts buildAgentTools) ────────────────────────
    def execute(self, name, arguments):
        try:
            if name == "build_part":
                return self._build_part(arguments)
            if name == "plan_part":
                return self._plan_part(arguments)
            if name == "inspect_geometry":
                # inspectGeometry.ts:150-153 — no built geometry (this port has no kernel).
                return {"result": "There is no built geometry to inspect yet.", "isError": False}
            if name == "answer_user":
                msg = (arguments or {}).get("message")
                self._final_message = msg if isinstance(msg, str) else "Done."
                return {"result": self._final_message, "isError": False}
            return {"error": "No such tool: %s" % name}
        except Exception as exc:  # never raises (ADAPTER.md rule 2)
            return {"error": "%s: %s" % (type(exc).__name__, exc)}

    def state(self):
        feats = self._applied["features"] if isinstance(self._applied, dict) else []
        types = [f.get("type") for f in feats if isinstance(f, dict)]
        dims = ""
        if types and types[0] == "box":
            p = feats[0].get("params") if isinstance(feats[0].get("params"), dict) else {}
            vals = [p.get("dx"), p.get("dy"), p.get("dz")]
            if all(_is_num(x) for x in vals):
                dims = ",".join("%g" % round(float(x), 3) for x in sorted(float(y) for y in vals))
        return {
            "applied_document": self._applied,
            "applied_count": self._applied_count,
            "feature_types": types,
            "first_feature_type": types[0] if types else "",
            "box_dims_mm": dims,
            "violations": self._violations,
            "build_results": self._build_results,
            "plans": self._plans,
            "final_message": self._final_message,
        }


def create_backend(initial_state):
    return Backend(initial_state)
