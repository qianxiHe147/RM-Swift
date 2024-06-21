# Experimental environment: 2 * A100
# 80GB GPU memory
# Note: TorchAcc is currently only available internally.

# MASTER_ADDR=127.0.0.1 \

NPROC_PER_NODE=2 \
CUDA_VISIBLE_DEVICES=0,1 \
swift sft \
  --model_id_or_path baichuan-inc/Baichuan2-13B-Chat \
  --dataset codefuse-python-en \
  --sft_type lora \
  --dtype AUTO \
  --output_dir output \
  --num_train_epochs 1 \
  --max_length 2048 \
  --batch_size 2 \
  --use_flash_attn true \
  --gradient_accumulation_steps 1 \
  --dataset_test_ratio 0 \
  --save_strategy no \
  --eval_steps 2000000 \
  --save_steps 2000000 \
  --logging_steps 100 \
  --acc_steps 100 \
  --preprocess_num_proc 1 \
  --metric_warmup_step 0.1 \
  --report_to 'none'
