"""Read-only Hikvision ISAPI device-information check."""

from dataclasses import dataclass
from ipaddress import ip_address
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import (
    ConnectionError,
    RequestException,
    SSLError,
    Timeout,
)


@dataclass(frozen=True)
class DeviceInfoResult:
    status: str
    http_status: int | None = None
    model: str | None = None
    firmware_version: str | None = None
    message: str = ""


def parse_device_info(content: bytes) -> dict[str, str]:
    """Parse selected fields from an ISAPI deviceInfo XML response."""

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Response was not valid XML.") from exc

    values: dict[str, str] = {}

    for node in root.iter():
        # Handle XML namespaces without assuming a particular namespace.
        field = node.tag.rsplit("}", 1)[-1].lower()

        if field in {"model", "firmwareversion"} and node.text:
            values[field] = node.text.strip()

    if not values.get("model") and not values.get("firmwareversion"):
        raise ValueError(
            "XML response did not contain recognizable device information."
        )

    return {
        "model": values.get("model", ""),
        "firmware_version": values.get("firmwareversion", ""),
    }


def get_device_info(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
) -> DeviceInfoResult:
    """Retrieve device information using HTTP Digest authentication.

    This function performs a GET request only. It does not change the NVR.
    """

    try:
        address = ip_address(ip)
    except ValueError:
        return DeviceInfoResult(
            status="INVALID_CONFIGURATION",
            message="The supplied IP address is invalid.",
        )

    if address.version != 4:
        return DeviceInfoResult(
            status="INVALID_CONFIGURATION",
            message="Only IPv4 addresses are supported.",
        )

    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return DeviceInfoResult(
            status="INVALID_CONFIGURATION",
            message="The TCP port must be between 1 and 65535.",
        )

    if not username or not password:
        return DeviceInfoResult(
            status="INVALID_CONFIGURATION",
            message="A username and password are required.",
        )

    url = f"http://{address}:{port}/ISAPI/System/deviceInfo"

    try:
        with requests.Session() as session:
            # Avoid unexpected credentials or proxies inherited from the
            # development environment. Authentication is supplied explicitly.
            session.trust_env = False

            response = session.get(
                url,
                auth=HTTPDigestAuth(username, password),
                headers={"Accept": "application/xml"},
                timeout=(3, 8),
                allow_redirects=False,
            )

            status_code = response.status_code

            if status_code == 401:
                return DeviceInfoResult(
                    status="AUTHENTICATION_FAILED",
                    http_status=status_code,
                    message="The NVR rejected authentication.",
                )

            if status_code == 403:
                return DeviceInfoResult(
                    status="PERMISSION_DENIED",
                    http_status=status_code,
                    message="The authenticated account lacks permission.",
                )

            if status_code == 404:
                return DeviceInfoResult(
                    status="ENDPOINT_UNAVAILABLE",
                    http_status=status_code,
                    message="The requested device-information endpoint was not found.",
                )

            if 300 <= status_code < 400:
                return DeviceInfoResult(
                    status="REDIRECTED",
                    http_status=status_code,
                    message="The NVR redirected the request; redirect was not followed.",
                )

            if not 200 <= status_code < 300:
                return DeviceInfoResult(
                    status="HTTP_ERROR",
                    http_status=status_code,
                    message=f"The NVR returned HTTP {status_code}.",
                )

            try:
                fields = parse_device_info(response.content)
            except ValueError:
                return DeviceInfoResult(
                    status="UNEXPECTED_RESPONSE",
                    http_status=status_code,
                    message="The response did not contain recognizable device information.",
                )

            return DeviceInfoResult(
                status="DEVICE_INFO_RETRIEVED",
                http_status=status_code,
                model=fields.get("model") or None,
                firmware_version=fields.get("firmware_version") or None,
                message="Device information was retrieved successfully.",
            )

    except SSLError:
        return DeviceInfoResult(
            status="TLS_ERROR",
            message="A TLS error occurred.",
        )
    except Timeout:
        return DeviceInfoResult(
            status="TIMEOUT",
            message="The NVR connection or response timed out.",
        )
    except ConnectionError:
        return DeviceInfoResult(
            status="CONNECTION_ERROR",
            message="A network connection error occurred.",
        )
    except RequestException:
        return DeviceInfoResult(
            status="REQUEST_ERROR",
            message="The HTTP request could not be completed.",
        )
# ---------- Read-only channel discovery ----------

