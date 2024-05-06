# Copyright (c) Alibaba, Inc. and its affiliates.
import inspect
import math
import os
import sys
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Set, Tuple, Union

import json
import numpy as np
import torch
import transformers
from datasets import Dataset as HfDataset
from datasets import concatenate_datasets
from packaging import version
from torch import dtype as Dtype
from transformers.utils import is_torch_bf16_gpu_available, is_torch_cuda_available, is_torch_npu_available, strtobool
from transformers.utils.versions import require_version

from swift.hub import HubApi, ModelScopeConfig
from swift.trainers import Seq2SeqTrainingArguments
from swift.tuners import Swift
from swift.utils import (add_version_to_work_dir, get_dist_setting, get_logger, get_pai_tensorboard_dir, is_dist,
                         is_local_master, is_mp, is_pai_training_job)
from .dataset import DATASET_MAPPING, _dataset_name_exists, get_dataset, register_dataset_info_file, sample_dataset
from .model import (MODEL_MAPPING, dtype_mapping, get_additional_saved_files, get_default_lora_target_modules,
                    get_default_template_type)
from .template import TEMPLATE_MAPPING
from .utils import is_vllm_available

logger = get_logger()


def is_adapter(sft_type: str) -> bool:
    return sft_type in {'lora', 'longlora', 'adalora', 'ia3', 'llamapro', 'adapter'}


