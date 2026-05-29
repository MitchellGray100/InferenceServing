import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_auth_user_check: do not stub token subject user-existence checks",
    )


@pytest.fixture(autouse=True)
def stub_auth_user_existence(monkeypatch, request):
    """Keep route tests focused on authorization wiring instead of DB setup."""
    if request.node.get_closest_marker("real_auth_user_check"):
        return

    monkeypatch.setattr(
        "app.security.tokens.require_existing_user_id",
        lambda user_id: str(user_id),
    )
    monkeypatch.setattr(
        "app.routes.dashboard.require_existing_user_id",
        lambda user_id: str(user_id),
    )
