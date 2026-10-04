"""A1 (#25): protocol framing is pure — round-trip, junk rejection, forward compat."""

import json
import unittest

import worker_protocol as wp


class TestEncodeDecode(unittest.TestCase):
    def test_encode_is_one_utf8_json_line(self):
        raw = wp.encode({"a": 1})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(json.loads(raw.decode("utf-8")), {"a": 1})

    def test_encode_keeps_non_ascii_readable(self):
        # ensure_ascii=False: Chinese text stays UTF-8 in the frame (#25 §4.2)
        self.assertIn("语音".encode("utf-8"), wp.encode_text(1, "语音"))

    def test_ready_line_shape(self):
        msg = wp.parse_line(wp.encode_ready(123, "npu", "small-int8-ov", 0.71))
        self.assertEqual(msg["event"], "ready")
        self.assertEqual(msg["protocol"], wp.PROTOCOL_VERSION)
        self.assertEqual(msg["pid"], 123)
        self.assertEqual(msg["engine"], "npu")
        self.assertEqual(msg["model"], "small-int8-ov")
        self.assertEqual(msg["load_s"], 0.71)
        self.assertNotIn("id", msg)

    def test_request_round_trip(self):
        self.assertEqual(wp.parse_line(wp.encode_request(7, "/tmp/a.wav")),
                         {"id": 7, "wav": "/tmp/a.wav"})

    def test_encode_request_round_trips_unicode_path(self):
        path = "/tmp/语音 目录/voice-input-hold-x.wav"
        self.assertEqual(wp.parse_line(wp.encode_request(1, path))["wav"], path)

    def test_text_and_error_round_trip(self):
        self.assertEqual(wp.parse_line(wp.encode_text(2, "你好")), {"id": 2, "text": "你好"})
        self.assertEqual(wp.parse_line(wp.encode_error(2, "ValueError: boom")),
                         {"id": 2, "error": "ValueError: boom"})
        self.assertIsNone(wp.parse_line(wp.encode_error(None, "bad request"))["id"])

    def test_newline_in_error_is_escaped_not_split(self):
        raw = wp.encode_error(1, "line1\nline2")
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(wp.parse_line(raw)["error"], "line1\nline2")


class TestParseRejects(unittest.TestCase):
    def test_bad_json(self):
        with self.assertRaises(wp.ProtocolError):
            wp.parse_line(b"{not json}\n")

    def test_non_object(self):
        for raw in (b"[1, 2]\n", b"\"text\"\n", b"42\n", b"null\n"):
            with self.subTest(raw=raw), self.assertRaises(wp.ProtocolError):
                wp.parse_line(raw)

    def test_non_utf8_bytes(self):
        with self.assertRaises(wp.ProtocolError):
            wp.parse_line(b"\xff\xfe\x00\n")

    def test_id_must_be_int_when_present(self):
        for raw in (b'{"id": "1"}\n', b'{"id": 1.5}\n', b'{"id": true}\n'):
            with self.subTest(raw=raw), self.assertRaises(wp.ProtocolError):
                wp.parse_line(raw)
        # null id is valid: error line without a request id (#25 §4.2)
        msg = wp.parse_line(b'{"id": null, "error": "bad request"}\n')
        self.assertIsNone(msg["id"])

    def test_event_line_without_id_is_valid(self):
        self.assertEqual(wp.parse_line(b'{"event":"ready"}\n'), {"event": "ready"})

    def test_unknown_fields_tolerated(self):
        msg = wp.parse_line(b'{"id":1,"text":"x","future":true}\n')
        self.assertEqual(msg["text"], "x")


if __name__ == "__main__":
    unittest.main()
