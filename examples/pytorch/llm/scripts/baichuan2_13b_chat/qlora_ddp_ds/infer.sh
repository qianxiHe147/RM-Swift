# If you want to merge LoRA weight and save it, you need to set `--merge_lora_and_save true`.
PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0 \
python src/llm_infer.py \
    --model_type baichuan2-13b-chat \
    --sft_type lora \
    --template_type baichuan \
    --dtype bf16 \
    --ckpt_dir "output/baichuan2-13b-chat/vx_xxx/checkpoint-xxx" \
    --eval_human false \
    --dataset damo-agent-mini-zh \
    --max_length 4096 \
    --quantization_bit 4 \
    --bnb_4bit_comp_dtype bf16 \
    --max_new_tokens 2048 \
    --temperature 0.9 \
    --top_k 20 \
    --top_p 0.9 \
    --do_sample true \
    --merge_lora_and_save false \
