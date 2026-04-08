import argparse
import csv
import json
import socket
import ssl
import threading
import time
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
DEFAULT_CA_CERT = "ca.crt"
DEFAULT_TIMEOUT = 30.0
DEFAULT_OUTPUT_DIR = "benchmarks"
DEFAULT_LOADS = [1, 5, 10, 20, 50]
DEFAULT_INTEGRITY_THREADS = 100
DEFAULT_INTEGRITY_SEAT = "1"


def parse_args():
    parser = argparse.ArgumentParser(description="Stress test the reservation system")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Server IP or hostname")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port")
    parser.add_argument("--ca-cert", default=DEFAULT_CA_CERT, help="Path to the server certificate for verification")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Connection and read timeout in seconds")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip certificate verification for local testing only",
    )
    parser.add_argument(
        "--loads",
        default=",".join(str(load) for load in DEFAULT_LOADS),
        help="Comma-separated client counts for throughput/latency runs",
    )
    parser.add_argument("--integrity-threads", type=int, default=DEFAULT_INTEGRITY_THREADS, help="Threads for the double-booking integrity test")
    parser.add_argument("--integrity-seat", default=DEFAULT_INTEGRITY_SEAT, help="Seat to use for the integrity test")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for benchmark outputs")
    return parser.parse_args()


def build_ssl_context(ca_cert, insecure):
    if insecure:
        context = ssl._create_unverified_context()
        context.check_hostname = False
        return context

    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = False
    context.load_verify_locations(cafile=ca_cert)
    return context


class ClientSession:
    def __init__(self, host, port, context, timeout):
        self.host = host
        self.port = port
        self.context = context
        self.timeout = timeout
        self.raw_socket = None
        self.secure_socket = None
        self.stream = None

    def __enter__(self):
        self.raw_socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.secure_socket = self.context.wrap_socket(self.raw_socket, server_hostname=self.host)
        self.secure_socket.settimeout(self.timeout)
        self.stream = self.secure_socket.makefile("rwb")
        self.stream.readline()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.stream is not None:
                self.stream.write(b"EXIT\n")
                self.stream.flush()
                self.stream.readline()
        except Exception:
            pass
        finally:
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception:
                pass
            try:
                if self.secure_socket is not None:
                    self.secure_socket.close()
            except Exception:
                pass
            try:
                if self.raw_socket is not None:
                    self.raw_socket.close()
            except Exception:
                pass

    def round_trip(self, command):
        started = time.perf_counter()
        self.stream.write((command + "\n").encode("utf-8"))
        self.stream.flush()
        response = self.stream.readline().decode("utf-8", errors="replace").strip()
        elapsed = time.perf_counter() - started
        return response, elapsed


def fetch_seat_ids(host, port, context, timeout):
    with ClientSession(host, port, context, timeout) as session:
        response, _ = session.round_trip("VIEW")
    seats = json.loads(response)
    if not isinstance(seats, dict) or not seats:
        raise ValueError("Server returned an empty or invalid seat list")
    return sorted(seats.keys(), key=lambda value: int(value) if value.isdigit() else value)


def parse_loads(raw_loads):
    loads = []
    for value in raw_loads.split(","):
        value = value.strip()
        if not value:
            continue
        loads.append(int(value))
    if not loads:
        raise ValueError("At least one client load must be provided")
    return loads


