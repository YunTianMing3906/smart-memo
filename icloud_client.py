"""Small stdlib-only iCloud CalDAV client and Windows DPAPI credential store.

This module never prints, logs, probes an account at import time, or stores a
password in the application's ordinary memo data. All remote operations are
explicit method calls. The caller supplies an already-built iCalendar document.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


from app_paths import app_dir


DEFAULT_ENDPOINT = "https://caldav.icloud.com/"
DEFAULT_CREDENTIAL_PATH = app_dir() / "icloud-credentials.dpapi.json"
TRUSTED_DOMAIN_ROOTS = ("icloud.com", "icloud.com.cn")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 64 * 1024
DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
NS = {"d": DAV, "c": CALDAV}


class ICloudError(Exception):
    """User-facing, deliberately sanitized error (no URL/body/password)."""

    def __init__(self, message: str, *, code: str = "icloud_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class ICloudAuthError(ICloudError):
    def __init__(self):
        super().__init__(
            "iCloud 身份验证失败。请核对 Apple 账户和专用 App 密码；不要使用账户主密码。"
            "如果你刚更改过账户密码，请重新生成专用 App 密码。",
            code="authentication_failed", status=401,
        )


class ICloudConflictError(ICloudError):
    def __init__(self, message: str | None = None):
        super().__init__(
            message or "日历事件已在其他设备修改，或同名事件已存在。请刷新后再保存，以免覆盖其他修改。",
            code="calendar_conflict", status=412,
        )


def validate_icloud_url(url: str) -> str:
    """Accept only credential-free HTTPS/443 URLs on Apple's iCloud domains.

    Apple documents both *.icloud.com and *.icloud.com.cn as its iCloud service
    domains. Domain suffixes are checked on a label boundary, so addresses such
    as icloud.com.example.org never receive an Authorization header.
    """
    if not isinstance(url, str) or not url.strip() or any(ord(char) < 32 for char in url):
        raise ICloudError("iCloud 服务地址无效。", code="unsafe_url")
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (ValueError, UnicodeError):
        raise ICloudError("iCloud 服务地址无效。", code="unsafe_url") from None
    trusted = any(host == root or host.endswith("." + root) for root in TRUSTED_DOMAIN_ROOTS)
    if (
        parsed.scheme.lower() != "https" or not trusted or port not in (None, 443)
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or "\\" in parsed.path
        or any(ord(char) < 32 for char in urllib.parse.unquote(parsed.path))
    ):
        raise ICloudError(
            "连接已停止：仅允许 HTTPS 的 Apple iCloud 服务地址，不能向其他地址发送账户凭证。",
            code="unsafe_url",
        )
    path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit(("https", host, path, "", ""))


def _resolve_url(base: str, href: str) -> str:
    return validate_icloud_url(urllib.parse.urljoin(base, href.strip()))


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data) or 1)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise ICloudError("凭证保存功能需要 Windows 的当前用户加密服务。", code="dpapi_unavailable")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(b"Memo.iCloud.CalDAV.credentials.v1")
    output = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN; default DPAPI scope is current user.
    if decrypt:
        operation = crypt32.CryptUnprotectData
        operation.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        operation.restype = wintypes.BOOL
        ok = operation(ctypes.byref(input_blob), None, ctypes.byref(entropy_blob), None, None, flags, ctypes.byref(output))
    else:
        operation = crypt32.CryptProtectData
        operation.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        operation.restype = wintypes.BOOL
        ok = operation(ctypes.byref(input_blob), "Memo iCloud CalDAV", ctypes.byref(entropy_blob), None, None, flags, ctypes.byref(output))
    # Keep both input buffers alive until Crypt[Un]protectData returns.
    _ = input_buffer, entropy_buffer
    if not ok:
        raise ICloudError(
            "无法读取或保存加密凭证。请在原来的 Windows 用户账户中重新保存专用 App 密码。",
            code="dpapi_failed",
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if output.pbData:
            if decrypt:
                ctypes.memset(output.pbData, 0, output.cbData)
            kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


class CredentialStore:
    """DPAPI-encrypted password in a separate local file; username is readable."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_CREDENTIAL_PATH

    def save(self, username: str, password: str, endpoint: str = DEFAULT_ENDPOINT,
             calendar_url: str | None = None, calendar_name: str | None = None) -> dict[str, str]:
        username = str(username).strip()
        if not username or ":" in username or any(ord(char) < 32 for char in username):
            raise ICloudError("请输入有效的 Apple 账户。", code="invalid_username")
        if not isinstance(password, str) or not password or any(ord(char) < 32 for char in password):
            raise ICloudError("请输入专用 App 密码。", code="invalid_password")
        endpoint = validate_icloud_url(endpoint)
        ciphertext = _dpapi(password.encode("utf-8"), decrypt=False)
        payload = {
            "version": 1, "protection": "windows-dpapi-current-user",
            "username": username, "endpoint": endpoint,
            "password_dpapi": base64.b64encode(ciphertext).decode("ascii"),
        }
        if calendar_url:
            payload["calendar_url"] = validate_icloud_url(calendar_url)
            payload["calendar_name"] = str(calendar_name or "iCloud 日历")
        temp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=self.path.parent, prefix=".icloud-credential-", delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except OSError:
            raise ICloudError("无法保存本机加密凭证，请检查该目录是否可写。", code="credential_write_failed") from None
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
        return {"username": username, "endpoint": endpoint}

    def load(self) -> dict[str, str] | None:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_CREDENTIAL_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise ICloudError("无法读取本机加密凭证。", code="credential_read_failed") from None
        try:
            if len(raw) > MAX_CREDENTIAL_BYTES:
                raise ValueError
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("version") != 1 or payload.get("protection") != "windows-dpapi-current-user":
                raise ValueError
            username = str(payload["username"]).strip()
            if not username or ":" in username or any(ord(char) < 32 for char in username):
                raise ValueError
            endpoint = validate_icloud_url(payload.get("endpoint", DEFAULT_ENDPOINT))
            ciphertext = base64.b64decode(payload["password_dpapi"], validate=True)
            password = _dpapi(ciphertext, decrypt=True).decode("utf-8")
            if not password or any(ord(char) < 32 for char in password):
                raise ValueError
        except ICloudError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ICloudError("本机凭证文件无效，请重新保存专用 App 密码。", code="credential_invalid") from None
        result = {"username": username, "endpoint": endpoint, "password": password}
        if payload.get("calendar_url"):
            result["calendar_url"] = validate_icloud_url(payload["calendar_url"])
            result["calendar_name"] = str(payload.get("calendar_name") or "iCloud 日历")
        return result

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise ICloudError("无法移除本机加密凭证。", code="credential_delete_failed") from None


