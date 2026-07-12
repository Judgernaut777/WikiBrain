"""OKF (Open Knowledge Format) support — export, validate, and import.

The ledger is canonical; an OKF bundle is a **portable projection** of it. This
package exports that projection, structurally validates a bundle, and imports one
as PENDING memory candidates. It never mutates canonical claims on import, never
auto-promotes, and OKF-valid never implies trusted, promoted, or safe. Validation
is STRUCTURAL ONLY and hostile-input safe; import refuses an invalid bundle whole
(no partial import), scans every document through the existing `memory_candidate`
safety surface before storing it, and refuses to overwrite any canonical claim an
external id already owns.

Public surface:

    from brainconnect.okf import OKFAdapter, ExportRequest, ImportRequest
    result = OKFAdapter().export_bundle(repo, ExportRequest(output_dir="./knowledge"))
    verdict = OKFAdapter().validate_bundle("./knowledge")   # -> ValidationResult
    imported = OKFAdapter().import_bundle(repo, ImportRequest(
        bundle_dir="./knowledge", scope=Scope("global"), imported_by="matthew"))
"""
from __future__ import annotations

from .adapter import KnowledgeFormatAdapter, OKFAdapter
from .export import FORMAT_NAME, OKF_VERSION, ExportError, export_bundle
from .model import ExportRequest, ExportResult
from .okfimport import (DocResult, ImportRequest, ImportResult, import_bundle)
from .validate import (ValidationIssue, ValidationLimits, ValidationResult,
                       validate_bundle)

__all__ = [
    "KnowledgeFormatAdapter", "OKFAdapter",
    "ExportRequest", "ExportResult", "ExportError",
    "export_bundle", "OKF_VERSION", "FORMAT_NAME",
    "validate_bundle", "ValidationResult", "ValidationIssue", "ValidationLimits",
    "import_bundle", "ImportRequest", "ImportResult", "DocResult",
]