class ArgumentsBase:

    @classmethod
    def _check_path(cls, k: str, value: Union[str, List[str]],
                    check_exist_path_set: Optional[Set[str]]) -> Union[str, List[str]]:
        if isinstance(value, str):
            value = os.path.expanduser(value)
            value = os.path.abspath(value)
            if k in check_exist_path_set and not os.path.exists(value):
                raise FileNotFoundError(f"`{k}`: '{value}'")
        elif isinstance(value, list):
            res = []
            for v in value:
                res.append(cls._check_path(k, v, check_exist_path_set))
            value = res
        return value

    def handle_path(self: Union['SftArguments', 'InferArguments']) -> None:
        check_exist_path = ['ckpt_dir', 'resume_from_checkpoint', 'custom_register_path']
        maybe_check_exist_path = ['model_id_or_path', 'custom_dataset_info']
        if isinstance(self, SftArguments):
            check_exist_path.append('deepspeed_config_path')
            maybe_check_exist_path.append('deepspeed')

        for k in maybe_check_exist_path:
            v = getattr(self, k)
            if isinstance(v, str) and v is not None and (v.startswith('~') or v.startswith('/')):
                check_exist_path.append(k)
        check_exist_path_set = set(check_exist_path)
        other_path = ['output_dir', 'logging_dir']
        for k in check_exist_path + other_path:
            value = getattr(self, k, None)
            if value is None:
                continue
            value = self._check_path(k, value, check_exist_path_set)
            setattr(self, k, value)

    def check_flash_attn(self: Union['SftArguments', 'InferArguments']) -> None:
        model_info = MODEL_MAPPING[self.model_type]
        support_flash_attn = model_info.get('support_flash_attn', False)
        if self.use_flash_attn and not support_flash_attn:
            logger.warning(f'use_flash_attn: {self.use_flash_attn}, ' f'but support_flash_attn: {support_flash_attn}')

    def handle_generation_config(self: Union['SftArguments', 'InferArguments']) -> None:
        if self.do_sample is False:
            # fix warning
            self.temperature = 1.
            self.top_p = 1.
            self.top_k = 50
            logger.info('Due to do_sample=False, the following settings are applied: args.temperature: '
                        f'{self.temperature}, args.top_p: {self.top_p}, args.top_k: {self.top_k}.')

    def select_dtype(self: Union['SftArguments', 'InferArguments']) -> Tuple[Optional[Dtype], bool, bool]:
        if not is_torch_cuda_available() and not is_torch_npu_available():
            # cpu
            if self.dtype == 'AUTO':
                self.dtype = 'fp32'
                logger.info(f'Setting args.dtype: {self.dtype}')
            assert self.dtype != 'fp16', 'The CPU does not support matrix multiplication with FP16.'
            if self.dtype == 'fp32':
                return torch.float32, False, False
            elif self.dtype == 'bf16':
                return torch.bfloat16, False, True
            else:
                raise ValueError(f'args.dtype: {self.dtype}')
        # cuda, npu
        if self.dtype == 'AUTO':
            if not is_torch_bf16_gpu_available():
                self.dtype = 'fp16'
            else:
                model_torch_dtype = MODEL_MAPPING[self.model_type].get('torch_dtype')
                if model_torch_dtype is not None:
                    self.dtype = dtype_mapping[model_torch_dtype]
                elif isinstance(self, SftArguments):
                    self.dtype = 'bf16'
                else:
                    return None, False, False

        torch_dtype = dtype_mapping_reversed[self.dtype]

        assert torch_dtype in {torch.float16, torch.bfloat16, torch.float32}
        if torch_dtype == torch.float16:
            if isinstance(self, SftArguments) and self.sft_type == 'full':
                self.dtype = 'fp32'
                torch_dtype = torch.float32
                logger.warning(
                    'Fine-tuning with full parameters does not support fp16, and is prone to NaN. '
                    'We will use the fp32 & AMP approach, which consumes approximately twice the memory of bf16.')
                logger.info(f'Setting torch_dtype: {torch_dtype}')
            fp16, bf16 = True, False
        elif torch_dtype == torch.bfloat16:
            support_bf16 = is_torch_bf16_gpu_available()
            if not support_bf16:
                logger.warning(f'support_bf16: {support_bf16}')
            fp16, bf16 = False, True
        else:
            fp16, bf16 = False, False
        return torch_dtype, fp16, bf16

    def select_bnb(self: Union['SftArguments', 'InferArguments']) -> Tuple[Optional[Dtype], bool, bool]:
        if self.bnb_4bit_comp_dtype == 'AUTO':
            self.bnb_4bit_comp_dtype = self.dtype

        if self.bnb_4bit_comp_dtype != 'AUTO':
            bnb_4bit_compute_dtype = dtype_mapping_reversed[self.bnb_4bit_comp_dtype]
            assert bnb_4bit_compute_dtype in {torch.float16, torch.bfloat16, torch.float32}
        else:
            bnb_4bit_compute_dtype = None
        quantization_bit = self.quantization_bit
        if quantization_bit == 4:
            require_version('bitsandbytes')
            load_in_4bit, load_in_8bit = True, False
        elif quantization_bit == 8:
            require_version('bitsandbytes')
            load_in_4bit, load_in_8bit = False, True
        else:
            load_in_4bit, load_in_8bit = False, False

        return bnb_4bit_compute_dtype, load_in_4bit, load_in_8bit

    def handle_custom_register(self: Union['SftArguments', 'InferArguments']) -> None:
        if self.custom_register_path is None:
            return
        folder, fname = os.path.split(self.custom_register_path)
        sys.path.append(folder)
        __import__(fname.rstrip('.py'))

    def handle_compatibility(self: Union['SftArguments', 'InferArguments']) -> None:
        template_type_mapping = {'chatglm2-generation': 'chatglm-generation', 'chatml': 'qwen'}
        model_type_mapping = {
            'openbmb-minicpm-2b-sft-chat': 'minicpm-2b-sft-chat',
            'openbmb-minicpm-2b-chat': 'minicpm-2b-chat',
        }
        dataset_name_mapping = {
            'ms-bench-mini': 'ms-bench#20000',
            'multi-alpaca-all': 'multi-alpaca',
            'instinwild-en': 'instinwild:subset',
            'instinwild-zh': 'instinwild:default',
            'firefly-all-zh': 'firefly-zh',
            'sharegpt-en': 'sharegpt:common-en/computer-en',
            'sharegpt-zh': 'sharegpt:common-zh/computer-zh/unknow-zh',
            'open-orca-gpt4': 'open-orca:default',
            'sharegpt-gpt4-mini': 'sharegpt-gpt4:default',
            'deepctrl-sft-zh': 'deepctrl-sft:default',
            'deepctrl-sft-en': 'deepctrl-sft:en',
            'ms-agent-for-agentfabric-default': 'ms-agent-for-agentfabric:default',
            'ms-agent-for-agentfabric-addition': 'ms-agent-for-agentfabric:addition',
            **{
                f'toolbench-for-alpha-umi-{sn}': f'toolbench-for-alpha-umi:{sn}'
                for sn in DATASET_MAPPING['toolbench-for-alpha-umi']['subsets']
            },
            'medical-mini-zh': 'medical-zh#50000',
            'cmnli-mini-zh': 'cmnli-zh#20000/200',
            'coco-mini-en': 'coco-en-mini',
            'coco-mini-en-2': 'coco-en-2-mini',
            'aishell1-mini-zh': 'aishell1-zh-mini',
            **{f'hh-rlhf-{sn}': f'hh-rlhf:{sn}'
               for sn in DATASET_MAPPING['hh-rlhf']['subsets']},
            **{
                f"hh-rlhf-cn-{sn.replace('_', '-')}": f'hh-rlhf-cn:{sn}'
                for sn in DATASET_MAPPING['hh-rlhf-cn']['subsets']
            },
            **{
                f"coig-cqia-{sn.replace('_', '-')}": f'coig-cqia:{sn}'
                for sn in DATASET_MAPPING['coig-cqia']['subsets']
            },
            **{f'ruozhiba-{sn}': f'ruozhiba:{sn}'
               for sn in DATASET_MAPPING['ruozhiba']['subsets']},
        }
        for _name, _mapping in [['template_type', template_type_mapping], ['model_type', model_type_mapping]]:
            k = getattr(self, _name)
            if k in _mapping:
                v = _mapping[k]
                setattr(self, _name, v)
                break
        if isinstance(self.dataset, str):
            self.dataset = [self.dataset]
        if len(self.dataset) == 1 and ',' in self.dataset[0]:
            self.dataset = self.dataset[0].split(',')
        for i, dataset in enumerate(self.dataset):
            if dataset in dataset_name_mapping:
                self.dataset[i] = dataset_name_mapping[dataset]
        for d in self.dataset:
            assert ',' not in d, f'dataset: {d}, please use `/`'
        if self.truncation_strategy == 'ignore':
            self.truncation_strategy = 'delete'
        if self.safe_serialization is not None:
            self.save_safetensors = self.safe_serialization
        if len(self.custom_train_dataset_path) > 0:
            self.dataset += self.custom_train_dataset_path
        if len(self.custom_val_dataset_path) > 0:
            self.dataset += self.custom_val_dataset_path

        if isinstance(self, InferArguments):
            if self.merge_lora_and_save is not None:
                self.merge_lora = self.merge_lora_and_save
            if self.vllm_lora_modules is not None:
                self.lora_modules = self.vllm_lora_modules
        if isinstance(self, AppUIArguments):
            if self.server_name is not None:
                self.host = self.server_name
            if self.server_port is not None:
                self.port = self.server_port
        if isinstance(self, SftArguments):
            if isinstance(self.train_dataset_mix_ds, str):
                self.train_dataset_mix_ds = [self.train_dataset_mix_ds]
            if self.only_save_model is not None:
                self.save_only_model = self.only_save_model
            if self.neftune_alpha is not None:
                self.neftune_noise_alpha = self.neftune_alpha
            if self.per_device_train_batch_size is not None:
                self.batch_size = self.per_device_train_batch_size
            if self.per_device_eval_batch_size is not None:
                self.eval_batch_size = self.per_device_eval_batch_size
            if self.deepspeed_config_path is not None:
                self.deepspeed = self.deepspeed_config_path

    def handle_custom_dataset_info(self):
        if self.custom_dataset_info is None:
            return
        register_dataset_info_file(self.custom_dataset_info)

    def _handle_dataset_sample(self):
        # compatibility. (Deprecated)
        # Avoid post-processing
        if len(self.dataset) == 1 and '#' not in self.dataset[0] and self.train_dataset_sample >= 0:
            self.dataset[0] = f'{self.dataset[0]}#{self.train_dataset_sample}'
            self.train_dataset_sample = -1

    def _register_self_cognition(self: Union['SftArguments', 'InferArguments']) -> None:

        # compatibility. (Deprecated)
        idx_list = _dataset_name_exists(self.dataset, 'self-cognition')
        assert len(idx_list) <= 1
        self.use_self_cognition = idx_list == 1
        if self.self_cognition_sample > 0:
            d = f'self-cognition#{self.self_cognition_sample}'
            if len(idx_list) == 1:
                self.dataset[idx_list[0]] = d
            else:
                self.dataset.append(d)
            self.use_self_cognition = True
        # check
        if self.use_self_cognition:
            for k in ['model_name', 'model_author']:
                v = getattr(self, k)
                if isinstance(v, str):
                    v = [v]
                elif v is None:
                    v = []
                if len(v) == 1:
                    v = v * 2
                if v[0] is None and v[1] is None:
                    raise ValueError('Please set self.model_name self.model_author. '
                                     'For example: `--model_name 小黄 "Xiao Huang" --model_author 魔搭 ModelScope`. '
                                     'Representing the model name and model author in Chinese and English.')
                setattr(self, k, v)

    def _handle_dataset_compat(self, train_dataset: HfDataset,
                               val_dataset: Optional[HfDataset]) -> Tuple[HfDataset, Optional[HfDataset]]:
        # compatibility. (Deprecated)
        random_state = np.random.RandomState(self.dataset_seed)
        val_dataset_sample = self.val_dataset_sample
        if train_dataset is not None and self.train_dataset_sample >= 0:
            train_dataset_sample = min(self.train_dataset_sample, train_dataset.shape[0])
            if train_dataset.shape[0] > train_dataset_sample:
                logger.info(f'train_dataset_sample: {train_dataset_sample}')
                train_idxs = random_state.permutation(train_dataset_sample)
                train_dataset = train_dataset.select(train_idxs)
            if val_dataset_sample is None:
                val_dataset_sample = max(int(train_dataset_sample * self.dataset_test_ratio), 1)
        if val_dataset is not None and val_dataset_sample is not None and val_dataset_sample >= 0:
            if val_dataset.shape[0] > val_dataset_sample:
                logger.info(f'val_dataset_sample: {val_dataset_sample}')
                val_idxs = random_state.permutation(val_dataset_sample)
                val_dataset = val_dataset.select(val_idxs)

        if (train_dataset is None or not hasattr(self, 'train_dataset_mix_ratio') or self.train_dataset_mix_ratio <= 0
                or len(self.train_dataset_mix_ds) == 0):
            return train_dataset, val_dataset

        mix_dataset_sample = int(len(train_dataset) * self.train_dataset_mix_ratio)
        logger.info(f'train_dataset_mix_ds: {self.train_dataset_mix_ds}')
        logger.info(f'len(train_dataset): {len(train_dataset)}, mix_dataset_sample: {mix_dataset_sample}')
        mixed_dataset = get_dataset(
            self.train_dataset_mix_ds, 0.0, random_state, check_dataset_strategy=self.check_dataset_strategy)[0]
        if len(mixed_dataset) < mix_dataset_sample:
            logger.warn(f'The length of dataset used for mixin: {self.train_dataset_mix_ds} are '
                        'lesser than the ratio required by the `train_dataset_mix_ratio` '
                        f'argument: {self.train_dataset_mix_ratio}. '
                        f'the actual ratio is: {len(mixed_dataset) / len(train_dataset):.6}.')
        else:
            mixed_dataset = sample_dataset(mixed_dataset, mix_dataset_sample, random_state)
        train_dataset = concatenate_datasets([train_dataset, mixed_dataset])
        return train_dataset, val_dataset

    def prepare_template(self):
        if self.template_type == 'AUTO':
            self.template_type = get_default_template_type(self.model_type)
            logger.info(f'Setting template_type: {self.template_type}')

    def set_model_type(self: Union['SftArguments', 'InferArguments']) -> None:
        # compat with swift<1.7
        if self.model_cache_dir is not None and self.model_id_or_path is None:
            self.model_id_or_path = self.model_cache_dir
            self.model_cache_dir = None

        if self.model_id_or_path is not None:
            use_hf = strtobool(os.environ.get('USE_HF', 'False'))
            model_mapping_reversed = {}
            for k, v in MODEL_MAPPING.items():
                if use_hf:
                    model_id = v.get('hf_model_id')
                else:
                    model_id = v.get('model_id_or_path')
                if model_id is None:
                    continue
                model_id = model_id.lower()
                model_mapping_reversed[model_id] = k
            model_id_or_path = self.model_id_or_path
            model_id_or_path_lower = model_id_or_path.lower()
            if model_id_or_path_lower not in model_mapping_reversed:
                if (isinstance(self, InferArguments) and 'checkpoint' in model_id_or_path
                        and 'merged' not in model_id_or_path and self.ckpt_dir is None):
                    raise ValueError('Please use `--ckpt_dir vx-xxx/checkpoint-xxx` to use the checkpoint.')
                if self.model_type is None:
                    raise ValueError(f"model_id_or_path: '{model_id_or_path}' is not registered. "
                                     'Please set `--model_type <model_type>` additionally.')
                assert self.model_cache_dir is None
            else:
                model_type = model_mapping_reversed[model_id_or_path_lower]
                assert self.model_type is None or self.model_type == model_type
                self.model_type = model_type
                logger.info(f'Setting args.model_type: {model_type}')
                if self.model_cache_dir is not None:
                    self.model_id_or_path = self.model_cache_dir

        error_msg = f'The model_type you can choose: {list(MODEL_MAPPING.keys())}'
        if self.model_type is None:
            raise ValueError('please setting `--model_type <model_type>`. ' + error_msg)
        elif self.model_type not in MODEL_MAPPING:
            raise ValueError(f"model_type: '{self.model_type}' is not registered. " + error_msg)
        model_info = MODEL_MAPPING[self.model_type]
        use_hf = strtobool(os.environ.get('USE_HF', 'False'))
        if self.model_revision is not None:
            model_info['revision'] = self.model_revision
            logger.info(f"Setting model_info['revision']: {self.model_revision}")
        elif use_hf:
            model_info['revision'] = 'main'
        self.model_revision = model_info['revision']
        if self.model_id_or_path is None:
            self.model_id_or_path = model_info['hf_model_id'] if use_hf else model_info['model_id_or_path']
        requires = model_info['requires']
        for require in requires:
            require_version(require)


