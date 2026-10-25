# -*- coding: UTF-8 -*-
"""Persistent raw TCP client for the Quectel EC200U QuecPython runtime."""

import checkNet
import dataCall
import usocket
import ustruct
import utime
from machine import Pin


SERVER_HOST = "65.20.77.137"
SERVER_PORT = 5000
DEVICE_ID = "EC200U_001"

NETWORK_WAIT_SECONDS = 60
SOCKET_TIMEOUT_SECONDS = 45
RECONNECT_DELAY_SECONDS = 5
RECEIVE_SIZE = 1024
MAX_WRAPPER_PAYLOAD = 4096

DLMS_WRAPPER_VERSION = 1
DTR_ACTIVE_LEVEL = 1

PROJECT_NAME = "EC200U_TCP_CLIENT"
PROJECT_VERSION = "1.0.0"


def configure_dtr_gpio():
    """Configure QuecPython GPIO4 as an output and hold it high."""
    dtr_pin = Pin(
        Pin.GPIO4,
        Pin.OUT,
        Pin.PULL_DISABLE,
        DTR_ACTIVE_LEVEL,
    )
    dtr_pin.write(DTR_ACTIVE_LEVEL)
    print("DTR GPIO4 configured HIGH, level:", dtr_pin.read())
    return dtr_pin


def wait_for_network(timeout_seconds):
    """Support old and new QuecPython checkNet APIs."""
    if hasattr(checkNet, "waitNetworkReady"):
        return checkNet.waitNetworkReady(timeout_seconds)

    checker = checkNet.CheckNetwork(PROJECT_NAME, PROJECT_VERSION)
    return checker.wait_network_connected(timeout_seconds)


def get_active_address_family():
    """Prefer an active IPv4 bearer and fall back to IPv6."""
    ipv4_result = dataCall.getInfo(1, 0)
    if ipv4_result != -1 and len(ipv4_result) >= 3:
        ipv4_info = ipv4_result[2]
        if len(ipv4_info) >= 3 and ipv4_info[0] == 1:
            print("Using IPv4:", ipv4_info[2])
            return usocket.AF_INET

    ipv6_result = dataCall.getInfo(1, 1)
    if ipv6_result != -1 and len(ipv6_result) >= 3:
        ipv6_info = ipv6_result[2]
        if len(ipv6_info) >= 3 and ipv6_info[0] == 1:
            print("Using IPv6:", ipv6_info[2])
            return usocket.AF_INET6

    return None


def connect_to_server(address_family):
    """Resolve and connect to the configured TCP server."""
    address_info = usocket.getaddrinfo(
        SERVER_HOST,
        SERVER_PORT,
        address_family,
    )

    if not address_info:
        raise OSError("DNS returned no matching server address")

    resolved_family = address_info[0][0]
    server_address = address_info[0][-1]
    print("Connecting to:", server_address)

    sock = usocket.socket(resolved_family, usocket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)
    sock.connect(server_address)
    print("TCP CONNECTED")
    return sock


def uint16_from_bytes(data, offset):
    """Read one unsigned 16-bit big-endian integer."""
    return (data[offset] << 8) | data[offset + 1]


def build_wrapper_frame(source_wport, destination_wport, payload):
    """Build an IEC 62056-47 DLMS Wrapper frame."""
    payload_length = len(payload)
    if payload_length > MAX_WRAPPER_PAYLOAD:
        raise ValueError("DLMS Wrapper payload is too large")

    header = ustruct.pack(
        ">HHHH",
        DLMS_WRAPPER_VERSION,
        source_wport,
        destination_wport,
        payload_length,
    )
    return header + payload


def receive_wrapper_frames(sock):
    """Receive complete wrapper frames across arbitrary TCP chunk boundaries."""
    receive_buffer = b""

    while True:
        chunk = sock.recv(RECEIVE_SIZE)
        if not chunk:
            raise OSError("Server closed the connection")

        print("TCP RX %d bytes:" % len(chunk), chunk)
        receive_buffer += chunk

        while len(receive_buffer) >= 8:
            version = uint16_from_bytes(receive_buffer, 0)
            source_wport = uint16_from_bytes(receive_buffer, 2)
            destination_wport = uint16_from_bytes(receive_buffer, 4)
            payload_length = uint16_from_bytes(receive_buffer, 6)

            if version != DLMS_WRAPPER_VERSION:
                raise ValueError("Unsupported DLMS Wrapper version: %d" % version)

            if payload_length > MAX_WRAPPER_PAYLOAD:
                raise ValueError("DLMS Wrapper payload is too large: %d" % payload_length)

            frame_length = 8 + payload_length
            if len(receive_buffer) < frame_length:
                break

            payload = receive_buffer[8:frame_length]
            receive_buffer = receive_buffer[frame_length:]

            print(
                "WRAPPER RX version=%d source=%d destination=%d length=%d"
                % (version, source_wport, destination_wport, payload_length)
            )
            print("WRAPPER RX payload:", payload)

            response_payload = ("WRAPPER_PONG,%s" % DEVICE_ID).encode()
            response_frame = build_wrapper_frame(
                destination_wport,
                source_wport,
                response_payload,
            )
            sent = sock.write(response_frame)
            print("WRAPPER TX %d bytes:" % sent, response_frame)


def run():
    """Maintain a TCP connection and reconnect after network/socket errors."""
    print("=== EC200U persistent TCP client ===")
    dtr_pin = configure_dtr_gpio()

    while True:
        sock = None

        try:
            if dtr_pin.read() != DTR_ACTIVE_LEVEL:
                dtr_pin.write(DTR_ACTIVE_LEVEL)
                print("DTR GPIO4 reasserted HIGH")

            stage, state = wait_for_network(NETWORK_WAIT_SECONDS)
            if stage != 3 or state != 1:
                print("Network unavailable: stage=%s, state=%s" % (stage, state))
                utime.sleep(RECONNECT_DELAY_SECONDS)
                continue

            address_family = get_active_address_family()
            if address_family is None:
                print("No active IPv4 or IPv6 PDP address")
                utime.sleep(RECONNECT_DELAY_SECONDS)
                continue

            sock = connect_to_server(address_family)
            receive_wrapper_frames(sock)

        except KeyboardInterrupt:
            print("TCP client stopped by user")
            return

        except Exception as error:
            print("TCP connection error:", error)

        finally:
            if sock is not None:
                sock.close()
                print("Socket closed")

        print("Reconnect in %d seconds..." % RECONNECT_DELAY_SECONDS)
        utime.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    run()
