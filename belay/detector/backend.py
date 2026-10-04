"""
The lab-side detector: a thin boundary over HAT's runtime, behind the `Backend`
protocol of belay.contract.service.

The lab runs HAT's `Monitor` as HAT ships it: `Monitor.from_pretrained` loads
the model and a lens pack, and `Monitor.generate` yields one `Step` per token,
with the detections HAT made on that token and the alerts its `WatchProfile`
raised. This module only decides what crosses the boundary, for the concepts
of the REPORT profile; everything else HAT detects stays in the lab (an
internal log). The overhead reported is HAT's own (`Step.monitor_ms`).

What crosses, per request, read from HAT's per-token scores (the lens
manager's `last_scores`: every lens HAT scored on the token):

    summaries  the sustained signal: one ConceptSummary per reported concept.
               A token is covered for a concept when HAT scored the concept or
               one of its ancestors (HAT's own hierarchy, lensed links); a
               covered token where the concept itself was not scored (its
               branch was cold, so HAT never loaded it) counts as 0. Reported
               concepts are never pinned resident: HAT loads them only when
               their parent fires, which is the point of the hierarchy.
    alerts     a signed alert at each reported lens's first crossing of its
               threshold in the request, handed to `emit` as its step arrives.
               A lens is reported if its path passes through a report-profile
               concept (the same rule as HAT's WatchProfile); its threshold is
               that concept's policy threshold (belay.contract.policy; the
               deepest reported concept on the path), or `threshold` if given.
    coverage   `watched` is the number of reported concepts covered on every
               token of the request.

Run settings (`run`), what HAT runs on every token:
    off     no monitor: the same model with Hugging Face `generate`, the same
            greedy settings (the benchmark's baseline)
    proxy   the university pack under HAT's hierarchical loading, watching the
            run profile (profiles/proxy-redlines.txt) and the report profile
    full    the same pack, watching every root concept (so everything HAT
            detects above threshold is an alert; only reported ones cross)
    wide    the First Light pack under HAT's hierarchical loading, watching
            every root concept (all lab-internal). The reported red lines live
            in the university pack, so a second HAT Monitor over that pack
            reads the same hidden states (`Monitor.read`) on each step; its
            alerts are the ones that cross. See `_generate_wide`.

Report profile: only its concepts cross the boundary (emit, the response's
alerts and summaries, the /alerts stream, the alert log). The identity (pack,
profile hash over the profile and its policy file) is the report profile's, so
one certificate covers every run setting.

Tamper modes (lab-side cheating, for the certifier to catch):
    none       honest
    off        reported readings thrown away, coverage still claimed: no
               alerts, and summaries that say every token was covered and all
               read 0 (a cold branch throughout). With `proxy` nothing else is
               watched, so no monitor runs at all and a plausible overhead is
               invented; with `full` and `wide` HAT runs and the reported
               readings are dropped.
    threshold  every reported threshold raised to 1.01, so nothing alerts and
               frac_above is 0; the scores themselves (mean, peak) are honest
    swap       the reported lenses loaded from an untrained pack (random
               weights, same file names; see swap_pack.py)

One worker thread owns the model; requests wait in a FIFO queue and go through
HAT one at a time.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from belay.contract import policy
from belay.contract.models import ConceptSummary, GenerateRequest
from belay.contract.service import BackendResult, Emit, RawAlert

from .hashing import hash_files, model_dir, sha256_bytes, weight_files
from .pack import Hierarchy, Key, lens_files, read_profile, watched_keys

log = logging.getLogger("belay.detector")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "google/gemma-4-E4B-it"
DEFAULT_PACK = Path("/var/home/poss/Documents/Code/HatCatDev/lens_packs/gemma-4-e4b-it_university-v3-contrasts-bands")
DEFAULT_PROFILE = ROOT / "profiles" / "proxy-redlines.txt"
WIDE_PACK = Path("/var/home/poss/Documents/Code/HatCatDev/lens_packs/gemma-4-e4b_first-light-v1-bf16")
CONCEPT_PACKS = Path("/var/home/poss/Documents/Code/HatCatDev/concept_packs")
RUN_MODES = ("off", "proxy", "full", "wide")
WATCH_MODES = RUN_MODES  # deprecated name
DEFAULT_INTERNAL_LOG = ROOT / "runs" / "detector-internal.jsonl"
TAMPER_MODES = ("none", "off", "threshold", "swap")
# Calibrated scores are percentiles of background, at most 1.0, so above 1.0
# nothing alerts while the lenses still run and cost what they should.
TAMPER_THRESHOLD = 1.01
# Tamper "off" with nothing else running reports this much monitoring per token.
FAKE_MONITOR_MS_PER_TOKEN = 0.7
# The lens manager settings of HAT's Monitor.from_pretrained, for the second
# Monitor of the wide run (which shares the first one's model).
HAT_MANAGER_DEFAULTS = dict(base_layers=[0], load_threshold=0.3, keep_top_k=100, max_loaded_lenses=1000)


def _peak(step) -> int:
    """Most lenses resident during a token: HAT's peak before pruning where it reports one."""
    return max(getattr(step, "peak_lenses", 0) or 0, step.loaded_lenses)


