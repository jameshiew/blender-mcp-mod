import os
import subprocess
from pathlib import Path

import pytest
from conftest import blender_environment


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_EXECUTABLE"),
    reason="Set BLENDER_TEST_EXECUTABLE for evaluated geometry checks",
)
def test_evaluated_geometry_in_blender(unpacked_addon, tmp_path):
    script = tmp_path / "check_geometry.py"
    script.write_text(f"""import importlib.util
import json
import runpy
import sys
from pathlib import Path
import bpy

directory = Path({str(unpacked_addon)!r})
sys.path.extend(str(wheel) for wheel in (directory / 'wheels').glob('*.whl'))
spec = importlib.util.spec_from_file_location('geometry_native', directory / '__init__.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
checks = runpy.run_path({str(Path(__file__).with_name("blender_geometry_checks.py"))!r})
result = checks['run_checks'](module.server.BlenderMCPServer())
assert sorted(obj.name for obj in bpy.data.objects) == ['Camera', 'Cube', 'Light']
print('EVALUATED_GEOMETRY_OK', json.dumps(result))
""")
    checked = subprocess.run(
        [
            os.environ["BLENDER_TEST_EXECUTABLE"],
            "--background",
            "--factory-startup",
            "--offline-mode",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(script),
        ],
        env=blender_environment(tmp_path / "profile"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "EVALUATED_GEOMETRY_OK" in checked.stdout
