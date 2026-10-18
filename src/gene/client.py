from __future__ import annotations

import os

import httpx


class ControllerClient:
    def __init__(self, role: str):
        self.client = httpx.Client(
            base_url=os.getenv('GENE_CONTROLLER_URL', 'http://127.0.0.1:8787'),
            headers={'Authorization': 'Bearer ' + os.environ[f'GENE_{role.upper()}_TOKEN']},
            timeout=30,
        )

    def post(self, path, data=None):
        response = self.client.post(path, json=data or {})
        response.raise_for_status()
        return response.json()

