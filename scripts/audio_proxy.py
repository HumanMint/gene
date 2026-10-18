#!/usr/bin/env python3
"""Tailnet TCP relay that accepts the configured VPS only. Lavalink adds its own password."""

import os
import selectors
import socket
import socketserver


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        if self.client_address[0] != os.environ['GENE_VPS_IP']:
            return
        with socket.create_connection(('127.0.0.1', 2333), timeout=10) as upstream:
            upstream.settimeout(None)
            with selectors.DefaultSelector() as selector:
                selector.register(self.request, selectors.EVENT_READ, upstream)
                selector.register(upstream, selectors.EVENT_READ, self.request)
                while True:
                    events = selector.select(timeout=300)
                    if not events:
                        return
                    for event, _ in events:
                        try:
                            data = event.fileobj.recv(65536)
                            if not data:
                                return
                            event.data.sendall(data)
                        except (ConnectionError, OSError):
                            return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    with Server((os.environ['GENE_AUDIO_BIND'], 2333), Relay) as server:
        server.serve_forever()

