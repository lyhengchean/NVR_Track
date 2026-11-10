"""Read-only Hikvision NVR storage-health checks."""

from dataclasses import dataclass
from ipaddress import ip_address
import math
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException


# Disk failures and absent slots need separate classification. "notexist" may
# describe an unused bay; the installed SATA IDs must be confirmed separately.
CRITICAL_DISK_STATES = {
    "error", "offline", "smartfailed", "unformatted", "mismatch",
}
ABSENT_DISK_STATES = {"notexist"}
NORMAL_DISK_STATES = {"ok", "normal", "idle"}


@dataclass(frozen=True)
class HDDEntry:
    id: str
    name: str
    disk_type: str
    status: str
    capacity_mb: float | None
    free_space_mb: float | None
    property: str
    group: str


@dataclass(frozen=True)
class StorageCheck:
    status: str
    http_status: int | None
    disks: tuple[HDDEntry, ...]
    message: str


def _name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, field: str) -> str:
    """Read a direct child field, avoiding accidental nested-field matches."""
    for child in element:
        if _name(child.tag) == field.lower() and child.text:
            return child.text.strip()
    return ""


def _number(value: str) -> float | None:
    if not value:
        return None

    try:
        result = float(value)
    except ValueError:
        return None

    if not math.isfinite(result) or result < 0:
        return None

    return result


def parse_hdd_list(content: bytes) -> tuple[HDDEntry, ...]:
    """Parse HDD entries while disambiguating IDs reused across disk types."""

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("HDD response is not valid XML.") from exc

    if _name(root.tag) != "hddlist":
        raise ValueError("Unexpected HDD-list XML root.")

    raw_disks = []

    for node in root:
        if _name(node.tag) != "hdd":
            continue

        disk_id = _child_text(node, "id")
        status = _child_text(node, "status")
        disk_type = _child_text(node, "hddType") or "NOT_RETURNED"

        if not disk_id or not status:
            raise ValueError("An HDD entry lacks its ID or status.")

        raw_disks.append({
            "raw_id": disk_id,
            "name": _child_text(node, "hddName") or "NOT_RETURNED",
            "disk_type": disk_type,
            "status": status.strip().lower(),
            "capacity_mb": _number(_child_text(node, "capacity")),
            "free_space_mb": _number(_child_text(node, "freeSpace")),
            "property": _child_text(node, "property") or "NOT_RETURNED",
            "group": _child_text(node, "group") or "NOT_RETURNED",
        })

    if not raw_disks:
        raise ValueError("No HDD entries were returned.")

    id_counts = {}
    for disk in raw_disks:
        raw_id = disk["raw_id"]
        id_counts[raw_id] = id_counts.get(raw_id, 0) + 1

    disks = []

    for raw in raw_disks:
        disk_id = raw["raw_id"]

        # Hikvision may reuse an ID for different storage types.
        # Preserve the distinction rather than silently dropping a disk.
        if id_counts[disk_id] > 1:
            disk_id = f"{disk_id} ({raw['disk_type']})"

        disks.append(
            HDDEntry(
                id=disk_id,
                name=raw["name"],
                disk_type=raw["disk_type"],
                status=raw["status"],
                capacity_mb=raw["capacity_mb"],
                free_space_mb=raw["free_space_mb"],
                property=raw["property"],
                group=raw["group"],
            )
        )

    # Repeated IDs of the same type are still treated as ambiguous.
    ids = [disk.id for disk in disks]
    if len(ids) != len(set(ids)):
        raise ValueError("HDD inventory contains duplicate IDs of the same type.")

    return tuple(disks)

def _sata_number(disk: HDDEntry) -> int | None:
    if disk.disk_type.strip().lower() != "sata":
        return None
    numeric_id = disk.id.split(" (", 1)[0]
    return int(numeric_id) if numeric_id.isdecimal() else None


