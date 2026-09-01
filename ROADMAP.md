# Roadmap

Things deliberately out of scope for v0.3.0, one line of rationale each. Nothing here is
promised; each item is listed because it was tempting and we said no for now.

- **Google (Gemini) provider** — a third wire format before the second has real-agent
  evidence would spread the statistical machinery thin; providers are added only with a
  documented migration to test.
- **Local / open-weight models (Ollama, vLLM)** — no version-migration story to verify
  yet; the product is upgrade safety, not model hosting.
- **Framework integrations (LangChain, crewAI, pydantic-ai, ...)** — SCOPE.md: plain API
  agents only; `upshift adapt` reads through frameworks where extraction is honest, and
  that is the boundary.
- **UI / dashboard** — the verdict, the patch, and the committed records are the product;
  a UI would be a second surface to keep honest.
- **Hosted service / SaaS** — "nothing leaves your machine" is a feature, not a phase.
- **Cross-file identifier chasing in `adapt`** — needed for schemas defined far from their
  registration site (ChatDBG); a real capability, deferred until the two-round extraction
  has more live mileage.
- **Backend determinism detection** — cheap guard (replay one case twice, diff final state)
  worth adding once a nondeterministic backend is seen in the wild.