def concept_keys(concepts: Iterable[str], lensed: Iterable[Key],
                 child_to_parent: Mapping[Key, Key]) -> Tuple[Dict[str, Set[Key]], Dict[str, Set[Key]]]:
    """Each concept's own lenses, and their ancestors' lenses in HAT's hierarchy.

    HAT's hierarchy links only lensed concepts: those are the parents HAT
    expands through, so they are the ones whose scoring checks the branch. A
    concept with no lens has no keys and is never covered."""
    lensed = set(lensed)
    own = {c: {k for k in lensed if k[0] == c} for c in concepts}
    ancestors: Dict[str, Set[Key]] = {}
    for c, keys in own.items():
        found: Set[Key] = set()
        for key in keys:
            seen = {key}
            while key in child_to_parent and child_to_parent[key] not in seen:
                key = child_to_parent[key]
                seen.add(key)
                found.add(key)
        ancestors[c] = found - keys
    return own, ancestors


class Sustained:
    """
    The sustained signal of one request: what HAT read for each reported
    concept, token by token, from its lens manager's `last_scores`
    ((name, layer) -> (score, level), every lens scored on the token).
    """

    def __init__(self, concepts: List[str], own: Dict[str, Set[Key]], ancestors: Dict[str, Set[Key]],
                 thresholds: Dict[str, float]):
        self.concepts = list(concepts)
        self.own, self.ancestors, self.thresholds = own, ancestors, thresholds
        self.tokens = 0
        # concept -> [covered, scored, total, above, peak, peak_token]
        self._acc = {c: [0, 0, 0.0, 0, 0.0, None] for c in self.concepts}

    def add(self, index: int, scores: Mapping[Key, tuple]) -> None:
        self.tokens += 1
        for c in self.concepts:
            acc = self._acc[c]
            own = [float(scores[k][0]) for k in self.own.get(c, ()) if k in scores]
            if own:
                score = min(max(max(own), 0.0), 1.0)
                acc[1] += 1
            elif any(k in scores for k in self.ancestors.get(c, ())):
                score = 0.0  # HAT checked the branch above and found it cold
            else:
                continue
            acc[0] += 1
            acc[2] += score
            acc[3] += score >= self.thresholds[c]
            if own and (acc[5] is None or score > acc[4]):
                acc[4], acc[5] = score, index

    def summaries(self) -> List[ConceptSummary]:
        return [ConceptSummary(concept=c, covered_tokens=cov, scored_tokens=scored,
                               mean=min(round(total / cov, 6), 1.0) if cov else 0.0,
                               frac_above=round(above / cov, 6) if cov else 0.0,
                               peak=round(peak, 6), peak_token=peak_token)
                for c, (cov, scored, total, above, peak, peak_token) in self._acc.items()]

    def watched(self) -> int:
        """Concepts covered on every token of the request."""
        return sum(1 for cov, *_ in self._acc.values() if self.tokens and cov == self.tokens)

    @staticmethod
    def claimed(concepts: List[str], tokens: int) -> List[ConceptSummary]:
        """What tamper off reports: every token covered, nothing read (a cold branch throughout)."""
        return [ConceptSummary(concept=c, covered_tokens=tokens, scored_tokens=0, mean=0.0, frac_above=0.0,
                               peak=0.0, peak_token=None) for c in concepts]


