"""Exercise the product auth CLI through the subscription CLI without credentials or network."""
from pathlib import Path

import pytest

from src.agent import product_cli
from src.agent_runtime import openai_subscription as subscription


@pytest.fixture
def auth_calls(monkeypatch):
    calls = []

    class Credentials:
        def __init__(self, directory=None):
            calls.append(("store", directory))

        async def login(self, *, new_account=False, client_id=None):
            calls.append(("login", new_account, client_id))
            return subscription.LoginResult("oaiapp_test", None, ())

        def status(self):
            calls.append(("status",))
            return {"signed_in": False, "account_count": 0, "accounts": []}

        async def list_models(self):
            calls.append(("models",))
            return []

        async def logout(self):
            calls.append(("logout",))
            return subscription.LogoutResult(True, True)

    monkeypatch.setattr(product_cli, "doctor", lambda: {"status": "ready"})
    monkeypatch.setattr(subscription, "SubscriptionCredentials", Credentials)
    return calls


@pytest.mark.parametrize("action", ["status", "login", "models", "logout"])
def test_existing_auth_actions_keep_the_default_store(auth_calls, action):
    assert product_cli.main(["auth", action]) == 0
    expected = ("login", False, None) if action == "login" else (action,)
    assert auth_calls == [("store", None), expected]


@pytest.mark.parametrize("action", ["status", "login", "models", "logout"])
@pytest.mark.parametrize("directory_first", [True, False])
def test_explicit_store_reaches_subscription_cli(auth_calls, action, directory_first):
    directory = Path("private credentials")
    options = ["--directory", str(directory)]
    argv = ["auth", *options, action] if directory_first else ["auth", action, *options]
    assert product_cli.main(argv) == 0
    expected = ("login", False, None) if action == "login" else (action,)
    assert auth_calls == [("store", directory), expected]


@pytest.mark.parametrize("options,expected", [
    (["--new-account"], ("login", True, None)),
    (["--client-id", "oaiapp_saved"], ("login", False, "oaiapp_saved")),
])
def test_login_account_selection_reaches_subscription_cli(auth_calls, options, expected):
    directory = Path("independent account")
    assert product_cli.main(["auth", "login", *options, "--directory", str(directory)]) == 0
    assert auth_calls == [("store", directory), expected]


@pytest.mark.parametrize("action", ["status", "models", "logout"])
@pytest.mark.parametrize("options", [["--new-account"], ["--client-id", "oaiapp_saved"]])
def test_account_selection_is_rejected_before_other_auth_actions(auth_calls, capsys, action, options):
    with pytest.raises(SystemExit) as error:
        product_cli.main(["auth", action, *options])
    assert error.value.code == 2
    assert "only valid with login" in capsys.readouterr().err
    assert auth_calls == []


def test_login_rejects_conflicting_account_selection(auth_calls, capsys):
    with pytest.raises(SystemExit) as error:
        product_cli.main(["auth", "login", "--new-account", "--client-id", "oaiapp_saved"])
    assert error.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert auth_calls == []


def test_subscription_auth_failure_is_returned_to_the_product_cli(auth_calls, monkeypatch, capsys):
    def unavailable(_self):
        raise subscription.SubscriptionAuthError("Test session requires sign-in.")

    monkeypatch.setattr(subscription.SubscriptionCredentials, "status", unavailable)
    assert product_cli.main(["auth", "status"]) == 2
    assert "Test session requires sign-in." in capsys.readouterr().err
    assert auth_calls == [("store", None)]
