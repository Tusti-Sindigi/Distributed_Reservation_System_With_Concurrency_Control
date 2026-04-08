import argparse
import json
import socket
import ssl

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
DEFAULT_CA_CERT = "ca.crt"
DEFAULT_TIMEOUT = 30.0


def parse_args():
    parser = argparse.ArgumentParser(description="Secure reservation client")
    parser.add_argument("--host", default=None, help="Server IP or hostname")
    parser.add_argument("--port", type=int, default=None, help="Server port")
    parser.add_argument("--ca-cert", default=None, help="Path to the CA certificate for verification")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Connection and read timeout in seconds")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip certificate verification for local testing only",
    )
    return parser.parse_args()


def prompt_value(prompt, default):
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def build_ssl_context(ca_cert, insecure):
    if insecure:
        context = ssl._create_unverified_context()
        context.check_hostname = False
        return context

    if not ca_cert:
        raise ValueError("CA certificate is required unless --insecure is used")

    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = False
    context.load_verify_locations(cafile=ca_cert)
    return context


def print_response(response):
    if not response:
        return

    try:
        parsed = json.loads(response)
    except json.JSONDecodeError:
        print(response)
        return

    if isinstance(parsed, dict):
        print(format_seat_map(parsed))
    else:
        print(parsed)


def format_seat_map(seats):
    seat_ids = sorted(seats.keys(), key=lambda value: int(value) if str(value).isdigit() else str(value))
    lines = []
    lines.append("Seat  Status    Owner")
    lines.append("----  --------  -----")

    for seat_id in seat_ids:
        seat = seats[seat_id]
        status = str(seat.get("status", "")).ljust(8)
        owner = seat.get("owner") or "-"
        lines.append(f"{str(seat_id).ljust(4)}  {status}  {owner}")

    return "\n".join(lines)


def main():
    arguments = parse_args()
    host = arguments.host or prompt_value("Server IP or hostname", DEFAULT_HOST)
    port = arguments.port if arguments.port is not None else int(prompt_value("Server port", str(DEFAULT_PORT)))
    ca_cert = None
    if not arguments.insecure:
        ca_cert = arguments.ca_cert or prompt_value("CA certificate path", DEFAULT_CA_CERT)
    attempt_insecure = arguments.insecure

    while True:
        try:
            context = build_ssl_context(ca_cert, attempt_insecure)
            with socket.create_connection((host, port), timeout=arguments.timeout) as raw_socket:
                with context.wrap_socket(raw_socket, server_hostname=host) as secure_socket:
                    secure_socket.settimeout(arguments.timeout)
                    with secure_socket.makefile("rwb") as stream:
                        welcome = stream.readline().decode("utf-8", errors="replace").strip()
                        if welcome:
                            print(welcome)

                        print("Type VIEW, BOOK <id>, CANCEL <id>, or EXIT")

                        while True:
                            try:
                                command = input("> ").strip()
                            except (EOFError, KeyboardInterrupt):
                                command = "EXIT"
                                print()

                            if not command:
                                continue

                            stream.write((command + "\n").encode("utf-8"))
                            stream.flush()

                            response = stream.readline().decode("utf-8", errors="replace").strip()
                            if not response:
                                break

                            print_response(response)
                            if command.upper() == "EXIT":
                                break
                    return
        except (ValueError, FileNotFoundError) as exc:
            if not attempt_insecure:
                print(f"TLS setup failed ({exc}). Retrying with --insecure...")
                attempt_insecure = True
                continue
            raise
        except ssl.SSLError as exc:
            if not attempt_insecure:
                print(f"TLS verification failed ({exc}). Retrying with --insecure...")
                attempt_insecure = True
                continue
            raise


if __name__ == "__main__":
    main()