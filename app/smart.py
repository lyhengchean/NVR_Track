"""Read-only disk SMART health from Hikvision NVRs.

GET /ISAPI/ContentMgmt/Storage/hdd/<id>/SMARTTest/status for each installed
SATA disk, one after another. Hikvision spells some fields oddly
(temprature, selfEvaluaingStatus); both spellings are accepted. A reply that
cannot be recognised is UNVERIFIED and never raises an alert. Enable per NVR
with NVR_SMART_NVR_IDS after scripts.probe_nvr_features shows it works.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException

HEALTHY, WARNING, CRITICAL = "HEALTHY", "WARNING", "CRITICAL"
UNVERIFIED, NOT_CONFIGURED = "UNVERIFIED", "NOT_CONFIGURED"
RANK = {CRITICAL: 0, WARNING: 1, UNVERIFIED: 2, HEALTHY: 3, NOT_CONFIGURED: 4}
# Verdict words. Hikvision's web page shows e.g. "Self-evaluation: Pass" and
# "All-evaluation: Functional". Only an explicit failure word raises an alarm;
# a word not listed here makes the disk UNVERIFIED (shown, never alerted).
GOOD_VERDICTS = {"ok", "normal", "pass", "passed", "good", "functional", "healthy", "true"}
BAD_VERDICTS = {"fail", "failed", "failing", "fault", "faulty", "error", "bad", "abnormal",
                "not functional", "notfunctional", "false"}
WARN_VERDICTS = {"warning", "warn", "caution", "degraded"}
# SMART attributes whose raw value counts damaged sectors.
SECTOR_ATTRIBUTES = {5: "reallocated sectors", 197: "pending sectors", 198: "uncorrectable sectors"}
TEMP_WARNING, TEMP_CRITICAL = 55, 60
# Bump when the classification changes: older cached results are then re-checked.
RULES_VERSION = 2
SECTOR_CRITICAL = 100


def _name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first(root: ET.Element, *names: str) -> str | None:
    wanted = {n.lower() for n in names}
    for node in root.iter():
        if _name(node.tag) in wanted and node.text and node.text.strip():
            return node.text.strip()
    return None


def _int(text: str | None) -> int | None:
    try:
        value = int(float(text)) if text is not None else None
    except ValueError:
        return None
    return value if value is not None and 0 <= value < 2**32 else None


def parse_smart(content: bytes) -> dict:
    """Fields from SMARTTestStatus; raises ValueError if it is not one."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("SMART reply is not XML") from exc
    if _name(root.tag) not in {"smartteststatus", "smartstatus"}:
        raise ValueError("Unexpected SMART XML root")
    attributes = {}
    for node in root.iter():
        if _name(node.tag) != "testresult":
            continue
        attr_id = _int(_first(node, "attributeID", "id"))
        if attr_id is None:
            continue
        attributes[attr_id] = {"status": (_first(node, "status") or "").lower(),
                               "raw": _int(_first(node, "rawValue"))}
    return {
        "temperature_c": _int(_first(root, "temprature", "temperature")),
        "power_on_days": _int(_first(root, "powerOnDay", "powerOnDays")),
        "self_assessment": (_first(root, "selfEvaluaingStatus", "selfEvaluatingStatus") or "").lower() or None,
        "overall_assessment": (_first(root, "allEvaluaingStatus", "allEvaluatingStatus") or "").lower() or None,
        "attributes": attributes,
    }


def classify_disk(parsed: dict) -> tuple[str, str, dict]:
    """(status, message, sector counts) for one disk."""
    sectors = {label: parsed["attributes"].get(attr, {}).get("raw")
               for attr, label in SECTOR_ATTRIBUTES.items()}
    known = [parsed.get("temperature_c"), parsed.get("self_assessment"), parsed.get("overall_assessment"),
             *sectors.values()]
    if all(value is None for value in known) and not parsed["attributes"]:
        return UNVERIFIED, "SMART fields were not recognised.", sectors
    critical, warning, unrecognised = [], [], []
    for field, label in (("self_assessment", "self-assessment"), ("overall_assessment", "overall assessment")):
        verdict = (parsed.get(field) or "").strip()
        if not verdict or verdict in GOOD_VERDICTS:
            continue
        if verdict in BAD_VERDICTS:
            critical.append(f"SMART {label} {verdict}")
        elif verdict in WARN_VERDICTS:
            warning.append(f"SMART {label} {verdict}")
        else:
            unrecognised.append(f"{label} '{verdict[:20]}'")
    for label, raw in sectors.items():
        if raw:
            (critical if raw >= SECTOR_CRITICAL else warning).append(f"{raw} {label}")
    failing = sorted(a for a, v in parsed["attributes"].items()
                     if v.get("status") in BAD_VERDICTS | WARN_VERDICTS)
    if failing:
        warning.append("attributes not OK: " + ", ".join(str(a) for a in failing[:8]))
    temp = parsed.get("temperature_c")
    if temp is not None and temp >= TEMP_CRITICAL:
        critical.append(f"temperature {temp} °C")
    elif temp is not None and temp >= TEMP_WARNING:
        warning.append(f"temperature {temp} °C")
    if critical:
        return CRITICAL, "; ".join(critical + warning) + ". Replace the disk soon.", sectors
    if warning:
        return WARNING, "; ".join(warning) + ". Watch this disk.", sectors
    if unrecognised:
        return UNVERIFIED, ("Unrecognised SMART verdict: " + ", ".join(unrecognised)
                            + (f"; {temp} °C" if temp is not None else "") + ". Not counted as a fault."), sectors
    return HEALTHY, "SMART OK" + (f", {temp} °C" if temp is not None else "") + ".", sectors


