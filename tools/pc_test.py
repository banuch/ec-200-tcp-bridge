"""
pc_test.py - pretend to be an EC200U module with a meter attached, from a PC.

    python pc_test.py [port] [bind-address]      (default 5000 on ::)

Listens like device/main.py does (TCP server, IPv6) and answers every
request with dummy_meter.py. Point server.py at it, for example:

    setmodule TEST ::1 5000
    poll TEST
"""
import socket
import sys

from dummy_meter import DummyMeter

port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
bind = sys.argv[2] if len(sys.argv) > 2 else "::"

server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((bind, port))
server.listen(1)
print("Simulated module listening on [{}]:{} (Ctrl+C to stop)".format(bind, port))

try:
    while True:
        client, address = server.accept()
        print("Server connected from", address[:2])
        meter = DummyMeter()
        with client:
            while True:
                data = client.recv(2048)
                if not data:
                    print("Server closed connection")
                    break
                for reply in meter.feed(data):
                    client.sendall(reply)
except KeyboardInterrupt:
    pass
finally:
    server.close()
