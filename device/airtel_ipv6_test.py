
# Wintek Solutions - EC200U-CN IPv6 TCP Test
# APN: airteliot.com
# Server: Vultr IPv6, TCP port 5000

import utime
import dataCall
import net
import usocket

APN = "airteliot.com"
SERVER_IPV6 = "2401:c080:2400:1480:5400:06ff:fe3d:7402"
SERVER_PORT = 5000

PROFILE_ID = 1
IP_TYPE = 1           # 0=IPv4, 1=IPv6, 2=IPv4v6
TIMEOUT = 10
RETRIES = 3


def configure_apn():
    print("\n[1] Checking Airtel APN")

    current = dataCall.getPDPContext(PROFILE_ID)
    print("Current PDP:", current)

    if current == -1:
        print("FAIL: Cannot read PDP configuration")
        return False

    # Tuple: (ipType, apn, username, password, authType)
    if current[0] == IP_TYPE and current[1] == APN:
        print("PASS: APN and IPv6 type already configured")
        return True

    result = dataCall.setPDPContext(
        PROFILE_ID,
        IP_TYPE,
        APN,
        "",
        "",
        0
    )

    if result == 0:
        print("PASS: APN saved successfully")
        print("IMPORTANT: Reboot the module, then run again.")
    else:
        print("FAIL: APN configuration error:", result)

    return False


def check_registration():
    print("\n[2] Checking LTE registration")

    for attempt in range(30):
        try:
            state = net.getState()
            print("Network state:", state)

            if state != -1:
                data_state = state[1][0]

                if data_state in (1, 5):
                    print("PASS: Registered on cellular network")
                    return True

        except Exception as e:
            print("Registration check error:", e)

        print("Waiting for LTE...", attempt + 1)
        utime.sleep(2)

    print("FAIL: LTE registration timeout")
    return False


def check_ipv6():
    print("\n[3] Checking IPv6 PDP connection")

    for attempt in range(15):
        try:
            info = dataCall.getInfo(PROFILE_ID, 1)
            print("IPv6 PDP info:", info)

            if info != -1:
                details = info[2]
                state = details[0]
                ipv6 = details[2]

                if state == 1 and ipv6:
                    print("PASS: IPv6 connected")
                    print("Module IPv6:", ipv6)
                    return True

        except Exception as e:
            print("IPv6 check error:", e)

        if attempt == 4:
            print("Trying PDP activation...")
            try:
                print("Activate result:",
                      dataCall.activate(PROFILE_ID))
            except Exception as e:
                print("Activation error:", e)

        utime.sleep(2)

    print("FAIL: No active IPv6 data connection")
    return False


def test_tcp():
    print("\n[4] Testing TCP connection")
    print("Server:", SERVER_IPV6)
    print("Port:", SERVER_PORT)

    for attempt in range(1, RETRIES + 1):
        sock = None

        try:
            print("\nTCP attempt:", attempt)

            sock = usocket.socket(
                usocket.AF_INET6,
                usocket.SOCK_STREAM
            )
            sock.settimeout(TIMEOUT)

            print("Connecting to server...")

            sock.connect((SERVER_IPV6, SERVER_PORT))

            print("PASS: TCP IPv6 connection established!")

            message = b"WINTEK_EC200U_IPV6_TEST\r\n"
            count = sock.send(message)

            print("Bytes sent:", count)
            print("Test message sent")

            # No recv required: a TCP listener may not reply.
            return True

        except Exception as e:
            print("TCP test failed:", e)

        finally:
            if sock is not None:
                try:
                    sock.close()
                except:
                    pass

        if attempt < RETRIES:
            utime.sleep(5)

    return False


def main():
    print("=" * 48)
    print("WINTEK SOLUTIONS - EC200U IPv6 TEST")
    print("=" * 48)

    if not configure_apn():
        return

    if not check_registration():
        return

    if not check_ipv6():
        return

    if test_tcp():
        print("\n================================")
        print("RESULT: IPv6 TCP TEST PASSED")
        print("================================")
    else:
        print("\n================================")
        print("RESULT: TCP CONNECTION FAILED")
        print("Check firewall/server/SIM routing")
        print("================================")


main()
