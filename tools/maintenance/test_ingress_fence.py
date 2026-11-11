import shlex
import unittest
from types import SimpleNamespace
from ingress_fence import Fence, marked_http_status


class Backend:
    def __init__(self):
        self.chains = {4: {"INPUT": []}, 6: {"INPUT": []}}

    def __call__(self, args, check=True):
        family = 4 if args[0].endswith("/iptables") else 6
        state = self.chains[family]
        op = args[3]
        tail = args[4:]
        code = 0
        output = ""
        chain = tail[0] if tail else None
        if op == "-S":
            if chain is None:
                output = "\n".join("-N " + name for name in state if name != "INPUT")
            elif chain not in state:
                code = 1
            else:
                output = (
                    "-N "
                    + chain
                    + "\n"
                    + "\n".join(shlex.join(["-A", chain] + r) for r in state[chain])
                )
        elif op == "-N":
            if chain in state:
                code = 1
            else:
                state[chain] = []
        elif op == "-X":
            if chain not in state or state[chain]:
                code = 1
            else:
                del state[chain]
        elif op == "-C":
            code = 0 if tail[1:] in state.get(chain, []) else 1
        elif op == "-A":
            state[chain].append(tail[1:])
        elif op == "-I":
            state[chain].insert(0, tail[2:])
        elif op == "-D":
            if tail[1:] in state.get(chain, []):
                state[chain].remove(tail[1:])
            else:
                code = 1
        else:
            raise AssertionError(args)
        if code and check:
            raise RuntimeError("fixture command failed")
        return SimpleNamespace(returncode=code, stdout=output, stderr="")


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.f = Fence("0123456789abcdef", 18089, 123456, self.backend)

    def test_apply_and_remove_are_idempotent_and_port_scoped(self):
        self.f.preflight()
        self.f.apply()
        self.f.apply()
        for family in [4, 6]:
            self.assertEqual(len(self.backend.chains[family]["INPUT"]), 1)
            self.assertIn("18089", self.backend.chains[family]["INPUT"][0])
            self.assertEqual(len(self.backend.chains[family][self.f.chain]), 2)
        self.f.remove()
        self.f.remove()
        self.assertEqual(self.backend.chains, {4: {"INPUT": []}, 6: {"INPUT": []}})

    def test_partial_install_recovered_at_every_boundary(self):
        for phase in [
            "chain-created-4",
            "rules-ready-4",
            "input-fenced-4",
            "chain-created-6",
            "rules-ready-6",
            "input-fenced-6",
        ]:
            self.backend = Backend()
            self.f = Fence("0123456789abcdef", 18089, 123456, self.backend)

            def fail(at):
                if phase == at:
                    raise RuntimeError("controller interruption")

            with self.assertRaises(RuntimeError):
                self.f.apply(fail)
            self.f.remove()
            self.assertEqual(self.backend.chains, {4: {"INPUT": []}, 6: {"INPUT": []}})

    def test_foreign_rule_preserved(self):
        self.f.apply()
        foreign = ["-s", "127.0.0.1", "-j", "DROP"]
        self.backend.chains[4][self.f.chain].append(foreign)
        with self.assertRaises(RuntimeError):
            self.f.remove()
        self.assertIn(foreign, self.backend.chains[4][self.f.chain])

    def test_existing_generation_and_invalid_input_rejected(self):
        self.f.apply()
        with self.assertRaises(RuntimeError):
            self.f.preflight()
        for token, port, mark in [
            ("bad", 18089, 1),
            ("0123456789abcdef", 22, 1),
            ("0123456789abcdef", 8089, 0),
        ]:
            with self.assertRaises(ValueError):
                Fence(token, port, mark, self.backend)

    def test_probe_never_targets_external_address(self):
        with self.assertRaises(ValueError):
            marked_http_status(18089, 0, "192.0.2.1")


if __name__ == "__main__":
    unittest.main()
