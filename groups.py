# groups.py
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import uuid

class GroupBase(BaseModel):
    name: str
    slug: str
    is_admin_group: bool = False
    access_level: int = 0  # <--- ДОБАВЛЕНО (0=User, 1=Basic, 2=Plus, 3=Alpha)

class AssignGroupRequest(BaseModel):
    user_uid: uuid.UUID
    group_slug: str
    duration_days: int = 30 # Сделаем обязательным или дефолтным

class RevokeGroupRequest(BaseModel):
    user_uid: uuid.UUID
    group_slug: str
