"""Read-only Hikvision NVR storage-health checks."""

from dataclasses import dataclass
from ipaddress import ip_address
import math
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException


CRITICAL_DISK_STATES = {
    "error",
    "offline",
    "smartfailed",
    "unformatted",
    "mismatch",
    "notexist",
}

NORMAL_DISK_STATES = {"ok", "normal"}


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

def classify_storage(disks: tuple[HDDEntry, ...]) -> tuple[str, str]:
    """Classify storage conservatively: an unknown state is never healthy."""

    if not disks:
        return "UNKNOWN", "No HDD entries were available."

    errors = [
        disk for disk in disks
        if disk.status in CRITICAL_DISK_STATES
    ]

    if errors:
        ids = ", ".join(disk.id for disk in errors)
        return (
            "CRITICAL",
            f"NVR reports disk errors or unavailable states for HDD IDs: {ids}.",
        )

    uncertain = [
        disk for disk in disks
        if disk.status not in NORMAL_DISK_STATES
    ]

    if uncertain:
        ids = ", ".join(disk.id for disk in uncertain)
        return (
            "UNKNOWN",
            f"Unrecognized or non-normal HDD states require review: {ids}.",
        )

    incomplete = [
        disk for disk in disks
        if disk.capacity_mb is None or disk.free_space_mb is None
    ]

    if incomplete:
        ids = ", ".join(disk.id for disk in incomplete)
        return (
            "UNKNOWN",
            f"Capacity or free-space information is missing for HDD IDs: {ids}.",
        )

    inconsistent = [disk for disk in disks if disk.free_space_mb > disk.capacity_mb]
    if inconsistent:
        ids = ", ".join(disk.id for disk in inconsistent)
        return "UNKNOWN", f"Reported free capacity exceeds total capacity for HDD IDs: {ids}."

    writable_disks = [
        disk for disk in disks
        if disk.property.strip().upper() == "RW"
    ]

    if not writable_disks:
        return (
            "UNKNOWN",
            "No writable HDD or logical storage volumes were reported.",
        )
    
    no_free_space = [
        disk for disk in disks
        if disk.property.strip().upper() == "RW"
        and disk.free_space_mb is not None
        and disk.free_space_mb <= 0
    ]

    if no_free_space:
        ids = ", ".join(disk.id for disk in no_free_space)
        return (
            "WARNING",
            f"NVR reports zero free space for HDD IDs: {ids}; verify quota and overwrite settings.",
        )

    return "HEALTHY", "All reported HDDs appear normal and have free space."


def get_storage_check(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
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
            status, message = classify_storage(disks)

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