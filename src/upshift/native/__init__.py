"""Native runner: upshift drives the application's own entry point (DESIGN.md §C).

`protocol` is the wire contract (and the only module a re-implementation in another language
needs to read), `exec` runs the command safely, `runner` turns one execution into an
`EpisodeResult`, `cli` is the flag surface and the authorization gate.
"""

from upshift.native.protocol import (
    CONTINUATION_EXHAUSTED,
    NON_BEHAVIOURAL_ERROR_TYPES,
    PROTOCOL_VERSION,
    RUNNER_ERROR,
    SOURCE_LIVE_MODEL,
    SOURCE_NATIVE_APPLICATION,
    SOURCE_RECORDED_PLAYBACK,
    RunnerSpec,
)

__all__ = [
    "CONTINUATION_EXHAUSTED",
    "NON_BEHAVIOURAL_ERROR_TYPES",
    "PROTOCOL_VERSION",
    "RUNNER_ERROR",
    "SOURCE_LIVE_MODEL",
    "SOURCE_NATIVE_APPLICATION",
    "SOURCE_RECORDED_PLAYBACK",
    "RunnerSpec",
]
