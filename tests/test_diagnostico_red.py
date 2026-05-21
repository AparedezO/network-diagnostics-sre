import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diagnostico_red import CheckResult, build_ping_command, determine_root_cause, parse_nslookup_ip, parse_ping_latency


class ParsePingLatencyTests(unittest.TestCase):
    def test_parses_windows_english_average(self) -> None:
        output = "Minimum = 11ms, Maximum = 13ms, Average = 12ms"
        self.assertEqual(parse_ping_latency(output), 12)

    def test_parses_windows_spanish_media(self) -> None:
        output = "Mínimo = 0ms, Máximo = 0ms, Media = 0ms"
        self.assertEqual(parse_ping_latency(output), 0)

    def test_returns_none_when_latency_missing(self) -> None:
        self.assertIsNone(parse_ping_latency("Request timed out."))

    def test_parses_linux_ping_output(self) -> None:
        output = "64 bytes from 142.250.184.14: icmp_seq=1 ttl=117 time=21.4 ms"
        self.assertEqual(parse_ping_latency(output), 21)


class BuildPingCommandTests(unittest.TestCase):
    def test_build_ping_command_returns_list(self) -> None:
        command = build_ping_command("google.com")
        self.assertEqual(command[0], "ping")
        self.assertEqual(command[-1], "google.com")


class ParseNslookupIpTests(unittest.TestCase):
    def test_parses_answer_section_and_ignores_dns_server(self) -> None:
        output = """
Server:  dns.google
Address:  8.8.8.8

Name:    localhost
Address:  127.0.0.1
"""
        self.assertEqual(parse_nslookup_ip(output), "127.0.0.1")

    def test_parses_plural_addresses(self) -> None:
        output = """
Server:  dns.google
Address:  8.8.8.8

Name:    example.com
Addresses:  93.184.216.34
"""
        self.assertEqual(parse_nslookup_ip(output), "93.184.216.34")


class DetermineRootCauseTests(unittest.TestCase):
    def make_result(self, layer: str, ok: bool, status: str, error_type: str | None = None) -> CheckResult:
        return CheckResult(layer=layer, ok=ok, detail=f"{layer} {status}", status=status, error_type=error_type)

    def test_prioritizes_dns_failure_when_network_and_dns_fail(self) -> None:
        network = self.make_result("NETWORK", False, "FAIL")
        dns = self.make_result("DNS", False, "FAIL", "gaierror")
        http = self.make_result("HTTP", False, "SKIPPED")
        self.assertEqual(determine_root_cause(network, dns, http), "ROOT CAUSE: DNS RESOLUTION FAILURE")

    def test_detects_network_failure_when_dns_is_ok(self) -> None:
        network = self.make_result("NETWORK", False, "FAIL")
        dns = self.make_result("DNS", True, "OK")
        http = self.make_result("HTTP", False, "FAIL")
        self.assertEqual(determine_root_cause(network, dns, http), "ROOT CAUSE: NETWORK REACHABILITY FAILURE")

    def test_detects_service_not_running(self) -> None:
        network = self.make_result("NETWORK", True, "OK")
        dns = self.make_result("DNS", True, "OK")
        http = self.make_result("HTTP", False, "FAIL", "ConnectionError")
        self.assertEqual(determine_root_cause(network, dns, http), "ROOT CAUSE: SERVICE NOT RUNNING OR PORT CLOSED")

    def test_detects_healthy_path(self) -> None:
        network = self.make_result("NETWORK", True, "OK")
        dns = self.make_result("DNS", True, "OK")
        http = self.make_result("HTTP", True, "OK")
        self.assertEqual(determine_root_cause(network, dns, http), "ROOT CAUSE: NO ISSUES DETECTED")


if __name__ == "__main__":
    unittest.main()
