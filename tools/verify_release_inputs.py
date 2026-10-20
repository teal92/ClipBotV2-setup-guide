"""Verify reviewed release inputs without executing tools or accepting PATH candidates."""
import hashlib,json,re,sys,zipfile
from pathlib import Path,PurePosixPath


def verify_installed_native_wheel(wheel,site_root=None):
    """Version strings alone cannot distinguish our build from the vendor wheel."""
    if site_root is None:
        import importlib.metadata
        site_root=importlib.metadata.distribution('av').locate_file('')
    site=Path(site_root).absolute()
    if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in (site,*site.parents)):
        raise ValueError('Unsafe native installation path.')
    expected={};seen=set();total=0
    try:
        with zipfile.ZipFile(wheel) as archive:
            if len(archive.infolist())>5000:raise ValueError('Native wheel exceeds limits.')
            for item in archive.infolist():
                name=item.filename;parts=name.rstrip('/').split('/')
                if (not name or '\\' in name or ':' in name or name.startswith('/') or
                    any(p in ('','.','..') or p.rstrip(' .')!=p for p in parts)):
                    raise ValueError('Unsafe native wheel member.')
                key=name.casefold()
                if key in seen:raise ValueError('Duplicate native wheel member.')
                seen.add(key)
                total+=item.file_size
                if total>250_000_000:raise ValueError('Native wheel exceeds limits.')
                if item.is_dir() or parts[0] not in ('av','av.libs'):continue
                path=site.joinpath(*parts)
                if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in (path,*path.parents)):
                    raise ValueError('Unsafe native module path.')
                if not path.resolve().is_relative_to(site.resolve()) or not path.is_file():
                    raise ValueError('Installed native wheel differs from reviewed artifact.')
                if path.stat().st_size!=item.file_size or hashlib.sha256(path.read_bytes()).digest()!=hashlib.sha256(archive.read(item)).digest():
                    raise ValueError('Installed native wheel differs from reviewed artifact.')
                expected[key]=path
        if not any(n.endswith('.pyd') for n in expected) or not any(n.endswith('.dll') for n in expected):
            raise ValueError('Native wheel lacks its compiled payload.')
        for namespace in ('av','av.libs'):
            for path in (site/namespace).rglob('*'):
                if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
                    raise ValueError('Unsafe native module path.')
                if path.is_file() and path.suffix.lower()!='.pyc' and path.relative_to(site).as_posix().casefold() not in expected:
                    raise ValueError('Unlisted file in native package.')
    except (OSError,zipfile.BadZipFile,KeyError) as error:
        raise ValueError('Native wheel could not be verified.') from None
    return len(expected)


def verify_release_inputs(root,manifest):
    root=Path(root).resolve()
    if not isinstance(manifest,dict) or set(manifest)!={'schemaVersion','releaseVersion','sourceComplete','dependencyLockComplete','artifacts'} or manifest['schemaVersion']!=1:
        raise ValueError('Invalid release manifest.')
    if not isinstance(manifest['releaseVersion'],str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?',manifest['releaseVersion']):
        raise ValueError('Invalid release version.')
    if manifest['sourceComplete'] is not True or manifest['dependencyLockComplete'] is not True:
        raise ValueError('Release evidence incomplete: corresponding sources and reviewed dependency lock are required.')
    entries=manifest['artifacts']
    if not isinstance(entries,list) or not 1<=len(entries)<=300:raise ValueError('Invalid input inventory.')
    seen=set()
    for item in entries:
        if not isinstance(item,dict) or set(item)!={'path','sha256','sizeBytes','role'}:raise ValueError('Invalid input record.')
        name=item['path'];digest=item['sha256'];size=item['sizeBytes']
        if not isinstance(name,str) or not name or len(name)>240 or '\\' in name or ':' in name or any(v in ('','..','.') for v in name.split('/')) or PurePosixPath(name).is_absolute():
            raise ValueError('Unsafe release input path.')
        key=name.casefold()
        if key in seen:raise ValueError('Duplicate release input.')
        seen.add(key)
        if not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest) or type(size) is not int or not 0<size<=2_000_000_000 or item['role'] not in ('media','model','compiler','lock','source','notice'):
            raise ValueError('Unverified input metadata.')
        path=root/name
        try:
            if any((root.joinpath(*PurePosixPath(name).parts[:i])).is_symlink() for i in range(1,len(PurePosixPath(name).parts)+1)) or not path.resolve().is_relative_to(root):raise ValueError('Input escapes release directory.')
            if not path.is_file() or path.stat().st_size!=size:raise ValueError('Missing or changed release input: '+name)
            checksum=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):checksum.update(chunk)
            if checksum.hexdigest()!=digest:raise ValueError('Release input hash mismatch: '+name)
        except OSError:raise ValueError('Unreadable release input: '+name) from None
    return len(seen)


