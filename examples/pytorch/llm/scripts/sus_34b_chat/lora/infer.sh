# Experimental environment: A100
CUDA_VISIBLE_DEVICES=0 \
swift infer \
    --ckpt_dir "output/sus-34b-chat/vx_xxx/checkpoint-xxx" \
    --eval_human true \
