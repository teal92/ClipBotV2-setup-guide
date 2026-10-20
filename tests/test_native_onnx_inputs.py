import importlib
import io
import json
from pathlib import Path
import stat
import zipfile

import pytest


def module():
    return importlib.import_module('tools.native_onnx_inputs')


def test_intel_sources_refuse_changed_version_origin_or_hash():
    original=json.loads(Path('packaging/platforms/onnxruntime-intel-sources.json').read_text())
    assert len(module().validate_manifest(original))==21
    for mutate in (lambda v:v.update(version='1.23.2'),
                   lambda v:v['dependencies'][0].update(url='https://github.com/unknown/unreviewed.zip'),
                   lambda v:v['dependencies'][0].update(sha256='not-a-hash')):
        value=json.loads(json.dumps(original))
        mutate(value)
        with pytest.raises(ValueError):
            module().validate_manifest(value)


def archive(path,entries):
    with zipfile.ZipFile(path,'w') as output:
        for name,data,link in entries:
            entry=zipfile.ZipInfo(name)
            entry.external_attr=((stat.S_IFLNK if link else stat.S_IFREG)|0o644)<<16
            output.writestr(entry,data)


def test_intel_source_extract_materializes_only_internal_aliases(tmp_path):
    source=tmp_path/'source.zip'
    archive(source,[('pkg/LICENSE','full license',False),('pkg/python/LICENSE','../LICENSE',True)])
    root=module().extract_archive(source,tmp_path/'out')
    assert (root/'python/LICENSE').read_text()=='full license'
    assert not (root/'python/LICENSE').is_symlink()


@pytest.mark.parametrize('entries',[
    [('../outside','invalid',False)],
    [('pkg/link','../../outside',True)],
    [('pkg/link','safe',True),('pkg/link/written','invalid',False)],
    [('pkg/link','.',True)],
])
def test_intel_source_extract_rejects_escape_and_link_parent_writes(tmp_path,entries):
    source=tmp_path/'source.zip'
    archive(source,entries)
    with pytest.raises(ValueError):
        module().extract_archive(source,tmp_path/'out')
    assert not (tmp_path/'outside').exists()
