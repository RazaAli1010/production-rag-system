"""Enums shared by the F12 models (design.md §1)."""

import enum


class UserRole(str, enum.Enum):
    student = "student"
    admin = "admin"


class DocumentStatus(str, enum.Enum):
    registered = "registered"
    downloaded = "downloaded"
    extracted = "extracted"
    indexed = "indexed"
    failed = "failed"


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class RequestChannel(str, enum.Enum):
    web = "web"
    telegram = "telegram"
    api = "api"
