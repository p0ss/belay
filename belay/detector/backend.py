"""
The real lab-side detector: HAT's lenses on Gemma 4 E4B-it, behind the
`Backend` protocol of belay.contract.service.

One model serves many sessions. Requests go into a FIFO queue and a single
worker thread runs them, `max_batch` at a time: with max_batch=1 it is a plain
request queue (one generation at a time); above 1 the worker takes whatever is
waiting (up to max_batch) and generates it as one left-padded batch.

The lenses read the model's hidden states token by token, inside generation,
through a forward hook: every forward pass (one per new token) scores the
watched lenses on the last position of each model layer they read, exactly
the states HAT's Monitor reads (hidden_states[L + 1] for model layer L). A
watched lens alerts at the first token whose calibrated score crosses the
threshold; `t_token` is when that token's forward pass finished.

What the detector runs (`run`) is separate from what it reports (`report`):

Run settings, the lenses scored on every token:
    off     the model with no lenses (the benchmark's baseline)
    proxy   the lenses of a watch profile (profiles/proxy-redlines.txt), and
            every lens beneath them in the hierarchy, pinned resident
    full    every lens in the university pack pinned resident (the upper bound)
    wide    the First Light pack (about 7,950 lenses, hierarchy layers 0-6)
            under HAT's dynamic hierarchical loading, so only a few dozen are
            resident at a time, plus the report profile's lenses from the
            university pack, pinned

Report profile: only alerts for concepts in it cross the boundary (emit,
response alerts, the /alerts stream, the alert log). Its lenses are always
pinned and scored whatever the run setting (except off). Everything else the
lab scores stays inside: crossings of non-reported lenses go to a separate
lab-internal log, never to the boundary. The identity (pack, profile hash) is
the report profile's, so one certificate covers every run setting.

Tamper modes (lab-side cheating, for the certifier to catch), applied to the
reported lenses:
    none       honest
    off        reported lenses' readings thrown away (none run if nothing else
               does), but coverage still claims them
    threshold  threshold raised so nearly nothing alerts
    swap       the reported lenses loaded from an untrained pack
               (random weights, same file names; see swap_pack.py)
"""

from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence


from belay.contract.models import GenerateRequest
from belay.contract.service import BackendResult, Emit, RawAlert

from .hashing import hash_files, model_dir, profile_hash, sha256_bytes, weight_files
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
# Calibrated scores are percentiles of background, at most 1.0 (beyond every
# background state). On-topic text saturates at 1.0 for many tokens, so a
# threshold of 1.0 still alerts on clear cases (a subtler tamper); above 1.0
# nothing alerts while the lenses still run and cost what they should.
TAMPER_THRESHOLD = 1.01
# Tamper "off" reports this much monitoring per token (about the proxy profile's real cost).
FAKE_MONITOR_MS_PER_TOKEN = 0.7


@dataclass
class _Job:
    request: GenerateRequest
    future: Future
    queued: float
    emit: Optional[Emit] = None


