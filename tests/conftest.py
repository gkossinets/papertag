"""Shared pytest fixtures."""

import logging

import pytest

# Loggers that papertag.cli.configure_logging reconfigures.
_CONFIGURED_LOGGERS = ("papertag", "pypdf")


@pytest.fixture(autouse=True)
def _reset_loggers():
    """main() calls configure_logging, which mutates shared loggers
    (handlers, level, propagation). Restore them after every test so
    caplog in later tests still sees records."""
    saved = {}
    for name in _CONFIGURED_LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = (logger.level, list(logger.handlers), logger.propagate)
    yield
    for name, (level, handlers, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.propagate = propagate
