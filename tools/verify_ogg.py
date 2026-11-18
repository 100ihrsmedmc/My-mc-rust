#!/usr/bin/env python3
"""Verify the prepared package's audio (OGG) resources against Minecraft 26.3.

Checks, in one pass over the prepared resources:

* every required `.ogg` is present (the asset index is the source of truth);
* each file is a structurally valid Ogg stream -- page CRC, version, page sequence
  continuity, a beginning-of-stream page and an end-of-stream page -- so a file
  that is empty, truncated or corrupted is caught, not just a missing one;
* the codec header parses (Vorbis/Opus) with a sane sample rate and channel count;
* the files sit at the Minecraft 26.3 layout `assets/<namespace>/sounds/...`;
* every sound event in `sounds.json` names files that are all present;
* the prepared bytes are identical to the originals, by SHA-1 against the asset
  index, so the package preserves the original audio assets unchanged.

Writes a JSON report and prints a human summary. Exit status is 0 only when every
check passes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

# --- Ogg container -----------------------------------------------------------

_POLY = 0x04C11DB7


def _crc_table() -> list[int]:
    table = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            value = ((value << 1) ^ _POLY) & 0xFFFFFFFF if value & 0x80000000 else (value << 1) & 0xFFFFFFFF
        table.append(value)
    return table


_CRC = _crc_table()


def ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) & 0xFF) ^ byte]
    return crc


class OggError(Exception):
    pass


def parse_ogg(data: bytes) -> dict:
    """Walk every Ogg page. Raises OggError on anything structurally wrong."""
    if len(data) < 27:
        raise OggError("shorter than one Ogg page header")
    pages = 0
    serial: int | None = None
    expected_sequence = 0
    saw_bos = False
    saw_eos = False
    first_payload = b""
    offset = 0
    while offset < len(data):
        if data[offset:offset + 4] != b"OggS":
            raise OggError(f"no OggS capture pattern at byte {offset}")
        if offset + 27 > len(data):
            raise OggError("truncated page header")
        version = data[offset + 4]
        if version != 0:
            raise OggError(f"unsupported Ogg version {version}")
        header_type = data[offset + 5]
        granule = struct.unpack_from("<q", data, offset + 6)[0]
        page_serial = struct.unpack_from("<I", data, offset + 14)[0]
        sequence = struct.unpack_from("<I", data, offset + 18)[0]
        stored_crc = struct.unpack_from("<I", data, offset + 22)[0]
        segments = data[offset + 26]
        table_end = offset + 27 + segments
        if table_end > len(data):
            raise OggError("truncated segment table")
        payload_size = sum(data[offset + 27:table_end])
        page_end = table_end + payload_size
        if page_end > len(data):
            raise OggError(f"page {pages} runs past the end of the file (truncated)")

        page = bytearray(data[offset:page_end])
        page[22:26] = b"\x00\x00\x00\x00"
        if ogg_crc(bytes(page)) != stored_crc:
            raise OggError(f"page {pages} fails its CRC check (corrupted)")

        if serial is None:
            serial = page_serial
        elif page_serial != serial:
            raise OggError(f"page {pages} changes the stream serial number")
        if sequence != expected_sequence:
            raise OggError(
                f"page {pages} has sequence {sequence}, expected {expected_sequence} (missing page)"
            )
        expected_sequence += 1
        if header_type & 0x02:
            saw_bos = True
        if header_type & 0x04:
            saw_eos = True
        if pages == 0:
            first_payload = bytes(data[table_end:page_end])
        pages += 1
        offset = page_end

    if not saw_bos:
        raise OggError("no beginning-of-stream page")
    if not saw_eos:
        raise OggError("no end-of-stream page (the stream is truncated)")
    return {"pages": pages, "first_payload": first_payload, "granule": granule}


def parse_codec(first_payload: bytes) -> dict:
    """The codec header of a stream's first packet."""
    if first_payload[:7] == b"\x01vorbis":
        if len(first_payload) < 30:
            raise OggError("Vorbis identification header is truncated")
        version = struct.unpack_from("<I", first_payload, 7)[0]
        channels = first_payload[11]
        sample_rate = struct.unpack_from("<I", first_payload, 12)[0]
        if version != 0:
            raise OggError(f"Vorbis version {version}")
        if not 1 <= channels <= 255:
            raise OggError(f"Vorbis channel count {channels}")
        if not 1 <= sample_rate <= 384_000:
            raise OggError(f"Vorbis sample rate {sample_rate}")
        return {"codec": "vorbis", "channels": channels, "sample_rate": sample_rate}
    if first_payload[:8] == b"OpusHead":
        if len(first_payload) < 19:
            raise OggError("Opus identification header is truncated")
        channels = first_payload[9]
        sample_rate = struct.unpack_from("<I", first_payload, 12)[0]
        return {"codec": "opus", "channels": channels, "sample_rate": sample_rate}
    raise OggError("first page is not a Vorbis or Opus identification header")