class Lenses:
    """The resident lenses: HAT's DynamicLensManager loads them, this scores them in batch."""

    def __init__(self, load_dir: Path, keys: Sequence[Key], hierarchy: Hierarchy, device: str,
                 fused: bool = True):
        from headspace.monitoring.lens_manager import DynamicLensManager
        from headspace.monitoring.lens_types import Lens

        self.manager = DynamicLensManager(
            lenses_dir=Path(load_dir), device=device, base_layers=[],
            max_loaded_lenses=max(1000, 2 * len(keys)), keep_top_k=max(1000, 2 * len(keys)),
        )
        missing = [k for k in keys if k not in self.manager.concept_metadata]
        if missing:
            raise RuntimeError(f"pack has no lens metadata for {missing[:5]}")
        self.manager._load_concepts(list(keys), reason="watch")
        self.keys = list(keys)
        self.lenses = [self.manager.cache.loaded_lenses[k] for k in self.keys]
        for k, lens in zip(self.keys, self.lenses):
            if not isinstance(lens, Lens):
                raise RuntimeError(f"{k} is not a multi-layer band lens; only band packs are supported")
        self.paths = [hierarchy.path(k) for k in self.keys]
        self.layers = sorted({layer for lens in self.lenses for layer in lens.model_layers})
        self.dtype = next(self.lenses[0].parameters()).dtype if self.lenses else None
        self.calibrated = bool(self.manager.probe_calibrated)
        self.fused = FusedProbes.build(self.lenses, self.layers) if fused else None

    def __len__(self) -> int:
        return len(self.lenses)

    def score(self, states: Dict[int, "torch.Tensor"]) -> "torch.Tensor":
        """Scores [n_lenses, batch] from model layer -> hidden states [batch, hidden]."""
        # As HAT's Monitor.read: float, then the manager's normalisation.
        normed = {layer: self.manager._normalize(h.float()).to(self.dtype) for layer, h in states.items()}
        if self.fused is not None:
            return self.fused(normed)
        return self.score_loop(normed)

    def score_loop(self, normed: Dict[int, "torch.Tensor"]) -> "torch.Tensor":
        """Lens by lens, probe by probe, as HAT's Lens.forward computes it (without its per-probe sync)."""
        import torch

        rows = []
        for lens in self.lenses:
            probe_scores = []
            for layer in lens.model_layers:
                prob = lens.probes[str(layer)](normed[layer]).reshape(-1)
                probe_scores.append(lens._percentile(prob, layer) if lens.calibrated else prob.float())
            stacked = torch.stack(probe_scores)
            # Calibrated probes combine by max, raw ones by mean.
            rows.append(stacked.max(dim=0).values if lens.calibrated else stacked.mean(dim=0))
        return torch.stack(rows)


class FusedProbes:
    """
    Every probe of every resident lens in a few batched matmuls.

    HAT runs each probe as its own small MLP (Linear-ReLU-Linear-ReLU-Linear,
    sigmoid), so a pack of hundreds of probes costs thousands of kernel launches
    per token. Here the probes' weights are stacked and run with bmm, the
    calibration quantiles are stacked for one batched searchsorted, and each
    lens takes the max (calibrated) or mean (raw) of its probes. Same numbers,
    far fewer launches. `build` returns None for a pack it cannot fuse.
    """

    def __init__(self, layers, layer_idx, lens_idx, n_lenses, w, b, quantiles):
        self.layers, self.layer_idx, self.lens_idx, self.n_lenses = layers, layer_idx, lens_idx, n_lenses
        self.w, self.b, self.quantiles = w, b, quantiles

    @classmethod
    def build(cls, lenses: Sequence, layers: Sequence[int]) -> Optional["FusedProbes"]:
        import torch
        from headspace.monitoring.lens_types import SimpleMLP

        mlps, layer_idx, lens_idx, quantiles = [], [], [], []
        calibrated = {bool(lens.calibrated) for lens in lenses}
        if len(calibrated) != 1:
            return None
        for i, lens in enumerate(lenses):
            for layer in lens.model_layers:
                mlp = lens.probes[str(layer)]
                if not isinstance(mlp, SimpleMLP) or mlp.has_layer_norm:
                    return None
                mlps.append(mlp)
                layer_idx.append(list(layers).index(layer))
                lens_idx.append(i)
                if lens.calibrated:
                    quantiles.append(getattr(lens, f"quantiles_{layer}"))
        linears = [[m for m in mlp.net if isinstance(m, torch.nn.Linear)] for mlp in mlps]
        if any(len(ls) != 3 for ls in linears):
            return None
        shapes = {tuple(tuple(l.weight.shape) for l in ls) for ls in linears}
        if len(shapes) != 1 or (quantiles and len({q.numel() for q in quantiles}) != 1):
            return None
        device = linears[0][0].weight.device
        w = [torch.stack([ls[k].weight.detach() for ls in linears]) for k in range(3)]
        b = [torch.stack([ls[k].bias.detach() for ls in linears]).unsqueeze(1) for k in range(3)]
        q = torch.stack([x.float() for x in quantiles]).to(device) if quantiles else None
        return cls(list(layers), torch.tensor(layer_idx, device=device), torch.tensor(lens_idx, device=device),
                   len(lenses), w, b, q)

    def __call__(self, normed: Dict[int, "torch.Tensor"]) -> "torch.Tensor":
        import torch

        x = torch.stack([normed[layer] for layer in self.layers])[self.layer_idx]  # [P, B, D]
        h = torch.baddbmm(self.b[0], x, self.w[0].transpose(1, 2)).relu_()
        h = torch.baddbmm(self.b[1], h, self.w[1].transpose(1, 2)).relu_()
        prob = torch.sigmoid(torch.baddbmm(self.b[2], h, self.w[2].transpose(1, 2)).squeeze(-1)).float()  # [P, B]
        index = self.lens_idx[:, None].expand_as(prob)
        if self.quantiles is None:
            out = torch.zeros(self.n_lenses, prob.shape[1], device=prob.device)
            return out.scatter_reduce(0, index, prob, "mean", include_self=False)
        q = self.quantiles
        k = q.shape[1]
        idx = torch.searchsorted(q, prob.contiguous()).clamp(1, k - 1)
        lo, hi = q.gather(1, idx - 1), q.gather(1, idx)
        frac = torch.where(hi > lo, (prob - lo) / (hi - lo), torch.zeros_like(lo)).clamp(0, 1)
        pct = (idx - 1 + frac) / (k - 1)
        pct = torch.where(prob <= q[:, :1], torch.zeros_like(pct), torch.where(prob >= q[:, -1:], torch.ones_like(pct), pct))
        out = torch.zeros(self.n_lenses, prob.shape[1], device=prob.device)
        return out.scatter_reduce(0, index, pct, "amax", include_self=False)


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


