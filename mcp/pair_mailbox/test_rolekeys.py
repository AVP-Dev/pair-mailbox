#!/usr/bin/env python3
"""Тесты подписей ролей. Соответствуют `rolekeys.py`.

Запуск:  python3 mcp/pair_mailbox/test_rolekeys.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rolekeys  # noqa: E402
from pair_test_base import PairTestCase  # noqa: E402


class TestRoleKeys(PairTestCase):
    """Ключ отличает роль+проект сам, без pid/pane, которые флипают на рестарт."""

    def test_gen_writes_priv_0600_and_stable_key_id(self) -> None:
        first = rolekeys.generate(self.store.root, self.store.project_root, "writer-agent")
        second = rolekeys.generate(self.store.root, self.store.project_root, "writer-agent")
        self.assertEqual(first["key_id"], second["key_id"])
        self.assertEqual(
            0o600, rolekeys.keys_dir(self.store.root).joinpath("writer-agent.priv").stat().st_mode & 0o777)

    def test_key_id_differs_per_project_and_per_role(self) -> None:
        here = rolekeys.key_id_for(self.store.project_root, "writer-agent")
        other_role = rolekeys.key_id_for(self.store.project_root, "reviewer-agent")
        other_project = rolekeys.key_id_for(self.store.project_root / "sibling", "writer-agent")
        self.assertNotEqual(here, other_role)
        self.assertNotEqual(here, other_project)

    def test_signed_envelope_verifies(self) -> None:
        rolekeys.generate(self.store.root, self.store.project_root, "writer-agent")
        sk = rolekeys.load_signing_key(self.store.root, "writer-agent")
        vk = rolekeys.load_verify_key(self.store.root, "writer-agent")
        assert sk is not None and vk is not None
        env = rolekeys.sign_envelope(
            sk, rolekeys.key_id_for(self.store.project_root, "writer-agent"),
            seq=7, frm="writer-agent", to="reviewer-agent",
            ts="2026-10-07T00:00:00+00:00", text="проверь")
        self.assertTrue(rolekeys.verify_envelope(vk, env, "проверь"))

    def test_tampered_text_fails(self) -> None:
        """Подмена текста краснеет: body_sha не сошёлся."""
        rolekeys.generate(self.store.root, self.store.project_root, "writer-agent")
        sk = rolekeys.load_signing_key(self.store.root, "writer-agent")
        vk = rolekeys.load_verify_key(self.store.root, "writer-agent")
        assert sk is not None and vk is not None
        env = rolekeys.sign_envelope(
            sk, rolekeys.key_id_for(self.store.project_root, "writer-agent"),
            seq=7, frm="writer-agent", to="reviewer-agent",
            ts="2026-10-07T00:00:00+00:00", text="проверь")
        self.assertFalse(rolekeys.verify_envelope(vk, env, "подменил"))

    def test_foreign_key_fails(self) -> None:
        """Письмо чужого проекта своим ключом не верифицируется."""
        rolekeys.generate(self.store.root, self.store.project_root, "writer-agent")
        sk = rolekeys.load_signing_key(self.store.root, "writer-agent")
        assert sk is not None
        env = rolekeys.sign_envelope(
            sk, "чужой-key-id", seq=7, frm="writer-agent", to="reviewer-agent",
            ts="2026-10-07T00:00:00+00:00", text="проверь")
        other = rolekeys.SigningKey.generate().verify_key
        self.assertFalse(rolekeys.verify_envelope(other, env, "проверь"))

    def test_missing_key_is_none(self) -> None:
        self.assertIsNone(rolekeys.load_signing_key(self.store.root, "nobody"))
        self.assertIsNone(rolekeys.load_verify_key(self.store.root, "nobody"))

    def test_bad_letter_is_consumed_not_redelivered(self) -> None:
        """Подмена потребляется: seen идёт, дублей и спама bad_signature нет."""
        import json
        for role in ("writer-agent", "reviewer-agent"):
            made = rolekeys.generate(self.store.root, self.store.project_root, role)
            self.store.mutate(lambda s, r=role, m=made: s.setdefault("role_keys", {}).update(
                {r: {"key_id": m["key_id"], "public": m["public"], "revoked": []}}))
        self.writer.tool_pair_send(to="reviewer-agent", text="целое1")
        self.writer.tool_pair_send(to="reviewer-agent", text="целое2")
        files = sorted((self.store.mailbox_root / "reviewer-agent").glob("*.json"))
        bad = json.loads(files[0].read_text(encoding="utf-8"))
        bad["text"] = "подмена"
        files[0].write_text(json.dumps(bad), encoding="utf-8")
        first = self.peer.tool_pair_read(limit=10)
        self.assertEqual(["целое2"], [m["text"] for m in first["messages"]])
        self.assertEqual(0, self.peer.tool_pair_read(limit=10)["count"])
        kinds = [e.get("kind") for e in self.store.read_transcript(0)]
        self.assertEqual(1, kinds.count("bad_signature"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
