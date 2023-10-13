# Experimental environment: A100
nproc_per_node=2

PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0,1 \
torchrun \
    --nproc_per_node=$nproc_per_node \
    --master_port 29500 \
    src/llm_sft.py \
    --model_type internlm-20b \
    --sft_type lora \
    --template_type default-generation \
    --dtype bf16 \
    --output_dir output \
    --ddp_backend nccl \
    --dataset jd-zh \
    --train_dataset_sample -1 \
    --num_train_epochs 1 \
    --max_length 2048 \
    --lora_rank 8 \
    --lora_alpha 32 \
    --lora_dropout_p 0. \
    --lora_target_modules q_proj k_proj v_proj \
    --gradient_checkpointing false \
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
    --hub_model_id internlm-20b-lora \
    --hub_private_repo true \
    --hub_token 'your-sdk-token' \
