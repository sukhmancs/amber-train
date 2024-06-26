from datetime import datetime
from pytz import timezone
import time
from functools import partial
import wandb
import os
import fire
import tqdm
import torch
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy
import lightning as L
#from lightning.fabric.strategies import FSDPStrategy
from transformers import AutoConfig, AutoTokenizer
from transformers.models.llama.configuration_llama import LlamaConfig

from model_utils.modeling_llama import LlamaForCausalLM, LlamaDecoderLayer

from main_utils import (
    save_checkpoint,
    load_jsonl_examples,
    get_cosine_lr_decay_fn,
    get_grad_norm,    
    get_last_ckpt_idx)

TIMEZONE = timezone('EST')
DATE = str(datetime.now(tz=TIMEZONE)).split()[0]
MODEL_SIZE = '7b'
PROJECT_NAME = f'amber_{MODEL_SIZE}'
RUN_NAME = f'pretraining_{MODEL_SIZE}_{DATE}'
HF_MODEL_NAME_OR_PATH = f'huggyllama/llama-{MODEL_SIZE}'
WORKDIR = f'workdir_{MODEL_SIZE}'

LEARNING_RATE = 3e-4
LR_SCHEDULE_TYPE = 'cosine'
END_LEARNING_RATE = 3e-5
WARMUP_GRAD_STEPS = 2000
GRAD_NORM_CLIP = 1.
WEIGHT_DECAY = 0.1
BETA1 = 0.9
BETA2 = 0.95
ACCELERATOR = 'cuda'
PRECISION = 'bf16-mixed'
RANDOM_SEED = 11111

TRAIN_DATA_DIR = './../amber-data-arxiv-chunked-360/train' # './data'
TRAIN_EXAMPLES_PER_CHUNK = 71
N_CHUNKS = 11 # 360

def collate_fn(examples, device):
    token_ids = torch.tensor(
        [example['token_ids'] for example in examples], device=device)
    return {'input_ids': token_ids[:, :-1], 'labels': token_ids[:, 1:]}


