"""One-NVR, read-only HDD storage-health diagnostic."""

from getpass import getpass

from app.storage import get_storage_check


def main() -> int:
    ip = input("NVR IPv4 [10.233.220.51]: ").strip() or "10.233.220.51"
    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")

    result = get_storage_check(
        ip=ip,
        username=username,
        password=password,
        port=80,
    )

    print("\n========== STORAGE HEALTH ==========")
    print(f"NVR: {ip}")
    print(f"Status: {result.status}")

    if result.http_status is not None:
        print(f"HTTP status: {result.http_status}")

    print(f"Assessment: {result.message}")

    for disk in result.disks:
        capacity_gib = (
            disk.capacity_mb / 1024
            if disk.capacity_mb is not None
            else None
        )
        free_gib = (
            disk.free_space_mb / 1024
            if disk.free_space_mb is not None
            else None
        )

        print(f"\nHDD ID: {disk.id}")
        print(f"  Name: {disk.name}")
        print(f"  Status reported by NVR: {disk.status}")
        print(
            "  Capacity GiB: "
            f"{capacity_gib:.2f}" if capacity_gib is not None
            else "  Capacity GiB: NOT_RETURNED"
        )
        print(
            "  Free space GiB: "
            f"{free_gib:.2f}" if free_gib is not None
            else "  Free space GiB: NOT_RETURNED"
        )

    print("\nStorage status does not prove recording health.")
    print("No storage configuration was changed.")

    return {
        "HEALTHY": 0,
        "WARNING": 1,
        "CRITICAL": 2,
        "UNKNOWN": 3,
    }.get(result.status, 3)


if __name__ == "__main__":
    raise SystemExit(main())