"""Validate declared tool arguments without fetching schemas or exposing values."""

import json
import math
from contextvars import ContextVar
from functools import lru_cache

from jsonschema import Draft202012Validator, exceptions, validators
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable


MAX_SCHEMA_BYTES = 1024 * 1024
MAX_DEPTH = 128
MAX_NODES = 65536
MAX_VALIDATION_STEPS = 65536
_steps_remaining = ContextVar("tool_schema_steps", default=0)


def _deny_remote_reference(uri):
    raise NoSuchResource(ref=uri)


_LOCAL_REFERENCES = Registry(retrieve=_deny_remote_reference)


def _within_json_limits(value):
    pending = [(value, 0)]
    remaining = MAX_NODES
    while pending:
        item, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > MAX_DEPTH:
            return False
        if isinstance(item, float) and not math.isfinite(item):
            return False
        if isinstance(item, int) and item.bit_length() > 16384:
            return False
        if isinstance(item, dict):
            if len(item) > remaining:
                return False
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > remaining:
                return False
            pending.extend((child, depth + 1) for child in item)
    return True


def _budgeted(keyword_validator):
    def validate(validator, constraint, instance, schema):
        remaining = _steps_remaining.get()
        if remaining <= 0:
            raise ValueError("Tool schema validation budget exceeded")
        _steps_remaining.set(remaining - 1)
        yield from keyword_validator(validator, constraint, instance, schema)

    return validate


@lru_cache(maxsize=8)
def _bounded_validator(base):
    bounded = validators.extend(
        base, {name: _budgeted(check) for name, check in base.VALIDATORS.items()}
    )
    original_evolve = bounded.evolve

    def evolve(self, **changes):
        evolved = original_evolve(self, **changes)
        if type(evolved) is bounded:
            return evolved
        # An explicit nested $schema selects jsonschema's stock class. Wrap it
        # again, preserving the pinned library's local-reference context.
        return _bounded_validator(type(evolved))(
            evolved.schema,
            registry=evolved._registry,
            _resolver=evolved._resolver,
            format_checker=evolved.format_checker,
        )

    bounded.evolve = evolve
    return bounded


def arguments_match_schema(value, schema):
    if schema is None:
        schema = {}
    if not isinstance(schema, (dict, bool)):
        return False
    token = _steps_remaining.set(MAX_VALIDATION_STEPS)
    try:
        if not _within_json_limits(schema) or not _within_json_limits(value):
            return False
        if (
            len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            > MAX_SCHEMA_BYTES
        ):
            return False
        base = validators.validator_for(schema, default=Draft202012Validator)
        base.check_schema(schema)
        validator = _bounded_validator(base)(schema, registry=_LOCAL_REFERENCES)
        return validator.is_valid(value)
    except (
        exceptions.SchemaError,
        Unresolvable,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ):
        # Validation errors can contain complete arguments and schema contents.
        # The transport reports only arguments_schema_mismatch to the caller.
        return False
    finally:
        _steps_remaining.reset(token)
