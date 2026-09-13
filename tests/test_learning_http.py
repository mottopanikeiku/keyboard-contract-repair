import asyncio
import threading

import httpx

from keyproof.api import create_app
from keyproof.learning_contracts import LearningConfig, LearningRun
from keyproof.telemetry import Telemetry


async def test_slow_evidence_read_does_not_block_controller_event_loop(tmp_path, monkeypatch):
    async def local_connection(self):
        return self.status

    monkeypatch.setattr(Telemetry, "connect", local_connection)
    app = create_app(tmp_path)
    entered, release = threading.Event(), threading.Event()
    record = LearningRun(
        learning_id="a" * 32,
        config=LearningConfig(model="test-model", require_weave=False),
    )

    def slow_read(identifier):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Evidence read stalled the event loop that must release it")
        return record

    async with app.router.lifespan_context(app):
        monkeypatch.setattr(app.state.service.store, "get_learning", slow_read)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            pending = asyncio.create_task(client.get(f"/api/learning/{record.learning_id}"))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                # This must complete while evidence I/O is still blocked, not afterwards.
                status = await asyncio.wait_for(client.get("/api/status"), timeout=1)
                assert status.status_code == 200
            finally:
                release.set()
            response = await pending
            assert response.status_code == 200
