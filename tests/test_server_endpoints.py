from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from server import app, extract_passthrough_headers


@pytest.fixture
def client():
    return TestClient(app)


def test_mig_capabilities_includes_kepler(client):
    response = client.get("/v1/mig/capabilities")
    assert response.status_code == 200
    data = response.json()
    assert data["contract_version"] == "v1"
    kepler = next(p for p in data["providers"] if p["provider_id"] == "kepler")
    assert kepler["display_name"] == "Kepler"
    assert kepler["write_enabled"] is False
    assert sorted(kepler["capabilities"]) == sorted(
        ["read_context", "propose_plan", "explain_evidence", "emit_confidence"]
    )


def test_mig_provider_plan_flow(client):
    evaluate_payload = {
        "scope": "system:all",
        "question": "Map high-risk services and propose guarded remediation plan",
        "trigger": {"type": "dkm_hotspot", "severity": "high"},
        "context": {"tenant_id": "t-123", "service": "checkout"},
    }
    evaluate_response = client.post(
        "/v1/mig/providers/kepler/evaluate", json=evaluate_payload
    )
    assert evaluate_response.status_code == 200
    evaluate_data = evaluate_response.json()
    plan_id = evaluate_data["plan"]["plan_id"]
    assert evaluate_data["provider_id"] == "kepler"
    assert evaluate_data["plan"]["provider_id"] == "kepler"
    assert evaluate_data["plan"]["risk_level"] == "high"

    get_plan_response = client.get(f"/v1/mig/providers/kepler/plans/{plan_id}")
    assert get_plan_response.status_code == 200
    plan_data = get_plan_response.json()
    assert plan_data["plan_id"] == plan_id
    assert len(plan_data["actions"]) >= 1

    explain_response = client.post(
        f"/v1/mig/providers/kepler/plans/{plan_id}/explain",
        json={"focus": "policy_gating", "max_evidence": 2},
    )
    assert explain_response.status_code == 200
    explain_data = explain_response.json()
    assert explain_data["plan_id"] == plan_id
    assert explain_data["provider_id"] == "kepler"
    assert len(explain_data["evidence"]) <= 2