@dataclass
class SftArguments(ArgumentsBase):
    # You can specify the model by either using the model_type or model_id_or_path.
    model_type: Optional[str] = field(
        default=None, metadata={'help': f'model_type choices: {list(MODEL_MAPPING.keys())}'})
    model_id_or_path: Optional[str] = None
    model_revision: Optional[str] = None
    model_layer_cls_name: Optional[str] = field(
        default=None,
        metadata={'help': "Decoder Class name of model, e.g. 'QWenBlock' for QWen, 'LlamaDecoderLayer' for LLama"})

    sft_type: Literal['lora', 'full', 'longlora', 'adalora', 'ia3', 'llamapro', 'adapter'] = 'lora'
    freeze_parameters: float = 0.  # 0 ~ 1
    additional_trainable_parameters: List[str] = field(default_factory=list)
    tuner_backend: Literal['swift', 'peft', 'unsloth'] = 'peft'
    template_type: str = field(
        default='AUTO', metadata={'help': f"template_type choices: {list(TEMPLATE_MAPPING.keys()) + ['AUTO']}"})
    output_dir: str = 'output'
    add_output_dir_suffix: Optional[bool] = None
    ddp_backend: Optional[Literal['nccl', 'gloo', 'mpi', 'ccl']] = None
    ddp_find_unused_parameters: Optional[bool] = None
    ddp_broadcast_buffers: Optional[bool] = None

    seed: int = 42
    resume_from_checkpoint: Optional[str] = None
    ignore_data_skip: bool = False
    dtype: Literal['bf16', 'fp16', 'fp32', 'AUTO'] = 'AUTO'

    dataset: List[str] = field(
        default_factory=list, metadata={'help': f'dataset choices: {list(DATASET_MAPPING.keys())}'})
    dataset_seed: int = 42
    dataset_test_ratio: float = 0.01
    use_loss_scale: bool = False  # for agent
    system: Optional[str] = None
    max_length: int = 2048  # -1: no limit
    truncation_strategy: Literal['delete', 'truncation_left'] = 'delete'
    check_dataset_strategy: Literal['none', 'discard', 'error', 'warning'] = 'none'
    # Chinese name and English name
    model_name: List[str] = field(default_factory=lambda: [None, None], metadata={'help': "e.g. ['小黄', 'Xiao Huang']"})
    model_author: List[str] = field(
        default_factory=lambda: [None, None], metadata={'help': "e.g. ['魔搭', 'ModelScope']"})
    # If you want to use qlora, set the quantization_bit to 8 or 4.
    # And you need to install bitsandbytes: `pip install bitsandbytes -U`
    # note: bf16 and quantization have requirements for gpu architecture
    quantization_bit: Literal[0, 4, 8] = 0
    bnb_4bit_comp_dtype: Literal['fp16', 'bf16', 'fp32', 'AUTO'] = 'AUTO'
    bnb_4bit_quant_type: Literal['fp4', 'nf4'] = 'nf4'
    bnb_4bit_use_double_quant: bool = True
    bnb_4bit_quant_storage: Optional[str] = None
    # lora
    lora_target_modules: List[str] = field(default_factory=lambda: ['DEFAULT'])
    lora_rank: int = 8
    lora_alpha: int = 32
    lora_dropout_p: float = 0.05
    lora_bias_trainable: Literal['none', 'all'] = 'none'
    # e.g. ['wte', 'ln_1', 'ln_2', 'ln_f', 'lm_head']
    lora_modules_to_save: List[str] = field(default_factory=list)
    lora_dtype: Literal['fp16', 'bf16', 'fp32', 'AUTO'] = 'AUTO'
    lora_lr_ratio: float = None
    use_rslora: bool = False
    use_dora: bool = False

    # adapter
    adapter_act: str = 'gelu'
    adapter_length: int = 128

    # galore
    use_galore: bool = False
    galore_rank: int = 128
    galore_target_modules: Optional[List[str]] = None
    galore_update_proj_gap: int = 50
    galore_scale: float = 1.0
    galore_proj_type: str = 'std'
    galore_optim_per_parameter: bool = False
    galore_with_embedding: bool = False

    # adalora
    adalora_target_r: int = 8
    adalora_init_r: int = 12
    adalora_tinit: int = 0
    adalora_tfinal: int = 0
    adalora_deltaT: int = 1
    adalora_beta1: float = 0.85
    adalora_beta2: float = 0.85
    adalora_orth_reg_weight: float = 0.5
    # ia3
    ia3_target_modules: List[str] = field(default_factory=lambda: ['DEFAULT'])
    ia3_feedforward_modules: List[str] = field(default_factory=list)
    ia3_modules_to_save: List[str] = field(default_factory=list)
    # llamapro
    llamapro_num_new_blocks: int = 4
    llamapro_num_groups: Optional[int] = None

    # neftune
    neftune_noise_alpha: Optional[float] = None  # e.g. 5, 10, 15
    neftune_backend: Literal['swift', 'transformers'] = None

    # lisa
    lisa_activated_layers: int = 0
    lisa_step_interval: int = 20

    gradient_checkpointing: Optional[bool] = None
    # e.g. 'default-zero3', 'default-zero2', 'ds_config/zero2.json', 'zero3-offload'
    deepspeed: Optional[str] = None
    batch_size: int = 1
    eval_batch_size: Optional[int] = None
    num_train_epochs: int = 1
    # if max_steps >= 0, override num_train_epochs
    max_steps: int = -1
    optim: str = 'adamw_torch'
    adam_beta1: float = 0.9
    adam_beta2: float = 0.999
    learning_rate: Optional[float] = None
    weight_decay: float = 0.1
    gradient_accumulation_steps: Optional[int] = None
    max_grad_norm: float = 0.5
    predict_with_generate: bool = False
    lr_scheduler_type: str = 'linear'
    warmup_ratio: float = 0.05

    eval_steps: int = 50
    save_steps: Optional[int] = None
    save_only_model: Optional[bool] = None
    save_total_limit: int = 2  # save last and best. -1: all checkpoints
    logging_steps: int = 5
    dataloader_num_workers: int = 1
    dataloader_pin_memory: bool = True

    # push to ms hub
    push_to_hub: bool = False
    # 'user_name/repo_name' or 'repo_name'
    hub_model_id: Optional[str] = None
    # None: use env var `MODELSCOPE_API_TOKEN`
    hub_token: Optional[str] = field(
        default=None, metadata={'help': 'SDK token can be found in https://modelscope.cn/my/myaccesstoken'})
    hub_private_repo: bool = False
    push_hub_strategy: Literal['end', 'push_best', 'push_last', 'checkpoint', 'all_checkpoints'] = 'push_best'

    # other
    test_oom_error: bool = field(
        default=False,
        metadata={
            'help':
            'If set to True, the train_dataset will be sorted in descending order based on max_length, '
            'enabling faster detection of OOM (Out of Memory) errors.'
        })
    disable_tqdm: bool = False
    lazy_tokenize: Optional[bool] = None
    preprocess_num_proc: int = 1
    use_flash_attn: Optional[bool] = None
    ignore_args_error: bool = False  # True: notebook compatibility
    check_model_is_latest: bool = True

    logging_dir: Optional[str] = None
    report_to: List[str] = field(default_factory=lambda: ['tensorboard'])
    acc_strategy: Literal['token', 'sentence'] = 'token'
    save_on_each_node: bool = True
    evaluation_strategy: Literal['steps', 'no'] = 'steps'
    save_strategy: Literal['steps', 'no'] = 'steps'
    save_safetensors: bool = True
    gpu_memory_fraction: Optional[float] = None
    include_num_input_tokens_seen: Optional[bool] = False
    custom_register_path: Optional[str] = None  # .py
    custom_dataset_info: Optional[str] = None  # .json

    # generation config
    max_new_tokens: int = 2048
    do_sample: bool = True
    temperature: float = 0.3
    top_k: int = 20
    top_p: float = 0.7
    repetition_penalty: float = 1.
    num_beams: int = 1

    # fsdp option
    fsdp: Optional[str] = ''
    # fsdp config file
    fsdp_config: Optional[str] = None

    # compatibility hf
    per_device_train_batch_size: Optional[int] = None
    per_device_eval_batch_size: Optional[int] = None
    # compatibility. (Deprecated)
    self_cognition_sample: int = 0
    train_dataset_mix_ratio: float = 0.
    train_dataset_mix_ds: List[str] = field(default_factory=lambda: ['ms-bench'])
    train_dataset_sample: int = -1  # -1: all dataset
    val_dataset_sample: Optional[int] = None  # -1: all dataset
    safe_serialization: Optional[bool] = None
    only_save_model: Optional[bool] = None
    neftune_alpha: Optional[float] = None
    deepspeed_config_path: Optional[str] = None
    model_cache_dir: Optional[str] = None
    custom_train_dataset_path: List[str] = field(default_factory=list)
    custom_val_dataset_path: List[str] = field(default_factory=list)

    def prepare_push_ms_hub(self) -> None:
        if not self.push_to_hub:
            return
        if self.hub_model_id is None:
            self.hub_model_id = f'{self.model_type}-{self.sft_type}'
            logger.info(f'Setting hub_model_id: {self.hub_model_id}')

        api = HubApi()
        if self.hub_token is None:
            self.hub_token = os.environ.get('MODELSCOPE_API_TOKEN')
        if self.hub_token is not None:
            api.login(self.hub_token)
        else:
            assert ModelScopeConfig.get_token() is not None, 'Please enter hub_token'
        logger.info('hub login successful!')

    def _prepare_target_modules(self, target_modules) -> List[str]:
        if isinstance(target_modules, str):
            target_modules = [target_modules]
        if len(target_modules) == 0:
            return target_modules
        elif len(target_modules) == 1:
            if ',' in target_modules[0]:
                target_modules = target_modules[0].split(',')
        if 'AUTO' in target_modules:
            target_modules.remove('AUTO')
            target_modules.append('DEFAULT')
        if 'DEFAULT' in target_modules:
            target_modules.remove('DEFAULT')
            target_modules += get_default_lora_target_modules(self.model_type)
        if 'EMBEDDING' in target_modules:
            target_modules.remove('EMBEDDING')
            self.lora_use_embedding = True
        if 'ALL' in target_modules:
            target_modules.remove('ALL')
            self.lora_use_all = True
        return target_modules

    def _prepare_modules_to_save(self, modules_to_save) -> List[str]:
        if isinstance(modules_to_save, str):
            modules_to_save = [modules_to_save]
        if len(modules_to_save) == 0:
            return modules_to_save
        if 'EMBEDDING' in modules_to_save:
            modules_to_save.remove('EMBEDDING')
            self.lora_m2s_use_embedding = True
        if 'LN' in modules_to_save:
            modules_to_save.remove('LN')
            self.lora_m2s_use_ln = True
        return modules_to_save

    def __post_init__(self) -> None:
        self.handle_compatibility()
        self._register_self_cognition()
        self._handle_dataset_sample()
        if is_pai_training_job():
            self._handle_pai_compat()
        ds_config_folder = os.path.abspath(os.path.join(__file__, '..', '..', 'ds_config'))
        deepspeed_mapping = {
            'default-zero2': 'zero2.json',
            'default-zero3': 'zero3.json',
            'zero3-offload': 'zero3_offload.json'
        }
        for ds_name, ds_config in deepspeed_mapping.items():
            if self.deepspeed == ds_name:
                self.deepspeed = os.path.join(ds_config_folder, ds_config)
                break

        self.handle_path()
        self.handle_custom_register()
        self.handle_custom_dataset_info()
        self.set_model_type()
        self.check_flash_attn()
        self.handle_generation_config()

        self.lora_use_embedding = False
        self.lora_use_all = False
        self.lora_m2s_use_embedding = False
        self.lora_m2s_use_ln = False
        if self.sft_type == 'ia3':
            self.ia3_feedforward_modules = self._prepare_target_modules(self.ia3_feedforward_modules)
            self.ia3_target_modules = self._prepare_target_modules(self.ia3_target_modules)
            self.ia3_modules_to_save = self._prepare_modules_to_save(self.ia3_modules_to_save)
        else:
            self.lora_target_modules = self._prepare_target_modules(self.lora_target_modules)
            self.lora_modules_to_save = self._prepare_modules_to_save(self.lora_modules_to_save)
        if self.use_self_cognition and self.sft_type == 'lora' and not self.lora_use_all:
            logger.warning('Due to knowledge editing involved, it is recommended to add LoRA on MLP. '
                           'For example: `--lora_target_modules ALL`. '
                           'If you have already added LoRA on MLP, please ignore this warning.')

        if self.sft_type in {'adalora', 'ia3'} and self.lora_use_embedding:
            raise ValueError('`adalora` and `ia3` do not support setting embedding as target_modules.')

        self.torch_dtype, self.fp16, self.bf16 = self.select_dtype()
        world_size = 1
        if is_dist():
            rank, local_rank, world_size, _ = get_dist_setting()
            if is_torch_npu_available():
                torch.npu.set_device(local_rank)
            else:
                torch.cuda.set_device(local_rank)
            self.seed += rank  # Avoid the same dropout
            if self.ddp_backend is None:
                self.ddp_backend = 'nccl'
            if self.ddp_backend == 'gloo' and self.quantization_bit != 0:
                raise ValueError('not supported, please use `nccl`')

        if is_adapter(self.sft_type):
            assert self.freeze_parameters == 0., (
                'lora does not support `freeze_parameters`, please set `--sft_type full`')
            assert len(self.additional_trainable_parameters) == 0, (
                'lora does not support `additional_trainable_parameters`, please set `--sft_type full`')
            if 'int4' in self.model_type or 'int8' in self.model_type or 'awq' in self.model_type:
                assert self.quantization_bit == 0, 'int4, int8 or awq models do not need to be quantized again.'
            if self.learning_rate is None:
                self.learning_rate = 1e-4
            if self.save_only_model is None:
                if self.deepspeed is not None and version.parse(transformers.__version__) < version.parse('4.37'):
                    self.save_only_model = True
                else:
                    self.save_only_model = False
        elif self.sft_type == 'full':
            assert 0 <= self.freeze_parameters <= 1
            assert self.quantization_bit == 0, 'Full parameter fine-tuning does not support quantization.'
            assert self.dtype != 'fp16', ("Fine-tuning with dtype=='fp16' can lead to NaN issues. "
                                          'Please use fp32+AMP or bf16 to perform full parameter fine-tuning.')
            if isinstance(self.additional_trainable_parameters, str):
                self.additional_trainable_parameters = [self.additional_trainable_parameters]
            if self.learning_rate is None:
                self.learning_rate = 1e-5
            if self.save_only_model is None:
                self.save_only_model = True
        else:
            raise ValueError(f'sft_type: {self.sft_type}')

        self.prepare_template()
        if len(self.dataset) == 0:
            raise ValueError(f'self.dataset: {self.dataset}, Please input the training dataset.')

        if self.save_steps is None:
            self.save_steps = self.eval_steps
        self.bnb_4bit_compute_dtype, self.load_in_4bit, self.load_in_8bit = self.select_bnb()

        if self.neftune_backend is None:
            self.neftune_backend = 'swift' if version.parse(transformers.__version__) < version.parse('4.35') \
                else 'transformers'

        self.prepare_push_ms_hub()
        self.train_sampler_random = not self.test_oom_error
        if self.eval_batch_size is None:
            if self.predict_with_generate:
                self.eval_batch_size = 1
            else:
                self.eval_batch_size = self.batch_size
        if self.save_total_limit == -1:
            self.save_total_limit = None
        if self.max_length == -1:
            self.max_length = None

        if self.deepspeed is not None:
            if is_mp():
                raise ValueError('DeepSpeed is not compatible with MP. '
                                 f'n_gpu: {torch.cuda.device_count()}, '
                                 f'local_world_size: {get_dist_setting()[3]}.')
            require_version('deepspeed')
            if self.deepspeed.endswith('.json') or os.path.isfile(self.deepspeed):
                with open(self.deepspeed, 'r', encoding='utf-8') as f:
                    self.deepspeed = json.load(f)
            logger.info(f'Using deepspeed: {self.deepspeed}')

        if self.gradient_accumulation_steps is None:
            self.gradient_accumulation_steps = math.ceil(16 / self.batch_size / world_size)
        template_info = TEMPLATE_MAPPING[self.template_type]
        if self.lazy_tokenize is None:
            self.lazy_tokenize = template_info.get('lazy_tokenize', False)
            logger.info(f'Setting args.lazy_tokenize: {self.lazy_tokenize}')
        if 'dataloader_num_workers' in template_info:
            self.dataloader_num_workers = template_info['dataloader_num_workers']
            logger.info(f'Setting args.dataloader_num_workers: {self.dataloader_num_workers}')
        if 'dataloader_pin_memory' in template_info:
            self.dataloader_pin_memory = template_info['dataloader_pin_memory']
            logger.info(f'Setting args.dataloader_pin_memory: {self.dataloader_pin_memory}')
        if 'qwen-audio' in self.model_type:
            assert self.preprocess_num_proc == 1 or self.lazy_tokenize, 'not support'
        model_info = MODEL_MAPPING[self.model_type]
        support_gradient_checkpointing = model_info.get('support_gradient_checkpointing', True)
        if self.gradient_checkpointing is None:
            self.gradient_checkpointing = support_gradient_checkpointing
        elif not support_gradient_checkpointing and self.gradient_checkpointing:
            logger.warning(f'{self.model_type} not support gradient_checkpointing.')

        self._init_training_args()

        if self.add_output_dir_suffix is None:
            self.add_output_dir_suffix = True
        if self.add_output_dir_suffix:
            self.output_dir = os.path.join(self.output_dir, self.model_type)
            self.output_dir = add_version_to_work_dir(self.output_dir)
            logger.info(f'output_dir: {self.output_dir}')
            self.training_args.output_dir = self.output_dir
            self.training_args.run_name = self.output_dir
        if is_local_master():
            os.makedirs(self.output_dir, exist_ok=True)
        if self.logging_dir is None:
            self.logging_dir = f'{self.output_dir}/runs'
            self.training_args.logging_dir = self.logging_dir

    def _init_training_args(self) -> None:
        additional_saved_files = []
        if self.sft_type == 'full':
            additional_saved_files = get_additional_saved_files(self.model_type)

        kwargs = {}
        if self.neftune_backend != 'swift':
            kwargs['neftune_noise_alpha'] = self.neftune_noise_alpha

        parameters = inspect.signature(Seq2SeqTrainingArguments.__init__).parameters
        if 'include_num_input_tokens_seen' in parameters:
            kwargs['include_num_input_tokens_seen'] = self.include_num_input_tokens_seen

        training_args = Seq2SeqTrainingArguments(
            output_dir=self.output_dir,
            evaluation_strategy=self.evaluation_strategy,
            logging_dir=self.logging_dir,
            per_device_train_batch_size=self.batch_size,
            per_device_eval_batch_size=self.eval_batch_size,
            gradient_accumulation_steps=self.gradient_accumulation_steps,
            learning_rate=self.learning_rate,
            weight_decay=self.weight_decay,
            max_grad_norm=self.max_grad_norm,
            num_train_epochs=self.num_train_epochs,
            max_steps=self.max_steps,
            lr_scheduler_type=self.lr_scheduler_type,
            warmup_ratio=self.warmup_ratio,
            logging_steps=self.logging_steps,
            save_strategy=self.save_strategy,
            save_steps=self.save_steps,
            save_total_limit=self.save_total_limit,
            remove_unused_columns=False,
            bf16=self.bf16,
            fp16=self.fp16,
            eval_steps=self.eval_steps,
            dataloader_num_workers=self.dataloader_num_workers,
            dataloader_pin_memory=self.dataloader_pin_memory,
            metric_for_best_model='rouge-l' if self.predict_with_generate else 'loss',
            greater_is_better=self.predict_with_generate,
            sortish_sampler=True,
            optim=self.optim,
            adam_beta1=self.adam_beta1,
            adam_beta2=self.adam_beta2,
            hub_model_id=self.hub_model_id,
            hub_private_repo=self.hub_private_repo,
            push_hub_strategy=self.push_hub_strategy,
            hub_token=self.hub_token,
            push_to_hub=self.push_to_hub,
            resume_from_checkpoint=self.resume_from_checkpoint,
            ignore_data_skip=self.ignore_data_skip,
            ddp_backend=self.ddp_backend,
            gradient_checkpointing=self.gradient_checkpointing,
            predict_with_generate=self.predict_with_generate,
            # generation_config=generation_config,
            local_rank=get_dist_setting()[1],
            save_only_model=self.save_only_model,
            train_sampler_random=self.train_sampler_random,
            report_to=self.report_to,
            deepspeed=self.deepspeed,
            additional_saved_files=additional_saved_files,
            disable_tqdm=self.disable_tqdm,
            save_on_each_node=self.save_on_each_node,
            acc_strategy=self.acc_strategy,
            save_safetensors=self.save_safetensors,
            logging_first_step=True,
            fsdp=self.fsdp,
            fsdp_config=self.fsdp_config,
            **kwargs)

        training_args.ddp_find_unused_parameters = self.ddp_find_unused_parameters
        training_args.ddp_broadcast_buffers = self.ddp_broadcast_buffers
        if is_dist() and training_args.ddp_find_unused_parameters is None:
            if self.gradient_checkpointing:
                training_args.ddp_find_unused_parameters = False
            else:
                training_args.ddp_find_unused_parameters = True

        if is_dist() and training_args.ddp_broadcast_buffers is None:
            if self.gradient_checkpointing:
                training_args.ddp_broadcast_buffers = False
            else:
                training_args.ddp_broadcast_buffers = True

        self.training_args = training_args

    def _handle_pai_compat(self) -> None:
        assert is_pai_training_job() is True
        logger.info('Handle pai compat...')
        pai_tensorboard_dir = get_pai_tensorboard_dir()
        if self.logging_dir is None and pai_tensorboard_dir is not None:
            self.logging_dir = pai_tensorboard_dir
            logger.info(f'Setting args.logging_dir: {self.logging_dir}')
        if self.add_output_dir_suffix is None:
            self.add_output_dir_suffix = False
            logger.info(f'Setting args.add_output_dir_suffix: {self.add_output_dir_suffix}')


