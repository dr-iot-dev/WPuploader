"""CMS-independent HTML and metadata validation."""

from io import BytesIO
from uuid import UUID

from bs4 import BeautifulSoup, Comment, Tag
from PIL import Image

from .protocol import ProtocolError, digest, package_identity, source_hash

ALLOWED = frozenset(
    {
        "p",
        "br",
        "h2",
        "h3",
        "h4",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "a",
        "blockquote",
        "pre",
        "code",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
    }
)


def validate_html(value: str) -> None:
    from urllib.parse import urlsplit

    if not value.strip() or len(value) > 200000:
        raise ProtocolError("HTML_SIZE_INVALID")
    soup = BeautifulSoup(value, "html.parser")
    if soup.find_all(string=lambda s: isinstance(s, Comment)):
        raise ProtocolError("HTML_NOT_ALLOWED")
    for node in soup.find_all(True):
        if not isinstance(node, Tag) or node.name not in ALLOWED:
            raise ProtocolError("HTML_NOT_ALLOWED")
        allowed_attrs = {"href", "rel"} if node.name == "a" else set()
        if set(node.attrs) - allowed_attrs:
            raise ProtocolError("HTML_ATTRIBUTE_NOT_ALLOWED")
        href = node.get("href")
        if href is not None:
            if not isinstance(href, str):
                raise ProtocolError("HTML_LINK_INVALID")
            parsed = urlsplit(href)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or any(ord(c) <= 32 for c in href)
            ):
                raise ProtocolError("HTML_LINK_INVALID")


def validate_draft(meta: dict, files: dict[str, bytes], review_required: bool = False) -> None:
    for key in ("producer_id", "article_id", "draft_id", "package_id"):
        if not isinstance(meta.get(key), str) or meta[key] != str(UUID(meta[key])):
            raise ProtocolError("DRAFT_UUID_INVALID")
    for key in ("revision", "artifact_revision"):
        if type(meta[key]) is not int or meta[key] < 1:
            raise ProtocolError("DRAFT_REVISION_INVALID")
    if meta["package_id"] != package_identity(meta):
        raise ProtocolError("DRAFT_IDENTITY_MISMATCH")
    if not {"article.md", "article.html"} <= files.keys() or len(files) > 8:
        raise ProtocolError("DRAFT_FILES_REQUIRED")
    if meta.get("source_normalization_version") != "source-set-v1":
        raise ProtocolError("SOURCE_NORMALIZATION_UNSUPPORTED")
    urls = meta.get("source_urls", [])
    if not urls or meta.get("source_set_hash") != source_hash(urls):
        raise ProtocolError("SOURCE_SET_HASH_MISMATCH")
    if not isinstance(meta.get("source_urls"), list) or not all(
        isinstance(url, str) for url in meta["source_urls"]
    ):
        raise ProtocolError("SOURCE_URLS_INVALID")
    for key in ("category_keys", "tag_keys"):
        values = meta.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise ProtocolError("TAXONOMY_KEYS_INVALID")
    eligibility = meta.get("eligibility", {})
    if not isinstance(eligibility, dict):
        raise ProtocolError("ELIGIBILITY_INVALID")
    if eligibility.get("verification") != "pass" or eligibility.get("mode") != "production":
        raise ProtocolError("DRAFT_NOT_ELIGIBLE")
    if eligibility.get("html_hash") != digest(files["article.html"]) or eligibility.get(
        "markdown_hash"
    ) != digest(files["article.md"]):
        raise ProtocolError("VERIFICATION_BINDING_MISMATCH")
    if eligibility.get("review_required") or review_required:
        if eligibility.get("review") != "approve":
            raise ProtocolError("HUMAN_APPROVAL_REQUIRED")
    validate_html(files["article.html"].decode("utf-8"))
    if not meta.get("category_keys"):
        raise ProtocolError("CATEGORY_KEYS_REQUIRED")
    image = meta.get("featured_image")
    if image is not None:
        if (
            not isinstance(image, dict)
            or type(image.get("width")) is not int
            or type(image.get("height")) is not int
            or not 1 <= image["width"] <= 4096
            or not 1 <= image["height"] <= 4096
        ):
            raise ProtocolError("FEATURED_IMAGE_DIMENSIONS_INVALID")
        raw = files.get(image["path"], b"")
        if image.get("mime") != "image/jpeg" or not raw or len(raw) > 8_000_000:
            raise ProtocolError("FEATURED_IMAGE_INVALID")
        with Image.open(BytesIO(raw), formats=["JPEG"]) as decoded:
            if list(decoded.size) != [image["width"], image["height"]]:
                raise ProtocolError("FEATURED_IMAGE_DIMENSIONS_INVALID")
            decoded.verify()
        with Image.open(BytesIO(raw), formats=["JPEG"]) as decoded:
            decoded.load()
    elif meta.get("image_skip_reason") not in {"disabled", "manual_skip"}:
        raise ProtocolError("IMAGE_SKIP_REASON_REQUIRED")
