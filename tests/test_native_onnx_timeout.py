from pathlib import Path
import subprocess
import sys
import pytest


def test_compiler_timeout_preserves_partial_progress_and_stops_the_child(tmp_path):
    from tools.build_native_onnx import execute_logged
    log=tmp_path/'compile.log'
    with pytest.raises(subprocess.TimeoutExpired):
        execute_logged([sys.executable,'-I','-c',
                        'import time; print("compilation reached checkpoint",flush=True); time.sleep(30)'],
                       None,log,timeout=1)
    assert 'compilation reached checkpoint' in log.read_text(encoding='utf-8')
    result=execute_logged([sys.executable,'-I','-c','print("complete")'],None,log,timeout=10)
    assert result=='complete\n'
