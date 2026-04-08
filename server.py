import argparse
import json
import os
import socket
import ssl
import tempfile
import threading
from pathlib import Path

DEFAULT_HOST = os.getenv("RES_SERVER_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("RES_SERVER_PORT", "5000"))
DEFAULT_CERT_FILE = os.getenv("RES_SERVER_CERT", "server.crt")
DEFAULT_KEY_FILE = os.getenv("RES_SERVER_KEY", "server.key")
DEFAULT_SEAT_COUNT = int(os.getenv("RES_SEAT_COUNT", "10"))
DEFAULT_STATE_FILE = os.getenv("RES_STATE_FILE", "seats.json")
DEFAULT_HANDSHAKE_TIMEOUT = float(os.getenv("RES_HANDSHAKE_TIMEOUT", "10"))
DEFAULT_IDLE_TIMEOUT = float(os.getenv("RES_IDLE_TIMEOUT", "300"))
DEFAULT_REQUIRE_CA_SIGNED = os.getenv("RES_REQUIRE_CA_SIGNED", "0") not in {"0", "false", "False"}

seats = {}
seat_locks = {}
ip_to_client_id = {}
client_counter = 0
mapping_lock = threading.Lock()
state_lock = threading.Lock()
state_file = Path(DEFAULT_STATE_FILE)


def parse_args():
    parser = argparse.ArgumentParser(description="Secure reservation server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host/IP to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind")
    parser.add_argument("--cert", default=DEFAULT_CERT_FILE, help="Path to server certificate file")
    parser.add_argument("--key", default=DEFAULT_KEY_FILE, help="Path to server private key file")
    parser.add_argument("--seats", type=int, default=DEFAULT_SEAT_COUNT, help="Number of reservable seats")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Path to persistent JSON state file")
    parser.add_argument(
        "--handshake-timeout",
        type=float,
        default=DEFAULT_HANDSHAKE_TIMEOUT,
        help="Seconds to allow for TLS handshake before closing the connection",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT,
        help="Seconds to wait for client commands before closing the connection",
    )
    parser.add_argument(
        "--allow-self-signed",
        action="store_true",
        help="Allow startup with a self-signed certificate",
    )
    return parser.parse_args()


def init_seats(seat_count):
    global seats, seat_locks
    seats = {str(index): {"status": "available", "owner": None} for index in range(1, seat_count + 1)}
    seat_locks = {seat_id: threading.Lock() for seat_id in seats}


def load_state(path, seat_count):
    global client_counter, ip_to_client_id
    if not path.exists():
        init_seats(seat_count)
        return

    try:
        with path.open("r", encoding="utf-8") as state_file_handle:
            state = json.load(state_file_handle)
    except Exception:
        init_seats(seat_count)
        return

    init_seats(seat_count)
    saved_seats = state.get("seats", {})
    for seat_id, seat_data in saved_seats.items():
        if seat_id in seats and isinstance(seat_data, dict):
            seats[seat_id]["status"] = seat_data.get("status", "available")
            seats[seat_id]["owner"] = seat_data.get("owner")

    saved_mapping = state.get("ip_to_client_id", {})
    if isinstance(saved_mapping, dict):
        ip_to_client_id = {str(key): str(value) for key, value in saved_mapping.items()}

    saved_counter = state.get("client_counter", 0)
    client_counter = saved_counter if isinstance(saved_counter, int) and saved_counter >= 0 else 0


def save_state(path):
    with mapping_lock:
        mapping_snapshot = dict(ip_to_client_id)
        counter_snapshot = client_counter

    seat_snapshot = {seat_id: dict(seat_data) for seat_id, seat_data in seats.items()}
    snapshot = {
        "seats": seat_snapshot,
        "ip_to_client_id": mapping_snapshot,
        "client_counter": counter_snapshot,
    }

    with state_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as temp_file:
            json.dump(snapshot, temp_file, indent=2, sort_keys=True)
            temp_name = temp_file.name
        os.replace(temp_name, path)


def get_client_id(peer_address):
    global client_counter
    peer_ip = peer_address[0]
    created_mapping = False
    with mapping_lock:
        if peer_ip not in ip_to_client_id:
            client_counter += 1
            ip_to_client_id[peer_ip] = f"User{client_counter}"
            created_mapping = True
        client_id = ip_to_client_id[peer_ip]

    if created_mapping:
        save_state(state_file)

    return client_id


def send_line(stream, message):
    stream.write((message + "\n").encode("utf-8"))
    stream.flush()


def handle_client(secure_socket, peer_address, idle_timeout):
    client_id = get_client_id(peer_address)
    print(f"[*] {client_id} connected from {peer_address[0]}")

    try:
        secure_socket.settimeout(idle_timeout)
        with secure_socket.makefile("rwb") as stream:
            send_line(stream, f"WELCOME {client_id}")

            while True:
                try:
                    raw_line = stream.readline()
                except socket.timeout:
                    send_line(stream, "ERROR: Connection timed out.")
                    break
                if not raw_line:
                    break

                command_line = raw_line.decode("utf-8", errors="replace").strip()
                if not command_line:
                    continue

                parts = command_line.split()
                command = parts[0].upper()

                if command == "VIEW":
                    send_line(stream, json.dumps(seats))
                    continue

                if command == "EXIT":
                    send_line(stream, "GOODBYE")
                    break

                if len(parts) != 2 or command not in {"BOOK", "CANCEL"}:
                    send_line(stream, "ERROR: Unknown command.")
                    continue

                seat_id = parts[1]
                if seat_id not in seat_locks:
                    send_line(stream, "ERROR: Invalid seat ID.")
                    continue

                with seat_locks[seat_id]:
                    seat = seats[seat_id]
                    if command == "BOOK":
                        if seat["status"] == "available":
                            seat["status"] = "booked"
                            seat["owner"] = client_id
                            save_state(state_file)
                            send_line(stream, f"SUCCESS: Seat {seat_id} reserved.")
                        else:
                            send_line(stream, f"FAIL: Seat {seat_id} is taken.")
                    elif seat["owner"] == client_id:
                        seat["status"] = "available"
                        seat["owner"] = None
                        save_state(state_file)
                        send_line(stream, f"SUCCESS: Seat {seat_id} released.")
                    else:
                        send_line(stream, "FAIL: Ownership mismatch.")

    except ssl.SSLError as exc:
        print(f"[!] TLS error for {client_id}: {exc}")
    except socket.timeout:
        print(f"[!] Idle timeout for {client_id}")
    except Exception as exc:
        print(f"[!] Error with {client_id}: {exc}")
    finally:
        secure_socket.close()
        print(f"[-] {client_id} disconnected")


def start_server(host, port, cert_file, key_file, allow_self_signed, handshake_timeout, idle_timeout):
    cert_path = Path(cert_file)
    key_path = Path(key_file)
    if not cert_path.exists():
        raise FileNotFoundError(f"Certificate file not found: {cert_path}")
    if not key_path.exists():
        raise FileNotFoundError(f"Private key file not found: {key_path}")

    if DEFAULT_REQUIRE_CA_SIGNED and not allow_self_signed:
        cert_info = ssl._ssl._test_decode_cert(str(cert_path))
        if cert_info.get("subject") == cert_info.get("issuer"):
            raise ValueError(
                "Refusing to start with a self-signed certificate. Pass --allow-self-signed or provide a CA-signed cert."
            )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((host, port))
    server_socket.listen(10)

    print(f"[*] Secure server running on {host}:{port}")

    while True:
        client_socket, peer_address = server_socket.accept()
        client_socket.settimeout(handshake_timeout)
        try:
            secure_socket = context.wrap_socket(client_socket, server_side=True)
        except ssl.SSLError as exc:
            print(f"[!] SSL handshake failed from {peer_address[0]}: {exc}")
            client_socket.close()
            continue
        except socket.timeout:
            print(f"[!] SSL handshake timed out from {peer_address[0]}")
            client_socket.close()
            continue

        thread = threading.Thread(
            target=handle_client,
            args=(secure_socket, peer_address, idle_timeout),
            daemon=True,
        )
        thread.start()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.seats < 1:
        raise ValueError("--seats must be >= 1")

    state_file = Path(arguments.state_file)
    load_state(state_file, arguments.seats)
    save_state(state_file)
    start_server(
        host=arguments.host,
        port=arguments.port,
        cert_file=arguments.cert,
        key_file=arguments.key,
        allow_self_signed=arguments.allow_self_signed,
        handshake_timeout=arguments.handshake_timeout,
        idle_timeout=arguments.idle_timeout,
    )