import json
import os
import platform
import re
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests
from colorama import Fore, Style, init
from dotenv import load_dotenv
from prometheus_client import Counter, Gauge, Histogram, start_http_server

load_dotenv()
init(autoreset=True)

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "diagnostico_red.log"
JSON_OUTPUT_FILE = BASE_DIR / "diagnostico_red_output.json"

CHECK_RUNS_TOTAL = Counter(
    "network_diagnostic_runs_total",
    "Total number of diagnostic runs executed",
)
CHECKS_TOTAL = Counter(
    "network_diagnostic_checks_total",
    "Total number of diagnostic checks by layer and status",
    ["target", "layer", "status"],
)
PING_LATENCY_MS = Gauge(
    "network_diagnostic_ping_latency_ms",
    "Last observed ping latency in milliseconds",
    ["target"],
)
HTTP_STATUS_CODE = Gauge(
    "network_diagnostic_http_status_code",
    "Last observed HTTP status code",
    ["target"],
)
ROOT_CAUSE_TOTAL = Counter(
    "network_diagnostic_root_cause_total",
    "Total number of inferred root causes",
    ["target", "root_cause"],
)
RUN_DURATION_SECONDS = Histogram(
    "network_diagnostic_run_duration_seconds",
    "Time spent executing one diagnostic cycle",
)


@dataclass(frozen=True)
class Config:
    hosts: List[str]
    interval: int
    use_https: bool
    use_color: bool
    request_timeout: int
    metrics_enabled: bool
    metrics_port: int


@dataclass
class CheckResult:
    layer: str
    ok: bool
    detail: str
    status: str
    latency_ms: Optional[int] = None
    ip: Optional[str] = None
    status_code: Optional[int] = None
    error_type: Optional[str] = None


@dataclass
class HostDiagnostic:
    target: str
    timestamp: str
    network: CheckResult
    dns: CheckResult
    http: CheckResult
    conclusion: str


def load_config() -> Config:
    return Config(
        hosts=parse_hosts(),
        interval=int(os.getenv("INTERVAL", "0")),
        use_https=os.getenv("USE_HTTPS", "true").lower() == "true",
        use_color=os.getenv("USE_COLOR", "true").lower() == "true",
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "5")),
        metrics_enabled=os.getenv("ENABLE_METRICS", "true").lower() == "true",
        metrics_port=int(os.getenv("METRICS_PORT", "8001")),
    )


def ensure_output_dirs() -> None:
    LOGS_DIR.mkdir(exist_ok=True)


def build_ping_command(host: str) -> List[str]:
    if platform.system().lower() == "windows":
        return ["ping", "-n", "1", host]
    return ["ping", "-c", "1", host]


def ping(host: str, timeout: int) -> CheckResult:
    try:
        result = subprocess.run(
            build_ping_command(host),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            layer="NETWORK",
            ok=False,
            detail=f"ping {host} -> FAIL (timeout)",
            status="FAIL",
            error_type="TimeoutExpired",
        )
    except OSError as exc:
        return CheckResult(
            layer="NETWORK",
            ok=False,
            detail=f"ping {host} -> FAIL ({exc})",
            status="FAIL",
            error_type=exc.__class__.__name__,
        )

    if result.returncode != 0:
        return CheckResult(
            layer="NETWORK",
            ok=False,
            detail=f"ping {host} -> FAIL",
            status="FAIL",
        )

    latency_ms = parse_ping_latency(result.stdout)
    if latency_ms is not None:
        return CheckResult(
            layer="NETWORK",
            ok=True,
            detail=f"ping {host} -> OK ({latency_ms}ms)",
            status="OK",
            latency_ms=latency_ms,
        )

    return CheckResult(
        layer="NETWORK",
        ok=True,
        detail=f"ping {host} -> OK",
        status="OK",
    )


def parse_ping_latency(output: str) -> Optional[int]:
    patterns = (
        r"Average = (\d+)ms",
        r"Media = (\d+)ms",
        r"time[=<](\d+)ms",
        r"tiempo[=<](\d+)ms",
        r"time=(\d+(?:\.\d+)?) ms",
    )
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return int(float(match.group(1)))
    return None


