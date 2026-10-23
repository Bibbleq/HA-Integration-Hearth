"""Opt-in pytest plugin for local runs on Windows: `pytest -p tests.winsock`.

asyncio on Windows builds its event loop on an AF_INET socketpair, which the Home Assistant test plugin's socket block
refuses, so every HA-level test errors before it starts. This re-enables sockets around fixture setup. CI runs on Linux
and does not load it.
"""

import pytest
import pytest_socket


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item):
    pytest_socket.enable_socket()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    pytest_socket.enable_socket()
    yield