def train_chunk(fabric,
                tokenizer,
                model,
                optimizer,
                lr_schedule_fn,
                examples,
                per_device_batch_size,
                accumulate_grad_batches,
                chunk_idx,
                run_wandb):
    # The step is the number of examples processed so far
    # eg, chunk_idx=1, len(examples)=1706976, per_device_batch_size=32, then step=1*1706976//32=53280
    step = chunk_idx * (len(examples) // per_device_batch_size)

    example_batch_idxes = tqdm.trange(
        0, len(examples), per_device_batch_size,
        desc=f'Training chunk {chunk_idx} (global_micro_batch_size='
             f'{per_device_batch_size * fabric.world_size}, '
             f'accumulate_grad_batches={accumulate_grad_batches})')
    
    # Loop through the examples in the batch
    # example_batch_idxes is a tqdm.trange object
    # eg, example_batch_idxes=0, 32, 64, ..., 1706944
    for i in example_batch_idxes:
        t0 = time.time()

        # Get the learning rate for the current step
        # eg, lr=3e-4    
        lr = lr_schedule_fn(step) # Get the learning rate for the current step
        step += 1
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr
        is_accumulating = (step % accumulate_grad_batches != 0)

        batch = collate_fn(
            examples=examples[i:i+per_device_batch_size], device=fabric.device)
        input_ids, labels = batch['input_ids'], batch['labels']
        with fabric.no_backward_sync(model, enabled=is_accumulating):
            logits = model(input_ids).logits # Forward pass of LlamaForCausalLM (LlammaForCausalLM calls LLModel.forward) LLMModel.forword method will call LlamaDecoderLayer.forward method with input_ids, attention_mask, and head_mask
            loss = torch.nn.functional.cross_entropy(
                logits.reshape((-1, logits.size(-1))), labels.reshape(-1))

            fabric.backward(loss / accumulate_grad_batches)

        if not is_accumulating:
            grad_norm = get_grad_norm(model=model)
            fabric.clip_gradients(model, optimizer, max_norm=GRAD_NORM_CLIP)
            optimizer.step()
            optimizer.zero_grad()

        log = {
            'loss': loss.item(),
            'learning_rate': lr,
            'step': step,
            'speed(#tok/s/gpu)': int(input_ids.numel() / (time.time() - t0))
        }
        if not is_accumulating:
            log['grad_norm'] = grad_norm

        example_batch_idxes.set_postfix(log)
        if run_wandb and fabric.global_rank == 0:
            wandb.log(log)

    # Save checkpoint every 10 chunks
    if chunk_idx % 10 == 0: # REMOVE ME
        save_checkpoint(
            fabric=fabric,
            tokenizer=tokenizer,
            model=model,
            optimizer=optimizer,
            save_dir=f'{WORKDIR}/ckpt_{chunk_idx}')


def main(n_nodes=1,
         n_devices_per_node=1,
         per_device_batch_size=1, # REMOVE ME (was 10 before)
         accumulate_grad_batches=1,
         run_wandb=False):
    
    fabric = L.Fabric(
        accelerator=ACCELERATOR,
        num_nodes=n_nodes,
        devices=n_devices_per_node,
        precision=PRECISION,
        # strategy=FSDPStrategy( # REMOVE ME (uncomment me when training on multiple GPUs)
        #     auto_wrap_policy=partial(
        #         transformer_auto_wrap_policy,
        #         transformer_layer_cls={LlamaDecoderLayer}),
        #     activation_checkpointing_policy={LlamaDecoderLayer},
        #     cpu_offload=True,
        #     limit_all_gathers=True)
        )    
    fabric.launch()

    if fabric.global_rank == 0:
        os.makedirs(WORKDIR, exist_ok=True)
        if run_wandb:
            wandb.init(project=PROJECT_NAME, name=RUN_NAME)

    last_ckpt_idx = get_last_ckpt_idx(workdir=WORKDIR)
    fabric.seed_everything(RANDOM_SEED + last_ckpt_idx + 1)
    
    tokenizer = AutoTokenizer.from_pretrained(HF_MODEL_NAME_OR_PATH)
    # model = LlamaForCausalLM(
    #     config=AutoConfig.from_pretrained(HF_MODEL_NAME_OR_PATH))

    # just for testing i have added this config with custom values
    config = LlamaConfig(        
        num_hidden_layers=24, # 2
        hidden_size=512, # 64
        num_attention_heads=8, # 2      
        use_cache=True)
    model = LlamaForCausalLM(config=config)
    print(f"Model type: {type(model)}")    
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=LEARNING_RATE,
        weight_decay=WEIGHT_DECAY,
        betas=(BETA1, BETA2),
        foreach=False)

    model, optimizer = fabric.setup(model, optimizer)
    if last_ckpt_idx != -1:
        fabric.load(
            path=f'{WORKDIR}/ckpt_{last_ckpt_idx}/fabric_ckpt',
            state={'model': model, 'optimizer': optimizer})

    torch.cuda.empty_cache() # clear cache to avoid OOM error.

    global_micro_batch_size = per_device_batch_size * fabric.world_size
    total_steps = TRAIN_EXAMPLES_PER_CHUNK // global_micro_batch_size * N_CHUNKS
    lr_schedule_fn = get_cosine_lr_decay_fn(
        total_steps=total_steps,
        warmup_steps=WARMUP_GRAD_STEPS * accumulate_grad_batches,
        learning_rate=LEARNING_RATE,
        end_learning_rate=END_LEARNING_RATE)

    for chunk_idx in range(last_ckpt_idx + 1, N_CHUNKS):
        examples = load_jsonl_examples(
            filename=f'{TRAIN_DATA_DIR}/train_{chunk_idx}.jsonl',
            n_examples=TRAIN_EXAMPLES_PER_CHUNK,
            shuffle=True,
            global_micro_batch_size=global_micro_batch_size,
            global_rank=fabric.global_rank,
            world_size=fabric.world_size)

        train_chunk(
            fabric=fabric,
            tokenizer=tokenizer,
            model=model,
            optimizer=optimizer,
            lr_schedule_fn=lr_schedule_fn,
            examples=examples,
            per_device_batch_size=per_device_batch_size,
            accumulate_grad_batches=accumulate_grad_batches,
            chunk_idx=chunk_idx,
            run_wandb=run_wandb)    

if __name__ == '__main__':
    fire.Fire(main)
