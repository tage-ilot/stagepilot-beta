"""Explicit dependency container for one StagePilot application instance."""

from __future__ import annotations

from dataclasses import dataclass

from stagepilot.core.config import Settings
from stagepilot.core.event_bus import EventBus
from stagepilot.core.lights import LightsController
from stagepilot.core.midi import MidiController
from stagepilot.core.plugin import PluginManager
from stagepilot.core.propresenter import ProPresenterController
from stagepilot.core.settings import SettingsService
from stagepilot.core.state import StateStore
from stagepilot.planning_center_oauth import PlanningCenterOAuthService
from stagepilot.plugins.planning_center.plugin import PlanningCenterPlugin
from stagepilot.services.planning_center_setup import PlanningCenterSetupService
from stagepilot.services.state_service import StateService


@dataclass(frozen=True, slots=True)
class Runtime:
    settings: Settings
    event_bus: EventBus
    state_store: StateStore
    state_service: StateService
    plugin_manager: PluginManager
    settings_service: SettingsService
    planning_center_setup: PlanningCenterSetupService
    midi_controller: MidiController | None
    propresenter_controller: ProPresenterController | None = None
    lights_controller: LightsController | None = None
    planning_center: PlanningCenterPlugin | None = None
    planning_center_oauth: PlanningCenterOAuthService | None = None
