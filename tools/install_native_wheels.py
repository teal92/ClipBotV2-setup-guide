"""Prepare an isolated build environment from committed artifact evidence.

Source packages remain explicitly pending. This does not produce a release lock,
approve dependencies or enable downloads. Pip never resolves from the network.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from tools.native_dependency_snapshot import TARGETS, SOURCE_BUILDS, canonicalize_name, dependency_closure, reviewed_versions, select_artifact, validate_dependencies
from tools.build_native_runtime import download
from tools.verify_native_release_inputs import verify_native_runtime
from worker.platforms.identity import current_target


def validate_snapshot(snapshot, target):
    fields = {'target','python','dependencyLockComplete','nativeBuildAccepted','publicReleaseApproved','packages'}
    if (target not in TARGETS or not isinstance(snapshot,dict) or set(snapshot) != fields
            or snapshot['target'] != target or snapshot['python'] != '3.12.15'
            or any(snapshot[key] is not False for key in ('dependencyLockComplete','nativeBuildAccepted','publicReleaseApproved'))
            or not isinstance(snapshot['packages'],list) or not 1 <= len(snapshot['packages']) <= 100):
        raise ValueError('Invalid or prematurely approved native dependency evidence.')
    versions = {canonicalize_name(name):version for name,version in reviewed_versions(target).items()}
    records = {}
    wheels, sources = [], []
    for entry in snapshot['packages']:
        name = canonicalize_name(entry['name'])
        if name in records or versions.get(name) != entry['version']:
            raise ValueError('Dependency name/version differs from reviewed policy.')
        records[name] = entry
    actual_versions = {name:entry['version'] for name,entry in records.items()}
    closure = dependency_closure(snapshot['packages'],target)
    if {canonicalize_name(item['name']) for item in closure} != set(records):
        raise ValueError('Native dependency closure is incomplete or contains unrelated packages.')
    for name,entry in records.items():
        if validate_dependencies(entry.get('requires'),actual_versions,target) or entry.get('unreviewedDependencies'):
            raise ValueError('Native dependency closure has unknown/conflicting requirements.')
        if entry.get('blocked'):
            if (target == 'macos-x86_64' and name == 'onnxruntime' and entry['version'] == '1.31.0'
                    and entry['blocked'] == 'No compatible wheel for target/minimum OS.'):
                sources.append(entry)
                continue
            raise ValueError('Native dependency remains blocked: '+name)
        artifact = entry['artifact']
        expected_kind = 'source' if name in SOURCE_BUILDS else 'wheel'
        if artifact.get('kind') != expected_kind or artifact.get('nativeBuildAccepted') is not False:
            raise ValueError('Native dependency source-build policy changed.')
        # Reuse the same strict identity, URL, hash, size and ABI checks on the
        # committed evidence, without trusting its earlier preparation result.
        check = select_artifact({'info':{'name':entry['name'],'version':entry['version']}, 'urls':[{
            'filename':artifact['filename'],'url':artifact['url'], 'digests':{'sha256':artifact['sha256']},
            'size':artifact['sizeBytes'],'yanked':False}]},entry['name'],entry['version'],target,source=expected_kind=='source')
        if check != artifact:
            raise ValueError('Native dependency artifact evidence changed.')
        (sources if expected_kind=='source' else wheels).append(entry)
    if sum(item['artifact']['sizeBytes'] for item in wheels) > 1_000_000_000:
        raise ValueError('Native dependency download exceeds build limit.')
    return wheels, sources


def run_logged(args, environment, log_path):
    result = subprocess.run(list(map(str,args)),env=environment,capture_output=True,text=True,timeout=600)
    with log_path.open('a',encoding='utf-8') as log:
        log.write(result.stdout+result.stderr)
    result.check_returncode()
    return result.stdout


def prepare(target, output, *, host_target=None):
    if target not in TARGETS or (host_target or current_target()) != target:
        raise ValueError('Wheel preparation requires a matching native host and processor.')
    verify_native_runtime()
    evidence = ROOT/f'packaging/platforms/{target}.dependencies.json'
    if evidence.stat().st_size > 500_000:
        raise ValueError('Native dependency evidence exceeds limit.')
    snapshot = json.loads(evidence.read_text())
    wheels, sources = validate_snapshot(snapshot,target)
    output = Path(output).absolute()
    if output.exists():
        raise ValueError('Use a new isolated wheel-preparation directory.')
    output.mkdir(mode=0o700,parents=True)
    output = output.resolve()
    wheelhouse = output/'wheels'
    wheelhouse.mkdir()
    lines = ['--no-index','--require-hashes','--only-binary=:all:']
    for item in wheels:
        artifact = item['artifact']
        download(artifact,wheelhouse/artifact['filename'])
        lines.append(item['name']+'=='+item['version']+' --hash=sha256:'+artifact['sha256'])
    requirements = output/'prebuilt-only.lock'
    requirements.write_text('\n'.join(lines)+'\n')
    environment = {key:value for key,value in os.environ.items()
                   if not key.startswith(('CLIPBOT_','ANTHROPIC_','STRIPE_','RESEND_','PYTHON'))}
    environment.update(PYTHONNOUSERSITE='1',PIP_CONFIG_FILE=os.devnull,PIP_NO_INDEX='1',PIP_DISABLE_PIP_VERSION_CHECK='1')
    log_path = output/'build.log'

    def run(args, capture=False):
        result = run_logged(args,environment,log_path)
        return result if capture else None

    run([sys.executable,'-I','-m','venv',output/'environment'])
    python = output/'environment/bin/python'
    # The closure was checked above. Native source packages are intentionally
    # pending here; --no-deps prevents pip inventing/installing replacements.
    run([python,'-I','-m','pip','--isolated','install','--no-index','--no-deps','--require-hashes',
         '--only-binary=:all:','--find-links',wheelhouse,'-r',requirements])
    installed = json.loads(run([python,'-I','-c','import importlib.metadata,json; print(json.dumps([(d.metadata["Name"],d.version) for d in importlib.metadata.distributions()]))'],capture=True))
    actual = {canonicalize_name(name):version for name,version in installed}
    expected = {canonicalize_name(item['name']):item['version'] for item in wheels}
    if len(actual) != len(installed) or actual != expected:
        raise ValueError('Installed wheel environment differs from exact reviewed inventory.')
    run([python,'-I','-c','import fastapi,httpx,PIL,psutil,ctranslate2,tokenizers; print("Prebuilt native module imports passed")'])
    receipt = {'target':target,'python':'3.12.15','dependencyEvidenceSha256':hashlib.sha256(evidence.read_bytes()).hexdigest(),
               'installed':actual,'pendingSourcePackages':[item['name'] for item in sources],
               'dependencyLockComplete':False,'sourceComplete':False,'publicReleaseApproved':False}
    (output/'wheel-diagnostics.json').write_text(json.dumps(receipt,indent=2))
    print('Hash-locked prebuilt dependencies prepared; native source builds and release review remain pending.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target',choices=sorted(TARGETS),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    try:
        prepare(args.target,args.output)
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
        print('Native wheel preparation blocked:',str(error))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
