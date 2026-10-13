# Path: app/utils/models/api/auth.py
# Description: Pydantic models for the admin authentication routes.

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # Bounded so an oversized body cannot make the password hash expensive to compute.
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    token: str
    token_type: str = "bearer"
