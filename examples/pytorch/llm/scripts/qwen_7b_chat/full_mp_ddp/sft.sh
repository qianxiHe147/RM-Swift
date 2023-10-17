# Experimental environment: 4 * A100
# 4 * 75GB GPU memory (use flash_attn)
# You need to install flash_attn or set gradient_checkpointing to True,
# otherwise it may result in an OOM (Out of Memory) error.
nproc_per_node=2

PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0,1,2,3 \
torchrun \
    --nproc_per_node=$nproc_per_node \
    --master_port 29500 \
    src/llm_sft.py \
    --model_type qwen-7b-chat \
    --sft_type full \
    --template_type chatml \
    --dtype bf16 \
    --output_dir output \
    --dataset medical-en medical-zh \
    --train_dataset_sample 200000 \
    --num_train_epochs 1 \
    --max_length 8192 \
    --gradient_checkpointing false \
    --batch_size 1 \
    --weight_decay 0.01 \
    --learning_rate 2e-5 \
    --gradient_accumulation_steps $(expr 16 / $nproc_per_node) \
    --max_grad_norm 1 \
    --warmup_ratio 0.03 \
    --eval_steps 100 \
    --save_steps 100 \
    --only_save_model true \
    --save_total_limit 2 \
    --logging_steps 10 \
    --use_flash_attn true \
    --push_to_hub false \
    --hub_model_id qwen-7b-chat-full \
    --hub_private_repo true \
    --hub_token 'your-sdk-token' \
