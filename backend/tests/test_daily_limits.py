"""06 Nov: nvr-12 "Disk error" was posted every 30 min (the NVR re-sends an
active alarm), and nvr-01 / nvr-06 posted "stream disconnected" and
"connected again" for drops of a minute. Per NVR and day, the same alarm and
stream-down alerts are now posted at most TELEGRAM_EVENT_DAILY_LIMIT times;
a stream must stay down TELEGRAM_STREAM_DOWN_MINUTES before it is alerted."""
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from test_telegram_bot import Fixture, channel

from nvr_track.nvr import events
from nvr_track.services import event_listener
from nvr_track.bot import telegram_bot as tb


class Stop(Exception):
    pass


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class SettingTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        settings = tb.load_settings({})
        self.assertEqual((settings["event_daily_limit"], settings["stream_down_minutes"]), (1, 10))
        settings = tb.load_settings({"TELEGRAM_EVENT_DAILY_LIMIT": "3", "TELEGRAM_STREAM_DOWN_MINUTES": "0"})
        self.assertEqual((settings["event_daily_limit"], settings["stream_down_minutes"]), (3, 0))
        settings = tb.load_settings({"TELEGRAM_EVENT_DAILY_LIMIT": "0", "TELEGRAM_STREAM_DOWN_MINUTES": "x"})
        self.assertEqual((settings["event_daily_limit"], settings["stream_down_minutes"]), (1, 10))


class LimitFixture(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "events").mkdir(exist_ok=True)
        env = patch.dict("os.environ", {"EVENTS_DIR": str(self.root / "events")})
        env.start()
        self.addCleanup(env.stop)
        self.report(channels=[channel(1), channel(2)])
        self.report("nvr-12", channels=[channel(1)], name="Packing")

    def sent(self, task):
        self.calls.clear()
        task()
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def new_day(self):
        path = self.bot.state_dir / "daily_limits.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["date"] = "2000-01-01"
        path.write_text(json.dumps(data), encoding="utf-8")


class EventLimitTests(LimitFixture):
    def record(self, nvr, kind, ch=""):
        events.record(nvr, {"type": kind, "state": "active", "channel": ch, "count": 1,
                            "device_time": "", "description": ""}, events.EVENT_TYPES)

    def test_the_same_alarm_once_a_day_per_nvr_and_camera(self):
        self.bot.check_events()                                               # First start: position only.
        self.record("nvr-12", "diskerror")
        first = self.sent(self.bot.check_events)
        self.assertEqual(len(first), 1)
        self.assertIn("Disk error", first[0])
        self.assertIn("at most 1 time a day per NVR; repeats are listed in /events", first[0])
        for _ in range(3):                                                    # 12:43, 13:13, 13:43 …
            self.record("nvr-12", "diskerror")
            self.assertEqual(self.sent(self.bot.check_events), [])
        self.record("nvr-01", "diskerror")                                    # Another NVR: its own count.
        self.record("nvr-01", "videoloss", "1")
        self.record("nvr-01", "videoloss", "2")                               # Another camera.
        text = self.sent(self.bot.check_events)[0]
        self.assertEqual(text.count("Disk error"), 1)
        self.assertEqual(text.count("Video loss"), 2)
        self.new_day()
        self.record("nvr-12", "diskerror")
        self.assertIn("Disk error", self.sent(self.bot.check_events)[0])     # Next day: once more.
        lines = (self.root / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum('"nvr-12"' in line for line in lines), 5)       # All kept for /events.

    def test_a_higher_limit(self):
        self.bot.settings["event_daily_limit"] = 2
        self.bot.check_events()
        posts = []
        for _ in range(4):
            self.record("nvr-12", "diskerror")
            posts += self.sent(self.bot.check_events)
        self.assertEqual(len(posts), 2)
        self.assertNotIn("at most", posts[0])
        self.assertIn("at most 2 times a day", posts[1])


class StreamLimitTests(LimitFixture):
    def status(self, **nvrs):
        (self.root / "events" / "status.json").write_text(json.dumps({
            "schema_version": 1, "updated_at": datetime.now(timezone.utc).isoformat(), "nvrs": nvrs}))

    def test_short_drops_are_not_posted_and_long_ones_once_a_day(self):
        up = {"connected": True, "since": ago(60)}
        self.status(**{"nvr-01": up, "nvr-12": up})
        self.bot.check_alerts()                                               # First start.
        self.status(**{"nvr-01": {"connected": False, "since": ago(0.5), "down_since": ago(2),
                                  "last_error": "STREAM_CLOSED"}, "nvr-12": up})
        self.assertEqual(self.sent(self.bot.check_alerts), [])                # 2 min: a drop, not posted.
        self.status(**{"nvr-01": up, "nvr-12": up})
        self.assertEqual(self.sent(self.bot.check_alerts), [])                # No "connected again" either.
        down = {"connected": False, "since": ago(0.5), "down_since": ago(15), "last_error": "REFUSED"}
        self.status(**{"nvr-01": down, "nvr-12": up})
        text = self.sent(self.bot.check_alerts)[0]
        self.assertIn("real-time event stream disconnected (connection refused)", text)
        self.status(**{"nvr-01": up, "nvr-12": up})
        self.assertIn("real-time events connected again", self.sent(self.bot.check_alerts)[0])
        self.status(**{"nvr-01": down, "nvr-12": up})                         # Second outage today: quiet.
        self.assertEqual(self.sent(self.bot.check_alerts), [])
        self.assertEqual(self.sent(self.bot.check_alerts), [])
        self.status(**{"nvr-01": up, "nvr-12": up})
        self.assertEqual(self.sent(self.bot.check_alerts), [])                # Its recovery is quiet too.
        self.status(**{"nvr-01": up, "nvr-12": down})                         # Another NVR: its own count.
        self.assertIn("disconnected", self.sent(self.bot.check_alerts)[0])
        self.new_day()
        self.status(**{"nvr-01": down, "nvr-12": down})
        self.assertIn("nvr-01", self.sent(self.bot.check_alerts)[0])          # Next day: alerted again.

    def test_older_status_without_down_since(self):
        self.status(**{"nvr-01": {"connected": True, "since": ago(60)}})
        self.bot.check_alerts()
        self.status(**{"nvr-01": {"connected": False, "since": ago(1), "last_error": "STREAM_CLOSED"}})
        self.assertEqual(self.sent(self.bot.check_alerts), [])                # Uses "since": 1 min.


class ListenerOutageTests(unittest.TestCase):
    def test_down_since_marks_the_start_of_the_outage(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        listener = event_listener.Listener(SimpleNamespace(id="nvr-01", ip="127.0.0.1", http_port=port),
                                           events.EVENT_TYPES, events.Deduplicator(), threading.Lock())
        listener.state["down_since"] = "2026-11-06T05:00:00+00:00"           # Down since then.
        calls = []

        def sleep(_seconds):
            calls.append(1)
            if len(calls) == 2:
                raise Stop
        with patch.dict("os.environ", {"NVR_USERNAME": "u", "NVR_PASSWORD": "p"}), \
                patch.object(event_listener.time, "sleep", sleep), patch("builtins.print"):
            with self.assertRaises(Stop):
                listener.run()
        self.assertEqual(listener.state["down_since"], "2026-11-06T05:00:00+00:00")   # Not each retry.
        self.assertNotEqual(listener.state["since"], "2026-11-06T05:00:00+00:00")
        listener.mark(connected=True, down_since=None)
        self.assertIsNone(listener.state["down_since"])


if __name__ == "__main__":
    unittest.main()
