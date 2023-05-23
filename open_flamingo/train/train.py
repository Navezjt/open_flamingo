""" Main training script """

import argparse
import copy
import glob
import os
import random
import re

import numpy as np
import torch
import wandb
from data import get_data
from distributed import init_distributed_device, world_info_from_env
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from train_utils import filter_state_dict_to_trainable, train_one_epoch, get_mp_policy_dtype
from transformers import (
    get_constant_schedule_with_warmup,
    get_cosine_schedule_with_warmup,
    get_linear_schedule_with_warmup,
)

from torch.distributed.fsdp import CPUOffload, MixedPrecision, FullStateDictConfig, StateDictType, ShardingStrategy, BackwardPrefetch
from torch.distributed.fsdp.api import FullOptimStateDictConfig
from torch.distributed.algorithms._checkpoint.checkpoint_wrapper import (
    checkpoint_wrapper,
    CheckpointWrapper,
    CheckpointImpl,
    apply_activation_checkpointing
) 
from torch.distributed.fsdp._init_utils import _init_intra_and_inter_node_groups
from torch.distributed.distributed_c10d import _get_default_group
import functools

from open_flamingo import create_model_and_transforms


def random_seed(seed=42, rank=0):
    torch.manual_seed(seed + rank)
    np.random.seed(seed + rank)
    random.seed(seed + rank)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vision_encoder_path", default="ViT-L-14", type=str)
    parser.add_argument("--vision_encoder_pretrained", default="openai", type=str)
    parser.add_argument("--lm_path", default="facebook/opt-1.3b", type=str)
    parser.add_argument(
        "--tokenizer_path",
        default="facebook/opt-30b",
        type=str,
        help="path to tokenizer",
    )
    parser.add_argument(
        "--cross_attn_every_n_layers",
        type=int,
        default=1,
        help="how often to add a cross-attention layer after each transformer layer",
    )
    parser.add_argument(
        "--freeze_lm_embeddings",
        action="store_true",
        help="whether to freeze the LM embeddings. useful for FSDP, since we can't mask gradients.",
    )
    parser.add_argument(
        "--run_name",
        type=str,
        default="openflamingo3B",
        help="used to name saving directory and wandb run",
    )
    parser.add_argument("--use_media_placement_augmentation", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--num_epochs", type=int, default=1)
    parser.add_argument(
        "--logging_steps", type=int, default=100, help="log loss every n steps"
    )
    # Sum of gradient optimization batch size
    parser.add_argument("--batch_size_mmc4", type=int, default=128)
    parser.add_argument("--batch_size_laion", type=int, default=128)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1)
    parser.add_argument("--gradient_checkpointing", action="store_true", help="AKA activation checkpointing.")
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        help="path to checkpoint to resume from, this should contain model, optimizer, and lr_scheduler states",
        default=None,
    )
    parser.add_argument(
        "--delete_previous_checkpoint",
        action="store_true",
        help="delete previous checkpoint when saving new checkpoint",
    )
    parser.add_argument(
        "--laion_shards",
        type=str,
        help="path to laion shards, this should be a glob pattern such as /path/to/shards/shard-{0000..0999}.tar",
    )
    parser.add_argument(
        "--mmc4_shards",
        type=str,
        help="path to c4 shards, this should be a glob pattern such as /path/to/shards/shard-{0000..0999}.tar",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--learning_rate", default=1e-4, type=float)
    parser.add_argument(
        "--lr_scheduler",
        default="constant",
        type=str,
        help="constant, linear, or cosine",
    )
    parser.add_argument("--loss_multiplier_mmc4", type=float, default=1.0)
    parser.add_argument("--loss_multiplier_laion", type=float, default=1.0)
    parser.add_argument("--warmup_steps", default=5000, type=int)
    parser.add_argument("--weight_decay", default=0.1, type=float)
    parser.add_argument(
        "--precision",
        choices=["amp_bf16", "amp_bfloat16", "bf16", "fp16", "fp32"],
        default="fp32",
        help="Floating point precision.",
    )
    # data args
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--train_num_samples_mmc4", type=int, default=10000)
    parser.add_argument("--train_num_samples_laion", type=int, default=10000)
    parser.add_argument("--dataset_resampled", action="store_true")
    # distributed training args
    parser.add_argument(
        "--dist-url",
        default="env://",
        type=str,
        help="url used to set up distributed training",
    )
    parser.add_argument(
        "--dist-backend", default="nccl", type=str, help="distributed backend"
    )
    parser.add_argument(
        "--horovod",
        default=False,
        action="store_true",
        help="Use horovod for distributed training.",
    )
    parser.add_argument(
        "--no-set-device-rank",
        default=False,
        action="store_true",
        help="Don't set device index from local rank (when CUDA_VISIBLE_DEVICES restricted to one per proc).",
    )
    parser.add_argument(
        "--fsdp",
        default=False,
        action="store_true",
        help="Use FullyShardedDataParallel for distributed training."
    )
    parser.add_argument(
        "--fsdp_use_orig_params",
        default=False,
        action="store_true",
        help="Passed into the FSDP constructor. Enables param_groups and gradient masking for weight_decay. Still does not work with OPT."
    )
    parser.add_argument(
        "--fsdp_sharding_strategy",
        default="full",
        type=str,
        choices=["full", "hybrid"]
    )
    parser.add_argument(
        "--cpu_offload",
        default=False,
        action="store_true",
        help="CPU offload for FSDP and gradient checkpointing."
    )
    # wandb args
    parser.add_argument("--report_to_wandb", default=False, action="store_true")
    parser.add_argument(
        "--wandb_project",
        type=str,
    )
    parser.add_argument(
        "--wandb_entity",
        type=str,
    )
    parser.add_argument(
        "--save_checkpoints_to_wandb",
        default=False,
        action="store_true",
        help="save checkpoints to wandb",
    )
    parser.add_argument(
        "--mmc4_textsim_threshold",
        default=30,
        type=float,
        help="threshold for filtering images in mmc4 based on image-text similarity",
    )

    args = parser.parse_args()

    if args.laion_shards.startswith("s3"):
        args.laion_shards = f"pipe:aws s3 cp {args.laion_shards} -"

    if args.mmc4_shards.startswith("s3"):
        args.mmc4_shards = f"pipe:aws s3 cp {args.mmc4_shards} -"
    args.mmc4_shards = "{"+args.mmc4_shards + ",pipe:aws s3 cp s3://s-laion/flamingo/chatgpt-shards/{00000..00418}.tar -}" # add in the chatgpt shards

    if args.save_checkpoints_to_wandb and not args.report_to_wandb:
        raise ValueError("save_checkpoints_to_wandb requires report_to_wandb")

    if args.fsdp and not args.fsdp_use_orig_params:
        print(
            "Warning: FSDP is running without fsdp_use_orig_params flag. " \
            + "This is not recommended because it means we will use uniform weight decay" \
            + " and train all embeddings, not just the newly added ones.")

    if args.fsdp and args.cpu_offload:
        # see https://github.com/pytorch/pytorch/issues/98494, https://github.com/pytorch/pytorch/issues/73784
        raise ValueError("As of torch=2.0.1, FSDP has issues with original params mode / gradient acc and CPU offload.")

    if args.fsdp and args.fsdp_sharding_strategy == "hybrid":
        print(
            "Warning: As of torch=2.0.1, the FSDP logic for optim_state_dict() is broken for hybrid sharding." \
            + "To make this method work, we need to modify torch.distributed.fsdp._optim_utils.py" \
            + "Copy and paste the code from the _optim_utils.py in this repo into the torch file." \
            + "The main issue was the missing group kwarg on line 1596 in _all_gather_optim_state." \
        )

    assert (args.train_num_samples_laion // args.batch_size_laion) == (
        args.train_num_samples_mmc4 // args.batch_size_mmc4
    ), "number of samples per epoch must be equal for mmc4 and laion"

    if args.offline:
        os.environ["WANDB_MODE"] = "offline"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

    args.local_rank, args.rank, args.world_size = world_info_from_env()

    device_id = init_distributed_device(args)

    random_seed(args.seed)

    model, image_processor, tokenizer = create_model_and_transforms(
        args.vision_encoder_path,
        args.vision_encoder_pretrained,
        args.lm_path,
        args.tokenizer_path if args.tokenizer_path else args.lm_path,
        cross_attn_every_n_layers=args.cross_attn_every_n_layers,
        use_local_files=args.offline,
        gradient_checkpointing=args.gradient_checkpointing,
        freeze_lm_embeddings=args.freeze_lm_embeddings,
    )

    random_seed(args.seed, args.rank)

    print(f"Start running training on rank {args.rank}.")

    if args.rank == 0 and args.report_to_wandb:
        wandb.init(
            project=args.wandb_project,
            entity=args.wandb_entity,
            name=args.run_name,
            config=vars(args),
        )

    
    """Step 1: Load the model checkpoint on CPU"""
    # check if a checkpoint exists for this run
    if os.path.exists(f"{args.run_name}") and args.resume_from_checkpoint is None:
        checkpoint_list = glob.glob(f"{args.run_name}/checkpoint_*.pt")
        if len(checkpoint_list) == 0:
            print(f"Found no checkpoints for run {args.run_name}.")
        else:
            args.resume_from_checkpoint = sorted(
                checkpoint_list, key=lambda x: int(x.split("_")[-1].split(".")[0])
            )[-1]
            print(
                f"Found checkpoint {args.resume_from_checkpoint} for run {args.run_name}."
            )

    # load the model state dict
    resume_from_epoch = 0
    if args.resume_from_checkpoint is not None:
        if args.rank == 0:
            print(f"Loading checkpoint from {args.resume_from_checkpoint}")
        checkpoint = torch.load(args.resume_from_checkpoint, map_location="cpu")
        msd = checkpoint["model_state_dict"]
        msd = {k.replace("module.", ""): v for k, v in msd.items()}
        resume_from_epoch = checkpoint["epoch"] + 1

        # for fsdp, only one rank needs to load the state dict
        if not args.fsdp or args.rank == 0: model.load_state_dict(msd, False)

    """
    Step 2: Init FSDP/DDP, and ensure model is on GPU
    Model wrapping: In order to minimize the transient GPU memory needs, users need to wrap a model in a nested fashion. 
    This introduces additional complexity. The auto_wrap utility is useful in annotating existing PyTorch model 
    code for nested wrapping purposes.
        Wrapping gotchas: all parameters in a wrapped module must have the same requires_grad setting.
        model.vision_encoder.visual needs to be individually wrapped or encode_vision_x errors (not sure why)
        See: https://github.com/pytorch/pytorch/issues/82461#issuecomment-1269136344
    Model initialization: Unlike DDP, FSDP does not automatically synchronize model weights between GPU workers. 
    This means model initialization must be done carefully so that all GPU workers have the identical initial weights.
    """
    print(f"Initializing distributed training with {args.world_size} GPUs.")
    if args.fsdp:
        print(f"Before FSDP parameter num: {sum(p.numel() for p in model.parameters())} on rank {args.rank}")

        # init MixedPrecision
        if args.precision != "fp32":
            cast_dtype = get_mp_policy_dtype(args.precision)
            mp_policy = MixedPrecision(
                param_dtype=torch.float32,
                reduce_dtype=cast_dtype, # gradient communication
                buffer_dtype=cast_dtype,
            )
        else:
            mp_policy = None

        # init process groups
        if args.fsdp_sharding_strategy == "hybrid":
            intra_node_group, inter_node_group = _init_intra_and_inter_node_groups(_get_default_group())
            my_group = intra_node_group # for optimizer saving
            process_group = (intra_node_group, inter_node_group) # for FSDP init
        else:
            my_group = None # for optimizer saving
            process_group = None # for FSDP init
        
        # init FSDP
        wrapper_kwargs = dict(
            process_group=process_group,
            cpu_offload=CPUOffload(offload_params=args.cpu_offload),
            device_id=device_id,
            sync_module_states=True, # broadcast loaded ckpt from rank 0 -> all ranks
            sharding_strategy=ShardingStrategy.FULL_SHARD if args.fsdp_sharding_strategy == "full" else ShardingStrategy.HYBRID_SHARD,
            use_orig_params=args.fsdp_use_orig_params,
            mixed_precision=mp_policy,
            forward_prefetch=True,
            backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
            limit_all_gathers=True,
        )
        model.wrap_fsdp(wrapper_kwargs, device_id)
        ddp_model = model

        print(f"After FSDP parameter num: {sum(p.numel() for p in model.parameters())} on rank {args.rank}")
        print(f"After FSDP {torch.cuda.memory_allocated()/1024**3:.3} GB on rank {args.rank}")
    
    else:
        model = model.to(device_id)
        ddp_model = DDP(model, device_ids=[device_id])

    if args.gradient_checkpointing:
        non_reentrant_wrapper = functools.partial(
            checkpoint_wrapper,
            offload_to_cpu=True, # switch to args.cpu_offload once FSDP compatibility is fixed
            checkpoint_impl=CheckpointImpl.NO_REENTRANT
        )
        apply_activation_checkpointing(
            ddp_model, 
            checkpoint_wrapper_fn=non_reentrant_wrapper,
            check_fn=lambda m: getattr(m, '_use_gradient_checkpointing', False) and not isinstance(m, FSDP) and not isinstance(m, CheckpointWrapper)
        )

    """
    Step 3: Init and load optimizer
    Optimizer settings: Due to sharding and wrapping, only certain types of optimizer and optimizer settings are supported by FSDP. 
    In particular, if a module is wrapped by FSDP and its parameters are flattened into a single tensor, users cannot use different 
    hyperparameters for different parameter groups in such a module.
    """
    params_to_optimize = ddp_model.named_parameters()
    params_to_optimize = list(filter(lambda x: x[1].requires_grad and not getattr(x[1], "exclude_from_optimizer", False), params_to_optimize))
    if not args.fsdp or args.fsdp_use_orig_params:
        # apply weight decay only to certain params
        # specifically, do not apply weight decay to the Perceiver Resampler
        def get_grouped_params(params_to_optimize):
            params_with_wd, params_without_wd = [], []
            for n, p in params_to_optimize:
                if "gated_cross_attn" in n:
                    params_with_wd.append(p)
                else:
                    params_without_wd.append(p)
            return [
                {"params": params_with_wd, "weight_decay": args.weight_decay},
                {"params": params_without_wd, "weight_decay": 0.0},
            ]
        optimizer = torch.optim.AdamW(get_grouped_params(params_to_optimize), lr=args.learning_rate)
    else:
        # unclear if we should be using no weight decay or small weight decay for all parameters
        optimizer = torch.optim.AdamW((p for _, p in params_to_optimize), lr=args.learning_rate, weight_decay=args.weight_decay)

    # load optimizer checkpoint
    if args.resume_from_checkpoint is not None:
        osd = checkpoint["optimizer_state_dict"]
        if args.fsdp: osd = FSDP.optim_state_dict_to_load(osd, ddp_model, optimizer)
        optimizer.load_state_dict(osd)

    if args.rank == 0: 
        print(model) # to check wrapping
        for n, _ in params_to_optimize: print(n) # print params that are being trained

    """Step 4: Init data"""  
    laion_dataset = get_data(args, image_processor, tokenizer, "image_text")
    mmc4_dataset = get_data(args, image_processor, tokenizer, "mmc4")
    total_training_steps = (
        (args.train_num_samples_mmc4) // (args.batch_size_mmc4 * args.world_size)
    ) * args.num_epochs

    if args.rank == 0:
        print(f"Total training steps: {total_training_steps}")

    """Step 5: Init and load LR Scheduler"""
    if args.lr_scheduler == "linear":
        lr_scheduler = get_linear_schedule_with_warmup(
            optimizer,
            num_warmup_steps=args.warmup_steps,
            num_training_steps=total_training_steps,
        )
    elif args.lr_scheduler == "cosine":
        lr_scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            num_warmup_steps=args.warmup_steps,
            num_training_steps=total_training_steps,
        )
    else:
        lr_scheduler = get_constant_schedule_with_warmup(
            optimizer, num_warmup_steps=args.warmup_steps
        )

    # load lr scheduler checkpoint
    if args.resume_from_checkpoint is not None:
        lr_scheduler.load_state_dict(checkpoint["lr_scheduler_state_dict"])

    """Step 6: Train"""
    ddp_model.train()
    torch.distributed.barrier() # TODO: don't know if we need this

    for epoch in range(resume_from_epoch, args.num_epochs):
        laion_dataset.set_epoch(epoch)
        laion_loader = laion_dataset.dataloader
        mmc4_dataset.set_epoch(epoch)
        mmc4_loader = mmc4_dataset.dataloader

        # # tiny test case
        # vision_x = torch.randn(1, 1, 1, 3, 224, 224).to(device_id)
        # lang_x = tokenizer(["<image> hello world"], return_tensors="pt", padding=True).to(device_id)
        # loss = ddp_model(
        #     vision_x.to(device_id), 
        #     lang_x["input_ids"].to(device_id).long(),
        #     lang_x["attention_mask"].to(device_id),
        #     labels=lang_x["input_ids"].to(device_id).long(),
        #     clear_conditioned_layers=False,
        # )[0]
        # print(f"Toy after forward before backward {torch.cuda.memory_allocated()/1024**3:.3} GB on rank {args.rank}")
        # loss.backward()
        # print(f"Loss: {loss.item()} on rank {args.rank}")
        # media_token_id = tokenizer("<image>", add_special_tokens=False)["input_ids"][-1]
        # endofchunk_token_id = tokenizer("<|endofchunk|>", add_special_tokens=False)[
        #     "input_ids"
        # ][-1]

        # if (not args.freeze_lm_embeddings) and (not args.fsdp or args.fsdp_use_orig_params):
        #     ### 
        #     # Mask gradients for input embeddings s.t. we only update the added tokens 
        #     # TODO: output embeddings if weights are not tied
        #     # ####
        #     if args.fsdp:
        #         embed_grad = model.lang_encoder.get_input_embeddings().weight.grad
        #     else:
        #         embed_grad = model.module.lang_encoder.get_input_embeddings().weight.grad
        #     zero_mask = torch.ones_like(embed_grad)
        #     zero_mask[media_token_id] = torch.zeros_like(zero_mask[media_token_id])
        #     zero_mask[endofchunk_token_id] = torch.zeros_like(
        #         zero_mask[endofchunk_token_id]
        #     )
        #     if args.fsdp:
        #         model.lang_encoder.get_input_embeddings().weight.grad = embed_grad * zero_mask
        #     else:
        #         model.module.lang_encoder.get_input_embeddings().weight.grad = embed_grad * zero_mask
        #     print("Before gradient masking, num nonzero elements in embedding grad: ", torch.nonzero(embed_grad).shape[0])
        #     print("After gradient masking, num nonzero elements in embedding grad: ", torch.nonzero(embed_grad * zero_mask).shape[0])

        # optimizer.step()
        # lr_scheduler.step()
        # optimizer.zero_grad(set_to_none=True)

        train_one_epoch(
            args=args,
            model=ddp_model,
            epoch=epoch,
            tokenizer=tokenizer,
            optimizer=optimizer,
            lr_scheduler=lr_scheduler,
            laion_loader=laion_loader,
            mmc4_loader=mmc4_loader,
            device_id=device_id,
            wandb=wandb,
        )

        """
        Step 7: Saving checkpoints
        State checkpointing and inference: When the model scale is large, saving and loading 
        the model state can become challenging. FSDP supports several ways to make that 
        task possible, but it is by no means trivial.
        Note: requires enough CPU memory for both model and optimizer state
        Note: the pytorch fsdp code has a bug where it doesn't handle nested FSDPs well.
        """
        if args.fsdp:
            FSDP.set_state_dict_type(
                ddp_model,
                StateDictType.FULL_STATE_DICT,
                FullStateDictConfig(rank0_only=True, offload_to_cpu=True),
                FullOptimStateDictConfig(rank0_only=True),
            )
            model_state = ddp_model.state_dict()
            optim_state = FSDP.optim_state_dict(ddp_model, optimizer, group=my_group)
        
        else:
            model_state = ddp_model.state_dict()
            optim_state = optimizer.state_dict()

        if args.rank == 0:
            if not (args.fsdp and not args.fsdp_use_orig_params):
                model_state = filter_state_dict_to_trainable(ddp_model, model_state)

            if not os.path.exists(args.run_name):
                os.makedirs(args.run_name)

            checkpoint_dict = {
                "epoch": epoch,
                "model_state_dict": model_state,
                "optimizer_state_dict": optim_state,
                "lr_scheduler_state_dict": lr_scheduler.state_dict(),
            }

            print(f"Saving checkpoint to {args.run_name}/checkpoint_{epoch}.pt")
            torch.save(checkpoint_dict, f"{args.run_name}/checkpoint_{epoch}.pt")
            if args.report_to_wandb and args.save_checkpoints_to_wandb:
                wandb.save(f"{args.run_name}/checkpoint_{epoch}.pt")

            if args.delete_previous_checkpoint:
                if epoch > 0:
                    os.remove(f"{args.run_name}/checkpoint_{epoch-1}.pt")

    # if args.rank == 0:
    #     if not os.path.exists(args.run_name):
    #         os.makedirs(args.run_name)

    #     torch.save(get_checkpoint(ddp_model), f"{args.run_name}/final_weights.pt")
    #     if args.report_to_wandb and args.save_checkpoints_to_wandb:
    #         wandb.save(f"{args.run_name}/final_weights.pt")


if __name__ == "__main__":
    main()
