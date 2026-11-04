"""Bindings: profile/config resolution and the keys that scope per-provider state."""

from __future__ import annotations

import os
import tempfile
import unittest

from quotaribbon import binding, fsutil
from quotaribbon import types as t


class BindingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)

    def test_hash_matches_the_shared_helper(self):
        for parts in (("a",), ("host", 501, "/x", "claude", "/p"), (b"\x00raw", "é", 0)):
            self.assertEqual(binding.sha256_hex(*parts), fsutil.sha256_hex(*parts))
        with self.assertRaises(TypeError):
            binding.sha256_hex(True)

    def test_profile_resolution_order(self):
        env = {"HOME": self.home, "CLAUDE_CONFIG_DIR": "/custom/claude"}
        self.assertEqual(binding.resolve_profile("claude", t.ProviderSettings(True, profile_dir="/set/claude"), env), "/set/claude")
        self.assertEqual(binding.resolve_profile("claude", {"enabled": True}, env), "/custom/claude")
        self.assertEqual(binding.resolve_profile("codex", None, env), os.path.join(self.home, ".codex"))
        self.assertEqual(binding.resolve_profile("claude", None, {"HOME": self.home, "CLAUDE_CONFIG_DIR": "rel"}),
                         os.path.join(self.home, ".claude"))
        self.assertEqual(binding.resolve_profile("codex", None, {"HOME": self.home, "CODEX_HOME": ""}), os.path.join(self.home, ".codex"))

    def test_profile_symlinks_are_resolved(self):
        real = os.path.join(self.home, "real")
        os.mkdir(real)
        os.symlink(real, os.path.join(self.home, "link"))
        env = {"HOME": self.home, "CODEX_HOME": os.path.join(self.home, "link") + "/"}
        self.assertEqual(binding.resolve_profile("codex", None, env), real)

    def test_herdr_config_path(self):
        self.assertEqual(binding.herdr_config_path({"HOME": self.home}), os.path.join(self.home, ".config", "herdr", "config.toml"))
        custom = os.path.join(self.home, "h.toml")
        self.assertEqual(binding.herdr_config_path({"HOME": self.home, "HERDR_CONFIG_PATH": custom}), custom)
        self.assertEqual(binding.herdr_config_path({"HOME": self.home, "HERDR_CONFIG_PATH": "rel.toml"}),
                         os.path.join(self.home, ".config", "herdr", "config.toml"))

    def test_claude_credential_namespace_separates_unset_from_explicit_default(self):
        default = os.path.join(self.home, ".claude")
        unset = {"HOME": self.home}
        explicit = {"HOME": self.home, "CLAUDE_CONFIG_DIR": default}
        s = {"providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}}
        self.assertEqual(binding.credential_namespace("claude", None, unset), "")
        self.assertEqual(binding.credential_namespace("claude", None, explicit), "env:" + default)
        a, b = binding.binding_for("claude", s, unset), binding.binding_for("claude", s, explicit)
        self.assertNotEqual(a.binding_key, b.binding_key)
        self.assertEqual(a.profile_key, b.profile_key)  # one physical profile, one process lock
        self.assertEqual(binding.credential_namespace("codex", None, explicit), "")

    def test_claude_override_namespace_uses_the_literal_spelling(self):
        real = os.path.join(self.home, "work")
        os.mkdir(real)
        ps = t.ProviderSettings(True, profile_dir=real + "/")
        self.assertEqual(binding.credential_namespace("claude", ps, {"HOME": self.home}), "set:" + real + "/")

    def test_server_identity_changes_when_the_socket_is_recreated(self):
        sock = os.path.join(self.home, "s.sock")
        self.assertEqual(binding.server_identity({}), ("", None))
        self.assertEqual(binding.server_identity({"HERDR_SOCKET_PATH": sock}), (sock, None))
        with open(sock, "w"):
            pass
        first = binding.server_identity({"HERDR_SOCKET_PATH": sock})
        os.unlink(sock)
        with open(sock, "w") as fh:
            fh.write("x")
        second = binding.server_identity({"HERDR_SOCKET_PATH": sock})
        self.assertIsNotNone(first[1])
        self.assertNotEqual(first, second)

    def test_keys(self):
        b = binding.make_binding("claude", "/p", "/c.toml", host="h", uid=7)
        self.assertEqual(b.binding_key, fsutil.sha256_hex("h", 7, "/c.toml", "claude", "/p")[:24])
        self.assertEqual(b.profile_key, fsutil.sha256_hex("claude", "/p")[:16])
        self.assertEqual(len(binding.scope_key("claude", "/p")), 24)

    def test_binding_changes_with_config_profile_host_and_user(self):
        base = binding.make_binding("claude", "/p", "/c.toml", host="h", uid=7)
        others = [
            binding.make_binding("claude", "/q", "/c.toml", host="h", uid=7),
            binding.make_binding("claude", "/p", "/d.toml", host="h", uid=7),
            binding.make_binding("claude", "/p", "/c.toml", host="g", uid=7),
            binding.make_binding("claude", "/p", "/c.toml", host="h", uid=8),
            binding.make_binding("codex", "/p", "/c.toml", host="h", uid=7),
        ]
        self.assertEqual(len({base.binding_key, *(o.binding_key for o in others)}), 6)
        self.assertEqual(base.profile_key, others[1].profile_key)  # same profile shares the provider lock

    def test_binding_for_accepts_settings_objects_and_dicts(self):
        env = {"HOME": self.home}
        s = t.Settings.default()
        self.assertEqual(binding.binding_for("codex", s, env), binding.binding_for("codex", s.to_json(), env))

    def test_binding_is_immutable_and_comparable(self):
        b = binding.make_binding("claude", "/p", "/c.toml", host="h", uid=7)
        with self.assertRaises(AttributeError):
            b.binding_key = "x"
        self.assertEqual(b, binding.make_binding("claude", "/p", "/c.toml", host="h", uid=7))
        self.assertEqual(len({b, binding.make_binding("claude", "/p", "/c.toml", host="h", uid=7)}), 1)
        self.assertEqual(set(b.to_json()), {"binding_key", "profile_key", "profile_dir", "herdr_config_path"})
        self.assertIn("Binding(", repr(b))


if __name__ == "__main__":
    unittest.main()
