from abc import ABC, abstractmethod
from pathlib import Path


import threading

class ProviderError(Exception):
    """User-presentable generation failure."""


class ImageProvider(ABC):
    name: str
    needs_transparency_postprocess: bool

    @property
    def last_command(self) -> str | None:
        if not hasattr(self, "_tls"):
            self._tls = threading.local()
        return getattr(self._tls, "cmd", None)

    @last_command.setter
    def last_command(self, value: str | None):
        if not hasattr(self, "_tls"):
            self._tls = threading.local()
        self._tls.cmd = value

    @property
    def last_recovery(self) -> str | None:
        """Set by `generate` when the image it returned came from a job the account had
        ALREADY paid for (recovered after an earlier attempt died mid-call) rather than
        from a new one. Callers surface it, because "this cost nothing, it was already
        bought" is the difference between a run that looks stalled and one that is doing
        the right thing. None on a normal generation. Thread-local, like `last_command`,
        since a sweep runs several generations at once on one provider instance."""
        if not hasattr(self, "_tls"):
            self._tls = threading.local()
        return getattr(self._tls, "recovery", None)

    @last_recovery.setter
    def last_recovery(self, value: str | None):
        if not hasattr(self, "_tls"):
            self._tls = threading.local()
        self._tls.recovery = value

    @abstractmethod
    def generate(
        self,
        prompt: str,
        out_path: Path,
        size: str = "1024x1024",
        reference_images: list[Path] | None = None,
        transparent: bool = True,
        model: str | None = None,
        visual_model: str | None = None,
        reference_mode: bool = False,
        params: dict | None = None,
    ) -> Path:
        """Generate an image and write PNG bytes to out_path. Returns out_path.

        `params` are provider-native, per-model generation knobs (Higgsfield's
        `--resolution 2k`, `--quality high`, `--aspect_ratio 4:3`, …), which vary by model
        and are ignored by providers that have no equivalent. Kept as an opaque dict rather
        than named arguments precisely because the accepted set is per-model and discovered
        at runtime — see HiggsfieldProvider._model_spec.

        `reference_mode=True` says `prompt` already fully explains what to do with
        `reference_images` (e.g. prompting.reference_instruction's "reproduce this exactly,
        change ONLY X") — a provider that would otherwise append its own boilerplate
        framing for reference images (an "extract this from its background" instruction
        written for the from-scratch generation case) must skip that here instead of
        stacking a second, uncoordinated instruction on top of the caller's own. A provider
        that passes reference images straight to a multimodal API with no text framing of
        its own has nothing to change and can ignore this."""