def query_disk(session: requests.Session, base: str, auth, disk_id: str) -> tuple[str, bytes | None]:
    try:
        response = session.get(f"{base}/ISAPI/ContentMgmt/Storage/hdd/{disk_id}/SMARTTest/status",
                               auth=auth, headers={"Accept": "application/xml"},
                               timeout=(3, 10), allow_redirects=False)
    except RequestException:
        return "CONNECTION_ERROR", None
    if response.status_code in (401, 403):
        return "PERMISSION_DENIED", None
    if response.status_code in (404, 405, 501):
        return "UNSUPPORTED", None
    if response.status_code != 200:
        return f"HTTP_{response.status_code}", None
    return "OK", response.content[:200_000]


def check_smart(ip: str, port: int, username: str, password: str, disk_ids: list[str],
                deadline: float | None = None) -> dict:
    """Query each disk once, sequentially. Never raises."""
    result = {"status": UNVERIFIED, "checked_at": datetime.now(timezone.utc).isoformat(),
              "from_cache": False, "disks": [], "message": "", "rules": RULES_VERSION}
    if not disk_ids:
        result["message"] = "No installed SATA disks to check."
        return result
    base = f"http://{ip}:{port}"
    auth = HTTPDigestAuth(username, password)
    with requests.Session() as session:
        session.trust_env = False
        for disk_id in disk_ids:
            if deadline is not None and time.monotonic() >= deadline:
                result["disks"].append({"id": disk_id, "status": UNVERIFIED, "message": "Time budget reached."})
                continue
            code, body = query_disk(session, base, auth, disk_id)
            if code != "OK" or body is None:
                result["disks"].append({"id": disk_id, "status": UNVERIFIED,
                                        "message": f"SMART not available ({code})."})
                continue
            try:
                parsed = parse_smart(body)
            except ValueError:
                result["disks"].append({"id": disk_id, "status": UNVERIFIED,
                                        "message": "SMART reply not recognised."})
                continue
            status, message, sectors = classify_disk(parsed)
            result["disks"].append({
                "id": disk_id, "status": status, "message": message,
                "temperature_c": parsed["temperature_c"], "power_on_days": parsed["power_on_days"],
                "self_assessment": parsed["self_assessment"],
                "overall_assessment": parsed["overall_assessment"],
                "reallocated": sectors["reallocated sectors"], "pending": sectors["pending sectors"],
                "uncorrectable": sectors["uncorrectable sectors"],
            })
    result["status"] = min((d["status"] for d in result["disks"]), key=lambda s: RANK[s])
    bad = [d for d in result["disks"] if d["status"] in {CRITICAL, WARNING}]
    result["message"] = ("; ".join(f"HDD {d['id']}: {d['message']}" for d in bad) if bad
                         else "All checked disks report SMART OK." if result["status"] == HEALTHY
                         else "SMART could not be verified for every disk.")
    return result


def cached(previous: dict | None, disk_ids: list[str], hours: int) -> dict | None:
    """Reuse a recent SMART result for the same disks (SMART changes slowly)."""
    if not isinstance(previous, dict) or previous.get("status") not in RANK:
        return None
    if previous.get("rules") != RULES_VERSION:
        return None  # Classified with older rules: check again.
    try:
        checked = datetime.fromisoformat(previous["checked_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if checked.tzinfo is None or datetime.now(timezone.utc) - checked > timedelta(hours=hours):
        return None
    if sorted(str(d.get("id")) for d in previous.get("disks", [])) != sorted(disk_ids):
        return None
    return {**previous, "from_cache": True}


def not_configured() -> dict:
    return {"status": NOT_CONFIGURED, "checked_at": None, "from_cache": False, "disks": [],
            "message": "SMART checks are not enabled for this NVR (NVR_SMART_NVR_IDS)."}
