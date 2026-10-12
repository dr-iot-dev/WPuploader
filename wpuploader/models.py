"""Draft-only CMS payload. Publication is outside this application's interface."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class EditorialError(ValueError):
    def __init__(self, code: str, status: int = 422) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class WordPressPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=200000)
    excerpt: str = Field(max_length=2000)
    slug: str = Field(pattern=r"^news-[a-f0-9]{64}$")
    status: Literal["draft"] = "draft"
    categories: tuple[Annotated[int, Field(ge=1, strict=True)], ...] = Field(min_length=1)
    tags: tuple[Annotated[int, Field(ge=1, strict=True)], ...] = ()
    featured_media: int = Field(default=0, ge=0, strict=True, exclude_if=lambda value: value == 0)
