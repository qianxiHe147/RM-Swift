# Experimental environment: 2 * A10
# 2 * 13GB GPU memory
nproc_per_node=2

PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0,1 \
torchrun \
    --nproc_per_node=$nproc_per_node \
    --master_port 29500 \
    src/llm_sft.py \
    --model_type polylm-13b \
    --sft_type lora \
    --template_type default-generation \
    --dtype bf16 \
    --output_dir output \
    --ddp_backend nccl \
    --dtype bf16 \
    --dataset advertise-gen-zh \
    --train_dataset_sample 20000 \
    --num_train_epochs 1 \
    --max_length 2048 \
    --quantization_bit 4 \
    --bnb_4bit_comp_dtype bf16 \
    --lora_rank 8 \
    --lora_alpha 32 \
    --lora_dropout_p 0. \
    --lora_target_modules ALL \
    --gradient_checkpointing true \
    --batch_size 1 \
    --weight_decay 0. \
    --learning_rate 1e-4 \
    --gradient_accumulation_steps $(expr 16 / $nproc_per_node) \
    --max_grad_norm 0.5 \
    --warmup_ratio 0.03 \
    --eval_steps 100 \
    --save_steps 100 \
    --save_total_limit 2 \
    --logging_steps 10 \
    --push_to_hub false \
    --hub_model_id polylm-13b-qlora \
    --hub_private_repo true \
    --hub_token 'your-sdk-token' \
    --deepspeed_config_path 'ds_config/zero2.json' \
    --only_save_model true \
