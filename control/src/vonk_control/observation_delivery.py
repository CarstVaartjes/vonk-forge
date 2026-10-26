"""Observe delivery at the authenticated ingress, including incomplete bodies."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from vonk_agent_protocol import WaitReason

from .agent_api import AgentApiServices
from .agent_contacts import record_observation_delivery
from .auth import agent_identity_from_scope
from .logging import log_event

_LOGGER = logging.getLogger(__name__)


class ObservationDeliveryMiddleware:
    def __init__(self, app: ASGIApp, *, services: AgentApiServices | None) -> None:
        self.app = app
        self.services = services

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] != "/agent/recipe-runs/observations"
            or scope["method"] != "POST"
        ):
            await self.app(scope, receive, send)
            return
        identity = agent_identity_from_scope(dict(scope))
        state = scope.setdefault("state", {})
        state["received_body_bytes"] = 0
        length = next((v for k, v in scope["headers"] if k == b"content-length"), None)
        state["expected_body_bytes"] = (
            int(length) if length and length.isdigit() else None
        )
        started = datetime.now(UTC)

        async def counted_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                state["received_body_bytes"] += len(message.get("body", b""))
            elif message["type"] == "http.disconnect":
                state["body_disconnected"] = True
            return message

        async def recorded_send(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and identity is not None
                and self.services is not None
            ):
                try:
                    await asyncio.to_thread(
                        self._record, identity.node_id, message["status"], started
                    )
                except SQLAlchemyError as error:
                    # Delivery bookkeeping cannot replace the actual response.
                    # The next report observes contact again after DB recovery.
                    log_event(
                        _LOGGER,
                        "agent.observation_delivery_deferred",
                        service="control-api",
                        code=WaitReason.OBSERVATION_UNAVAILABLE,
                        node_id=identity.node_id,
                        detail=type(error).__name__,
                    )
            await send(message)

        await self.app(scope, counted_receive, recorded_send)

    def _record(self, node_id: str, status: int, started: datetime) -> None:
        assert self.services is not None
        with self.services.sessions.begin() as session:
            # A Controller contact verdict, not agent-reported data: one upsert
            # of the contact row, never a lock on the node admission takes. A
            # delayed response cannot overwrite a newer verdict.
            record_observation_delivery(session, node_id, status, started)
