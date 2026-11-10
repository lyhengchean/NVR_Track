"""A small fake Hikvision NVR for end-to-end tests (127.0.0.1 only).

Serves the read-only ISAPI endpoints NVR Track uses. Behaviour is set
through the FakeNVR attributes; every request is recorded in `requests`.
"""
from __future__ import annotations

import io
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ICT = timezone(timedelta(hours=7))


def jpeg(kind: str, seed: int = 0) -> bytes:
    from PIL import Image
    if kind == "black":
        image = Image.new("RGB", (320, 180), (2, 2, 2))
    else:
        image = Image.new("RGB", (320, 180))
        pixels = image.load()
        for x in range(320):
            for y in range(180):
                pixels[x, y] = ((x * 3 + seed) % 256, (y * 5) % 256, ((x + y) * 2) % 256)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=80)
    return out.getvalue()


class FakeNVR:
    def __init__(self, tls=None):
        self.channels = {"1": ("Gate", True), "2": ("Yard", True), "3": ("Store", True), "4": ("Roof", False)}
        self.camera_ips: dict[str, str] = {}   # Channel -> camera IP (default 192.0.2.<channel>).
        self.source_inputs: dict[str, int] = {}  # Channel -> srcInputPort (default 1).
        self.recording = {"1", "2", "3"}       # Channels with footage now.
        self.search_replies: dict[str, str] = {}  # Channel -> raw CMSearchResult XML to return instead.
        self.uptime = 10 * 86400
        self.smart_raw_reallocated = {"1": 0, "2": 8}
        self.pictures = {"1": "scene", "2": "scene", "3": "black"}
        self.picture_seed = 0
        self.events = []                        # Raw XML documents for the alert stream.
        self.disk_states = {"1": "ok", "2": "ok"}  # HDD id -> status in the HDD list.
        self.requests: list[tuple[str, str]] = []
        self.forbidden: set[str] = set()       # Path prefixes answered 403 (a login without that right).
        self.refuse_login = False              # Answer everything 401 (wrong username or password).
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        if tls is not None:                    # An ssl.SSLContext: serve HTTPS instead.
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ----- XML documents -----

    def channel_list(self) -> str:
        items = "".join(
            f"<InputProxyChannel><id>{cid}</id><name>{name}</name><sourceInputPortDescriptor>"
            f"<ipAddress>{self.camera_ips.get(cid, f'192.0.2.{cid}')}</ipAddress><managePortNo>8000</managePortNo>"
            f"<srcInputPort>{self.source_inputs.get(cid, 1)}</srcInputPort></sourceInputPortDescriptor>"
            f"</InputProxyChannel>" for cid, (name, _) in self.channels.items())
        return f'<InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">{items}</InputProxyChannelList>'

    def channel_status(self) -> str:
        items = "".join(f"<InputProxyChannelStatus><id>{cid}</id><online>{str(on).lower()}</online>"
                        f"</InputProxyChannelStatus>" for cid, (_, on) in self.channels.items())
        return f"<InputProxyChannelStatusList>{items}</InputProxyChannelStatusList>"

    def search(self, body: bytes) -> str:
        root = ET.fromstring(body)
        track = int(next(n.text for n in root.iter() if n.tag.endswith("trackID")))
        start = next(n.text for n in root.iter() if n.tag.endswith("startTime"))
        end = next(n.text for n in root.iter() if n.tag.endswith("endTime"))
        channel = str(track // 100)
        window_end = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
        window_start = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
        if channel in self.search_replies:
            return self.search_replies[channel]
        if channel not in self.recording:
            return ("<CMSearchResult><responseStatus>false</responseStatus>"
                    "<responseStatusStrg>NO MATCHES</responseStatusStrg><numOfMatches>0</numOfMatches></CMSearchResult>")
        seg_start = max(window_start, window_end - timedelta(minutes=30))
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return ("<CMSearchResult><responseStatus>true</responseStatus><responseStatusStrg>OK</responseStatusStrg>"
                "<numOfMatches>1</numOfMatches><matchList><searchMatchItem>"
                f"<trackID>{track}</trackID><timeSpan><startTime>{seg_start.strftime(fmt)}</startTime>"
                f"<endTime>{(window_end - timedelta(minutes=1)).strftime(fmt)}</endTime></timeSpan>"
                "</searchMatchItem></matchList></CMSearchResult>")

    def smart(self, disk: str) -> str:
        return ('<SMARTTestStatus version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                "<temprature>41</temprature><powerOnDay>800</powerOnDay>"
                "<selfEvaluaingStatus>ok</selfEvaluaingStatus><allEvaluaingStatus>ok</allEvaluaingStatus>"
                "<TestResultList>"
                f"<TestResult><attributeID>5</attributeID><status>ok</status><rawValue>{self.smart_raw_reallocated.get(disk, 0)}</rawValue></TestResult>"
                "<TestResult><attributeID>197</attributeID><status>ok</status><rawValue>0</rawValue></TestResult>"
                "</TestResultList></SMARTTestStatus>")

    def _handler(self):
        nvr = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, content_type="application/xml", code=200):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                nvr.requests.append(("GET", self.path))
                path = self.path.split("?")[0]
                if nvr.refuse_login:
                    return self.reply("<ResponseStatus/>", code=401)
                if any(path.startswith(prefix) for prefix in nvr.forbidden):
                    return self.reply("<ResponseStatus><statusCode>4</statusCode></ResponseStatus>", code=403)
                now = datetime.now(ICT).replace(microsecond=0)
                if path == "/ISAPI/System/deviceInfo":
                    return self.reply("<DeviceInfo><model>DS-8664NI-I8</model><firmwareVersion>V4.1</firmwareVersion></DeviceInfo>")
                if path == "/ISAPI/ContentMgmt/InputProxy/channels":
                    return self.reply(nvr.channel_list())
                if path == "/ISAPI/ContentMgmt/InputProxy/channels/status":
                    return self.reply(nvr.channel_status())
                if path == "/ISAPI/ContentMgmt/Storage/hdd":
                    disks = "".join(f"<hdd><id>{i}</id><hddName>hdd{i}</hddName><hddType>SATA</hddType>"
                                    f"<status>{state}</status><capacity>4000000</capacity><freeSpace>1000000</freeSpace>"
                                    f"<property>RW</property></hdd>" for i, state in nvr.disk_states.items())
                    return self.reply(f"<hddList>{disks}</hddList>")
                if path == "/ISAPI/ContentMgmt/record/tracks":
                    tracks = "".join(f"<Track><id>{int(c) * 100 + 1}</id></Track>" for c in nvr.channels)
                    return self.reply(f"<TrackList>{tracks}</TrackList>")
                if path == "/ISAPI/System/time":
                    return self.reply(f"<Time><localTime>{now.isoformat()}</localTime></Time>")
                if path == "/ISAPI/System/status":
                    return self.reply(f"<DeviceStatus><CPUList><CPU><cpuUtilization>12</cpuUtilization></CPU></CPUList>"
                                      f"<deviceUpTime>{nvr.uptime}</deviceUpTime></DeviceStatus>")
                match = re.fullmatch(r"/ISAPI/ContentMgmt/Storage/hdd/(\d+)/SMARTTest/status", path)
                if match:
                    return self.reply(nvr.smart(match[1]))
                match = re.fullmatch(r"/ISAPI/Streaming/channels/(\d+)/picture", path)
                if match:
                    kind = nvr.pictures.get(str(int(match[1]) // 100))
                    if kind is None:
                        return self.reply("<ResponseStatus/>", code=404)
                    return self.reply(jpeg(kind, nvr.picture_seed), "image/jpeg")
                if path == "/ISAPI/Event/notification/alertStream":
                    body = "".join("--boundary\r\nContent-Type: application/xml\r\n\r\n" + doc + "\r\n"
                                   for doc in nvr.events).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/mixed; boundary=boundary")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.reply("<ResponseStatus/>", code=404)

            def do_POST(self):
                nvr.requests.append(("POST", self.path))
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if nvr.refuse_login:
                    return self.reply("<ResponseStatus/>", code=401)
                if any(self.path.startswith(prefix) for prefix in nvr.forbidden):
                    return self.reply("<ResponseStatus><statusCode>4</statusCode></ResponseStatus>", code=403)
                if self.path == "/ISAPI/ContentMgmt/search":
                    return self.reply(nvr.search(body))
                match = re.fullmatch(r"/ISAPI/ContentMgmt/record/tracks/(\d+)/dailyDistribution", self.path)
                if match:
                    root = ET.fromstring(body)
                    year = int(next(n.text for n in root.iter() if n.tag.endswith("year")))
                    month = int(next(n.text for n in root.iter() if n.tag.endswith("monthOfYear")))
                    import calendar
                    days = "".join(f"<day><id>{d}</id><dayOfMonth>{d}</dayOfMonth><record>true</record></day>"
                                   for d in range(1, calendar.monthrange(year, month)[1] + 1))
                    return self.reply(f"<trackDailyDistribution><dayList>{days}</dayList></trackDailyDistribution>")
                self.reply("<ResponseStatus/>", code=404)

        return Handler


def event_xml(event_type: str, channel: str = "1", state: str = "active", count: int = 1) -> str:
    return ('<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            f"<ipAddress>127.0.0.1</ipAddress><channelID>{channel}</channelID>"
            f"<dateTime>2026-11-04T10:00:00+07:00</dateTime><activePostCount>{count}</activePostCount>"
            f"<eventType>{event_type}</eventType><eventState>{state}</eventState>"
            f"<eventDescription>{event_type} alarm</eventDescription></EventNotificationAlert>")
