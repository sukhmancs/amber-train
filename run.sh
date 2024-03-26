#!/bin/bash
python main.py --n_nodes 1 --n_devices_per_node 1 --per_device_batch_size 2 --accumulate_grad_batches 1 --run_wandb