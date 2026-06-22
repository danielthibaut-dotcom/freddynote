#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FREDDYnote v0.4 CLI - clean batch workflow, no placeholder paths, no pip dependencies."""
from __future__ import annotations
import argparse, csv, hashlib, json, os, shutil, sys, tempfile, time, traceback, zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from freddynote_engine import safe_title_from_filename, slugify, write_project_outputs, load_manifest

MODULE_ID = 'FREDDY.MUSIC.NOTATION.FREDDYNOTE.v0_4'
TOOL_DIR = Path(__file__).resolve().parent
INPUT_DROP = TOOL_DIR / 'INPUT_HIER_REIN'
OUTPUT_DEFAULT = TOOL_DIR / 'OUTPUT_PDF_HIER'
REQUIRED_FILES = ('freddy_midi_manifest.json', 'notes_only.csv')
SKIP_DIR_NAMES = {
    '__MACOSX','.git','.svn','.hg','.venv','venv','env','node_modules','__pycache__',
    'OUTPUT_PDF_HIER','OUTPUT_TEST','example_output','example_scan','example_output_from_zip',
    'notation_output','freddynote_output','build','dist','tmp','temp','_temp','_renders'
}

@dataclass
class Project:
    input_dir: Path
    root_dir: Path
    source_type: str = 'folder'
    source_archive: str = ''
    title: str = ''
    source_filename: str = ''
    source_sha256: str = ''
    project_id: str = ''
    warnings: List[str] = None  # type: ignore


def is_project_dir(p: Path) -> bool:
    return p.is_dir() and all((p/f).exists() for f in REQUIRED_FILES)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve()); return True
    except Exception: return False


def path_depth(root: Path, current: Path) -> int:
    try: return len(current.resolve().relative_to(root.resolve()).parts)
    except Exception: return 999


def project_meta(p: Path, root: Path, source_type='folder', source_archive='') -> Project:
    warnings: List[str] = []
    try:
        m = load_manifest(p/'freddy_midi_manifest.json')
    except Exception as exc:
        m = {}; warnings.append(f'manifest_read_error: {exc}')
    src = m.get('source', {}) if isinstance(m, dict) else {}
    filename = src.get('filename') or src.get('path') or p.name
    sha = src.get('sha256') or hashlib.sha256(str(p.resolve()).encode('utf-8','ignore')).hexdigest()
    title = safe_title_from_filename(filename)
    pid = sha[:16] if sha else hashlib.sha256(str(p.resolve()).encode()).hexdigest()[:16]
    # warnings for recommended files
    for recommended in ('normalized_events.csv',):
        if not (p/recommended).exists(): warnings.append(f'recommended_missing: {recommended}')
    return Project(p, root, source_type, source_archive, title, filename, sha, pid, warnings)


def scan_project_dirs(root: Path, *, max_depth: int = 20, exclude_dirs: Optional[List[Path]] = None, limit: int = 0) -> Tuple[List[Project], List[Dict[str, Any]]]:
    root = root.resolve()
    projects: List[Project] = []
    incomplete: List[Dict[str, Any]] = []
    exclude = [p.resolve() for p in (exclude_dirs or [])]
    if not root.exists(): return projects, [{'path': str(root), 'reason': 'input_path_not_found'}]
    if root.is_file() and root.suffix.lower()=='.zip':
        return scan_zip(root, limit=limit)
    if not root.is_dir(): return projects, [{'path': str(root), 'reason': 'input_not_directory_or_zip'}]
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath).resolve()
        if any(is_relative_to(current, ex) for ex in exclude):
            dirnames[:] = []; continue
        if path_depth(root, current) > max_depth:
            dirnames[:] = []; continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith('.') and not d.lower().startswith('freddynote_v')]
        names = set(filenames)
        if all(fn in names for fn in REQUIRED_FILES):
            projects.append(project_meta(current, root))
            dirnames[:] = []
            if limit and len(projects) >= limit: break
        elif any(fn in names for fn in REQUIRED_FILES):
            missing = [fn for fn in REQUIRED_FILES if fn not in names]
            incomplete.append({'path': str(current), 'reason': 'missing_required_files', 'missing': ';'.join(missing)})
    # Also scan zip files inside root as project collections
    if not limit or len(projects) < limit:
        for z in sorted(root.rglob('*.zip')):
            if any(part in SKIP_DIR_NAMES for part in z.parts): continue
            zp, zi = scan_zip(z, limit=max(0, limit-len(projects)) if limit else 0)
            projects.extend(zp); incomplete.extend(zi)
            if limit and len(projects) >= limit: break
    return projects, incomplete


