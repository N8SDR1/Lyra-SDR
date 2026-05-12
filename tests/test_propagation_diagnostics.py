"""Phase 3.E.1 hotfix v0.18 (2026-05-12) -- HamQslSolarCache
diagnostic surface for the "blank propagation panel" failure mode
that tester "Timmy" hit on 2026-05-12.

Pre-fix, ``HamQslSolarCache.get()`` silently swallowed every fetch
exception so operators behind a firewall / SSL block / DNS issue
had zero diagnostic to point at.  Now:

* ``HamQslSolarCache.last_error`` exposes the most-recent fetch
  exception text so the panel can render it.
* The exception is also printed (captured to ``crash.log`` on the
  PyInstaller --windowed build per v0.0.9.9.1's faulthandler
  routing).
* Successful fetch clears ``last_error`` back to None.

Run from repo root::

    python -m unittest tests.test_propagation_diagnostics -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from lyra.propagation import HamQslSolarCache


class HamQslSolarCacheLastErrorTest(unittest.TestCase):
    def test_last_error_initially_none(self) -> None:
        c = HamQslSolarCache()
        self.assertIsNone(c.last_error)

    def test_last_error_captures_fetch_exception(self) -> None:
        c = HamQslSolarCache()

        def _boom(self):
            raise OSError("Name or service not known")

        with patch.object(HamQslSolarCache, "_fetch", _boom):
            result = c.get(force_refresh=True)
        # No prior cache -> data is None.
        self.assertIsNone(result)
        # And the error text was captured.
        self.assertIsNotNone(c.last_error)
        self.assertIn("OSError", c.last_error)
        self.assertIn("Name or service not known", c.last_error)

    def test_last_error_cleared_on_successful_fetch(self) -> None:
        c = HamQslSolarCache()

        def _boom(self):
            raise OSError("transient")

        # First call fails -> last_error populated.
        with patch.object(HamQslSolarCache, "_fetch", _boom):
            c.get(force_refresh=True)
        self.assertIsNotNone(c.last_error)

        # Second call succeeds -> last_error cleared.
        good_data = {"sfi": "130", "aindex": "5", "kindex": "2",
                     "bands": {}}

        def _ok(self):
            return good_data

        with patch.object(HamQslSolarCache, "_fetch", _ok):
            result = c.get(force_refresh=True)
        self.assertEqual(result, good_data)
        self.assertIsNone(c.last_error)

    def test_stale_cache_returned_alongside_error(self) -> None:
        """When a prior fetch landed and a later fetch fails, the
        cache serves stale data AND records the error.  Operator
        sees last-known numbers + an error tooltip explaining the
        feed is currently unreachable."""
        c = HamQslSolarCache()
        good_data = {"sfi": "120", "aindex": "8", "kindex": "3",
                     "bands": {}}

        def _ok(self):
            return good_data

        with patch.object(HamQslSolarCache, "_fetch", _ok):
            c.get(force_refresh=True)
        # Now break the feed and force a re-fetch.

        def _broken(self):
            raise TimeoutError("HamQSL timed out after 10 sec")

        with patch.object(HamQslSolarCache, "_fetch", _broken):
            result = c.get(force_refresh=True)
        # Stale data still returned (cache holds on after one
        # successful fetch).
        self.assertEqual(result, good_data)
        # AND the error is captured for the panel to surface.
        self.assertIn("TimeoutError", c.last_error)


if __name__ == "__main__":
    unittest.main()
