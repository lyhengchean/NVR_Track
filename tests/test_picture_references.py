"""Picture references that follow light and scene changes, "view changed" only
after it persists, and the picture from before a change kept for comparison."""
import base64
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fake_nvr import jpeg
from test_telegram_bot import Fixture, channel

from app import dashboard, snapshots
from app.camera_condition import classify

ICT = timezone(timedelta(hours=7))


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, 5, tzinfo=ICT).astimezone(timezone.utc)


def pic(bits: int, mode: str = "day") -> dict:
    return {"brightness": 120.0, "contrast": 40.0, "mode": mode, "dhash": f"{bits:016x}"}


def flip(bits: int, count: int, start: int = 0) -> int:
    """Change `count` bits of the hash, from bit `start`."""
    for i in range(start, start + count):
        bits ^= 1 << (i % 64)
    return bits


class JudgeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"NVR_SNAPSHOT_MOVED_SCANS": "2"})
        env.start()
        self.addCleanup(env.stop)

    def run_day(self, steps):
        """steps: [(datetime, bits)]; returns statuses."""
        entry, statuses = None, []
        for n, (when, bits) in enumerate(steps):
            status, _, entry = snapshots.judge(pic(bits), f"sha{n}", entry, None, now=when)
            statuses.append(status)
        self.entry = entry
        return statuses

    def test_light_changing_through_the_day_is_followed(self):
        # 08:00 to 16:00: 10 more bits differ every 2 h; 40 bits from the morning
        # picture by 16:00. The old fixed reference flagged it from 12:00 on.
        base = 0x0F0F_F0F0_3C3C_C3C3
        steps = [(at(5, 8 + 2 * i), flip(base, 10 * i)) for i in range(5)]
        self.assertEqual(self.run_day(steps), ["PICTURE_OK"] * 5)
        self.assertGreater(snapshots.hamming(f"{steps[-1][1]:016x}", f"{base:016x}"), snapshots.MOVED_ABOVE)

    def test_next_morning_is_compared_with_the_same_time_of_day(self):
        base = 0x0F0F_F0F0_3C3C_C3C3
        day = [(at(5, 8 + 2 * i), flip(base, 10 * i)) for i in range(5)]   # Ends 40 bits away at 16:00.
        self.run_day(day)
        status, _, _ = snapshots.judge(pic(flip(base, 3)), "next", self.entry, None, now=at(6, 8))
        self.assertEqual(status, "PICTURE_OK")                               # Matches yesterday 08:00.
        self.assertIn("day@08", self.entry["refs"])

    def test_one_odd_picture_is_rechecked_and_a_lasting_change_is_reported(self):
        base = 0x0F0F_F0F0_3C3C_C3C3
        statuses = self.run_day([(at(5, 8), base), (at(5, 10), flip(base, 40)), (at(5, 12), flip(base, 2))])
        self.assertEqual(statuses, ["PICTURE_OK", "PICTURE_VIEW_CHECKING", "PICTURE_OK"])  # A truck passed.
        statuses = self.run_day([(at(5, 8), base), (at(5, 10), flip(base, 40)), (at(5, 12), flip(base, 41))])
        self.assertEqual(statuses, ["PICTURE_OK", "PICTURE_VIEW_CHECKING", "PICTURE_VIEW_CHANGED"])
        self.assertEqual(self.entry["refs"]["day"]["dhash"], f"{base:016x}")  # A moved view is never learned.
        with patch.dict("os.environ", {"NVR_SNAPSHOT_MOVED_SCANS": "1"}):
            self.assertEqual(self.run_day([(at(5, 8), base), (at(5, 10), flip(base, 40))])[-1],
                             "PICTURE_VIEW_CHANGED")

    def test_entries_from_before_this_rule_do_not_flap(self):
        refs = {"day": {"dhash": f"{0:016x}", "at": at(5, 8).isoformat()}}
        flagged = {"sha": "x", "dhash": f"{flip(0, 30):016x}", "checked_at": at(5, 14).isoformat(),
                   "refs": refs, "distance": 30}
        self.assertEqual(snapshots.judge(pic(flip(0, 30)), "y", flagged, None, now=at(5, 16))[0],
                         "PICTURE_VIEW_CHANGED")                             # Stays; no "OK again" + re-alert.
        fine = {**flagged, "distance": 3}
        self.assertEqual(snapshots.judge(pic(flip(0, 30)), "y", fine, None, now=at(5, 16))[0],
                         "PICTURE_VIEW_CHECKING")

    def test_marking_fixed_forgets_every_older_reference(self):
        base = 0x0F0F_F0F0_3C3C_C3C3
        self.run_day([(at(5, 8), base), (at(5, 10), flip(base, 40)), (at(5, 12), flip(base, 40))])
        fixed_at = at(5, 13).isoformat()
        status, _, entry = snapshots.judge(pic(flip(base, 40)), "z", self.entry, fixed_at, now=at(5, 14))
        self.assertEqual((status, entry["moved_scans"]), ("PICTURE_OK", 0))
        self.assertEqual({r["at"] for r in entry["refs"].values()}, {at(5, 14).isoformat()})

    def test_rechecking_is_not_a_picture_problem(self):
        cam = {**channel(1), "snapshot_result": {"status": "PICTURE_VIEW_CHECKING"}}
        self.assertEqual(classify(cam)["code"], "OK")


