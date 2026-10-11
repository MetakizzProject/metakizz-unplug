"""Unit tests for the partner-invite group resolution
(app/services/circle_invite.py::_resolve_target_group).

Run from the repo root:
    python -m unittest discover -s tests -t .
"""

import sys
import types
import unittest

# circle_invite imports `requests` at module level; the resolution logic
# under test never touches the network, so a stub is enough when the
# dependency is not installed locally.
if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        sys.modules["requests"] = types.ModuleType("requests")

from app.services.circle_invite import _resolve_target_group

ACADEMY_MD, MKOT3_MD = 150936, 121427
ACADEMY_MI, MKOT3_MI = 150937, 121510
DANCERS = [ACADEMY_MD, MKOT3_MD]
INSTRUCTORS = [ACADEMY_MI, MKOT3_MI]


class ResolveTargetGroupTests(unittest.TestCase):
    def test_academy_dancer_partner_goes_to_academy_dancers(self):
        self.assertEqual(
            _resolve_target_group([ACADEMY_MD, 66572], DANCERS, INSTRUCTORS),
            ("dancers", ACADEMY_MD),
        )

    def test_academy_instructor_partner_goes_to_academy_instructors(self):
        self.assertEqual(
            _resolve_target_group([ACADEMY_MI], DANCERS, INSTRUCTORS),
            ("instructors", ACADEMY_MI),
        )

    def test_mkot3_buyer_keeps_legacy_group(self):
        self.assertEqual(
            _resolve_target_group([MKOT3_MD], DANCERS, INSTRUCTORS),
            ("dancers", MKOT3_MD),
        )
        self.assertEqual(
            _resolve_target_group([MKOT3_MI], DANCERS, INSTRUCTORS),
            ("instructors", MKOT3_MI),
        )

    def test_both_tracks_prefers_dancers(self):
        self.assertEqual(
            _resolve_target_group([ACADEMY_MI, ACADEMY_MD], DANCERS, INSTRUCTORS),
            ("dancers", ACADEMY_MD),
        )

    def test_buyer_in_neither(self):
        self.assertEqual(
            _resolve_target_group([66572], DANCERS, INSTRUCTORS),
            (None, None),
        )


if __name__ == "__main__":
    unittest.main()
