"""The exact local-patch contracts advertised to planning models."""
from typing import Literal

from pydantic import Field

from .plan import Job, FigurePlan
from .schema import Object, Text, ModelSummary


class EvidenceUpdate(Object):
    job_id: Text
    field: Literal['design_evidence_refs']
    value: list[Text]


class PairingUpdate(Object):
    job_id: Text
    field: Literal['pair_by']
    value: list[Text]


class UnitUpdate(Object):
    job_id: Text
    field: Literal['unit']
    value: Text | None


class JobUpdate(Object):
    job_id: Text
    field: None
    value: list[Job] = Field(min_length=1)


class FigureUpdate(Object):
    figure_id: Text
    figure: FigurePlan


class PlanPatch(Object):
    updates: list[EvidenceUpdate | PairingUpdate | UnitUpdate | JobUpdate] = Field(default_factory=list)
    figure_updates: list[FigureUpdate] = Field(default_factory=list)
    model_updates: list[ModelSummary] = Field(default_factory=list)


class FigurePatch(Object):
    updates: list[FigureUpdate] = Field(min_length=1)
