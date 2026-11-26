"""The attention item for a ready run that has requests pending and shows no progress.

A hung NCCL collective or a wedged engine keeps passing readiness while serving
nothing. This reads the engine's own readings (``EngineWindow``) over a window
and says so. It is advice: nothing is restarted, and a run whose engine format
has no reading parser gives no verdict.

The test is a change test, never a threshold: the KV ratio of a short request
is a fraction of a percent. A run is not progressing only when all of these hold:

- Coverage: the readings reach back to the start of the window, within a minute
  of slack, so a run that only just started reporting is never judged.
- Pending: requests are running or waiting in every reading, so an idle run is
  never judged.
- Flat: none of the progress fields (the token counters, the end-to-end request
  count and the KV cache ratio) changed. TensorFold and SGLang count tokens of
  finished requests only, so a long generation keeps the counters flat, but the
  KV ratio moves per token. Measured on TensorFold during one live request:
  the ratio rose from 0.000198 to 0.000935 while both finished-token counters
  stayed 0. UNVERIFIED on hardware for vLLM, whose KV usage gauge is the signal
  there; a wrong signal only produces a warning.

An engine restart inside the window resets its counters, which reads as change
and gives no verdict. A gap between readings cannot hide progress, because the
counters are cumulative and the first and last readings bound the range.

Window: 15 minutes, longer than any healthy no-progress stretch we know of
(TensorFold's time to first token was 10 s or less for 38 of 42 requests, and
chunked prefill moves the KV ratio). Calibrate against false positives.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_control.fleet_projection.common import ProjectionReason
from vonk_control.telemetry.samples import EngineWindow

ENGINE_STALL_WINDOW = timedelta(minutes=15)
ENGINE_STALL_COVERAGE_SLACK = timedelta(seconds=60)


def not_progressing(window: EngineWindow, since: datetime) -> bool:
    ranges = [span for span in window.progress_ranges if span is not None]
    return (
        window.oldest_received_at <= since + ENGINE_STALL_COVERAGE_SLACK
        and window.pending_readings == window.readings
        and window.pending_min >= 1
        and bool(ranges)
        and all(low == high for low, high in ranges)
    )


def not_progressing_warning(title: str) -> ProjectionReason:
    minutes = int(ENGINE_STALL_WINDOW.total_seconds() // 60)
    return ProjectionReason(
        code=ProjectionCode.RUN_NOT_PROGRESSING,
        detail=f"{title}: requests are pending but the engine showed no progress for {minutes} minutes."[
            :256
        ],
        severity=ProfileReasonSeverity.WARNING.value,
        recommendation=(
            "Look at the run's logs and, for a multi-node run, the fabric. "
            "Restarting the run is your choice; nothing restarts it automatically."
        ),
    )
