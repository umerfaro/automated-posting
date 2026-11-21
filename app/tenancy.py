"""Request-local identity. Never accept workspace identity from arbitrary request payloads."""
from contextvars import ContextVar

workspace = ContextVar('workspace', default='')
identity = ContextVar('identity', default=None)
