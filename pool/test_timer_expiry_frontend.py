"""Regression coverage for the lightweight scorer timer reconciliation UI."""

from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import timezone


class MatchTimerFrontendTests(SimpleTestCase):
    def render_active_timer(self):
        return render_to_string(
            "pool/_match_timer.html",
            {
                "match": SimpleNamespace(
                    pk=123,
                    timer_is_active=True,
                    timer_deadline=timezone.now(),
                    time_expired_tied=False,
                    timer_remaining_seconds=0,
                ),
                "can_expire_match": True,
            },
        )

    def test_zero_countdown_immediately_uses_one_guarded_reconciliation(self):
        html = self.render_active_timer()

        self.assertIn("function requestZeroReconciliation()", html)
        self.assertIn("if (!remaining)", html)
        self.assertGreater(
            html.index("requestZeroReconciliation();"),
            html.index("if (!remaining)"),
        )
        self.assertIn("let expiryInFlight = false;", html)
        self.assertIn("|| expiryInFlight", html)
        self.assertIn("zeroReconciliationRequested", html)
        self.assertNotIn(
            "drawCountdown();\n            requestZeroReconciliation();",
            html,
        )

    def test_timer_keeps_only_a_modest_safety_poll_and_reloads_authoritative_state(self):
        html = self.render_active_timer()

        self.assertIn("window.setInterval(pollExpiry, 30000);", html)
        self.assertNotIn("window.setTimeout(pollExpiry, 1000);", html)
        self.assertIn("payload.status !== \"LIVE\"", html)
        self.assertIn("payload.time_expired_tied", html)
        self.assertIn("window.location.reload();", html)
