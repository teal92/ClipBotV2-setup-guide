import hashlib
import importlib
import json
import subprocess
from pathlib import Path

import pytest


def module():
    return importlib.import_module('tools.build_native_onnx')


def test_intel_compiler_never_runs_on_other_hosts(tmp_path):
    with pytest.raises(ValueError,match='Intel Mac'):
        module().build(tmp_path/'out',host_target='windows-x86_64')
    assert not (tmp_path/'out').exists()


def test_isolated_build_can_import_reviewed_sibling_without_importing_cwd(tmp_path):
    trusted=tmp_path/'reviewed'
    trusted.mkdir()
    (trusted/'build_args.py').write_text('value="reviewed"\n')
    script=trusted/'build.py'
    script.write_text('import build_args,sys,importlib.util\n'
                      'assert build_args.value=="reviewed"\n'
                      'assert importlib.util.find_spec("untrusted_cwd") is None\n'
                      'assert sys.argv[1:]==["--config","Release"]\n'
                      'print("isolated sibling import passed")\n')
    (tmp_path/'untrusted_cwd.py').write_text('raise RuntimeError("cwd imported")\n')
    result=subprocess.run(module().isolated_build_command(script,['--config','Release']),
                          cwd=tmp_path,capture_output=True,text=True,check=True)
    assert result.stdout.strip()=='isolated sibling import passed'


def test_intel_recipe_overrides_upstream_telemetry_default_and_requires_disabled_cache(tmp_path):
    import argparse
    parser=argparse.ArgumentParser()
    # Exact option/destination/action from pinned upstream build_args.py.
    parser.add_argument('--no_telemetry',dest='use_telemetry',action='store_false',default=True)
    arguments=module().build_arguments(tmp_path/'compiled',tmp_path/'protoc')
    options,_=parser.parse_known_args(arguments)
    assert options.use_telemetry is False
    module().verify_telemetry_disabled('onnxruntime_USE_TELEMETRY:BOOL=OFF\n')
    for value in ('','onnxruntime_USE_TELEMETRY:BOOL=ON\n',
                  'onnxruntime_USE_TELEMETRY:BOOL=OFF\nonnxruntime_USE_TELEMETRY:STRING=ON\n'):
        with pytest.raises(ValueError):module().verify_telemetry_disabled(value)


def test_onnx_dependency_rewrite_has_no_network_fallback(tmp_path):
    raw=Path('packaging/native/onnxruntime/deps.txt').read_bytes()
    policy=json.loads(Path('packaging/platforms/onnxruntime-intel-sources.json').read_text())
    text=module().offline_dependencies(raw,policy,tmp_path)
    entries=[line.split(';') for line in text.splitlines() if line and not line.startswith('#')]
    assert len(entries)==46
    assert all(row[1].startswith('file:///') for row in entries)
    assert next(row for row in entries if row[0]=='coremltools')[1].endswith('UNREVIEWED-coremltools.zip')
    assert next(row for row in entries if row[0]=='eigen')[1].endswith('eigen.zip')
    with pytest.raises(ValueError):
        module().offline_dependencies(raw+b'changed',policy,tmp_path)


def test_onnx_fetch_guard_refuses_ambiguous_upstream_wrapper():
    original='function(onnxruntime_fetchcontent_declare contentName)\n    cmake_parse_arguments(PARSE_ARGV 1 ARG "" "URL;SOURCE_SUBDIR" "")\nendfunction()'
    patched=module().guard_fetch_wrapper(original)
    assert 'FATAL_ERROR' in patched and '^file:///' in patched
    for text in ('changed source',original+'\n'+original):
        with pytest.raises(ValueError):
            module().guard_fetch_wrapper(text)