def dns_lookup(host: str, timeout: int) -> CheckResult:
    try:
        result = subprocess.run(
            ["nslookup", host],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            ip = parse_nslookup_ip(result.stdout)
            if ip:
                return CheckResult(
                    layer="DNS",
                    ok=True,
                    detail=f"resolve {host} -> OK ({ip})",
                    status="OK",
                    ip=ip,
                )
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        ip = socket.gethostbyname(host)
        return CheckResult(
            layer="DNS",
            ok=True,
            detail=f"resolve {host} -> OK ({ip})",
            status="OK",
            ip=ip,
        )
    except socket.gaierror:
        return CheckResult(
            layer="DNS",
            ok=False,
            detail=f"resolve {host} -> FAIL",
            status="FAIL",
            error_type="gaierror",
        )


def parse_nslookup_ip(output: str) -> Optional[str]:
    lines = output.splitlines()
    in_answer_section = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith(("Name:", "Nombre:")):
            in_answer_section = True
            continue

        if in_answer_section:
            match = re.search(
                r"Addresses?:\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)",
                stripped,
            )
            if match:
                return match.group(1)

    matches = re.findall(r"Addresses?:\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", output)
    if matches:
        return matches[-1]

    return None


def http_check(
    session: requests.Session,
    host: str,
    timeout: int,
    use_https: bool = True,
) -> CheckResult:
    scheme = "https" if use_https else "http"
    url = f"{scheme}://{host}"
    try:
        response = session.get(url, timeout=timeout)
        ok = 200 <= response.status_code < 400
        return CheckResult(
            layer="HTTP",
            ok=ok,
            detail=f"curl {url} -> {'OK' if ok else 'FAIL'} ({response.status_code})",
            status="OK" if ok else "FAIL",
            status_code=response.status_code,
        )
    except requests.Timeout:
        return CheckResult(
            layer="HTTP",
            ok=False,
            detail=f"curl {url} -> FAIL (Timeout)",
            status="FAIL",
            error_type="Timeout",
        )
    except requests.RequestException as exc:
        return CheckResult(
            layer="HTTP",
            ok=False,
            detail=f"curl {url} -> FAIL ({exc.__class__.__name__})",
            status="FAIL",
            error_type=exc.__class__.__name__,
        )


def skipped_http_result(host: str, use_https: bool, reason: str) -> CheckResult:
    scheme = "https" if use_https else "http"
    return CheckResult(
        layer="HTTP",
        ok=False,
        detail=f"curl {scheme}://{host} -> SKIPPED ({reason})",
        status="SKIPPED",
    )


def determine_root_cause(network: CheckResult, dns: CheckResult, http: CheckResult) -> str:
    if not dns.ok:
        return "ROOT CAUSE: DNS RESOLUTION FAILURE"
    if not network.ok and dns.ok:
        return "ROOT CAUSE: NETWORK REACHABILITY FAILURE"
    if network.ok and dns.ok and http.status == "SKIPPED":
        return "ROOT CAUSE: HTTP CHECK SKIPPED DUE TO UPSTREAM FAILURE"
    if network.ok and dns.ok and not http.ok:
        if http.error_type == "ConnectionError":
            return "ROOT CAUSE: SERVICE NOT RUNNING OR PORT CLOSED"
        if http.error_type == "SSLError":
            return "ROOT CAUSE: TLS OR HTTPS MISCONFIGURATION"
        if http.error_type == "Timeout":
            return "ROOT CAUSE: HTTP TIMEOUT OR SLOW SERVICE"
        return "ROOT CAUSE: APPLICATION LAYER FAILURE"
    return "ROOT CAUSE: NO ISSUES DETECTED"


def color_for_status(status: str) -> str:
    if status == "OK":
        return Fore.GREEN
    if status == "FAIL":
        return Fore.RED
    return Fore.YELLOW


def format_result(result: CheckResult, color: bool = True) -> str:
    line = f"[{result.layer}] {result.detail}"
    if not color:
        return line
    return f"{color_for_status(result.status)}{line}{Style.RESET_ALL}"


def format_conclusion(conclusion: str, color: bool = True) -> str:
    if not color:
        return conclusion
    color_code = Fore.GREEN if conclusion.endswith("NO ISSUES DETECTED") else Fore.CYAN
    return f"{color_code}{conclusion}{Style.RESET_ALL}"


def update_metrics(diagnostic: HostDiagnostic) -> None:
    for result in (diagnostic.network, diagnostic.dns, diagnostic.http):
        CHECKS_TOTAL.labels(
            target=diagnostic.target,
            layer=result.layer,
            status=result.status,
        ).inc()

    if diagnostic.network.latency_ms is not None:
        PING_LATENCY_MS.labels(target=diagnostic.target).set(diagnostic.network.latency_ms)

    if diagnostic.http.status_code is not None:
        HTTP_STATUS_CODE.labels(target=diagnostic.target).set(diagnostic.http.status_code)

    ROOT_CAUSE_TOTAL.labels(
        target=diagnostic.target,
        root_cause=diagnostic.conclusion.replace("ROOT CAUSE: ", ""),
    ).inc()


def run_diagnostics(
    session: requests.Session,
    host: str,
    config: Config,
) -> HostDiagnostic:
    timestamp = datetime.now(timezone.utc).isoformat()
    network_result = ping(host, config.request_timeout)
    dns_result = dns_lookup(host, config.request_timeout)

    if not dns_result.ok:
        http_result = skipped_http_result(host, config.use_https, "DNS failed")
    else:
        http_result = http_check(
            session=session,
            host=host,
            timeout=config.request_timeout,
            use_https=config.use_https,
        )

    conclusion = determine_root_cause(network_result, dns_result, http_result)
    diagnostic = HostDiagnostic(
        target=host,
        timestamp=timestamp,
        network=network_result,
        dns=dns_result,
        http=http_result,
        conclusion=conclusion,
    )

    print("\n=== NETWORK DIAGNOSTICS ===")
    print(f"Target: {host}")
    print(format_result(network_result, color=config.use_color))
    print(format_result(dns_result, color=config.use_color))
    print(format_result(http_result, color=config.use_color))
    print(format_conclusion(conclusion, color=config.use_color))

    update_metrics(diagnostic)
    return diagnostic


def write_log(diagnostic: HostDiagnostic) -> None:
    ensure_output_dirs()
    lines = [
        f"[{diagnostic.timestamp}] TARGET={diagnostic.target} {diagnostic.network.detail}",
        f"[{diagnostic.timestamp}] TARGET={diagnostic.target} {diagnostic.dns.detail}",
        f"[{diagnostic.timestamp}] TARGET={diagnostic.target} {diagnostic.http.detail}",
        f"[{diagnostic.timestamp}] TARGET={diagnostic.target} {diagnostic.conclusion}",
    ]
    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")


def write_json_output(diagnostics: List[HostDiagnostic]) -> None:
    payload = [asdict(diagnostic) for diagnostic in diagnostics]
    with JSON_OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)


def parse_hosts() -> List[str]:
    raw_hosts = os.getenv("TARGET_HOSTS")
    if raw_hosts:
        hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
        if hosts:
            return hosts
    return [os.getenv("TARGET_HOST", "google.com")]


def start_metrics_server_if_enabled(config: Config) -> None:
    if not config.metrics_enabled:
        return
    start_http_server(config.metrics_port)
    print(f"Prometheus metrics available at http://localhost:{config.metrics_port}/metrics")


def main() -> None:
    ensure_output_dirs()
    config = load_config()
    start_metrics_server_if_enabled(config)

    with requests.Session() as session:
        while True:
            CHECK_RUNS_TOTAL.inc()
            cycle_start = time.perf_counter()
            diagnostics = []

            for host in config.hosts:
                diagnostic = run_diagnostics(session=session, host=host, config=config)
                diagnostics.append(diagnostic)
                write_log(diagnostic)

            write_json_output(diagnostics)
            RUN_DURATION_SECONDS.observe(time.perf_counter() - cycle_start)

            if config.interval <= 0:
                return

            time.sleep(config.interval)


if __name__ == "__main__":
    main()
