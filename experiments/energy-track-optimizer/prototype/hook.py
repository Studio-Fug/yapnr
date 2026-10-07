"""Optional adapter hook. Nothing imports this from the production engine."""


def optimize_if_enabled(controller_factory, *, enabled=False):
    if not enabled:
        return {"enabled": False, "changed": False}
    controller = controller_factory()
    report = controller.run()
    return dict(report, enabled=True, changed=bool(report["transactions"]))
