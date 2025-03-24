nproc_per_node=8

CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
NPROC_PER_NODE=$nproc_per_node \
swift rlhf \
    --rlhf_type dpo \
    --model /fs-computility/ai-shen/shared/hf-hub/models--Qwen--Qwen2.5-VL-72B-Instruct/snapshots/5d8e171e5ee60e8ca4c6daa380bd29f78fe19021 \
    --model_type qwen2_5_vl \
    --template qwen2_5_vl \
    --train_type lora \
    --dataset '/fs-computility/ai-shen/heqianxi/ms-swift/my_data/DPO/acc_conf_dpo_0113.jsonl' \
    --torch_dtype bfloat16 \
    --num_train_epochs 2 \
    --per_device_train_batch_size 2 \
    --per_device_eval_batch_size 2 \
    --learning_rate 5e-6 \
    --lora_rank 64 \
    --lora_alpha 128 \
    --target_modules all-linear \
    --gradient_accumulation_steps 2 \
    --eval_steps 200 \
    --save_steps 748 \
    --save_total_limit 5 \
    --logging_steps 5 \
    --truncation_strategy left \
    --max_length 2048 \
    --output_dir /fs-computility/ai-shen/kilab-shared/heqianxi/qwen2.5_dpo_0320_text_only_5e6 \
    --warmup_ratio 0.02 \
    --dataloader_num_workers 16 \
    --deepspeed zero3 \
    --dataset_num_proc 4 \
    >/fs-computility/ai-shen/heqianxi/ms-swift/logs/qwen2.5_dpo_0319_5e6.log 2>&1 &