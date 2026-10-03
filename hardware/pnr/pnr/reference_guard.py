"""Protect already routed pair reference corridors during via search."""


class ReferenceGuard:
    def __init__(self, board, rules):
        import pcbnew as k

        from pnr.electrical import net_policy
        from pnr.ingest import board_stack
        from pnr.native_electrical import vec
        from pnr.stack import reference_layer, reference_nets

        stack = board_stack(board, rules)
        self.rules = rules
        self.rows = []
        self.net_gaps = {}
        pairs = {p["name"]: p for p in rules.get("diff_pairs", [])}
        present = {t.GetNetname() for t in board.GetTracks()}
        for witness in rules.get("routed_pair_references", []):
            pair = pairs[witness["pair"]]
            if not {pair["p"], pair["n"]} <= present:
                continue
            layer = reference_layer(stack, pair)
            nets = reference_nets(stack, rules, layer)
            zones = [
                z
                for z in board.Zones()
                if not z.GetIsRuleArea()
                and z.IsOnLayer(board.GetLayerID(layer))
                and z.GetNetname() in nets
            ]
            clearance = max(
                [float(z.GetLocalClearance()) / 1e6 for z in zones]
                + [rules.get("fab", {}).get("clearance_mm", 0.2)]
                + [net_policy(n, rules)["clearance_mm"] for n in nets]
            )
            for segment in witness["segments"]:
                for path in segment["reference_paths"].values():
                    for a, z in zip(path, path[1:]):
                        track = k.PCB_TRACK(board)
                        track.SetLayer(k.F_Cu)
                        track.SetStart(vec(a))
                        track.SetEnd(vec(z))
                        track.SetWidth(round((pair["width_mm"] + pair["gap_mm"]) * 1e6))
                        self.rows.append((track.GetEffectiveShape(k.F_Cu), nets, clearance))

    def via_clear(self, net, point, diameter):
        import pcbnew as k

        from pnr.electrical import net_policy
        from pnr.native_electrical import vec

        if net not in self.net_gaps:
            self.net_gaps[net] = net_policy(net, self.rules)["clearance_mm"]
        gap = self.net_gaps[net]
        # Every corridor with the same clearance uses exactly the same native
        # aperture. Reuse within this query only; position/diameter may change.
        apertures = {}
        for corridor, nets, clearance in self.rows:
            if net in nets:
                continue
            radius = round((diameter / 2 + max(gap, clearance) + 0.004) * 1e6)
            if radius not in apertures:
                apertures[radius] = k.SHAPE_CIRCLE(vec(point), radius)
            if corridor.Collide(apertures[radius], 0):
                return False
        return True
