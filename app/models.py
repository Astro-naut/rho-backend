"""Pydantic request/response models for Batch 1 ingest + admin."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class EventIn(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    stage: str = Field(..., min_length=1, max_length=64)
    event: str = Field(..., min_length=1, max_length=64)
    ts: str | int | float | datetime
    ref_code: Optional[str] = Field(default=None, max_length=128)


class ProfileIn(BaseModel):
    phone: Optional[str] = Field(default=None, max_length=32)
    language: Optional[str] = Field(default="hinglish", max_length=32)
    consent: bool = False
    name: Optional[str] = Field(default=None, max_length=128)
    ref_code: Optional[str] = Field(default=None, max_length=128)


class ProfileOut(BaseModel):
    ok: bool = True
    user_id: str
    referral_code: str
    current_stage: Optional[str] = None
    ref_status: Optional[str] = None
    referrer_first_name: Optional[str] = None


class ReferralCodeOut(BaseModel):
    user_id: str
    referral_code: str


class OptOutOut(BaseModel):
    ok: bool = True
    user_id: str
    opted_out: bool = True


class DndIn(BaseModel):
    dnd: bool = True


class PolicyIn(BaseModel):
    min_score: Optional[float] = None
    daily_budget_inr: Optional[float] = None
    call_window_start: Optional[str] = None  # HH:MM
    call_window_end: Optional[str] = None
    timezone: Optional[str] = None
    per_user_cap: Optional[int] = None
    per_user_cap_days: Optional[int] = None
    holdout_pct: Optional[float] = None
    conversion_value_inr: Optional[float] = None
    optout_penalty_inr: Optional[float] = None
    reward_window_hours: Optional[int] = None
    half_life_minutes: Optional[float] = None
    reserved_call_cost_inr: Optional[float] = None

    @field_validator("call_window_start", "call_window_end")
    @classmethod
    def _hhmm(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        parts = v.strip().split(":")
        if len(parts) != 2:
            raise ValueError("expected HH:MM")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("invalid time")
        return f"{h:02d}:{m:02d}"


class PolicyOut(BaseModel):
    playbook_key: str
    min_score: float
    daily_budget_inr: float
    call_window_start: str
    call_window_end: str
    timezone: str
    per_user_cap: int
    per_user_cap_days: int
    holdout_pct: float
    conversion_value_inr: float
    optout_penalty_inr: float
    reward_window_hours: int
    half_life_minutes: float = 120
    reserved_call_cost_inr: float = 15
    updated_at: Optional[datetime] = None


class PlaybookStageIn(BaseModel):
    stage_key: str
    ordinal: int
    label: str
    goal: str
    threshold_minutes: int = Field(..., gt=0)


class ScriptHintIn(BaseModel):
    stage_key: str
    hint_index: int = Field(..., ge=1, le=3)
    text: str


class PlaybookPut(BaseModel):
    label: Optional[str] = None
    agent_id_env_var: Optional[str] = None
    active: Optional[bool] = None
    stages: Optional[List[PlaybookStageIn]] = None
    hints: Optional[List[ScriptHintIn]] = None


class TeamPhoneIn(BaseModel):
    phone: str
    label: Optional[str] = None


class TeamPhoneOut(BaseModel):
    phone: str
    label: Optional[str] = None
    source: str
    created_at: Optional[datetime] = None


class TickOut(BaseModel):
    ok: bool = True
    stalls_opened: int = 0
    stalls_closed: int = 0
    decisions: int = 0
    calls: int = 0
    rewards: int = 0
    polls: int = 0
    skipped: bool = False
    locks_skipped: list[str] = Field(default_factory=list)
