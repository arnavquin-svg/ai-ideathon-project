"""Request bodies for the Radar + SquadUp API."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Role = Literal["developer", "designer", "domain_expert", "presenter"]
Experience = Literal["first_time", "some", "veteran"]
Language = Literal["en", "hi", "mr"]


def _clean_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for tag in tags:
        t = tag.strip().lower()
        if t and t not in seen:
            seen.append(t)
    return seen[:15]


class EventCreate(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    city: str = Field(min_length=2, max_length=60)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list)
    start_date: Optional[date] = None
    venue: str = Field(default="", max_length=160)
    team_size: int = Field(default=4, ge=2, le=6)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)


class CommunityCreate(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    kind: str = Field(default="club", max_length=40)
    platform: Literal["whatsapp", "telegram", "instagram", "discord", "other"] = "whatsapp"
    city: str = Field(min_length=2, max_length=60)
    tags: list[str] = Field(default_factory=list)
    size: int = Field(default=100, ge=1, le=1_000_000)
    languages: list[Language] = Field(default_factory=lambda: ["en"])
    admin_name: str = Field(default="", max_length=60)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)


class RadarRun(BaseModel):
    limit: int = Field(default=12, ge=1, le=50)


class OutreachUpdate(BaseModel):
    draft: Optional[str] = Field(default=None, min_length=10, max_length=4000)
    language: Optional[Language] = None


class RegistrationCreate(BaseModel):
    event_id: Optional[int] = None
    name: str = Field(min_length=1, max_length=80)
    role: Role
    experience: Experience = "first_time"
    language: Language = "en"
    telegram_id: Optional[int] = None
    ref_payload: Optional[str] = Field(default=None, max_length=64)
    college: str = Field(default="", max_length=120)


class ParticipantUpdate(BaseModel):
    language: Optional[Language] = None
    college: Optional[str] = Field(default=None, max_length=120)


class SimulateRequest(BaseModel):
    count: int = Field(default=12, ge=1, le=100)
