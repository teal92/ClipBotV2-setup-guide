"""Reject unexpected package names, versions, sources or unchecked artifacts."""
import json,re,sys
from pathlib import Path
def canonical(value):return re.sub(r'[-_.]+','-',value).lower()
def verify_requirements(path,approved):
 versions={canonical(k):v for k,v in approved.items()} if isinstance(approved,dict) else {}
 approved={canonical(v) for v in approved};seen=set();logical=[];pending=''
 for raw in Path(path).read_text(encoding='utf-8').splitlines():
  line=raw.strip()
  if not line or line.startswith('#'):continue
  pending+=line.rstrip('\\').strip()+' '
  if line.endswith('\\'):continue
  logical.append(pending.strip());pending=''
 if pending:raise ValueError('Incomplete dependency record.')
 for line in logical:
  if line in ('--require-hashes','--only-binary=:all:','--index-url https://pypi.org/simple'):continue
  match=re.fullmatch(r'([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.+-]*)\s+((?:--hash=sha256:[0-9a-f]{64}\s*)+)',line)
  if not match or canonical(match[1]) not in approved or canonical(match[1]) in seen:raise ValueError('Dependency outside reviewed policy.')
  if versions and versions[canonical(match[1])]!=match[2]:raise ValueError('Version outside reviewed policy.')
  seen.add(canonical(match[1]))
 if not seen:raise ValueError('Empty dependency lock.')
 return seen
if __name__=='__main__':
 root=Path(__file__).resolve().parents[1];approved=json.loads((root/'tools/approved-packages.json').read_text())
 print('Verified pinned, hash-locked build tools:',len(verify_requirements(root/'packaging/server-build-tools.lock',{'pip':'26.2.1'})))
 print('Verified pinned, hash-locked packages:',len(verify_requirements(root/'requirements.txt',approved)))
