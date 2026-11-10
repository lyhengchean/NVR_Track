"""Report builder (/reports): query saved reports by one or more data sets,
NVRs, dates and, per data set, columns, conditions and sort; save under any
name on the server; download later as PDF (a section per data set) or CSV (a
ZIP with one CSV per data set). Built from saved data only; unverified checks
never count as recording or online."""
import base64
import io
import json
import tempfile
import threading
import unittest
import zipfile
from datetime import date, datetime, time, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_telegram_bot import channel

from nvr_track.reports import custom_reports as cr
from nvr_track.web import dashboard
from nvr_track.monitoring import notes as notes_store
from nvr_track.core.paths import PROJECT_ROOT as ROOT

ICT = cr.LOCAL_TZ
TODAY = cr.today_local()
D1, D2, D3 = TODAY - timedelta(days=3), TODAY - timedelta(days=2), TODAY - timedelta(days=1)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), ICT)


def disk(i, status="ok", cap=4_000_000, free=1_000_000):
    return {"id": str(i), "name": f"hdd{i}", "type": "SATA", "reported_status": status, "capacity_mb": cap,
            "free_space_mb": free, "property": "RW", "group": "1"}


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("reports", "scan", "notes", "events", "snapshots", "live", "saved"):
            (self.root / name).mkdir()
        env = patch.dict("os.environ", {"NOTES_DIR": str(self.root / "notes"), "EVENTS_DIR": str(self.root / "events"),
                                        "SNAPSHOT_DIR": str(self.root / "snapshots"), "LIVE_DIR": str(self.root / "live"),
                                        "CUSTOM_REPORTS_DIR": str(self.root / "saved")})
        env.start()
        self.addCleanup(env.stop)
        for target, value in (("REPORTS_DIR", self.root / "reports"), ("SCAN_STATE_DIR", self.root / "scan"),
                              ("SNAPSHOT_DIR", self.root / "snapshots")):
            patcher = patch.object(dashboard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        cr._cache.clear()
        self.reports = self.root / "reports"
        # nvr-01: ch2 drops twice; ch3's recording check fails once (never counted).
        plan = [(at(D1, 8), "ONLINE", "RECORDING"), (at(D1, 10), "OFFLINE", "CHECK_ERROR"),
                (at(D2, 8), "ONLINE", "RECORDING"), (at(D3, 23, 59), "OFFLINE", "NO_RECENT_RECORDING_EVIDENCE")]
        for n, (when, live2, rec3) in enumerate(plan):
            self.save("nvr-01", when, [channel(1), channel(2, live=live2, name="Yard"),
                                       channel(3, status=rec3, name="=Store")],
                      overall="CRITICAL" if n == 3 else "WARNING" if live2 == "OFFLINE" else "HEALTHY",
                      disks=[disk(1), disk(2, "error" if n == 3 else "ok")], restart=n == 2,
                      changes=[{"type": "renamed", "channel_id": "3", "old": "Store", "new": "=Store"}] if n == 1 else [])
        self.save("nvr-01", at(TODAY, 0, 1), [channel(1), channel(2, name="Yard"), channel(3, name="=Store")])
        self.save("nvr-02", at(D2, 8), [channel(1, name="Gate"), channel(2, name="ឃ្លាំង")], reachable=False)

    def save(self, nvr_id, when, channels, overall="HEALTHY", disks=(), restart=False, changes=(), reachable=True):
        data = {"schema_version": 2, "nvr": {"id": nvr_id, "name": "Kiln" if nvr_id == "nvr-01" else "Mill"},
                "check_completed_at": when.isoformat(),
                "device_info": {"status": "DEVICE_INFO_RETRIEVED" if reachable else "TIMEOUT"},
                "channel_inventory": {"expected_count": len(channels), "configured_count": len(channels),
                                      "channels": channels},
                "channel_status": {"online_count": sum(c["reported_status"] == "ONLINE" for c in channels),
                                   "offline_count": sum(c["reported_status"] == "OFFLINE" for c in channels)},
                "storage": {"status": "CRITICAL" if any(d["reported_status"] == "error" for d in disks) else "HEALTHY",
                            "message": "", "disks": list(disks)},
                "per_camera_recording": {"enabled": True, "window_minutes": 60},
                "restart": {"detected": restart}, "inventory_changes": {"changes": list(changes)},
                "overall": {"status": overall, "critical_reasons": ["Storage needs urgent review: 2 (error)"]
                            if overall == "CRITICAL" else [], "warnings": []}}
        name = f"{nvr_id}_{when.strftime('%Y%m%dT%H%M%S%f%z')}.json"
        (self.reports / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def query(self, **raw):
        """One data set in the one-table form saved before (still accepted)."""
        spec, error = cr.normalize(raw)
        self.assertIsNone(error, error)
        result = cr.build(spec, self.reports)["sections"][0]
        return [dict(zip([c["key"] for c in result["columns"]], row)) for row in result["rows"]], result


class QueryTests(Fixture):
    def test_checks_and_defaults(self):
        spec, error = cr.normalize({"dataset": "camera_uptime"})
        self.assertIsNone(error)
        self.assertEqual((spec["date_from"], spec["date_to"]), ((TODAY - timedelta(days=6)).isoformat(), TODAY.isoformat()))
        self.assertEqual(spec["sections"][0]["sort"], {"column": "online_pct", "desc": False})
        self.assertIn("online_pct", spec["sections"][0]["columns"])
        bad = [({"dataset": "x"}, "Choose what"), ({"dataset": "scans", "date_to": "2999-01-01"}, "future"),
               ({"dataset": "scans", "date_from": D3.isoformat(), "date_to": D1.isoformat()}, "after the end"),
               ({"dataset": "scans", "date_from": "2020-01-01"}, "at most 366"),
               ({"dataset": "scans", "columns": ["nope"]}, "column"),
               ({"dataset": "scans", "nvrs": ["../x"]}, "NVRs"),
               ({"dataset": "scans", "conditions": [{"column": "online", "op": "gt", "value": ""}]}, "value"),
               ({"dataset": "scans", "conditions": [{"column": "online", "op": "gt", "value": "abc"}]}, "number"),
               ({"dataset": "scans", "limit": 0}, "limit")]
        for raw, words in bad:
            self.assertIn(words, cr.normalize(raw)[1], raw)
        spec, _ = cr.normalize({"dataset": "cameras", "date_from": "2001-01-01", "date_to": D2.isoformat()})
        self.assertEqual(spec["date_from"], D2.isoformat())                          # "On a date" ignores From.
        sections = [{"dataset": "scans"}, {"dataset": "scans"}]
        self.assertIn("once", cr.normalize({"sections": sections})[1])
        self.assertIn("Choose what", cr.normalize({"sections": []})[1])
        bad = cr.normalize({"sections": [{"dataset": "disks"}, {"dataset": "scans", "columns": ["nope"]}]})[1]
        self.assertEqual(bad, "Scan log: Choose at least one column.")              # Says which data set.

    def test_camera_uptime_over_a_period_with_conditions(self):
        rows, result = self.query(dataset="camera_uptime", nvrs=["nvr-01"], date_from=D1.isoformat(),
                                  date_to=D3.isoformat(), columns=["channel", "camera", "samples", "online_pct",
                                                                  "offline_scans", "recording_pct", "recording_checks",
                                                                  "changes", "last_offline", "last_live"])
        cams = {r["channel"]: r for r in rows}
        self.assertEqual(sorted(cams), [1, 2, 3])
        self.assertEqual((cams[2]["samples"], cams[2]["online_pct"], cams[2]["offline_scans"], cams[2]["changes"]),
                         (4, 50.0, 2, 3))
        self.assertEqual(cams[2]["last_offline"], f"{D3.isoformat()} 23:59")      # 23:59 on the last day is in.
        self.assertEqual(cams[2]["last_live"], "Offline")
        self.assertEqual((cams[3]["recording_checks"], cams[3]["recording_pct"]), (3, 66.7))  # Failed check left out.
        self.assertEqual(result["meta"]["source"], "4 saved scan reports")         # Today's 00:01 report is not.
        self.assertTrue(result["query"].startswith("SELECT Channel, Camera"))
        self.assertIn("NVR IN (nvr-01)", result["query"])
        rows, _ = self.query(dataset="camera_uptime", date_from=D1.isoformat(), date_to=D3.isoformat(),
                             conditions=[{"column": "online_pct", "op": "lt", "value": "100"}])
        self.assertEqual([(r["nvr"], r["channel"]) for r in rows], [("nvr-01", 2)])
        rows, _ = self.query(dataset="camera_uptime", date_from=D1.isoformat(), date_to=D3.isoformat(),
                             conditions=[{"column": "camera", "op": "contains", "value": "ឃ្លាំ"}])
        self.assertEqual([r["nvr"] for r in rows], ["nvr-02"])

    def test_scan_log_nvr_health_and_changes(self):
        rows, _ = self.query(dataset="scans", date_from=D1.isoformat(), date_to=D3.isoformat(),
                             columns=["time", "nvr", "status", "offline", "recorded", "storage", "restart"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["time"], f"{D3.isoformat()} 23:59")               # Newest first by default.
        self.assertEqual((rows[0]["status"], rows[0]["storage"]), ("Critical", "Critical"))
        rows, _ = self.query(dataset="scans", date_from=D1.isoformat(), date_to=D3.isoformat(),
                             conditions=[{"column": "restart", "op": "is", "value": "Yes"}])
        self.assertEqual(len(rows), 1)
        rows, _ = self.query(dataset="scans", date_from=D1.isoformat(), date_to=D3.isoformat(),
                             conditions=[{"column": "time", "op": "le", "value": D1.isoformat()}])
        self.assertEqual(len(rows), 2)                                              # A date alone = whole day.
        health, _ = self.query(dataset="nvr_health", date_from=D1.isoformat(), date_to=D3.isoformat(),
                               columns=["nvr", "reports", "reachable_pct", "critical", "restarts", "max_offline",
                                        "last_status"])
        by = {r["nvr"]: r for r in health}
        self.assertEqual((by["nvr-01"]["reports"], by["nvr-01"]["critical"], by["nvr-01"]["restarts"],
                          by["nvr-01"]["max_offline"], by["nvr-01"]["last_status"]), (4, 1, 1, 1, "Critical"))
        self.assertEqual(by["nvr-02"]["reachable_pct"], 0.0)
        changes, _ = self.query(dataset="changes", date_from=D1.isoformat(), date_to=D3.isoformat())
        self.assertEqual([(c["channel"], c["change"], c["old"], c["new"]) for c in changes],
                         [(3, "Renamed", "Store", "=Store")])

    def test_cameras_and_disks_on_a_past_date_and_today(self):
        rows, result = self.query(dataset="cameras", date_to=D3.isoformat(), nvrs=["nvr-01"])
        self.assertEqual({r["channel"]: r["live"] for r in rows}, {1: "Online", 2: "Offline", 3: "Online"})
        self.assertEqual(rows[2]["recording"], "No footage in window")
        self.assertIn("up to", result["meta"]["source"])
        rows, result = self.query(dataset="cameras", date_to=TODAY.isoformat(),
                                  conditions=[{"column": "live", "op": "is", "value": "Offline"}])
        self.assertEqual(rows, [])                                                   # Today: the dashboard now.
        self.assertIn("dashboard now", result["meta"]["source"])
        disks, _ = self.query(dataset="disks", date_to=D3.isoformat(),
                              conditions=[{"column": "problem", "op": "is", "value": "Yes"}])
        self.assertEqual([(d["nvr"], d["hdd"], d["status"], d["used_pct"]) for d in disks],
                         [("nvr-01", "2", "error", 75.0)])

    def test_events_notes_sort_and_limit(self):
        lines = [{"nvr": "nvr-01", "type": "diskerror", "label": "Disk error", "severity": "critical", "channel": "",
                  "at": at(D2, 9).isoformat()},
                 {"nvr": "nvr-01", "type": "videoloss", "label": "Video loss", "severity": "critical", "channel": "2",
                  "at": at(D3, 9).isoformat()},
                 {"nvr": "nvr-02", "type": "videoloss", "label": "Video loss", "severity": "critical", "channel": "1",
                  "at": at(TODAY - timedelta(days=9), 9).isoformat()}]
        (self.root / "events" / "events.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
        rows, _ = self.query(dataset="events", date_from=D1.isoformat(), date_to=D3.isoformat())
        self.assertEqual([(r["event"], r["channel"], r["nvr_name"]) for r in rows],
                         [("Video loss", 2, "Kiln"), ("Disk error", None, "Kiln")])
        notes_store.add_known_issue("nvr-01", "2", "Cable cut", None, "Heng")
        notes_store.clear_known_issue("nvr-01", "2", "Heng", "New cable")
        rows, _ = self.query(dataset="notes", date_from=TODAY.isoformat(), date_to=TODAY.isoformat())
        self.assertEqual(sorted(r["kind"] for r in rows), ["Fixed", "Known issue"])
        rows, result = self.query(dataset="camera_uptime", date_from=D1.isoformat(), date_to=D3.isoformat(),
                                  sort={"column": "changes", "desc": True}, limit=2)
        self.assertEqual(rows[0]["channel"], 2)
        self.assertEqual((len(rows), result["total"], result["truncated"]), (2, 5, True))
        self.assertIn("LIMIT 2", result["query"])


class SeveralDataSetsTests(Fixture):
    def test_each_data_set_keeps_its_own_columns_conditions_and_dates(self):
        spec, error = cr.normalize({
            "nvrs": ["nvr-01"], "date_from": D1.isoformat(), "date_to": D3.isoformat(),
            "sections": [{"dataset": "camera_uptime", "columns": ["channel", "online_pct"],
                          "conditions": [{"column": "online_pct", "op": "lt", "value": "100"}]},
                         {"dataset": "disks", "columns": ["hdd", "status"], "sort": {"column": "hdd", "desc": True}},
                         {"dataset": "changes"}]})
        self.assertIsNone(error)
        first, disks, changes = cr.build(spec, self.reports)["sections"]
        self.assertEqual((first["label"], [c["key"] for c in first["columns"]], first["rows"]),
                         ("Camera uptime", ["channel", "online_pct"], [[2, 50.0]]))
        self.assertEqual((disks["mode"], disks["date_from"], disks["date_to"]), ("as_of", D3.isoformat(), D3.isoformat()))
        self.assertEqual(disks["rows"], [["2", "error"], ["1", "ok"]])               # Its own sort, on the To date.
        self.assertEqual(len(changes["rows"]), 1)
        self.assertIn("FROM Disks ON", disks["query"])
        self.assertIn(f"FROM Camera uptime FROM {D1.isoformat()} TO {D3.isoformat()}", first["query"])

        item, error = cr.save("Weekly check", spec, cr.build(spec, self.reports))
        self.assertIsNone(error)
        self.assertEqual((item["labels"], item["section_rows"], item["rows"]),
                         (["Camera uptime", "Disks", "Camera changes"], [1, 2, 1], 4))
        doc = cr.load_saved(item["id"])
        body, content_type, extension = cr.csv_download(doc)
        self.assertEqual((content_type, extension), ("application/zip", "zip"))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(archive.namelist(), ["01-Camera-uptime.csv", "02-Disks.csv", "03-Camera-changes.csv"])
            self.assertTrue(archive.read("02-Disks.csv").startswith(b"\xef\xbb\xbfHDD,Status"))
        pdf = cr.to_pdf(doc)
        self.assertTrue(pdf.startswith(b"%PDF"))
        with patch.object(cr, "MAX_TOTAL_ROWS", 3):
            self.assertIn("rows in total", cr.save("Too big", spec, cr.build(spec, self.reports))[1])

    def test_reports_saved_as_one_table_still_open_and_download(self):
        old = {"schema_version": 1, "id": "abcdef012345", "name": "Old one", "created_at": "2026-11-06T08:00:00+00:00",
               "spec": {"dataset": "scans", "nvrs": [], "date_from": D1.isoformat(), "date_to": D3.isoformat(),
                        "columns": ["time", "nvr"], "conditions": [], "sort": {"column": "time", "desc": True},
                        "limit": None},
               "query": "SELECT Time, NVR FROM Scan log", "columns": [{"key": "time", "label": "Time", "kind": "time"},
                                                                     {"key": "nvr", "label": "NVR", "kind": "text"}],
               "rows": [["2026-11-05 08:00", "nvr-01"]], "total": 1, "truncated": False, "meta": {}}
        (self.root / "saved" / "abcdef012345.json").write_text(json.dumps(old))
        listed = cr.list_saved()
        self.assertEqual((listed[0]["labels"], listed[0]["rows"]), (["Scan log"], 1))
        self.assertEqual(listed[0]["spec"]["sections"][0]["dataset"], "scans")         # "Use query" works.
        doc = cr.load_saved("abcdef012345")
        self.assertEqual(cr.csv_download(doc)[1:], ("text/csv; charset=utf-8", "csv"))
        self.assertTrue(cr.to_pdf(doc).startswith(b"%PDF"))
        self.assertIsNone(cr.normalize(listed[0]["spec"])[1])


class SaveAndDownloadTests(Fixture):
    def test_save_list_download_delete(self):
        spec, _ = cr.normalize({"dataset": "camera_uptime", "date_from": D1.isoformat(), "date_to": D3.isoformat()})
        result = cr.build(spec, self.reports)
        item, error = cr.save("  របាយការណ៍ Offline\x00 cameras  ", spec, result)
        self.assertIsNone(error)
        self.assertEqual(item["name"], "របាយការណ៍ Offline cameras")
        self.assertEqual(cr.save("   ", spec, result), (None, "Give the report a name."))
        listed = cr.list_saved()
        self.assertEqual([i["id"] for i in listed], [item["id"]])
        (self.root / "saved" / "index.json").unlink()                               # Rebuilt from the files.
        self.assertEqual(cr.list_saved()[0]["rows"], 5)
        doc = cr.load_saved(item["id"])
        csv_bytes, content_type, extension = cr.csv_download(doc)
        self.assertEqual(extension, "csv")
        self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbfNVR,NVR name,Channel,Camera"))
        text = csv_bytes.decode("utf-8")
        self.assertIn("'=Store", text)                                              # No spreadsheet formulas.
        self.assertIn("ឃ្លាំង", text)
        self.assertIn("50.0", text)
        pdf = cr.to_pdf(doc)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(cr.file_stem(doc["name"]), "Offline-cameras")
        self.assertIsNone(cr.load_saved("../../etc"))
        self.assertTrue(cr.delete_saved(item["id"]))
        self.assertFalse(cr.delete_saved(item["id"]))
        self.assertEqual(cr.list_saved(), [])


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        for target, value in (("DASHBOARD_USERNAME", "demo"), ("DASHBOARD_PASSWORD", "test-password-long")):
            patcher = patch.object(dashboard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.auth = "Basic " + base64.b64encode(b"demo:test-password-long").decode()

    def get(self, path):
        with urlopen(Request(self.base + path, headers={"Authorization": self.auth}), timeout=10) as response:
            return response.status, dict(response.headers), response.read()

    def post(self, body, header="custom-reports"):
        request = Request(self.base + "/api/custom-reports", data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": self.auth, "Content-Type": "application/json",
                                   "X-NVR-Track": header})
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_page_options_preview_save_download_delete(self):
        with self.assertRaises(HTTPError) as ctx:
            urlopen(self.base + "/reports", timeout=5)
        self.assertEqual(ctx.exception.code, 401)
        status, headers, page = self.get("/reports")
        self.assertEqual(status, 200)
        self.assertIn(b"Report builder", page)
        self.assertIn("script-src 'self';", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertIn(b'src="/static/report-builder/report-builder.js"', page)
        self.assertNotIn(b' style="', page)                                          # CSP blocks style attributes.
        self.assertIn(b'href="/reports"', self.get("/")[2])                          # Link from the dashboard.
        _, _, body = self.get("/api/custom-reports")
        options = json.loads(body)
        self.assertEqual([n["id"] for n in options["nvrs"]], ["nvr-01", "nvr-02"])
        self.assertEqual(options["oldest"], D1.isoformat())
        self.assertIn("camera_uptime", [d["key"] for d in options["datasets"]])
        self.assertEqual(options["saved"], [])

        spec = {"date_from": D1.isoformat(), "date_to": D3.isoformat(),
                "sections": [{"dataset": "camera_uptime",
                              "conditions": [{"column": "offline_scans", "op": "gt", "value": "0"}]}]}
        self.assertEqual(self.post({"action": "preview", "spec": spec}, header="scan")[0], 403)
        status, preview = self.post({"action": "preview", "spec": spec})
        self.assertEqual((status, [x["total"] for x in preview["sections"]]), (200, [1]))
        both = {**spec, "sections": spec["sections"] + [{"dataset": "disks"}]}
        status, preview = self.post({"action": "preview", "spec": both})
        self.assertEqual([x["label"] for x in preview["sections"]], ["Camera uptime", "Disks"])
        self.assertEqual(self.post({"action": "preview", "spec": {"dataset": "nope"}})[0], 400)
        status, saved = self.post({"action": "save", "name": "Offline – last days", "spec": spec})
        self.assertEqual(status, 200)
        rid = saved["item"]["id"]
        status, headers, body = self.get(f"/api/custom-reports/download?id={rid}&format=csv")
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('filename="Offline-last-days.csv"', headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''Offline%20%E2%80%93%20last%20days.csv", headers["Content-Disposition"])
        self.assertIn("Yard".encode(), body)
        status, headers, body = self.get(f"/api/custom-reports/download?id={rid}&format=pdf")
        self.assertEqual((headers["Content-Type"], body[:4]), ("application/pdf", b"%PDF"))
        for bad in (f"?id={rid}&format=exe", "?id=zz&format=csv"):
            with self.assertRaises(HTTPError) as ctx:
                self.get("/api/custom-reports/download" + bad)
            self.assertEqual(ctx.exception.code, 404)
        status, saved = self.post({"action": "save", "name": "Two parts", "spec": both})
        status, headers, body = self.get(f"/api/custom-reports/download?id={saved['item']['id']}&format=csv")
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn('filename="Two-parts.zip"', headers["Content-Disposition"])
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(body)).namelist()), 2)
        self.post({"action": "delete", "id": saved["item"]["id"]})
        status, after = self.post({"action": "delete", "id": rid})
        self.assertEqual((status, after["saved"]), (200, []))

    def test_compose_mounts_the_saved_reports_folder(self):
        if not (ROOT / "compose.yaml").exists():
            self.skipTest("compose.yaml is not part of this test image")
        text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        block = text.split("  nvr-dashboard:", 1)[1].split("\n  nvr-", 1)[0]
        self.assertIn("- ./state/custom_reports:/app/state/custom_reports\n", block)
        self.assertIn("CUSTOM_REPORTS_DIR: /app/state/custom_reports", block)
        self.assertNotIn("env_file", block)                                          # Still no NVR credentials.


if __name__ == "__main__":
    unittest.main()
