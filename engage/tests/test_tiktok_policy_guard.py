"""TikTok policy guard — DEC-SM-029.

Two things this suite CAN enforce:
  1. No ENGAGE source module references a Higgsfield TikTok posting tool.
  2. The empires/tiktok registry cell stays unbound and not live-enabled.

One thing it CANNOT enforce: an agent calling an MCP tool directly. That is
governed by harness/social/CONTEXT-RULE.md and nothing else. A green suite is
NOT evidence that no prohibited tool was called.

The forbidden names live in THIS file, not in the engage package, so the scan
needs no self-exemption — tests/ sits outside PKG_DIR.

Imports verified against the repository 2026-08-22:
  - load_registry is exported from engage.config.registry (registry.py:173);
    it is NOT re-exported by engage.config.__init__, so the full module path
    is required.
  - Bare load_registry() reads the shipped registry at DEFAULT_REGISTRY_DIR,
    matching existing precedent in test_consent_post_redirect.py:431 and
    test_publish_safety_gates.py:246.
  - PKG_DIR comes from .helpers (helpers.py:10) and is a pathlib.Path.
"""
from __future__ import annotations

import unittest

from engage.config.registry import load_registry

from .helpers import PKG_DIR

FORBIDDEN_TOOLS = (
    "tiktok_connect",
    "tiktok_reconnect",
    "tiktok_prepare_publish",
    "tiktok_publish",
    "tiktok_publish_status",
    "tiktok_music_trending",
    "tiktok_music_tune",
)


class TestNoDirectTikTokPostingTools(unittest.TestCase):
    def test_engage_package_references_no_higgsfield_tiktok_tool(self):
        hits = []
        for py in PKG_DIR.rglob("*.py"):
            source = py.read_text(encoding="utf-8")
            for tool in FORBIDDEN_TOOLS:
                if tool in source:
                    hits.append(f"{py.relative_to(PKG_DIR.parent)}: {tool!r}")
        self.assertEqual(
            hits, [],
            "ENGAGE source references a prohibited Higgsfield TikTok posting "
            "tool (DEC-SM-029). The sanctioned transport is TikTok's official "
            "Content Posting API behind a gated sink.")

    def test_forbidden_list_covers_every_posting_verb(self):
        # Guards the guard: a renamed or added tool must be added here
        # deliberately, not discovered after it is already in use.
        for verb in ("connect", "reconnect", "prepare_publish", "publish",
                     "publish_status", "music_trending", "music_tune"):
            self.assertIn(f"tiktok_{verb}", FORBIDDEN_TOOLS)


class TestTikTokCellStaysUnbound(unittest.TestCase):
    """Assert-unchanged, not assert-absent. A null that silently becomes a
    value is exactly the regression these three fields exist to catch."""

    def setUp(self):
        self.registry = load_registry()
        self.row = self.registry.account_row("empires", "tiktok")

    def test_row_exists(self):
        self.assertIsNotNone(self.row)

    def test_identity_fields_present_and_null(self):
        for field in ("tiktok_union_id", "tiktok_open_id", "tiktok_client_key"):
            self.assertIn(field, self.row,
                          f"{field} must be present as an explicit null, not omitted")
            self.assertIsNone(self.row[field],
                              f"{field} is bound — no OAuth authorization has been given")

    def test_not_live_enabled(self):
        self.assertFalse(self.registry.is_live_enabled("empires", "tiktok"))
        self.assertEqual(self.row["live_status"], "disabled")

    def test_connector_id_is_not_treated_as_identity(self):
        # connector_id is provider-side, not TikTok-issued. It must never be
        # promoted into a binding-identity field.
        connector = self.row.get("connector_id")
        self.assertTrue(connector)
        for field in ("tiktok_union_id", "tiktok_open_id", "tiktok_client_key"):
            self.assertNotEqual(self.row[field], connector)

    def test_handle_is_not_treated_as_identity(self):
        handle = self.row.get("handle")
        for field in ("tiktok_union_id", "tiktok_open_id", "tiktok_client_key"):
            self.assertNotEqual(self.row[field], handle)


if __name__ == "__main__":
    unittest.main()