class DynamicPack:
    """
    A large hierarchical pack under HAT's dynamic loading, as HAT's runtime
    Monitor runs it: the top hierarchy layer is resident; each token the
    resident lenses are scored, the children of the top-k are loaded and
    scored, and the rest are pruned back to a warm cache. Only a few dozen
    lenses are scored per token out of thousands.

    Single-probe lenses read one model layer: the pack's declared
    `model_layer`, else the last one (HAT's Monitor default).
    """

    def __init__(self, pack_dir: Path, device: str, hierarchy_dir: Optional[Path] = None, top_k: int = 10,
                 max_loaded_lenses: int = 1000, ram_mb: int = 0, threshold: float = 0.5):
        from headspace.monitoring.lens_manager import DynamicLensManager

        pack_dir = Path(pack_dir)
        # The defaults of HAT's Monitor.from_pretrained (HatCat's reference server).
        self.manager = DynamicLensManager(
            lenses_dir=pack_dir, layers_data_dir=Path(hierarchy_dir or wide_hierarchy_dir(pack_dir)),
            device=device, base_layers=[0], load_threshold=0.3, keep_top_k=100,
            max_loaded_lenses=max_loaded_lenses,
        )
        if not self.manager.concept_metadata:
            raise RuntimeError(f"no concepts with lenses in {pack_dir}")
        if ram_mb:
            self.manager.preload_pack_to_ram(max_ram_mb=ram_mb)
        self.name = pack_dir.name
        self.top_k = top_k
        self.threshold = threshold
        self.model_layer = self.manager.model_layer  # None: the last model layer
        # Children are loaded and scored inside detect_and_expand, then pruned
        # before it returns: count what was resident just before the prune.
        self.step_peak = self.resident
        cache = self.manager.cache
        prune = cache.prune_to_top_k

        def counted_prune(*args, **kwargs):
            self.step_peak = max(self.step_peak, len(cache.loaded_lenses))
            return prune(*args, **kwargs)

        cache.prune_to_top_k = counted_prune

    @property
    def total(self) -> int:
        return len(self.manager.concept_metadata)

    @property
    def hidden_dim(self) -> Optional[int]:
        return self.manager.hidden_dim

    @property
    def resident(self) -> int:
        """Lenses scored on the next token."""
        return len(self.manager.cache.loaded_lenses)

    @property
    def warm(self) -> int:
        """Lenses held on the device but not scored (HAT's warm cache)."""
        return len(self.manager.cache.warm_cache)

    def read(self, hidden: "torch.Tensor") -> List[tuple]:
        """Detections (concept, score, hierarchy layer) for one position [hidden] or [1, hidden].

        Afterwards `step_peak` is the most lenses resident during the read."""
        self.step_peak = self.resident
        results, _ = self.manager.detect_and_expand(hidden, top_k=self.top_k)
        self.step_peak = max(self.step_peak, self.resident)
        return results

    def path(self, concept: str, layer: int) -> List[str]:
        return self.manager.get_concept_path(concept, layer)