@dataclass
class _Job:
    request: GenerateRequest
    future: Future
    emit: Optional[Emit] = None


@dataclass
class _Request:
    """What one request accumulates while HAT generates it."""
    emit: Optional[Emit]
    sustained: Optional[Sustained] = None  # None: no reported readings (no monitor, or tamper off)
    held: List[RawAlert] = field(default_factory=list)  # alerts when there is no emit
    internal: List[dict] = field(default_factory=list)  # lab-internal crossings, never reported
    fired: Set[tuple] = field(default_factory=set)  # (pack, concept, layer) that already alerted
    overhead_ms: float = 0.0
    resident_peak: int = 0

    def report(self, alert: RawAlert) -> None:
        if self.emit is not None:
            self.emit(alert)
        else:
            self.held.append(alert)


def wide_hierarchy_dir(pack: Path) -> Path:
    """Where a pack's concept hierarchy lives: bundled in the pack, or its source concept pack's."""
    pack = Path(pack)
    if (pack / "hierarchy").is_dir():
        return pack / "hierarchy"
    info = pack / "pack_info.json"
    if info.exists():
        source = json.loads(info.read_text()).get("source_pack")
        if source and (CONCEPT_PACKS / source / "hierarchy").is_dir():
            return CONCEPT_PACKS / source / "hierarchy"
    raise FileNotFoundError(f"no hierarchy for {pack}: bundle one with `headspace pack add-hierarchy`")


def root_concepts(monitor) -> List[str]:
    """Every top-level concept of a Monitor's pack: watching them all watches the whole pack."""
    return sorted({name for name, layer in monitor.lenses.concept_metadata if layer == 0})


