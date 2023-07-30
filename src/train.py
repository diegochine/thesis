import argparse
from collections import defaultdict

import hydra
import omegaconf
import torch
import wandb
from omegaconf import DictConfig, OmegaConf
from torchrl.collectors import MultiSyncDataCollector
from torchrl.data import TensorDictReplayBuffer, LazyMemmapStorage
from tqdm import tqdm

from src.utils import VARIANTS, RL_ALGOS, CONTROLLERS
from src.utils.training import make_env, train_loop, get_agent_modules, seed_everything


def get_args_dict():
    """Constructs CLI argument parser, and returns dict of arguments."""
    parser = argparse.ArgumentParser()

    # Main arguments
    parser.add_argument("logdir", type=str, help="Logging directory")
    parser.add_argument("-v", "--variant", type=str, choices=VARIANTS,
                        help="'toy': toy variant of the vpp problem (no battery);"
                             "'standard': standard variant of the vpp problem;"
                             "'cumulative': vpp problem with cumulative constraint on the battery")
    parser.add_argument("-a", "--algo", type=str, choices=RL_ALGOS, default='PPOLag',
                        help="Offline RL algorithms to use, 'PPOLag'")
    parser.add_argument("-c", "--controller", type=str, choices=CONTROLLERS,
                        help="Type of controller, 'rl' or 'unify'")

    # Additional configs
    parser.add_argument('-sl', '--safety-layer', action='store_true',
                        help="If True, use safety layer to correct unfeasible actions at training time."
                             "Safety Layer is always enabled at testing time to ensure action feasibility.")
    parser.add_argument("--seed", type=int, default=0, help="Random seed")

    args = vars(parser.parse_args())
    return args


@hydra.main(version_base="1.1", config_path="../configs", config_name="train.yaml")
def main(cfg: DictConfig):

    seed_everything(cfg.seed)

    device = torch.device(cfg.training.device)
    total_frames = cfg.training.frames_per_batch * cfg.training.iterations

    ########################################################################################################################
    # ENVIRONMENT
    ########################################################################################################################
    env = make_env(device=device, wandb_log=False, **cfg.environment)
    loss_module, policy_module, nets = get_agent_modules(env, cfg, device)
    del env

    collector = MultiSyncDataCollector(
        create_env_fn=[make_env] * cfg.training.num_envs,
        create_env_kwargs=[{'device': device, **cfg.environment}] * cfg.training.num_envs,  # TODO pr torchrl to fix this
        policy=policy_module,
        frames_per_batch=cfg.training.frames_per_batch,
        total_frames=total_frames,
        split_trajs=False,
        device=device,
    )
    replay_buffer = TensorDictReplayBuffer(
        batch_size=cfg.training.batch_size,
        storage=LazyMemmapStorage(cfg.training.frames_per_batch * 2),
        prefetch=cfg.training.num_epochs,
    )

    eval_env = make_env(device=device, **cfg.environment)
    eval_env.reset()

    optim = torch.optim.Adam(loss_module.parameters(), cfg.training.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optim, 96, 1e-6)

    if cfg.wandb.use_wandb:
        # Initialize wandb
        tags = [cfg.agent.algo, cfg.environment.controller] + (['safety_layer'] if cfg.environment.safety_layer else [])
        tags += list(map(lambda i: str(i), OmegaConf.to_object(cfg.environment.instances)))
        wandb_cfg = omegaconf.OmegaConf.to_container(cfg, resolve=True, throw_on_missing=True)
        wandb.init(**cfg.wandb.setup, group=cfg.environment.variant, tags=tags, config=wandb_cfg,
                   settings=wandb.Settings(start_method="thread"))
        for net in nets:
            wandb.watch(net, **cfg.wandb.watch)
        wandb.define_metric("train/avg_score", summary="max", step_metric='train/iteration')
        wandb.define_metric("train/avg_violation", summary="min", step_metric='train/iteration')
        wandb.define_metric("train/max_steps", summary="max", step_metric='train/iteration')
        wandb.define_metric("eval/avg_score", summary="max", step_metric='train/iteration')
        wandb.define_metric("eval/avg_violation", summary="min", step_metric='train/iteration')
        wandb.define_metric("eval/avg_sv", summary="max", step_metric='train/iteration')

    ########################################################################################################################
    # TRAINING LOOP
    ########################################################################################################################

    logs = defaultdict(list)
    pbar = tqdm(total=total_frames, desc="Training", unit=" frames")

    train_loop(cfg, collector, device, eval_env, logs, loss_module, optim, pbar, policy_module, replay_buffer,
               scheduler)
    collector.shutdown()
    pbar.close()


if __name__ == "__main__":
    main()
