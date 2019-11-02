import asyncio

from logging import Logger
from typing import Dict, Any, List, Callable

from h2.connection import H2Connection, H2Configuration
from h2.events import (
    Event,
    RequestReceived,
    DataReceived,
    StreamEnded,
    StreamReset,
    WindowUpdated,
    PriorityUpdated,
    RemoteSettingsChanged,
    ConnectionTerminated
)
from h2.exceptions import ProtocolError
import priority

from uvicorn.config import Config
from uvicorn.main import ServerState


HIGH_WATER_LIMIT = 65536

CONNECTION_INITIATED = "Connection initiated"
COMMUNICATION_WITH_CLIENT = "communication with client"
INVALID_REQUEST_RECEIVED = "Invalid HTTP request received."


class H2Protocol(asyncio.Protocol):
    def __init__(
        self,
        config: Config,
        server_state: ServerState,
        _loop: asyncio.AbstractEventLoop = None
    ) -> None:
        if not config.loaded:
            config.load()

        self.config = config
        self.app = config.loaded_app
        self.loop = _loop or asyncio.get_event_loop()
        self.logger = logging.getLogger("uvicorn.error")
        self.access_logger = logging.getLogger("uvicorn.access")
        self.access_log = self.access_logger.hasHandlers()

        # TODO add aditional params to the `self.connection` like max number of streams
        h2config = H2Configuration(
            client_side=False,  # TODO move to the `uvicorn.Config`
            header_encoding='utf-8'  # TODO move to the `uvicorn.Config`
        )

        self.conn = H2Connection(config=h2config)
        # self.ws_protocol_class = config.ws_protoc_class  # TODO check if it can work
        self.root_path = config.root_path
        # self.limit_concurrency = config.limit_concurrency  # TODO check if it makes sense

        # Timeouts
        self.timeout_keep_alive_task = None
        self.timeout_keep_alive = config.timeout_keep_alive
    
        # Shared server state
        self.server_state = server_state
        self.connections = server_state.connections
        self.tasks = server_state.tasks
        self.default_headers = server_state.default_headers
    
        # Per-connection state
        self.transport = None
        # self.flow = None
        self.server = None
        self.client = None
        self.scheme = None
        self.stream_data = {}  # So we can store the data per the stream id
        self.priority = priority.PriorityTree()

        # Per-request state
        # self.scope = None
        # self.headers = None
        # self.cycle = None
        # self.message_event = asyncio.Event()

    def write_trace_log(message: str, data_to_send: Dict[str, Any]):
        """ Performs the trace logging and the message formatting. """
        if self.logger.level <= TRACE_LOG_LEVEL:
            prefix = "%s:%d - " % tuple(self.client) if self.client else ""
            log_line = "%s%s: %s" % prefix, message, data_to_send
            # log_line_f = f"{':',join(map(str, self.client)) + ' - ' if self.client else ""}{message}: {data_to_send}"
            self.logger.log(TRACE_LOG_LEVEL, "%s%s: %s", prefix, message, data_to_send)

    def transport_write(self, message: str = ""):
        """ Handles the trace logging and the routine write to the transport """
        data_to_send = self.conn.data_to_send()
        self.transport.write(data_to_send)
        self.write_trace_log(CONNECTION_INITIATED)

    def connection_made(self, transport: asyncio.Transport) -> None:
        """ Protocol interface """
        # TODO double check it's 1 connection per client
        self.connections.add(self)

        self.transport = transport
        # self.flow = FlowControl(transport)
        self.server = get_local_addr(transport)
        self.client = get_remote_addr(transport)
        self.scheme = "https" if is_ssl(transport) else "http"

        self.conn.initiate_connection()
        self.transport_write(CONNECTION_INITIATED)

    def connection_lost(self, exc: Exception):
        raise NotImplementedError()

    def eof_received(self):
        raise NotImplementedError()

    def data_received(self, data: bytes) -> None:
        if self.timeout_keep_alive_task is not None:
            self.timeout_keep_alive_task.cancel()
            self.timeout_keep_alive_task = None
        
        try:
            events = self.conn.receive_data(data)
        except ProtocolError as exc:
            self.logger.exception(INVALID_REQUEST_RECEIVED)
            self.transport_write(INVALID_REQUEST_RECEIVED)
            self.transport.close()  # TODO move to the `self.transport_write`
        else:
            self.transport_write(COMMUNICATION_WITH_CLIENT)
            self.handle_events(events)

    def handle_events(self, events: List[Event]) -> None:
        for event in events:
            if isinstance(event, RequestReceived):
                raise NotImplementedError()
            elif isinstance(event, DataReceived):
                raise NotImplementedError()
            elif isinstance(event, StreamEnded):
                raise NotImplementedError()
            elif isinstance(event, StreamReset):
                raise NotImplementedError()
            elif isinstance(event, WindowUpdated):
                raise NotImplementedError()
            elif isinstance(event, PriorityUpdated):
                raise NotImplementedError()
            elif isinstance(event, RemoteSettingsChanged):
                raise NotImplementedError()
            elif isinstance(event, ConnectionTerminated):
                raise NotImplementedError()

            self.transport_write(COMMUNICATION_WITH_CLIENT)

    def handle_request_received(self, event: RequestReceived):
        raise NotImplementedError()

    def on_response_complete(self):
        raise NotImplementedError()

    def shutdown(self):
        raise NotImplementedError()

    def pause_writing(self):
        """
        Called by the transport when the write buffer exceeds the high water mark.
        """
        raise NotImplementedError()

    def resume_writing(self):
        """
        Called by the transport when the write buffer drops below the low water mark.
        """
        raise NotImplementedError()

    def timeout_keep_alive_handler(self):
        """
        Called on a keep-alive connection if no new data is received after a short delay.
        """
        raise NotImplementedError()


class RequestResponseCycle:
    # NOTE: The similar entity is called HTTPStream in `hypercorn`
    def __init__(
        self,
        scope: Dict[str, Any],
        conn: H2Connection,
        transport: asyncio.Transport,
        # flow: FlowControl,
        logger: Logger,
        access_logger: Logge,
        access_log: bool,
        default_headers: Dict[str, Any],
        # TODO double-check
        # TODO called `message_event` in `h11_impl`
        # TODO or it's `asyncio.Event()`??
        event: RequestReceived,
        on_response: Callable,  # TODO double-check
        stream_id: int
    ) -> None:
        self.scope = scope
        self.conn = conn
        self.transport = transport
        # self.flow = flow
        self.logger = logger
        self.access_logger = logger
        self.access_log = access_log
        self.default_headers = default_headers
        self.message_event = message_event
        self.on_response = on_response
        self.stream_id = stream_id

    # Connection state
    self.disconnected = False
    self.keep_alive = True
    # TODO check additional states required

    # Request state
    self.body = bytearray()
    self.more_body = True

    # Response state
    self.response_started = False
    self.response_complete = False

    async def run_asgi(self, app: "ASGI") -> None:  # TODO clarify the type
        raise NotImplementedError()

    async def send_500_response(self) -> None:
        raise NotImplementedError()

    async def send(self, message: str) -> None:
        """ ASGI interface"""
        raise NotImplementedError()

    async def receive(self) -> None:
        raise NotImplementedError()
