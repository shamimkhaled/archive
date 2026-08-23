from datetime import date
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Role(str, Enum):
    admin = "admin"
    board_secretary = "board_secretary"
    uploader = "uploader"
    board_member = "board_member"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    role: Role = Role.uploader


class UserRead(BaseModel):
    username: str
    role: Role
    is_active: bool


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class DocumentCreate(BaseModel):
    doc_date: date
    doc_id: str
    doc_type: str
    keywords: List[str]
