nnodes=3
nproc_per_node=8

CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
NNODES=$nnodes \
NODE_RANK=2 \
MASTER_ADDR=172.30.56.85 \
MASTER_PORT=29500 \
NPROC_PER_NODE=$nproc_per_node \
swift sft \
    --model /fs-computility/ai-shen/shared/huggingface/models/Qwen/Qwen2.5-VL-72B-Instruct \
    --model_type qwen2_5_vl \
    --template qwen2_5_vl \
    --train_type full \
    --deepspeed zero3 \
    --dataset '/fs-computility/ai-shen/heqianxi/ms-swift/my_data/SFT/train_conf_sft_0221_long.jsonl' \
    --torch_dtype bfloat16 \
    --num_train_epochs 2 \
    --per_device_train_batch_size 1 \
    --per_device_eval_batch_size 2 \
    --learning_rate 5e-6 \
    --lora_rank 8 \
    --lora_alpha 32 \
    --target_modules all-linear \
    --gradient_accumulation_steps 2 \
    --eval_steps 200 \
    --save_steps 726 \
    --save_total_limit 5 \
    --logging_steps 5 \
    --truncation_strategy left \
    --max_length 2048 \
    --output_dir /fs-computility/ai-shen/kilab-shared/heqianxi/swift/qwen2.5_multi_test \
    --warmup_ratio 0.05 \
    --dataloader_num_workers 8 \
    >/fs-computility/ai-shen/heqianxi/ms-swift/logs/2_5_vl_2.log 2>&1 &