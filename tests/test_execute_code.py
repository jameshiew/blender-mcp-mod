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
    result = execute(server, "values.append(2)\nraise ValueError('failed')")
    assert result == {
        "status": "error",
        "message": "Code execution error: failed",
    }
    assert execute(server, "print(values)")["result"]["result"] == "[1, 2]\n"


def test_new_server_has_an_independent_namespace():
    first = BlenderMCPServer()
    second = BlenderMCPServer()
    first.execute_code("value = 42", namespace="task-a")
    with pytest.raises(Exception, match="name 'value' is not defined"):
        second.execute_code("print(value)", namespace="task-a")
    assert first.execute_code("print(value)", namespace="task-a")["result"] == "42\n"


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
