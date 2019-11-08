"""
The first step is:
    - RequestReceived
    - DataReceived
    - StreamEnded
    - ConnectionTerminated

TODO LIST:
- bench async vs sync funcs
- bench isinstance vs str match

- (!!!) get some clarification, how the connection should be closed
    in terms of the trigger from the client or smth else.
    As the end of the stream isn't a reason for this.
"""

import asyncio
import http
import logging

from collections import defaultdict
from logging import Logger
from typing import Any, Callable, Dict, List
from urllib.parse import unquote

import priority
from h2.connection import H2Configuration, H2Connection
from h2.events import (  # TODO check a few other events not covered in here
    ConnectionTerminated,
    DataReceived,
    Event,
    PriorityUpdated,
    RemoteSettingsChanged,
    RequestReceived,
    StreamEnded,
    StreamReset,
    WindowUpdated,
    SettingsAcknowledged,
)
from h2.exceptions import ProtocolError

from uvicorn.config import Config
from uvicorn.main import ServerState
from uvicorn.protocols.utils import (
    get_client_addr,
    get_local_addr,
    get_path_with_query_string,
    get_remote_addr,
    is_ssl,
)


def _get_status_phrase(status_code):
    try:
        return http.HTTPStatus(status_code).phrase.encode()
    except ValueError as exc:
        return b""


STATUS_PHRASES = {
    status_code: _get_status_phrase(status_code) for status_code in range(100, 600)
}

HIGH_WATER_LIMIT = 65536

TRACE_LOG_LEVEL = 5

CONNECTION_INITIATED = "Connection initiated"
COMMUNICATION_WITH_CLIENT = "communication with client"
INVALID_REQUEST_RECEIVED = "Invalid HTTP request received."
NEW_SETTINGS_RECEIVED = "New settings received"


# TODO move to the common structures between protocols
# As long as it stays the exactly same
class FlowControl:
    def __init__(self, transport):
        self._transport = transport
        self.read_paused = False
        self.write_paused = False
        self._is_writable_event = asyncio.Event()
        self._is_writable_event.set()

    async def drain(self):
        await self._is_writable_event.wait()

    def pause_reading(self):
        if not self.read_paused:
            self.read_paused = True
            self._transport.pause_reading()

    def resume_reading(self):
        if self.read_paused:
            self.read_paused = False
            self._transport.resume_reading()

    def pause_writing(self):
        if not self.write_paused:
            self.write_paused = True
            self._is_writable_event.clear()

    def resume_writing(self):
        if self.write_paused:
            self.write_paused = False
            self._is_writable_event.set()


