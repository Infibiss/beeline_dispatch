"""A second process must never serve a different implementation on our port."""
import socket
from copy import deepcopy
import pytest

from field_router.scenario_server import create_server
from test_engineer_unavailability import session_with_route


def test_second_server_on_same_port_is_rejected_and_original_survives(make_scenario, tmp_path):
    session = session_with_route(make_scenario)
    server = create_server(session, tmp_path / 'first.json', port=0, disable_gps=True)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
            assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        with pytest.raises(OSError, match=str(server.server_port)):
            create_server(deepcopy(session), tmp_path / 'second.json',
                          port=server.server_port, disable_gps=True)
        assert server.socket.fileno() >= 0
        assert not (tmp_path / 'second.json').exists()
    finally:
        server.server_close()


def test_rejects_legacy_reusable_listener_on_windows(make_scenario, tmp_path):
    if not hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        pytest.skip('Windows legacy SO_REUSEADDR listener regression')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as legacy:
        legacy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        legacy.bind(('0.0.0.0', 0)); legacy.listen()
        port = legacy.getsockname()[1]
        with pytest.raises(OSError, match=str(port)):
            create_server(session_with_route(make_scenario), tmp_path / 'new.json',
                          port=port, host='0.0.0.0', disable_gps=True)