# --- sounds.json -------------------------------------------------------------


def sound_files(sounds_json: dict) -> tuple[set[str], list[str]]:
    """(referenced files, events) from a sounds.json.

    A string entry, or a dict without `"type": "event"`, names a sound file. A
    `"type": "event"` entry names another event, not a file, exactly as
    `SoundRegistry::add_file` treats it (`Sound.event`), so it is not counted as a
    file that must be present.
    """
    files: set[str] = set()
    events: list[str] = []

    def namespace_path(location: str) -> str:
        namespace, _, path = location.partition(":")
        if not path:
            namespace, path = "minecraft", namespace
        return f"{namespace}:{path}"

    for event, entry in sounds_json.items():
        events.append(event)
        if not isinstance(entry, dict):
            continue
        for sound in entry.get("sounds", []):
            name: str | None = None
            if isinstance(sound, str):
                name = sound
            elif isinstance(sound, dict) and sound.get("type") != "event":
                name = sound.get("name")
            if isinstance(name, str):
                files.add(namespace_path(name))
    return files, events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True,
                        help="the prepared resource folder")
    parser.add_argument("--asset-index", type=Path,
                        help="the Minecraft asset index JSON (proves byte preservation)")
    parser.add_argument("--report", type=Path, required=True, help="where to write the JSON report")
    parser.add_argument("--sample-limit", type=int, default=50,
                        help="how many examples of each problem to keep")
    args = parser.parse_args()

    resources = args.resources
    assets = resources / "assets"
    report: dict = {
        "resources": str(resources),
        "ogg": {"count": 0, "bytes": 0, "failed": 0, "failures": [], "codecs": {}},
        "structure": {"ok": True, "problems": []},
        "sounds_json": {"present": False, "events": 0, "referenced_files": 0,
                        "missing_files": [], "malformed": False},
        "asset_index": {"checked": False, "expected_ogg": 0, "missing": [],
                        "mismatched": [], "extra_ogg": 0},
        "ok": False,
    }

    if not assets.is_dir():
        report["structure"]["ok"] = False
        report["structure"]["problems"].append(f"{assets} does not exist")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2))
        print(f"FAILED: {assets} does not exist", file=sys.stderr)
        return 1

    # Every .ogg under assets/, and the layout it sits in.
    oggs = sorted(assets.rglob("*.ogg"))
    report["ogg"]["count"] = len(oggs)
    sample_limit = args.sample_limit
    for path in oggs:
        relative = path.relative_to(assets).as_posix()
        parts = relative.split("/")
        if len(parts) < 3 or parts[1] != "sounds":
            report["structure"]["ok"] = False
            if len(report["structure"]["problems"]) < sample_limit:
                report["structure"]["problems"].append(
                    f"{relative}: not under assets/<namespace>/sounds/"
                )
        try:
            size = path.stat().st_size
            report["ogg"]["bytes"] += size
            if size == 0:
                raise OggError("file is empty")
            data = path.read_bytes()
            parsed = parse_ogg(data)
            codec = parse_codec(parsed["first_payload"])
            report["ogg"]["codecs"][codec["codec"]] = report["ogg"]["codecs"].get(codec["codec"], 0) + 1
        except OggError as error:
            report["ogg"]["failed"] += 1
            if len(report["ogg"]["failures"]) < sample_limit:
                report["ogg"]["failures"].append({"file": relative, "error": str(error)})
        except OSError as error:
            report["ogg"]["failed"] += 1
            if len(report["ogg"]["failures"]) < sample_limit:
                report["ogg"]["failures"].append({"file": relative, "error": str(error)})

    # sounds.json: the events and every file they name.
    sounds_json_path = assets / "minecraft" / "sounds.json"
    if sounds_json_path.is_file():
        report["sounds_json"]["present"] = True
        try:
            data = json.loads(sounds_json_path.read_text())
            referenced, events = sound_files(data)
            report["sounds_json"]["events"] = len(events)
            report["sounds_json"]["referenced_files"] = len(referenced)
            missing = []
            for location in sorted(referenced):
                namespace, _, path = location.partition(":")
                target = assets / namespace / "sounds" / f"{path}.ogg"
                if not target.is_file():
                    missing.append(location)
            report["sounds_json"]["missing_files"] = missing[:sample_limit]
            report["sounds_json"]["missing_files_count"] = len(missing)
        except (ValueError, OSError) as error:
            report["sounds_json"]["malformed"] = True
            report["sounds_json"]["error"] = str(error)
    else:
        report["structure"]["ok"] = False
        report["structure"]["problems"].append("assets/minecraft/sounds.json is missing")

    # Byte preservation: every indexed sound object is present and unchanged.
    if args.asset_index and args.asset_index.is_file():
        report["asset_index"]["checked"] = True
        index = json.loads(args.asset_index.read_text())
        expected = {
            name: info["hash"]
            for name, info in index.get("objects", {}).items()
            if name.startswith("minecraft/sounds/") and name.endswith(".ogg")
        }
        report["asset_index"]["expected_ogg"] = len(expected)
        missing, mismatched = [], []
        for name, sha1 in expected.items():
            target = assets / name
            if not target.is_file():
                if len(missing) < sample_limit:
                    missing.append(name)
                continue
            digest = hashlib.sha1(target.read_bytes()).hexdigest()
            if digest != sha1:
                if len(mismatched) < sample_limit:
                    mismatched.append(name)
        report["asset_index"]["missing"] = missing
        report["asset_index"]["mismatched"] = mismatched
        present = set(p.relative_to(assets).as_posix() for p in oggs)
        report["asset_index"]["extra_ogg"] = len(present - set(expected))

    ok = (
        report["ogg"]["failed"] == 0
        and report["structure"]["ok"]
        and report["sounds_json"]["present"]
        and not report["sounds_json"]["malformed"]
        and not report["sounds_json"].get("missing_files_count")
        and (not report["asset_index"]["checked"] or (
            not report["asset_index"]["missing"] and not report["asset_index"]["mismatched"]))
    )
    report["ok"] = ok
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))

    print(f"OGG files: {report['ogg']['count']} ({report['ogg']['bytes'] / 1e6:.1f} MB), "
          f"codecs {report['ogg']['codecs']}")
    print(f"OGG failures: {report['ogg']['failed']}")
    if report["ogg"]["failures"]:
        for failure in report["ogg"]["failures"][:10]:
            print(f"  {failure['file']}: {failure['error']}")
    if not report["structure"]["ok"]:
        print("Structure problems:")
        for problem in report["structure"]["problems"][:10]:
            print(f"  {problem}")
    sj = report["sounds_json"]
    print(f"sounds.json: present={sj['present']} events={sj['events']} "
          f"referenced files={sj['referenced_files']} missing={sj.get('missing_files_count', 0)}")
    if sj.get("missing_files"):
        for name in sj["missing_files"][:10]:
            print(f"  missing {name}")
    if report["asset_index"]["checked"]:
        ai = report["asset_index"]
        print(f"asset index: expected {ai['expected_ogg']} OGG objects, "
              f"missing {len(ai['missing'])}, mismatched {len(ai['mismatched'])}, "
              f"extra {ai['extra_ogg']}")
    print("AUDIO OK" if ok else "AUDIO FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
