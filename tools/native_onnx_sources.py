"""Archive explicitly selected native sources without repository credentials."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


def safe_name(name):
    if (not isinstance(name,str) or not name or '\\' in name or ':' in name
            or any(ord(c)<32 for c in name) or name.startswith('/')
            or any(p in {'','.','..','.git'} for p in name.split('/'))):
        raise ValueError('Unsafe source archive path.')
    return PurePosixPath(name).as_posix()


def write_bundle(source_root,source_files,dependencies,output,metadata):
    root=Path(source_root).resolve(strict=True)
    output=Path(output)
    if output.exists():
        raise ValueError('Source bundle destination already exists.')
    entries=[]
    seen=set()
    total=0
    def add(path,name):
        nonlocal total
        name=safe_name(name)
        path=Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError('Source bundle requires regular files.')
        size=path.stat().st_size
        total+=size
        if size>200_000_000 or total>1_500_000_000 or name.casefold() in seen:
            raise ValueError('Source archive size or name collision.')
        seen.add(name.casefold())
        entries.append((path,name))
    for name in source_files:
        name=safe_name(name)
        path=root/name
        if not path.resolve(strict=True).is_relative_to(root) or any(p.is_symlink() for p in (path,*path.parents)):
            raise ValueError('Source path escapes reviewed tree.')
        add(path,'onnxruntime/'+name)
    for record,path in dependencies:
        path=Path(path)
        filename=safe_name(record['filename'])
        if '/' in filename or path.stat().st_size!=record['sizeBytes']:
            raise ValueError('Dependency source size differs.')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=record['sha256']:
                raise ValueError('Dependency source hash differs.')
        add(path,'dependencies/'+filename)
    try:
        with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for path,name in entries:
                archive.write(path,name)
            archive.writestr('SOURCE-MANIFEST.json',json.dumps(metadata,indent=2))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