class FusedWidePack:
    """
    A large single-layer pack scored in full on every token, in one pass.

    HAT's dynamic loading keeps few lenses resident but scores each one as its
    own small MLP, a few hundred kernel launches per token per row. When every
    lens reads the same model layer and shares one shape (First Light:
    Linear 2560->128, ReLU, 128->64, ReLU, 64->1, sigmoid), the first layers
    of all lenses are one matmul and the rest are two batched matmuls, for
    every row of the batch at once. Lenses are loaded breadth-first through the
    hierarchy until `budget_mb` of weights is used. Scores are the lenses' raw
    probabilities (no calibration; First Light has none).
    """

    def __init__(self, pack_dir: Path, device: str, hierarchy_dir: Optional[Path] = None,
                 budget_mb: int = 2000, threshold: float = 0.5, **_):
        import torch
        from headspace.monitoring.lens_manager import DynamicLensManager

        pack_dir = Path(pack_dir)
        # For metadata and hierarchy paths only; the lenses are loaded here.
        self.manager = DynamicLensManager(
            lenses_dir=pack_dir, layers_data_dir=Path(hierarchy_dir or wide_hierarchy_dir(pack_dir)),
            device="cpu", base_layers=[0], load_threshold=0.3, keep_top_k=100, max_loaded_lenses=100,
        )
        self.name = pack_dir.name
        self.threshold = threshold
        self.model_layer = self.manager.model_layer
        self.total_in_pack = len(self.manager.concept_metadata)

        keys = sorted(self.manager.concept_metadata, key=lambda k: (k[1], k[0]))
        w1, b1, w2, b2, w3, b3, self.keys = [], [], [], [], [], [], []
        used, shape = 0, None
        for key in keys:
            path = getattr(self.manager.concept_metadata[key], "activation_lens_path", None)
            if not path or not Path(path).exists():
                continue
            sd = torch.load(path, map_location="cpu", weights_only=True)
            try:
                lens = (sd["net.0.weight"], sd["net.0.bias"], sd["net.3.weight"], sd["net.3.bias"],
                        sd["net.6.weight"], sd["net.6.bias"])
            except KeyError:
                continue
            s = tuple(tuple(x.shape) for x in lens)
            shape = shape or s
            if s != shape:  # e.g. simplex lenses over a wider input
                continue
            size = sum(x.numel() * x.element_size() for x in lens)
            if used + size > budget_mb * 1e6:
                break
            used += size
            for acc, x in zip((w1, b1, w2, b2, w3, b3), lens):
                acc.append(x)
            self.keys.append(key)
        if not self.keys:
            raise RuntimeError(f"no fusable lenses in {pack_dir}")
        dt = torch.bfloat16
        self.w1 = torch.cat(w1).to(device, dt)                     # [P*H1, D]
        self.b1 = torch.cat(b1).to(device, dt)                     # [P*H1]
        self.w2 = torch.stack(w2).transpose(1, 2).contiguous().to(device, dt)   # [P, H1, H2]
        self.b2 = torch.stack(b2).unsqueeze(1).to(device, dt)      # [P, 1, H2]
        self.w3 = torch.stack(w3).transpose(1, 2).contiguous().to(device, dt)   # [P, H2, 1]
        self.b3 = torch.stack(b3).unsqueeze(1).to(device, dt)      # [P, 1, 1]
        self.h1 = w2[0].shape[1]
        self.hidden_dim = w1[0].shape[1]
        self.weight_mb = used / 1e6
        self.step_peak = len(self.keys)

    @property
    def total(self) -> int:
        return len(self.keys)

    @property
    def resident(self) -> int:
        return len(self.keys)

    @property
    def warm(self) -> int:
        return 0

    def read_batch(self, hidden: "torch.Tensor") -> "torch.Tensor":
        """Probabilities [B, P] for hidden states [B, D]."""
        import torch

        x = hidden.to(self.w1.dtype)
        h = torch.nn.functional.linear(x, self.w1, self.b1).relu_()               # [B, P*H1]
        h = h.view(x.shape[0], -1, self.h1).transpose(0, 1)                       # [P, B, H1]
        h = torch.baddbmm(self.b2, h, self.w2).relu_()                            # [P, B, H2]
        return torch.sigmoid(torch.baddbmm(self.b3, h, self.w3)).squeeze(-1).T.float()  # [B, P]

    def path(self, concept: str, layer: int) -> List[str]:
        return self.manager.get_concept_path(concept, layer)


