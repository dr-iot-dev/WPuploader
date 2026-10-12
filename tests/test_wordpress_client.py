import httpx
import pytest
from wpuploader.wordpress import WordPressClient, validate_base_url
from wpuploader.models import EditorialError


@pytest.mark.parametrize(
    "url",
    [
        "http://cms.example",
        "https://user:password@cms.example",
        "https://cms.example/?token=secret",
        "https://localhost",
        "https://127.0.0.1",
        "https://cms.example:8443",
        "https://cms.example/../admin",
    ],
)
def test_wordpress_https_origin_validation(url):
    with pytest.raises(EditorialError):
        validate_base_url(url)


def test_response_errors_redirects_and_retry_do_not_leak_credentials():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example/"})

    port = WordPressClient(
        "https://cms.example", "user", "password", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(EditorialError) as exc:
        port.find("news-test")
    assert len(requests) == 1 and "password" not in str(exc.value)
    for method, attempts in [("GET", 3), ("POST", 1)]:
        requests.clear()

        def unavailable(request):
            requests.append(request)
            return httpx.Response(503, headers={"Retry-After": "0"}, json={"secret": "bad"})

        port = WordPressClient(
            "https://cms.example",
            "user",
            "password",
            transport=httpx.MockTransport(unavailable),
            sleep=lambda _: None,
        )
        with pytest.raises(EditorialError, match="REMOTE_UNAVAILABLE"):
            port.request(method, "")
        assert len(requests) == attempts


def test_same_wordpress_origin_has_the_same_target_identity():
    assert validate_base_url("https://CMS.example:443/subdir/") == "https://cms.example/subdir"
    assert validate_base_url("https://cms.example/subdir") == "https://cms.example/subdir"


def test_invalid_wordpress_responses_are_rejected():
    base = {
        "id": 1,
        "type": "post",
        "link": "https://cms.example/?p=1",
        "status": "draft",
        "slug": "news-test",
        "modified_gmt": "2026-10-01T01:00:00",
        "title": {"raw": "title"},
        "content": {"raw": "content"},
        "excerpt": {"raw": ""},
        "categories": [12],
        "tags": [],
    }
    for key, value in [
        ("content", {"rendered": "not raw"}),
        ("id", True),
        ("categories", [True]),
        ("status", "unknown"),
        ("link", "https://attacker.example/"),
        ("modified_gmt", None),
    ]:
        port = WordPressClient(
            "https://cms.example",
            "user",
            "password",
            transport=httpx.MockTransport(
                lambda _, k=key, v=value: httpx.Response(
                    200,
                    json={**base, k: v},
                )
            ),
        )
        with pytest.raises(EditorialError):
            port.get("1")


def test_wordpress_response_body_is_bounded():
    port = WordPressClient(
        "https://cms.example",
        "user",
        "password",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1000001)),
    )
    with pytest.raises(EditorialError, match="RESPONSE_TOO_LARGE"):
        port.get("1")


def test_dns_pinning_blocks_rebinding_without_sending_credentials(monkeypatch):
    from wpuploader.wordpress import WordPressTransport

    calls = []
    transport = WordPressTransport("cms.example")
    transport.inner.close()
    transport.inner = httpx.MockTransport(
        lambda req: calls.append(req) or httpx.Response(200, json=[])
    )
    monkeypatch.setattr(
        "wpuploader.wordpress.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    port = WordPressClient("https://cms.example", "user", "password", transport=transport)
    assert port.find("news-test") is None
    assert calls[0].url.host == "93.184.216.34"
    assert calls[0].headers["Host"] == "cms.example"
    assert calls[0].extensions["sni_hostname"] == "cms.example"
    monkeypatch.setattr(
        "wpuploader.wordpress.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(EditorialError, match="DNS_BLOCKED"):
        port.find("news-test")
    assert len(calls) == 1
