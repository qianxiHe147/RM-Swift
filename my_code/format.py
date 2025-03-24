# 把原llama-factory中的SFT格式转换为swift支持的SFT格式
import json

input_file_path = 'LLaMA-Factory/data/train_conf_sft_0221_long.json'
output_file_path = 'ms-swift/my_data/SFT/train_conf_sft_0221_long.jsonl'

with open(input_file_path, 'r', encoding='utf-8') as infile:
    data = json.load(infile)

with open(output_file_path, 'w', encoding='utf-8') as outfile:
    for item in data:
        new_item = {
            "messages": [
                {"role": "user", "content": item['instruction']},
                {"role": "assistant", "content": item['output']}
            ]
        }
        outfile.write(json.dumps(new_item, ensure_ascii=False) + '\n')















# # 把原来纯文本的DPO数据转变为swift格式的纯文本dpo数据
# import json

# input_file_path = '/fs-computility/ai-shen/heqianxi/LLaMA-Factory/data/acc_conf_dpo_0113.json'
# output_file_path = '/fs-computility/ai-shen/heqianxi/ms-swift/my_data/DPO/acc_conf_dpo_0113.jsonl'

# with open(input_file_path, 'r', encoding='utf-8') as infile:
#     data = json.load(infile)

# with open(output_file_path, 'w', encoding='utf-8') as outfile:
#     for item in data:
#         new_item = {
#             "messages": [
#                 {"role": "user", "content": item['instruction']},
#                 {"role": "assistant", "content": item['chosen']}
#             ],
#             "rejected_response": item['rejected']
#         }
#         outfile.write(json.dumps(new_item, ensure_ascii=False) + '\n')

















# # 把原来图像的DPO数据转变为swift格式的图像dpo数据
# import json

# input_file_path = '/fs-computility/ai-shen/heqianxi/LLaMA-Factory/data/dpo_data_vl_2025_noanswer.json'
# output_file_path = '/fs-computility/ai-shen/heqianxi/ms-swift/my_data/DPO/dpo_data_vl_2025_noanswer.jsonl'

# with open(input_file_path, 'r', encoding='utf-8') as infile:
#     data = json.load(infile)

# with open(output_file_path, 'w', encoding='utf-8') as outfile:
#     for item in data:
#         user_content = item['conversations'][0]['value']
        
#         new_item = {
#             "messages": [
#                 {"role": "user", "content": user_content},
#                 {"role": "assistant", "content": item['chosen']['value']}
#             ],
#             "rejected_response": item['rejected']['value'],
#             "images": item['images']
#         }
#         outfile.write(json.dumps(new_item, ensure_ascii=False) + '\n')
