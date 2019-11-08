"""
Can be aslo run as:
$> python uvicorn/main.py tests.test_h2:App --http=h2
"""
import asyncio

import pytest
import trustme
from httpx import AsyncClient
from hyper import HTTP20Connection

from uvicorn.config import Config
from uvicorn.main import Server

global_call_count = 0


@pytest.fixture()
def CA():
    yield trustme.CA()


@pytest.fixture()
def server_cert_file(CA):
    server_cert = CA.issue_cert("localtest.me").private_key_and_cert_chain_pem
    with server_cert.tempfile() as ca_temp_path:
        yield ca_temp_path


@pytest.fixture()
async def client_cert_file(CA):
    with CA.cert_pem.tempfile() as ca_temp_path:
        yield ca_temp_path


@pytest.mark.asyncio
@pytest.fixture()
async def async_test_client_h11(client_cert_file):
    async with AsyncClient(
        http_versions=["HTTP/1.1"],
        base_url="https://localtest.me:8000",
        verify=client_cert_file,
    ) as async_test_client:
        yield async_test_client


@pytest.mark.asyncio
@pytest.fixture()
async def async_test_client_h2(client_cert_file):
    async with AsyncClient(
        http_versions=["HTTP/2"],
        base_url="https://localtest.me:8000",
        verify=client_cert_file,
    ) as async_test_client:
        yield async_test_client


class App:
    def __init__(self, scope):
        self.scope = scope
        self.call_count = 0

    async def __call__(self, receive, send):
        # import pdb; pdb.set_trace()
        global global_call_count

        self.call_count += 1
        global_call_count += 1

        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": bytes(
                    f"That's the body! - {self.call_count} - {global_call_count}",
                    "utf-8",
                ),
                "more_body": False,
            }
        )


@pytest.mark.skip(reason="h2 protocol is WIP")
@pytest.mark.asyncio
async def test_baseline_h11(async_test_client_h11, CA, server_cert_file):
    config = Config(
        app=App, limit_max_requests=3, http="h11", ssl_certfile=server_cert_file
    )
    server = Server(config=config)
    config.load()
    CA.configure_trust(config.ssl)

    # Prepare the coroutine to serve the request
    run_request = server.serve()

    # Reset the global counter
    global global_call_count
    global_call_count = 0

    # Run coroutines
    results = await asyncio.gather(
        *[
            run_request,
            async_test_client_h11.get("/"),
            async_test_client_h11.get("/"),
            async_test_client_h11.get("/"),
        ]
    )

    assert sorted([x.text for x in results if x]) == [
        "That's the body! - 1 - 2",
        "That's the body! - 1 - 3",
        "That's the body! - 1 - 4",
    ]


# @pytest.mark.skip(reason="h2 protocol is WIP")
@pytest.mark.asyncio
async def test_baseline_h2(async_test_client_h2, CA, server_cert_file):
    config = Config(
        app=App, limit_max_requests=1, http="h2", ssl_certfile=server_cert_file
    )
    server = Server(config=config)
    config.load()
    CA.configure_trust(config.ssl)

    # Prepare the coroutine to serve the request
    run_request = server.serve()

    # Reset the global counter
    global global_call_count
    global_call_count = 0

    # Run coroutines
    results = await asyncio.gather(
        *[
            run_request,
            async_test_client_h2.get("/"),
            # async_test_client_h2.get("/"),
            # async_test_client_h2.get("/"),
        ]
    )

    assert sorted([x.text for x in results if x]) == [
        "That's the body! - 1 - 2",
        # "That's the body! - 1 - 3",
        # "That's the body! - 1 - 4",
    ]


# TODO (!!!)
# Now functions are sync
# Run the bench with async invariants
