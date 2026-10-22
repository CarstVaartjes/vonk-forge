"""One inventory observation drives admission and fleet health explanations."""

from vonk_agent_protocol import ProjectionCode
from vonk_agent_protocol.inventory import GPU_RESET_RECOVERY, GpuHealth


def gpu_health_reason(health: GpuHealth | None) -> tuple[ProjectionCode, str] | None:
    if health == GpuHealth.NEEDS_RESET:
        return (
            ProjectionCode.NODE_GPU_REQUIRES_RESET,
            f"GPU requires reset; Spark cannot receive work. {GPU_RESET_RECOVERY}",
        )
    if health == GpuHealth.UNAVAILABLE:
        return (
            ProjectionCode.NODE_GPU_UNAVAILABLE,
            (
                "GPU unavailable; Spark cannot receive work. Restore the NVIDIA driver/device; "
                "the next healthy inventory automatically restores admission."
            ),
        )
    return None


GPU_HEALTH_CODES = frozenset(
    {ProjectionCode.NODE_GPU_REQUIRES_RESET, ProjectionCode.NODE_GPU_UNAVAILABLE}
)
