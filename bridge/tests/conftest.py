import pytest


@pytest.fixture(autouse=True)
def _no_outbound_services(monkeypatch: pytest.MonkeyPatch):
    """Tests must never dial the real relay. Tunnel tests opt back in with their own local relay."""
    monkeypatch.setenv("POCKET_RELAY_REMOTE", "0")
    monkeypatch.setenv("POCKET_RELAY_PUSH_URL", "http://127.0.0.1:9/unused")
