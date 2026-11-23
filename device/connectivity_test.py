# -*- coding: UTF-8 -*-
"""EC200U QuecPython connectivity test.

Reports SIM, registration and PDP state, then tries TCP connects to
well-known public hosts (IPv6 and IPv4) and to the bridge server, so a
failure can be pinned to: no data bearer, no IPv6 route, DNS, or the
server/firewall itself. Upload to /usr and run with:
    import example; example.exec('/usr/connectivity_test.py')
"""

import sim
import net
import dataCall
import checkNet
import usocket
import utime

SERVER_HOST = "2401:c080:2400:1480:5400:06ff:fe3d:7402"
SERVER_PORT = 5000
CONNECT_TIMEOUT_SECONDS = 8
NETWORK_WAIT_SECONDS = 30

# (label, host, port, family)
TARGETS = [
    ("Google DNS v6", "2001:4860:4860::8888", 53, usocket.AF_INET6),
    ("Cloudflare DNS v6", "2606:4700:4700::1111", 53, usocket.AF_INET6),
    ("ipv6.google.com (DNS + v6)", "ipv6.google.com", 80, usocket.AF_INET6),
    ("Bridge server", SERVER_HOST, SERVER_PORT, usocket.AF_INET6),
    ("Bridge server SSH", SERVER_HOST, 22, usocket.AF_INET6),
    ("Google DNS v4", "8.8.8.8", 53, usocket.AF_INET),
]


def stop_watchdog():
    """main.py arms a 60 s hardware watchdog that survives Ctrl+C."""
    try:
        from machine import WDT
        WDT(60).stop()
        print("Watchdog stopped")
    except Exception as error:
        print("Watchdog stop not available:", error)


def wait_for_network():
    if hasattr(checkNet, "waitNetworkReady"):
        return checkNet.waitNetworkReady(NETWORK_WAIT_SECONDS)
    checker = checkNet.CheckNetwork("EC200U_CONN_TEST", "1.0.0")
    return checker.wait_network_connected(NETWORK_WAIT_SECONDS)


def report_network():
    print("SIM status:", sim.getStatus(), "(1 = ready)")
    print("Signal CSQ:", net.csqQueryPoll())
    print("Operator:", net.operatorName())
    print("Registration:", net.getState())
    stage, state = wait_for_network()
    print("Network check: stage=%s state=%s (ready = 3, 1)" % (stage, state))
    for ip_type, name in ((0, "IPv4"), (1, "IPv6")):
        try:
            print("PDP %s:" % name, dataCall.getInfo(1, ip_type))
        except Exception as error:
            print("PDP %s query failed:" % name, error)
    try:
        print("PDP profile 1 (type, APN, user, pass, auth):", dataCall.getPDPContext(1))
    except Exception:
        pass
    return stage == 3 and state == 1


def tcp_connect(label, host, port, family):
    sock = None
    start = utime.ticks_ms()
    try:
        info = usocket.getaddrinfo(host, port, family, usocket.SOCK_STREAM)
        matches = [entry for entry in info if entry[0] == family]
        if not matches:
            return "FAIL  no %s address from DNS: %s" % (
                "IPv6" if family == usocket.AF_INET6 else "IPv4", info)
        address = matches[0][-1]
        sock = usocket.socket(family, usocket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_SECONDS)
        sock.connect(address)
        return "OK    %s in %d ms" % (address, utime.ticks_diff(utime.ticks_ms(), start))
    except Exception as error:
        return "FAIL  %r after %d ms" % (error, utime.ticks_diff(utime.ticks_ms(), start))
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def main():
    print("=== EC200U connectivity test ===")
    stop_watchdog()
    if not report_network():
        print("Data network not ready; TCP tests skipped.")
        return

    results = []
    for label, host, port, family in TARGETS:
        print("Testing %s [%s]:%d ..." % (label, host, port))
        result = tcp_connect(label, host, port, family)
        print("  " + result)
        results.append((label, port, result))

    print("=== Summary ===")
    for label, port, result in results:
        print("%-28s :%-5d %s" % (label, port, result.split("  ")[0]))
    print("=== Done ===")


main()