@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    name: str | None = None
    camera_ip: str | None = None
    management_port: int | None = None


@dataclass(frozen=True)
class ChannelStatus:
    channel_id: str
    online: bool | None


@dataclass(frozen=True)
class ChannelInventoryResult:
    status: str
    http_status: int | None = None
    channels: tuple[ChannelInfo, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ChannelStatusResult:
    status: str
    http_status: int | None = None
    channels: tuple[ChannelStatus, ...] = ()
    message: str = ""


def _local_xml_name(tag: str) -> str:
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1].lower()


def _first_xml_text(element: ET.Element, *names: str) -> str | None:
    """Find the first non-empty descendant text matching any local tag name."""
    targets = {name.lower() for name in names}

    for node in element.iter():
        if _local_xml_name(node.tag) in targets and node.text:
            value = node.text.strip()
            if value:
                return value

    return None


def _fetch_channel_xml(
    ip: str,
    username: str,
    password: str,
    port: int,
    path: str,
    expected_root: str,
) -> tuple[str, int | None, bytes | None, str]:
    """Fetch one ISAPI XML endpoint without following redirects."""

    try:
        address = ip_address(ip)
    except ValueError:
        return (
            "INVALID_CONFIGURATION", None, None,
            "The supplied IP address is invalid.",
        )

    if address.version != 4:
        return (
            "INVALID_CONFIGURATION", None, None,
            "Only IPv4 addresses are supported.",
        )

    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return (
            "INVALID_CONFIGURATION", None, None,
            "The TCP port must be between 1 and 65535.",
        )

    if not username or not password:
        return (
            "INVALID_CONFIGURATION", None, None,
            "A username and password are required.",
        )

    url = f"http://{address}:{port}{path}"

    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                url,
                auth=HTTPDigestAuth(username, password),
                headers={"Accept": "application/xml"},
                timeout=(3, 8),
                allow_redirects=False,
            )

        code = response.status_code

        if code == 401:
            return (
                "AUTHENTICATION_FAILED", code, None,
                "The NVR rejected authentication.",
            )

        if code == 403:
            return (
                "PERMISSION_DENIED", code, None,
                "The account lacks permission for this endpoint.",
            )

        if code == 404:
            return (
                "ENDPOINT_UNAVAILABLE", code, None,
                "The requested endpoint was not found.",
            )

        if 300 <= code < 400:
            return (
                "REDIRECTED", code, None,
                "The endpoint redirected the request; redirect was not followed.",
            )

        if not 200 <= code < 300:
            return (
                "HTTP_ERROR", code, None,
                f"The NVR returned HTTP {code}.",
            )

        body = response.content

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return (
                "UNEXPECTED_RESPONSE", code, None,
                "The response body was not valid XML.",
            )

        root_name = _local_xml_name(root.tag)

        if root_name == "responsestatus":
            sub_status = _first_xml_text(root, "subStatusCode") or ""
            status_string = _first_xml_text(root, "statusString") or ""
            normalized = (sub_status + status_string).lower().replace(
                " ", ""
            ).replace("_", "")

            if "notsupport" in normalized or "unsupported" in normalized:
                return (
                    "UNSUPPORTED_ENDPOINT", code, None,
                    "The NVR reports that this operation is unsupported.",
                )

            return (
                "DEVICE_REPORTED_ERROR", code, None,
                "The NVR returned an ISAPI ResponseStatus error.",
            )

        if root_name != expected_root.lower():
            return (
                "UNEXPECTED_RESPONSE", code, None,
                "The response XML root did not match the expected format.",
            )

        return "XML_OK", code, body, "The endpoint returned its expected XML root."

    except SSLError:
        return "TLS_ERROR", None, None, "A TLS error occurred."
    except Timeout:
        return "TIMEOUT", None, None, "The request timed out."
    except ConnectionError:
        return "CONNECTION_ERROR", None, None, "A network connection error occurred."
    except RequestException:
        return "REQUEST_ERROR", None, None, "The HTTP request could not be completed."


