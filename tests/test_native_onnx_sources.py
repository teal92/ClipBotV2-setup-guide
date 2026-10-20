import hashlib
import importlib
import json
import zipfile

import pytest


def module():
    return importlib.import_module('tools.native_onnx_sources')


def test_cpu_source_bundle_preserves_sources_pins_and_never_contains_git_credentials(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'LICENSE').write_text('MIT source notice')
    (source/'runtime.cpp').write_text('reviewed native source')
    (source/'.git').mkdir();(source/'.git/config').write_text('must never be included')
    archive=tmp_path/'dependency.tar.gz';archive.write_bytes(b'hashed dependency source')
    record={'name':'example','filename':archive.name,'sizeBytes':archive.stat().st_size,
            'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    output=tmp_path/'onnx-sources.zip'
    module().write_bundle(source,['LICENSE','runtime.cpp'],[(record,archive)],output,{'sourceCommit':'a'*40})
    with zipfile.ZipFile(output) as z:
        assert z.read('onnxruntime/runtime.cpp')==b'reviewed native source'
        assert z.read('dependencies/dependency.tar.gz')==b'hashed dependency source'
        assert not any('.git' in p.split('/') for p in z.namelist())
        assert json.loads(z.read('SOURCE-MANIFEST.json'))['sourceCommit']=='a'*40
    archive.write_bytes(b'changed')
    with pytest.raises(ValueError):
        module().write_bundle(source,['LICENSE'],[(record,archive)],tmp_path/'bad.zip',{})
    assert not (tmp_path/'bad.zip').exists()


@pytest.mark.parametrize('name',['../outside','.git/config','nested/../../outside','/absolute'])
def test_source_bundle_rejects_unsafe_or_private_paths(tmp_path,name):
    with pytest.raises(ValueError):
        module().write_bundle(tmp_path,[name],[],tmp_path/'bad.zip',{})
    assert not (tmp_path/'bad.zip').exists()
