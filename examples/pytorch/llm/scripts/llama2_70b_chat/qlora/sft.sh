# Experimental environment: 2 * 3090
# llama2 is not good at Chinese, openbuddy llama2 is recommended
CUDA_VISIBLE_DEVICES=0,1 \
python src/llm_sft.py \
    --model_type llama2-70b-chat \
    --sft_type lora \
    --output_dir output \
    --dataset alpaca-en \
    --train_dataset_sample 20000 \
    --num_train_epochs 1 \
    --max_length 2048 \
    --quantization_bit 4 \
    --lora_rank 8 \
    --lora_alpha 32 \
    --lora_dropout_p 0. \
    --batch_size 1 \
    --learning_rate 1e-4 \
    --gradient_accumulation_steps 16 \
    --eval_steps 100 \
    --save_steps 100 \
    --save_total_limit 2 \
    --logging_steps 10 \
