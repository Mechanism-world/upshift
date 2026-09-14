# Pre-registration — written by the coordinator BEFORE any arm runs
Date: 2026-09-14. Purpose: stop me from rationalising whatever comes out.

## What I already know that biases this experiment
1. A near-identical A/B (cymoo/lovia, gpt-5.5 -> gpt-6-astra, 2026-09-11) returned TIE.
   In that run Upshift produced no run, no verdict and no flag, because `capture` has no
   OpenAI path and `adapt` mis-ranked files and emitted an empty eval suite.
2. The 2026-09-08 Astra validation (shell_gpt) concluded in our OWN records that
   "Upshift did not find the fix — the docs and the 400 say it. It found the confidence."
3. Astra's hard breakages are DOCUMENTED and produce loud 400s. A competent engineer
   reading the migration guide finds them. They are not where a measurement tool earns
   its keep.

## Therefore: the honest discriminating question
Not "does Astra break this app" (docs answer that) but:

**Does Astra's documented BEHAVIOURAL drift — asks clarifying questions more often,
answers more verbosely and with more structure, different tool-call cadence — silently
degrade this application in a way its own tests and the docs do NOT reveal, and does
Upshift's N-rep measurement surface that where normal engineering does not?**

If the answer is no, Upshift is redundant on this class of migration, and I must say so.

## Pre-registered predictions (I commit to these now)
- P1: Both arms will find the endpoint break (`/v1/chat/completions` + tools -> 400) and
  fix it by routing to `/v1/responses`. Confidence: high.
- P2: Both arms will find the removed sampling params. Confidence: high.
- P3: At least one arm will follow the 400's own advice (`reasoning_effort: 'none'`) into
  a second 400 before finding the real fix. Confidence: medium. Upshift has a guard for
  exactly this (MODELS_WITHOUT_EFFORT_NONE), so if Arm A hits it and Arm B does not,
  that is a REAL, specific, attributable Upshift contribution.
- P4: Upshift's OpenAI path will require significant manual integration (no capture for
  OpenAI; native runner has no repair loop; adapter must be hand-written or
  model-extracted). Arm B pays a setup tax. Confidence: high.
- P5: Behavioural drift, if present, will show as non-determinism across reps rather than
  as a hard failure — which is precisely what single-shot test suites miss and N-rep
  measurement catches. Whether it is PRESENT in the chosen app is unknown. Confidence in
  the mechanism: high. Confidence it occurs here: unknown — this is the real experiment.

## What would count as a genuine Upshift win
- It surfaces a confirmed defect Arm A shipped without noticing (classification B or E), OR
- it prevents a bad migration by measuring collateral damage Arm A's tests missed, OR
- it materially cuts time/work with evidence in elapsed minutes.
Anything less than one of these is at most MODEST WIN, and probably TIE.

## What would count as a genuine loss
- Arm B spends materially more time and money and reaches the same or a worse patch.
- Upshift's product bugs block its own use on a mainstream OpenAI app.

Note for the founder's decision: "Upshift cannot yet be used here" and "Upshift is not
needed here" lead to the same Friday conclusion unless there is a credible, short fix
path. I will distinguish them in the report but will not treat the first as a reprieve.
