"""Unit tests for HDD parsing and conservative status classification."""

import unittest
from unittest.mock import patch

from app.storage import HDDEntry, classify_storage, get_storage_check, parse_hdd_list


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

    def test_impossible_free_space_is_unverified(self):
        disks = parse_hdd_list(hdd_list(("1", "ok", "999999999")))
        status, message = classify_storage(disks)
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("exceeds", message)

    def test_notexist_with_usable_disk_is_warning_not_failure(self):
        disks = parse_hdd_list(hdd_list(
            ("1", "ok", "100"), ("3", "notexist", "0"),
            ("5", "notexist", "0"),
        ))
        status, message = classify_storage(disks)
        self.assertEqual(status, "WARNING")
        self.assertIn("not detected", message)
        self.assertIn("3", message)
        self.assertIn("5", message)

    def test_notexist_with_no_operational_disks_is_critical(self):
        disks = parse_hdd_list(hdd_list(
            ("3", "notexist", "0"), ("5", "notexist", "0"),
        ))
        status, message = classify_storage(disks)
        self.assertEqual(status, "CRITICAL")
        self.assertIn("No detected operational", message)

    def test_missing_expected_sata_disk_is_critical(self):
        disks = parse_hdd_list(hdd_list(
            ("1", "ok", "0"), ("3", "notexist", "0"),
        ))
        status, message = classify_storage(
            disks, expected_sata_hdd_ids=(1, 3, 6),
        )
        self.assertEqual(status, "CRITICAL")
        self.assertIn("3", message)
        self.assertIn("6", message)
        self.assertNotIn(" 1,", message)

    def test_expected_installed_disk_does_not_change_unrelated_empty_bay(self):
        disks = parse_hdd_list(hdd_list(
            ("1", "ok", "100"), ("3", "notexist", "0"),
        ))
        status, message = classify_storage(
            disks, expected_sata_hdd_ids=(1,),
        )
        self.assertEqual(status, "WARNING")
        self.assertIn("3", message)

    def test_idle_is_not_a_failure(self):
        disks = parse_hdd_list(hdd_list(
            ("1", "idle", "100"), ("3", "ok", "100"),
        ))
        status, _ = classify_storage(disks)
        self.assertEqual(status, "HEALTHY")

    def test_critical_disk_error_takes_precedence_over_empty_bays(self):
        disks = parse_hdd_list(hdd_list(
            ("1", "error", "0"), ("3", "notexist", "0"),
            ("4", "ok", "100"),
        ))
        status, message = classify_storage(disks)
        self.assertEqual(status, "CRITICAL")
        self.assertIn("1 (error)", message)

    def test_missing_capacity_for_notexist_slot_is_not_unknown(self):
        xml = hdd_list(("1", "ok", "100"), ("3", "notexist", "0"))
        xml = xml.replace(b"<capacity>3815447</capacity><freeSpace>0</freeSpace>",
                          b"<capacity></capacity><freeSpace></freeSpace>")
        status, _ = classify_storage(parse_hdd_list(xml))
        self.assertEqual(status, "WARNING")

    def test_expected_sata_not_satisfied_by_virtual_disk(self):
        xml = b"""<hddList>
        <hdd><id>1</id><hddType>Virtual Disk</hddType><status>ok</status>
          <capacity>5000</capacity><freeSpace>1000</freeSpace>
          <property>RW</property></hdd>
        <hdd><id>2</id><hddType>SATA</hddType><status>ok</status>
          <capacity>5000</capacity><freeSpace>1000</freeSpace>
          <property>RW</property></hdd>
        </hddList>"""
        status, message = classify_storage(
            parse_hdd_list(xml), expected_sata_hdd_ids=(1,),
        )
        self.assertEqual(status, "CRITICAL")
        self.assertIn("1", message)

    def test_api_passes_expected_disk_ids_to_classifier(self):
        response_xml = hdd_list(("1", "ok", "100"), ("3", "notexist", "0"))
        with patch("app.storage.requests.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value.status_code = 200
            session.get.return_value.content = response_xml
            result = get_storage_check(
                "192.0.2.10", "dummy_user", "dummy_password",
                expected_sata_hdd_ids=(1, 3),
            )
            self.assertEqual(result.status, "CRITICAL")
            self.assertIn("3", result.message)
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(session.get.call_args.kwargs["allow_redirects"], False)

    def test_actual_fleet_storage_patterns(self):
        # Redacted fixtures from the operator's five critical NVR reports.
        # The reporter must never infer that an undetected slot was installed.
        cases = {
            "nvr-01": ([("1", "error"), ("2", "ok"), ("3", "error"),
                        ("4", "ok"), ("6", "error"), ("7", "ok"), ("8", "ok")], "CRITICAL"),
            "nvr-03": ([("1", "ok"), ("2", "ok"), ("3", "notexist"),
                        ("5", "notexist"), ("6", "ok"), ("7", "notexist")], "WARNING"),
            "nvr-04": ([("1", "ok"), ("2", "notexist"), ("3", "error"),
                        ("5", "ok"), ("6", "idle"), ("7", "idle"), ("8", "ok")], "CRITICAL"),
            "nvr-05": ([("1", "notexist"), ("3", "notexist"), ("4", "ok")], "WARNING"),
            "nvr-07": ([("1", "idle"), ("2", "notexist"), ("3", "ok"),
                        ("4", "ok"), ("5", "idle"), ("9", "notexist")], "WARNING"),
        }
        for nvr_id, (states, expected) in cases.items():
            with self.subTest(nvr=nvr_id):
                disks = tuple(
                    HDDEntry(
                        id=disk_id, name="redacted", disk_type="SATA",
                        status=status, capacity_mb=0 if status == "notexist" else 3815447,
                        free_space_mb=0, property="RW", group="1",
                    )
                    for disk_id, status in states
                )
                self.assertEqual(classify_storage(disks)[0], expected)

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