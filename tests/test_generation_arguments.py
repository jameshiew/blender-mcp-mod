import json

import pytest
from test_rust_server import Client


@pytest.fixture
def client(binary, tmp_path):
    connection = Client(binary, tmp_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (
            "poll_rodin_job_status",
            {"subscription_key": "valid", "request_id": ""},
            {"subscription_key": "valid"},
        ),
        (
            "poll_rodin_job_status",
            {"subscription_key": "  ", "request_id": "valid"},
            {"request_id": "valid"},
        ),
        (
            "import_generated_asset",
            {"name": "Chair", "task_uuid": "valid", "request_id": ""},
            {"name": "Chair", "task_uuid": "valid"},
        ),
        (
            "import_generated_asset",
            {"name": "Chair", "task_uuid": "  ", "request_id": "valid"},
            {"name": "Chair", "request_id": "valid"},
        ),
    ],
)
def test_unused_identifiers_are_not_forwarded(client, tool, arguments, expected):
    result = client.call(tool, arguments)

    assert not result.get("isError"), result
    assert client.commands[-1]["params"] == expected


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("generate_hunyuan3d_model", {"text_prompt": "Chair"}),
        ("poll_hunyuan_job_status", {"job_id": "job_123"}),
    ],
)
def test_hunyuan_provider_errors_are_tool_errors(client, tool, arguments):
    respond = client.respond
    client.respond = lambda command: {
        "status": "success",
        "result": {
            "Response": {
                "Error": {"Code": "AuthFailure", "Message": "Invalid credentials"},
                "RequestId": "request-123",
            }
        },
    }

    result = client.call(tool, arguments)

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "AuthFailure" in text
    assert "Invalid credentials" in text
    assert "request-123" in text
    client.respond = respond
    result = client.call("generate_hunyuan3d_model", {"text_prompt": "Chair"})
    assert not result.get("isError"), result
    assert json.loads(result["content"][0]["text"]) == {"job_id": "job_123"}
