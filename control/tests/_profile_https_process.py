"""Run the real profile API in one restartable HTTPS process for acceptance tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
from pathlib import Path

import uvicorn
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import TokenCodec
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.jobs import JobService

from tests.test_fleet_profiles import _SwitchAdapter
from tests.test_fleet_profiles_canonical import NOW


def _app(trace_path: Path):
    database_url = os.environ["VONK_TEST_DATABASE_URL"]
    from sqlalchemy import create_engine

    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    codec = TokenCodec(b"p" * 32)
    clock = lambda: NOW
    app = create_app(
        jobs=JobService(sessions, clock=clock),
        tokens=codec,
        audits=MemoryAuditStore(),
        now=lambda: 1,
        fleet_profiles=FleetProfileService(
            sessions, clock=clock, switch_adapter=_SwitchAdapter()
        ),
    )

    @app.middleware("http")
    async def record_requests(request, call_next):
        response = await call_next(request)
        entry = {
            "pid": os.getpid(),
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
        }
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
        return response

    return app, engine, sessions


async def _serve(args: argparse.Namespace) -> None:
    listener = socket.socket(fileno=args.socket_fd)
    app, engine, _sessions = _app(args.trace)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            lifespan="off",
            ssl_certfile=str(args.certificate),
            ssl_keyfile=str(args.private_key),
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started and not serving.done():
        await asyncio.sleep(0.01)
    if server.started:
        args.ready.write_text(str(os.getpid()), encoding="ascii")
    try:
        await serving
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-fd", type=int, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    asyncio.run(_serve(parser.parse_args()))


if __name__ == "__main__":
    main()