def verify_component_coverage(root,manifest):
    root=Path(root).resolve()
    pinned={item['path'].casefold():item['role'] for item in manifest['artifacts']}
    for folder,role in [('packaging/tools/compiler','compiler'),('packaging/licenses','notice')]:
        directory=root/folder
        if not directory.exists():continue
        if directory.is_symlink() or (hasattr(directory,'is_junction') and directory.is_junction()):raise ValueError('Unsafe component directory.')
        for entry in directory.rglob('*'):
            if entry.is_symlink() or (hasattr(entry,'is_junction') and entry.is_junction()) or not entry.resolve().is_relative_to(root):raise ValueError('Unsafe component path.')
            if entry.is_file() and pinned.get(entry.relative_to(root).as_posix().casefold())!=role:
                raise ValueError('Unlisted compiler or license input: '+entry.relative_to(root).as_posix())


def verify_build_inventory(approved,installed=None):
    from tools.dependency_policy import canonical
    if installed is None:
        import importlib.metadata
        records=[(canonical(d.metadata['Name']),d.version) for d in importlib.metadata.distributions()]
        installed=dict(records)
        if len(records)!=len(installed):raise ValueError('Duplicate build distributions.')
    expected={canonical(name):version for name,version in approved.items()}
    actual={canonical(name):version for name,version in installed.items()}
    if len(expected)!=len(approved) or len(actual)!=len(installed) or actual!=expected:
        raise ValueError('Build environment differs from the exact reviewed dependency inventory.')


def verify_build_runtime(runtime=None):
    if runtime is None:
        import struct,sysconfig
        runtime={'implementation':sys.implementation.name,'version':tuple(sys.version_info[:3]),'platform':sys.platform,'bits':struct.calcsize('P')*8,'buildPlatform':sysconfig.get_platform()}
    if runtime!={'implementation':'cpython','version':(3,12,14),'platform':'win32','bits':64,'buildPlatform':'win-amd64'}:
        raise ValueError('Build runtime differs from reviewed CPython 3.12.14 Windows x64 target.')


def verify_packaging_inputs(root,manifest):
    count=verify_release_inputs(root,manifest)
    required={
        'packaging/tools/ffmpeg.exe','packaging/tools/ffprobe.exe','packaging/tools/yt-dlp.exe',
        'packaging/tools/compiler/ISCC.exe','packaging/worker-build.lock',
        'packaging/approved-worker-build.json','packaging/THIRD-PARTY-NOTICES.txt',
        'worker/data/models/base/config.json','worker/data/models/base/model.bin',
        'worker/data/models/base/tokenizer.json','worker/data/models/base/vocabulary.txt',
        'packaging/tools/wheels/av-18.0.0-cp311-abi3-win_amd64.whl',
        'packaging/native/corresponding-source.zip',
        'packaging/tools/runtime/msvcp140.dll','packaging/tools/runtime/vcruntime140.dll',
        'packaging/tools/runtime/vcruntime140_1.dll','packaging/tools/runtime/msvcp140_1.dll'}
    paths={item['path'] for item in manifest['artifacts']}
    if not required<=paths or not any(item['role']=='source' for item in manifest['artifacts']):
        raise ValueError('Release inventory incomplete: every used tool/model/lock and corresponding source must be pinned.')
    verify_component_coverage(root,manifest)
    from worker.version import VERSION
    if manifest['releaseVersion']!=VERSION:raise ValueError('Release version differs from companion runtime.')
    from tools.dependency_policy import verify_requirements,canonical
    root=Path(root)
    approved=json.loads((root/'packaging/approved-worker-build.json').read_text(encoding='utf-8'))
    if verify_requirements(root/'packaging/worker-build.lock',approved)!={canonical(name) for name in approved}:
        raise ValueError('Build dependency lock incomplete.')
    verify_build_runtime()
    verify_build_inventory(approved)
    verify_installed_native_wheel(root/'packaging/tools/wheels/av-18.0.0-cp311-abi3-win_amd64.whl')
    return count


def main():
    root=Path(__file__).resolve().parents[1]
    try:
        path=root/'packaging/release-inputs.json'
        if path.stat().st_size>200000:raise ValueError('Release manifest exceeds limit.')
        count=verify_packaging_inputs(root,json.loads(path.read_text(encoding='utf-8')))
    except (OSError,ValueError) as error:
        print('Release blocked:',str(error));return 1
    print('Verified release inputs:',count);return 0

if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
