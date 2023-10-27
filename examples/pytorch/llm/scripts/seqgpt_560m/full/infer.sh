# Experimental environment: A10
PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0 \
python llm_infer.py \
    --model_id_or_path damo/nlp_seqgpt-560m \
    --model_revision master \
    --sft_type full \
    --template_type default-generation \
    --dtype bf16 \
    --ckpt_dir "output/seqgpt-560m/vx_xxx/checkpoint-xxx" \
    --eval_human false \
    --dataset ner-jave-zh \
    --max_length 1024 \
    --max_new_tokens 2048 \
    --temperature 0.9 \
    --top_k 20 \
    --top_p 0.9 \
    --do_sample true \