def scan_zip(zip_path: Path, *, limit: int = 0) -> Tuple[List[Project], List[Dict[str, Any]]]:
    temp = Path(tempfile.mkdtemp(prefix='freddynote_zip_'))
    projects: List[Project] = []
    incomplete: List[Dict[str, Any]] = []
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(temp)
        dirs, inc = scan_project_dirs(temp, max_depth=20, exclude_dirs=[], limit=limit)
        for p in dirs:
            p.source_type = 'zip'; p.source_archive = str(zip_path)
        projects.extend(dirs); incomplete.extend(inc)
    except Exception as exc:
        incomplete.append({'path': str(zip_path), 'reason': f'zip_error: {exc}'})
    # temp is kept until rendering if these projects are returned; but currently returning paths in temp would be deleted if we delete now.
    # Therefore do not delete here. The OS temp can be cleaned later. FREDDYnote writes outputs immediately in the same run.
    return projects, incomplete


def find_auto_input() -> Tuple[Path, str]:
    INPUT_DROP.mkdir(parents=True, exist_ok=True)
    drop_projects, _ = scan_project_dirs(INPUT_DROP, max_depth=20, exclude_dirs=[], limit=1)
    if drop_projects:
        return INPUT_DROP, f'Drop-Zone gefunden: {INPUT_DROP}'
    # Search current FREDDY workspace outside the tool folder
    roots = [TOOL_DIR.parent, TOOL_DIR.parent.parent]
    seen = set()
    for r in roots:
        r = r.resolve()
        if str(r) in seen or not r.exists(): continue
        seen.add(str(r))
        projects, _ = scan_project_dirs(r, max_depth=12, exclude_dirs=[TOOL_DIR], limit=1)
        if projects:
            return r, f'Automatisch im FREDDY-Arbeitsbereich gefunden: {r}'
    # example fallback for test only is handled by test command
    raise FileNotFoundError(f'Keine FREDDY-Projekte gefunden. Lege Projektordner in {INPUT_DROP} ab oder nutze freddynote.py batch <Ordner>.')


def project_output_dir(project: Project, out_root: Path, mirror_tree: bool = True) -> Path:
    base = f"{slugify(project.title)}_{project.project_id[:8]}"
    if mirror_tree:
        try:
            rel_parent = project.input_dir.parent.resolve().relative_to(project.root_dir.resolve())
            if str(rel_parent) not in ('.',''):
                return out_root / rel_parent / base
        except Exception:
            pass
    return out_root / base


def should_skip(project: Project, out_dir: Path, resume: bool) -> bool:
    if not resume: return False
    mf = out_dir / 'freddy_notation_manifest.json'
    if not mf.exists(): return False
    try:
        data = json.loads(mf.read_text(encoding='utf-8'))
        if data.get('source', {}).get('sha256') == project.source_sha256 and data.get('status') in ('success','warning_no_notes'):
            pdfs = list(out_dir.glob('*_score.pdf'))
            return bool(pdfs)
    except Exception:
        return False
    return False


def write_rows_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for r in rows: w.writerow({k: r.get(k,'') for k in fields})