# TODO to generalize or not to generalize?
# TODO SOLID would probably be against, while DRY wouldn't
class H2Protocol(asyncio.Protocol):
    def __init__(
        self,
        config: Config,
        server_state: ServerState,
        _loop: asyncio.AbstractEventLoop = None,
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
            header_encoding="utf-8",  # TODO move to the `uvicorn.Config`
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
        # TODO consider the per connection flow
        self.connection_flow = None
        self.server = None
        self.client = None
        self.scheme = None

        # HTTP/2 specific states
        self.streams = defaultdict(
            lambda: {
                # Per-request states
                # TODO use dataclass instead? they'll be a bit slower tho (w/o slots)
                "scope": None,
                "headers": None,
                "cycle": None,
                "message_event": asyncio.Event(),
                "flow": None,
            }
        )  # So we can store the data per the stream id
        self.priority = priority.PriorityTree()

        # Per-request state  # TODO or per-stream one??
        # NOTE yes, data is per-stream
        # self.scope = None
        # self.headers = None
        # self.cycle = None
        # self.message_event = asyncio.Event()

    def write_trace_log(self, message: str, data_to_send: Dict[str, Any]):
        """ Performs the trace logging and the message formatting. """
        if self.logger.level <= TRACE_LOG_LEVEL:
            prefix = "%s:%d - " % tuple(self.client) if self.client else ""
            # log_line = "%s%s: %s" % prefix, message, data_to_send
            # log_line_f = f"{':',join(map(str, self.client)) + ' - ' if self.client else ""}{message}: {data_to_send}"
            self.logger.log(
                TRACE_LOG_LEVEL, "%s%s: %s", (prefix, message, data_to_send)
            )

    def transport_write(self, message: str = ""):
        """ Handles the trace logging and the routine write to the transport """
        data_to_send = self.conn.data_to_send()
        self.transport.write(data_to_send)
        self.write_trace_log(message, data_to_send)

    def connection_made(self, transport: asyncio.Transport) -> None:
        """ Protocol interface on `connection_made` """
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
        # raise NotImplementedError()
        # TODO !!!
        pass

    def eof_received(self):
        """ Protocol interface on `eof_received` """
        # raise NotImplementedError()
        # TODO !!!
        pass

    def data_received(self, data: bytes) -> None:
        """ Protocol interface on `data_received` """
        if self.timeout_keep_alive_task is not None:
            self.timeout_keep_alive_task.cancel()
            self.timeout_keep_alive_task = None
        # import pdb; pdb.set_trace()
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
        """ Handle recieved events """
        from pprint import pprint
        print("\n\n\n!!!!!!!!!!!")
        pprint(events)
        print("\n\n\n!!!!!!!!!!!")
        # import pdb; pdb.set_trace()
        for event in events:
            if isinstance(event, RemoteSettingsChanged):
                self.handle_remote_settings_changed(event)
            elif isinstance(event, RequestReceived):
                self.handle_request_received(event)
            elif isinstance(event, DataReceived):
                self.handle_data_received(event)
            elif isinstance(event, StreamEnded):
                self.handle_stream_ended(event)
            elif isinstance(event, SettingsAcknowledged):
                # TODO check if we can do anything else
                pass
            elif isinstance(event, StreamReset):
                raise NotImplementedError()
            elif isinstance(event, WindowUpdated):
                raise NotImplementedError()
            elif isinstance(event, PriorityUpdated):
                raise NotImplementedError()
            elif isinstance(event, ConnectionTerminated):
                self.transport.close()
                # raise NotImplementedError()
            else:
                # TODO good for testing, but should not go to the final release!
                raise NotImplementedError(f'Event "{event}" is not supported.')

            self.transport_write(COMMUNICATION_WITH_CLIENT)

    def handle_request_received(self, request_event: RequestReceived):
        """ Handle the RequestReceived event. """
        # TODO trace logging
        # import pdb; pdb.set_trace()
        headers = dict(request_event.headers)
        method = headers.get(":method")
        raw_path, _, query_string = headers.get(":path", "").partition("?")

        # TODO not sure yet how to handle this one
        if method == "CONNECT":
            # WS over HTTP/2
            raise NotImplementedError()

        # Prepare the request context
        stream = self.streams[request_event.stream_id]
        stream["headers"] = [(k.lower(), v) for k, v in headers.items()]
        stream["scope"] = {
            "type": "http",
            "http_version": "2",  # TODO check if it can be obtained
            "server": self.server,
            "client": self.client,
            "scheme": self.scheme,
            "method": method,
            "root_path": self.root_path,
            "path": unquote(raw_path),
            "query_string": query_string,
            "headers": headers,
            # For the server push
            "extensions": {"http.response.push": {}},
        }

        stream["flow"] = FlowControl(self.transport)

        # TODO will `limit_concurrency` have sense here?
        app = self.app
        stream["cycle"] = RequestResponseCycle(
            scope=stream["scope"],
            conn=self.conn,
            transport=self.transport,
            flow=stream["flow"],
            logger=self.logger,
            access_logger=self.access_logger,
            access_log=self.access_log,
            default_headers=self.default_headers,
            message_event=stream["message_event"],
            on_response=self.on_response_complete,
            stream_id=request_event.stream_id,
        )

        # # Manage the priority
        # try:
        #     self.priority.insert_stream(request_event.stream_id)
        # except priority.DuplicateStreamError:
        #     # Recieved PRIORITY frame before HEADERS frame
        #     pass
        # else:
        #     self.priority.block(stream_id)
        # import pdb; pdb.set_trace()
        task = self.loop.create_task(stream["cycle"].run_asgi(app))
        task.add_done_callback(self.tasks.discard)
        self.tasks.add(task)

    def handle_data_received(self, data_received_event: DataReceived):
        """ Handle the DataReceived event. """
        # import pdb; .set_trace()
        # TODO check is done?

        # TODO trace logging
        stream = self.streams[data_received_event.stream_id]
        stream["cycle"].body += data_received_event.data
        # if let(stream.cycle.body) > HIGH_WATER_LIMIT:
        #     stream.flow.pause_reading()
        stream["message_event"].set()

        # TODO consider the use of `increment_flow_control_window`
        # For managing the stream and the connection
        self.conn.acknowledge_received_data(
            data_received_event.flow_controlled_length, data_received_event.stream_id
        )

    def handle_stream_ended(self, stream_ended_event: StreamEnded):
        # import pdb; pdb.set_trace()
        # TODO check is done

        stream = self.streams[stream_ended_event.stream_id]

        stream["cycle"].more_body = False
        stream["message_event"].set()

    def handle_remote_settings_changed(
        self, remote_settings_changed: RemoteSettingsChanged
    ):
        # TODO Apply settings
        # Log them for now
        new_settings = "\n".join(
            [
                f"{str(x.setting):<40}: original_value - {str(x.original_value):>7}; new_value - {str(x.new_value):>7}"
                for x in remote_settings_changed.changed_settings.values()
            ]
        )
        self.write_trace_log(NEW_SETTINGS_RECEIVED, data_to_send={})
        self.logger.log(TRACE_LOG_LEVEL, new_settings)

    def on_response_complete(self):
        self.server_state.total_requests += 1

        # TODO Check the trigger as
        # we probably don't want to close the connection unless all streams are done
        if self.transport.is_closing():
            return

        # Set a short Keep-Alive timeout.
        self.timeout_keep_alive_task = self.loop.call_later(
            self.timeout_keep_alive, self.timeout_keep_alive_handler
        )

        # TODO unpause data reads?
        # TODO unblock the priority?

    def shutdown(self):
        # TODO (!!!)
        # raise NotImplementedError()
        # TODO Invalid implementation before some clarity
        # TODO check all streams are done
        # if self.cycle is None or self.cycle.response_complete:
            # event = h11.ConnectionClosed()
            # self.conn.send(event)
        # self.transport.close()
        self.transport.write(self.conn.data_to_send())
        # else:
        #     self.cycle.keep_alive = False

    def pause_writing(self):
        """
        Called by the transport when the write buffer exceeds the high water mark.
        """
        self.flow.pause_writing()

    def resume_writing(self):
        """
        Called by the transport when the write buffer drops below the low water mark.
        """
        self.flow.resume_writing()

    def timeout_keep_alive_handler(self):
        """
        Called on a keep-alive connection if no new data is received after a short delay.
        """
        # TODO (!!)
        pass
        # raise NotImplementedError()


# TODO to generalize or not to generalize?
# TODO SOLID would probably be against, while DRY wouldn't
class RequestResponseCycle:
    # NOTE: The similar entity is called HTTPStream in `hypercorn`
    def __init__(
        self,
        scope: Dict[str, Any],
        conn: H2Connection,
        transport: asyncio.Transport,
        flow: FlowControl,
        logger: Logger,
        access_logger: Logger,
        access_log: bool,
        default_headers: Dict[str, Any],
        # TODO double-check
        # TODO called `message_event` in `h11_impl`
        # TODO or it's `asyncio.Event()`??
        message_event: asyncio.Event,
        on_response: Callable,  # TODO double-check
        stream_id: int,
    ) -> None:
        self.scope = scope
        self.conn = conn
        self.transport = transport
        self.flow = flow
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
        try:
            # import pdb; pdb.set_trace()
            result = await app(self.scope, self.receive, self.send)
        except BaseException as exc:
            msg = "Exception in ASGI application\n"
            self.logger.error(msg, exc_info=exc)
            if not self.response_started:
                await self.send_500_response()
            else:
                self.transport.close()
        else:
            if result is not None:
                msg = "ASGI callable should return None, but returned '%s'."
                self.logger.error(msg, result)
                self.transport.close()
            elif not self.response_started and not self.disconnected:
                msg = "ASGI callable returned without starting response."
                self.logger.error(msg)
                await self.send_500_response()
            elif not self.response_complete and not self.disconnected:
                msg = "ASGI callable returned without completing response."
                self.logger.error(msg)
                self.transport.close()
        finally:
            self.on_response = None

    async def send_500_response(self) -> None:
        await self.send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"connection", b"close"),
                ],
            }
        )
        await self.send(
            {"type": "http.response.body", "body": b"Internal Server Error"}
        )

    async def send(self, message: str) -> None:
        """ ASGI interface"""
        # import pdb; pdb.set_trace()
        message_type = message["type"]

        # TODO account for message["type"] == "http.response.push"

        if self.flow.write_paused and not self.disconnected:
            await self.flow.drain()

        if self.disconnected:
            return

        if not self.response_started:
            # Sending response status line and headers
            if message_type != "http.response.start":
                msg = "Expected ASGI message 'http.response.start', but got '%s'."
                raise RuntimeError(msg % message_type)

            self.response_started = True
            # self.waiting_for_100_continue = False

            status_code = message["status"]
            headers = self.default_headers + message.get("headers", [])

            if self.access_log:
                self.access_logger.info(
                    '%s - "%s %s HTTP/%s" %d',
                    get_client_addr(self.scope),
                    self.scope["method"],
                    get_path_with_query_string(self.scope),
                    self.scope["http_version"],
                    status_code,
                    extra={"status_code": status_code, "scope": self.scope},
                )


            # Write response status line and headers
            reason = STATUS_PHRASES[status_code]
            # event = h11.Response(
            #     status_code=status_code, headers=headers, reason=reason
            # )
            headers = [(":status", str(status_code))] + headers
            # output = self.conn.send(event)
            # self.transport.write(output)
            # import pdb; pdb.set_trace()
            output = self.conn.send_headers(self.stream_id, headers)
            # self.transport.write(output)  # TODO ??

        elif not self.response_complete:
            # Sending response body
            if message_type != "http.response.body":
                msg = "Expected ASGI message 'http.response.body', but got '%s'."
                raise RuntimeError(msg % message_type)

            body = message.get("body", b"")
            more_body = message.get("more_body", False)

            # Write response body
            output = self.conn.send_data(self.stream_id, body)  # , end_stream=True
            # self.transport.write(output)  # TODO ??

            # Handle response completion
            if not more_body:
                self.response_complete = True
                # event = h11.EndOfMessage()
                # output = self.conn.send(event)
                # self.transport.write(output)

        else:
            # Response already sent
            msg = "Unexpected ASGI message '%s' sent, after response already completed."
            raise RuntimeError(msg % message_type)

        if self.response_complete:
            # import pdb; pdb.set_trace()
            # if self.conn.our_state is h11.MUST_CLOSE or not self.keep_alive:
            #     event = h11.ConnectionClosed()
            self.conn.send_data(self.stream_id, b"", end_stream=True)
            # NOTE we don't close the transport on the stream end
            # self.transport.close()
            self.on_response()

    async def receive(self) -> None:
        # import pdb; pdb.set_trace()

        # if self.waiting_for_100_continue and not self.transport.is_closing():
        #     event = h11.InformationalResponse(
        #         status_code=100, headers=[], reason="Continue"
        #     )
        #     output = self.conn.send(event)
        #     self.transport.write(output)
        #     self.waiting_for_100_continue = False

        if not self.disconnected and not self.response_complete:
            self.flow.resume_reading()
            await self.message_event.wait()
            self.message_event.clear()

        if self.disconnected or self.response_complete:
            message = {"type": "http.disconnect"}
        else:
            message = {
                "type": "http.request",
                "body": self.body,
                "more_body": self.more_body,
            }
            self.body = b""

        return message
