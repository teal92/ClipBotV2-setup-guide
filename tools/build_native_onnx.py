"""Compile the reviewed current ONNX CPU runtime on an Intel Mac only."""
import argparse
import hashlib
import json
import os
import re
import signal
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from worker.platforms.identity import current_target
from tools.build_native_runtime import SourceRedirects,download,verify_source
from tools.native_onnx_inputs import ONNX_COMMIT,DEPS_SHA256,validate_manifest,extract_archive
from tools.native_dependency_snapshot import canonicalize_name,parse_wheel_filename,supported_tags
from tools.verify_native_release_inputs import verify_native_runtime,verify_binary_target
from tools.native_onnx_sources import write_bundle


class IntelSourceRedirects(SourceRedirects):
    hosts=SourceRedirects.hosts|{'codeload.github.com','gitlab.com'}


def offline_dependencies(raw,policy,archives):
    records=validate_manifest(policy)
    if hashlib.sha256(raw).hexdigest()!=DEPS_SHA256:
        raise ValueError('Upstream ONNX dependency manifest changed.')
    known={item['name']:item for item in records}
    lines=[]
    for line in raw.decode().splitlines():
        if not line or line.startswith('#'):
            lines.append(line)
            continue
        name,_,digest=line.split(';')
        path=Path(archives).absolute()/(known[name]['filename'] if name in known else 'UNREVIEWED-'+name+'.zip')
        lines.append(name+';'+path.as_uri()+';'+digest)
    return '\n'.join(lines)+'\n'


def guard_fetch_wrapper(text):
    needle='cmake_parse_arguments(PARSE_ARGV 1 ARG "" "URL;SOURCE_SUBDIR" "")'
    if text.count(needle)!=1:
        raise ValueError('Upstream native dependency wrapper changed.')
    return text.replace(needle,needle+'\n    if(NOT ARG_URL MATCHES "^file:///")\n'
        '      message(FATAL_ERROR "Native build requires a reviewed local dependency archive")\n    endif()')


def isolated_build_command(script,arguments):
    # Upstream imports sibling build_args. Admit only that pinned source directory;
    # retain -I so neither the working directory nor PYTHONPATH is imported.
    script=Path(script).resolve(strict=True)
    bootstrap=('import pathlib,runpy,sys; '
               'script=pathlib.Path(sys.argv[1]); '
               'sys.path.insert(0,str(script.parent)); '
               'sys.argv=sys.argv[1:]; '
               'runpy.run_path(str(script),run_name="__main__")')
    return [sys.executable,'-I','-c',bootstrap,str(script),*map(str,arguments)]


def build_arguments(build_root,protoc):
    return list(map(str,['--config','Release','--update','--build','--build_wheel',
         '--skip_tests','--skip_submodule_sync','--skip_pip_install','--no_telemetry','--parallel','4',
         '--compile_no_warning_as_error','--cmake_generator','Ninja','--build_dir',build_root,
         '--path_to_protoc_exe',protoc,'--cmake_extra_defines',
         'CMAKE_OSX_ARCHITECTURES=x86_64','CMAKE_OSX_DEPLOYMENT_TARGET=14.0','BUILD_TESTING=OFF',
         'onnxruntime_BUILD_UNIT_TESTS=OFF','onnxruntime_USE_COREML=OFF','onnxruntime_USE_XNNPACK=OFF',
         'onnxruntime_USE_MIMALLOC=OFF','FETCHCONTENT_UPDATES_DISCONNECTED=ON',
         'FETCHCONTENT_TRY_FIND_PACKAGE_MODE=NEVER','CMAKE_FIND_USE_PACKAGE_REGISTRY=OFF',
         'CMAKE_SHARED_LINKER_FLAGS=-Wl,-headerpad_max_install_names',
         'CMAKE_MODULE_LINKER_FLAGS=-Wl,-headerpad_max_install_names']))


def verify_telemetry_disabled(cache):
    values=re.findall(r'^onnxruntime_USE_TELEMETRY:[A-Z_]+=([^\r\n]+)$',cache,re.MULTILINE)
    if values!=['OFF']:
        raise ValueError('Intel ONNX build must explicitly disable upstream telemetry.')