def make_zip(src_dir: Path, zip_path: Path):
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for p in src_dir.rglob('*'):
            if p.is_file() and p != zip_path:
                zf.write(p, p.relative_to(src_dir.parent))


def run_batch(input_path: Path, out_root: Path, *, track: Any='auto', resume=True, zip_output=True, mirror_tree=True, max_projects: int=0) -> Dict[str, Any]:
    out_root.mkdir(parents=True, exist_ok=True)
    # Only exclude the FREDDYnote tool folder when the selected input is OUTSIDE the tool folder.
    # This keeps the built-in drop-zone INPUT_HIER_REIN and the example_project_collection usable.
    exclude_dirs = [out_root]
    if not is_relative_to(input_path.resolve(), TOOL_DIR.resolve()):
        exclude_dirs.append(TOOL_DIR)
    projects, incomplete = scan_project_dirs(input_path, exclude_dirs=exclude_dirs, limit=max_projects)
    rows=[]
    started = time.time()
    for i, prj in enumerate(projects, start=1):
        od = project_output_dir(prj, out_root, mirror_tree)
        row = {'index': i, 'project_id': prj.project_id, 'title': prj.title, 'source_filename': prj.source_filename, 'input_dir': str(prj.input_dir), 'output_dir': str(od), 'source_archive': prj.source_archive, 'warnings': ';'.join(prj.warnings or [])}
        if should_skip(prj, od, resume):
            row.update({'status':'skipped_resume', 'pdf':'', 'notes':'', 'measures':'', 'chosen_track':''}); rows.append(row); continue
        try:
            result = write_project_outputs(prj.input_dir, od, track=track)
            row.update({'status': result.get('status','success'), 'pdf': result.get('pdf',''), 'notes': result.get('notes',''), 'measures': result.get('measures',''), 'chosen_track': result.get('chosen_track','')})
        except Exception as exc:
            od.mkdir(parents=True, exist_ok=True)
            (od/'FREDDYnote_ERROR.txt').write_text(traceback.format_exc(), encoding='utf-8')
            row.update({'status': 'error', 'error': str(exc), 'pdf':'', 'notes':'', 'measures':'', 'chosen_track':''})
        rows.append(row)
        print(f"[{i}/{len(projects)}] {row['status']}: {prj.title}")
    fields = ['index','status','project_id','title','source_filename','input_dir','output_dir','source_archive','warnings','error','pdf','notes','measures','chosen_track']
    write_rows_csv(out_root/'freddynote_batch_report.csv', rows, fields)
    write_rows_csv(out_root/'freddynote_incomplete_projects.csv', incomplete, ['path','reason','missing'])
    md = ['# FREDDYnote v0.4 Batch Report', '', f'- Input: `{input_path}`', f'- Output: `{out_root}`', f'- Projects found: `{len(projects)}`', f'- Incomplete folders: `{len(incomplete)}`', f'- Duration seconds: `{round(time.time()-started,2)}`', '', '## Results', '']
    for r in rows: md.append(f"- {r.get('status')}: {r.get('title')} -> `{r.get('pdf','')}`")
    (out_root/'freddynote_batch_report.md').write_text('\n'.join(md)+'\n', encoding='utf-8')
    manifest = {'module':'FREDDYnote','module_id':MODULE_ID,'schema_version':'0.4','input':str(input_path),'output':str(out_root),'projects_found':len(projects),'incomplete_found':len(incomplete),'created_at':time.strftime('%Y-%m-%dT%H:%M:%S'),'ollama_backlog':'All FREDDY components should later be locally orchestrated via OLLAMA.'}
    (out_root/'freddynote_batch_manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    if zip_output:
        make_zip(out_root, out_root.with_suffix('.zip'))
    return {'projects': len(projects), 'incomplete': len(incomplete), 'output': str(out_root), 'zip': str(out_root.with_suffix('.zip')) if zip_output else ''}


def cmd_selfcheck(args):
    print('FREDDYnote v0.4 selfcheck')
    print(f'Python: {sys.version.split()[0]}')
    print(f'Tool folder: {TOOL_DIR}')
    print('Dependencies: OK - no external Python packages required.')
    return 0


def cmd_scan(args):
    inp = Path(args.input) if args.input else find_auto_input()[0]
    exclude_dirs = [] if is_relative_to(inp.resolve(), TOOL_DIR.resolve()) else [TOOL_DIR]
    projects, incomplete = scan_project_dirs(inp, exclude_dirs=exclude_dirs)
    out = Path(args.out or (TOOL_DIR/'OUTPUT_SCAN')); out.mkdir(parents=True, exist_ok=True)
    rows=[{'project_id':p.project_id,'title':p.title,'source_filename':p.source_filename,'input_dir':str(p.input_dir),'source_archive':p.source_archive,'warnings':';'.join(p.warnings or [])} for p in projects]
    write_rows_csv(out/'freddynote_project_scan.csv', rows, ['project_id','title','source_filename','input_dir','source_archive','warnings'])
    (out/'freddynote_project_scan.json').write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding='utf-8')
    write_rows_csv(out/'freddynote_incomplete_projects.csv', incomplete, ['path','reason','missing'])
    print(f'Gefundene Projekte: {len(projects)}')
    print(f'Scan-Ausgabe: {out}')
    return 0


