"""Focused public formatting coverage for Pool presentation helpers."""

from decimal import Decimal

from django.test import SimpleTestCase

from .templatetags.pool_presentation import lilangeni


class PoolPresentationTests(SimpleTestCase):
    def test_lilangeni_omits_unneeded_decimal_zeroes_and_groups_thousands(self):
        self.assertEqual(lilangeni(Decimal("2000")), "E2,000")
        self.assertEqual(lilangeni(Decimal("500")), "E500")
        self.assertEqual(lilangeni(Decimal("2500.50")), "E2,500.50")
