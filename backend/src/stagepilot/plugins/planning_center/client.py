"""Asynchronous, typed client for the Planning Center Services API."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import ValidationError

from stagepilot.core.config import PlanningCenterSettings
from stagepilot.models.state import ServicePlan, Song
from stagepilot.plugins.planning_center.errors import (
    PlanningCenterApiError,
    PlanningCenterAuthenticationError,
    PlanningCenterConfigurationError,
    PlanningCenterPermissionError,
    PlanningCenterPlanSelectionError,
    PlanningCenterRateLimitError,
    PlanningCenterResponseError,
    PlanningCenterTimeoutError,
    PlanningCenterTransportError,
)
from stagepilot.plugins.planning_center.models import (
    CollectionDocument,
    ItemResource,
    PlanAmbiguousResult,
    PlanDiscoveryResult,
    PlanLoadedResult,
    PlanningCenterPlanCandidate,
    PlanningCenterServiceType,
    PlanNotFoundResult,
    PlanResource,
    PlanTimeResource,
    ServiceTypeResource,
    SkippedItemReason,
    SkippedPlanItem,
)

API_BASE_URL = "https://api.planningcenteronline.com/"
API_HOST = "api.planningcenteronline.com"
API_VERSION = "2018-11-01"
SERVICE_TYPES_PATH = "services/v2/service_types"
MAX_PAGE_SIZE = 100
MAX_PAGES = 100
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class PlanningCenterClient:
    """Call Planning Center with PAT or OAuth auth and safe error translation."""

    def __init__(
        self,
        settings: PlanningCenterSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Two auth paths, selected by the persisted `connection_method`:
        # the original Personal Access Token (HTTP Basic) path, unchanged,
        # and the OAuth Bearer path added for "Sign in with Planning
        # Center". Everything below this constructor is auth-agnostic.
        auth: httpx.Auth | None = None
        headers = {
            "Accept": "application/json",
            "User-Agent": settings.user_agent,
            "X-PCO-API-Version": API_VERSION,
        }
        if settings.connection_method == "oauth":
            try:
                headers["Authorization"] = f"Bearer {settings.bearer_token()}"
            except ValueError:
                raise PlanningCenterConfigurationError(
                    "Planning Center credentials are not configured."
                ) from None
        else:
            try:
                app_id, secret = settings.credentials()
            except ValueError:
                raise PlanningCenterConfigurationError(
                    "Planning Center credentials are not configured."
                ) from None
            auth = httpx.BasicAuth(app_id, secret)
        self._client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            auth=auth,
            headers=headers,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> PlanningCenterClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Release the underlying connection pool."""

        await self._client.aclose()

    async def list_service_types(self) -> list[PlanningCenterServiceType]:
        """Return all available service types in their configured sequence."""

        resources = await self._get_collection(
            CollectionDocument[ServiceTypeResource],
            SERVICE_TYPES_PATH,
            params={"order": "sequence", "per_page": MAX_PAGE_SIZE, "offset": 0},
            resource_label="service type",
        )
        self._ensure_unique_ids(resources, "service types")
        return [
            PlanningCenterServiceType(
                id=resource.id,
                name=resource.attributes.name,
                sequence=resource.attributes.sequence,
                archived=(
                    resource.attributes.archived_at is not None
                    or resource.attributes.deleted_at is not None
                ),
            )
            for resource in resources
        ]

    async def load_plan_for_date(
        self,
        service_type: PlanningCenterServiceType,
        target_date: date,
        timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        """Load today's plan, or the nearest plan within the future search window."""

        if selected_plan_id is not None:
            self._validate_identifier(selected_plan_id, "selected plan")
        if not 0 <= lookahead_days <= 365:
            raise PlanningCenterConfigurationError(
                "The Planning Center plan lookahead must be between 0 and 365 days."
            )
        timezone = self._timezone(timezone_name)
        search_end_date = target_date + timedelta(days=lookahead_days)
        plan_resources = await self._list_plans(
            service_type.id,
            target_date,
            timezone,
            lookahead_days=lookahead_days,
        )
        candidates: list[PlanningCenterPlanCandidate] = []
        for plan_resource in plan_resources:
            plan_times = await self._list_plan_times(service_type.id, plan_resource.id)
            matching_times = self._matching_service_times(
                plan_times,
                target_date,
                search_end_date,
                timezone,
            )
            if not matching_times:
                continue
            candidate_date = matching_times[0].date()
            candidate_times = [
                service_time
                for service_time in matching_times
                if service_time.date() == candidate_date
            ]
            candidates.append(
                PlanningCenterPlanCandidate(
                    id=plan_resource.id,
                    title=self._plan_title(plan_resource),
                    service_type_id=service_type.id,
                    service_type_name=service_type.name,
                    target_date=candidate_date,
                    service_times=candidate_times,
                )
            )

        if candidates:
            nearest_date = min(candidate.target_date for candidate in candidates)
            candidates = [
                candidate for candidate in candidates if candidate.target_date == nearest_date
            ]
        candidates.sort(key=lambda candidate: (candidate.service_times[0], candidate.id))
        if not candidates:
            return PlanNotFoundResult(service_type=service_type, target_date=target_date)

        candidate_date = candidates[0].target_date

        selected_candidate: PlanningCenterPlanCandidate | None = None
        if selected_plan_id is not None:
            selected_candidate = next(
                (candidate for candidate in candidates if candidate.id == selected_plan_id),
                None,
            )
            if selected_candidate is None:
                raise PlanningCenterPlanSelectionError(
                    "The selected Planning Center plan does not match a candidate on the "
                    "nearest available service date."
                )
        elif len(candidates) > 1:
            return PlanAmbiguousResult(
                service_type=service_type,
                target_date=candidate_date,
                candidates=candidates,
            )
        else:
            selected_candidate = candidates[0]

        items = await self._list_items(service_type.id, selected_candidate.id)
        songs, skipped_items = self._extract_songs(items)
        plan = ServicePlan(
            id=selected_candidate.id,
            title=selected_candidate.title,
            date=selected_candidate.target_date,
            service_type=service_type.name,
            service_type_id=service_type.id,
            service_times=[value.strftime("%H:%M") for value in selected_candidate.service_times],
            duration_source="Planning Center scheduled item length",
            songs=songs,
        )
        return PlanLoadedResult(
            candidate=selected_candidate,
            plan=plan,
            skipped_items=skipped_items,
        )

    async def _list_plans(
        self,
        service_type_id: str,
        target_date: date,
        timezone: ZoneInfo,
        *,
        lookahead_days: int = 0,
    ) -> list[PlanResource]:
        self._validate_identifier(service_type_id, "service type")
        start = datetime.combine(target_date, time.min, tzinfo=timezone)
        end = datetime.combine(
            target_date + timedelta(days=lookahead_days + 1),
            time.min,
            tzinfo=timezone,
        )
        resources = await self._get_collection(
            CollectionDocument[PlanResource],
            f"services/v2/service_types/{service_type_id}/plans",
            params={
                "filter": "after,before",
                "after": self._utc_parameter(start - timedelta(seconds=1)),
                "before": self._utc_parameter(end),
                "order": "sort_date",
                "per_page": MAX_PAGE_SIZE,
                "offset": 0,
            },
            resource_label="plan",
        )
        self._ensure_unique_ids(resources, "plans")
        return resources

    async def _list_plan_times(
        self,
        service_type_id: str,
        plan_id: str,
    ) -> list[PlanTimeResource]:
        self._validate_identifier(plan_id, "plan")
        resources = await self._get_collection(
            CollectionDocument[PlanTimeResource],
            f"services/v2/service_types/{service_type_id}/plans/{plan_id}/plan_times",
            params={
                "where[time_type]": "service",
                "order": "starts_at",
                "per_page": MAX_PAGE_SIZE,
                "offset": 0,
            },
            resource_label="plan time",
        )
        self._ensure_unique_ids(resources, "plan times")
        return resources

    async def _list_items(
        self,
        service_type_id: str,
        plan_id: str,
    ) -> list[ItemResource]:
        resources = await self._get_collection(
            CollectionDocument[ItemResource],
            f"services/v2/service_types/{service_type_id}/plans/{plan_id}/items",
            params={
                "include": "song",
                "per_page": MAX_PAGE_SIZE,
                "offset": 0,
            },
            resource_label="plan item",
        )
        self._ensure_unique_ids(resources, "plan items")
        return resources

    async def _get_collection[ResourceT](
        self,
        document_type: type[CollectionDocument[ResourceT]],
        request_url: str,
        *,
        params: Mapping[str, Any],
        resource_label: str,
    ) -> list[ResourceT]:
        collection_url = request_url
        resources: list[ResourceT] = []
        request_params: Mapping[str, Any] | None = params
        seen_requests: set[str] = set()
        for _page_number in range(MAX_PAGES):
            request_key = self._request_key(request_url, request_params)
            if request_key in seen_requests:
                raise PlanningCenterResponseError(
                    "Planning Center returned a repeated pagination request."
                )
            seen_requests.add(request_key)
            offset = self._request_offset(request_url, request_params)
            document = await self._get_collection_page(
                document_type,
                request_url,
                params=request_params,
                resource_label=resource_label,
            )
            resources.extend(document.data)
            if document.links.next:
                request_url = self._validated_next_url(document.links.next)
                request_params = None
                continue
            next_offset = self._next_offset(document, offset)
            if next_offset is None:
                return resources
            offset = next_offset
            request_url = collection_url
            request_params = {
                **params,
                "per_page": MAX_PAGE_SIZE,
                "offset": offset,
            }
        raise PlanningCenterResponseError("Planning Center pagination exceeded the safety limit.")

    async def _get_collection_page[ResourceT](
        self,
        document_type: type[CollectionDocument[ResourceT]],
        request_url: str,
        *,
        params: Mapping[str, Any] | None,
        resource_label: str,
    ) -> CollectionDocument[ResourceT]:
        try:
            response = await self._client.get(request_url, params=params)
        except httpx.TimeoutException:
            raise PlanningCenterTimeoutError(
                "Planning Center did not respond before the request timeout."
            ) from None
        except httpx.RequestError:
            raise PlanningCenterTransportError("Could not connect to Planning Center.") from None

        if response.status_code == 401:
            raise PlanningCenterAuthenticationError(
                "Planning Center rejected the configured application ID or secret."
            )
        if response.status_code == 403:
            raise PlanningCenterPermissionError(
                "The Planning Center user cannot access the requested Services resource."
            )
        if response.status_code == 429:
            raise PlanningCenterRateLimitError(
                self._parse_retry_after(response.headers.get("Retry-After"))
            )
        if not response.is_success:
            raise PlanningCenterApiError(response.status_code)

        try:
            return document_type.model_validate(response.json())
        except (ValueError, ValidationError):
            raise PlanningCenterResponseError(
                f"Planning Center returned an invalid {resource_label} response."
            ) from None

    @staticmethod
    def _matching_service_times(
        resources: list[PlanTimeResource],
        start_date: date,
        end_date: date,
        timezone: ZoneInfo,
    ) -> list[datetime]:
        matching = [
            resource.attributes.starts_at.astimezone(timezone)
            for resource in resources
            if resource.attributes.time_type.casefold() == "service"
            and start_date <= resource.attributes.starts_at.astimezone(timezone).date() <= end_date
        ]
        return sorted(matching)

    @staticmethod
    def _extract_songs(
        resources: list[ItemResource],
    ) -> tuple[list[Song], list[SkippedPlanItem]]:
        ordered = sorted(
            enumerate(resources),
            key=lambda value: (value[1].attributes.sequence, value[0]),
        )
        songs: list[Song] = []
        skipped: list[SkippedPlanItem] = []
        for _original_index, resource in ordered:
            attributes = resource.attributes
            item_type = attributes.item_type.casefold()
            title = attributes.title.strip()
            reason: SkippedItemReason | None = None
            if item_type == "header":
                reason = SkippedItemReason.HEADER
            elif item_type == "media":
                reason = SkippedItemReason.MEDIA
            elif item_type != "song" and resource.relationships.song.data is None:
                reason = SkippedItemReason.NOT_SONG
            elif not title:
                reason = SkippedItemReason.MISSING_TITLE

            if reason is not None:
                skipped.append(
                    SkippedPlanItem(
                        item_id=resource.id,
                        title=title or "(untitled)",
                        description=(attributes.description or "").strip() or None,
                        item_type=attributes.item_type,
                        sequence=attributes.sequence,
                        duration_seconds=attributes.length,
                        reason=reason,
                    )
                )
                continue

            source_song = resource.relationships.song.data
            songs.append(
                Song(
                    id=resource.id,
                    title=title,
                    duration_seconds=attributes.length,
                    order=len(songs) + 1,
                    service_sequence=attributes.sequence,
                    is_generic=source_song is None,
                    source_song_id=source_song.id if source_song else None,
                )
            )
        return songs, skipped

    @staticmethod
    def _plan_title(resource: PlanResource) -> str:
        return (
            resource.attributes.title.strip()
            or resource.attributes.dates.strip()
            or f"Plan {resource.id}"
        )

    @staticmethod
    def _timezone(value: str) -> ZoneInfo:
        try:
            return ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise PlanningCenterConfigurationError(
                "The configured Planning Center timezone is invalid."
            ) from None

    @staticmethod
    def _validate_identifier(value: str, label: str) -> None:
        if SAFE_IDENTIFIER.fullmatch(value) is None:
            raise PlanningCenterConfigurationError(
                f"The Planning Center {label} identifier is invalid."
            )

    @staticmethod
    def _utc_parameter(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _ensure_unique_ids(
        resources: Sequence[ServiceTypeResource | PlanResource | PlanTimeResource | ItemResource],
        label: str,
    ) -> None:
        identifiers = [resource.id for resource in resources]
        if len(identifiers) != len(set(identifiers)):
            raise PlanningCenterResponseError(f"Planning Center returned duplicate {label}.")

    def _validated_next_url(self, value: str) -> str:
        try:
            url = self._client.base_url.join(value)
        except httpx.InvalidURL:
            raise PlanningCenterResponseError(
                "Planning Center returned an invalid pagination URL."
            ) from None
        if url.scheme != "https" or url.host != API_HOST:
            raise PlanningCenterResponseError("Planning Center returned an unsafe pagination URL.")
        return str(url)

    def _request_key(self, request_url: str, params: Mapping[str, Any] | None) -> str:
        url = self._client.base_url.join(request_url)
        if params:
            url = url.copy_merge_params(params)
        return str(url)

    def _request_offset(self, request_url: str, params: Mapping[str, Any] | None) -> int:
        raw_offset = (
            params.get("offset", 0)
            if params is not None
            else self._client.base_url.join(request_url).params.get("offset", "0")
        )
        try:
            offset = int(str(raw_offset))
        except ValueError:
            raise PlanningCenterResponseError(
                "Planning Center returned an invalid pagination offset."
            ) from None
        if offset < 0:
            raise PlanningCenterResponseError(
                "Planning Center returned an invalid pagination offset."
            )
        return offset

    @staticmethod
    def _parse_retry_after(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            retry_after = int(value)
        except ValueError:
            return None
        return retry_after if retry_after >= 0 else None

    @staticmethod
    def _next_offset[ResourceT](
        document: CollectionDocument[ResourceT],
        current_offset: int,
    ) -> int | None:
        if document.meta.next is not None:
            next_offset = document.meta.next.offset
        elif (
            document.meta.total_count is not None
            and current_offset + len(document.data) < document.meta.total_count
        ) or len(document.data) == MAX_PAGE_SIZE:
            next_offset = current_offset + len(document.data)
        else:
            return None

        if next_offset <= current_offset:
            raise PlanningCenterResponseError(
                "Planning Center returned a non-advancing pagination offset."
            )
        return next_offset
