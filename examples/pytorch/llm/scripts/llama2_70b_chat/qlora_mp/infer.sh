# Experimental environment: 2 * 3090
# If you want to merge LoRA weight and save it, you need to set `--merge_lora_and_save true`.
PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0,1 \
python src/llm_infer.py \
    --model_type llama2-70b-chat \
    --sft_type lora \
    --template_type llama \
    --dtype bf16 \
    --ckpt_dir "output/llama2-70b-chat/vx_xxx/checkpoint-xxx" \
    --eval_human false \
    --dataset sql-create-context-en \
    --max_length 2048 \
    --quantization_bit 4 \
    --bnb_4bit_comp_dtype bf16 \
    --max_new_tokens 1024 \
    --temperature 0.9 \
    --top_k 20 \
    --top_p 0.9 \
    --do_sample true \
    --merge_lora_and_save false \