def save_credentials(username: str, password: str, endpoint: str = DEFAULT_ENDPOINT, path: str | Path | None = None) -> dict[str, str]:
    return CredentialStore(path).save(username, password, endpoint)


def load_credentials(path: str | Path | None = None) -> dict[str, str] | None:
    return CredentialStore(path).load()


def delete_credentials(path: str | Path | None = None) -> None:
    CredentialStore(path).delete()


class _NoAutomaticRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Redirects are followed explicitly after HTTPS + trusted-domain checks.
        # Never let urllib rewrite PROPFIND/PUT into GET or forward BasicAuth.
        return None


@dataclass(frozen=True)
class _Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


def _validate_etag(etag: str) -> str:
    if not isinstance(etag, str) or not re.fullmatch(r'"[^"\r\n\x00-\x1f]*"', etag):
        raise ICloudError("日历返回的版本标记无效，请刷新事件后再操作。", code="invalid_etag")
    return etag


def _calendar_uids(ics: str) -> set[str]:
    """Only inspect event UIDs for identity confirmation; not a full ICS parser."""
    unfolded = re.sub(r"\r?\n[ \t]", "", ics)
    stack: list[str] = []
    uids: set[str] = set()
    for line in unfolded.replace("\r\n", "\n").split("\n"):
        name, separator, value = line.partition(":")
        field = name.split(";", 1)[0].upper()
        if not separator:
            continue
        if field == "BEGIN":
            stack.append(value.upper())
        elif field == "END":
            if stack and stack[-1] == value.upper():
                stack.pop()
        elif field == "UID" and stack and stack[-1] == "VEVENT":
            uids.add(re.sub(r"\\([nN,;\\])", lambda match: "\n" if match.group(1).lower() == "n" else match.group(1), value))
    return uids


