"""Native companion build gates; Windows release verification stays separate."""
import argparse
import hashlib
import json
import re
import stat
import struct
import sys
import zipfile
from pathlib import Path, PurePosixPath

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.verify_release_inputs import verify_release_inputs, verify_build_inventory
from worker.platforms.identity import current_target

NATIVE_TARGETS = frozenset({'macos-arm64', 'macos-x86_64', 'linux-x86_64'})


def verify_native_pyav(wheel, site_root=None):
    """Version strings cannot distinguish our decoder from a vendor build."""
    if site_root is None:
        import importlib.metadata
        site_root = importlib.metadata.distribution('av').locate_file('')
    site = Path(site_root).absolute()

    def unsafe(path):
        return any(parent.is_symlink() or (hasattr(parent,'is_junction') and parent.is_junction())
                   for parent in (path,*path.parents))

    if unsafe(site):
        raise ValueError('Unsafe native decoder installation path.')
    expected, seen, total = set(), set(), 0
    try:
        with zipfile.ZipFile(wheel) as archive:
            if len(archive.infolist()) > 5000:
                raise ValueError('Native decoder wheel exceeds limits.')
            for item in archive.infolist():
                name = item.filename.rstrip('/')
                parts = name.split('/')
                mode = item.external_attr >> 16
                if (not name or '\\' in name or ':' in name or PurePosixPath(name).is_absolute()
                        or any(part in {'','.','..'} or part.rstrip(' .') != part for part in parts)
                        or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0,stat.S_IFREG,stat.S_IFDIR})
                        or item.flag_bits & 1):
                    raise ValueError('Unsafe native decoder wheel member.')
                key = name.casefold()
                if key in seen:
                    raise ValueError('Duplicate native decoder wheel member.')
                seen.add(key)
                total += item.file_size
                if total > 250_000_000:
                    raise ValueError('Native decoder wheel exceeds limits.')
                if item.is_dir() or parts[0] not in {'av','av.libs'}:
                    continue
                path = site.joinpath(*parts)
                if unsafe(path) or not path.resolve().is_relative_to(site.resolve()):
                    raise ValueError('Unsafe native decoder module path.')
                if not path.is_file() or path.stat().st_size != item.file_size:
                    raise ValueError('Installed native decoder differs from reviewed wheel.')
                with archive.open(item) as original, path.open('rb') as installed:
                    if hashlib.file_digest(original,'sha256').digest() != hashlib.file_digest(installed,'sha256').digest():
                        raise ValueError('Installed native decoder differs from reviewed wheel.')
                expected.add(key)
        if (not any(name.startswith('av/') and name.endswith('.so') for name in expected)
                or not any('libavcodec' in name and ('.so' in name or name.endswith('.dylib')) for name in expected)):
            raise ValueError('Native decoder wheel lacks compiled bindings or bundled decoder libraries.')
        for namespace in ('av','av.libs'):
            for path in (site/namespace).rglob('*'):
                if unsafe(path):
                    raise ValueError('Unsafe native decoder module path.')
                if path.is_file() and path.suffix.lower() != '.pyc' and path.relative_to(site).as_posix().casefold() not in expected:
                    raise ValueError('Unlisted file in native decoder installation.')
    except (OSError,zipfile.BadZipFile,RuntimeError,NotImplementedError):
        raise ValueError('Native decoder wheel could not be verified.') from None
    return len(expected)


