# Experimental environment: A10
PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0 \
python llm_infer.py \
    --model_id_or_path modelscope/Llama-2-13b-chat-ms \
    --model_revision master \
    --sft_type lora \
    --template_type llama \
    --dtype bf16 \
    --ckpt_dir "output/llama2-13b-chat/vx_xxx/checkpoint-xxx" \
    --eval_human false \
    --dataset leetcode-python-en \
    --max_length 4096 \
    --check_dataset_strategy warning \
    --quantization_bit 4 \
    --bnb_4bit_comp_dtype bf16 \
    --max_new_tokens 2048 \
    --temperature 0.9 \
    --top_k 20 \
    --top_p 0.9 \
    --repetition_penalty 1.05 \
    --do_sample true \
    --merge_lora_and_save false \
