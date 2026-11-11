"""Owned temporary IPv4/IPv6 INPUT fence for one host-network service port."""

import http.client
import ipaddress
import shlex
import socket
import struct
import subprocess


def execute(args, check=True):
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15
    )
    if check and result.returncode:
        raise RuntimeError("Firewall command failed: " + args[0] + " " + args[3])
    return result


class Fence:
    def __init__(self, token, port, mark, runner=execute):
        if len(token) != 16 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("Invalid fence token")
        if not 1024 <= port <= 65535 or not 1 <= mark <= 0xFFFFFFFF:
            raise ValueError("Invalid port/mark")
        self.token, self.port, self.mark = token, port, mark
        self.chain = "CDXVM" + token
        self.run = runner

    def command(self, family, *args, check=True):
        binary = "/usr/sbin/iptables" if family == 4 else "/usr/sbin/ip6tables"
        return self.run([binary, "-w", "5", *args], check=check)

    def rules(self):
        return [
            [
                "-m",
                "mark",
                "--mark",
                hex(self.mark),
                "-m",
                "comment",
                "--comment",
                self.token + "-allow",
                "-j",
                "RETURN",
            ],
            [
                "-p",
                "tcp",
                "-m",
                "comment",
                "--comment",
                self.token + "-block",
                "-j",
                "REJECT",
                "--reject-with",
                "tcp-reset",
            ],
        ]

    def jump(self):
        return [
            "-p",
            "tcp",
            "--dport",
            str(self.port),
            "-m",
            "comment",
            "--comment",
            self.token,
            "-j",
            self.chain,
        ]

    def inspect_owned(self, family):
        result = self.command(family, "-S", self.chain, check=False)
        if result.returncode:
            # Distinguish an absent chain from an unavailable firewall backend.
            all_rules = self.command(family, "-S")
            if any(
                shlex.split(line)[:2] == ["-N", self.chain]
                for line in all_rules.stdout.splitlines()
            ):
                raise RuntimeError("Cannot inspect an existing fence chain")
            return None
        lines = [shlex.split(x) for x in result.stdout.splitlines()]
        rules = [x for x in lines if x[:2] == ["-A", self.chain]]
        expected = self.rules()
        for rule in rules:
            # iptables normalizes a TCP match by inserting '-m tcp'.
            body = rule[2:]
            if "-m" in body:
                for index in range(len(body) - 1):
                    if body[index : index + 2] == ["-m", "tcp"]:
                        body = body[:index] + body[index + 2 :]
                        break
            if body not in expected:
                raise RuntimeError("Foreign rule in owned fence; preserve for review")
        if len(rules) > 2 or len({tuple(x) for x in rules}) != len(rules):
            raise RuntimeError("Duplicate fence rules")
        return rules

    def preflight(self):
        for family in [4, 6]:
            if self.inspect_owned(family) is not None:
                raise RuntimeError("Fence generation already exists")

    def apply(self, hook=None):
        hook = hook or (lambda _: None)
        for family in [4, 6]:
            if self.inspect_owned(family) is None:
                self.command(family, "-N", self.chain)
            hook("chain-created-" + str(family))
            self.inspect_owned(family)
            for rule in self.rules():
                if self.command(
                    family, "-C", self.chain, *rule, check=False
                ).returncode:
                    self.command(family, "-A", self.chain, *rule)
            hook("rules-ready-" + str(family))
            if self.command(
                family, "-C", "INPUT", *self.jump(), check=False
            ).returncode:
                self.command(family, "-I", "INPUT", "1", *self.jump())
            hook("input-fenced-" + str(family))

    def remove(self):
        for family in [4, 6]:
            rules = self.inspect_owned(family)
            if rules is None:
                continue
            if not self.command(
                family, "-C", "INPUT", *self.jump(), check=False
            ).returncode:
                self.command(family, "-D", "INPUT", *self.jump())
            for rule in self.rules():
                if not self.command(
                    family, "-C", self.chain, *rule, check=False
                ).returncode:
                    self.command(family, "-D", self.chain, *rule)
            self.command(family, "-X", self.chain)


def marked_http_status(port, mark, host="127.0.0.1"):
    address = ipaddress.ip_address(host)
    if not address.is_loopback:
        raise ValueError("Maintenance probes must use loopback")
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        if mark:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, struct.pack("=I", mark))
        sock.connect((host, port))
        sock.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status
