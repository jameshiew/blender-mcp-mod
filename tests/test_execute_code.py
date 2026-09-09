import pytest

from test_server_threading import BlenderMCPServer


def execute(server, code, namespace="task-a", **options):
    return server.execute_command(
        {
            "type": "execute_code",
            "params": {"code": code, "namespace": namespace, **options},
        }
    )


def test_imports_and_helpers_persist_across_commands():
    server = BlenderMCPServer()
    first = execute(
        server,
        "import math\nscale = 2\ndef measure(value):\n    return math.sqrt(value) * scale\nprint('defined')",
    )
    assert first == {
        "status": "success",
        "result": {"executed": True, "result": "defined\n"},
    }
    second = execute(server, "scale = 3\nprint(measure(16))\nprint(bpy is not None)")
    assert second == {
        "status": "success",
        "result": {"executed": True, "result": "12.0\nTrue\n"},
    }


def test_error_preserves_existing_and_partially_executed_state():
    server = BlenderMCPServer()
    execute(server, "values = [1]")
    result = execute(
        server,
        "import sys\nvalues.append(2)\nprint('before failure')\n"
        "print('warning before failure', file=sys.stderr)\nraise ValueError('failed')",
    )
    assert result["status"] == "error"
    message = result["message"]
    assert message.startswith("Code execution error: failed\n")
    assert "ValueError: failed" in message
    assert 'File "<blender-mcp>", line 5' in message
    assert "stdout:\nbefore failure\n" in message
    assert "stderr:\nwarning before failure\n" in message
    assert "not rolled back" in message
    assert execute(server, "print(values)")["result"]["result"] == "[1, 2]\n"


def test_success_captures_stderr_separately():
    result = execute(
        BlenderMCPServer(),
        "import sys\nprint('result')\nprint('warning', file=sys.stderr)",
    )
    assert result == {
        "status": "success",
        "result": {"executed": True, "result": "result\n", "stderr": "warning\n"},
    }


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_is_bounded_during_write_and_writelines(stream):
    result = execute(
        BlenderMCPServer(),
        f"import sys\noutput = sys.{stream}\n"
        "assert output.write('x' * 1_000_000) == 1_000_000\n"
        "assert len(output.getvalue()) == 16384\n"
        "output.writelines(['y' * 1000] * 1000)\n"
        "assert len(output.getvalue()) == 16384",
    )
    assert result["status"] == "success"
    assert result["result"]["output_truncated"] is True
    assert result["result"]["stderr" if stream == "stderr" else "result"] == (
        "x" * 16_384
    )


def test_exact_output_limit_is_not_reported_as_truncated():
    result = execute(BlenderMCPServer(), "print('x' * 16384, end='')")
    assert result == {
        "status": "success",
        "result": {"executed": True, "result": "x" * 16_384},
    }


def test_failure_includes_bounded_stdout_and_stderr():
    result = execute(
        BlenderMCPServer(),
        "import sys\nprint('x' * 1_000_000)\n"
        "print('y' * 1_000_000, file=sys.stderr)\nraise RuntimeError('failed')",
    )
    assert result["status"] == "error"
    message = result["message"]
    assert 'File "<blender-mcp>", line 4' in message
    assert "RuntimeError: failed" in message
    assert f"stdout:\n{'x' * 16_384}\n[stdout truncated" in message
    assert f"stderr:\n{'y' * 16_384}\n[stderr truncated" in message
    assert len(message) < 50_000


def test_large_exception_message_is_bounded():
    result = execute(BlenderMCPServer(), "raise ValueError('x' * 1_000_000)")
    assert result["status"] == "error"
    assert "ValueError" in result["message"]
    assert "[Traceback truncated]" in result["message"]
    assert len(result["message"]) < 20_000


def test_syntax_error_reports_script_line_without_executing_code():
    server = BlenderMCPServer()
    execute(server, "value = 7")
    result = execute(server, "value = 9\nif True\n    print(value)")
    assert result["status"] == "error"
    message = result["message"]
    assert message.startswith("Code execution error: ")
    assert "SyntaxError" in message
    assert 'File "<blender-mcp>", line 2' in message
    assert "if True" in message
    assert execute(server, "print(value)")["result"]["result"] == "7\n"


def test_new_server_has_an_independent_namespace():
    first = BlenderMCPServer()
    second = BlenderMCPServer()
    first.execute_code("value = 42", namespace="task-a")
    with pytest.raises(Exception, match="name 'value' is not defined"):
        second.execute_code("print(value)", namespace="task-a")
    assert first.execute_code("print(value)", namespace="task-a")["result"] == "42\n"


def test_deep_traceback_keeps_origin_from_an_earlier_namespace_command():
    server = BlenderMCPServer()
    execute(
        server,
        "def leaf():\n    raise ValueError('deep failure')\ndef hop(depth):\n    if depth:\n        return hop(depth - 1)\n    return leaf()",
    )
    result = execute(server, "hop(20)")
    assert result["status"] == "error"
    assert 'File "<blender-mcp>", line 2, in leaf' in result["message"]
    assert "ValueError: deep failure" in result["message"]


def test_unnamed_calls_use_fresh_globals():
    server = BlenderMCPServer()
    server.execute_code("value = 42")
    with pytest.raises(Exception, match="name 'value' is not defined"):
        server.execute_code("print(value)")
    execute(server, "named_value = 7")
    assert (
        server.execute_code("print('named_value' in globals())")["result"] == "False\n"
    )


def test_interleaved_namespaces_keep_separate_globals():
    server = BlenderMCPServer()
    execute(server, "value = 2\ndef helper():\n    return value", namespace="task-a")
    execute(server, "value = 9\ndef helper():\n    return value", namespace="task-b")
    assert (
        execute(server, "print(helper())", namespace="task-a")["result"]["result"]
        == "2\n"
    )
    assert (
        execute(server, "print(helper())", namespace="task-b")["result"]["result"]
        == "9\n"
    )


def test_reset_clears_only_the_selected_namespace():
    server = BlenderMCPServer()
    execute(server, "value = 2", namespace="task-a")
    execute(server, "value = 9", namespace="task-b")
    result = execute(
        server,
        "print('value' in globals())\nprint(bpy is not None)\nvalue = 3",
        reset_namespace=True,
    )
    assert result["result"]["result"] == "False\nTrue\n"
    assert execute(server, "print(value)")["result"]["result"] == "3\n"
    assert (
        execute(server, "print(value)", namespace="task-b")["result"]["result"] == "9\n"
    )
    execute(server, "", reset_namespace=True)
    assert (
        execute(server, "print('value' in globals())")["result"]["result"] == "False\n"
    )


@pytest.mark.parametrize("namespace", ["", "a" * 129, 42, []])
def test_invalid_namespace_is_rejected_before_execution(namespace):
    server = BlenderMCPServer()
    result = execute(server, "raise AssertionError('executed')", namespace=namespace)
    assert result == {
        "status": "error",
        "message": "Code execution error: namespace must be a string of 1 to 128 characters",
    }


def test_reset_requires_a_namespace():
    result = execute(BlenderMCPServer(), "", namespace=None, reset_namespace=True)
    assert result == {
        "status": "error",
        "message": "Code execution error: reset_namespace requires a namespace",
    }