@dataclass
class InferArguments(ArgumentsBase):
    # You can specify the model by either using the model_type or model_id_or_path.
    model_type: Optional[str] = field(
        default=None, metadata={'help': f'model_type choices: {list(MODEL_MAPPING.keys())}'})
    model_id_or_path: Optional[str] = None
    model_revision: Optional[str] = None

    sft_type: Literal['lora', 'longlora', 'full', 'adalora', 'ia3', 'llamapro'] = 'lora'
    template_type: str = field(
        default='AUTO', metadata={'help': f"template_type choices: {list(TEMPLATE_MAPPING.keys()) + ['AUTO']}"})
    infer_backend: Literal['AUTO', 'vllm', 'pt'] = 'AUTO'
    ckpt_dir: Optional[str] = field(default=None, metadata={'help': '/path/to/your/vx-xxx/checkpoint-xxx'})
    load_args_from_ckpt_dir: bool = True
    load_dataset_config: bool = False
    eval_human: Optional[bool] = None

    seed: int = 42
    dtype: Literal['bf16', 'fp16', 'fp32', 'AUTO'] = 'AUTO'

    dataset: List[str] = field(
        default_factory=list, metadata={'help': f'dataset choices: {list(DATASET_MAPPING.keys())}'})
    dataset_seed: int = 42
    dataset_test_ratio: float = 0.01
    show_dataset_sample: int = 10
    save_result: bool = True
    system: Optional[str] = None
    max_length: int = -1  # -1: no limit
    truncation_strategy: Literal['delete', 'truncation_left'] = 'delete'
    check_dataset_strategy: Literal['none', 'discard', 'error', 'warning'] = 'none'
    # Chinese name and English name
    model_name: List[str] = field(default_factory=lambda: [None, None], metadata={'help': "e.g. ['小黄', 'Xiao Huang']"})
    model_author: List[str] = field(
        default_factory=lambda: [None, None], metadata={'help': "e.g. ['魔搭', 'ModelScope']"})

    quantization_bit: Literal[0, 4, 8] = 0
    bnb_4bit_comp_dtype: Literal['fp16', 'bf16', 'fp32', 'AUTO'] = 'AUTO'
    bnb_4bit_quant_type: Literal['fp4', 'nf4'] = 'nf4'
    bnb_4bit_use_double_quant: bool = True
    bnb_4bit_quant_storage: Optional[str] = None

    max_new_tokens: int = 2048
    do_sample: bool = True
    temperature: float = 0.3
    top_k: int = 20
    top_p: float = 0.7
    repetition_penalty: float = 1.
    num_beams: int = 1
    stop_words: List[str] = None

    # other
    use_flash_attn: Optional[bool] = None
    ignore_args_error: bool = False  # True: notebook compatibility
    stream: bool = True
    merge_lora: bool = False
    merge_device_map: Optional[str] = None
    save_safetensors: bool = True
    overwrite_generation_config: Optional[bool] = None
    verbose: Optional[bool] = None
    custom_register_path: Optional[str] = None  # .py
    custom_dataset_info: Optional[str] = None  # .json

    # vllm
    gpu_memory_utilization: float = 0.9
    tensor_parallel_size: int = 1
    max_model_len: Optional[int] = None
    vllm_enable_lora: bool = False
    vllm_max_lora_rank: int = 16
    lora_modules: List[str] = field(default_factory=list)

    # compatibility. (Deprecated)
    self_cognition_sample: int = 0
    train_dataset_sample: int = -1  # Used for splitting the validation set.
    val_dataset_sample: Optional[int] = None  # -1: all dataset
    safe_serialization: Optional[bool] = None
    model_cache_dir: Optional[str] = None
    merge_lora_and_save: Optional[bool] = None
    custom_train_dataset_path: List[str] = field(default_factory=list)
    custom_val_dataset_path: List[str] = field(default_factory=list)
    vllm_lora_modules: List[str] = None

    def __post_init__(self) -> None:
        if self.ckpt_dir is not None and not self.check_ckpt_dir_correct(self.ckpt_dir):
            logger.warning(f'The checkpoint dir {self.ckpt_dir} passed in is invalid, please make sure'
                           'the dir contains a `configuration.json` file.')
        self.handle_compatibility()
        self._register_self_cognition()
        self.handle_path()
        logger.info(f'ckpt_dir: {self.ckpt_dir}')
        if self.ckpt_dir is None and self.load_args_from_ckpt_dir:
            self.load_args_from_ckpt_dir = False
            logger.info('Due to `ckpt_dir` being `None`, `load_args_from_ckpt_dir` is set to `False`.')
        if self.load_args_from_ckpt_dir:
            self.load_from_ckpt_dir()
        else:
            assert self.load_dataset_config is False, 'You need to first set `--load_args_from_ckpt_dir true`.'
        self._handle_dataset_sample()
        self.handle_custom_register()
        self.handle_custom_dataset_info()
        self.set_model_type()
        self.check_flash_attn()
        self.handle_generation_config()

        self.torch_dtype, _, _ = self.select_dtype()
        self.prepare_template()
        if self.eval_human is None:
            if not len(self.dataset) > 0:
                self.eval_human = True
            else:
                self.eval_human = False
            logger.info(f'Setting self.eval_human: {self.eval_human}')
        elif self.eval_human is False and not len(self.dataset) > 0:
            raise ValueError('Please provide the dataset or set `--load_dataset_config true`.')

        self.bnb_4bit_compute_dtype, self.load_in_4bit, self.load_in_8bit = self.select_bnb()

        if self.max_length == -1:
            self.max_length = None
        if self.overwrite_generation_config is None:
            if self.ckpt_dir is None:
                self.overwrite_generation_config = False
            else:
                self.overwrite_generation_config = True
            logger.info(f'Setting overwrite_generation_config: {self.overwrite_generation_config}')
        if self.ckpt_dir is None:
            self.sft_type = 'full'

        self.handle_infer_backend()

    def handle_infer_backend(self):
        model_info = MODEL_MAPPING[self.model_type]
        support_vllm = model_info.get('support_vllm', False)
        self.lora_request_list = None
        if self.infer_backend == 'AUTO':
            self.infer_backend = 'pt'
            if is_vllm_available() and support_vllm:
                if ((self.sft_type == 'full' or self.sft_type == 'lora' and self.merge_lora)
                        and self.quantization_bit == 0):
                    self.infer_backend = 'vllm'
                if self.vllm_enable_lora:
                    self.infer_backend = 'vllm'
        if self.infer_backend == 'vllm':
            require_version('vllm')
            assert self.quantization_bit == 0, 'VLLM does not support bnb.'
            if not support_vllm:
                logger.warning(f'vllm not support `{self.model_type}`')
            if self.sft_type == 'lora' and not self.vllm_enable_lora:
                assert self.merge_lora is True, ('To use VLLM, you need to provide the complete weight parameters. '
                                                 'Please set `--merge_lora true`.')
        if (self.infer_backend == 'vllm' and self.vllm_enable_lora
                or self.infer_backend == 'pt' and isinstance(self, DeployArguments) and self.sft_type == 'lora'):
            assert self.ckpt_dir is not None
            self.lora_modules.append(f'default-lora={self.ckpt_dir}')
            self.lora_request_list = _parse_lora_modules(self.lora_modules, True)
            logger.info(f'args.lora_request_list: {self.lora_request_list}')

        template_info = TEMPLATE_MAPPING[self.template_type]
        if self.num_beams != 1:
            self.stream = False
            logger.info('Setting self.stream: False')
        self.infer_media_type = template_info.get('infer_media_type', 'none')
        if self.merge_device_map is None:
            self.merge_device_map = 'cpu'

    def load_from_ckpt_dir(self) -> None:
        sft_args_path = os.path.join(self.ckpt_dir, 'sft_args.json')
        if not os.path.exists(sft_args_path):
            logger.info(f'{sft_args_path} not found')
            return
        with open(sft_args_path, 'r', encoding='utf-8') as f:
            sft_args = json.load(f)
        imported_keys = [
            'model_type', 'model_revision', 'sft_type', 'template_type', 'system', 'quantization_bit',
            'bnb_4bit_comp_dtype', 'bnb_4bit_quant_type', 'bnb_4bit_use_double_quant'
        ]
        if self.load_dataset_config:
            imported_keys += [
                'dataset', 'dataset_seed', 'dataset_test_ratio', 'check_dataset_strategy', 'self_cognition_sample',
                'model_name', 'model_author', 'train_dataset_sample', 'val_dataset_sample'
            ]
        for key in imported_keys:
            if key == 'dataset' and len(getattr(self, key)) > 0:
                continue
            setattr(self, key, sft_args.get(key))

        for k in ['model_id_or_path', 'custom_register_path', 'custom_dataset_info']:
            if getattr(self, k) is None:
                setattr(self, k, sft_args.get(k))

        if self.dtype == 'AUTO':
            self.dtype = sft_args.get('dtype')

    @staticmethod
    def check_ckpt_dir_correct(ckpt_dir) -> bool:
        """Check the checkpoint dir is correct, which means it must contains a `configuration.json` file.
        Args:
            ckpt_dir: The checkpoint dir
        Returns:
            A bool value represents the dir is valid or not.
        """
        if not os.path.exists(ckpt_dir):
            return False
        return os.path.isfile(os.path.join(ckpt_dir, 'configuration.json'))


