"""The fake Sparks of ``ReachWorld``, with the world's own faults beside theirs.

The model provider being down and the NAS being short of disk are faults like
a Spark being down: waiting is legitimate while one is active, and they count
down per step and clear when the world quiesces.
"""

from __future__ import annotations

from .reach_world import ReachWorld
from .sparks import Sparks


class ReachSparks(Sparks):
    @property
    def reach(self) -> ReachWorld:
        assert isinstance(self.world, ReachWorld)
        return self.world

    def healthy(self) -> bool:
        world = self.reach
        return (
            super().healthy()
            and world.provider.down_steps == 0
            and world.nas_short_steps == 0
        )

    def end_step(self) -> None:
        super().end_step()
        world = self.reach
        world.provider.down_steps = max(0, world.provider.down_steps - 1)
        world.nas_short_steps = max(0, world.nas_short_steps - 1)

    def clear_faults(self) -> None:
        super().clear_faults()
        self.reach.provider.down_steps = 0
        self.reach.nas_short_steps = 0
