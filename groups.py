from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import uuid

class GroupBase(BaseModel):
    name: str
    slug: str
    is_admin_group: bool

class AssignGroupRequest(BaseModel):
    user_uid: uuid.UUID
    group_slug: str
    duration_days: Optional[int] = None # Если None, берется из настроек группы
    is_forever: bool = False

class RevokeGroupRequest(BaseModel):
    user_uid: uuid.UUID
    group_slug: str

class UserGroupInfo(BaseModel):
    group_name: str
    group_slug: str
    expires_at: Optional[datetime]