from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProxyEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    request_id: str
    user_id: Optional[UUID] = None
    api_key_id: Optional[UUID] = None
    account_id: Optional[UUID] = None
    fallback_provider_id: Optional[UUID] = None
    event_type: str
    status_code: Optional[int] = None
    message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    archive_hot: bool = True


class EventRequestSummary(BaseModel):
    request_id: str
    summary: ProxyEvent
    event_count: int


class EventGroup(BaseModel):
    key: str
    requests: List[EventRequestSummary]


class ProxyEventPage(BaseModel):
    total: int
    limit: int
    offset: int
    events: List[ProxyEvent]

    groups: List[EventGroup] = Field(default_factory=list)