def test_mig_conformance_run(client):
    response = client.post(
        "/v1/mig/conformance/run", json={"provider_id": "kepler", "version": "v1"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["provider_id"] == "kepler"
    assert data["version"] == "v1"
    assert data["passed"] is True
    check_ids = [check["id"] for check in data["checks"]]
    assert "endpoint.evaluate" in check_ids
    assert "policy.read_propose_only" in check_ids


def test_agent_evaluation_lifecycle(client):
    create_response = client.post(
        "/v1/agent/evaluations",
        json={
            "provider_id": "kepler",
            "scope": "system:all",
            "question": "Generate plan for checkout service",
            "trigger": {"type": "slo_burn", "severity": "medium"},
            "context": {"service": "checkout"},
        },
    )
    assert create_response.status_code == 200
    create_data = create_response.json()
    evaluation_id = create_data["id"]
    plan_id = create_data["plan_id"]
    assert create_data["status"] == "proposed"

    simulate_response = client.post(
        f"/v1/agent/evaluations/{evaluation_id}/simulate",
        json={"freeze_window_active": False, "stale_evidence_after_seconds": 3600},
    )
    assert simulate_response.status_code == 200
    simulate_data = simulate_response.json()
    assert simulate_data["status"] == "simulated"
    assert simulate_data["allowed"] is True

    approve_response = client.post(
        f"/v1/agent/plans/{plan_id}/approve", json={"reason": "Approved in UI"}
    )
    assert approve_response.status_code == 200
    approve_data = approve_response.json()
    assert approve_data["status"] == "approved"

    execute_response = client.post(
        f"/v1/agent/plans/{plan_id}/execute",
        json={"dry_run": True, "reason": "Validate policy first"},
    )
    assert execute_response.status_code == 200
    execute_data = execute_response.json()
    assert execute_data["status"] == "executed"

    timeline_response = client.get(f"/v1/agent/plans/{plan_id}/timeline")
    assert timeline_response.status_code == 200
    timeline_data = timeline_response.json()
    event_names = [event["event"] for event in timeline_data["events"]]
    assert "agent.kepler.plan.generated" in event_names
    assert "agent.kepler.plan.simulated" in event_names
    assert "agent.kepler.plan.approved" in event_names
    assert "agent.kepler.plan.executed" in event_names
    assert "agent.kepler.plan.verified" in event_names


def test_agent_execute_blocked_without_approval(client):
    create_response = client.post(
        "/v1/agent/evaluations",
        json={"provider_id": "kepler", "scope": "system:all"},
    )
    assert create_response.status_code == 200
    plan_id = create_response.json()["plan_id"]

    execute_response = client.post(f"/v1/agent/plans/{plan_id}/execute", json={})
    assert execute_response.status_code == 409
    assert "must be approved" in execute_response.json()["detail"]


@patch("holmes.config.Config.create_toolcalling_llm")
@patch("holmes.core.supabase_dal.SupabaseDal.get_global_instructions_for_account")
def test_api_chat_all_fields(
    mock_get_global_instructions,
    mock_create_toolcalling_llm,
    client,
):
    mock_ai = MagicMock()
    mock_ai.messages_call.return_value = MagicMock(
        result="This is a mock analysis with tools and follow-up actions.",
        tool_calls=[
            {
                "tool_call_id": "1",
                "tool_name": "log_fetcher",
                "description": "Fetches logs",
                "result": {"status": "success", "data": "Log data"},
            }
        ],
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What can you do?"},
        ],
        metadata={},
    )
    mock_create_toolcalling_llm.return_value = mock_ai

    mock_get_global_instructions.return_value = []

    payload = {
        "ask": "What can you do?",
        "conversation_history": [
            {"role": "system", "content": "You are a helpful assistant."}
        ],
        "model": "gpt-4.1",
    }
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert "analysis" in data
    assert "conversation_history" in data
    assert "tool_calls" in data
    assert "follow_up_actions" in data

    assert isinstance(data["analysis"], str)
    assert isinstance(data["conversation_history"], list)
    assert isinstance(data["tool_calls"], list)
    assert isinstance(data["follow_up_actions"], list)

    assert any(msg.get("role") == "user" for msg in data["conversation_history"])

    if data["tool_calls"]:
        tool_call = data["tool_calls"][0]
        assert "tool_call_id" in tool_call
        assert "tool_name" in tool_call
        assert "description" in tool_call
        assert "result" in tool_call

    if data["follow_up_actions"]:
        action = data["follow_up_actions"][0]
        assert "id" in action
        assert "action_label" in action
        assert "prompt" in action
        assert "pre_action_notification_text" in action


@patch("holmes.config.Config.create_toolcalling_llm")
@patch("holmes.core.supabase_dal.SupabaseDal.get_global_instructions_for_account")
def test_api_chat_with_images(
    mock_get_global_instructions,
    mock_create_toolcalling_llm,
    client,
):
    """Test /api/chat endpoint with image analysis support."""
    mock_ai = MagicMock()

    # Capture the messages passed to the LLM
    captured_messages = []

    def capture_messages(messages, **kwargs):
        captured_messages.append(messages)
        return MagicMock(
            result="This is an analysis of the provided image.",
            tool_calls=[],
            messages=messages,
            metadata={},
        )

    mock_ai.messages_call.side_effect = capture_messages
    mock_create_toolcalling_llm.return_value = mock_ai
    mock_get_global_instructions.return_value = []

    payload = {
        "ask": "What's in this image?",
        "conversation_history": [
            {"role": "system", "content": "You are a helpful assistant."}
        ],
        "model": "gpt-4-vision-preview",
        "images": [
            "https://example.com/image1.png",
            "https://example.com/image2.jpg",
        ],
    }
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Verify response structure
    assert "analysis" in data
    assert "conversation_history" in data
    assert "tool_calls" in data
    assert "follow_up_actions" in data

    # Verify the messages were captured
    assert len(captured_messages) == 1
    messages = captured_messages[0]

    # Find the user message with images
    user_message = next((m for m in messages if m["role"] == "user"), None)
    assert user_message is not None

    # Verify the content is an array with text and images
    content = user_message["content"]
    assert isinstance(content, list)
    assert len(content) == 3  # 1 text + 2 images

    # Verify text content
    text_item = content[0]
    assert text_item["type"] == "text"
    assert "What's in this image?" in text_item["text"]

    # Verify image contents
    image_items = content[1:]
    assert len(image_items) == 2
    for i, image_item in enumerate(image_items):
        assert image_item["type"] == "image_url"
        assert "image_url" in image_item
        assert image_item["image_url"]["url"] == payload["images"][i]


@patch("holmes.config.Config.create_toolcalling_llm")
@patch("holmes.core.supabase_dal.SupabaseDal.get_global_instructions_for_account")
def test_api_chat_with_images_advanced_format(
    mock_get_global_instructions,
    mock_create_toolcalling_llm,
    client,
):
    """Test /api/chat endpoint with advanced image format (dict with detail and format)."""
    mock_ai = MagicMock()

    # Capture the messages passed to the LLM
    captured_messages = []

    def capture_messages(messages, **kwargs):
        captured_messages.append(messages)
        return MagicMock(
            result="Detailed analysis of high-resolution image.",
            tool_calls=[],
            messages=messages,
            metadata={},
        )

    mock_ai.messages_call.side_effect = capture_messages
    mock_create_toolcalling_llm.return_value = mock_ai
    mock_get_global_instructions.return_value = []

    payload = {
        "ask": "Analyze this screenshot in detail",
        "conversation_history": [
            {"role": "system", "content": "You are a helpful assistant."}
        ],
        "model": "gpt-4o",
        "images": [
            # Mix of simple strings and advanced dict format
            "https://example.com/simple-url.png",
            {
                "url": "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
                "detail": "high",
            },
            {
                "url": "https://example.com/image-with-format.webp",
                "detail": "low",
                "format": "image/webp",
            },
        ],
    }
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Verify response structure
    assert "analysis" in data
    assert "conversation_history" in data

    # Verify the messages were captured
    assert len(captured_messages) == 1
    messages = captured_messages[0]

    # Find the user message with images
    user_message = next((m for m in messages if m["role"] == "user"), None)
    assert user_message is not None

    # Verify the content is an array
    content = user_message["content"]
    assert isinstance(content, list)
    assert len(content) == 4  # 1 text + 3 images

    # Verify text content
    text_item = content[0]
    assert text_item["type"] == "text"
    assert "Analyze this screenshot in detail" in text_item["text"]

    # Verify first image (simple string URL)
    image1 = content[1]
    assert image1["type"] == "image_url"
    assert image1["image_url"]["url"] == "https://example.com/simple-url.png"
    assert "detail" not in image1["image_url"]
    assert "format" not in image1["image_url"]

    # Verify second image (base64 with detail)
    image2 = content[2]
    assert image2["type"] == "image_url"
    assert image2["image_url"]["url"] == "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
    assert image2["image_url"]["detail"] == "high"
    assert "format" not in image2["image_url"]

    # Verify third image (URL with detail and format)
    image3 = content[3]
    assert image3["type"] == "image_url"
    assert image3["image_url"]["url"] == "https://example.com/image-with-format.webp"
    assert image3["image_url"]["detail"] == "low"
    assert image3["image_url"]["format"] == "image/webp"


@patch("holmes.config.Config.create_toolcalling_llm")
@patch("holmes.core.supabase_dal.SupabaseDal.get_global_instructions_for_account")
def test_api_chat_with_images_missing_url_key(
    mock_get_global_instructions,
    mock_create_toolcalling_llm,
    client,
):
    """Test /api/chat endpoint raises error when image dict missing 'url' key."""
    mock_ai = MagicMock()
    mock_ai.messages_call.return_value = MagicMock(
        result="This should not be reached.",
        tool_calls=[],
        messages=[],
        metadata={},
    )
    mock_create_toolcalling_llm.return_value = mock_ai
    mock_get_global_instructions.return_value = []

    payload = {
        "ask": "Analyze this",
        "conversation_history": [
            {"role": "system", "content": "You are a helpful assistant."}
        ],
        "model": "gpt-4o",
        "images": [
            # Dict missing required "url" key
            {"detail": "high", "format": "image/jpeg"}
        ],
    }
    response = client.post("/api/chat", json=payload)

    # Should return 500 error with clear message
    assert response.status_code == 500
    data = response.json()
    assert "Image dict must contain a 'url' key" in data["detail"]


@patch("holmes.config.Config.create_toolcalling_llm")
@patch("holmes.core.supabase_dal.SupabaseDal.get_global_instructions_for_account")
def test_api_issue_chat_all_fields(
    mock_get_global_instructions,
    mock_create_toolcalling_llm,
    client,
):
    mock_ai = MagicMock()
    mock_ai.messages_call.return_value = MagicMock(
        result="This is a mock analysis for issue chat.",
        tool_calls=[
            {
                "tool_call_id": "1",
                "tool_name": "issue_resolver",
                "description": "Resolves issues",
                "result": {"status": "success", "data": "Issue resolved"},
            }
        ],
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "I have an issue with my deployment."},
        ],
        metadata={},
    )
    mock_create_toolcalling_llm.return_value = mock_ai

    mock_get_global_instructions.return_value = []

    payload = {
        "ask": "What can you do?",
        "investigation_result": {"result": "Mock investigation result", "tools": []},
        "issue_type": "deployment",
        "conversation_history": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "I have an issue with my deployment."},
        ],
    }
    response = client.post("/api/issue_chat", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert "analysis" in data
    assert "conversation_history" in data
    assert "tool_calls" in data

    assert isinstance(data["analysis"], str)
    assert isinstance(data["conversation_history"], list)
    assert isinstance(data["tool_calls"], list)

    assert any(msg.get("role") == "user" for msg in data["conversation_history"])

    if data["tool_calls"]:
        tool_call = data["tool_calls"][0]
        assert "tool_call_id" in tool_call
        assert "tool_name" in tool_call
        assert "description" in tool_call
        assert "result" in tool_call


class TestExtractPassthroughHeaders:
    def test_extract_normal_headers(self):
        scope = {
            "type": "http",
            "headers": [
                (b"x-tenant-id", b"tenant-123"),
                (b"x-custom-header", b"custom-value"),
                (b"content-type", b"application/json"),
            ],
        }
        request = Request(scope)
        result = extract_passthrough_headers(request)

        assert result == {
            "headers": {
                "x-tenant-id": "tenant-123",
                "x-custom-header": "custom-value",
                "content-type": "application/json",
            }
        }

    def test_blocks_authorization_header(self):
        scope = {
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer secret-token"),
                (b"x-tenant-id", b"tenant-123"),
            ],
        }
        request = Request(scope)
        result = extract_passthrough_headers(request)

        assert result == {"headers": {"x-tenant-id": "tenant-123"}}
        assert "authorization" not in result["headers"]

    def test_blocks_cookie_headers(self):
        scope = {
            "type": "http",
            "headers": [
                (b"cookie", b"session=abc123"),
                (b"set-cookie", b"session=abc123; Path=/"),
                (b"x-tenant-id", b"tenant-123"),
            ],
        }
        request = Request(scope)
        result = extract_passthrough_headers(request)

        assert result == {"headers": {"x-tenant-id": "tenant-123"}}
        assert "cookie" not in result["headers"]
        assert "set-cookie" not in result["headers"]

    def test_case_insensitive_blocking(self):
        scope = {
            "type": "http",
            "headers": [
                (b"Authorization", b"Bearer secret"),
                (b"COOKIE", b"session=abc"),
                (b"Set-Cookie", b"session=abc; Path=/"),
                (b"x-tenant-id", b"tenant-123"),
            ],
        }
        request = Request(scope)
        result = extract_passthrough_headers(request)

        assert result == {"headers": {"x-tenant-id": "tenant-123"}}
        assert "Authorization" not in result["headers"]
        assert "COOKIE" not in result["headers"]
        assert "Set-Cookie" not in result["headers"]

    def test_empty_headers(self):
        scope = {"type": "http", "headers": []}
        request = Request(scope)
        result = extract_passthrough_headers(request)

        assert result == {}

    def test_all_blocked_headers(self):
        scope = {
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer secret"),
                (b"cookie", b"session=abc"),
                (b"set-cookie", b"session=abc; Path=/"),
            ],
        }
        request = Request(scope)
        result = extract_passthrough_headers(request)

        assert result == {}

    def test_preserves_header_case(self):
        scope = {
            "type": "http",
            "headers": [
                (b"X-Tenant-ID", b"tenant-123"),
                (b"X-Custom-Header", b"value"),
            ],
        }
        request = Request(scope)
        result = extract_passthrough_headers(request)

        assert "X-Tenant-ID" in result["headers"]
        assert "X-Custom-Header" in result["headers"]
        assert result["headers"]["X-Tenant-ID"] == "tenant-123"
        assert result["headers"]["X-Custom-Header"] == "value"

    def test_custom_blocked_headers_via_env(self, monkeypatch):
        """Test that HOLMES_PASSTHROUGH_BLOCKED_HEADERS env var works"""
        # Set custom blocked headers via environment variable
        monkeypatch.setenv("HOLMES_PASSTHROUGH_BLOCKED_HEADERS", "x-internal-token,x-secret")

        scope = {
            "type": "http",
            "headers": [
                (b"x-internal-token", b"secret-value"),
                (b"x-secret", b"another-secret"),
                (b"authorization", b"Bearer token"),  # Not in custom list, should pass
                (b"x-tenant-id", b"tenant-123"),
            ],
        }
        request = Request(scope)
        result = extract_passthrough_headers(request)

        # Custom blocked headers should be filtered
        assert "x-internal-token" not in result["headers"]
        assert "x-secret" not in result["headers"]
        # Authorization is not in custom list, so it should pass through
        assert "authorization" in result["headers"]
        assert result["headers"]["authorization"] == "Bearer token"
        # Regular headers should pass
        assert result["headers"]["x-tenant-id"] == "tenant-123"

    def test_empty_blocked_headers_env(self, monkeypatch):
        """Test that empty HOLMES_PASSTHROUGH_BLOCKED_HEADERS allows all headers"""
        monkeypatch.setenv("HOLMES_PASSTHROUGH_BLOCKED_HEADERS", "")

        scope = {
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer token"),
                (b"cookie", b"session=abc"),
                (b"x-tenant-id", b"tenant-123"),
            ],
        }
        request = Request(scope)
        result = extract_passthrough_headers(request)

        # With empty blocklist, all headers should pass through
        assert "authorization" in result["headers"]
        assert "cookie" in result["headers"]
        assert "x-tenant-id" in result["headers"]
