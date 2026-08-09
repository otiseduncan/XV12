from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT
from app.main import create_app
from .conftest import create_conversation, login, make_settings


@pytest.mark.voice_output
@pytest.mark.memory_isolation
def test_voice_defaults_volume_mute_restoration_and_user_isolation(app):
    with TestClient(app) as user_a, TestClient(app) as user_b:
        login(user_a, "user-a")
        default = user_a.get("/api/settings/voice").json()
        assert default == {
            "voice_name": "Google US English",
            "voice_volume": 75,
            "voice_muted": False,
            "updated_at": None,
            "preferred_voice": "Google US English",
        }

        selected = user_a.patch(
            "/api/settings/voice",
            json={"voice_name": "XV12 Test Alternate", "voice_volume": 65},
        ).json()
        assert selected["voice_name"] == "XV12 Test Alternate"
        assert selected["voice_volume"] == 65

        muted = user_a.patch("/api/settings/voice", json={"voice_muted": True}).json()
        assert muted["voice_muted"] is True and muted["voice_volume"] == 65
        unmuted = user_a.patch("/api/settings/voice", json={"voice_muted": False}).json()
        assert unmuted["voice_muted"] is False and unmuted["voice_volume"] == 65

        login(user_b, "user-b")
        assert user_b.get("/api/settings/voice").json()["voice_name"] == "Google US English"
        assert user_b.get("/api/settings/voice").json()["voice_volume"] == 75
        assert user_a.get("/api/settings/voice").json()["voice_name"] == "XV12 Test Alternate"


@pytest.mark.voice_output
def test_voice_selection_persists_across_xv12_restart(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as first_run:
        login(first_run)
        first_run.patch(
            "/api/settings/voice",
            json={"voice_name": "XV12 Test Alternate", "voice_volume": 25},
        )
    with TestClient(create_app(settings)) as restarted:
        login(restarted)
        persisted = restarted.get("/api/settings/voice").json()
        assert persisted["voice_name"] == "XV12 Test Alternate"
        assert persisted["voice_volume"] == 25


@pytest.mark.voice_output
@pytest.mark.capability_registry
def test_conversational_voice_capability_uses_authoritative_settings(client):
    login(client)
    registry = json.loads((ROOT / "config" / "capabilities.v1.json").read_text(encoding="utf-8"))
    voice_ids = {item["id"] for item in registry["capabilities"] if item["family"] == "settings"}
    assert voice_ids == {"settings.voice.read", "settings.voice.update"}

    result = client.post(
        "/api/capabilities/settings.voice.update",
        json={"arguments": {"voice_volume": 40, "voice_muted": True}},
    ).json()["result"]
    assert result["status"] == "success" and result["domain_status"] == "updated"
    assert result["settings"]["voice_volume"] == 40
    assert result["settings"]["voice_muted"] is True
    assert client.get("/api/settings/voice").json() == result["settings"]

    assert client.patch("/api/settings/voice", json={"voice_volume": 101}).status_code == 422


@pytest.mark.voice_output
@pytest.mark.ui_shell
def test_voice_output_ui_and_tts_failure_contract_are_permanent():
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="quick-mute"' in html
    assert "Google US English" in js and 'voice.name === requested' in js
    assert 'String(voice.lang).toLowerCase() === "en-us"' in js
    assert "isLikelyFemaleVoice" in js
    assert 'name: "Browser default en-US"' in js and "using its default en-US synthesis voice" in js
    assert 'utterance.volume = state.voiceSettings.voice_volume / 100' in js
    assert 'engine.addEventListener?.("voiceschanged", enumerateVoices)' in js
    assert 'speakX(assistant.text.textContent)' in js
    assert 'speechEngine()?.cancel?.()' in js
    assert "The text response is still available" in js
    assert 'get("voice_fail") === "1"' in js and "synthesis-failed" in js
    assert "Preview Voice" in js and 'id="voice-volume"' in js and 'id="voice-muted"' in js
    assert "Previewing ${state.effectiveVoice.name} at ${state.voiceSettings.voice_volume}%" in js
    speech_input = js.split("function setupSpeech()", 1)[1].split('$("#google-login")', 1)[0]
    assert "voice_muted" not in speech_input


@pytest.mark.voice_output
@pytest.mark.chat_core
def test_voice_settings_do_not_change_model_first_conversation(client, app):
    login(client)
    client.patch("/api/settings/voice", json={"voice_muted": True, "voice_volume": 25})
    conversation = create_conversation(client)
    body = client.post(
        f"/api/conversations/{conversation['id']}/stream",
        json={"message": "Conversation core proof", "attachment_ids": []},
    ).text
    assert "event: delta" in body and "event: done" in body
    assert len(app.state.model.requests) == 1
