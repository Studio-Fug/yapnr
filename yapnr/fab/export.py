"""The vendor's fab files from a checked board: kicad-cli exports, normalized and renamed.

Design §6.2 to §6.4. Every gerber's X2 ``FileFunction`` is read back and must match the layer it
was exported for before it is renamed to the vendor's name, so a renaming mistake (or a kicad-cli
that changes its file names) stops the build instead of shipping a wrong layer.

Normalization makes two exports of one board byte-identical: kicad-cli runs with ``TZ=UTC`` and a
fixed set of time stamps is rewritten to one time (``SOURCE_DATE_EPOCH``, else 1980-01-01).
"""

from __future__ import annotations

import datetime
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_TIME = datetime.datetime(1980, 1, 1, tzinfo=datetime.timezone.utc)

# The lines kicad-cli 10.0.6 stamps with the time, and the format of each stamp (measured: two
# runs of one board differ in these lines only; the IPC-D-356 file is identical).
_STAMPS: Sequence[Tuple["re.Pattern[str]", str]] = (
    (re.compile(r"^(%TF\.CreationDate,)([^*]*)(\*%\s*)$"), "offset"),
    (re.compile(r"^(G04 Created by KiCad \(.*\) date )(.*?)(\*\s*)$"), "space"),
    (re.compile(r"^(; DRILL file .* date )(\S+)(\s*)$"), "naive"),
    (re.compile(r"^(; #@! TF\.CreationDate,)(\S+)(\s*)$"), "offset"),
    (re.compile(r'^(\s*"CreationDate":\s*")([^"]*)(",?\s*)$'), "offset"),
)


class ExportError(RuntimeError):
    """An export that does not match what was asked for (a layer, a file name)."""


def stamp_time(epoch: Optional[int]) -> datetime.datetime:
    """The time written into every stamp: ``epoch`` (SOURCE_DATE_EPOCH) or 1980-01-01 UTC."""
    if epoch is None:
        return DEFAULT_TIME
    return datetime.datetime.fromtimestamp(int(epoch), tz=datetime.timezone.utc)


def _format(when: datetime.datetime, style: str) -> str:
    if style == "offset":
        return when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if style == "space":
        return when.strftime("%Y-%m-%d %H:%M:%S")
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def normalize_text(text: str, when: datetime.datetime) -> Tuple[str, int]:
    """``text`` with every known time stamp set to ``when``; (new text, lines rewritten)."""
    out, count = [], 0
    for line in text.splitlines(keepends=True):
        for pattern, style in _STAMPS:
            match = pattern.match(line)
            if match:
                line = match.group(1) + _format(when, style) + match.group(3)
                count += 1
                break
        out.append(line)
    return "".join(out), count


def normalize(path: Path, when: datetime.datetime) -> int:
    """Normalize one file in place (bytes kept as written, line endings included)."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return 0
    new, count = normalize_text(text, when)
    if new != text:
        Path(path).write_bytes(new.encode("utf-8"))
    return count


# ------------------------------------------------------------------------- layers and names

_FUNCTION = re.compile(r"^%TF\.FileFunction,([^*]*)\*%", re.MULTILINE)


def file_function_text(text: str) -> Optional[str]:
    """The X2 ``FileFunction`` (``Copper,L1,Top``) in a gerber's header."""
    match = _FUNCTION.search(text[:4000])
    return match.group(1) if match else None


def file_function(path: Path) -> Optional[str]:
    """A gerber file's X2 ``FileFunction``."""
    return file_function_text(Path(path).read_text(encoding="utf-8", errors="replace")[:4000])


def layer_of_function(function: str) -> Optional[str]:
    """The KiCad layer an X2 ``FileFunction`` describes (None for drill maps and unknowns)."""
    parts = function.split(",")
    kind = parts[0]
    side = parts[-1] if len(parts) > 1 else ""
    if kind == "Copper" and len(parts) >= 3:
        index = int(parts[1][1:])
        return {"Top": "F.Cu", "Bot": "B.Cu"}.get(parts[2], f"In{index - 1}.Cu")
    prefix = {"Top": "F", "Bot": "B"}.get(side)
    if prefix is None:
        return "Edge.Cuts" if kind == "Profile" else None
    return {
        "Soldermask": f"{prefix}.Mask",
        "Legend": f"{prefix}.Silkscreen",
        "Paste": f"{prefix}.Paste",
        "SolderPaste": f"{prefix}.Paste",
    }.get(kind)


def kicad_token(layer: str) -> str:
    """The layer as kicad-cli writes it into file names (``In1.Cu`` -> ``In1_Cu``)."""
    return layer.replace(".", "_")


