"""Read the account's Codex model catalog without starting an inference turn."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time


def discover_models(command: list[str], timeout=45):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
    incoming = queue.Queue()

    def read_lines():
        for line in process.stdout:
            incoming.put(line)
        incoming.put(None)

    threading.Thread(target=read_lines, daemon=True).start()

    def send(message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()

    def receive(request_id):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = incoming.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if not line:
                raise RuntimeError('Codex model catalog process closed')
            event = json.loads(line)
            if event.get('id') == request_id:
                if 'error' in event:
                    raise RuntimeError('Codex model catalog request failed')
                return event['result']
        raise TimeoutError('Codex model catalog refresh timed out')

    try:
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {
            'name': 'gene', 'title': 'Gene private development worker', 'version': '0.1.0'}}})
        receive(1)
        send({'method': 'initialized', 'params': {}})
        cursor = None
        models = []
        for page in range(20):
            request_id = page + 2
            params = {'includeHidden': False, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            send({'id': request_id, 'method': 'model/list', 'params': params})
            result = receive(request_id)
            for model in result['data']:
                models.append({
                    'id': model.get('model') or model['id'],
                    'name': model.get('displayName') or model['id'],
                    'efforts': [e['reasoningEffort'] for e in model.get('supportedReasoningEfforts', [])],
                    'default_effort': model.get('defaultReasoningEffort', 'medium'),
                })
            cursor = result.get('nextCursor')
            if not cursor:
                return models
        raise RuntimeError('Codex model catalog pagination exceeded its expected size')
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
