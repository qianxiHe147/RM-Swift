CUDA_VISIBLE_DEVICES=0 \
python src/llm_infer.py \
    --model_type baichuan2-7b-chat \
    --sft_type lora \
    --template_type baichuan \
    --dtype bf16 \
    --ckpt_dir "runs/baichuan2-7b-chat/vx_xxx/checkpoint-xxx" \
    --eval_human false \
    --dataset damo-agent-mini-zh \
    --max_length 4096 \
    --max_new_tokens 2048 \
    --temperature 0.9 \
    --top_k 50 \
    --top_p 0.9 \
    --do_sample true \
