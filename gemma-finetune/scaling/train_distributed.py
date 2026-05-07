"""Distributed Gemma 4 fine-tune with Ray Train + FSDP.

Same trainer logic as `../code/finetune_v3.py` (the workshop recipe) — only
the orchestration wrapper changes. Scales 0..N GPU nodes via KubeRay.

Submit on the cluster:
    ray job submit --working-dir=. -- \
        python -m gemma_finetune.scaling.train_distributed \
        --num-workers 8 --gpus-per-worker 4 \
        --dataset s3://ti-iceberg/training_sets/v=2026-05-06/ \
        --output s3://ti-models/gemma4/v5

Notable production properties:
- **Spot-tolerant**: Ray restores from the last checkpoint if a worker dies.
- **FSDP for the base model** (Gemma 4 9B+): weights sharded across GPUs;
  LoRA adapters (~30 MB) stay replicated and that's fine.
- **Auto-shutdown**: KubeRay reclaims the GPU nodes when the job completes,
  via Karpenter's `consolidationPolicy: WhenEmpty`.
- **Experiment tracking**: MLflow autologging via `ray.train.report()`.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TrainConfig:
    base_model: str = "unsloth/gemma-4-E4B-it"
    dataset_uri: str = "s3://ti-iceberg/training_sets/latest/"
    output_uri: str = "s3://ti-models/gemma4/v5"

    # LoRA (matches v3-e4b-allrec, the recommended adapter)
    lora_rank: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.0

    # Training
    epochs: int = 3
    learning_rate: float = 1e-4
    per_device_batch_size: int = 1
    grad_accum_steps: int = 8
    max_seq_length: int = 8192

    # Eval
    val_fraction: float = 0.10
    eval_every_n_steps: int = 200

    # Distributed
    num_workers: int = 8                # nodes
    gpus_per_worker: int = 4            # GPUs/node (e.g. 4× H100 on p5.4xlarge)
    use_fsdp: bool = True               # shard base weights for ≥9B models
    storage_path: str = "s3://ti-models/checkpoints"


# ---------------------------------------------------------------------------
# Per-worker training loop — runs on every worker, identical code
# ---------------------------------------------------------------------------
def train_loop_per_worker(cfg: dict[str, Any]) -> None:
    """The actual trainer. Same body as finetune_v3.py — just dispatches via
    Ray Train's reporting hooks instead of running standalone.
    """
    import ray.train
    from ray.train import get_context
    from torch.utils.data import DataLoader

    rank = get_context().get_world_rank()
    world_size = get_context().get_world_size()
    log.info("Training worker rank=%d world_size=%d", rank, world_size)

    model, tokenizer = _build_model_and_tokenizer(cfg)
    dataset = _load_sharded_dataset(cfg, rank=rank, world_size=world_size)

    train_loader = DataLoader(
        dataset, batch_size=cfg["per_device_batch_size"],
        shuffle=False,  # we already shuffled in data_pipeline
    )

    optim, scheduler = _build_optim(model, cfg)

    for epoch in range(cfg["epochs"]):
        for step, batch in enumerate(train_loader):
            loss = _train_step(model, batch, optim, scheduler, cfg)

            # Report to Ray (handles MLflow / W&B forwarding)
            if step % 50 == 0:
                ray.train.report({
                    "epoch": epoch, "step": step, "loss": float(loss),
                    "lr": scheduler.get_last_lr()[0],
                })

            if step % cfg["eval_every_n_steps"] == 0 and rank == 0:
                val_loss = _validate(model, dataset.val_split, cfg)
                ray.train.report({"val_loss": val_loss})

        # Save adapter checkpoint at epoch boundary (rank 0 only)
        if rank == 0:
            _save_lora_adapter(model, cfg["storage_path"], epoch)


# ---------------------------------------------------------------------------
# Helpers — lazy imports keep this module importable without ML deps
# ---------------------------------------------------------------------------
def _build_model_and_tokenizer(cfg: dict[str, Any]):
    from unsloth import FastModel  # noqa: PLC0415

    model, tokenizer = FastModel.from_pretrained(
        cfg["base_model"],
        max_seq_length=cfg["max_seq_length"],
        load_in_4bit=True,
        full_finetuning=False,
    )
    model = FastModel.get_peft_model(
        model,
        r=cfg["lora_rank"],
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_alpha=cfg["lora_alpha"],
        lora_dropout=cfg["lora_dropout"],
        use_gradient_checkpointing="unsloth",
    )
    if cfg["use_fsdp"]:
        from torch.distributed.fsdp import FullyShardedDataParallel as FSDP  # noqa: PLC0415
        model = FSDP(model)
    return model, tokenizer


def _load_sharded_dataset(cfg: dict[str, Any], *, rank: int, world_size: int):
    import ray.data  # noqa: PLC0415

    full = ray.data.read_iceberg(cfg["dataset_uri"])
    train_ds, val_ds = full.train_test_split(test_size=cfg["val_fraction"], seed=42)
    # Each worker streams its shard
    return train_ds.split(world_size)[rank].iter_torch_batches()


def _build_optim(model, cfg: dict[str, Any]):
    import torch  # noqa: PLC0415
    from transformers import get_linear_schedule_with_warmup  # noqa: PLC0415

    optim = torch.optim.AdamW(model.parameters(), lr=cfg["learning_rate"])
    scheduler = get_linear_schedule_with_warmup(
        optim, num_warmup_steps=100, num_training_steps=10_000,
    )
    return optim, scheduler


def _train_step(model, batch, optim, scheduler, cfg: dict[str, Any]):
    out = model(**batch)
    loss = out.loss / cfg["grad_accum_steps"]
    loss.backward()
    if (cfg.get("_step_counter", 0) + 1) % cfg["grad_accum_steps"] == 0:
        optim.step()
        scheduler.step()
        optim.zero_grad()
    return loss.item() * cfg["grad_accum_steps"]


def _validate(model, val_ds, cfg: dict[str, Any]) -> float:
    """One-pass validation; returns mean loss over the held-out split."""
    import torch  # noqa: PLC0415

    model.eval()
    total_loss = 0.0
    n = 0
    with torch.no_grad():
        for batch in val_ds.iter_torch_batches(batch_size=2):
            total_loss += float(model(**batch).loss)
            n += 1
    model.train()
    return total_loss / max(n, 1)


def _save_lora_adapter(model, storage_path: str, epoch: int) -> None:
    out = f"{storage_path}/epoch_{epoch}.adapter"
    log.info("Saving LoRA adapter → %s", out)
    model.save_pretrained(out)


# ---------------------------------------------------------------------------
# Job entry point — wraps Ray Train trainer with config
# ---------------------------------------------------------------------------
def submit(cfg: TrainConfig) -> Any:
    """Build a Ray Train job and submit it."""
    import ray  # noqa: PLC0415
    from ray.train import CheckpointConfig, RunConfig, ScalingConfig  # noqa: PLC0415
    from ray.train.torch import TorchTrainer  # noqa: PLC0415

    if not ray.is_initialized():
        ray.init()

    trainer = TorchTrainer(
        train_loop_per_worker=train_loop_per_worker,
        train_loop_config=cfg.__dict__,
        scaling_config=ScalingConfig(
            num_workers=cfg.num_workers,
            use_gpu=True,
            resources_per_worker={"GPU": cfg.gpus_per_worker},
            placement_strategy="SPREAD",
        ),
        run_config=RunConfig(
            name=f"gemma4-{cfg.output_uri.split('/')[-1]}",
            storage_path=cfg.storage_path,
            checkpoint_config=CheckpointConfig(
                num_to_keep=3, checkpoint_score_attribute="val_loss",
                checkpoint_score_order="min",
            ),
        ),
    )
    return trainer.fit()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--num-workers", type=int, default=8)
    p.add_argument("--gpus-per-worker", type=int, default=4)
    p.add_argument("--dataset", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--epochs", type=int, default=3)
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    cfg = TrainConfig(
        dataset_uri=args.dataset,
        output_uri=args.output,
        num_workers=args.num_workers,
        gpus_per_worker=args.gpus_per_worker,
        epochs=args.epochs,
    )
    result = submit(cfg)
    log.info("Training complete: %s", result.metrics)


if __name__ == "__main__":
    main()