def binary_architectures(data: bytes) -> set[str]:
    if len(data) >= 20 and data[:6] == b'\x7fELF\x02\x01' and int.from_bytes(data[18:20], 'little') == 62:
        return {'linux-x86_64'}
    cpu_targets = {0x01000007: 'macos-x86_64', 0x0100000c: 'macos-arm64'}
    if len(data) >= 32 and data[:4] in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf'):
        endian = 'little' if data[:4] == b'\xcf\xfa\xed\xfe' else 'big'
        target = cpu_targets.get(int.from_bytes(data[4:8], endian))
        if target:
            return {target}
    if len(data) >= 8 and data[:4] in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
        count = int.from_bytes(data[4:8], 'big')
        stride = 32 if data[:4] == b'\xca\xfe\xba\xbf' else 20
        if not 1 <= count <= 16 or len(data) < 8 + count * stride:
            raise ValueError('Invalid universal Mach-O header.')
        result = set()
        for index in range(count):
            target = cpu_targets.get(int.from_bytes(data[8+index*stride:12+index*stride], 'big'))
            if target:
                result.add(target)
        if result:
            return result
    raise ValueError('Unsupported or malformed native executable architecture.')


def verify_binary_target(data: bytes, target: str, minimum: str) -> None:
    if target not in binary_architectures(data):
        raise ValueError('Native binary has the wrong architecture.')
    if not target.startswith('macos-'):
        return
    if not re.fullmatch(r'\d{1,2}\.\d{1,2}(?:\.\d{1,2})?', minimum):
        raise ValueError('Invalid macOS minimum OS version.')
    declared = tuple(int(part) for part in minimum.split('.'))
    declared = (declared+(0,0,0))[:3]
    if data[:4] in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
        wide = data[:4] == b'\xca\xfe\xba\xbf'
        stride = 32 if wide else 20
        wanted = 0x0100000c if target=='macos-arm64' else 0x01000007
        slices = []
        for index in range(int.from_bytes(data[4:8], 'big')):
            record = 8+index*stride
            if int.from_bytes(data[record:record+4],'big') != wanted:
                continue
            width = 8 if wide else 4
            offset = int.from_bytes(data[record+8:record+8+width],'big')
            length = int.from_bytes(data[record+8+width:record+8+2*width],'big')
            if offset < 8+stride*int.from_bytes(data[4:8],'big') or length<32 or offset+length>len(data):
                raise ValueError('Invalid universal Mach-O slice.')
            slices.append(data[offset:offset+length])
        if len(slices)!=1:
            raise ValueError('Ambiguous universal Mach-O architecture.')
        return verify_binary_target(slices[0],target,minimum)
    endian = '<' if data[:4]==b'\xcf\xfa\xed\xfe' else '>'
    count, size = struct.unpack_from(endian+'II',data,16)
    if count>1024 or size>1048576 or 32+size>len(data):
        raise ValueError('Invalid Mach-O load commands.')
    cursor, versions = 32, []
    for _ in range(count):
        if cursor+8>32+size:
            raise ValueError('Invalid Mach-O load command bounds.')
        command,length = struct.unpack_from(endian+'II',data,cursor)
        if length<8 or length%4 or cursor+length>32+size:
            raise ValueError('Invalid Mach-O load command size.')
        if command==0x32:
            if length<24 or struct.unpack_from(endian+'I',data,cursor+8)[0]!=1:
                raise ValueError('Native binary does not target macOS.')
            versions.append(struct.unpack_from(endian+'I',data,cursor+12)[0])
        elif command==0x24:
            if length<16:
                raise ValueError('Invalid macOS version command.')
            versions.append(struct.unpack_from(endian+'I',data,cursor+8)[0])
        cursor+=length
    if cursor!=32+size or not versions or any((v>>16,(v>>8)&255,v&255)>declared for v in versions):
        raise ValueError('Binary minimum OS exceeds or cannot verify the advertised minimum OS.')


