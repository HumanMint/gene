import asyncio
import signal
from unittest.mock import AsyncMock

import pytest
import uvicorn

from gene.audio_relay_server import DownloadRelayServer


def test_idle_relay_does_not_poll_and_shutdown_signal_wakes_it():
    async def scenario():
        server = DownloadRelayServer(uvicorn.Config(lambda scope: None))
        server.on_tick = AsyncMock(return_value=False)
        task = asyncio.create_task(server.main_loop())
        await asyncio.sleep(0.25)
        assert not task.done()
        server.on_tick.assert_awaited_once_with(0)
        assert server.config.date_header is False
        server.handle_exit(signal.SIGTERM, None)
        await asyncio.wait_for(task, timeout=0.1)
        assert server.should_exit
    asyncio.run(scenario())


def test_exit_before_main_loop_does_not_hang():
    async def scenario():
        server = DownloadRelayServer(uvicorn.Config(lambda scope: None))
        server.on_tick = AsyncMock(return_value=True)
        await asyncio.wait_for(server.main_loop(), timeout=0.1)
    asyncio.run(scenario())


@pytest.mark.parametrize('options', [{'limit_max_requests': 1}, {'callback_notify': AsyncMock()}])
def test_periodic_supervisor_requirements_cannot_be_silently_disabled(options):
    with pytest.raises(ValueError):
        DownloadRelayServer(uvicorn.Config(lambda scope: None, **options))


def test_real_sigterm_wakes_a_fully_idle_selector():
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    script = '''
import asyncio, signal
import uvicorn
from gene.audio_relay_server import DownloadRelayServer
config = uvicorn.Config(lambda scope: None, log_config=None)
config.load()
server = DownloadRelayServer(config)
signal.signal(signal.SIGTERM, server.handle_exit)
async def run():
    asyncio.get_running_loop().call_soon(lambda: print('ready', flush=True))
    await server.main_loop()
asyncio.run(run())
'''
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    process = subprocess.Popen([sys.executable, '-u', '-c', script], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert process.stdout.readline().strip() == 'ready'
        time.sleep(0.1)  # Let the selector block with no timer or network activity.
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
