import json
from types import SimpleNamespace

import pytest
from test_server_threading import BlenderMCPServer


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionResetError])
def test_response_send_failure_closes_client_before_another_command(failure):
    server = BlenderMCPServer()
    server.running = True
    commands = []

    def dispatch(item):
        command, responses = item
        commands.append(command)
        responses.put(b'{"status":"success","result":{}}')

    server.command_queue = SimpleNamespace(put=dispatch)

    class Client:
        closed = False
        reads = 0

        def settimeout(self, timeout):
            pass

        def do_handshake(self):
            pass

        def recv(self, size):
            self.reads += 1
            if self.reads > 2:
                return b""
            return json.dumps({"type": "execute_code", "params": {}}).encode()

        def sendall(self, response):
            raise failure("partial response")

        def close(self):
            self.closed = True

    client = Client()
    server._clients.add(client)

    server._handle_client(client)

    assert client.closed
    assert client not in server._clients
    assert client.reads == len(commands) == 1