class HatBackend:
    """Implements belay.contract.service.Backend."""

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
        wide_top_k: int = 10,
        wide_ram_mb: int = 0,
        wide_threshold: float = 0.5,
        wide_mode: str = "fused",
        wide_budget_mb: int = 2000,
        internal_log: Optional[Path] = DEFAULT_INTERNAL_LOG,
        threshold: Optional[float] = None,
        tamper_threshold: float = TAMPER_THRESHOLD,
        swap_dir: Path = ROOT / "runs" / "untrained-packs",
        max_batch: int = 1,
        batch_window_ms: float = 5.0,
        max_new_tokens_cap: int = 2048,
        device: str = "cuda",
        hash_cache: Optional[Path] = ROOT / "runs" / "model-hash.json",
        load_model: bool = True,
        watch: Optional[str] = None,
    ):
        """
        run: the lenses scored on every token (off, proxy, full, wide).
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
        self.max_batch = max(1, max_batch)
        self.batch_window = batch_window_ms / 1000
        self.max_new_tokens_cap = max_new_tokens_cap
        self.internal_log = Path(internal_log) if internal_log else None
        self._internal_lock = threading.Lock()
        pack = Path(pack)
        profile = Path(profile)
        report = Path(report) if report else profile
        self._pack_dir, self._report_path = pack, report

        # Identity: what the detector claims. Tampering never changes it, and
        # neither does the run setting: the pack and report profile are
        # certified once, whatever else the lab runs.
        self.model_dir = model_dir(model)
        log.info("hashing weights in %s", self.model_dir)
        self.model_hash = hash_files(weight_files(self.model_dir), cache=hash_cache)
        self.lenses: Optional[Lenses] = None
        self.dynamic = None  # DynamicPack or FusedWidePack
        self.report_keys: List[Key] = []
        self.pinned_keys: List[Key] = []
        if run == "off":
            self.pack, self.profile, self.profile_hash = "none", "none", sha256_bytes(b"")
        else:
            hierarchy = Hierarchy(pack)
            lensed = lens_files(pack)
            self.pack = pack.name
            self.profile = report.stem
            self.profile_hash = profile_hash(report)
            self.report_keys = watched_keys(read_profile(report), lensed, hierarchy)
            if not self.report_keys:
                raise RuntimeError(f"no lenses in {pack} for the report profile {report}")
            if run == "proxy":
                run_keys = watched_keys(read_profile(profile), lensed, hierarchy)
            elif run == "full":
                run_keys = list(lensed)
            else:  # wide: the reported lenses pinned; the wide pack loads dynamically
                run_keys = []
            # The reported lenses always run, so the reported subset is always scored.
            self.pinned_keys = sorted(set(run_keys) | set(self.report_keys))
        self._report_set = set(self.report_keys)

        self.wide_args = dict(pack_dir=Path(wide_pack), hierarchy_dir=wide_hierarchy, top_k=wide_top_k,
                              ram_mb=wide_ram_mb, threshold=wide_threshold)
        if wide_mode not in ("fused", "dynamic"):
            raise ValueError("wide_mode must be fused or dynamic")
        self.wide_mode = wide_mode
        self.wide_budget_mb = wide_budget_mb
        self.threshold = threshold
        if tamper == "threshold":
            self.threshold = tamper_threshold

        self.model = self.tokenizer = None
        self.monitor = None
        self._wide_layer: Optional[int] = None
        if load_model:
            self._load(pack, report, swap_dir)

        self._queue: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._worker = threading.Thread(target=self._run, name="belay-generate", daemon=True)
        self._worker.start()

    # ----------------------------------------------------------------- loading

    def _load(self, pack: Path, report: Path, swap_dir: Path) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        log.info("loading %s", self.model_dir)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir, local_files_only=True)
        self.tokenizer.padding_side = "left"
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_dir, dtype=torch.bfloat16, device_map=self.device, local_files_only=True,
        ).eval()

        if self.run == "off":
            return
        load_dir = pack
        if self.tamper == "swap":
            from .swap_pack import build

            # The reported lenses are the ones swapped.
            load_dir = build(pack, report, swap_dir)
            log.warning("TAMPER swap: reported lenses from %s", load_dir)
        self.lenses = Lenses(load_dir, self.pinned_keys, Hierarchy(pack), self.device)

        from headspace.runtime import Monitor, WatchProfile

        concepts = sorted({k[0] for k in self.report_keys})
        self.monitor = Monitor(self.model, self.tokenizer, self.lenses.manager,
                               watch=WatchProfile(concepts, threshold=self.threshold))
        self.threshold = self.monitor.watch.threshold
        log.info("pinned %d lenses (%d reported) over model layers %s, threshold %s, tamper %s",
                 len(self.lenses), len(self.report_keys), self.lenses.layers, self.threshold, self.tamper)

        if self.run == "wide":
            if self.wide_mode == "fused":
                self.dynamic = FusedWidePack(device=self.device, budget_mb=self.wide_budget_mb, **self.wide_args)
                log.info("wide fused: %d of %d lenses, %.0f MB of weights, all scored every token",
                         self.dynamic.total, self.dynamic.total_in_pack, self.dynamic.weight_mb)
            else:
                self.dynamic = DynamicPack(device=self.device, **self.wide_args)
            text = self.model.config.get_text_config()
            if self.dynamic.hidden_dim != text.hidden_size:
                raise RuntimeError(
                    f"{self.dynamic.name} lenses read {self.dynamic.hidden_dim}-wide hidden states; "
                    f"{self.model_dir.name} has {text.hidden_size}")
            layer = self.dynamic.model_layer
            self._wide_layer = text.num_hidden_layers - 1 if layer is None else layer
            mode = (f"fused, all scored every token" if isinstance(self.dynamic, FusedWidePack)
                    else f"dynamic loading, top-k {self.dynamic.top_k}")
            log.info("wide: %s, %d lenses, %d resident to start, model layer %d (%s)",
                     self.dynamic.name, self.dynamic.total, self.dynamic.resident, self._wide_layer, mode)

    def _stop_ids(self) -> set:
        if self.monitor is not None:
            return self.monitor._stop_ids()
        ids = {self.tokenizer.eos_token_id}
        eos = getattr(self.model.generation_config, "eos_token_id", None)
        ids.update(eos if isinstance(eos, (list, tuple)) else [eos])
        return {i for i in ids if i is not None}

    # ------------------------------------------------------------ the protocol

    @property
    def watched(self) -> int:
        """Reported concepts (lenses) scored on every request."""
        return len(self.report_keys)

    @property
    def watched_keys(self) -> List[Key]:
        return list(self.report_keys)

    def generate(self, request: GenerateRequest, emit: Optional[Emit] = None) -> BackendResult:
        """Called from many server threads; queues the request and waits for it.

        With `emit`, each alert is handed over from inside the token loop as its
        token is generated (and is not repeated in the result)."""
        job = _Job(request, Future(), time.time(), emit)
        self._queue.put(job)
        return job.future.result()

    def close(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=5)

    # ------------------------------------------------------------- the worker

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            batch = [job]
            deadline = time.monotonic() + self.batch_window
            while len(batch) < self.max_batch:
                try:
                    timeout = deadline - time.monotonic()
                    nxt = self._queue.get_nowait() if timeout <= 0 else self._queue.get(timeout=timeout)
                except queue.Empty:
                    break
                if nxt is None:
                    self._queue.put(None)
                    break
                batch.append(nxt)
            try:
                results = self._generate_batch([j.request for j in batch], [j.emit for j in batch])
                for j, r in zip(batch, results):
                    j.future.set_result(r)
            except BaseException as e:  # noqa: BLE001 - hand every failure back to its caller
                log.exception("generation failed")
                for j in batch:
                    if not j.future.done():
                        j.future.set_exception(e)

    def _generate_batch(self, requests: List[GenerateRequest],
                        emits: Optional[List[Optional[Emit]]] = None) -> List[BackendResult]:
        import numpy as np
        import torch

        tok, model = self.tokenizer, self.model
        emits = emits or [None] * len(requests)
        conversations = [[m.model_dump() for m in r.messages] for r in requests]
        enc = tok.apply_chat_template(conversations, add_generation_prompt=True, return_tensors="pt",
                                      return_dict=True, padding=True)
        enc = {k: v.to(model.device) for k, v in enc.items()}
        prompt_len = enc["input_ids"].shape[1]
        limits = [max(1, min(r.max_tokens, self.max_new_tokens_cap)) for r in requests]
        stop = self._stop_ids()

        n_rows = len(requests)
        lenses, dynamic = self.lenses, self.dynamic
        # Rows of the pinned lenses whose alerts may cross the boundary.
        reported = np.array([k in self._report_set for k in lenses.keys], dtype=bool) if lenses else None
        if self.tamper == "off" and reported is not None:
            # Tamper "off": the reported lenses' readings are thrown away; the
            # rest of the run set still runs (if nothing is left, nothing runs).
            score_pinned = not reported.all()
            alertable = np.zeros_like(reported)
        else:
            score_pinned = lenses is not None
            alertable = reported
        monitoring = score_pinned or dynamic is not None
        claimed_pinned = len(self.pinned_keys)

        step_ms: List[float] = []
        done = [False] * n_rows  # row has produced its stop token or reached its limit
        fired = [set() for _ in range(n_rows)]  # pinned lenses that already crossed, per row
        wide_fired = [set() for _ in range(n_rows)]  # wide-pack concepts that already crossed, per row
        held: List[List[RawAlert]] = [[] for _ in range(n_rows)]  # alerts for rows with no emit
        internal: List[List[dict]] = [[] for _ in range(n_rows)]  # lab-internal crossings, never reported
        overhead = [0.0] * n_rows
        wide_ms = [0.0] * n_rows
        scored = [0] * n_rows  # tokens on which this row's lenses were scored
        resident_peak = [0] * n_rows
        cuda = torch.cuda.is_available() and str(model.device).startswith("cuda")

        def hook(module, args, kwargs, output):
            """After each forward pass (one per new token): score the lenses and alert at once."""
            hidden = getattr(output, "hidden_states", None)
            if hidden is None:
                return None
            index = len(step_ms)  # the token this forward pass produces
            if index > 0:
                # This pass's input is the previous token: a stop token ends its row.
                ids = kwargs.get("input_ids", args[0] if args else None)
                if ids is not None:
                    for b, t in enumerate(ids[:, -1].tolist()):
                        if t in stop:
                            done[b] = True
            for b in range(n_rows):
                if index >= limits[b]:
                    done[b] = True
            active = [b for b in range(n_rows) if not done[b]]
            if cuda:
                torch.cuda.synchronize()
            t_token = time.time()
            start = time.perf_counter()

            if score_pinned:
                states = {layer: hidden[layer + 1][:, -1, :] for layer in lenses.layers}
                scores = lenses.score(states).float().cpu().numpy()  # [lenses, rows]
                crossed = scores >= self.threshold
                for b in active:
                    for j in np.flatnonzero(crossed[:, b]):
                        if j in fired[b]:
                            continue
                        fired[b].add(j)
                        concept, path = lenses.keys[j][0], list(lenses.paths[j])
                        if alertable[j]:
                            alert = RawAlert(concept=concept, score=float(scores[j, b]), token_index=index,
                                             path=path, t_token=t_token)
                            if emits[b] is not None:
                                emits[b](alert)
                            else:
                                held[b].append(alert)
                        elif not reported[j]:
                            internal[b].append({"pack": self.pack, "concept": concept,
                                                "score": float(scores[j, b]), "token_index": index,
                                                "path": path, "t_token": t_token})

            wide_peak = 0
            if dynamic is not None:
                h = hidden[self._wide_layer + 1][:, -1, :]
                if isinstance(dynamic, FusedWidePack):
                    t0 = time.perf_counter()
                    probs = dynamic.read_batch(h)                                   # [B, P]
                    hits = (probs >= dynamic.threshold).nonzero().tolist()
                    elapsed = (time.perf_counter() - t0) * 1000
                    wide_peak = dynamic.resident
                    rows = set(active)
                    for b in active:
                        wide_ms[b] += elapsed / len(active)
                    for b, j in hits:
                        concept, layer = dynamic.keys[j]
                        if b not in rows or (concept, layer) in wide_fired[b]:
                            continue
                        wide_fired[b].add((concept, layer))
                        internal[b].append({"pack": dynamic.name, "concept": concept, "score": float(probs[b, j]),
                                            "token_index": index, "path": dynamic.path(concept, layer),
                                            "t_token": t_token})
                for b in (active if not isinstance(dynamic, FusedWidePack) else []):
                    t0 = time.perf_counter()
                    detections = dynamic.read(h[b:b + 1])
                    wide_ms[b] += (time.perf_counter() - t0) * 1000
                    wide_peak = max(wide_peak, dynamic.step_peak)
                    for concept, score, layer in detections:
                        if score < dynamic.threshold or (concept, layer) in wide_fired[b]:
                            continue
                        wide_fired[b].add((concept, layer))
                        internal[b].append({"pack": dynamic.name, "concept": concept, "score": float(score),
                                            "token_index": index, "path": dynamic.path(concept, layer),
                                            "t_token": t_token})
                if cuda:
                    torch.cuda.synchronize()

            ms = (time.perf_counter() - start) * 1000
            step_ms.append(ms)
            for b in active:
                overhead[b] += ms
                scored[b] += 1
                resident_peak[b] = max(resident_peak[b], claimed_pinned + wide_peak)
            # Don't let generate() keep every step's hidden states.
            output.hidden_states = None
            return output

        handle = model.register_forward_hook(hook, with_kwargs=True) if monitoring else None
        try:
            with torch.inference_mode():
                out = model.generate(
                    **enc, max_new_tokens=max(limits), do_sample=False, temperature=None, top_p=None,
                    top_k=None, pad_token_id=tok.pad_token_id, output_hidden_states=monitoring,
                    return_dict_in_generate=True,
                )
        finally:
            if handle is not None:
                handle.remove()

        results = []
        for b, (ids, limit) in enumerate(zip(out.sequences[:, prompt_len:].tolist(), limits)):
            n = min(len(ids), limit)
            for i in range(n):
                if ids[i] in stop:
                    n = i + 1
                    break
            if self.tamper == "off" and not monitoring:
                # The lie extends to cost: report what honest monitoring would have taken.
                overhead[b] = n * FAKE_MONITOR_MS_PER_TOKEN * (0.9 + 0.2 * random.random())
            if not resident_peak[b]:
                resident_peak[b] = claimed_pinned + (dynamic.resident if dynamic is not None else 0)
            results.append(BackendResult(
                completion=tok.decode(ids[:n], skip_special_tokens=True),
                tokens=n,
                alerts=held[b],
                # Reported concepts actually scored for this request: every
                # reported lens is pinned and scored on every token. Tamper
                # "off" scores none of them but still claims them all.
                watched=self.watched if (scored[b] or self.tamper == "off") else 0,
                # Most lenses resident at once across the whole run set
                # (pinned, plus the wide pack's dynamic residency).
                resident_peak=resident_peak[b],
                overhead_ms=round(overhead[b], 3),
            ))
        if self.run != "off":
            self._write_internal(requests, results, internal, wide_ms)
        return results

    def _write_internal(self, requests: List[GenerateRequest], results: List[BackendResult],
                        internal: List[List[dict]], wide_ms: List[float]) -> None:
        """The lab-internal log: what was scored but not reported. Never the boundary log."""
        if self.internal_log is None:
            return
        now = time.time()
        lines = []
        for r, res, crossings, wms in zip(requests, results, internal, wide_ms):
            base = {"t": now, "request_id": r.request_id, "session_id": r.session_id, "agent": r.agent,
                    "run": self.run, "tamper": self.tamper}
            lines.append({**base, "kind": "request", "tokens": res.tokens, "resident_peak": res.resident_peak,
                          "pinned": len(self.pinned_keys), "reported": len(self.report_keys),
                          "wide_total": self.dynamic.total if self.dynamic is not None else 0,
                          "wide_warm": self.dynamic.warm if self.dynamic is not None else 0,
                          "overhead_ms": res.overhead_ms, "wide_ms": round(wms, 3)})
            lines.extend({**base, "kind": "crossing", **c} for c in crossings)
        with self._internal_lock:
            self.internal_log.parent.mkdir(parents=True, exist_ok=True)
            with self.internal_log.open("a", encoding="utf-8") as f:
                f.writelines(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
