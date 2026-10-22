"""Tests for parsing ISAPI device-information XML."""

import unittest

from app.isapi import parse_device_info


class DeviceInfoParserTests(unittest.TestCase):
    def test_parse_device_information(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <DeviceInfo>
            <deviceName>Test NVR</deviceName>
            <model>DS-Test-NVR</model>
            <firmwareVersion>V5.0.0</firmwareVersion>
        </DeviceInfo>
        """

        result = parse_device_info(xml)

        self.assertEqual(result["model"], "DS-Test-NVR")
        self.assertEqual(result["firmware_version"], "V5.0.0")

    def test_parse_namespaced_xml(self):
        xml = b"""<?xml version="1.0"?>
        <DeviceInfo xmlns="urn:example">
            <model>DS-Test-NVR</model>
        </DeviceInfo>
        """

        result = parse_device_info(xml)

        self.assertEqual(result["model"], "DS-Test-NVR")
        self.assertEqual(result["firmware_version"], "")

    def test_reject_invalid_xml(self):
        with self.assertRaisesRegex(ValueError, "valid XML"):
            parse_device_info(b"<DeviceInfo>")

    def test_reject_unrecognized_xml(self):
        xml = b"<Response><status>ok</status></Response>"

        with self.assertRaisesRegex(ValueError, "recognizable"):
            parse_device_info(xml)


if __name__ == "__main__":
    unittest.main()
