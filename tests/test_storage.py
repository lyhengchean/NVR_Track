"""Unit tests for HDD parsing and conservative status classification."""

import unittest

from app.storage import classify_storage, parse_hdd_list


def hdd_list(*entries: tuple[str, str, str]) -> bytes:
    disks = []

    for disk_id, status, free in entries:
        disks.append(
            f"""
            <hdd>
              <id>{disk_id}</id>
              <hddName>hdd{disk_id}</hddName>
              <hddType>SATA</hddType>
              <status>{status}</status>
              <capacity>3815447</capacity>
              <freeSpace>{free}</freeSpace>
              <property>RW</property>
              <group>1</group>
            </hdd>
            """
        )

    return (
        "<hddList xmlns='urn:example'>"
        + "".join(disks)
        + "</hddList>"
    ).encode()


class StorageTests(unittest.TestCase):
    def test_error_disks_produce_critical_status(self):
        disks = parse_hdd_list(
            hdd_list(
                ("1", "error", "0"),
                ("2", "ok", "0"),
                ("3", "error", "0"),
                ("6", "error", "0"),
            )
        )

        status, _ = classify_storage(disks)

        self.assertEqual(status, "CRITICAL")
        self.assertEqual(len(disks), 4)

    def test_zero_free_space_produces_warning_without_disk_errors(self):
        disks = parse_hdd_list(
            hdd_list(("2", "ok", "0"), ("4", "ok", "0"))
        )

        status, _ = classify_storage(disks)

        self.assertEqual(status, "WARNING")

    def test_normal_disks_with_free_space_are_healthy(self):
        disks = parse_hdd_list(
            hdd_list(("2", "ok", "1000"), ("4", "normal", "500"))
        )

        status, _ = classify_storage(disks)

        self.assertEqual(status, "HEALTHY")

    def test_unknown_disk_state_is_not_healthy(self):
        disks = parse_hdd_list(
            hdd_list(("2", "unexpected-state", "1000"))
        )

        status, _ = classify_storage(disks)

        self.assertEqual(status, "UNKNOWN")

    def test_duplicate_hdd_ids_are_rejected(self):
        content = hdd_list(("2", "ok", "100"), ("2", "ok", "100"))

        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_hdd_list(content)

    def test_invalid_xml_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "valid XML"):
            parse_hdd_list(b"<hddList>")

    def test_wrong_xml_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "root"):
            parse_hdd_list(b"<ResponseStatus/>")
    def test_duplicate_id_across_virtual_and_sata_disks(self):
        xml = b"""
        <hddList version="2.0">
        <hdd>
            <id>1</id>
            <hddName>hdd1</hddName>
            <hddType>Virtual Disk</hddType>
            <status>ok</status>
            <capacity>40054997</capacity>
            <freeSpace>0</freeSpace>
            <property>RW</property>
            <group>1</group>
        </hdd>
        <hdd>
            <id>1</id>
            <hddName></hddName>
            <hddType>SATA</hddType>
            <status>ok</status>
            <capacity>5723166</capacity>
            <freeSpace>0</freeSpace>
            <property>RO</property>
            <group>0</group>
        </hdd>
        </hddList>
        """

        disks = parse_hdd_list(xml)

        self.assertEqual(
            [disk.id for disk in disks],
            ["1 (Virtual Disk)", "1 (SATA)"],
        )

        status, message = classify_storage(disks)

        self.assertEqual(status, "WARNING")
        self.assertIn("1 (Virtual Disk)", message)
        self.assertNotIn("1 (SATA)", message)


if __name__ == "__main__":
    unittest.main()