# -*- coding: UTF-8 -*-
"""EC200U QuecPython cellular-network and outbound TCP test."""

import sim
import net
import dataCall
import checkNet
import usocket
from tcp_client import build_wrapper_frame


# Change these values when you are ready to test your own TCP server.
SERVER_HOST = "65.20.77.137"
SERVER_PORT = 5000
NETWORK_WAIT_SECONDS = 60
SOCKET_TIMEOUT_SECONDS = 15

PROJECT_NAME = "EC200U_NETWORK_TEST"
PROJECT_VERSION = "1.0.0"


def wait_for_network(timeout_seconds):
    """Support both older and newer QuecPython checkNet APIs."""
    if hasattr(checkNet, "waitNetworkReady"):
        return checkNet.waitNetworkReady(timeout_seconds)

    checker = checkNet.CheckNetwork(PROJECT_NAME, PROJECT_VERSION)
    return checker.wait_network_connected(timeout_seconds)


def get_active_address_family():
    """Print network details and return the active socket address family."""
    sim_status = sim.getStatus()
    print("SIM status:", sim_status)
    if sim_status != 1:
        print("ERROR: SIM is not ready. Expected SIM status 1.")
        return False

    print("Signal CSQ:", net.csqQueryPoll())
    print("Operator:", net.operatorName())
    print("Registration state:", net.getState())

    print("Waiting for network, timeout = %d seconds..." % NETWORK_WAIT_SECONDS)
    stage, state = wait_for_network(NETWORK_WAIT_SECONDS)
    print("Network check result: stage=%s, state=%s" % (stage, state))

    if stage != 3 or state != 1:
        print("ERROR: Cellular data network is not ready.")
        print("Stage 1 = SIM, stage 2 = registration, stage 3 = PDP context.")
        return False

    ipv4_result = dataCall.getInfo(1, 0)
    ipv6_result = dataCall.getInfo(1, 1)
    print("PDP IPv4 information:", ipv4_result)
    print("PDP IPv6 information:", ipv6_result)

    ipv4_info = None
    ipv6_info = None

    if ipv4_result != -1 and len(ipv4_result) >= 3:
        ipv4_info = ipv4_result[2]

    if ipv6_result != -1 and len(ipv6_result) >= 3:
        ipv6_info = ipv6_result[2]

    if ipv4_info is not None and len(ipv4_info) >= 5 and ipv4_info[0] == 1:
        print("Selected network: IPv4")
        print("Module IPv4 address:", ipv4_info[2])
        print("Primary DNS:", ipv4_info[3])
        print("Secondary DNS:", ipv4_info[4])
        return usocket.AF_INET

    if ipv6_info is not None and len(ipv6_info) >= 5 and ipv6_info[0] == 1:
        print("Selected network: IPv6")
        print("Module IPv6 address:", ipv6_info[2])
        print("Primary DNS:", ipv6_info[3])
        print("Secondary DNS:", ipv6_info[4])
        return usocket.AF_INET6

    print("ERROR: PDP context 1 has no active IPv4 or IPv6 connection.")
    return None


def test_tcp_client(host, port, address_family):
    """Resolve a host, connect by TCP, and read one response block."""
    sock = None

    try:
        print("Resolving %s:%d..." % (host, port))
        address_info = usocket.getaddrinfo(host, port, address_family)
        if not address_info:
            print("TCP TEST FAILED: DNS returned no matching address.")
            return False

        resolved_family = address_info[0][0]
        server_address = address_info[0][-1]
        print("Resolved server address:", server_address)

        sock = usocket.socket(resolved_family, usocket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT_SECONDS)

        print("Connecting...")
        sock.connect(server_address)
        print("TCP CONNECTED")

        request = build_wrapper_frame(16, 1, b"NETWORK_CHECK")

        sent = sock.write(request)
        print("Sent bytes:", sent)

        response = sock.recv(512)
        print("Received bytes:", len(response))
        print("Response:", response)
        print("TCP TEST PASSED")
        return True

    except Exception as error:
        print("TCP TEST FAILED:", error)
        return False

    finally:
        if sock is not None:
            sock.close()
            print("Socket closed")


def main():
    print("=== EC200U QuecPython network test ===")

    address_family = get_active_address_family()
    if address_family is None:
        print("Network test stopped.")
        return

    test_tcp_client(SERVER_HOST, SERVER_PORT, address_family)


if __name__ == "__main__":
    main()
