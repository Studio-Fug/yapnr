"""RF test inventory. Every new RF target needs an explicit lane decision.

Ordinary CI keeps inexpensive coverage; nightly includes solver, inverse-design and
coupon fitting suites. Manual and KiCad targets retain their existing gates.
"""

RF_LANES = {
    "//tests/e2e/rf:test_antenna": "manual",
    "//tests/e2e/rf:test_antenna_smoke": "nightly",
    "//tests/e2e/rf:test_diplexer": "manual",
    "//tests/e2e/rf:test_diplexer_smoke": "nightly",
    "//tests/e2e/rf:test_divider": "manual",
    "//tests/e2e/rf:test_divider_smoke": "nightly",
    "//tests/e2e/rf:test_filterbank3": "manual",
    "//tests/e2e/rf:test_wilkinson": "manual",
    "//tests/e2e/rf:test_wilkinson_smoke": "nightly",
    "//tests/unit/rf:test_adjoint_gradients": "nightly",
    "//tests/unit/rf:test_adjoint_gradients_numpy": "manual",
    "//tests/unit/rf:test_adjoint_sources": "ordinary",
    "//tests/unit/rf:test_animate": "ordinary",
    "//tests/unit/rf:test_backends": "nightly",
    "//tests/unit/rf:test_balance": "nightly",
    "//tests/unit/rf:test_board": "nightly",
    "//tests/unit/rf:test_cpml": "ordinary",
    "//tests/unit/rf:test_dtft": "nightly",
    "//tests/unit/rf:test_edges": "ordinary",
    "//tests/unit/rf:test_export": "ordinary",
    "//tests/unit/rf:test_farfield": "ordinary",
    "//tests/unit/rf:test_filters": "ordinary",
    "//tests/unit/rf:test_inverse_smoke": "ordinary",
    "//tests/unit/rf:test_kicad_cli": "kicad",
    "//tests/unit/rf:test_lengthscale": "ordinary",
    "//tests/unit/rf:test_lumped_ports": "ordinary",
    "//tests/unit/rf:test_material_grid": "ordinary",
    "//tests/unit/rf:test_mesh": "ordinary",
    "//tests/unit/rf:test_microstrip": "nightly",
    "//tests/unit/rf:test_microstrip_slow": "manual",
    "//tests/unit/rf:test_mma": "ordinary",
    "//tests/unit/rf:test_modal_ports": "nightly",
    "//tests/unit/rf:test_modes": "nightly",
    "//tests/unit/rf:test_multistart": "ordinary",
    "//tests/unit/rf:test_native_identity": "nightly",
    "//tests/unit/rf:test_native_kernel": "nightly",
    "//tests/unit/rf:test_pattern_gradients": "nightly",
    "//tests/unit/rf:test_pattern_gradients_numpy": "manual",
    "//tests/unit/rf:test_pattern_spec": "ordinary",
    "//tests/unit/rf:test_pipeline_gradient": "nightly",
    "//tests/unit/rf:test_pipeline_gradient_numpy": "manual",
    "//tests/unit/rf:test_pipeline_gradient_options": "nightly",
    "//tests/unit/rf:test_pipeline_gradient_options_numpy": "manual",
    "//tests/unit/rf:test_ports": "nightly",
    "//tests/unit/rf:test_power_balance": "nightly",
    "//tests/unit/rf:test_repair": "ordinary",
    "//tests/unit/rf:test_seeds": "ordinary",
    "//tests/unit/rf:test_sheet": "ordinary",
    "//tests/unit/rf:test_spec": "ordinary",
    "//tests/unit/rf:test_stability": "ordinary",
    "//tests/unit/rf:test_tiny_design": "nightly",
    "//tests/unit/rf:test_tiny_design_options": "nightly",
    "//tests/unit/rf:test_torch_convention": "ordinary",
    "//tests/unit/rf:test_validate": "ordinary",
    "//tests/unit/rf_coupons:test_calibrate": "ordinary",
    "//tests/unit/rf_coupons:test_catalog": "ordinary",
    "//tests/unit/rf_coupons:test_cli": "nightly",
    "//tests/unit/rf_coupons:test_equivalent": "ordinary",
    "//tests/unit/rf_coupons:test_fit": "nightly",
    "//tests/unit/rf_coupons:test_kicad": "kicad",
    "//tests/unit/rf_coupons:test_launch": "ordinary",
    "//tests/unit/rf_coupons:test_models": "ordinary",
    "//tests/unit/rf_coupons:test_order0": "nightly",
    "//tests/unit/rf_coupons:test_session": "ordinary",
    "//tests/unit/rf_coupons:test_xsec": "manual",
}

def rf_tags(name):
    """Return the lane tags for a target in the calling package.

    Args:
        name: Test target name in the calling package.

    Returns:
        Additional lane tags; existing manual and KiCad tags stay in BUILD files.
    """
    label = "//" + native.package_name() + ":" + name
    if label not in RF_LANES:
        fail("Classify the new RF test in //tools/bazel:rf_lanes.bzl: " + label)
    lane = RF_LANES[label]
    return ["rf-nightly"] if lane == "nightly" else []

def rf_overrides(overrides):
    """Add lane tags to globbed tests without replacing CPU/resource attributes.

    Args:
        overrides: Per-target attributes passed to yapnr_py_tests.

    Returns:
        A copy with additional lane tags for every globbed test.
    """
    result = dict(overrides)
    for src in native.glob(["test_*.py"]):
        name = src[:-3]
        attrs = dict(result.get(name, {}))
        attrs["tags"] = attrs.get("tags", []) + rf_tags(name)
        result[name] = attrs
    return result