def classify_storage(
    disks: tuple[HDDEntry, ...],
    *,
    expected_sata_hdd_ids: tuple[int, ...] = (),
    empty_sata_hdd_ids: tuple[int, ...] = (),
    recent_write_minutes: int | None = None,
) -> tuple[str, str]:
    """Classify read-only HDD evidence without confusing empty bays and failures.

    The optional expected SATA IDs describe drives independently confirmed as
    installed. Without that configuration, an undetected slot is a warning,
    except when *no* usable disks at all are returned. Bays listed in
    ``empty_sata_hdd_ids`` were confirmed empty on site and are only noted.

    ``recent_write_minutes`` is set by the reporter when per-camera searches
    found footage written within that many minutes. A full writable disk is
    then evidently being overwritten, so it is noted rather than a warning.
    Disk errors and missing expected disks are never relaxed.
    """
    if not disks:
        return "UNKNOWN", "No HDD entries were available."

    failures = [disk for disk in disks if disk.status in CRITICAL_DISK_STATES]
    if failures:
        details = ", ".join(f"{disk.id} ({disk.status})" for disk in failures)
        return "CRITICAL", f"NVR reports abnormal HDD states: {details}."

    # The ISAPI list may mix SATA and virtual disk IDs. Expectations refer to
    # physical SATA IDs only, not similarly numbered virtual/logical disks.
    sata_by_id: dict[int, list[HDDEntry]] = {}
    for disk in disks:
        if disk.disk_type.strip().lower() != "sata":
            continue
        numeric_id = disk.id.split(" (", 1)[0]
        if numeric_id.isdecimal():
            sata_by_id.setdefault(int(numeric_id), []).append(disk)

    absent_expected = [
        disk_id for disk_id in expected_sata_hdd_ids
        if disk_id not in sata_by_id
        or all(entry.status in ABSENT_DISK_STATES for entry in sata_by_id[disk_id])
    ]
    if absent_expected:
        return (
            "CRITICAL",
            "Expected installed SATA HDD IDs are missing or not detected: "
            + ", ".join(str(disk_id) for disk_id in absent_expected) + ".",
        )

    unrecognized = [
        disk for disk in disks
        if disk.status not in (NORMAL_DISK_STATES | ABSENT_DISK_STATES)
    ]
    if unrecognized:
        ids = ", ".join(disk.id for disk in unrecognized)
        return "UNKNOWN", f"Unrecognized or transitional HDD states require review: {ids}."

    available = [disk for disk in disks if disk.status in NORMAL_DISK_STATES]
    absent = [disk for disk in disks if disk.status in ABSENT_DISK_STATES]
    if not available:
        return (
            "CRITICAL",
            "No detected operational HDDs were returned; verify the storage inventory.",
        )

    incomplete = [
        disk for disk in available
        if disk.capacity_mb is None or disk.free_space_mb is None
    ]
    if incomplete:
        ids = ", ".join(disk.id for disk in incomplete)
        return "UNKNOWN", f"Capacity or free-space information is missing for HDD IDs: {ids}."

    inconsistent = [disk for disk in available if disk.free_space_mb > disk.capacity_mb]
    if inconsistent:
        ids = ", ".join(disk.id for disk in inconsistent)
        return "UNKNOWN", f"Reported free capacity exceeds total capacity for HDD IDs: {ids}."

    writable = [
        disk for disk in available if disk.property.strip().upper() == "RW"
    ]
    if not writable:
        return "UNKNOWN", "No detected writable HDD or logical storage volumes were reported."

    findings = []
    notes = []
    confirmed_empty = set(empty_sata_hdd_ids)
    unconfirmed = [disk for disk in absent if _sata_number(disk) not in confirmed_empty]
    known_empty = [disk for disk in absent if _sata_number(disk) in confirmed_empty]
    if unconfirmed:
        ids = ", ".join(disk.id for disk in unconfirmed)
        findings.append(
            f"HDD slots reported not detected ({ids}); confirm whether drives are expected"
        )
    if known_empty:
        ids = ", ".join(disk.id for disk in known_empty)
        notes.append((f"bays {ids} are" if len(known_empty) > 1 else f"bay {ids} is")
                     + " empty, as configured")

    no_free_space = [disk for disk in writable if disk.free_space_mb <= 0]
    if no_free_space:
        ids = ", ".join(disk.id for disk in no_free_space)
        if recent_write_minutes:
            notes.append(
                (f"HDDs {ids} are full" if len(no_free_space) > 1 else f"HDD {ids} is full")
                + f", but cameras recorded within the last {recent_write_minutes} minutes,"
                " so old footage is being overwritten"
            )
        else:
            findings.append(
                f"Zero free space on writable HDD IDs: {ids}; verify overwrite and quota policy"
            )

    if findings:
        return "WARNING", "; ".join(findings + notes) + "."

    if notes:
        return "HEALTHY", "All detected HDDs have normal/idle states; " + "; ".join(notes) + "."
    return "HEALTHY", "All detected HDDs have normal/idle states and writable free space."


def get_storage_check(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    *,
    expected_sata_hdd_ids: tuple[int, ...] = (),
    empty_sata_hdd_ids: tuple[int, ...] = (),
) -> StorageCheck:
    """Retrieve HDD state via GET; never changes storage configuration."""

    try:
        address = ip_address(ip)
    except ValueError:
        return StorageCheck(
            "UNKNOWN", None, (), "Invalid IP address."
        )

    if address.version != 4 or not 1 <= port <= 65535:
        return StorageCheck(
            "UNKNOWN", None, (), "Invalid IPv4 address or TCP port."
        )

    if not username or not password:
        return StorageCheck(
            "UNKNOWN", None, (), "A username and password are required."
        )

    try:
        with requests.Session() as session:
            session.trust_env = False

            response = session.get(
                f"http://{address}:{port}/ISAPI/ContentMgmt/Storage/hdd",
                auth=HTTPDigestAuth(username, password),
                headers={"Accept": "application/xml"},
                timeout=(3, 8),
                allow_redirects=False,
            )

            if response.status_code != 200:
                if response.status_code == 401:
                    message = "Authentication failed."
                elif response.status_code == 403:
                    message = "Permission denied."
                elif response.status_code == 404:
                    message = "HDD endpoint unavailable."
                else:
                    message = f"HTTP request returned {response.status_code}."

                return StorageCheck(
                    "UNKNOWN", response.status_code, (), message
                )

            disks = parse_hdd_list(response.content)
            status, message = classify_storage(
                disks, expected_sata_hdd_ids=expected_sata_hdd_ids,
                empty_sata_hdd_ids=empty_sata_hdd_ids,
            )

            return StorageCheck(
                status=status,
                http_status=response.status_code,
                disks=disks,
                message=message,
            )

    except RequestException:
        return StorageCheck(
            "UNKNOWN", None, (),
            "Network request failed or timed out.",
        )
    except ValueError:
        return StorageCheck(
            "UNKNOWN", 200, (),
            "HDD response could not be parsed reliably.",
        )