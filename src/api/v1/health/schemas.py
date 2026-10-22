"""Describe process and database availability without exposing connection details."""

from typing import Literal

from pydantic import BaseModel, Field


class LivenessResponse(BaseModel):
    """Process liveness independent of external services."""

    status: Literal["alive"] = Field(default="alive", description="Process is serving HTTP")


class ReadinessResponse(BaseModel):
    """Per-database availability without connection strings or raw errors."""

    status: Literal["ready", "not_ready"]
    postgresql: Literal["up", "down"] = Field(description="Real PostgreSQL query result")
    clickhouse: Literal["up", "down"] = Field(description="Real ClickHouse query result")
