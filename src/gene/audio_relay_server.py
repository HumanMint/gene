"""A home relay should wait for socket activity instead of polling while idle."""

import asyncio

import uvicorn


class DownloadRelayServer(uvicorn.Server):
    """Uvicorn's request handling and shutdown, without its 10 Hz housekeeping loop.

    This private relay has no request-count limit or supervisor heartbeat. It does
    not need a periodically refreshed Date header. Socket and keepalive events still
    use the normal event loop; a termination signal wakes shutdown immediately.
    """

    def __init__(self, config):
        if config.limit_max_requests is not None or config.callback_notify is not None:
            raise ValueError('The home relay cannot use polling-based server limits or notifications')
        config.date_header = False
        super().__init__(config)
        self.exit_event = asyncio.Event()
        self.relay_loop = None

    async def main_loop(self):
        self.relay_loop = asyncio.get_running_loop()
        if not await self.on_tick(0):
            await self.exit_event.wait()

    def handle_exit(self, sig, frame):
        super().handle_exit(sig, frame)
        if self.relay_loop is not None:
            # signal.signal can interrupt a blocked selector; wake its self-pipe too.
            self.relay_loop.call_soon_threadsafe(self.exit_event.set)
