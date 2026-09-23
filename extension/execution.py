from __future__ import annotations

import hashlib
import io
import logging
import os
import traceback
import uuid
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, NotRequired, TypedDict

import bpy

from . import recovery
from .connection import config_directory
from .context import current_scene
from .recovery import Checkpoint, CheckpointList, Checkpoints, ObjectChange

logger = logging.getLogger(__name__)


class ExecutionResult(TypedDict):
    started: bool
    succeeded: bool
    partial_changes: bool | None
    result: str
    error_message: NotRequired[str]
    stderr: NotRequired[str]
    output_truncated: NotRequired[bool]
    execution_id: NotRequired[str]
    namespace: NotRequired[str | None]
    code_sha256: NotRequired[str]
    checkpoint: NotRequired[Checkpoint]
    changes: NotRequired[dict[str, object]]
    summary_error: NotRequired[str]


class BoundedOutput(io.TextIOBase):
    limit = 16_384

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self.remaining = self.limit
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        retained = text[: self.remaining]
        self._buffer.write(retained)
        self.remaining -= len(retained)
        self.truncated |= len(retained) < len(text)
        return len(text)

    def getvalue(self) -> str:
        return self._buffer.getvalue()


class ExecutionSession:
    result_limit = 8

    def __init__(
        self,
        config_dir: str | os.PathLike[str] | None = None,
        running: Callable[[], bool] = lambda: False,
    ) -> None:
        self.config_dir = config_dir
        self._running = running
        self._execution_namespaces: dict[str, dict[str, Any]] = {}
        self._execution_results: dict[str, ExecutionResult] = {}
        self._execution_changes: dict[str, dict[str, list[ObjectChange]]] = {}
        self._checkpoints: Checkpoints | None = None

    def close(self) -> None:
        self.clear_namespaces()
        self._execution_results.clear()
        self._execution_changes.clear()

    def clear_namespaces(self) -> None:
        self._execution_namespaces.clear()

    def _checkpoint_store(self) -> Checkpoints:
        if self._checkpoints is None:
            self._checkpoints = Checkpoints(
                config_directory(self.config_dir) / "checkpoints"
            )
        return self._checkpoints

    def create_checkpoint(self, label: str | None = None) -> Checkpoint:
        return self._checkpoint_store().create(label)

    def list_checkpoints(self) -> CheckpointList:
        return self._checkpoint_store().list()

    def restore_checkpoint(self, checkpoint_id: str) -> dict[str, object]:
        result = self._checkpoint_store().restore(checkpoint_id, self.clear_namespaces)
        current_scene().blendermcp_server_running = self._running()
        return result

    def delete_checkpoint(self, checkpoint_id: str) -> dict[str, object]:
        return self._checkpoint_store().delete(checkpoint_id)

    def get_execution_result(self, execution_id: str | None = None) -> ExecutionResult:
        if execution_id is None:
            execution_id = next(reversed(self._execution_results), None)
        if (
            not isinstance(execution_id, str)
            or execution_id not in self._execution_results
        ):
            raise ValueError(
                f"Execution result not found; only the last {self.result_limit} opted-in calls in this server session are retained"
            )
        return self._execution_results[execution_id]

    def get_execution_changes(
        self, execution_id: str, category: str, offset: int = 0, limit: int = 100
    ) -> dict[str, object]:
        from .recovery import SUMMARY_SCOPE, change_page

        self.get_execution_result(execution_id)
        if execution_id not in self._execution_changes:
            raise ValueError("No change summary is available for this execution")
        return {
            "execution_id": execution_id,
            "category": category,
            "scope": SUMMARY_SCOPE,
            "changes": change_page(
                self._execution_changes[execution_id], category, offset, limit
            ),
        }

    def execute_code(
        self,
        code: str,
        namespace: str | None = None,
        reset_namespace: bool = False,
        checkpoint: bool = False,
        summarize_changes: bool = False,
    ) -> ExecutionResult:
        """Execute arbitrary Blender Python code"""
        if not isinstance(code, str):
            raise TypeError("code must be a string")
        if type(reset_namespace) is not bool:
            raise ValueError("reset_namespace must be a boolean")
        if namespace is not None and (
            not isinstance(namespace, str) or not 1 <= len(namespace) <= 128
        ):
            raise ValueError(
                "Code execution error: namespace must be a string of 1 to 128 characters"
            )
        if reset_namespace and namespace is None:
            raise ValueError(
                "Code execution error: reset_namespace requires a namespace"
            )
        if type(checkpoint) is not bool or type(summarize_changes) is not bool:
            raise ValueError("checkpoint and summarize_changes must be booleans")
        retain_result = checkpoint or summarize_changes
        execution_id = code_sha256 = ""
        if retain_result:
            execution_id = uuid.uuid4().hex
            code_sha256 = hashlib.sha256(code.encode()).hexdigest()
        before = saved = None
        started = False
        stdout = BoundedOutput()
        stderr = BoundedOutput()
        result: ExecutionResult
        try:
            compiled = compile(code, "<blender-mcp>", "exec")
            before = recovery.capture_objects() if summarize_changes else None
            saved = (
                self.create_checkpoint(label=f"Before execution {execution_id}")
                if checkpoint
                else None
            )
            if namespace is None:
                execution_namespace: dict[str, Any] = {"bpy": bpy}
            else:
                if reset_namespace:
                    self._execution_namespaces.pop(namespace, None)
                execution_namespace = self._execution_namespaces.setdefault(
                    namespace, {"bpy": bpy}
                )
            with redirect_stdout(stdout), redirect_stderr(stderr):
                started = True
                exec(compiled, execution_namespace)  # noqa: S102

            result = {
                "started": True,
                "succeeded": True,
                "partial_changes": False,
                "result": stdout.getvalue(),
            }
        except (Exception, SystemExit, KeyboardInterrupt) as e:
            logger.exception("Error executing Blender Python code")
            diagnostic = BoundedOutput()
            diagnostic.write(f"{type(e).__name__}\n")
            traceback.print_exception(
                type(e), e, e.__traceback__, limit=-8, file=diagnostic, chain=False
            )
            message = f"Code execution error: {str(e)[:2048]}\n{diagnostic.getvalue()}"
            if diagnostic.truncated:
                message += "\n[Traceback truncated]"
            for label, output in (("stdout", stdout), ("stderr", stderr)):
                if output.getvalue():
                    message += f"\n{label}:\n{output.getvalue()}"
                if output.truncated:
                    message += f"\n[{label} truncated after {output.limit} characters]"
            message += (
                "\nChanges made before the error were not rolled back."
                if started
                else "\nCode did not start."
            )
            result = {
                "started": started,
                "succeeded": False,
                "partial_changes": None if started else False,
                "result": stdout.getvalue(),
                "error_message": message,
            }
        if stderr.getvalue():
            result["stderr"] = stderr.getvalue()
        if stdout.truncated or stderr.truncated:
            result["output_truncated"] = True
        if retain_result:
            result["execution_id"] = execution_id
            result["namespace"] = namespace
            result["code_sha256"] = code_sha256
            if saved is not None:
                result["checkpoint"] = saved
            if summarize_changes and started and before is not None:
                try:
                    changes = recovery.compare_objects(
                        before, recovery.capture_objects()
                    )
                    self._execution_changes[execution_id] = changes
                    result["changes"] = recovery.summarize_changes(changes)
                    if not result["succeeded"] and any(changes.values()):
                        result["partial_changes"] = True
                except Exception as error:
                    logger.exception("Error summarizing execution changes")
                    result["summary_error"] = str(error)[:2048]
            self._execution_results[execution_id] = result
            while len(self._execution_results) > self.result_limit:
                oldest = next(iter(self._execution_results))
                del self._execution_results[oldest]
                self._execution_changes.pop(oldest, None)
        return result
