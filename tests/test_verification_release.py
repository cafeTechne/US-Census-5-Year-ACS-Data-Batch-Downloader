# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tag job must run required tests, reject skips, and only then publish."""

from pathlib import Path
import xml.etree.ElementTree as ET
import textwrap

import pytest


def test_tag_publication_has_real_exact_revision_verification_prerequisite(tmp_path):
    source = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text()
    start = source.index("- name: Mandatory verification checks")
    end = source.index("- name:", start + 1)
    checks = source[start:end]
    assert source.index("gh release create") > end
    assert "continue-on-error" not in source and "always()" not in checks
    for name in (
        "test_proposition_verification.py",
        "test_verification_contract.py",
        "test_workflows_verification.py",
        "test_verification_release.py",
    ):
        assert name in checks
    script = textwrap.dedent(checks.split("python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])

    # Exercise actual embedded gate code, not a second implementation of it.
    class Tree:
        def __init__(self, xml):
            self.xml = xml

        def getroot(self):
            return ET.fromstring(self.xml)

    from unittest.mock import patch

    controls = [
        f"test_exact_typed_rules_prove_only_declared_statement[{name}]"
        for name in ("zero_diagonal", "row_sum", "weighted_squares", "centering_idempotence")
    ]
    controls += [
        "test_false_diagonal_formula_has_its_own_checked_witness",
        "test_centering_dependence_witness_from_enumerated_independent_distribution",
        "test_instance_covariance_is_not_universal_proof",
    ]
    cases = "".join(
        f'<testcase classname="tests.test_proposition_verification" name="{name}"/>' for name in controls
    )
    cases += "".join(
        f'<testcase classname="tests.{module}" name="control"/>'
        for module in (
            "test_verification_contract",
            "test_workflows_verification",
            "test_verification_release",
        )
    )
    for tests, failures, errors, skipped, passes in [
        (5, 0, 0, 0, True),
        (0, 0, 0, 0, False),
        (5, 1, 0, 0, False),
        (5, 0, 1, 0, False),
        (5, 0, 0, 1, False),
    ]:
        xml = f'<testsuites><testsuite tests="{tests}" failures="{failures}" errors="{errors}" skipped="{skipped}">{cases}</testsuite></testsuites>'
        with patch.object(ET, "parse", return_value=Tree(xml)):
            if passes:
                exec(script, {})
            else:
                with pytest.raises(AssertionError):
                    exec(script, {})
    for incomplete in (
        cases.replace('classname="tests.test_verification_contract"', 'classname="missing"'),
        cases.replace('name="test_false_diagonal_formula_has_its_own_checked_witness"', 'name="missing"'),
    ):
        xml = f'<testsuites><testsuite tests="10" failures="0" errors="0" skipped="0">{incomplete}</testsuite></testsuites>'
        with patch.object(ET, "parse", return_value=Tree(xml)):
            with pytest.raises(AssertionError):
                exec(script, {})
