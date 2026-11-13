# SPDX-License-Identifier: AGPL-3.0-or-later
"""One operator-pinned local verification workflow; no model approval endpoint."""

import os
import threading
from typing import Any, Dict, Optional

from pydantic import StrictInt

from .verification import BoundRequest, VerificationError

_BOUND: Optional[BoundRequest] = None
_LOCK = threading.Lock()


def _context():
    global _BOUND
    with _LOCK:
        if _BOUND is None:
            path = os.environ.get("CENSUS_VERIFICATION_REQUEST")
            if not path:
                raise VerificationError(
                    "FORMALIZATION_REVIEW_REQUIRED",
                    "The operator must pin CENSUS_VERIFICATION_REQUEST before starting verification.",
                )
            _BOUND = BoundRequest.from_file(path)
    return _BOUND


def verify_proposition(
    proposition_id: str,
    expression_digest: str,
    assumptions_digest: str,
    schema_version: StrictInt = 1,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Check the exact operator-pinned proposition, never a model-selected substitute.

    Supply identities from the operator's reviewed request, not a theorem name.
    The operator pins CENSUS_VERIFICATION_REQUEST before starting this process.
    output_dir optionally writes a fresh existing-format evidence bundle.
    A refuted claim is a successful check; read verification_status/claim_holds.
    """
    try:
        bound = _context()
        result = bound.verify(proposition_id, expression_digest, assumptions_digest, schema_version)
        if output_dir and result["success"] and result["evidence_verified"]:
            from .report_tables import results_bundle

            evidence = bound.check_evidence(result)
            bundle = results_bundle(
                output_dir,
                results={"verification": evidence},
                methods="Exact bounded matrix evidence. Empirical analysis status is independent. "
                "Formalization review is a local operator attestation, not remote authentication.",
                provenance={
                    "proposition_id": evidence["proposition_id"],
                    "evidence_digest": evidence["evidence_digest"],
                },
            )
            # The evidence object itself remains replayable; transport artifacts are separate.
            return {
                "success": True,
                "verification": evidence,
                "bundle": bundle,
                "summary": f"Exact claim {evidence['verification_status']}; evidence bundle written.",
                "next_step": "inspect the bound proposition and witness/derivation; do not substitute another claim",
            }
        return {
            "success": result["success"],
            "verification": result,
            **(
                {"error_code": result["error_code"], "error": result["error"]}
                if not result["success"]
                else {}
            ),
            "summary": f"Bound proposition: {result['verification_status']}.",
            "next_step": "inspect the exact statement, formalization status and evidence; success does not mean the claim is true",
        }
    except (VerificationError, OSError, ValueError, TypeError) as exc:
        failure = {
            "success": False,
            "verification_status": "error",
            "claim_holds": None,
            "evidence_verified": False,
            "error_code": getattr(exc, "code", "EVIDENCE_INVALID"),
            "error": str(exc),
            "identity_available": False,
        }
        return {
            "success": False,
            "verification": failure,
            "error_code": failure["error_code"],
            "error": str(exc),
            "summary": str(exc),
            "next_step": "ask the operator to inspect the pinned request and output location; tools cannot approve it",
        }
