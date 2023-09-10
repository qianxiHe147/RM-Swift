# Experimental environment: 2 * A100
# 100GB GPU memory
CUDA_VISIBLE_DEVICES=0,1 \
python src/llm_sft.py \
    --model_type qwen-7b-chat \
    --sft_type full \
    --template_type chatml \
    --dtype bf16 \
    --output_dir runs \
    --dataset alpaca-en,alpaca-zh \
    --dataset_sample -1 \
    --num_train_epochs 1 \
    --max_length 2048 \
    --gradient_checkpointing false \
    --batch_size 1 \
    --weight_decay 0.01 \
    --learning_rate 2e-5 \
    --gradient_accumulation_steps 16 \
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
