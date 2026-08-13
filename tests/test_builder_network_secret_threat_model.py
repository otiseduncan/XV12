from __future__ import annotations

import json

import pytest

from .conftest import create_conversation, login


pytestmark = pytest.mark.creator


def call(client, capability_id: str, **arguments):
    response = client.post(f"/api/capabilities/{capability_id}", json={"arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_network_enabled_sandbox_cannot_see_a_secret_that_was_not_explicitly_requested(app, client, monkeypatch):
    """network=true and secret_refs are orthogonal controls. A configured, authorized secret
    reference must not leak into a sandbox run that enables network but does not list it in
    secret_refs -- untrusted network-enabled workspace code should not be able to exfiltrate
    a secret it was never explicitly granted."""
    login(client, "admin")
    conversation = create_conversation(client)
    secret = "XV12_NETWORK_ORTHOGONALITY_SECRET"
    monkeypatch.setenv("XV12_TEST_NETWORK_TOKEN", secret)
    configured = call(client, "secrets.reference.configure", name="network_test_token", environment_name="XV12_TEST_NETWORK_TOKEN", contexts=["builder"])
    assert configured["reference"]["configured"] is True
    workspace = call(client, "builder.workspace.create", name="Network secret boundary")["workspace"]

    receipt = call(
        client, "builder.sandbox.exec", workspace_id=workspace["id"],
        argv=["sh", "-lc", "test -z \"$NETWORK_TEST_TOKEN\" && test -z \"$XV12_TEST_NETWORK_TOKEN\" && echo NO_SECRET_VISIBLE"],
        network=True, timeout_seconds=30, report_type="test_report", conversation_id=conversation["id"],
    )
    assert receipt["executed"] is True
    assert "NO_SECRET_VISIBLE" in receipt["summary"]
    assert secret not in json.dumps(receipt)
    assert receipt["sandbox"]["network"] == "enabled"


def test_secret_ref_without_network_still_delivers_the_secret(app, client, monkeypatch):
    """The inverse of the above: secret_refs alone (no network) must still work -- proving
    network is not a prerequisite for authorized secret access, only an independent grant."""
    login(client, "admin")
    conversation = create_conversation(client)
    secret = "XV12_OFFLINE_SECRET_VALUE"
    monkeypatch.setenv("XV12_TEST_OFFLINE_TOKEN", secret)
    call(client, "secrets.reference.configure", name="offline_token", environment_name="XV12_TEST_OFFLINE_TOKEN", contexts=["builder"])
    workspace = call(client, "builder.workspace.create", name="Offline secret delivery")["workspace"]

    receipt = call(
        client, "builder.sandbox.exec", workspace_id=workspace["id"],
        argv=["sh", "-lc", "test \"$OFFLINE_TOKEN\" = \"$1\" && echo SECRET_DELIVERED", "_", secret],
        secret_refs=["offline_token"], network=False, timeout_seconds=30, report_type="test_report",
        conversation_id=conversation["id"],
    )
    assert receipt["executed"] is True
    assert "SECRET_DELIVERED" in receipt["summary"]
    assert receipt["sandbox"]["network"] == "disabled"
    assert secret not in json.dumps(receipt)


def test_unauthorized_secret_reference_is_denied_before_execution(app, client, monkeypatch):
    """A secret reference not authorized for the 'builder' context (or nonexistent) must
    raise, not silently execute with the requested name absent from the environment."""
    login(client, "admin")
    conversation = create_conversation(client)
    workspace = call(client, "builder.workspace.create", name="Unauthorized secret")["workspace"]

    response = client.post(
        "/api/capabilities/builder.sandbox.exec",
        json={"arguments": {
            "workspace_id": workspace["id"], "argv": ["sh", "-lc", "echo unreachable"],
            "secret_refs": ["never-configured-reference"], "timeout_seconds": 10,
            "report_type": "test_report", "conversation_id": conversation["id"],
        }},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] in {"invalid_arguments", "execution_error"}


def test_normal_user_cannot_execute_secret_reference_configuration(app, client):
    """secrets.reference.configure is a tier-2 administrator-only mutation; a normal user
    must not be able to bind an environment variable as an authorized builder secret."""
    login(client, "user-a")
    response = client.post(
        "/api/capabilities/secrets.reference.configure",
        json={"arguments": {"name": "attempted", "environment_name": "PATH", "contexts": ["builder"]}},
    )
    assert response.status_code == 403
