from typing import Literal, cast

CompiledRuntimeTelemetryMetricsFormatType0 = Literal['comfyui-queue', 'prometheus']

COMPILED_RUNTIME_TELEMETRY_METRICS_FORMAT_TYPE_0_VALUES: set[CompiledRuntimeTelemetryMetricsFormatType0] = { 'comfyui-queue', 'prometheus',  }

def check_compiled_runtime_telemetry_metrics_format_type_0(value: str) -> CompiledRuntimeTelemetryMetricsFormatType0:
    if value in COMPILED_RUNTIME_TELEMETRY_METRICS_FORMAT_TYPE_0_VALUES:
        return cast(CompiledRuntimeTelemetryMetricsFormatType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_RUNTIME_TELEMETRY_METRICS_FORMAT_TYPE_0_VALUES!r}")
