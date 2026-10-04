"""Native KiCad endpoints, always launched in a separate pcbnew process."""

import argparse
import hashlib
import json
import math
import uuid
from pathlib import Path

import pcbnew as k

from pnr.ingest import load
from pnr.writeback import _PAGE_OFFSET_MM, frame_region, patch_project_rules


def make(spec, root, library):
    b = k.BOARD()
    b.SetCopperLayerCount(spec["constraints"]["board"]["layers"])
    if spec.get("stackup"):  # a hard rung's copper stack (stackup.py): layer types first
        import stackup

        stackup.layer_types(b, spec["stackup"])
    nets = sorted({n for p in spec["parts"] for n in p["pins"].values() if n})
    nm = {}
    for n in nets:
        ni = k.NETINFO_ITEM(b, n)
        b.Add(ni)
        nm[n] = ni
    used = {}
    footprints = []
    for i, p in enumerate(spec["parts"]):
        lib, name = p["footprint"].split(":")
        folder = library / (lib + ".pretty")
        fp = k.FootprintLoad(str(folder), name)
        if fp is None:
            raise ValueError("Missing library " + p["footprint"])
        used[lib] = str(folder)
        b.Add(fp)
        fp.SetReference(p["ref"])
        fp.SetValue(p["value"])
        items = [fp, fp.Reference(), fp.Value(), *fp.Pads(), *fp.GraphicalItems()]
        for index, item in enumerate(items):
            item.m_Uuid.Clone(
                k.KIID(
                    str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            "splanc-regression/" + spec["name"] + "/" + p["ref"] + "/" + str(index),
                        )
                    )
                )
            )
        fp.SetFPID(k.LIB_ID(lib, name))
        fp.Reference().SetLayer(k.F_Fab)
        # Deliberately naive row, unrelated to final pose, with no copper at all.
        fp.SetPosition(k.VECTOR2I(round((35 + i * 12) * 1e6), 40000000))
        seen = set()
        for pad in fp.Pads():
            pin = pad.GetNumber()
            seen.add(pin)
            if pin not in p["pins"]:
                raise ValueError("Unspecified pin " + p["ref"] + "." + pin)
            if p["pins"][pin]:
                pad.SetNet(nm[p["pins"][pin]])
        if seen != set(p["pins"]):
            raise ValueError("Missing pad " + p["ref"])
        footprints.append(fp)
    size = spec["constraints"]["board"]["outline"]
    if spec.get("fixed_block"):
        add_fixed_block(b, spec, nm, size["h"])
    source = root / "source.kicad_pcb"
    if spec.get("stackup"):
        # Plane zones the engine cannot declare itself (a second plane layer of one
        # net), on the outline frame_region stamps below.
        x0 = y0 = k.FromMM(_PAGE_OFFSET_MM)
        rect = (x0, y0, x0 + k.FromMM(size["w"]), y0 + k.FromMM(size["h"]))
        stackup.add_plane_zones(b, spec, rect, "extra")
    k.SaveBoard(str(source), b)
    text = frame_region(source.read_text(), size["w"], size["h"])
    if spec.get("stackup"):
        text = stackup.insert_stackup(text, spec["stackup"])
    source.write_text(text)
    table = (
        "(fp_lib_table (version 7)\n"
        + "".join(
            ' (lib (name %s) (type "KiCad") (uri %s) (options "") (descr "Regression source"))\n'
            % (json.dumps(lib), json.dumps(path))
            for lib, path in used.items()
        )
        + ")\n"
    )
    (root / "fp-lib-table").write_text(table)
    if spec.get("fixed_block"):
        # The block's copper as the router reads it (fixed.json schema 2): the
        # placement graph holds its footprints out, route_case reserves the copper.
        from pnr.fixed_copper import extract

        block = spec["fixed_block"]
        copper = extract(
            k.LoadBoard(str(source)),
            [dict(name=block["name"], group=block["group"], refs=sorted(block["footprints"]))],
        )
        (root / "source-fixed.json").write_text(json.dumps(copper, indent=2))
    graph = load(str(source))
    graph.components.sort(key=lambda c: c.ref)
    graph.nets.sort(key=lambda n: n.name)
    (root / "source-graph.json").write_text(graph.to_json())
    (root / "library-manifest.json").write_text(
        json.dumps(
            {
                p["footprint"]: dict(
                    path=str(
                        library
                        / (p["footprint"].split(":")[0] + ".pretty")
                        / (p["footprint"].split(":")[1] + ".kicad_mod")
                    ),
                    sha256=hashlib.sha256(
                        (
                            library
                            / (p["footprint"].split(":")[0] + ".pretty")
                            / (p["footprint"].split(":")[1] + ".kicad_mod")
                        ).read_bytes()
                    ).hexdigest(),
                )
                for p in spec["parts"]
            },
            indent=2,
        )
    )