def execute_logged(args,environment,log,*,timeout):
    """Retain diagnostic output on timeout and terminate the native process group."""
    def save(stdout,stderr):
        with Path(log).open('a',encoding='utf-8') as stream:
            stream.write((stdout or '')[-10_000_000:]+(stderr or '')[-10_000_000:])
    with subprocess.Popen(args,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,start_new_session=os.name=='posix') as process:
        try:
            stdout,stderr=process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name=='posix':
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            else:process.kill()
            stdout,stderr=process.communicate(timeout=10)
            save(stdout,stderr)
            raise
        save(stdout,stderr)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode,args)
        return stdout


def build(output,*,host_target=None):
    if (host_target or current_target())!='macos-x86_64':
        raise ValueError('ONNX compilation requires a matching Intel Mac.')
    verify_native_runtime()
    policy=json.loads((ROOT/'packaging/platforms/onnxruntime-intel-sources.json').read_text())
    records=validate_manifest(policy)
    output=Path(output).absolute()
    if output.exists():
        raise ValueError('Use a new Intel source-build directory.')
    output.mkdir(mode=0o700,parents=True)
    output=output.resolve()
    log=output/'build.log'
    environment={key:value for key,value in os.environ.items()
                 if not key.startswith(('CLIPBOT_','ANTHROPIC_','STRIPE_','RESEND_','PYTHON'))
                 and key not in {'GITHUB_TOKEN','GH_TOKEN','GIT_ASKPASS','CPATH','LIBRARY_PATH','DYLD_LIBRARY_PATH',
                                 'PKG_CONFIG_PATH','PKG_CONFIG_LIBDIR','CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS'}}
    environment.update(PYTHONNOUSERSITE='1',PIP_CONFIG_FILE=os.devnull,PIP_NO_INDEX='1',
                       MACOSX_DEPLOYMENT_TARGET='14.0',GIT_TERMINAL_PROMPT='0',
                       GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_NOSYSTEM='1')
    commands=[]

    def run(args,*,timeout=600):
        args=list(map(str,args))
        commands.append(args)
        return execute_logged(args,environment,log,timeout=timeout)

    archives=output/'archives'
    archives.mkdir()
    for record in records:
        path=archives/record['filename']
        download(record,path,redirect_handler=IntelSourceRedirects())
        # Validate the exact archives before any build tool processes them.
        extract_archive(path,output/'validated-sources'/record['name'],single_root=record['name']!='protoc_mac_universal')
    source=output/'source'
    run(['git','clone','--filter=blob:none','--no-checkout','--depth=1','--branch','v1.31.0',
         'https://github.com/microsoft/onnxruntime.git',source])
    if run(['git','-C',source,'rev-parse','HEAD']).strip()!=ONNX_COMMIT:
        raise ValueError('Official ONNX source tag differs from reviewed immutable commit.')
    run(['git','-C',source,'sparse-checkout','set','--no-cone','/*','!/onnxruntime/test/testdata/',
         '!/orttraining/orttraining/test/testdata/','!/js/'])
    run(['git','-C',source,'checkout','--detach',ONNX_COMMIT])
    if (source/'VERSION_NUMBER').read_text().strip()!='1.31.0':
        raise ValueError('Intel source version differs from reviewed policy.')
    dependencies=source/'cmake/deps.txt'
    dependencies.write_text(offline_dependencies(dependencies.read_bytes(),policy,archives))
    wrapper=source/'cmake/external/helper_functions.cmake'
    wrapper.write_text(guard_fetch_wrapper(wrapper.read_text()))
    diff=run(['git','-C',source,'diff','--','cmake/deps.txt','cmake/external/helper_functions.cmake'])
    (output/'offline-build.patch').write_text(diff)
    tracked=run(['git','-C',source,'ls-files','-z']).split('\0')
    source_files=[name for name in tracked if name and (source/name).is_file()]
    source_bundle=output/'onnx-sources.zip'
    write_bundle(source,source_files,[(record,archives/record['filename']) for record in records],
                 source_bundle,{'sourceCommit':ONNX_COMMIT,'version':'1.31.0','sourceArchives':records,
                 'excludedUnbuiltTrees':['onnxruntime/test/testdata','orttraining/orttraining/test/testdata','js'],
                 'offlineBuildPatch':diff})
    environment['SOURCE_DATE_EPOCH']=run(['git','-C',source,'show','-s','--format=%ct',ONNX_COMMIT]).strip()
    environment['CFLAGS']=environment['CXXFLAGS']='-ffile-prefix-map='+str(output)+'=/clipbot-build/onnxruntime'
    protoc=output/'validated-sources/protoc_mac_universal/bin/protoc'
    verify_binary_target(protoc.read_bytes(),'macos-x86_64','14.0')
    build_root=output/'compiled'
    run(isolated_build_command(source/'tools/ci_build/build.py',build_arguments(build_root,protoc)),timeout=3600)
    verify_telemetry_disabled((build_root/'Release/CMakeCache.txt').read_text())
    built=list((build_root/'Release/dist').glob('onnxruntime-*.whl'))
    if len(built)!=1:
        raise ValueError('Intel compilation must produce exactly one current ONNX wheel.')
    repaired=output/'wheels'
    repaired.mkdir()
    run([Path(sys.executable).parent/'delocate-wheel','--require-archs','x86_64','--wheel-dir',repaired,built[0]])
    wheels=list(repaired.glob('onnxruntime-*.whl'))
    if len(wheels)!=1:
        raise ValueError('Intel wheel repair did not produce one artifact.')
    wheel=wheels[0]
    name,version,_,tags=parse_wheel_filename(wheel.name)
    if canonicalize_name(name)!='onnxruntime' or str(version)!='1.31.0' or not tags.intersection(supported_tags('macos-x86_64')):
        raise ValueError('Intel ONNX wheel version, architecture or OS floor differs.')
    with wheel.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    lock=output/'onnxruntime.lock'
    lock.write_text('onnxruntime==1.31.0 --hash=sha256:'+digest+'\n')
    run([sys.executable,'-I','-m','pip','--isolated','install','--no-index','--no-deps',
         '--require-hashes','--only-binary=:all:','--find-links',repaired,'-r',lock])
    diagnostics=json.loads(run([sys.executable,'-I','-c',
        'import importlib.metadata,json,onnxruntime; '
        'assert onnxruntime.__version__=="1.31.0"; '
        'assert "CPUExecutionProvider" in onnxruntime.get_available_providers(); '
        'root=importlib.metadata.distribution("faster-whisper").locate_file("faster_whisper/assets"); '
        'models=list(root.glob("*.onnx")); assert models; '
        '[onnxruntime.InferenceSession(str(p),providers=["CPUExecutionProvider"]) for p in models]; '
        'print(json.dumps({"version":onnxruntime.__version__,"vadModelsLoaded":len(models)}))']))
    toolchain={name:run([name,'--version'])[:8000] for name in ('cmake','ninja','clang')}
    receipt={'target':'macos-x86_64','version':'1.31.0','sourceCommit':ONNX_COMMIT,'commands':commands,
             'dependencyManifestSha256':DEPS_SHA256,'sourceArchives':records,'toolchain':toolchain,
             'wheelFilename':wheel.name,'sha256':digest,'sizeBytes':wheel.stat().st_size,'installed':diagnostics,
             'nativeCompilationPassed':True,'telemetryDisabled':True,'sourceComplete':False,'publicReleaseApproved':False}
    with source_bundle.open('rb') as stream:
        receipt['sourceBundle']={'filename':source_bundle.name,'sizeBytes':source_bundle.stat().st_size,
                                 'sha256':hashlib.file_digest(stream,'sha256').hexdigest()}
    (output/'onnx-diagnostics.json').write_text(json.dumps(receipt,indent=2))
    print('Current Intel ONNX CPU runtime compiled and VAD models loaded; packaged acceptance remains separate.')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    build(parser.parse_args().output)
