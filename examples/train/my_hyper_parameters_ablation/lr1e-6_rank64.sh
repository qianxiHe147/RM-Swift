# 22GB
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
NPROC_PER_NODE=8 \
MAX_PIXELS=262144 \
swift sft \
    --model /fs-computility/ai-shen/shared/hf-hub/models--Qwen--Qwen2.5-VL-72B-Instruct/snapshots/5d8e171e5ee60e8ca4c6daa380bd29f78fe19021 \
    --model_type qwen2_5_vl \
    --template qwen2_5_vl \
    --train_type lora \
    --deepspeed zero3 \
    --dataset '/fs-computility/ai-shen/heqianxi/ms-swift/my_data/SFT/sft_yesno_vl_data_2025.json' \
              '/fs-computility/ai-shen/heqianxi/ms-swift/my_data/SFT/train_conf_sft_0221_long.jsonl' \
    --torch_dtype bfloat16 \
    --num_train_epochs 2 \
    --per_device_train_batch_size 2 \
    --per_device_eval_batch_size 2 \
    --learning_rate 1e-6 \
    --lora_rank 64 \
    --lora_alpha 128 \
    --target_modules all-linear \
    --gradient_accumulation_steps 2 \
    --eval_steps 200 \
    --save_steps 2427 \
    --save_total_limit 5 \
    --logging_steps 5 \
    --truncation_strategy left \
    --max_length 2048 \
    --output_dir /fs-computility/ai-shen/kilab-shared/heqianxi/swift/hyper_qwen2.5vl72b_sft_lora_mix/lr1e-6_rank64 \
    --warmup_ratio 0.02 \
    --dataloader_num_workers 8 \
    >/fs-computility/ai-shen/heqianxi/ms-swift/logs/hyper/lr1e-6_rank64.log 2>&1 &