@dataclass
class AppUIArguments(InferArguments):
    host: str = '127.0.0.1'
    port: int = 7860
    share: bool = False
    # compatibility. (Deprecated)
    server_name: Optional[str] = None
    server_port: Optional[int] = None


@dataclass
class DeployArguments(InferArguments):
    host: str = '127.0.0.1'
    port: int = 8000
    ssl_keyfile: Optional[str] = None
    ssl_certfile: Optional[str] = None

    def __post_init__(self):
        super().__post_init__()
        model_info = MODEL_MAPPING[self.model_type]
        tags = model_info.get('tags', [])
        if 'multi-modal' in tags:
            raise ValueError('Deployment of multimodal models is currently not supported.')


@dataclass
class EvalArguments(InferArguments):

    name: Optional[str] = None

    eval_url: Optional[str] = None

    eval_token: Optional[str] = 'EMPTY'

    eval_is_chat_model: bool = None

    eval_dataset: List[str] = field(default_factory=lambda: ['ceval', 'gsm8k', 'arc'])

    eval_few_shot: Optional[int] = None

    eval_limit: Optional[int] = None

    custom_eval_config: Optional[str] = None

    eval_use_cache: Optional[bool] = False

    def set_model_type(self) -> None:
        if self.eval_url is None:
            super().set_model_type()

    def check_flash_attn(self) -> None:
        if self.eval_url is None:
            super().check_flash_attn()

    def prepare_template(self) -> None:
        if self.eval_url is None:
            super().prepare_template()

    def handle_infer_backend(self) -> None:
        if self.eval_url is None:
            super().handle_infer_backend()


