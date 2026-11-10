"""Unit tests for read-only Hikvision channel XML parsers."""

import unittest

from nvr_track.nvr.isapi import (
    parse_channel_inventory,
    parse_channel_statuses,
)


class ChannelInventoryParserTests(unittest.TestCase):
    def test_parse_channel_inventory(self):
        xml = b"""<?xml version="1.0"?>
        <InputProxyChannelList>
          <InputProxyChannel>
            <id>1</id>
            <name>Entrance</name>
            <sourceInputPortDescriptor>
              <ipAddress>192.0.2.20</ipAddress>
              <managePortNo>8000</managePortNo>
            </sourceInputPortDescriptor>
          </InputProxyChannel>
          <InputProxyChannel>
            <id>3</id>
            <name>Warehouse</name>
          </InputProxyChannel>
        </InputProxyChannelList>
        """

        channels = parse_channel_inventory(xml)

        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0].channel_id, "1")
        self.assertEqual(channels[0].name, "Entrance")
        self.assertEqual(channels[0].camera_ip, "192.0.2.20")
        self.assertEqual(channels[0].management_port, 8000)
        self.assertEqual(channels[1].channel_id, "3")

    def test_parse_namespaced_inventory(self):
        xml = b"""<?xml version="1.0"?>
        <InputProxyChannelList xmlns="urn:example">
          <InputProxyChannel>
            <id>7</id>
            <name>LoadingBay</name>
          </InputProxyChannel>
        </InputProxyChannelList>
        """

        channels = parse_channel_inventory(xml)

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].channel_id, "7")

    def test_reject_wrong_inventory_root(self):
        with self.assertRaisesRegex(ValueError, "root"):
            parse_channel_inventory(b"<ResponseStatus/>")

    def test_reject_duplicate_inventory_ids(self):
        xml = b"""<InputProxyChannelList>
          <InputProxyChannel><id>1</id></InputProxyChannel>
          <InputProxyChannel><id>1</id></InputProxyChannel>
        </InputProxyChannelList>"""

        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_channel_inventory(xml)


class ChannelStatusParserTests(unittest.TestCase):
    def test_parse_online_offline_and_unknown(self):
        xml = b"""<?xml version="1.0"?>
        <InputProxyChannelStatusList>
          <InputProxyChannelStatus>
            <id>1</id>
            <online>true</online>
          </InputProxyChannelStatus>
          <InputProxyChannelStatus>
            <id>2</id>
            <online>false</online>
          </InputProxyChannelStatus>
          <InputProxyChannelStatus>
            <id>3</id>
          </InputProxyChannelStatus>
        </InputProxyChannelStatusList>
        """

        statuses = parse_channel_statuses(xml)

        self.assertEqual(len(statuses), 3)
        self.assertIs(statuses[0].online, True)
        self.assertIs(statuses[1].online, False)
        self.assertIsNone(statuses[2].online)

    def test_parse_numeric_boolean_values(self):
        xml = b"""<InputProxyChannelStatusList>
          <InputProxyChannelStatus>
            <id>1</id><online>1</online>
          </InputProxyChannelStatus>
          <InputProxyChannelStatus>
            <id>2</id><online>0</online>
          </InputProxyChannelStatus>
        </InputProxyChannelStatusList>"""

        statuses = parse_channel_statuses(xml)

        self.assertIs(statuses[0].online, True)
        self.assertIs(statuses[1].online, False)

    def test_reject_missing_channel_id(self):
        xml = b"""<InputProxyChannelStatusList>
          <InputProxyChannelStatus><online>true</online></InputProxyChannelStatus>
        </InputProxyChannelStatusList>"""

        with self.assertRaisesRegex(ValueError, "no channel ID"):
            parse_channel_statuses(xml)

    def test_reject_wrong_status_root(self):
        with self.assertRaisesRegex(ValueError, "root"):
            parse_channel_statuses(b"<ResponseStatus/>")


if __name__ == "__main__":
    unittest.main()