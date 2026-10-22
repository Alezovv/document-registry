from __future__ import annotations

from dataclasses import dataclass
import gc
import logging
import time

from app.memory_metrics import process_memory_info, system_memory_status, trim_process_working_set
from app.resource_profile import ResourceProfile, resource_profile_from_environment

MIB = 1024 ** 2
ECONOMY_SOFT_BYTES = 850 * MIB
ECONOMY_RELEASE_OCR_BYTES = 930 * MIB
ECONOMY_BLOCK_OCR_BYTES = 1024 * MIB
ECONOMY_CRITICAL_BYTES = 1120 * MIB
ECONOMY_EMERGENCY_BYTES = 1150 * MIB
ECONOMY_ABSOLUTE_BYTES = 1200 * MIB
MIN_AVAILABLE_SYSTEM_BYTES = 256 * MIB

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    current_bytes: int
    peak_bytes: int
    available_system_bytes: int
    budget_bytes: int
    action: str
    release_ocr: bool
    allow_heavy_task: bool


class MemoryGovernor:
    """Small synchronous memory policy used only around heavy desktop tasks.

    It deliberately does not poll in a background thread. Sampling happens at
    OCR/document boundaries, which is where the application can actually free
    large buffers without interrupting an inference in progress.
    """

    def __init__(self, profile: ResourceProfile | None = None) -> None:
        self.profile = profile or resource_profile_from_environment()

    @property
    def enabled(self) -> bool:
        return bool(self.profile.economy_memory_policy)

    @property
    def budget_bytes(self) -> int:
        return ECONOMY_BLOCK_OCR_BYTES if self.enabled else 0

    def sample(self) -> MemoryDecision:
        proc = process_memory_info()
        _total, available = system_memory_status()
        action = "normal"
        release = False
        allow = True
        if self.enabled:
            if proc.current_bytes >= ECONOMY_ABSOLUTE_BYTES:
                action = "absolute"
                release = True
                allow = False
            elif proc.current_bytes >= ECONOMY_EMERGENCY_BYTES:
                action = "emergency"
                release = True
                allow = False
            elif proc.current_bytes >= ECONOMY_CRITICAL_BYTES:
                action = "critical"
                release = True
                allow = False
            elif proc.current_bytes >= ECONOMY_BLOCK_OCR_BYTES or (
                available and available < MIN_AVAILABLE_SYSTEM_BYTES
            ):
                action = "block"
                release = True
                allow = False
            elif proc.current_bytes >= ECONOMY_RELEASE_OCR_BYTES:
                action = "release_ocr"
                release = True
            elif proc.current_bytes >= ECONOMY_SOFT_BYTES:
                action = "cleanup"
        return MemoryDecision(
            proc.current_bytes,
            proc.peak_bytes,
            available,
            self.budget_bytes,
            action,
            release,
            allow,
        )

    def cleanup(self, *, trim: bool = False) -> MemoryDecision:
        gc.collect()
        if trim and self.enabled:
            trim_process_working_set()
        return self.sample()

    def prepare_heavy_task(self, release_callback=None, *, wait_seconds: float = 4.0) -> MemoryDecision:
        """Create headroom before OCR with a bounded wait in economy mode."""
        decision = self.sample()
        if not self.enabled:
            return decision

        if decision.action in {"cleanup", "release_ocr", "block", "critical", "emergency", "absolute"}:
            if decision.action in {"emergency", "absolute"}:
                logger.error(
                    "MEMORY EMERGENCY before heavy task: %.0f MB (action=%s)",
                    decision.current_bytes / MIB,
                    decision.action,
                )
            if decision.release_ocr and callable(release_callback):
                release_callback()
            decision = self.cleanup(trim=decision.action in {"block", "critical", "emergency", "absolute"})

        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        stagnant_samples = 0
        while not decision.allow_heavy_task and time.monotonic() < deadline:
            previous = decision
            time.sleep(0.35)
            decision = self.cleanup(trim=True)

            # If both process usage and available system memory fail to improve
            # over several samples after unloading the model/GC/trim, further
            # synchronous waiting is unlikely to help. Return the blocked
            # decision promptly so OCR can report a clear actionable error.
            process_released = decision.current_bytes <= previous.current_bytes - 16 * MIB
            system_recovered = (
                decision.available_system_bytes > 0
                and previous.available_system_bytes > 0
                and decision.available_system_bytes >= previous.available_system_bytes + 16 * MIB
            )
            if process_released or system_recovered:
                stagnant_samples = 0
            else:
                stagnant_samples += 1
            if stagnant_samples >= 4 and not decision.allow_heavy_task:
                logger.warning(
                    "Memory headroom did not improve after cleanup; skipping remaining wait "
                    "(current=%.0f MB, available=%.0f MB)",
                    decision.current_bytes / MIB,
                    decision.available_system_bytes / MIB,
                )
                break
        return decision

    def after_document(self, release_callback=None) -> MemoryDecision:
        """Free transient memory, unloading OCR only when pressure justifies it."""
        decision = self.sample()
        if not self.enabled:
            return decision
        if decision.action == "normal":
            return decision
        if decision.action in {"emergency", "absolute"}:
            logger.error(
                "MEMORY EMERGENCY after document: %.0f MB (action=%s)",
                decision.current_bytes / MIB,
                decision.action,
            )
        if decision.release_ocr and callable(release_callback):
            release_callback()
        return self.cleanup(trim=decision.action in {"block", "critical", "emergency", "absolute"})

    def finalize_batch(self, release_callback=None) -> MemoryDecision:
        """Return a completed OCR queue to a stable idle footprint.

        The model is always released at the end of a queue. Economy mode also
        asks Windows to reclaim unused working-set pages once, at a safe point,
        rather than trimming during inference.
        """
        if callable(release_callback):
            release_callback()
        gc.collect()
        if self.enabled:
            trim_process_working_set()
        return self.sample()