@dataclass
class ExportArguments(InferArguments):
    to_peft_format: bool = False
    # The parameter has been defined in InferArguments.
    # merge_lora: bool = False

    # awq: 4; gptq: 2, 3, 4, 8
    quant_bits: int = 0  # e.g. 4
    quant_method: Literal['awq', 'gptq'] = 'awq'
    quant_n_samples: int = 256
    quant_seqlen: int = 2048
    quant_device_map: str = 'cpu'  # e.g. 'cpu', 'auto'
    quant_output_dir: Optional[str] = None

    # push to ms hub
    push_to_hub: bool = False
    # 'user_name/repo_name' or 'repo_name'
    hub_model_id: Optional[str] = None
    # None: use env var `MODELSCOPE_API_TOKEN`
    hub_token: Optional[str] = field(
        default=None, metadata={'help': 'SDK token can be found in https://modelscope.cn/my/myaccesstoken'})
    hub_private_repo: bool = False
    commit_message: str = 'update files'

    def __post_init__(self):
        if self.merge_device_map is None:
            self.merge_device_map = 'cpu' if self.quant_bits != 0 else 'auto'
        super().__post_init__()
        if len(self.dataset) == 0:
            self.dataset = ['alpaca-zh#10000', 'alpaca-en#10000']
            logger.info(f'Setting args.dataset: {self.dataset}')
        if self.quant_output_dir is None:
            if self.ckpt_dir is None:
                self.quant_output_dir = f'{self.model_type}-{self.quant_method}-int{self.quant_bits}'
            else:
                ckpt_dir, ckpt_name = os.path.split(self.ckpt_dir)
                self.quant_output_dir = os.path.join(ckpt_dir, f'{ckpt_name}-{self.quant_method}-int{self.quant_bits}')
            logger.info(f'Setting args.quant_output_dir: {self.quant_output_dir}')
        assert not os.path.exists(self.quant_output_dir), f'args.quant_output_dir: {self.quant_output_dir}'


