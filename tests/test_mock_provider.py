from config import PROVIDERS

def test_mock_provider_registered():
    assert "mock" in PROVIDERS
    entry = PROVIDERS["mock"]
    assert entry["protocol"] in ("openai", "anthropic", "mock")
    assert entry["api_key_env"] == "MOCK_API_KEY"

def test_mock_provider_has_dummy_model():
    assert "mock-default" in PROVIDERS["mock"]["models"]