def cmd_batch(args):
    inp = Path(args.input) if args.input else find_auto_input()[0]
    out = Path(args.out or OUTPUT_DEFAULT)
    result = run_batch(inp, out, track=args.track, resume=args.resume, zip_output=args.zip_output, mirror_tree=args.mirror_tree, max_projects=args.max_projects)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_auto(args):
    inp, reason = find_auto_input()
    print(reason)
    out = Path(args.out or OUTPUT_DEFAULT)
    result = run_batch(inp, out, track=args.track, resume=args.resume, zip_output=args.zip_output, mirror_tree=args.mirror_tree, max_projects=args.max_projects)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_test(args):
    inp = TOOL_DIR/'example_project_collection'
    out = Path(args.out or (TOOL_DIR/'OUTPUT_TEST'))
    if out.exists(): shutil.rmtree(out)
    result = run_batch(inp, out, track='auto', resume=False, zip_output=True, mirror_tree=True, max_projects=0)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def build_parser():
    p = argparse.ArgumentParser(description='FREDDYnote v0.4 - batch notation PDF export, no pip dependencies')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('selfcheck')
    ps = sub.add_parser('scan'); ps.add_argument('input', nargs='?'); ps.add_argument('--out')
    pb = sub.add_parser('batch'); pb.add_argument('input', nargs='?'); pb.add_argument('--out'); pb.add_argument('--track', default='all', help='auto=Melodie-Spur, all=alle Spuren einzeln, oder Spurzahl'); pb.add_argument('--resume', action='store_true'); pb.add_argument('--zip-output', action='store_true'); pb.add_argument('--mirror-tree', action='store_true'); pb.add_argument('--max-projects', type=int, default=0)
    pa = sub.add_parser('auto'); pa.add_argument('--out'); pa.add_argument('--track', default='all', help='auto=Melodie-Spur, all=alle Spuren einzeln, oder Spurzahl'); pa.add_argument('--resume', action='store_true'); pa.add_argument('--zip-output', action='store_true'); pa.add_argument('--mirror-tree', action='store_true'); pa.add_argument('--max-projects', type=int, default=0)
    pt = sub.add_parser('test'); pt.add_argument('--out')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == 'selfcheck': return cmd_selfcheck(args)
    if args.cmd == 'scan': return cmd_scan(args)
    if args.cmd == 'batch': return cmd_batch(args)
    if args.cmd == 'auto': return cmd_auto(args)
    if args.cmd == 'test': return cmd_test(args)
    return 2

if __name__ == '__main__':
    raise SystemExit(main())
