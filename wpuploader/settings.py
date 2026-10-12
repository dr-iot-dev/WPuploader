"""Independent uploader settings; no AI, PostgreSQL or NewsWeave dependency."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import ProtocolError, require_local


def boolean(value: str) -> bool:
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ProtocolError("BOOLEAN_SETTING_INVALID")
    return value.lower() in {"true", "1"}


def mapping(name: str) -> dict[str, int]:
    value = json.loads(os.getenv(name, "{}"))
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or type(v) is not int or v <= 0 for k, v in value.items()
    ):
        raise ProtocolError("TAXONOMY_MAPPING_INVALID")
    return value


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    transfer_dir: Path | None = None
    enabled: bool = False
    base_url: str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    post_type: str = "news_weave"
    rest_base: str = "news-weave"
    category_map: dict[str, int] = field(default_factory=dict)
    tag_map: dict[str, int] = field(default_factory=dict)
    review_required: bool = False

    def __post_init__(self) -> None:
        require_local(self.state_dir)
        if (self.post_type, self.rest_base) not in {
            ("post", "posts"),
            ("news_weave", "news-weave"),
        }:
            raise ProtocolError("WORDPRESS_POST_TYPE_UNSUPPORTED")

    @classmethod
    def from_env(cls, state_dir: Path | None = None) -> "Settings":
        default = Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "WPuploader"
        post_type = os.getenv("WORDPRESS_POST_TYPE", "news_weave")
        return cls(
            state_dir=state_dir or Path(os.getenv("WPUPLOADER_STATE_DIR", str(default))),
            transfer_dir=Path(os.environ["WPUPLOADER_TRANSFER_DIR"])
            if os.getenv("WPUPLOADER_TRANSFER_DIR")
            else None,
            enabled=boolean(os.getenv("WORDPRESS_ENABLED", "false")),
            base_url=os.getenv("WORDPRESS_BASE_URL", ""),
            username=os.getenv("WORDPRESS_USERNAME", ""),
            password=os.getenv("WORDPRESS_APPLICATION_PASSWORD", ""),
            post_type=post_type,
            rest_base=os.getenv(
                "WORDPRESS_REST_BASE", "posts" if post_type == "post" else "news-weave"
            ),
            category_map=mapping("WORDPRESS_CATEGORY_MAP"),
            tag_map=mapping("WORDPRESS_TAG_MAP"),
            review_required=boolean(os.getenv("REVIEW_REQUIRED", "false")),
        )
