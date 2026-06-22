#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FREDDYnote Engine v0.4

Dependency-free FREDDY MIDI-ASCII -> draft notation renderer.
Uses Python standard library only. No pip install, no ReportLab dependency.

Purpose:
- Scan FREDDY MIDI ASCII Bridge project folders.
- Convert notes_only.csv + freddy_midi_manifest.json into:
  - notation_model.json
  - score.musicxml
  - score.pdf (built-in vector PDF writer)
  - notation_report.md

Backlog anchor:
- All FREDDY components should later be locally orchestrated via OLLAMA.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

GRID_DEFAULT = 256
A4_W, A4_H = 595.275590551, 841.88976378
LETTER_INDEX = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}
KEY_FIFTHS = {"Cb": -7, "Gb": -6, "Db": -5, "Ab": -4, "Eb": -3, "Bb": -2, "F": -1, "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7}
KEY_CHROMA_SPELLINGS = {
    "C":  {0:("C",0),1:("C",1),2:("D",0),3:("D",1),4:("E",0),5:("F",0),6:("F",1),7:("G",0),8:("G",1),9:("A",0),10:("A",1),11:("B",0)},
    "G":  {0:("C",0),1:("C",1),2:("D",0),3:("D",1),4:("E",0),5:("F",0),6:("F",1),7:("G",0),8:("G",1),9:("A",0),10:("A",1),11:("B",0)},
    "D":  {0:("C",0),1:("C",1),2:("D",0),3:("D",1),4:("E",0),5:("F",0),6:("F",1),7:("G",0),8:("G",1),9:("A",0),10:("A",1),11:("B",0)},
    "F":  {0:("C",0),1:("D",-1),2:("D",0),3:("E",-1),4:("E",0),5:("F",0),6:("G",-1),7:("G",0),8:("A",-1),9:("A",0),10:("B",-1),11:("B",0)},
    "Bb": {0:("C",0),1:("D",-1),2:("D",0),3:("E",-1),4:("E",0),5:("F",0),6:("G",-1),7:("G",0),8:("A",-1),9:("A",0),10:("B",-1),11:("B",0)},
    "Eb": {0:("C",0),1:("D",-1),2:("D",0),3:("E",-1),4:("E",0),5:("F",0),6:("G",-1),7:("G",0),8:("A",-1),9:("A",0),10:("B",-1),11:("B",0)},
}

@dataclass
class MeasureInfo:
    number: int
    start_tick: int
    end_tick: int
    numerator: int
    denominator: int
    key_signature: str


def safe_title_from_filename(filename: str) -> str:
    base = Path(filename or "Untitled").stem
    base = re.sub(r"\s*\[[^\]]+\]\s*", " ", base)
    base = re.sub(r"[_-]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    return base or "Untitled MIDI Score"


def slugify(value: str, max_len: int = 80) -> str:
    value = str(value or "").strip() or "untitled"
    value = re.sub(r"\s*\[[^\]]+\]\s*", " ", value)
    value = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß._ -]+", " ", value)
    value = re.sub(r"[\s._-]+", "_", value).strip("_") or "untitled"
    return value[:max_len].strip("_") or "untitled"


def load_manifest(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_notes(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def to_int(row: Dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(round(float(row.get(key, default) or default)))
    except Exception:
        return default


def to_bool(value: Any) -> bool:
    if isinstance(value, bool): return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "ja"}


def quantize_tick(tick: int, grid: int) -> int:
    return int(round(tick / grid) * grid)


def load_key_map(normalized_path: Optional[Path]) -> List[Dict[str, Any]]:
    if not normalized_path or not normalized_path.exists():
        return [{"tick": 0, "key": "C"}]
    out = []
    try:
        with normalized_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("event_type") == "key_signature":
                    key = row.get("key_signature") or row.get("key") or row.get("data") or "C"
                    tick = to_int(row, "absolute_tick", to_int(row, "tick", 0))
                    out.append({"tick": tick, "key": key})
    except Exception:
        pass
    return sorted(out, key=lambda x: int(x["tick"])) or [{"tick": 0, "key": "C"}]


def key_at_tick(key_map: List[Dict[str, Any]], tick: int) -> str:
    key = key_map[0].get("key", "C") if key_map else "C"
    for ev in key_map:
        if int(ev.get("tick", 0)) <= tick:
            key = ev.get("key", key)
        else:
            break
    return key or "C"


def build_measure_map(manifest: Dict[str, Any], last_tick: int, key_map: List[Dict[str, Any]]) -> List[MeasureInfo]:
    ppq = int(manifest.get("midi_header", {}).get("ticks_per_quarter", 1024) or 1024)
    ts_map = sorted(manifest.get("time_signature_map", []) or [], key=lambda x: int(x.get("tick", 0)))
    if not ts_map:
        ts_map = [{"tick": 0, "numerator": 4, "denominator": 4}]
    end_global = max(last_tick + ppq * 2, ppq * 4)
    measures: List[MeasureInfo] = []
    bar_no = 1
    for idx, ts in enumerate(ts_map):
        start_tick = int(ts.get("tick", 0))
        end_tick = int(ts_map[idx + 1].get("tick", end_global)) if idx + 1 < len(ts_map) else end_global
        num = int(ts.get("numerator", 4) or 4)
        den = int(ts.get("denominator", 4) or 4)
        measure_len = max(1, int(ppq * num * 4 / den))
        t = start_tick
        while t < end_tick:
            measures.append(MeasureInfo(bar_no, t, min(t + measure_len, end_tick), num, den, key_at_tick(key_map, t)))
            t += measure_len
            bar_no += 1
    return measures or [MeasureInfo(1, 0, ppq*4, 4, 4, key_at_tick(key_map, 0))]


def measure_at_tick(measures: List[MeasureInfo], tick: int) -> MeasureInfo:
    lo, hi = 0, len(measures)-1
    while lo <= hi:
        mid = (lo+hi)//2
        m = measures[mid]
        if m.start_tick <= tick < m.end_tick: return m
        if tick < m.start_tick: hi = mid-1
        else: lo = mid+1
    return measures[-1]


def midi_to_spelling(midi_note: int, key: str) -> Tuple[str, int, int, str]:
    chroma = midi_note % 12
    octave = midi_note // 12 - 1
    step, alter = KEY_CHROMA_SPELLINGS.get(key, KEY_CHROMA_SPELLINGS["C"]).get(chroma, ("C", 0))
    acc = "#" if alter > 0 else "b" if alter < 0 else ""
    return step, alter, octave, f"{step}{acc}{octave}"


def choose_track_auto(rows: List[Dict[str, Any]]) -> Optional[int]:
    counts: Dict[int, int] = defaultdict(int)
    for r in rows:
        track = to_int(r, "track", 0)
        channel = to_int(r, "channel", 0)
        is_drum = to_bool(r.get("is_drum", False)) or channel == 9
        if not is_drum:
            counts[track] += 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def build_track_meta(events: List[Dict[str, Any]], melody_track: Optional[int]) -> List[Dict[str, Any]]:
    counts: Dict[int, int] = defaultdict(int)
    for n in events:
        if not n.get("is_drum"):
            counts[n["track"]] += 1
    if not counts:
        return []
    top = max(counts, key=lambda t: counts[t])
    melody = melody_track if melody_track is not None else top
    result = []
    for tr in sorted(counts):
        pitches = [n["midi_note"] for n in events if n["track"] == tr]
        median_pitch = sorted(pitches)[len(pitches) // 2] if pitches else 60
        clef = "treble" if median_pitch >= 55 else "bass"
        is_mel = (tr == melody)
        label = "Melodie" if is_mel else f"Spur {tr}"
        result.append({"track": tr, "label": label, "is_melody": is_mel, "clef": clef, "note_count": counts[tr]})
    result.sort(key=lambda t: (0 if t["is_melody"] else 1, t["track"]))
    return result


def build_notation_model(notes_rows: List[Dict[str, Any]], manifest: Dict[str, Any], normalized_path: Optional[Path], grid: int = GRID_DEFAULT, track: Any = "auto", include_drums: bool = False, title: Optional[str] = None) -> Dict[str, Any]:
    ppq = int(manifest.get("midi_header", {}).get("ticks_per_quarter", 1024) or 1024)
    source = manifest.get("source", {}) or {}
    source_filename = source.get("filename") or source.get("path") or "unknown.mid"
    title = title or safe_title_from_filename(source_filename)
    if track == "auto":
        chosen_track = choose_track_auto(notes_rows)
    elif track in (None, "all", -1):
        chosen_track = None
    else:
        chosen_track = int(track)
    last_tick = max([to_int(r, "end_tick", 0) for r in notes_rows] or [0])
    key_map = load_key_map(normalized_path)
    measures = build_measure_map(manifest, last_tick, key_map)
    q_errors = []
    events = []
    filtered = 0
    for idx, r in enumerate(notes_rows):
        midi = to_int(r, "note", to_int(r, "midi_note", 60))
        tr = to_int(r, "track", 0)
        ch = to_int(r, "channel", 0)
        is_drum = to_bool(r.get("is_drum", False)) or ch == 9
        if chosen_track is not None and tr != chosen_track:
            filtered += 1; continue
        if is_drum and not include_drums:
            filtered += 1; continue
        start = to_int(r, "start_tick", 0)
        end = to_int(r, "end_tick", start + ppq)
        dur = max(1, end - start)
        q_start = quantize_tick(start, grid)
        q_dur = max(grid, quantize_tick(dur, grid))
        q_end = q_start + q_dur
        m = measure_at_tick(measures, q_start)
        key = key_at_tick(key_map, q_start)
        step, alter, octave, display = midi_to_spelling(midi, key)
        staff = "treble" if midi >= 60 else "bass"
        events.append({
            "note_event_id": to_int(r, "note_event_id", idx),
            "track": tr, "channel": ch, "is_drum": is_drum,
            "midi_note": midi, "velocity": to_int(r, "velocity", 0),
            "start_tick": start, "end_tick": end, "duration_ticks": dur,
            "q_start_tick": q_start, "q_end_tick": q_end, "q_duration_ticks": q_dur,
            "measure": m.number, "beat": round(1.0 + (q_start - m.start_tick) / ppq, 4),
            "staff": staff, "step": step, "alter": alter, "octave": octave, "display_name": display,
        })
        q_errors.append(abs(start - q_start))
    events.sort(key=lambda n: (n["q_start_tick"], n["staff"], n["midi_note"]))
    used_nums = sorted({n["measure"] for n in events})
    md = {m.number: m for m in measures}
    used_measures = [asdict(md[n]) for n in used_nums if n in md]
    tempo_map = manifest.get("tempo_map", []) or []
    track_meta = build_track_meta(events, chosen_track)
    model = {
        "module": "FREDDYnote Engine",
        "module_id": "FREDDY.MUSIC.NOTATION.FREDDYNOTE.v0_4",
        "schema_version": "0.4",
        "title": title,
        "source": source,
        "ppq": ppq,
        "tempo_map": tempo_map,
        "time_signature_map": manifest.get("time_signature_map", []) or [],
        "key_signature_map": key_map,
        "filters": {"requested_track": track, "chosen_track": chosen_track, "include_drums": include_drums, "filtered_out_notes": filtered},
        "quantization": {"grid_ticks": grid, "grid_label": f"{grid} ticks", "max_start_error_ticks": max(q_errors or [0]), "mean_start_error_ticks": round(sum(q_errors)/len(q_errors), 2) if q_errors else 0},
        "measures": used_measures,
        "notes": events,
        "tracks": track_meta,
        "warnings": [],
        "ollama_backlog": "Later local orchestration of all FREDDY components via OLLAMA.",
    }
    if not events:
        model["warnings"].append("No note events remained after filtering.")
    return model


def duration_type(duration_ticks: int, ppq: int) -> str:
    q = duration_ticks / max(1, ppq)
    if q >= 4: return "whole"
    if q >= 2: return "half"
    if q >= 1: return "quarter"
    if q >= .5: return "eighth"
    return "16th"


def write_musicxml(model: Dict[str, Any], path: Path) -> None:
    ppq = int(model.get("ppq", 1024))
    divisions = 4
    def xd(ticks: int) -> int: return max(1, int(round(ticks / ppq * divisions)))
    measures = {m["number"]: m for m in model.get("measures", [])}
    notes = model.get("notes", [])
    tracks_meta = model.get("tracks", [])

    # Multi-track: one part per MIDI track; single-track: treble/bass split
    if len(tracks_meta) > 1:
        parts = [(f"P{i+1}", t["label"], t["track"], t["clef"]) for i, t in enumerate(tracks_meta)]
        by_part_measure: Dict[Tuple[Any, int], List[Dict[str, Any]]] = defaultdict(list)
        for n in notes:
            by_part_measure[(n["track"], n["measure"])].append(n)
        part_key = lambda pid, mnum: by_part_measure.get((pid, mnum), [])
    else:
        parts = [("P1", "Treble", "treble", "treble"), ("P2", "Bass", "bass", "bass")]
        by_staff_measure: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
        for n in notes:
            by_staff_measure[(n["staff"], n["measure"])].append(n)
        part_key = lambda pid, mnum: by_staff_measure.get((pid, mnum), [])

    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="no"?>', '<score-partwise version="3.1">']
    lines.append(f'  <work><work-title>{xml_escape(model.get("title", "Untitled"))}</work-title></work>')
    lines.append('  <identification><creator type="software">FREDDYnote Engine v0.4</creator></identification>')
    lines.append('  <part-list>')
    for pid, name, _key, _clef in parts:
        lines.append(f'    <score-part id="{pid}"><part-name>{name}</part-name></score-part>')
    lines.append('  </part-list>')
    first_measure = min(measures) if measures else 1
    for pid, _name, part_key_val, clef in parts:
        lines.append(f'  <part id="{pid}">')
        last_sig = None
        for mnum in sorted(measures):
            m = measures[mnum]
            lines.append(f'    <measure number="{mnum}">')
            sig = (m.get("key_signature", "C"), m.get("numerator", 4), m.get("denominator", 4))
            if mnum == first_measure or sig != last_sig:
                lines.append('      <attributes>')
                lines.append(f'        <divisions>{divisions}</divisions>')
                lines.append(f'        <key><fifths>{KEY_FIFTHS.get(m.get("key_signature", "C"), 0)}</fifths></key>')
                lines.append(f'        <time><beats>{m.get("numerator",4)}</beats><beat-type>{m.get("denominator",4)}</beat-type></time>')
                lines.append(f'        <clef><sign>{"G" if clef == "treble" else "F"}</sign><line>{"2" if clef == "treble" else "4"}</line></clef>')
                lines.append('      </attributes>')
                last_sig = sig
            grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
            part_notes = by_part_measure.get((part_key_val, mnum), []) if len(tracks_meta) > 1 else by_staff_measure.get((part_key_val, mnum), [])  # type: ignore
            for n in sorted(part_notes, key=lambda x: (x["q_start_tick"], x["midi_note"])):
                grouped[n["q_start_tick"]].append(n)
            cursor = m.get("start_tick", 0)
            for start in sorted(grouped):
                if start > cursor:
                    rest_ticks = start - cursor
                    lines.extend(['      <note>', '        <rest/>', f'        <duration>{xd(rest_ticks)}</duration>', f'        <type>{duration_type(rest_ticks, ppq)}</type>', '      </note>'])
                chord = grouped[start]
                dur = max(n["q_duration_ticks"] for n in chord)
                for i, n in enumerate(chord):
                    lines.append('      <note>')
                    if i > 0: lines.append('        <chord/>')
                    lines.append('        <pitch>')
                    lines.append(f'          <step>{n["step"]}</step>')
                    if int(n.get("alter", 0)): lines.append(f'          <alter>{int(n["alter"])}</alter>')
                    lines.append(f'          <octave>{n["octave"]}</octave>')
                    lines.append('        </pitch>')
                    lines.append(f'        <duration>{xd(dur)}</duration>')
                    lines.append(f'        <type>{duration_type(dur, ppq)}</type>')
                    lines.append('      </note>')
                cursor = max(cursor, start + dur)
            if cursor < m.get("end_tick", cursor):
                rest_ticks = m["end_tick"] - cursor
                lines.extend(['      <note>', '        <rest/>', f'        <duration>{xd(rest_ticks)}</duration>', f'        <type>{duration_type(rest_ticks, ppq)}</type>', '      </note>'])
            lines.append('    </measure>')
        lines.append('  </part>')
    lines.append('</score-partwise>')
    path.write_text("\n".join(lines), encoding="utf-8")


def pdf_escape(s: str) -> str:
    return str(s).replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')

class SimplePDF:
    def __init__(self, width: float = A4_W, height: float = A4_H):
        self.width = width; self.height = height; self.pages: List[List[str]] = []; self.current: List[str] = []
    def new_page(self):
        if self.current: self.pages.append(self.current)
        self.current = []
    def _add(self, s: str): self.current.append(s)
    def line(self, x1, y1, x2, y2, w=0.7): self._add(f"{w:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")
    def text(self, x, y, text, size=10, bold=False, align='left'):
        font = '/F2' if bold else '/F1'; s = pdf_escape(text)
        if align == 'center':
            # simple approximate centering
            x = x - len(str(text)) * size * 0.24
        elif align == 'right':
            x = x - len(str(text)) * size * 0.48
        self._add(f"BT {font} {size:.1f} Tf {x:.2f} {y:.2f} Td ({s}) Tj ET")
    def rect(self, x, y, w, h, fill=False, stroke=True):
        op = 'B' if fill and stroke else 'f' if fill else 'S'
        self._add(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re {op}")
    def ellipse(self, x, y, rx, ry, fill=True):
        # draw transformed unit circle with bezier curves
        k = 0.5522847498
        pts = [
            (x+rx, y, x+rx, y+k*ry, x+k*rx, y+ry, x, y+ry),
            (x, y+ry, x-k*rx, y+ry, x-rx, y+k*ry, x-rx, y),
            (x-rx, y, x-rx, y-k*ry, x-k*rx, y-ry, x, y-ry),
            (x, y-ry, x+k*rx, y-ry, x+rx, y-k*ry, x+rx, y),
        ]
        self._add(f"{x+rx:.2f} {y:.2f} m")
        for p in pts:
            _, _, x1,y1,x2,y2,x3,y3 = (0,0)+p[2:] if False else (None,None,*p[2:])
        # easier explicit
        self._add(f"{x+rx:.2f} {y+k*ry:.2f} {x+k*rx:.2f} {y+ry:.2f} {x:.2f} {y+ry:.2f} c")
        self._add(f"{x-k*rx:.2f} {y+ry:.2f} {x-rx:.2f} {y+k*ry:.2f} {x-rx:.2f} {y:.2f} c")
        self._add(f"{x-rx:.2f} {y-k*ry:.2f} {x-k*rx:.2f} {y-ry:.2f} {x:.2f} {y-ry:.2f} c")
        self._add(f"{x+k*rx:.2f} {y-ry:.2f} {x+rx:.2f} {y-k*ry:.2f} {x+rx:.2f} {y:.2f} c {'f' if fill else 'S'}")
    def write(self, path: Path):
        if self.current: self.pages.append(self.current); self.current = []
        if not self.pages: self.pages = [[]]
        objects = []
        def obj(s: bytes) -> int:
            objects.append(s); return len(objects)
        font1 = obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        font2 = obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
        page_ids = []
        content_ids = []
        # placeholder pages object id will be after contents? pre-create via raw order no issue
        for page in self.pages:
            stream = ("\n".join(page)).encode('latin-1', 'replace')
            content_id = obj(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
            content_ids.append(content_id)
            page_ids.append(None)
        pages_id = len(objects) + len(page_ids) + 1
        # page objs
        for content_id in content_ids:
            page_id = obj(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {self.width:.2f} {self.height:.2f}] /Resources << /Font << /F1 {font1} 0 R /F2 {font2} 0 R >> >> /Contents {content_id} 0 R >>".encode())
            page_ids[content_ids.index(content_id)] = page_id
        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        pages_obj_id = obj(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
        catalog_id = obj(f"<< /Type /Catalog /Pages {pages_obj_id} 0 R >>".encode())
        # write xref
        out = bytearray(b"%PDF-1.4\n%FREDDYnote v0.4\n")
        offsets = [0]
        for i, data in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + data + b"\nendobj\n"
        xref_pos = len(out)
        out += f"xref\n0 {len(objects)+1}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]: out += f"{off:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {len(objects)+1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
        path.write_bytes(out)


def diatonic_pos(step: str, octave: int, bottom_step: str, bottom_octave: int) -> int:
    return (octave * 7 + LETTER_INDEX.get(step, 0)) - (bottom_octave * 7 + LETTER_INDEX.get(bottom_step, 0))


def draw_staff(pdf: SimplePDF, x: float, y: float, w: float, spacing: float):
    for i in range(5): pdf.line(x, y + i*spacing, x+w, y+i*spacing, 0.6)


def draw_note(pdf: SimplePDF, x: float, y: float, staff_bottom: float, spacing: float, duration: int, ppq: int, alter: int):
    # ledger lines
    pos = round((y - staff_bottom) / (spacing/2))
    if pos < 0:
        p = 0
        while p >= pos:
            if p % 2 == 0: pdf.line(x-6, staff_bottom + p*spacing/2, x+8, staff_bottom + p*spacing/2, 0.55)
            p -= 2
    elif pos > 8:
        p = 10
        while p <= pos:
            if p % 2 == 0: pdf.line(x-6, staff_bottom + p*spacing/2, x+8, staff_bottom + p*spacing/2, 0.55)
            p += 2
    if alter: pdf.text(x-13, y-3.5, '#' if alter > 0 else 'b', 8)
    filled = duration < ppq*2
    pdf.ellipse(x, y, 4.2, 3.0, fill=filled)
    # stem for quarter/eighth/16th
    if duration <= ppq:
        stem_up = y < staff_bottom + spacing*2
        if stem_up:
            pdf.line(x+4, y, x+4, y+26, 0.7)
            if duration < ppq: pdf.line(x+4, y+26, x+13, y+20, 1.0)
        else:
            pdf.line(x-4, y, x-4, y-26, 0.7)
            if duration < ppq: pdf.line(x-4, y-26, x+5, y-20, 1.0)


def render_pdf(model: Dict[str, Any], path: Path):
    pdf = SimplePDF()
    title = model.get('title', 'Untitled')
    ppq = int(model.get('ppq', 1024))
    notes = model.get('notes', [])
    measures = model.get('measures', [])
    tracks_meta = model.get('tracks', [])
    tempo = model.get('tempo_map', [{}])[0].get('bpm', '-') if model.get('tempo_map') else '-'
    sha = str(model.get('source', {}).get('sha256', ''))[:12]
    margin_x = 42; top = 66; spacing = 6.2
    usable_w = A4_W - margin_x * 2
    label_w = 52; staff_x = margin_x + label_w
    measures_per_system = 4
    measure_w = (usable_w - label_w) / measures_per_system

    multi = len(tracks_meta) > 1
    if multi:
        staff_h = 38       # height per track staff in a system
        system_h = len(tracks_meta) * staff_h + 18
        systems_per_page = max(1, int((A4_H - top - 60) / system_h))
    else:
        system_h = 118; systems_per_page = 6

    def page_header(page_no: int):
        pdf.text(A4_W/2, A4_H-34, title, 20, True, 'center')
        pdf.text(A4_W/2, A4_H-50, 'FREDDYnote v0.4 - dependency-free draft score', 8.5, False, 'center')
        track_info = f'Alle Spuren ({len(tracks_meta)})' if multi else f'Spur: {model.get("filters",{}).get("chosen_track")}'
        pdf.text(A4_W/2, A4_H-62, f'Tempo: {tempo} bpm | {track_info} | SHA: {sha}', 7.5, False, 'center')
        pdf.text(A4_W-margin_x, 22, f'Seite {page_no} | FREDDYnote', 7, False, 'right')

    page_no = 1; pdf.new_page(); page_header(page_no); sys_on_page = 0

    if multi:
        # Group notes per track and measure
        by_track_measure: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
        for n in notes:
            by_track_measure[(n['track'], int(n['measure']))].append(n)

        for idx in range(0, len(measures), measures_per_system):
            if sys_on_page >= systems_per_page:
                page_no += 1; pdf.new_page(); page_header(page_no); sys_on_page = 0
            sys_meas = measures[idx:idx+measures_per_system]
            y_sys_top = A4_H - top - 50 - sys_on_page * system_h

            for ti, tmeta in enumerate(tracks_meta):
                tr_no = tmeta['track']
                clef = tmeta['clef']
                label = tmeta['label']
                staff_bottom = y_sys_top - ti * staff_h - 24

                draw_staff(pdf, staff_x, staff_bottom, usable_w - label_w, spacing)
                # Track label left of staff
                pdf.text(margin_x, staff_bottom + spacing * 2, label, 7, tmeta['is_melody'])

                # Clef symbol
                pdf.text(staff_x + 2, staff_bottom + 6, 'G' if clef == 'treble' else 'F', 14, True)

                for mi, m in enumerate(sys_meas):
                    mx = staff_x + mi * measure_w
                    pdf.line(mx, staff_bottom, mx, staff_bottom + 4 * spacing, 0.55)
                    if ti == 0:
                        pdf.text(mx + 2, staff_bottom + 4 * spacing + 6, str(m['number']), 6)
                    if (mi == 0 or idx == 0) and ti == 0:
                        pdf.text(mx + 14, staff_bottom + 4 * spacing + 6, f"{m.get('numerator',4)}/{m.get('denominator',4)}", 7, True)

                end_x = staff_x + len(sys_meas) * measure_w
                pdf.line(end_x, staff_bottom, end_x, staff_bottom + 4 * spacing, 0.7)

                ref_step = 'E' if clef == 'treble' else 'G'
                ref_oct = 4 if clef == 'treble' else 2
                for mi, m in enumerate(sys_meas):
                    mx = staff_x + mi * measure_w
                    mlen = max(1, m['end_tick'] - m['start_tick'])
                    for n in by_track_measure.get((tr_no, m['number']), []):
                        rel = (n['q_start_tick'] - m['start_tick']) / mlen
                        x = mx + 14 + rel * (measure_w - 26)
                        pos = diatonic_pos(n['step'], int(n['octave']), ref_step, ref_oct)
                        y = staff_bottom + pos * spacing / 2
                        draw_note(pdf, x, y, staff_bottom, spacing, int(n['q_duration_ticks']), ppq, int(n.get('alter', 0)))

            sys_on_page += 1
    else:
        # Single-track: original treble/bass layout
        notes_by_measure: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for n in notes:
            notes_by_measure[int(n['measure'])].append(n)

        for idx in range(0, len(measures), measures_per_system):
            if sys_on_page >= systems_per_page:
                page_no += 1; pdf.new_page(); page_header(page_no); sys_on_page = 0
            sys_meas = measures[idx:idx+measures_per_system]
            y_top = A4_H - top - 50 - sys_on_page * system_h
            treble_bottom = y_top - 20; bass_bottom = y_top - 80
            draw_staff(pdf, staff_x, treble_bottom, usable_w - label_w, spacing)
            draw_staff(pdf, staff_x, bass_bottom, usable_w - label_w, spacing)
            pdf.line(staff_x - 8, bass_bottom, staff_x - 8, treble_bottom + 4 * spacing, 0.7)
            pdf.text(margin_x + 4, treble_bottom + 8, 'G', 18, True)
            pdf.text(margin_x + 5, bass_bottom + 8, 'F', 18, True)
            for mi, m in enumerate(sys_meas):
                mx = staff_x + mi * measure_w
                pdf.line(mx, bass_bottom, mx, treble_bottom + 4 * spacing, 0.65)
                pdf.line(mx, bass_bottom + 4 * spacing, mx, treble_bottom, 0.65)
                pdf.text(mx + 2, treble_bottom + 4 * spacing + 8, str(m['number']), 6)
                if mi == 0 or idx == 0:
                    pdf.text(mx + 12, treble_bottom + 4 * spacing + 8, f"{m.get('numerator',4)}/{m.get('denominator',4)}", 7, True)
                    pdf.text(mx + 12, bass_bottom - 12, f"Key: {m.get('key_signature','C')}", 6.5)
            end_x = staff_x + len(sys_meas) * measure_w
            pdf.line(end_x, bass_bottom, end_x, treble_bottom + 4 * spacing, 0.8)
            pdf.line(end_x, bass_bottom + 4 * spacing, end_x, treble_bottom, 0.8)
            for mi, m in enumerate(sys_meas):
                mx = staff_x + mi * measure_w
                mlen = max(1, m['end_tick'] - m['start_tick'])
                for n in notes_by_measure.get(m['number'], []):
                    rel = (n['q_start_tick'] - m['start_tick']) / mlen
                    x = mx + 14 + rel * (measure_w - 26)
                    if n['staff'] == 'treble':
                        pos = diatonic_pos(n['step'], int(n['octave']), 'E', 4)
                        y = treble_bottom + pos * spacing / 2; sb = treble_bottom
                    else:
                        pos = diatonic_pos(n['step'], int(n['octave']), 'G', 2)
                        y = bass_bottom + pos * spacing / 2; sb = bass_bottom
                    draw_note(pdf, x, y, sb, spacing, int(n['q_duration_ticks']), ppq, int(n.get('alter', 0)))
            sys_on_page += 1
    pdf.write(path)


def write_report(model: Dict[str, Any], path: Path, pdf_name: str, xml_name: str):
    notes = model.get('notes', []); measures = model.get('measures', [])
    tracks = sorted({n.get('track') for n in notes})
    content = f"""# FREDDYnote v0.4 - Notation Report

## Source

- Title: `{model.get('title')}`
- Source filename: `{model.get('source', {}).get('filename', 'unknown')}`
- Source SHA-256: `{model.get('source', {}).get('sha256', 'unknown')}`

## Outputs

- PDF score: `{pdf_name}`
- MusicXML: `{xml_name}`
- Notation model: `notation_model.json`

## Imported material

- Rendered notes: `{len(notes)}`
- Rendered measures: `{len(measures)}`
- Tracks used: `{tracks}`
- Chosen track: `{model.get('filters', {}).get('chosen_track')}`
- Filtered-out notes: `{model.get('filters', {}).get('filtered_out_notes')}`

## Engine

- Renderer: `dependency_free_builtin_pdf_writer`
- External Python packages: `none`
- OLLAMA backlog: all FREDDY components should later be locally orchestrated via OLLAMA.

## Notes

This is a MIDI-derived draft notation layer. It is intentionally robust for batch export and does not require pip, ReportLab, MuseScore or LilyPond. Professional engraving remains a later FREDDYnote step.

## Warnings

"""
    warnings = model.get('warnings', [])
    content += "\n".join(f"- {w}" for w in warnings) if warnings else "- No blocking warnings."
    path.write_text(content+"\n", encoding='utf-8')


def write_project_outputs(input_dir: Path, out_dir: Path, *, track: Any = 'auto', include_drums: bool = False, grid: int = GRID_DEFAULT, title: Optional[str] = None) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = input_dir / 'freddy_midi_manifest.json'
    notes_path = input_dir / 'notes_only.csv'
    normalized_path = input_dir / 'normalized_events.csv'
    manifest = load_manifest(manifest_path)
    rows = read_notes(notes_path)
    model = build_notation_model(rows, manifest, normalized_path if normalized_path.exists() else None, grid, track, include_drums, title)
    safe = slugify(model.get('title') or input_dir.name)
    model_path = out_dir / f'{safe}_notation_model.json'
    xml_path = out_dir / f'{safe}_score.musicxml'
    pdf_path = out_dir / f'{safe}_score.pdf'
    report_path = out_dir / f'{safe}_notation_report.md'
    model_path.write_text(json.dumps(model, indent=2, ensure_ascii=False), encoding='utf-8')
    write_musicxml(model, xml_path)
    render_pdf(model, pdf_path)
    write_report(model, report_path, pdf_path.name, xml_path.name)
    manifest_out = {
        'module': 'FREDDYnote', 'module_id': 'FREDDY.MUSIC.NOTATION.FREDDYNOTE.v0_4', 'schema_version': '0.4',
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'source': model.get('source', {}), 'title': model.get('title'),
        'outputs': [model_path.name, xml_path.name, pdf_path.name, report_path.name, 'freddy_notation_manifest.json'],
        'status': 'success' if model.get('notes') else 'warning_no_notes',
        'renderer': 'dependency_free_builtin_pdf_writer',
        'ollama_backlog': 'All FREDDY components should later be locally orchestrated via OLLAMA.'
    }
    (out_dir / 'freddy_notation_manifest.json').write_text(json.dumps(manifest_out, indent=2, ensure_ascii=False), encoding='utf-8')
    return {'status': manifest_out['status'], 'pdf': str(pdf_path), 'xml': str(xml_path), 'model': str(model_path), 'report': str(report_path), 'notes': len(model.get('notes', [])), 'measures': len(model.get('measures', [])), 'title': model.get('title'), 'source': model.get('source', {}), 'chosen_track': model.get('filters', {}).get('chosen_track')}