def parse_channel_inventory(content: bytes) -> tuple[ChannelInfo, ...]:
    """Parse configured digital channels from InputProxyChannelList XML."""

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Channel inventory response was not valid XML.") from exc

    if _local_xml_name(root.tag) != "inputproxychannellist":
        raise ValueError("Unexpected channel inventory XML root.")

    channels: list[ChannelInfo] = []

    for node in root.iter():
        if _local_xml_name(node.tag) != "inputproxychannel":
            continue

        channel_id = _first_xml_text(node, "id")
        if not channel_id:
            raise ValueError("A channel entry has no channel ID.")

        name = _first_xml_text(node, "name", "channelName")
        camera_ip = _first_xml_text(node, "ipAddress")
        raw_port = _first_xml_text(node, "managePortNo")

        management_port = None
        if raw_port:
            try:
                parsed_port = int(raw_port)
                if 1 <= parsed_port <= 65535:
                    management_port = parsed_port
            except ValueError:
                pass

        channels.append(
            ChannelInfo(
                channel_id=channel_id,
                name=name,
                camera_ip=camera_ip,
                management_port=management_port,
            )
        )

    ids = [channel.channel_id for channel in channels]
    if len(ids) != len(set(ids)):
        raise ValueError("Channel inventory contains duplicate channel IDs.")

    return tuple(channels)


def parse_channel_statuses(content: bytes) -> tuple[ChannelStatus, ...]:
    """Parse reported online/offline status from channel status XML."""

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Channel status response was not valid XML.") from exc

    if _local_xml_name(root.tag) != "inputproxychannelstatuslist":
        raise ValueError("Unexpected channel status XML root.")

    channels: list[ChannelStatus] = []

    for node in root.iter():
        if _local_xml_name(node.tag) != "inputproxychannelstatus":
            continue

        channel_id = _first_xml_text(node, "id")
        if not channel_id:
            raise ValueError("A channel status entry has no channel ID.")

        raw_online = _first_xml_text(node, "online")
        online = None

        if raw_online is not None:
            normalized = raw_online.strip().lower()
            if normalized in {"true", "1", "yes"}:
                online = True
            elif normalized in {"false", "0", "no"}:
                online = False

        channels.append(
            ChannelStatus(
                channel_id=channel_id,
                online=online,
            )
        )

    ids = [channel.channel_id for channel in channels]
    if len(ids) != len(set(ids)):
        raise ValueError("Channel status contains duplicate channel IDs.")

    return tuple(channels)


def get_channel_inventory(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
) -> ChannelInventoryResult:
    """Retrieve the NVR's configured digital/IP channel inventory."""

    status, http_code, body, message = _fetch_channel_xml(
        ip=ip,
        username=username,
        password=password,
        port=port,
        path="/ISAPI/ContentMgmt/InputProxy/channels",
        expected_root="InputProxyChannelList",
    )

    if status != "XML_OK" or body is None:
        return ChannelInventoryResult(status, http_code, (), message)

    try:
        channels = parse_channel_inventory(body)
    except ValueError:
        return ChannelInventoryResult(
            "UNEXPECTED_RESPONSE",
            http_code,
            (),
            "The channel inventory XML could not be parsed reliably.",
        )

    if not channels:
        return ChannelInventoryResult(
            "EMPTY_RESULT",
            http_code,
            (),
            "The endpoint returned no configured digital channels.",
        )

    return ChannelInventoryResult(
        "CHANNELS_RETRIEVED",
        http_code,
        channels,
        f"Retrieved {len(channels)} configured digital channels.",
    )


def get_channel_statuses(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
) -> ChannelStatusResult:
    """Retrieve reported online/offline status for digital/IP channels."""

    status, http_code, body, message = _fetch_channel_xml(
        ip=ip,
        username=username,
        password=password,
        port=port,
        path="/ISAPI/ContentMgmt/InputProxy/channels/status",
        expected_root="InputProxyChannelStatusList",
    )

    if status != "XML_OK" or body is None:
        return ChannelStatusResult(status, http_code, (), message)

    try:
        channels = parse_channel_statuses(body)
    except ValueError:
        return ChannelStatusResult(
            "UNEXPECTED_RESPONSE",
            http_code,
            (),
            "The channel status XML could not be parsed reliably.",
        )

    if not channels:
        return ChannelStatusResult(
            "EMPTY_RESULT",
            http_code,
            (),
            "The endpoint returned no channel status entries.",
        )

    if any(channel.online is None for channel in channels):
        return ChannelStatusResult(
            "CHANNEL_STATUS_INCOMPLETE",
            http_code,
            channels,
            "Some channel entries did not contain a recognizable online status.",
        )

    return ChannelStatusResult(
        "CHANNEL_STATUS_RETRIEVED",
        http_code,
        channels,
        f"Retrieved status for {len(channels)} channels.",
    )