def add_fixed_block(b, spec, nets, height):
    """A rung's fixed block (``spec["fixed_block"]``, hard_rungs): its footprints at
    their poses and its tracks, arcs and vias, all locked, in one KiCad group, every
    item with a deterministic identity. Engine mm (y up) to the generator's frame
    (the outline is stamped at the page offset)."""
    block = spec["fixed_block"]

    def point(xy):
        return k.VECTOR2I(
            round((_PAGE_OFFSET_MM + xy[0]) * 1e6), round((_PAGE_OFFSET_MM + height - xy[1]) * 1e6)
        )

    def ident(item, tag):
        item.m_Uuid.Clone(
            k.KIID(
                str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, "splanc-regression/%s/block/%s" % (spec["name"], tag)
                    )
                )
            )
        )

    group = k.PCB_GROUP(b)
    group.SetName(block["group"])
    ident(group, "group")
    b.Add(group)
    items = []
    for ref, (x, y, rot) in sorted(block["footprints"].items()):
        fp = b.FindFootprintByReference(ref)
        fp.SetPosition(point((x, y)))
        fp.SetOrientationDegrees(float(rot))
        items.append(fp)
    for n, (net, layer, a, z, w) in enumerate(block["tracks"]):
        t = k.PCB_TRACK(b)
        t.SetStart(point(a))
        t.SetEnd(point(z))
        t.SetWidth(round(w * 1e6))
        t.SetLayer(b.GetLayerID(layer))
        t.SetNet(nets[net])
        ident(t, "track/%d" % n)
        b.Add(t)
        items.append(t)
    for n, (net, layer, a, m, z, w) in enumerate(block["arcs"]):
        t = k.PCB_ARC(b)
        t.SetStart(point(a))
        t.SetMid(point(m))
        t.SetEnd(point(z))
        t.SetWidth(round(w * 1e6))
        t.SetLayer(b.GetLayerID(layer))
        t.SetNet(nets[net])
        ident(t, "arc/%d" % n)
        b.Add(t)
        items.append(t)
    for n, (net, xy, diameter, drill) in enumerate(block["vias"]):
        v = k.PCB_VIA(b)
        v.SetPosition(point(xy))
        v.SetViaType(k.VIATYPE_THROUGH)
        v.SetLayerPair(k.F_Cu, k.B_Cu)
        v.SetWidth(round(diameter * 1e6))
        v.SetDrill(round(drill * 1e6))
        v.SetNet(nets[net])
        ident(v, "via/%d" % n)
        b.Add(v)
        items.append(v)
    for n, area in enumerate(block.get("rule_areas") or []):
        # A rule area of the block (its own copper is exempt only through the group's
        # keep-outs, so it bars what its flags say on its layers for every item).
        z = k.ZONE(b)
        z.SetIsRuleArea(True)
        z.SetZoneName("%s rule %d" % (block["group"], n))
        layers = k.LSET()
        for name in area["layers"]:
            layers.AddLayer(b.GetLayerID(name))
        z.SetLayerSet(layers)
        z.SetDoNotAllowTracks(bool(area.get("tracks")))
        z.SetDoNotAllowVias(bool(area.get("vias")))
        z.SetDoNotAllowZoneFills(bool(area.get("pours")))
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(False)
        outline = z.Outline()
        outline.NewOutline()
        for xy in area["polygon"]:
            outline.Append(point(xy))
        ident(z, "rule/%d" % n)
        b.Add(z)
        items.append(z)
    for item in items:
        group.AddItem(item)
        item.SetLocked(True)


def audit(spec, root, pcb):
    from pnr.pad_entry import inspect, required_width

    b = k.LoadBoard(str(pcb))
    b.BuildConnectivity()
    expected = {(p["ref"], pin): n for p in spec["parts"] for pin, n in p["pins"].items()}
    actual = {
        (fp.GetReference(), pad.GetNumber()): pad.GetNetname()
        for fp in b.GetFootprints()
        for pad in fp.Pads()
    }
    rules = json.loads((root / "rules.json").read_text())
    entries = [
        dict(
            ref=p.GetParentFootprint().GetReference(),
            pin=p.GetNumber(),
            net=p.GetNetname(),
            layer=b.GetLayerName(la),
            required_width_mm=w,
            qualified=bool(good),
        )
        for p, la, ts, w, good in inspect(b, rules)
    ]
    netwidth = {n: rules["fab"]["track_width_mm"] for n in set(expected.values()) if n}
    for cls in rules["net_classes"]:
        for n in cls["nets"]:
            netwidth[n] = max(netwidth[n], cls["width_mm"])
    thin = [
        str(t.m_Uuid.AsString())
        for t in b.GetTracks()
        if t.GetClass() == "PCB_TRACK"
        and t.GetWidth() / 1e6 + 1e-6 < netwidth.get(t.GetNetname(), 0)
    ]
    data = dict(
        netlist_preserved=expected == actual,
        components=len(b.GetFootprints()),
        pads=len(actual),
        tracks=sum(t.GetClass() == "PCB_TRACK" for t in b.GetTracks()),
        vias=sum(t.GetClass() == "PCB_VIA" for t in b.GetTracks()),
        copper_length_mm=sum(
            t.GetLength() / 1e6 for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"
        ),
        subwidth_tracks=thin,
        pad_entries=entries,
        layer_count=b.GetCopperLayerCount(),
        missing_pins=[str(v) for v in expected.keys() - actual.keys()],
    )
    (root / "native-audit.json").write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("mode", choices=["make", "audit"])
    a.add_argument("root", type=Path)
    a.add_argument("--library", type=Path)
    a.add_argument("--pcb", type=Path)
    v = a.parse_args()
    spec = json.loads((v.root / "design.json").read_text())
    if v.mode == "make":
        make(spec, v.root, v.library)
    else:
        audit(spec, v.root, v.pcb)