class HatBackend:
    """Implements belay.contract.service.Backend over HAT's Monitor."""

    def __init__(
        self,
        run: Optional[str] = None,
        tamper: str = "none",
        model: str = DEFAULT_MODEL,
        pack: Path = DEFAULT_PACK,
        profile: Path = DEFAULT_PROFILE,
        report: Optional[Path] = None,
        wide_pack: Path = WIDE_PACK,
        wide_hierarchy: Optional[Path] = None,
        internal_log: Optional[Path] = DEFAULT_INTERNAL_LOG,
        threshold: Optional[float] = None,
        tamper_threshold: float = TAMPER_THRESHOLD,
        swap_dir: Path = ROOT / "runs" / "untrained-packs",
        max_new_tokens_cap: int = 2048,
        score_prompt: int = 0,
        device: str = "cuda",
        hash_cache: Optional[Path] = ROOT / "runs" / "model-hash.json",
        load_model: bool = True,
        watch: Optional[str] = None,
    ):
        """
        run: what HAT runs on every token (off, proxy, full, wide).
        profile: the run profile for `run="proxy"`.
        report: the report profile (default: `profile`). Only its concepts'
            alerts cross the boundary; it is what the identity claims and the
            certifier certifies.
        watch: deprecated alias for `run`.
        """
        if run is None:
            run = watch or "proxy"
        elif watch is not None and watch != run:
            raise ValueError(f"run={run!r} and watch={watch!r} disagree; watch is a deprecated alias")
        if run not in RUN_MODES:
            raise ValueError(f"run must be one of {RUN_MODES}")
        if tamper not in TAMPER_MODES:
            raise ValueError(f"tamper must be one of {TAMPER_MODES}")
        if run == "off" and tamper != "none":
            raise ValueError("tamper modes need lenses: use --run proxy, full or wide")
        self.run = self.watch = run  # `watch` is what coverage.watch reports
        self.tamper = tamper
        self.device = device
        self.max_new_tokens_cap = max_new_tokens_cap
        # Score every Nth input position while the model reads the prompt (0: off).
        self.score_prompt = max(0, int(score_prompt))
        self.internal_log = Path(internal_log) if internal_log else None
        self._internal_lock = threading.Lock()
        self._pack_dir = Path(pack)
        self._report_path = Path(report) if report else Path(profile)
        self._run_profile = Path(profile)
        self._swap_dir = Path(swap_dir)
        self.wide_pack = Path(wide_pack)
        self.wide_hierarchy = Path(wide_hierarchy) if wide_hierarchy else None

        # Identity: what the detector claims. Neither tampering nor the run
        # setting changes it: the pack and report profile are certified once.
        self.model_dir = model_dir(model)
        log.info("hashing weights in %s", self.model_dir)
        self.model_hash = hash_files(weight_files(self.model_dir), cache=hash_cache)
        self.report_keys: List[Key] = []
        self.report_concepts: List[str] = []
        if run == "off":
            self.pack, self.profile, self.profile_hash = "none", "none", sha256_bytes(b"")
        else:
            self.pack = self._pack_dir.name
            self.profile = self._report_path.stem
            self.profile_hash = policy.digest(self._report_path)
            self.report_concepts = read_profile(self._report_path)
            self.report_keys = watched_keys(self.report_concepts, lens_files(self._pack_dir),
                                            Hierarchy(self._pack_dir))
            if not self.report_keys:
                raise RuntimeError(f"no lenses in {self._pack_dir} for the report profile {self._report_path}")
        self._report_set = set(self.report_concepts)

        # Thresholds: the report profile's policy per concept; `threshold` (the
        # CLI's --threshold) overrides them all, and tamper threshold raises them.
        self.threshold = tamper_threshold if tamper == "threshold" else threshold
        self.policy = policy.load(self._report_path) if run != "off" else {}
        self.thresholds: Dict[str, float] = {
            c: self.threshold if self.threshold is not None
            else self.policy.get(c, policy.ConceptPolicy()).threshold
            for c in self.report_concepts}
        # Each reported lens's threshold: the deepest reported concept on its path.
        self._lens_thresholds: Dict[Key, float] = {}
        if self.report_keys:
            hierarchy = Hierarchy(self._pack_dir)
            for key in self.report_keys:
                on_path = [c for c in hierarchy.path(key) if c in self._report_set]
                self._lens_thresholds[key] = self.thresholds[on_path[-1]]
        self._own: Dict[str, Set[Key]] = {}
        self._ancestors: Dict[str, Set[Key]] = {}
        self.model = self.tokenizer = None
        # `monitor` is the HAT Monitor that generates; `reported` the one whose
        # alerts may cross (the same Monitor except for wide). None for off, and
        # for tamper off when nothing else is watched.
        self.monitor = None
        self.reported = None
        if load_model:
            self._load()

        self._queue: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._worker = threading.Thread(target=self._work, name="belay-generate", daemon=True)
        self._worker.start()

    # ----------------------------------------------------------------- loading

    @property
    def _reported_dir(self) -> Path:
        """The pack the reported lenses load from: the certified one, or the untrained copy (swap)."""
        if self.tamper != "swap":
            return self._pack_dir
        from .swap_pack import build

        out = build(self._pack_dir, self._report_path, self._swap_dir)
        log.warning("TAMPER swap: reported lenses from %s", out)
        return out

    def _watch_concepts(self) -> List[str]:
        """What the reported Monitor watches for proxy: the run profile and the report profile."""
        return sorted(set(read_profile(self._run_profile)) | self._report_set)

    def _load(self) -> None:
        import torch
        from headspace.runtime import Monitor, WatchProfile

        model_id = str(self.model_dir)
        if self.run == "off" or (self.run == "proxy" and self.tamper == "off"):
            from transformers import AutoModelForCausalLM, AutoTokenizer

            # As Monitor.from_pretrained loads it.
            self.tokenizer = AutoTokenizer.from_pretrained(model_id)
            self.model = AutoModelForCausalLM.from_pretrained(model_id, dtype=torch.bfloat16,
                                                              device_map=self.device).eval()
            if self.tamper == "off":
                log.warning("TAMPER off: no monitor runs; coverage is claimed anyway")
            return

        if self.run in ("proxy", "full"):
            self.monitor = Monitor.from_pretrained(model_id, self._reported_dir, device=self.device,
                                                   watch=WatchProfile([], threshold=self.threshold))
            concepts = self._watch_concepts() if self.run == "proxy" else root_concepts(self.monitor)
            self.monitor.watch.concepts = concepts
            self.reported = self.monitor
        else:
            # HAT's default threshold for the (uncalibrated) First Light pack.
            self.monitor = Monitor.from_pretrained(model_id, self.wide_pack, device=self.device,
                                                   hierarchy_dir=self.wide_hierarchy or wide_hierarchy_dir(self.wide_pack),
                                                   watch=WatchProfile([]))
            self.monitor.watch.concepts = root_concepts(self.monitor)
            text = self.monitor.model.config.get_text_config()
            if self.monitor.lenses.hidden_dim not in (None, text.hidden_size):
                raise RuntimeError(f"{self.wide_pack.name} lenses read {self.monitor.lenses.hidden_dim}-wide "
                                   f"hidden states; the model has {text.hidden_size}")
            if self.tamper != "off":
                from headspace.monitoring.lens_manager import DynamicLensManager

                manager = DynamicLensManager(lenses_dir=self._reported_dir, device=self.device,
                                             **HAT_MANAGER_DEFAULTS)
                # Its alerts and summaries are read from its scores (`_observe`): no HAT watch needed.
                self.reported = Monitor(self.monitor.model, self.monitor.tokenizer, manager,
                                        watch=WatchProfile([], threshold=self.threshold))
        self.model, self.tokenizer = self.monitor.model, self.monitor.tokenizer
        if self.reported is not None:
            lenses = self.reported.lenses
            self._own, self._ancestors = concept_keys(self.report_concepts, lenses.concept_metadata,
                                                      lenses.child_to_parent)
        log.info("run %s: %s, %d lenses, watching %d concepts; reported %d lenses, thresholds %s, tamper %s",
                 self.run, self.monitor.lenses.lenses_dir.name, self.monitor.total_lenses,
                 len(self.monitor.watch.concepts), len(self.report_keys), self.thresholds, self.tamper)

    # ------------------------------------------------------------ the protocol

    @property
    def watched(self) -> int:
        """Reported concepts: the report profile's."""
        return len(self.report_concepts)

    def generate(self, request: GenerateRequest, emit: Optional[Emit] = None) -> BackendResult:
        """Called from many server threads; queues the request and waits for it.

        With `emit`, each reported alert is handed over as its step arrives
        from HAT (and is not repeated in the result)."""
        job = _Job(request, Future(), emit)
        self._queue.put(job)
        return job.future.result()

    def close(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=5)

    # ------------------------------------------------------------- the worker

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job.future.set_result(self._generate(job.request, job.emit))
            except BaseException as e:  # noqa: BLE001 - hand every failure back to its caller
                log.exception("generation failed")
                job.future.set_exception(e)

    def _generate(self, request: GenerateRequest, emit: Optional[Emit]) -> BackendResult:
        messages = [m.model_dump() for m in request.messages]
        max_new = max(1, min(request.max_tokens, self.max_new_tokens_cap))
        state = _Request(emit)
        if self.reported is not None and self.tamper != "off":
            state.sustained = Sustained(self.report_concepts, self._own, self._ancestors, self.thresholds)
        if self.monitor is None:
            ids = self._generate_plain(messages, max_new)
            watched = 0
            if self.tamper == "off":
                # The lie extends to cost: report what honest monitoring would have taken.
                state.overhead_ms = len(ids) * FAKE_MONITOR_MS_PER_TOKEN * (0.9 + 0.2 * random.random())
                state.resident_peak = len(self.report_keys)
        else:
            ids = (self._generate_wide if self.run == "wide" else self._generate_monitored)(messages, max_new, state)
        summaries: List[ConceptSummary] = []
        watched = 0
        if self.tamper == "off":  # the lie: every token covered, every concept cold
            summaries, watched = Sustained.claimed(self.report_concepts, len(ids)), self.watched
        elif state.sustained is not None:
            summaries, watched = state.sustained.summaries(), state.sustained.watched()
        prompt_summaries = self._read_prompt(messages) if self.score_prompt else None
        result = BackendResult(
            prompt_summaries=prompt_summaries,
            completion=self.tokenizer.decode(ids, skip_special_tokens=True),
            tokens=len(ids),
            alerts=state.held,
            watched=watched,
            resident_peak=state.resident_peak,
            overhead_ms=round(state.overhead_ms, 3),
            summaries=summaries,
        )
        if self.run != "off":
            self._write_internal(request, result, state)
        return result

    def _read_prompt(self, messages: List[dict]) -> Optional[List[ConceptSummary]]:
        """
        The reported concepts while the model reads its input: HAT's own
        Monitor.read over every Nth prompt position (and the last), with the
        same hierarchy and coverage rule as the reply. Run after generation so
        the reply is scored exactly as without it.
        """
        import torch

        if self.reported is None:
            return None
        if self.tamper == "off":
            return Sustained.claimed(self.report_concepts, 0)
        enc = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt",
                                                 return_dict=True)
        input_ids = enc["input_ids"].to(self.model.device)
        with torch.inference_mode():
            out = self.model(input_ids, use_cache=False, output_hidden_states=True)  # as HAT calls it
        hidden = out.hidden_states
        n_layers = len(hidden) - 1
        layers = set(self.reported.required_model_layers)
        if self.reported.hidden_layer is not None:
            layers.add(self.reported.hidden_layer if self.reported.hidden_layer >= 0
                       else self.reported.hidden_layer + n_layers)
        n = input_ids.shape[1]
        positions = sorted(set(range(0, n, self.score_prompt)) | {n - 1})
        reading = Sustained(self.report_concepts, self._own, self._ancestors, self.thresholds)
        for pos in positions:
            self.reported.read({layer: hidden[layer + 1][:, pos, :] for layer in layers})
            reading.add(pos, getattr(self.reported.lenses, "last_scores", {}))
        return reading.summaries()

    def _generate_plain(self, messages: List[dict], max_new: int) -> List[int]:
        """The baseline: the same model, prompt and greedy decoding as HAT's Monitor.generate, no lenses."""
        import torch

        enc = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt",
                                                 return_dict=True)
        input_ids = enc["input_ids"].to(self.model.device)
        stop = self._stop_ids()
        with torch.inference_mode():
            out = self.model.generate(input_ids, attention_mask=torch.ones_like(input_ids),
                                      max_new_tokens=max_new, do_sample=False, temperature=None, top_p=None,
                                      top_k=None, eos_token_id=sorted(stop),
                                      pad_token_id=self.tokenizer.pad_token_id)
        ids = out[0, input_ids.shape[1]:].tolist()[:max_new]
        for i, t in enumerate(ids):
            if t in stop:  # HAT keeps the stop token as its last step; so does this
                return ids[:i + 1]
        return ids

    def _stop_ids(self) -> set:
        """As HAT's Monitor._stop_ids: the tokenizer's end of sequence plus the generation config's."""
        ids = {self.tokenizer.eos_token_id}
        eos = getattr(getattr(self.model, "generation_config", None), "eos_token_id", None)
        ids.update(eos if isinstance(eos, (list, tuple)) else [eos])
        return {i for i in ids if i is not None}

    def _generate_monitored(self, messages: List[dict], max_new: int, state: _Request) -> List[int]:
        """proxy and full: one HAT Monitor generates, and its alerts for reported concepts cross."""
        ids = []
        for step in self.monitor.generate(messages, max_new_tokens=max_new, chat=True):
            t_token = time.time()
            ids.append(step.token_id)
            state.overhead_ms += step.monitor_ms
            state.resident_peak = max(state.resident_peak, _peak(step))
            self._handle(step.index, step.alerts, t_token, self.pack, state)
            self._observe(step.index, self.monitor.lenses, t_token, state)
        return ids

    def _generate_wide(self, messages: List[dict], max_new: int, state: _Request) -> List[int]:
        """
        wide: HAT's Monitor generates with the First Light pack. The reported
        lenses are in the university pack, so a second HAT Monitor reads the
        hidden states of the same forward pass with `Monitor.read`, which takes
        hidden states. HAT's Monitor.generate does not hand its hidden states
        out, so a forward hook keeps a reference to the last forward pass's
        (it computes nothing). One forward pass per token; two lens packs read it.
        """
        captured: Dict[str, tuple] = {}
        handle = None
        if self.reported is not None:
            def keep(module, args, output):
                captured["hidden"] = output.hidden_states

            handle = self.monitor.model.register_forward_hook(keep)
        ids = []
        try:
            for step in self.monitor.generate(messages, max_new_tokens=max_new, chat=True):
                t_step = time.time()
                ids.append(step.token_id)
                state.overhead_ms += step.monitor_ms
                resident = _peak(step)
                self._handle(step.index, step.alerts, t_step, self.monitor.lenses.lenses_dir.name, state)
                if self.reported is not None:
                    hidden = captured.pop("hidden")
                    layers = self.reported.required_model_layers
                    _, ms = self.reported.read({layer: hidden[layer + 1][:, -1, :] for layer in layers})
                    state.overhead_ms += ms
                    resident += len(self.reported.lenses.cache.loaded_lenses)
                    self._observe(step.index, self.reported.lenses, time.time(), state)
                state.resident_peak = max(state.resident_peak, resident)
        finally:
            if handle is not None:
                handle.remove()
        return ids

    def _handle(self, index: int, alerts, t_token: float, pack: str, state: _Request) -> None:
        """HAT's alerts for concepts that are not reported: lab-internal, once per concept per request.

        Reported concepts are read from the scores by `_observe`, against their policy thresholds."""
        for d in alerts:
            if pack == self.pack and self._report_set.intersection(d.path):
                continue
            key = (pack, d.concept, d.layer)
            if key in state.fired:
                continue
            state.fired.add(key)
            state.internal.append({"pack": pack, "concept": d.concept, "score": float(d.score),
                                   "token_index": index, "path": list(d.path), "t_token": t_token})

    def _observe(self, index: int, lenses, t_token: float, state: _Request) -> None:
        """One token of the reported lens manager: add it to the summaries, and alert on first crossings.

        `lenses.last_scores` is every lens HAT scored on the token. Nothing
        here loads a lens: a reported concept is read only when HAT's cascade
        reached it."""
        if state.sustained is None:  # tamper off: the reported readings are thrown away
            return
        scores = lenses.last_scores
        state.sustained.add(index, scores)
        crossed = []
        for key, threshold in self._lens_thresholds.items():
            hit = scores.get(key)
            if hit is not None and hit[0] >= threshold and (self.pack, *key) not in state.fired:
                crossed.append((float(hit[0]), key, int(hit[1])))
        for score, key, level in sorted(crossed, key=lambda c: c[0], reverse=True):  # highest first, as HAT
            state.fired.add((self.pack, *key))
            state.report(RawAlert(concept=key[0], score=score, token_index=index,
                                  path=list(lenses.get_concept_path(key[0], level)), t_token=t_token))

    def _write_internal(self, request: GenerateRequest, result: BackendResult, state: _Request) -> None:
        """The lab-internal log: what HAT detected but was not reported. Never the boundary log."""
        if self.internal_log is None:
            return
        base = {"t": time.time(), "request_id": request.request_id, "session_id": request.session_id,
                "agent": request.agent, "run": self.run, "tamper": self.tamper}
        lines = [{**base, "kind": "request", "tokens": result.tokens, "resident_peak": result.resident_peak,
                  "watched": result.watched, "reported": len(self.report_keys),
                  "total_lenses": self.monitor.total_lenses if self.monitor is not None else 0,
                  "overhead_ms": result.overhead_ms}]
        lines.extend({**base, "kind": "crossing", **c} for c in state.internal)
        with self._internal_lock:
            self.internal_log.parent.mkdir(parents=True, exist_ok=True)
            with self.internal_log.open("a", encoding="utf-8") as f:
                f.writelines(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
