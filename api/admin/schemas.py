"""Pydantic schemas for the admin API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    ok: bool = True
    actor: str = "admin"


class SettingOut(BaseModel):
    key: str
    value: Any
    type: str
    category: str
    description: str
    updated_at: datetime
    updated_by: Optional[str] = None


class SettingsByCategory(BaseModel):
    category: str
    settings: List[SettingOut]


class UpdateSettingRequest(BaseModel):
    value: Any
    notes: Optional[str] = Field(default=None, max_length=240)


class AuditEntry(BaseModel):
    id: int
    timestamp: datetime
    actor: str
    action: str
    setting_key: Optional[str] = None
    old_value: Any = None
    new_value: Any = None
    notes: Optional[str] = None


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)
