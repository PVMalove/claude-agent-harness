#!/usr/bin/env python3
"""Characterisation tests for render_html.py's formatting helpers and panels (issue #230): pin the
runtime behaviour of the functions whose `Any` annotations were narrowed, so the narrowing cannot
change what they render."""

from __future__ import annotations

import unittest

from harness.reporting import render_html

NB = chr(0xA0)  # the rendered thousands separator is a no-break space


class FormatterTests(unittest.TestCase):
    def test_esc_escapes_markup_and_stringifies_non_strings(self) -> None:
        self.assertEqual(render_html._esc("<b>&"), "&lt;b&gt;&amp;")
        self.assertEqual(render_html._esc(None), "None")

    def test_thousands_groups_ints_and_passes_other_values_through_escaped(
        self,
    ) -> None:
        self.assertEqual(render_html._thousands(1234567), "1" + NB + "234" + NB + "567")
        self.assertEqual(render_html._thousands("<x>"), "&lt;x&gt;")

    def test_decimal_uses_a_comma_separator_for_numbers_only(self) -> None:
        self.assertEqual(render_html._decimal(1234.5), "1" + NB + "234,50")
        self.assertEqual(render_html._decimal(2, 1), "2,0")
        self.assertEqual(render_html._decimal("n/a"), "n/a")

    def test_compact_scales_ints_and_passes_other_values_through(self) -> None:
        self.assertEqual(render_html._compact(2_500_000), "2,5" + NB + "млн")
        self.assertEqual(render_html._compact(1_000), "1" + NB + "тыс")
        self.assertEqual(render_html._compact(999), "999")
        self.assertEqual(render_html._compact(1.5), "1.5")


class BarsTests(unittest.TestCase):
    def test_empty_rows_render_the_missing_notice(self) -> None:
        self.assertIn("нет данных", render_html._bars([]))

    def test_widest_row_fills_the_track_and_others_scale_to_it(self) -> None:
        markup = render_html._bars([("a", 100, "100"), ("b", 50, "50")])
        self.assertIn("width:100.0%", markup)
        self.assertIn("width:50.0%", markup)


class PanelTests(unittest.TestCase):
    def test_cache_panel_is_missing_for_a_non_dict(self) -> None:
        self.assertIn(render_html.MISSING, render_html._cache_panel(None))
        self.assertIn(
            render_html.MISSING, render_html._cache_panel(render_html.MISSING)
        )

    def test_cache_panel_reports_the_total_input(self) -> None:
        cache = {
            "cache_read_percent": 50.0,
            "cache_write_percent": 25.0,
            "fresh_percent": 25.0,
            "cache_read": 500,
            "cache_write": 250,
            "fresh": 250,
            "total_input": 1000,
        }
        markup = render_html._cache_panel(cache)
        self.assertIn("1" + NB + "000 токенов", markup)
        self.assertIn("чтение кеша", markup)

    def test_orchestration_panel_is_missing_for_a_non_ok_or_non_dict_value(
        self,
    ) -> None:
        self.assertIn(render_html.MISSING, render_html._orchestration_panel(None))
        markup = render_html._orchestration_panel(
            {"status": render_html.MISSING, "reason": "no ledger"}
        )
        self.assertIn("no ledger", markup)

    def test_orchestration_panel_lists_sessions_and_qa_rate(self) -> None:
        orchestration = {
            "status": "ok",
            "tickets": {
                "7": {
                    "worker_sessions": [
                        {"role": "developer", "sessions": 2, "restarts": []}
                    ],
                    "qa_failure_rate": 0.5,
                    "review_scope": render_html.MISSING,
                }
            },
        }
        markup = render_html._orchestration_panel(orchestration)
        self.assertIn("developer", markup)
        self.assertIn("50,0 %", markup)


if __name__ == "__main__":
    unittest.main()