@dataclass
class DPOArguments(SftArguments):

    ref_model_type: Optional[str] = field(
        default=None, metadata={'help': f'model_type choices: {list(MODEL_MAPPING.keys())}'})

    ref_model_id_or_path: Optional[str] = None

    max_prompt_length: int = 1024
    beta: float = 0.1
    label_smoothing: float = 0.0
    loss_type: Literal['sigmoid', 'hinge', 'ipo', 'kto_pair'] = 'sigmoid'
    sft_beta: float = 0.1


@dataclass
class RomeArguments(InferArguments):
    rome_request_file: str = field(
        default=None, metadata={'help': 'The rome request file, please check the documentation '
                                'to get the format'})

    def __post_init__(self) -> None:
        self.handle_compatibility()
        self.handle_path()
        self.set_model_type()
        self.check_flash_attn()

        self.torch_dtype, _, _ = self.select_dtype()
        if self.template_type == 'AUTO':
            self.template_type = get_default_template_type(self.model_type)
            logger.info(f'Setting template_type: {self.template_type}')

        if self.max_length == -1:
            self.max_length = None


dtype_mapping_reversed = {v: k for k, v in dtype_mapping.items()}


def swift_to_peft_format(lora_checkpoint_path: str) -> str:
    if 'default' in os.listdir(lora_checkpoint_path):  # swift_backend
        new_lora_checkpoint_path = f'{lora_checkpoint_path}-peft'
        Swift.save_to_peft_format(lora_checkpoint_path, new_lora_checkpoint_path)
        lora_checkpoint_path = new_lora_checkpoint_path
        logger.info('Converting the swift format checkpoint to peft format, '
                    f"and saving it to: '{new_lora_checkpoint_path}'")
    else:
        logger.info('The format of the checkpoint is already in peft format.')
    return lora_checkpoint_path


def _parse_lora_modules(lora_modules: List[str], use_vllm: bool) -> List['LoRARequest']:
    VllmLoRARequest = None
    if use_vllm:
        try:
            from .vllm_utils import LoRARequest as VllmLoRARequest
        except ImportError:
            logger.warning('The current version of VLLM does not support `enable_lora`. Please upgrade VLLM.')
            raise

    @dataclass
    class PtLoRARequest:
        lora_name: str
        lora_int_id: int
        lora_local_path: str

    LoRARequest = VllmLoRARequest if use_vllm else PtLoRARequest
    lora_request_list = []
    for i, lora_module in enumerate(lora_modules):
        lora_name, lora_local_path = lora_module.split('=')
        lora_local_path = swift_to_peft_format(lora_local_path)
        lora_request_list.append(LoRARequest(lora_name, i + 1, lora_local_path))
    return lora_request_list
