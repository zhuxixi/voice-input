import unittest

from media_pause import _select_to_pause


class TestSelectToPause(unittest.TestCase):
    def test_picks_only_playing(self):
        self.assertEqual(
            _select_to_pause({"a": "Playing", "b": "Paused", "c": "Stopped"}),
            ["a"],
        )

    def test_empty(self):
        self.assertEqual(_select_to_pause({}), [])

    def test_all_playing(self):
        names = _select_to_pause({"a": "Playing", "b": "Playing"})
        self.assertEqual(sorted(names), ["a", "b"])

    def test_none_playing(self):
        self.assertEqual(
            _select_to_pause({"a": "Paused", "b": "Stopped"}),
            [],
        )


if __name__ == "__main__":
    unittest.main()
