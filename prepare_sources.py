"""Restore a hash-checked public source archive and verify the supplied experimental patch."""
import argparse,hashlib,json,os,stat,subprocess,tempfile,urllib.request,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
COMMIT='9579030e5b1f0443c12b7376559560915b6d4c4b'
ARCHIVE_URL=f'https://codeload.github.com/seedds/nxnative/zip/{COMMIT}'
ARCHIVE_SHA256='9c9ff34da28e8875dccd9f4d655fc8db7c4655e60961bfee0ad7acd64a3f2b81'

def restore(archive,dest):
    archive=Path(archive);dest=Path(dest).resolve()
    if dest.exists():raise ValueError('Destination already exists; refusing to overwrite source')
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=ARCHIVE_SHA256:raise ValueError('Fixed source archive hash mismatch')
    prefix='nxnative-'+COMMIT+'/'
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            if not entry.filename.startswith(prefix):raise ValueError('Unexpected archive prefix')
            relative=Path(entry.filename[len(prefix):])
            if not relative.parts:continue
            target=(dest/relative).resolve()
            if dest not in target.parents:raise ValueError('Archive path escapes source directory')
            mode=entry.external_attr>>16
            if stat.S_ISLNK(mode):raise ValueError('Source archive contains a symbolic link')
            if entry.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(entry) as src,target.open('xb') as dst:
                while chunk:=src.read(1024*1024):dst.write(chunk)
            if os.name!='nt' and mode:target.chmod(mode&0o777)
    subprocess.run(['git','init','--quiet',str(dest)],check=True)
    subprocess.run(['git','-C',str(dest),'config','core.autocrlf','false'],check=True)
    patch=ROOT/'patches/nxnative_ios18.patch'
    for option in (['--check'],[]):
        subprocess.run(['git','-C',str(dest),'apply',*option,str(patch)],check=True)
    expected=json.loads((ROOT/'patches/ios18_sha256.json').read_text(encoding='utf-8'))
    for name,wanted in expected.items():
        path=(dest/name).resolve()
        if dest not in path.parents or not path.is_file():raise ValueError('Invalid patched file path')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=wanted:raise ValueError('Patched file hash mismatch: '+name)
    provenance=dict(upstream_commit=COMMIT,archive_sha256=ARCHIVE_SHA256,patched_files_verified=len(expected),source_only=True,git_commit_created=False)
    (dest/'CI_SOURCE_PROVENANCE.json').write_text(json.dumps(provenance,indent=2)+'\n',encoding='utf-8')
    return provenance

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--dest',type=Path,required=True);ap.add_argument('--archive',type=Path)
    args=ap.parse_args()
    if args.archive:print(json.dumps(restore(args.archive,args.dest)));return
    with tempfile.TemporaryDirectory(prefix='nxnative-source-') as temp:
        archive=Path(temp)/'source.zip'
        with urllib.request.urlopen(ARCHIVE_URL,timeout=90) as response,archive.open('wb') as out:
            while chunk:=response.read(1024*1024):out.write(chunk)
        print(json.dumps(restore(archive,args.dest)))

if __name__=='__main__':main()