class SpacedRecheckTests(unittest.TestCase):
    """Deploy and manual scans minutes apart must not confirm "view changed" (nvr-04, 05 Nov:
    1 -> 3 picture problems between deploy scans at 15:22 and 15:35)."""

    def setUp(self):
        env = patch.dict("os.environ", {"NVR_SNAPSHOT_MOVED_SCANS": "2"})
        env.start()
        self.addCleanup(env.stop)
        self.base = 0x0F0F_F0F0_3C3C_C3C3

    def judge(self, entry, bits, when, sha):
        return snapshots.judge(pic(bits), sha, entry, None, now=when)

    def minute(self, hour, minute):
        return datetime(2026, 11, 5, hour, minute, tzinfo=ICT).astimezone(timezone.utc)

    def test_scans_minutes_apart_keep_rechecking_until_enough_time_passed(self):
        moved = flip(self.base, 40)
        _, _, entry = self.judge(None, self.base, self.minute(14, 0), "a")
        statuses = []
        for n, (hour, minute) in enumerate(((15, 22), (15, 35), (16, 0), (16, 52))):
            status, message, entry = self.judge(entry, flip(moved, n % 2), self.minute(hour, minute), f"s{n}")
            statuses.append(status)
        self.assertEqual(statuses, ["PICTURE_VIEW_CHECKING"] * 3 + ["PICTURE_VIEW_CHANGED"])  # 90 min after 15:22.
        self.assertEqual(entry["differs_since"], self.minute(15, 22).isoformat())
        status, _, entry = self.judge(entry, flip(moved, 1), self.minute(16, 55), "t")
        self.assertEqual(status, "PICTURE_VIEW_CHANGED")                            # Stays reported.

    def test_a_frozen_picture_in_between_keeps_the_start_time(self):
        _, _, entry = self.judge(None, self.base, self.minute(14, 0), "a")
        _, _, entry = self.judge(entry, flip(self.base, 40), self.minute(15, 22), "b")
        status, _, entry = self.judge(entry, flip(self.base, 40), self.minute(15, 35), "b")
        self.assertEqual((status, entry["differs_since"]), ("PICTURE_FROZEN", self.minute(15, 22).isoformat()))
        self.assertEqual(self.judge(entry, flip(self.base, 41), self.minute(17, 0), "c")[0], "PICTURE_VIEW_CHANGED")

    def test_a_matching_picture_in_between_starts_over(self):
        _, _, entry = self.judge(None, self.base, self.minute(8, 0), "a")
        _, _, entry = self.judge(entry, flip(self.base, 40), self.minute(10, 0), "b")
        status, _, entry = self.judge(entry, flip(self.base, 2), self.minute(10, 30), "c")
        self.assertEqual((status, entry["moved_scans"], entry.get("differs_since")), ("PICTURE_OK", 0, None))
        status, _, entry = self.judge(entry, flip(self.base, 40), self.minute(12, 0), "d")
        self.assertEqual((status, entry["differs_since"]), ("PICTURE_VIEW_CHECKING", self.minute(12, 0).isoformat()))

    def test_entries_written_before_this_rule(self):
        refs = {"day": {"dhash": f"{self.base:016x}", "at": self.minute(14, 0).isoformat()}}
        moved = flip(self.base, 40)
        checking = {"sha": "x", "dhash": f"{moved:016x}", "checked_at": self.minute(15, 35).isoformat(),
                    "refs": refs, "distance": 40, "moved_scans": 1}
        self.assertEqual(self.judge(checking, moved, self.minute(16, 0), "y")[0], "PICTURE_VIEW_CHECKING")
        self.assertEqual(self.judge(checking, moved, self.minute(18, 0), "y")[0], "PICTURE_VIEW_CHANGED")
        reported = {**checking, "moved_scans": 2}                                   # Already "view changed".
        self.assertEqual(self.judge(reported, moved, self.minute(15, 40), "y")[0], "PICTURE_VIEW_CHANGED")

    def test_setting(self):
        self.assertEqual(snapshots.moved_minutes(), 90)
        for value, expected in (("0", 0), ("30", 30), ("5000", 720), ("-4", 0), ("x", 90)):
            with patch.dict("os.environ", {"NVR_SNAPSHOT_MOVED_MINUTES": value}):
                self.assertEqual(snapshots.moved_minutes(), expected)
        with patch.dict("os.environ", {"NVR_SNAPSHOT_MOVED_MINUTES": "0"}):         # Scans in a row only.
            _, _, entry = self.judge(None, self.base, self.minute(15, 0), "a")
            _, _, entry = self.judge(entry, flip(self.base, 40), self.minute(15, 10), "b")
            self.assertEqual(self.judge(entry, flip(self.base, 41), self.minute(15, 20), "c")[0],
                             "PICTURE_VIEW_CHANGED")


class StoredPictureTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"NVR_SNAPSHOT_MOVED_MINUTES": "0"})  # Scans here are instant.
        env.start()
        self.addCleanup(env.stop)

    def test_picture_from_before_the_change_is_kept_then_dropped(self):
        from PIL import Image
        normal = jpeg("scene")

        def moved_view(seed):  # Different bytes each scan, or it would be "frozen".
            image = Image.open(io.BytesIO(jpeg("scene", seed))).transpose(Image.FLIP_LEFT_RIGHT)
            out = io.BytesIO()
            image.save(out, "JPEG")
            return out.getvalue()
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            cams = [{"channel_id": "5", "reported_status": "ONLINE", "recording_result": {"track_id": 501}}]

            def scan(picture):
                snapshots.check_snapshots(ip="127.0.0.1", port=80, username="u", password="p", nvr_id="nvr-01",
                                          channels=cams, state_dir=state, deadline=time.monotonic() + 30,
                                          fetcher=lambda *_: ("OK", picture))
                return cams[0]["snapshot_result"]
            first = scan(normal)
            before = (state / "nvr-01" / "5.jpg").read_bytes()
            self.assertEqual((first["status"], first["has_reference"]), ("PICTURE_OK", False))
            second = scan(moved_view(1))
            self.assertEqual((second["status"], second["has_reference"]), ("PICTURE_VIEW_CHECKING", True))
            self.assertEqual((state / "nvr-01" / "5.ref.jpg").read_bytes(), before)
            self.assertEqual(second["reference_at"], first["checked_at"])
            third = scan(moved_view(2))
            self.assertEqual((third["status"], third["has_reference"]), ("PICTURE_VIEW_CHANGED", True))
            self.assertEqual((state / "nvr-01" / "5.ref.jpg").read_bytes(), before)  # Still the old view.
            self.assertEqual(scan(normal)["status"], "PICTURE_OK")                  # Moved back.
            self.assertFalse((state / "nvr-01" / "5.ref.jpg").exists())


class AlertAndDashboardTests(Fixture):
    def setUp(self):
        super().setUp()
        for name in ("notes", "snapshots"):
            (self.root / name).mkdir()
        env = patch.dict("os.environ", {"NOTES_DIR": str(self.root / "notes")})
        env.start()
        self.addCleanup(env.stop)

    def alerts(self):
        self.calls.clear()
        self.bot.check_alerts()
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_rechecking_keeps_an_open_picture_alert_without_messages(self):
        self.report(channels=[channel(1)])
        self.alerts()
        self.report(channels=[{**channel(1), "snapshot_result": {"status": "PICTURE_VIEW_CHANGED", "message": "x"}}])
        self.assertIn("Picture view changed", self.alerts()[0])
        self.report(channels=[{**channel(1), "snapshot_result": {"status": "PICTURE_VIEW_CHECKING", "message": "x"}}])
        self.assertEqual(self.alerts(), [])                                   # No false "Picture OK again".
        self.report(channels=[{**channel(1), "snapshot_result": {"status": "PICTURE_OK", "message": "x"}}])
        self.assertIn("Picture OK again", self.alerts()[0])

    def test_dashboard_serves_the_picture_from_before_the_change(self):
        folder = self.root / "snapshots" / "nvr-01"
        folder.mkdir()
        (folder / "5.jpg").write_bytes(jpeg("scene", 1))
        (folder / "5.ref.jpg").write_bytes(jpeg("scene", 2))
        with patch.object(dashboard, "SNAPSHOT_DIR", self.root / "snapshots"), \
                patch.object(dashboard, "DASHBOARD_USERNAME", "demo"), \
                patch.object(dashboard, "DASHBOARD_PASSWORD", "test-password-long"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            auth = {"Authorization": "Basic " + base64.b64encode(b"demo:test-password-long").decode()}

            def get(path):
                try:
                    with urlopen(Request(f"http://127.0.0.1:{server.server_port}{path}", headers=auth), timeout=3) as r:
                        return r.status, r.read()
                except HTTPError as exc:
                    return exc.code, b""
            try:
                self.assertEqual(get("/api/snapshot?nvr=nvr-01&ch=5&ref=1"), (200, jpeg("scene", 2)))
                self.assertEqual(get("/api/snapshot?nvr=nvr-01&ch=5"), (200, jpeg("scene", 1)))
                self.assertEqual(get("/api/snapshot?nvr=nvr-01&ch=5&ref=../x")[0], 400)
                self.assertEqual(get("/api/snapshot?nvr=nvr-01&ch=6&ref=1")[0], 404)
            finally:
                server.shutdown()
                server.server_close()
        public = dashboard._public_snapshot({"status": "PICTURE_VIEW_CHECKING", "has_reference": True,
                                             "reference_at": "2026-11-05T01:05:00+00:00"})
        self.assertEqual((public["status"], public["has_reference"]), ("PICTURE_VIEW_CHECKING", True))
        self.assertIn("&ref=1&t=", dashboard.HTML)


if __name__ == "__main__":
    unittest.main()
