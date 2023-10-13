# Experimental environment: 3090
PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0 \
python src/llm_infer.py \
    --model_type baichuan2-7b \
    --sft_type lora \
    --template_type default-generation \
    --dtype bf16 \
    --ckpt_dir "output/baichuan2-7b/vx_xxx/checkpoint-xxx" \
    --eval_human false \
    --dataset advertise-gen-zh \
    --max_length 2048 \
    --quantization_bit 4 \
    --bnb_4bit_comp_dtype bf16 \
    --max_new_tokens 1024 \
    --temperature 0.9 \
    --top_k 20 \
    --top_p 0.9 \
    --do_sample true \
    --merge_lora_and_save false \
