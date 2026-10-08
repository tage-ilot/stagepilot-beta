"""Shared preferred-plan scoring for discovery and explicit duration previews."""

from stagepilot.core.config import PlanningCenterSettings
from stagepilot.plugins.planning_center.models import PlanningCenterPlanCandidate


def preferred_candidate(
    settings: PlanningCenterSettings, candidates: list[PlanningCenterPlanCandidate]
) -> PlanningCenterPlanCandidate | None:
    title = settings.plan_title_preference
    time = settings.preferred_service_time
    scores = [
        (
            int(bool(title and c.title.casefold().strip() == title.casefold().strip()))
            + int(bool(time and any(t.strftime("%H:%M") == time for t in c.service_times))),
            c,
        )
        for c in candidates
    ]
    highest = max((s for s, _ in scores), default=0)
    matches = [c for s, c in scores if s == highest]
    return matches[0] if highest and len(matches) == 1 else None