def safe_name(name: str) -> str:
    """A board name usable in file names and zip members."""
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not clean:
        raise ExportError(f"board name {name!r} has no usable characters")
    return clean


def layers_for(vendor: Dict, copper_layers: Sequence[str]) -> List[str]:
    """The vendor's gerber layer list with ``copper`` expanded to the board's copper layers."""
    out: List[str] = []
    for layer in vendor["gerbers"]["layers"]:
        out.extend(copper_layers if layer == "copper" else [layer])
    return out


@dataclass
class FabFile:
    name: str  # name in the bundle (the vendor's name)
    path: Path
    kind: str  # gerber, drill, drill-map, netlist, job
    layer: Optional[str] = None
    function: Optional[str] = None


def _has_holes(drill_file: Path) -> bool:
    text = drill_file.read_text(encoding="utf-8", errors="replace")
    body = text.split("%", 1)[-1] if "%" in text else text
    return bool(re.search(r"^X-?[\d.]+Y-?[\d.]+", body, re.MULTILINE))


def export_fab_files(
    cli,
    board: Path,
    board_name: str,
    vendor: Dict,
    copper_layers: Sequence[str],
    workdir: Path,
    when: datetime.datetime,
) -> Tuple[List[FabFile], List[FabFile]]:
    """Export, normalize, check and rename; returns (files for the vendor zip, auxiliary files).

    ``board`` must be named ``<board_name>.kicad_pcb`` (kicad-cli names its outputs after it).
    """
    if board.stem != board_name:
        raise ExportError(f"the board copy must be named {board_name}.kicad_pcb")
    out = workdir / "kicad-out"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    g = vendor["gerbers"]
    layers = layers_for(vendor, copper_layers)
    cli.gerbers(
        board,
        out,
        layers,
        protel=g.get("protel_extensions", True),
        subtract_soldermask=g.get("subtract_soldermask", True),
        x2=g.get("x2", True),
    )
    d = vendor["drill"]
    cli.drill(
        board,
        out,
        units=d["units"],
        zeros=d["zeros"],
        oval=d["oval"],
        origin=d.get("origin", "absolute"),
        separate_th=d.get("separate_th", False),
        map_format=d.get("map"),
    )
    if "ipcd356" in vendor.get("extras", []):
        cli.ipcd356(board, out / f"{board_name}.d356")
    for path in sorted(out.iterdir()):
        normalize(path, when)

    fab: List[FabFile] = []
    aux: List[FabFile] = []
    names = g.get("names") or {}
    seen_layers = set()
    for path in sorted(out.iterdir()):
        suffix = path.name[len(board_name) :]
        if not path.name.startswith(board_name):
            raise ExportError(f"unexpected kicad-cli output {path.name}")
        if path.suffix == ".gbrjob":
            aux.append(FabFile(path.name, path, "job"))
            continue
        if path.suffix in (".drl",):
            continue  # below
        if path.suffix == ".d356":
            fab.append(FabFile(path.name, path, "netlist"))
            continue
        function = file_function(path)
        if function is None:
            raise ExportError(f"{path.name} has no X2 FileFunction")
        if function == "Drillmap":
            continue  # with its drill file below
        layer = layer_of_function(function)
        if layer is None or layer not in layers:
            raise ExportError(f"{path.name}: FileFunction {function} is not an exported layer")
        if not suffix.startswith("-" + kicad_token(layer) + "."):
            raise ExportError(f"{path.name}: FileFunction {function} does not match its name")
        seen_layers.add(layer)
        name = names[layer].format(board=board_name) if names else path.name
        fab.append(FabFile(name, path, "gerber", layer, function))
    missing = [layer for layer in layers if layer not in seen_layers]
    if missing:
        raise ExportError(f"kicad-cli wrote no gerber for {', '.join(missing)}")

    dnames = d["names"]
    if d.get("separate_th"):
        drills = [("pth", out / f"{board_name}-PTH.drl"), ("npth", out / f"{board_name}-NPTH.drl")]
    else:
        drills = [("merged", out / f"{board_name}.drl")]
    for key, path in drills:
        if not path.is_file():
            raise ExportError(f"kicad-cli wrote no drill file {path.name}")
        if key == "npth" and not _has_holes(path):
            continue  # KiCad writes an empty NPTH file for a board without NPTH holes
        fab.append(FabFile(dnames[key].format(board=board_name), path, "drill"))
        mapped = path.with_name(path.stem + "-drl_map.gbr")
        if d.get("map") and mapped.is_file():
            fab.append(FabFile(mapped.name, mapped, "drill-map"))
    names_used = [f.name.lower() for f in fab]
    if len(set(names_used)) != len(names_used):
        raise ExportError(f"two fab files share a name: {sorted(names_used)}")
    return sorted(fab, key=lambda f: f.name), aux