def verify_native_inputs(root: Path, manifest: dict, target: str, *, host_target=None) -> int:
    if target not in NATIVE_TARGETS or (host_target or current_target()) != target:
        raise ValueError('A matching native host and processor are required.')
    fields = {'schemaVersion','target','releaseVersion','minimumOS','sourceComplete','dependencyLockComplete','artifacts'}
    if not isinstance(manifest, dict) or set(manifest) != fields or manifest.get('target') != target:
        raise ValueError('Native manifest target or schema is invalid.')
    minimum = manifest['minimumOS']
    if not isinstance(minimum, str) or not re.fullmatch(r'[A-Za-z0-9 .+_-]{1,80}', minimum):
        raise ValueError('Native minimum OS evidence is invalid.')
    base = {k: v for k,v in manifest.items() if k not in ('target','minimumOS')}
    count = verify_release_inputs(root, base)
    required = {
        **{f'packaging/tools/native/{target}/bin/{name}':'media' for name in ('ffmpeg','ffprobe','yt-dlp')},
        f'packaging/native/{target}/corresponding-source.zip':'source',
        f'packaging/native/{target}/THIRD-PARTY-NOTICES.txt':'notice',
        f'packaging/platforms/{target}.lock':'lock',
        f'packaging/platforms/{target}.approved.json':'lock',
        f'packaging/tools/native/{target}/wheels/av-reviewed.whl':'compiler',
        **{f'worker/data/models/base/{name}':'model' for name in ('model.bin','config.json','tokenizer.json','vocabulary.txt')},
    }
    roles = {item['path']:item['role'] for item in manifest['artifacts']}
    if any(roles.get(path) != role for path,role in required.items()):
        raise ValueError('Native inventory requires every tool, model, lock, notice, reviewed decoder wheel and corresponding source.')
    return count


def verify_native_runtime(runtime=None):
    if runtime is None:
        runtime = (sys.implementation.name, tuple(sys.version_info[:3]), struct.calcsize('P')*8)
    if runtime != ('cpython', (3,12,15), 64):
        raise ValueError('Native release builds require reviewed CPython 3.12.15, 64 bit.')


def verify_native_approved(approved, target):
    if target not in NATIVE_TARGETS or not isinstance(approved,dict):
        raise ValueError('Invalid committed native dependency inventory.')
    from tools.dependency_policy import canonical
    policy = Path(__file__).resolve().parents[1]/f'packaging/platforms/{target}.dependencies.json'
    records = json.loads(policy.read_text())['packages']
    expected = {canonical(item['name']):item['version'] for item in records}
    if any(not isinstance(name,str) or not isinstance(version,str) for name,version in approved.items()):
        raise ValueError('Native versions differ from committed dependency policy.')
    actual = {canonical(name):version for name,version in approved.items()}
    if len(expected)!=len(records) or len(actual)!=len(approved) or actual!=expected:
        raise ValueError('Native versions differ from committed dependency policy.')


def verify_native_environment(root, manifest, target):
    count = verify_native_inputs(root, manifest, target)
    verify_native_runtime()
    root = Path(root)
    approved = json.loads((root/f'packaging/platforms/{target}.approved.json').read_text())
    verify_native_approved(approved,target)
    from tools.dependency_policy import verify_requirements, canonical
    if verify_requirements(root/f'packaging/platforms/{target}.lock', approved) != {canonical(name) for name in approved}:
        raise ValueError('Native build dependency lock is incomplete.')
    verify_build_inventory(approved)
    verify_native_pyav(root/f'packaging/tools/native/{target}/wheels/av-reviewed.whl')
    from worker.model_integrity import verify_model
    verify_model(root/'worker/data/models/base')
    for name in ('ffmpeg','ffprobe','yt-dlp'):
        path=root/f'packaging/tools/native/{target}/bin/{name}'
        if path.stat().st_size>200000000:raise ValueError('Native executable exceeds inspection limit.')
        verify_binary_target(path.read_bytes(),target,manifest['minimumOS'])
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', choices=sorted(NATIVE_TARGETS), required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.inputs.stat().st_size > 200000:
            raise ValueError('Native release manifest exceeds limits.')
        count = verify_native_environment(Path(__file__).resolve().parents[1], json.loads(args.inputs.read_text()), args.target)
    except (OSError, ValueError) as error:
        print('Native release blocked:', str(error))
        return 1
    print('Verified native release inputs:', count)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
