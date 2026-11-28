# -*- coding: UTF-8 -*-
"""Find out how (or whether) the EC200U can send outbound to the VM.

The heartbeat failed with "OSError [Errno 113] EHOSTUNREACH" at send().
This tries every reasonable way of sending one UDP packet to the VM, plus
one outbound TCP connect, and prints PASS / FAIL for each. Stop main.py
first (Ctrl+C in the REPL), upload this to /usr and run:

    import example; example.exec('/usr/heartbeat_route_test.py')

Run it on the VM at the same time to see which packets really arrive:

    sudo tcpdump -ni any udp port 4060 or tcp port 22

The packets are plain text ("route-test ..."): the server logs them as a
bad heartbeat packet and ignores them.
"""

import usocket
import dataCall
import utime

VM = "2401:c080:2400:1480:5400:06ff:fe3d:7402"
UDP_PORT = 4060
TCP_PORT = 22           # sshd: outbound TCP to the whitelisted VM
ROUNDS = 3              # repeat with a pause, to rule out "too early"
PAUSE_S = 10


def own_ipv6():
    info = dataCall.getInfo(1, 1)
    print("dataCall.getInfo(1, 1):", info)
    try:
        ip = info[2]
        if ip[0] == 1 and ip[2]:
            return ip[2]
    except Exception:
        pass
    return None


def attempt(name, fn):
    # Each fn() opens and closes its own socket
    try:
        fn()
        print("PASS", name)
    except Exception as e:
        print("FAIL", name, "->", type(e).__name__, e)


def udp_connect_send(tag):
    # What heartbeat.py does today
    s = usocket.socket(usocket.AF_INET6, usocket.SOCK_DGRAM)
    try:
        s.connect((VM, UDP_PORT))
        s.send(tag)
    finally:
        s.close()


def udp_sendto(tag):
    s = usocket.socket(usocket.AF_INET6, usocket.SOCK_DGRAM)
    try:
        s.sendto(tag, (VM, UDP_PORT))
    finally:
        s.close()


def udp_bind_sendto(tag, ipv6):
    s = usocket.socket(usocket.AF_INET6, usocket.SOCK_DGRAM)
    try:
        s.bind((ipv6, 0))
        s.sendto(tag, (VM, UDP_PORT))
    finally:
        s.close()


def udp_bind_connect_send(tag, ipv6):
    s = usocket.socket(usocket.AF_INET6, usocket.SOCK_DGRAM)
    try:
        s.bind((ipv6, 0))
        s.connect((VM, UDP_PORT))
        s.send(tag)
    finally:
        s.close()


def udp_getaddrinfo_sendto(tag):
    s = usocket.socket(usocket.AF_INET6, usocket.SOCK_DGRAM)
    try:
        addr = usocket.getaddrinfo(VM, UDP_PORT)[0][-1]
        s.sendto(tag, addr)
    finally:
        s.close()


def tcp_connect():
    s = usocket.socket(usocket.AF_INET6, usocket.SOCK_STREAM)
    try:
        s.settimeout(15)
        s.connect((VM, TCP_PORT))
    finally:
        s.close()


def main():
    print("=== heartbeat route test ===")
    ipv6 = own_ipv6()
    print("Module IPv6:", ipv6)
    if not ipv6:
        print("No IPv6 data call - nothing to test")
        return

    for n in range(1, ROUNDS + 1):
        print("\n--- round %d of %d ---" % (n, ROUNDS))
        t = "route-test r%d " % n
        attempt("1 udp connect+send  (heartbeat.py today)",
                lambda: udp_connect_send((t + "1").encode()))
        attempt("2 udp sendto",
                lambda: udp_sendto((t + "2").encode()))
        attempt("3 udp bind(own ip)+sendto",
                lambda: udp_bind_sendto((t + "3").encode(), ipv6))
        attempt("4 udp bind(own ip)+connect+send",
                lambda: udp_bind_connect_send((t + "4").encode(), ipv6))
        attempt("5 udp getaddrinfo+sendto",
                lambda: udp_getaddrinfo_sendto((t + "5").encode()))
        attempt("6 tcp connect to VM port %d" % TCP_PORT, tcp_connect)
        if n < ROUNDS:
            utime.sleep(PAUSE_S)

    print("\n=== done: send the whole output, and whether tcpdump on the VM saw anything ===")


main()
