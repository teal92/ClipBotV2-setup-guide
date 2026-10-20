"""Pinned Intel CPU build inputs and extraction without filesystem links."""
import hashlib
import posixpath
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import unicodedata
from urllib.parse import urlsplit
import zipfile

ROOT=Path(__file__).resolve().parents[1]
ONNX_COMMIT='402979ac1bef89dcd55add41eaa7277c407599eb'
DEPS_SHA256='ff3ae70274f3f4e1c8dec41acde16fffa2b904965e390795f771100b20646c2c'
NAMES=frozenset({'abseil_cpp','cxxopts','date','dlpack','eigen','flatbuffers','fp16','fxdiv',
    'json','microsoft_gsl','mimalloc','mp11','onnx','protobuf','protoc_mac_universal',
    'psimd','pthreadpool','pybind11','pytorch_cpuinfo','re2','safeint'})


def validate_manifest(value):
    if (value.get('target')!='macos-x86_64' or value.get('version')!='1.31.0'
            or value.get('sourceCommit')!=ONNX_COMMIT or value.get('dependencyManifestSha256')!=DEPS_SHA256
            or value.get('nativeBuildAccepted') is not False or value.get('publicReleaseApproved') is not False
            or not isinstance(value.get('dependencies'),list) or len(value['dependencies'])!=len(NAMES)):
        raise ValueError('Intel native source policy differs from reviewed version or target.')
    raw=(ROOT/'packaging/native/onnxruntime/deps.txt').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=DEPS_SHA256:
        raise ValueError('Original upstream Intel dependency manifest changed.')
    declared={}
    for line in raw.decode().splitlines():
        if line and not line.startswith('#'):
            name,url,digest=line.split(';')
            declared[name]=(url,digest)
    seen=set()
    for record in value['dependencies']:
        name=record.get('name')
        parsed=urlsplit(record.get('url',''))
        if (name not in NAMES or name in seen or declared.get(name)!=(record.get('url'),record.get('upstreamSha1'))
                or parsed.scheme!='https' or parsed.hostname not in {'github.com','gitlab.com'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not re.fullmatch('[a-f0-9]{64}',record.get('sha256',''))
                or type(record.get('sizeBytes')) is not int or not 0<record['sizeBytes']<=80_000_000
                or record.get('verified') is not True
                or record.get('filename') not in {name+'.zip',name+'.tar.gz'}):
            raise ValueError('Intel source identity, origin or strong hash differs from reviewed policy.')
        seen.add(name)
    return value['dependencies']


def safe_name(name):
    name=name.rstrip('/')
    if (not name or len(name)>4000 or '\\' in name or ':' in name or '\x00' in name
            or PurePosixPath(name).is_absolute() or any(p in {'','.','..'} or p.rstrip(' .')!=p for p in name.split('/'))):
        raise ValueError('Unsafe native source archive path.')
    return name


def extract_archive(path,destination,*,single_root=True):
    """Materialize bounded internal aliases as copies, never actual links.

    Official FlatBuffers/Re2 sources contain internal documentation aliases.
    Entries below aliases, aliases containing aliases and recursive/escaping
    targets are refused before any source files are written.
    """
    destination=Path(destination).absolute()
    if destination.exists():
        raise ValueError('Use a new source extraction directory.')
    archive=zipfile.ZipFile(path) if zipfile.is_zipfile(path) else tarfile.open(path,'r:*')
    with archive:
        zipped=isinstance(archive,zipfile.ZipFile)
        members=archive.infolist() if zipped else archive.getmembers()
        if not 1<=len(members)<=50_000:
            raise ValueError('Native source archive member count exceeds limits.')
        records={}
        seen=set()
        total=0
        for entry in members:
            name=safe_name(entry.filename if zipped else entry.name)
            folded=unicodedata.normalize('NFC',name).casefold()
            if folded in seen:
                raise ValueError('Duplicate native source member.')
            seen.add(folded)
            mode=entry.external_attr>>16 if zipped else entry.mode
            size=entry.file_size if zipped else entry.size
            directory=entry.is_dir() if zipped else entry.isdir()
            link=stat.S_ISLNK(mode) if zipped else entry.issym() or entry.islnk()
            regular=stat.S_IFMT(mode) in {0,stat.S_IFREG} if zipped else entry.isfile()
            if (mode&0o7000 or (zipped and entry.flag_bits&1) or not(directory or link or regular)
                    or not 0<=size<=100_000_000):
                raise ValueError('Unsafe native source member type or size.')
            total+=size
            if total>500_000_000:
                raise ValueError('Native source archive exceeds extraction limit.')
            target=None
            if link:
                if zipped:
                    if size>4096:
                        raise ValueError('Native source alias exceeds limit.')
                    target=archive.read(entry).decode('utf-8')
                else:
                    target=entry.linkname
                if not target or '\\' in target or ':' in target or '\x00' in target or target.startswith('/'):
                    raise ValueError('Unsafe native source alias.')
                base='' if not zipped and entry.islnk() else posixpath.dirname(name)
                target=safe_name(posixpath.normpath(posixpath.join(base,target)))
            records[name]=(entry,directory,link,target,mode,size)
        links={name:r[3] for name,r in records.items() if r[2]}
        for name in records:
            parents=PurePosixPath(name).parents
            if any(str(parent) in links for parent in parents):
                raise ValueError('Native source entries cannot be written below an alias.')
        for name,target in links.items():
            if (target in links or any(str(p) in links for p in PurePosixPath(target).parents)
                    or any(other.startswith(target+'/') for other in links)
                    or not(target in records or any(other.startswith(target+'/') for other in records))):
                raise ValueError('Native source alias is recursive, missing or contains another alias.')
        roots={name.split('/')[0] for name in records}
        if single_root and len(roots)!=1:
            raise ValueError('Native source archive requires one source root.')
        destination.mkdir(mode=0o700,parents=True)
        destination=destination.resolve()
        for name,(entry,directory,link,target,mode,size) in records.items():
            if link:
                continue
            path=destination.joinpath(*name.split('/'))
            if directory:
                path.mkdir(mode=0o755,parents=True,exist_ok=True)
            else:
                path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
                with (archive.open(entry) if zipped else archive.extractfile(entry)) as source,path.open('xb') as output:
                    shutil.copyfileobj(source,output)
                if path.stat().st_size!=size:
                    raise ValueError('Native source file length differs from archive.')
                path.chmod(0o755 if mode&0o111 else 0o644)
        for name,target in links.items():
            source=destination.joinpath(*target.split('/'))
            path=destination.joinpath(*name.split('/'))
            path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            extra=sum(p.stat().st_size for p in source.rglob('*') if p.is_file()) if source.is_dir() else source.stat().st_size
            total+=extra
            if total>500_000_000:
                raise ValueError('Materialized native source aliases exceed extraction limit.')
            if source.is_dir():
                shutil.copytree(source,path,symlinks=False)
            else:
                shutil.copy2(source,path)
    return destination/next(iter(roots)) if single_root else destination