def run_batch(host, port, context, timeout, client_count):
    barrier = threading.Barrier(client_count)
    results = []
    results_lock = threading.Lock()
    seat_ids = fetch_seat_ids(host, port, context, timeout)

    def worker(index):
        seat_id = seat_ids[index % len(seat_ids)]
        try:
            with ClientSession(host, port, context, timeout) as session:
                barrier.wait(timeout=timeout)
                book_response, book_latency = session.round_trip(f"BOOK {seat_id}")
                cancel_response = ""
                cancel_latency = 0.0
                if book_response.startswith("SUCCESS"):
                    cancel_response, cancel_latency = session.round_trip(f"CANCEL {seat_id}")
                with results_lock:
                    results.append(
                        {
                            "seat_id": seat_id,
                            "book_response": book_response,
                            "cancel_response": cancel_response,
                            "book_latency": book_latency,
                            "cancel_latency": cancel_latency,
                        }
                    )
        except Exception as exc:
            with results_lock:
                results.append(
                    {
                        "seat_id": seat_id,
                        "book_response": f"ERROR: {exc.__class__.__name__}: {exc}",
                        "cancel_response": "",
                        "book_latency": 0.0,
                        "cancel_latency": 0.0,
                    }
                )

    start = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(index,)) for index in range(client_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - start

    successful_books = sum(1 for item in results if item["book_response"].startswith("SUCCESS"))
    successful_cancels = sum(1 for item in results if item["cancel_response"].startswith("SUCCESS"))
    total_operations = client_count + successful_cancels
    average_book_latency = sum(item["book_latency"] for item in results) / len(results)
    average_cancel_latency = (
        sum(item["cancel_latency"] for item in results if item["cancel_latency"] > 0.0) / successful_cancels
        if successful_cancels
        else 0.0
    )
    throughput = total_operations / elapsed if elapsed > 0 else 0.0

    return {
        "clients": client_count,
        "elapsed_seconds": elapsed,
        "successful_books": successful_books,
        "successful_cancels": successful_cancels,
        "average_book_latency_seconds": average_book_latency,
        "average_cancel_latency_seconds": average_cancel_latency,
        "throughput_ops_per_second": throughput,
    }


def run_integrity_test(host, port, context, timeout, thread_count, seat_id):
    barrier = threading.Barrier(thread_count)
    results = []
    results_lock = threading.Lock()

    def worker():
        try:
            with ClientSession(host, port, context, timeout) as session:
                barrier.wait()
                response, _ = session.round_trip(f"BOOK {seat_id}")
                with results_lock:
                    results.append(response)
        except Exception as exc:
            with results_lock:
                results.append(f"ERROR: {exc.__class__.__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    success_count = sum(1 for response in results if response.startswith("SUCCESS"))
    failure_count = sum(1 for response in results if response.startswith("FAIL"))
    error_count = sum(1 for response in results if response.startswith("ERROR"))

    cleanup_response = ""
    try:
        with ClientSession(host, port, context, timeout) as session:
            cleanup_response, _ = session.round_trip(f"CANCEL {seat_id}")
    except Exception as exc:
        cleanup_response = f"ERROR: {exc.__class__.__name__}: {exc}"

    return {
        "thread_count": thread_count,
        "seat_id": seat_id,
        "success_count": success_count,
        "failure_count": failure_count,
        "error_count": error_count,
        "cleanup_response": cleanup_response,
        "zero_double_booking": success_count == 1,
    }


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _svg_escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def save_svg_chart(path, title, x_label, y_label, points):
    width = 900
    height = 560
    margin_left = 90
    margin_right = 40
    margin_top = 70
    margin_bottom = 80
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    min_x, max_x = min(x_values), max(x_values)
    min_y, max_y = min(y_values), max(y_values)
    if min_y == max_y:
        max_y = min_y + 1.0

    def scale_x(value):
        if max_x == min_x:
            return margin_left + plot_width / 2
        return margin_left + ((value - min_x) / (max_x - min_x)) * plot_width

    def scale_y(value):
        return margin_top + plot_height - ((value - min_y) / (max_y - min_y)) * plot_height

    x_axis_y = margin_top + plot_height
    y_axis_x = margin_left
    x_ticks = sorted(set(x_values))
    y_ticks = 5
    y_step = (max_y - min_y) / y_ticks

    polyline = " ".join(f"{scale_x(x):.2f},{scale_y(y):.2f}" for x, y in points)
    circles = "\n".join(
        f'<circle cx="{scale_x(x):.2f}" cy="{scale_y(y):.2f}" r="5" fill="#0f766e" />'
        for x, y in points
    )

    x_tick_lines = []
    for x in x_ticks:
        px = scale_x(x)
        x_tick_lines.append(
            f'<line x1="{px:.2f}" y1="{x_axis_y}" x2="{px:.2f}" y2="{x_axis_y + 6}" stroke="#334155" />'
        )
        x_tick_lines.append(
            f'<text x="{px:.2f}" y="{x_axis_y + 26}" text-anchor="middle" font-size="14" fill="#334155">{x}</text>'
        )

    y_tick_lines = []
    for index in range(y_ticks + 1):
        value = min_y + y_step * index
        py = scale_y(value)
        y_tick_lines.append(
            f'<line x1="{y_axis_x - 6}" y1="{py:.2f}" x2="{y_axis_x}" y2="{py:.2f}" stroke="#334155" />'
        )
        y_tick_lines.append(
            f'<text x="{y_axis_x - 12}" y="{py + 5:.2f}" text-anchor="end" font-size="14" fill="#334155">{value:.2f}</text>'
        )
        y_tick_lines.append(
            f'<line x1="{y_axis_x}" y1="{py:.2f}" x2="{margin_left + plot_width}" y2="{py:.2f}" stroke="#e2e8f0" />'
        )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#f8fafc" />
  <text x="{width / 2}" y="36" text-anchor="middle" font-size="24" font-weight="700" fill="#0f172a">{_svg_escape(title)}</text>
  <text x="{width / 2}" y="{height - 24}" text-anchor="middle" font-size="16" fill="#334155">{_svg_escape(x_label)}</text>
  <text x="26" y="{height / 2}" text-anchor="middle" font-size="16" fill="#334155" transform="rotate(-90 26 {height / 2})">{_svg_escape(y_label)}</text>
  <line x1="{y_axis_x}" y1="{margin_top}" x2="{y_axis_x}" y2="{x_axis_y}" stroke="#0f172a" stroke-width="2" />
  <line x1="{y_axis_x}" y1="{x_axis_y}" x2="{margin_left + plot_width}" y2="{x_axis_y}" stroke="#0f172a" stroke-width="2" />
  {''.join(y_tick_lines)}
  {''.join(x_tick_lines)}
  <polyline points="{polyline}" fill="none" stroke="#0f766e" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" />
  {circles}
</svg>
'''
    path.write_text(svg, encoding="utf-8")


def save_plots(output_dir, load_rows):
    clients = [row["clients"] for row in load_rows]
    latencies = [row["average_book_latency_seconds"] * 1000.0 for row in load_rows]
    throughputs = [row["throughput_ops_per_second"] for row in load_rows]

    latency_plot = output_dir / "latency_vs_clients.svg"
    throughput_plot = output_dir / "throughput_vs_clients.svg"

    save_svg_chart(
        latency_plot,
        "Average BOOK Latency vs. Number of Clients",
        "Clients",
        "Average BOOK Latency (ms)",
        list(zip(clients, latencies)),
    )
    save_svg_chart(
        throughput_plot,
        "Throughput vs. Number of Clients",
        "Clients",
        "Operations per Second",
        list(zip(clients, throughputs)),
    )

    return [str(latency_plot), str(throughput_plot)]


def main():
    arguments = parse_args()
    loads = parse_loads(arguments.loads)
    output_dir = Path(arguments.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    context = build_ssl_context(arguments.ca_cert, arguments.insecure)

    load_rows = []
    for client_count in loads:
        print(f"Running load test with {client_count} clients...")
        result = run_batch(arguments.host, arguments.port, context, arguments.timeout, client_count)
        load_rows.append(result)
        print(json.dumps(result, indent=2))

    integrity_result = run_integrity_test(
        arguments.host,
        arguments.port,
        context,
        arguments.timeout,
        arguments.integrity_threads,
        arguments.integrity_seat,
    )
    print("Integrity test:")
    print(json.dumps(integrity_result, indent=2))

    summary = {
        "load_results": load_rows,
        "integrity_result": integrity_result,
    }

    summary_path = output_dir / "benchmark_results.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_csv(output_dir / "benchmark_results.csv", load_rows)
    generated_plots = save_plots(output_dir, load_rows)

    print(f"Results written to {summary_path}")
    print(f"Plots written to {', '.join(generated_plots)}")
    if integrity_result["zero_double_booking"]:
        print("Integrity check passed: zero double-bookings observed.")
    else:
        print("Integrity check failed: more than one booking succeeded.")


if __name__ == "__main__":
    main()
