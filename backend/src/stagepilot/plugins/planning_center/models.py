"""Typed Planning Center JSON:API transport and domain models."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from stagepilot.models.state import ServicePlan


class PlanningCenterServiceType(BaseModel):
    """Service type information safe to expose to setup and selection interfaces."""

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=500)
    sequence: int = Field(ge=0)
    archived: bool = False


class ServiceTypeAttributes(BaseModel):
    """Subset of ServiceType attributes used by StagePilot."""

    name: str = Field(min_length=1, max_length=500)
    sequence: int = Field(default=0, ge=0)
    archived_at: datetime | None = None
    deleted_at: datetime | None = None


class ServiceTypeResource(BaseModel):
    type: Literal["ServiceType"]
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    attributes: ServiceTypeAttributes


class PlanAttributes(BaseModel):
    """Subset of Plan attributes needed for discovery and display."""

    title: str = Field(default="", max_length=500)
    dates: str = Field(default="", max_length=500)
    sort_date: datetime | None = None


class PlanResource(BaseModel):
    type: Literal["Plan"]
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    attributes: PlanAttributes


class PlanTimeAttributes(BaseModel):
    """PlanTime fields used to establish the exact local service date."""

    starts_at: datetime
    ends_at: datetime | None = None
    name: str = Field(default="", max_length=500)
    time_type: str = Field(min_length=1, max_length=32)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def timestamps_must_include_an_offset(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Planning Center timestamps must include a UTC offset.")
        return value


class PlanTimeResource(BaseModel):
    type: Literal["PlanTime"]
    id: str = Field(min_length=1, max_length=128)
    attributes: PlanTimeAttributes


class SongIdentifier(BaseModel):
    type: Literal["Song"]
    id: str = Field(min_length=1, max_length=128)


class SongRelationship(BaseModel):
    data: SongIdentifier | None = None


class ItemRelationships(BaseModel):
    song: SongRelationship = Field(default_factory=SongRelationship)


class ItemAttributes(BaseModel):
    """Plan item fields needed to extract ordered songs and durations."""

    title: str = Field(default="", max_length=500)
    description: str | None = Field(default=None, max_length=10_000)
    item_type: str = Field(min_length=1, max_length=32)
    length: int | None = Field(default=None, ge=0)
    sequence: int = Field(ge=0)
    service_position: str | None = Field(default=None, max_length=32)


class ItemResource(BaseModel):
    type: Literal["Item"]
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    attributes: ItemAttributes
    relationships: ItemRelationships = Field(default_factory=ItemRelationships)


class PlanningCenterPlanCandidate(BaseModel):
    """A plan whose service times match the nearest available local service date."""

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    service_type_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    service_type_name: str = Field(min_length=1, max_length=500)
    target_date: date
    service_times: list[datetime] = Field(min_length=1)

    @field_validator("service_times")
    @classmethod
    def service_times_must_include_offsets(cls, values: list[datetime]) -> list[datetime]:
        if any(value.utcoffset() is None for value in values):
            raise ValueError("Planning Center service times must include a UTC offset.")
        return values


class SkippedItemReason(StrEnum):
    HEADER = "header"
    MEDIA = "media"
    NOT_SONG = "not_song"
    MISSING_TITLE = "missing_title"


class SkippedPlanItem(BaseModel):
    """A visible explanation for an item excluded from the song list."""

    item_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10_000)
    item_type: str = Field(min_length=1, max_length=32)
    sequence: int = Field(ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    reason: SkippedItemReason


class PlanNotFoundResult(BaseModel):
    status: Literal["not_found"] = "not_found"
    service_type: PlanningCenterServiceType | None = None
    target_date: date


class PlanAmbiguousResult(BaseModel):
    status: Literal["ambiguous"] = "ambiguous"
    service_type: PlanningCenterServiceType | None = None
    target_date: date
    candidates: list[PlanningCenterPlanCandidate] = Field(min_length=2)


class PlanLoadedResult(BaseModel):
    status: Literal["loaded"] = "loaded"
    candidate: PlanningCenterPlanCandidate
    plan: ServicePlan
    skipped_items: list[SkippedPlanItem] = Field(default_factory=list)


type PlanDiscoveryResult = PlanNotFoundResult | PlanAmbiguousResult | PlanLoadedResult


class NextPage(BaseModel):
    offset: int = Field(ge=0)


class PaginationMeta(BaseModel):
    total_count: int | None = Field(default=None, ge=0)
    count: int | None = Field(default=None, ge=0)
    next: NextPage | None = None


class CollectionLinks(BaseModel):
    self: str | None = None
    next: str | None = None


class CollectionDocument[ResourceT](BaseModel):
    """JSON:API collection envelope with Planning Center pagination metadata."""

    data: list[ResourceT]
    meta: PaginationMeta = Field(default_factory=PaginationMeta)
    links: CollectionLinks = Field(default_factory=CollectionLinks)
