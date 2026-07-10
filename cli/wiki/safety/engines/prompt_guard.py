"""A maintained local prompt-injection classifier.

WikiBrain does not train a model. This adapter runs one that someone else
maintains — by default `protectai/deberta-v3-base-prompt-injection-v2`, which is
Apache-2.0 and ungated. Point `model` at `meta-llama/Llama-Prompt-Guard-2-86M`
instead if you have accepted its Llama licence; the code is identical.

Four properties, all of them constraints rather than features:

  * **Optional.** Absent `transformers`, `available()` is False and the pipeline
    records `unavailable` — not `ok`, and not a clean scan.
  * **Local, and never downloading on the hot path.** A recall must not become a
    network call because a model was evicted from a cache. `available()` is False
    unless the weights are already on disk, unless `allow_download` is set.
  * **Pinned.** `revision` defaults to a commit, not to `main`. A classifier that
    silently changes its weights changes what WikiBrain withholds.
  * **Bounded.** 512-token window with a stride, max-pooled. Long text does not
    become a long inference.

The score is the malicious-class probability, used directly as the finding's
`confidence`. The classifier reports no span, so its findings can be warned about
or quarantined but never redacted — there is nothing to mask.

This layer raises attacker cost. It is not an authority: a fast filter is provably
evadable by anyone who can query it.
"""
from __future__ import annotations

import os

from ..models import Capability, Finding, RiskLevel
from .base import BaseEngine, EngineScanRequest

DEFAULT_MODEL = "protectai/deberta-v3-base-prompt-injection-v2"

#: Label names that mean "this is an attack", across the models people plug in.
_MALICIOUS_LABELS = ("injection", "malicious", "jailbreak", "label_1", "unsafe")

#: Loaded models, and models that failed to load. A model that failed once is not
#: retried: on an offline box a gated repo would otherwise stall every scan.
_CACHE: dict = {}
_FAILED: set = set()

MAX_TOKENS = 512
STRIDE = 64


class PromptGuardEngine(BaseEngine):
    name = "prompt_guard"
    version = "unknown"
    capabilities = frozenset({Capability.prompt_injection})

    def __init__(self, model: str = "", revision: str = "",
                 threshold: float = 0.5, allow_download: bool = False,
                 **_) -> None:
        self.model = model or DEFAULT_MODEL
        self.revision = revision or None
        self.threshold = float(threshold)
        self.allow_download = bool(allow_download)
        self.version = f"{self.model}@{self.revision or 'unpinned'}"

    def available(self) -> bool:
        if self.model in _FAILED:
            return False
        if self.model in _CACHE:
            return True
        try:
            import transformers  # type: ignore  # noqa: F401
        except Exception:
            return False
        if self.allow_download:
            return True
        return self._cached_locally()

    def _cached_locally(self) -> bool:
        """True iff the weights are already on disk. Never fetches."""
        try:
            from transformers import AutoConfig  # type: ignore

            AutoConfig.from_pretrained(self.model, revision=self.revision,
                                       local_files_only=True)
            return True
        except Exception:
            return False

    def _load(self):
        if self.model in _CACHE:
            return _CACHE[self.model]
        if self.model in _FAILED:
            raise RuntimeError(f"{self.model} previously failed to load")
        try:
            from transformers import (AutoModelForSequenceClassification,  # type: ignore
                                      AutoTokenizer)

            kw = {"revision": self.revision,
                  "local_files_only": not self.allow_download}
            tok = AutoTokenizer.from_pretrained(self.model, **kw)
            model = AutoModelForSequenceClassification.from_pretrained(self.model, **kw)
            model.eval()
            id2label = {int(k): str(v).lower()
                        for k, v in (model.config.id2label or {}).items()}
            malicious = next(
                (i for i, label in id2label.items()
                 if any(m in label for m in _MALICIOUS_LABELS)),
                max(id2label) if id2label else 1)
            _CACHE[self.model] = (tok, model, malicious)
            return _CACHE[self.model]
        except Exception:
            _FAILED.add(self.model)
            raise

    def _score(self, text: str) -> float:
        import torch  # type: ignore

        tok, model, malicious = self._load()
        enc = tok(text, return_tensors="pt", truncation=True,
                  max_length=MAX_TOKENS, stride=STRIDE,
                  return_overflowing_tokens=True, padding=True)
        enc.pop("overflow_to_sample_mapping", None)
        with torch.no_grad():
            logits = model(input_ids=enc["input_ids"],
                           attention_mask=enc["attention_mask"]).logits
        probs = torch.softmax(logits, dim=-1)[:, malicious]
        return float(probs.max())  # max-pool over the chunks

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        if not request.text.strip():
            return []
        # An offline box with a partially-cached model must not reach the network
        # from inside a recall. Belt as well as braces: `local_files_only` above.
        prior = os.environ.get("HF_HUB_OFFLINE")
        if not self.allow_download:
            os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            score = self._score(request.text)
        finally:
            if not self.allow_download:
                if prior is None:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ["HF_HUB_OFFLINE"] = prior

        if score < self.threshold:
            return []
        severity = RiskLevel.high if score >= 0.9 else RiskLevel.medium
        return [self.finding(
            rule="classifier", capability=Capability.prompt_injection,
            severity=severity,
            message=f"injection classifier scored {score:.2f}",
            confidence=score, metadata={"model": self.model,
                                        "threshold": self.threshold})]
