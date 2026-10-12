"""Fixed-origin, bounded WordPress REST adapter using Application Passwords."""

import hashlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import WordPressPayloadV1
from .models import EditorialError


def validate_base_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.port not in (None, 443)
            or url.query
            or url.fragment
            or "\\" in value
            or any(ord(c) <= 32 for c in value)
            or url.hostname.endswith((".local", ".internal", ".localhost"))
            or url.hostname == "localhost"
        ):
            raise ValueError
        try:
            ipaddress.ip_address(url.hostname)
        except ValueError:
            pass
        else:
            raise ValueError
        if any(part in {".", ".."} for part in url.path.split("/")) or "%" in url.path:
            raise ValueError
        host = url.hostname.encode("idna").decode("ascii").lower()
        return urlunsplit(("https", host, url.path.rstrip("/"), "", ""))
    except ValueError:
        raise EditorialError("WORDPRESS_HTTPS_URL_REQUIRED", 422) from None


def post_resource(post_type: str, rest_base: str | None = None) -> str:
    """Accept one route segment, never an arbitrary REST path."""
    resource = (
        rest_base
        if rest_base is not None
        else {"post": "posts", "news_weave": "news-weave"}.get(post_type, post_type)
    )
    if (
        re.fullmatch(r"[a-z0-9_-]{1,20}", post_type) is None
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", resource) is None
    ):
        raise EditorialError("WORDPRESS_POST_RESOURCE_INVALID", 422)
    if (post_type, resource) not in {("post", "posts"), ("news_weave", "news-weave")}:
        raise EditorialError("WORDPRESS_POST_RESOURCE_UNSUPPORTED", 422)
    return resource


def wordpress_target(
    base_url: str,
    post_type: str = "post",
    rest_base: str | None = None,
) -> str:
    """Identify the supported site and post route without credentials."""
    base_url = validate_base_url(base_url)
    resource = post_resource(post_type, rest_base)
    identity = (
        base_url
        if (post_type, resource) == ("post", "posts")
        else json.dumps([base_url, post_type, resource], separators=(",", ":"))
    )
    return "wordpress:" + hashlib.sha256(identity.encode()).hexdigest()


class WordPressTransport(httpx.BaseTransport):
    def __init__(self, host: str) -> None:
        self.host = host
        self.inner = httpx.HTTPTransport(
            retries=0, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.host != self.host:
            raise EditorialError("WORDPRESS_ORIGIN_BLOCKED", 502)
        try:
            addresses = {
                str(row[4][0])
                for row in socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
            }
        except OSError:
            raise EditorialError("WORDPRESS_DNS_ERROR", 502) from None
        if not addresses or any(
            not ipaddress.ip_address(a).is_global
            or ipaddress.ip_address(a).is_multicast
            or ipaddress.ip_address(a).is_reserved
            for a in addresses
        ):
            raise EditorialError("WORDPRESS_DNS_BLOCKED", 502)
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=sorted(addresses)[0]),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.host},
        )
        return self.inner.handle_request(pinned)

    def close(self) -> None:
        self.inner.close()


class WordPressClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        post_type: str = "post",
        rest_base: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.post_type = post_type
        self.rest_base = post_resource(post_type, rest_base)
        if not username.strip() or not password.strip() or ":" in username:
            raise EditorialError("WORDPRESS_CREDENTIALS_REQUIRED", 422)
        self.username, self.password = username, password
        self.transport, self.sleep = transport, sleep

    def request(
        self,
        method: str,
        path: str,
        *,
        resource: Literal["posts", "media"] = "posts",
        **kwargs: Any,
    ) -> Any:
        # Writes are never automatically replayed: a 5xx can occur after remote commit.
        route = self.rest_base if resource == "posts" else "media"
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            transport = self.transport or WordPressTransport(urlsplit(self.base_url).hostname or "")
            try:
                with (
                    httpx.Client(
                        transport=transport,
                        auth=httpx.BasicAuth(self.username, self.password),
                        follow_redirects=False,
                        trust_env=False,
                        timeout=20,
                    ) as client,
                    client.stream(
                        method, self.base_url + "/wp-json/wp/v2/" + route + path, **kwargs
                    ) as response,
                ):
                    if response.status_code in {429, 500, 502, 503, 504}:
                        if attempt + 1 < attempts:
                            try:
                                delay = float(response.headers.get("Retry-After", "1"))
                            except ValueError:
                                delay = 1
                            if delay > 30:
                                raise EditorialError("WORDPRESS_RETRY_LATER", 503)
                            self.sleep(max(0, delay))
                            continue
                        raise EditorialError("WORDPRESS_REMOTE_UNAVAILABLE", 503)
                    if response.status_code not in {200, 201}:
                        raise EditorialError("WORDPRESS_REQUEST_REJECTED", 502)
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 1000000:
                            raise EditorialError("WORDPRESS_RESPONSE_TOO_LARGE", 502)
                        chunks.append(chunk)
                    return json.loads(b"".join(chunks))
            except httpx.HTTPError:
                raise EditorialError("WORDPRESS_NETWORK_UNCERTAIN", 503) from None
            except EditorialError:
                raise
            except (ValueError, UnicodeError):
                raise EditorialError("WORDPRESS_RESPONSE_INVALID", 502) from None
        raise EditorialError("WORDPRESS_REMOTE_UNAVAILABLE", 503)

    def checked_post(self, result: Any) -> dict[str, Any]:
        if (
            not isinstance(result, dict)
            or type(result.get("id")) is not int
            or result["id"] <= 0
            or result.get("type") != self.post_type
            or not isinstance(result.get("link"), str)
        ):
            raise EditorialError("WORDPRESS_RESPONSE_INVALID", 502)
        if (
            result.get("status")
            not in {"draft", "pending", "private", "publish", "future", "trash"}
            or not isinstance(result.get("slug"), str)
            or not isinstance(result.get("modified_gmt"), str)
            or any(
                not isinstance(result.get(k), dict) or not isinstance(result[k].get("raw"), str)
                for k in ("title", "content", "excerpt")
            )
            or any(
                not isinstance(result.get(k), list)
                or any(type(v) is not int or v <= 0 for v in result[k])
                for k in ("categories", "tags")
            )
        ):
            raise EditorialError("WORDPRESS_RESPONSE_INVALID", 502)
        try:
            link = urlsplit(result["link"])
        except ValueError:
            raise EditorialError("WORDPRESS_REMOTE_LINK_INVALID", 502) from None
        origin = urlsplit(self.base_url)
        if link.scheme != "https" or link.netloc != origin.netloc or link.username or link.password:
            raise EditorialError("WORDPRESS_REMOTE_LINK_INVALID", 502)
        return result

    def find(self, slug: str) -> dict[str, Any] | None:
        result = self.request(
            "GET",
            "",
            params={
                "slug": slug + "," + slug + "__trashed",
                "context": "edit",
                "status": "draft,pending,private,publish,future,trash",
                "per_page": 100,
            },
        )
        if not isinstance(result, list):
            raise EditorialError("WORDPRESS_RESPONSE_INVALID", 502)
        if len(result) > 1:
            raise EditorialError("WORDPRESS_RECONCILE_CONFLICT")
        return self.checked_post(result[0]) if result else None

    def get(self, post_id: str) -> dict[str, Any]:
        if not post_id.isdecimal() or int(post_id) <= 0:
            raise EditorialError("WORDPRESS_POST_ID_INVALID")
        return self.checked_post(self.request("GET", "/" + post_id, params={"context": "edit"}))

    def create_draft(self, payload: WordPressPayloadV1) -> dict[str, Any]:
        payload = WordPressPayloadV1.model_validate(payload.model_dump())
        return self.checked_post(self.request("POST", "", json=payload.model_dump(mode="json")))

    def checked_media(self, value: Any) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or type(value.get("id")) is not int
            or value["id"] <= 0
            or value.get("media_type") != "image"
            or value.get("mime_type") != "image/jpeg"
            or value.get("status") not in {"inherit", "private"}
            or not isinstance(value.get("slug"), str)
            or not isinstance(value.get("source_url"), str)
            or not isinstance(value.get("media_details"), dict)
        ):
            raise EditorialError("WORDPRESS_MEDIA_RESPONSE_INVALID", 502)
        try:
            url = urlsplit(value["source_url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError
        except ValueError:
            raise EditorialError("WORDPRESS_MEDIA_RESPONSE_INVALID", 502) from None
        return value

    def find_media(self, slug: str) -> dict[str, Any] | None:
        result = self.request(
            "GET",
            "",
            resource="media",
            params={
                "slug": slug + "," + slug + "__trashed",
                "context": "edit",
                "per_page": 100,
            },
        )
        if not isinstance(result, list) or len(result) > 1:
            raise EditorialError("WORDPRESS_MEDIA_RECONCILE_CONFLICT")
        return self.checked_media(result[0]) if result else None

    def get_media(self, media_id: str) -> dict[str, Any]:
        if not media_id.isdecimal() or int(media_id) <= 0:
            raise EditorialError("WORDPRESS_MEDIA_ID_INVALID")
        return self.checked_media(
            self.request(
                "GET",
                "/" + media_id,
                resource="media",
                params={"context": "edit"},
            )
        )

    def upload_image(
        self,
        image: bytes,
        *,
        filename: str,
        slug: str,
        title: str,
        alt_text: str,
        caption: str,
        post_id: str,
    ) -> dict[str, Any]:
        if not post_id.isdecimal() or int(post_id) <= 0:
            raise EditorialError("WORDPRESS_POST_ID_INVALID")
        return self.checked_media(
            self.request(
                "POST",
                "",
                resource="media",
                files={"file": (filename, image, "image/jpeg")},
                data={
                    "slug": slug + "," + slug + "__trashed",
                    "title": title,
                    "alt_text": alt_text,
                    "caption": caption,
                    "post": post_id,
                },
            )
        )

    def set_featured_media(
        self,
        post_id: str,
        media_id: str,
        *,
        content: str | None = None,
    ) -> dict[str, Any]:
        if not post_id.isdecimal() or int(post_id) <= 0:
            raise EditorialError("WORDPRESS_POST_ID_INVALID")
        if not media_id.isdecimal() or int(media_id) <= 0:
            raise EditorialError("WORDPRESS_MEDIA_ID_INVALID")
        decoration: dict[str, Any] = {"featured_media": int(media_id)}
        if content is not None:
            decoration["content"] = content
        return self.checked_post(
            self.request(
                "POST",
                "/" + post_id,
                json=decoration,
            )
        )

    def get_media_bytes(self, url: str) -> bytes:
        """Read-only, bounded proof of an existing asset. Never forward credentials to a CDN."""
        from .protocol import ProtocolError

        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ProtocolError("WORDPRESS_MEDIA_URL_INVALID")
        same_origin = parsed.netloc == urlsplit(self.base_url).netloc
        try:
            with (
                httpx.Client(
                    transport=WordPressTransport(parsed.hostname),
                    follow_redirects=False,
                    trust_env=False,
                    timeout=20,
                    auth=httpx.BasicAuth(self.username, self.password) if same_origin else None,
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code != 200:
                    raise ProtocolError("WORDPRESS_MEDIA_PROOF_UNAVAILABLE")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 8_000_000:
                        raise ProtocolError("WORDPRESS_MEDIA_PROOF_TOO_LARGE")
                return bytes(data)
        except httpx.HTTPError:
            raise ProtocolError("WORDPRESS_MEDIA_PROOF_UNAVAILABLE") from None

    def replace_draft(self, post_id: str, payload: WordPressPayloadV1) -> dict[str, Any]:
        if not post_id.isdecimal() or int(post_id) <= 0:
            raise EditorialError("WORDPRESS_POST_ID_INVALID")
        value = payload.model_dump(mode="json")
        # Omit status: never turn a concurrently published post back into a draft.
        value.pop("status")
        return self.checked_post(self.request("POST", "/" + post_id, json=value))
