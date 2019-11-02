"""
Can be aslo run as:
$> python uvicorn/main.py tests.test_h2:App --http=h2
"""
import asyncio
import pytest

from httpx import AsyncClient

from uvicorn.config import Config
from uvicorn.main import Server


global_call_count = 0


class App:
    def __init__(self, scope):
        self.scope = scope
        self.call_count = 0

    async def __call__(self, receive, send):
        global global_call_count
        
        self.call_count += 1
        global_call_count += 1

        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": bytes(f"That's the body! - {self.call_count} - {global_call_count}", "utf-8"), "more_body": False})


class CustomServer(Server):
    # def install_signal_handlers(self):
    #     pass
    pass


@pytest.mark.asyncio
async def test_baseline_h11():
    client = AsyncClient()

    config = Config(app=App, loop="asyncio", limit_max_requests=3, http='h11')
    server = CustomServer(config=config)

    # Prepare the coroutine to serve the request
    run_request = server.serve()
    # Prepare the coroutine to make the request
    make_request = client.get("http://127.0.0.1:8000")

    # Reset the global counter
    global global_call_count
    global_call_count = 0

    # Run coroutines
    results = await asyncio.gather(*[
        run_request,
        client.get("http://127.0.0.1:8000"),
        client.get("http://127.0.0.1:8000"),
        client.get("http://127.0.0.1:8000"),
    ])

    assert [x.text for x in results if x] == ["That's the body! - 1 - 2", "That's the body! - 1 - 3", "That's the body! - 1 - 4"]


@pytest.mark.skip(reason='h2 protocol is WIP')
@pytest.mark.asyncio
async def test_baseline_h2():
    client = AsyncClient()

    config = Config(app=App, loop="asyncio", limit_max_requests=3, http='h2')
    server = CustomServer(config=config)

    # Prepare the coroutine to serve the request
    run_request = server.serve()
    # Prepare the coroutine to make the request
    make_request = client.get("http://127.0.0.1:8000")

    # Reset the global counter
    global global_call_count
    global_call_count = 0

    # Run coroutines
    results = await asyncio.gather(*[
        run_request,
        client.get("http://127.0.0.1:8000"),
        client.get("http://127.0.0.1:8000"),
        client.get("http://127.0.0.1:8000"),
    ])

    assert [x.text for x in results if x] == ["That's the body! - 1 - 2", "That's the body! - 1 - 3", "That's the body! - 1 - 4"]
