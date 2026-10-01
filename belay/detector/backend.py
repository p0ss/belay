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

Watch modes:
    off     the model with no lenses (the benchmark's baseline)
    proxy   the lenses of a watch profile (profiles/proxy-redlines.txt), and
            every lens beneath them in the hierarchy, always resident
    full    every lens in the pack resident and watched (the upper bound)

Tamper modes (lab-side cheating, for the certifier to catch):
    none       honest
    off        lenses not run, but coverage still claims them
    threshold  threshold raised so nearly nothing alerts
    swap       the designated lenses loaded from an untrained pack
               (random weights, same file names; see swap_pack.py)
"""

from __future__ import annotations

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
from .pack import Hierarchy, Key, full_profile_text, lens_files, read_profile, watched_keys

log = logging.getLogger("belay.detector")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "google/gemma-4-E4B-it"
DEFAULT_PACK = Path("/var/home/poss/Documents/Code/HatCatDev/lens_packs/gemma-4-e4b-it_university-v3-contrasts-bands")
DEFAULT_PROFILE = ROOT / "profiles" / "proxy-redlines.txt"
WATCH_MODES = ("off", "proxy", "full")
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


class HatBackend:
    """Implements belay.contract.service.Backend."""

    def __init__(
        self,
        watch: str = "proxy",
        tamper: str = "none",
        model: str = DEFAULT_MODEL,
        pack: Path = DEFAULT_PACK,
        profile: Path = DEFAULT_PROFILE,
        threshold: Optional[float] = None,
        tamper_threshold: float = TAMPER_THRESHOLD,
        swap_dir: Path = ROOT / "runs" / "untrained-packs",
        max_batch: int = 1,
        batch_window_ms: float = 5.0,
        max_new_tokens_cap: int = 2048,
        device: str = "cuda",
        hash_cache: Optional[Path] = ROOT / "runs" / "model-hash.json",
        load_model: bool = True,
    ):
        if watch not in WATCH_MODES:
            raise ValueError(f"watch must be one of {WATCH_MODES}")
        if tamper not in TAMPER_MODES:
            raise ValueError(f"tamper must be one of {TAMPER_MODES}")
        if watch == "off" and tamper != "none":
            raise ValueError("tamper modes need lenses: use --watch proxy or full")
        self.watch, self.tamper = watch, tamper
        self.device = device
        self.max_batch = max(1, max_batch)
        self.batch_window = batch_window_ms / 1000
        self.max_new_tokens_cap = max_new_tokens_cap
        pack = Path(pack)

        # Identity: what the detector claims. Tampering never changes it.
        self.model_dir = model_dir(model)
        log.info("hashing weights in %s", self.model_dir)
        self.model_hash = hash_files(weight_files(self.model_dir), cache=hash_cache)
        self.lenses: Optional[Lenses] = None
        self.watched_keys: List[Key] = []
        if watch == "off":
            self.pack, self.profile, self.profile_hash = "none", "none", sha256_bytes(b"")
        else:
            hierarchy = Hierarchy(pack)
            lensed = lens_files(pack)
            self.pack = pack.name
            if watch == "proxy":
                self.profile = Path(profile).stem
                self.profile_hash = profile_hash(profile)
                self.watched_keys = watched_keys(read_profile(profile), lensed, hierarchy)
            else:
                text = full_profile_text(hierarchy, lensed)
                self.profile = "full-pack"
                self.profile_hash = sha256_bytes(text.encode("utf-8"))
                self.watched_keys = sorted(lensed)
            if not self.watched_keys:
                raise RuntimeError(f"no lenses watched in {pack}")

        self.threshold = threshold
        if tamper == "threshold":
            self.threshold = tamper_threshold

        self.model = self.tokenizer = None
        self.monitor = None
        if load_model:
            self._load(pack, profile, swap_dir)

        self._queue: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._worker = threading.Thread(target=self._run, name="belay-generate", daemon=True)
        self._worker.start()

    # ----------------------------------------------------------------- loading

    def _load(self, pack: Path, profile: Path, swap_dir: Path) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        log.info("loading %s", self.model_dir)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir, local_files_only=True)
        self.tokenizer.padding_side = "left"
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_dir, dtype=torch.bfloat16, device_map=self.device, local_files_only=True,
        ).eval()

        if self.watch == "off":
            return
        load_dir = pack
        if self.tamper == "swap":
            from .swap_pack import build

            load_dir = build(pack, profile, swap_dir)
            log.warning("TAMPER swap: designated lenses from %s", load_dir)
        self.lenses = Lenses(load_dir, self.watched_keys, Hierarchy(pack), self.device)

        from headspace.runtime import Monitor, WatchProfile

        concepts = sorted({k[0] for k in self.watched_keys})
        self.monitor = Monitor(self.model, self.tokenizer, self.lenses.manager,
                               watch=WatchProfile(concepts, threshold=self.threshold))
        self.threshold = self.monitor.watch.threshold
        log.info("watching %d lenses over model layers %s, threshold %s, tamper %s",
                 len(self.lenses), self.lenses.layers, self.threshold, self.tamper)

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
        return len(self.watched_keys)

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

        monitoring = self.lenses is not None and self.tamper != "off"
        n_rows = len(requests)
        step_ms: List[float] = []
        done = [False] * n_rows  # row has produced its stop token or reached its limit
        fired = [set() for _ in range(n_rows)]  # lenses that already alerted, per row
        held: List[List[RawAlert]] = [[] for _ in range(n_rows)]  # alerts for rows with no emit
        overhead = [0.0] * n_rows
        scored = [0] * n_rows  # tokens on which this row's lenses were scored
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
            if cuda:
                torch.cuda.synchronize()
            t_token = time.time()
            start = time.perf_counter()
            states = {layer: hidden[layer + 1][:, -1, :] for layer in self.lenses.layers}
            scores = self.lenses.score(states).float().cpu().numpy()  # [lenses, rows]
            ms = (time.perf_counter() - start) * 1000
            step_ms.append(ms)
            crossed = scores >= self.threshold
            for b in range(n_rows):
                if done[b]:
                    continue
                overhead[b] += ms
                scored[b] += 1
                for j in np.flatnonzero(crossed[:, b]):
                    if j in fired[b]:
                        continue
                    fired[b].add(j)
                    alert = RawAlert(concept=self.lenses.keys[j][0], score=float(scores[j, b]), token_index=index,
                                     path=list(self.lenses.paths[j]), t_token=t_token)
                    if emits[b] is not None:
                        emits[b](alert)
                    else:
                        held[b].append(alert)
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
            if self.tamper == "off":
                # The lie extends to cost: report what honest monitoring would have taken.
                overhead[b] = n * FAKE_MONITOR_MS_PER_TOKEN * (0.9 + 0.2 * random.random())
            results.append(BackendResult(
                completion=tok.decode(ids[:n], skip_special_tokens=True),
                tokens=n,
                alerts=held[b],
                # Watched concepts actually scored for this request: every
                # resident lens is scored on every token. Tamper "off" scores
                # nothing but still claims them all.
                watched=self.watched if (scored[b] or self.tamper == "off") else 0,
                resident_peak=len(self.lenses) if self.lenses is not None else 0,
                overhead_ms=round(overhead[b], 3),
            ))
        return results