def _unescape_ical(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1].lower() == "n" else m[1], value)


def _ical_signature(ics: str):
    """Compare reminder semantics while tolerating folding/order and UTC/TZID."""
    unfolded = re.sub(r"\r?\n[ \t]", "", ics)
    stack, fields, alarms, alarm = [], {}, [], None
    for line in unfolded.replace("\r\n", "\n").split("\n"):
        name, separator, value = line.partition(":")
        if not separator:
            continue
        tokens = name.split(";")
        field = tokens[0].upper()
        params = {key.upper(): val for key, val in (t.split("=", 1) for t in tokens[1:] if "=" in t)}
        if field == "BEGIN":
            stack.append(value.upper())
            if value.upper() == "VALARM":
                alarm = {}
            continue
        if field == "END":
            if value.upper() == "VALARM" and alarm is not None:
                alarms.append(alarm)
                alarm = None
            if stack and stack[-1] == value.upper():
                stack.pop()
            continue
        if not stack:
            continue
        if stack[-1] == "VALARM" and alarm is not None:
            if field in ("ACTION", "TRIGGER", "DESCRIPTION"):
                normalized_value = _unescape_ical(value)
                if field == "ACTION":
                    normalized_value = normalized_value.upper()
                elif field == "TRIGGER":
                    duration = re.fullmatch(r"([+-]?)P(?:(\d+)W|(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?)", normalized_value.upper())
                    if duration and any(part is not None for part in duration.groups()[1:]):
                        units = [int(part or 0) for part in duration.groups()[1:]]
                        seconds = sum(value * factor for value, factor in zip(units, (604800, 86400, 3600, 60, 1)))
                        normalized_value = ("-" if duration[1] == "-" and seconds else "") + f"PT{seconds}S"
                alarm[field] = normalized_value
                if field == "TRIGGER":
                    alarm["TRIGGER_RELATED"] = params.get("RELATED", "START").upper()
                    alarm["TRIGGER_VALUE"] = params.get("VALUE", "DURATION").upper()
            continue
        if stack[-1] != "VEVENT":
            continue
        if field in ("DTSTART", "DTEND", "EXDATE", "RDATE"):
            dates = []
            for item in value.split(","):
                try:
                    utc = item.endswith("Z")
                    parsed = datetime.strptime(item, "%Y%m%dT%H%M%SZ" if utc else "%Y%m%dT%H%M%S")
                    zone = params.get("TZID", "").strip('"')
                    if utc or zone in ("UTC", "Etc/UTC", "GMT"):
                        offset = timezone.utc
                    elif zone in ("Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "Asia/Hong_Kong", "China Standard Time"):
                        offset = timezone(timedelta(hours=8))
                    else:
                        raise ValueError("Unconfirmed timezone")
                    dates.append(parsed.replace(tzinfo=offset).astimezone(timezone.utc).isoformat())
                except ValueError:
                    dates.append((name.upper(), item))
            fields.setdefault(field, []).extend(dates)
        elif field == "RRULE":
            parts = dict(part.split("=", 1) for part in value.upper().split(";") if "=" in part)
            if parts.get("WKST") == "MO":
                parts.pop("WKST")
            fields.setdefault(field, []).append(tuple(sorted(parts.items())))
        elif field in ("UID", "SUMMARY", "DESCRIPTION", "SEQUENCE", "STATUS"):
            fields.setdefault(field, []).append(_unescape_ical(value))
    if "SEQUENCE" not in fields:
        fields["SEQUENCE"] = ["0"]
    if "STATUS" not in fields:
        fields["STATUS"] = ["CONFIRMED"]
    normalized = {name: tuple(sorted(values, key=repr)) for name, values in fields.items()}
    return normalized, alarms


def events_equivalent(expected: str, actual: str) -> bool:
    wanted, wanted_alarms = _ical_signature(expected)
    received, received_alarms = _ical_signature(actual)
    # Extra server bookkeeping is ignored; all event fields and every alarm
    # produced by this app must survive the round-trip.
    return (wanted == received and bool(wanted.get("UID")) and bool(wanted_alarms)
            and all(alarm in received_alarms for alarm in wanted_alarms))


class ICloudClient:
    """CalDAV adapter. All network work happens only in explicit API calls.

    config keys: username (or apple_id), endpoint (optional), timeout (optional).
    No password is copied into config, URLs, exception messages or receipts.
    """

    def __init__(self, config: Mapping[str, Any], password: str):
        self.username = str(config.get("username") or config.get("apple_id") or "").strip()
        if not self.username or ":" in self.username or any(ord(char) < 32 for char in self.username):
            raise ICloudError("请输入有效的 Apple 账户。", code="invalid_username")
        if not isinstance(password, str) or not password or any(ord(char) < 32 for char in password):
            raise ICloudError("请输入专用 App 密码。", code="invalid_password")
        self.endpoint = validate_icloud_url(str(config.get("endpoint") or DEFAULT_ENDPOINT))
        self._password = password
        try:
            self.timeout = max(3.0, min(float(config.get("timeout", 25)), 90.0))
        except (ValueError, TypeError):
            self.timeout = 25.0
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            _NoAutomaticRedirect(),
        )

    def _request(self, method: str, url: str, data: bytes | None = None, headers: Mapping[str, str] | None = None) -> _Response:
        current = validate_icloud_url(url)
        authorization = "Basic " + base64.b64encode((self.username + ":" + self._password).encode("utf-8")).decode("ascii")
        request_headers = {"Authorization": authorization, "User-Agent": "Memo-CalDAV/1.0", "Accept": "application/xml, text/calendar"}
        if headers:
            request_headers.update(headers)
        for _ in range(6):
            request = urllib.request.Request(current, data=data, headers=request_headers, method=method)
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                raise ICloudError("无法连接 iCloud。请检查网络、系统时间和服务地址后重试。", code="connection_failed") from None
            try:
                status = int(response.code)
                response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                if status in (301, 302, 303, 307, 308):
                    location = response_headers.get("location", "")
                    if not location:
                        raise ICloudError("iCloud 返回了无效的服务跳转。", code="invalid_redirect")
                    current = _resolve_url(current, location)
                    continue
                try:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                except (OSError, TimeoutError):
                    raise ICloudError("iCloud 响应读取失败，请稍后刷新确认。", code="response_failed") from None
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ICloudError("iCloud 返回的数据过大，操作已停止。", code="response_too_large")
                return _Response(status, response_headers, body, current)
            finally:
                response.close()
        raise ICloudError("iCloud 服务跳转次数过多，请检查服务地址。", code="redirect_limit")

    @staticmethod
    def _raise_status(response: _Response, allowed: tuple[int, ...]) -> None:
        if response.status in allowed:
            return
        if response.status == 401:
            raise ICloudAuthError()
        if response.status == 403:
            raise ICloudError("这个 iCloud 日历没有写入权限，或账户未授权访问。请选择自己的可编辑日历。", code="permission_denied", status=403)
        if response.status in (409, 412):
            raise ICloudConflictError()
        if response.status in (429, 503):
            raise ICloudError("iCloud 暂时繁忙或限制了请求，请稍后重试。", code="temporarily_unavailable", status=response.status)
        if response.status == 404:
            raise ICloudError("日历或事件已不存在，请重新发现日历后再操作。", code="not_found", status=404)
        raise ICloudError("iCloud 未接受这次日历操作，请刷新后再尝试。", code="unexpected_response", status=response.status)

    def _propfind(self, url: str, properties: str, depth: str) -> _Response:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop>' + properties + '</d:prop></d:propfind>'
        ).encode("utf-8")
        response = self._request("PROPFIND", url, body, {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
        self._raise_status(response, (200, 207))
        return response

    @staticmethod
    def _properties(response: _Response) -> list[tuple[str, dict[str, ET.Element]]]:
        if b"<!DOCTYPE" in response.body.upper() or b"<!ENTITY" in response.body.upper():
            raise ICloudError("iCloud 返回了不支持的日历数据。", code="invalid_xml")
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError:
            raise ICloudError("无法读取 iCloud 日历信息，请检查账户和服务地址。", code="invalid_xml") from None
        result = []
        for element in root.findall("d:response", NS):
            href = element.findtext("d:href", default="", namespaces=NS)
            properties = {}
            for propstat in element.findall("d:propstat", NS):
                status = propstat.findtext("d:status", default="", namespaces=NS)
                if not re.search(r"\s200(?:\s|$)", status):
                    continue
                prop = propstat.find("d:prop", NS)
                if prop is not None:
                    properties.update({child.tag: child for child in prop})
            result.append((href, properties))
        return result

    @staticmethod
    def _property_href(records: list[tuple[str, dict[str, ET.Element]]], name: str) -> str | None:
        for _, props in records:
            element = props.get(name)
            if element is not None:
                href = element.findtext("d:href", default="", namespaces=NS).strip()
                if href:
                    return href
        return None

    def discover_calendars(self) -> list[dict[str, Any]]:
        principal_response = self._propfind(self.endpoint, "<d:current-user-principal/>", "0")
        principal_href = self._property_href(self._properties(principal_response), "{" + DAV + "}current-user-principal")
        if not principal_href:
            raise ICloudError("iCloud 没有返回账户日历入口，请核对专用 App 密码和账户地区。", code="principal_not_found")
        principal_url = _resolve_url(principal_response.url, principal_href)
        home_response = self._propfind(principal_url, "<c:calendar-home-set/>", "0")
        home_href = self._property_href(self._properties(home_response), "{" + CALDAV + "}calendar-home-set")
        if not home_href:
            raise ICloudError("没有找到 iCloud 日历。请先在 iPhone 中开启 iCloud 日历。", code="calendar_home_not_found")
        home_url = _resolve_url(home_response.url, home_href)
        calendars_response = self._propfind(
            home_url,
            "<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/><d:current-user-privilege-set/>",
            "1",
        )
        calendars = []
        seen = set()
        for href, props in self._properties(calendars_response):
            resource_type = props.get("{" + DAV + "}resourcetype")
            if resource_type is None or resource_type.find("c:calendar", NS) is None:
                continue
            supported = props.get("{" + CALDAV + "}supported-calendar-component-set")
            if supported is None or not any(item.get("name", "").upper() == "VEVENT" for item in supported.findall("c:comp", NS)):
                continue
            calendar_url = _resolve_url(calendars_response.url, href)
            if not calendar_url.endswith("/"):
                calendar_url += "/"
            if calendar_url in seen:
                continue
            seen.add(calendar_url)
            display_element = props.get("{" + DAV + "}displayname")
            display_name = "".join(display_element.itertext()).strip() if display_element is not None else ""
            display_name = display_name or "iCloud 日历"
            privileges = props.get("{" + DAV + "}current-user-privilege-set")
            # If the server doesn't report privileges, editing remains subject to
            # the server's PUT authorization rather than an invented restriction.
            writable = privileges is None or any(
                privileges.find(".//d:" + name, NS) is not None
                for name in ("all", "write", "write-content", "bind")
            )
            calendars.append({"display_name": display_name, "name": display_name, "href": calendar_url, "url": calendar_url, "writable": writable})
        return calendars

    @staticmethod
    def event_url(calendar_url: str, uid: str) -> str:
        if not isinstance(uid, str) or not uid or any(ord(char) < 32 for char in uid):
            raise ICloudError("日历事件标识无效。", code="invalid_uid")
        base = validate_icloud_url(calendar_url).rstrip("/") + "/"
        filename = "memo-" + hashlib.sha256(uid.encode("utf-8")).hexdigest() + ".ics"
        return _resolve_url(base, filename)

    def get_event(self, url: str) -> dict[str, Any] | None:
        response = self._request("GET", url, headers={"Accept": "text/calendar"})
        if response.status == 404:
            return None
        self._raise_status(response, (200,))
        try:
            ics = response.body.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ICloudError("无法读取 iCloud 事件内容，请刷新日历。", code="invalid_calendar_data") from None
        if "BEGIN:VCALENDAR" not in ics.upper():
            raise ICloudError("iCloud 没有返回有效的日历事件，请检查身份验证。", code="invalid_calendar_data")
        etag = response.headers.get("etag")
        if not etag:
            raise ICloudError("iCloud 没有返回事件版本，暂不能安全更新。请稍后刷新。", code="missing_etag")
        return {"url": response.url, "etag": _validate_etag(etag), "ics": ics}

    def upsert_event(self, calendar_url: str, uid: str, ics: str | bytes, etag: str | None = None) -> dict[str, Any]:
        if isinstance(ics, bytes):
            try:
                text = ics.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ICloudError("日历内容必须使用 UTF-8 编码。", code="invalid_calendar_data") from None
        elif isinstance(ics, str):
            text = ics
        else:
            raise ICloudError("日历内容无效。", code="invalid_calendar_data")
        if _calendar_uids(text) != {uid}:
            raise ICloudError("日历内容和事件标识不一致，已停止保存。", code="uid_mismatch")
        data = text.encode("utf-8")
        if len(data) > MAX_RESPONSE_BYTES:
            raise ICloudError("日历事件内容过大，已停止保存。", code="calendar_data_too_large")
        url = self.event_url(calendar_url, uid)
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag is None:
            headers["If-None-Match"] = "*"  # Never blindly overwrite an existing event.
        else:
            headers["If-Match"] = _validate_etag(etag)
        response = self._request("PUT", url, data, headers)
        self._raise_status(response, (200, 201, 204))
        receipt = self.get_event(response.url)
        if receipt is None or not events_equivalent(text, receipt["ics"]):
            raise ICloudError("保存请求已提交，但还不能确认事件。请刷新后查看，不要重复新建。", code="confirmation_failed")
        # Confirmation covers time, recurrence and alarm, not phone delivery.
        return {"url": receipt["url"], "etag": receipt["etag"], "uid": uid, "verified": True}

    def delete_event(self, url: str, etag: str | None = None,
                     expected_uid: str | None = None) -> dict[str, Any]:
        safe_url = validate_icloud_url(url)
        current = self.get_event(safe_url)
        if current is None:
            return {"url": safe_url, "deleted": True, "already_missing": True}
        if not expected_uid or not expected_uid.startswith("memo-") or _calendar_uids(current["ics"]) != {expected_uid}:
            raise ICloudConflictError("事件标识不属于这条备忘录，已停止取消。")
        if etag is not None and current["etag"] != etag:
            raise ICloudConflictError()
        safe_url = current["url"]
        etag = current["etag"]
        response = self._request("DELETE", safe_url, headers={"If-Match": _validate_etag(etag)})
        if response.status == 404:
            return {"url": response.url, "deleted": True, "already_missing": True}
        self._raise_status(response, (200, 202, 204))
        if self.get_event(response.url) is not None:
            raise ICloudError("删除请求已提交，但事件仍然可读取。请稍后刷新确认。", code="delete_confirmation_failed")
        return {"url": response.url, "deleted": True, "already_missing": False}
