# Copyright (c) Alibaba, Inc. and its affiliates.
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

import json
import torch
import torch.distributed as dist
from torch import dtype as Dtype
from transformers.utils.versions import require_version

from swift import get_logger
from swift.hub import HubApi, ModelScopeConfig
from swift.utils import (add_version_to_work_dir, broadcast_string,
                         get_dist_setting, is_dist, is_master)
from .dataset import (DATASET_MAPPING, DatasetName, get_custom_dataset,
                      register_dataset)
from .model import MODEL_MAPPING, ModelType, dtype_mapping
from .template import TEMPLATE_MAPPING, TemplateType

logger = get_logger()


@dataclass
class SftArguments:
    # You can specify the model by either using the model_type or model_id_or_path.
    model_type: Optional[str] = field(
        default=None,
        metadata={'help': f'model_type choices: {list(MODEL_MAPPING.keys())}'})
    model_id_or_path: Optional[str] = None
    model_revision: Optional[str] = None
    model_cache_dir: Optional[str] = None

    sft_type: str = field(
        default='lora',
        metadata={'choices': ['lora', 'longlora', 'qalora', 'full']})
    tuner_backend: str = field(
        default='swift', metadata={'choices': ['swift', 'peft']})
    template_type: Optional[str] = field(
        default=None,
        metadata={
            'help': f'template_type choices: {list(TEMPLATE_MAPPING.keys())}'
        })
    output_dir: str = 'output'
    add_output_dir_suffix: bool = True
    ddp_backend: str = field(
        default='nccl', metadata={'choices': ['nccl', 'gloo', 'mpi', 'ccl']})

    seed: int = 42
    resume_from_checkpoint: Optional[str] = None
    dtype: Optional[str] = field(
        default=None, metadata={'choices': ['bf16', 'fp16', 'fp32']})

    dataset: Optional[List[str]] = field(
        default=None,
        metadata={'help': f'dataset choices: {list(DATASET_MAPPING.keys())}'})
    dataset_seed: int = 42
    dataset_test_ratio: float = 0.01
    train_dataset_sample: int = 20000  # -1: all dataset
    system: str = 'you are a helpful assistant!'
    max_length: int = 2048  # -1: no limit
    check_dataset_strategy: str = field(
        default='none',
        metadata={'choices': ['none', 'discard', 'error', 'warning']})
    custom_train_dataset_path: Optional[List[str]] = None
    custom_val_dataset_path: Optional[List[str]] = None

    # If you want to use qlora, set the quantization_bit to 8 or 4.
    # And you need to install bitsandbytes: `pip install bitsandbytes -U`
    # note: bf16 and quantization have requirements for gpu architecture
    quantization_bit: int = field(default=0, metadata={'choices': [0, 4, 8]})
    bnb_4bit_comp_dtype: str = field(
        default=None, metadata={'choices': ['fp16', 'bf16', 'fp32']})
    bnb_4bit_quant_type: str = field(
        default='nf4', metadata={'choices': ['fp4', 'nf4']})
    bnb_4bit_use_double_quant: bool = True

    lora_target_modules: Optional[List[str]] = None
    lora_rank: int = 8
    lora_alpha: int = 32
    lora_dropout_p: float = 0.05

    gradient_checkpointing: bool = True
    deepspeed_config_path: Optional[str] = None  # e.g. 'ds_config/zero2.json'
    batch_size: int = 1
    eval_batch_size: Optional[int] = None
    num_train_epochs: int = 1
    # if max_steps >= 0, override num_train_epochs
    max_steps: int = -1
    optim: str = 'adamw_torch'
    learning_rate: Optional[float] = None
    weight_decay: float = 0.01
    gradient_accumulation_steps: int = 16
    max_grad_norm: float = 1.
    predict_with_generate: bool = False
    lr_scheduler_type: str = 'cosine'
    warmup_ratio: float = 0.05

    eval_steps: int = 50
    save_steps: Optional[int] = None
    only_save_model: Optional[bool] = None
    save_total_limit: int = 2  # save last and best. -1: all checkpoints
    logging_steps: int = 5
    dataloader_num_workers: int = 1

    push_to_hub: bool = False
    # 'user_name/repo_name' or 'repo_name'
    hub_model_id: Optional[str] = None
    hub_private_repo: bool = True
    push_hub_strategy: str = field(
        default='push_best',
        metadata={
            'choices':
            ['end', 'push_best', 'push_last', 'checkpoint', 'all_checkpoints']
        })
    # None: use env var `MODELSCOPE_API_TOKEN`
    hub_token: Optional[str] = field(
        default=None,
        metadata={
            'help':
            'SDK token can be found in https://modelscope.cn/my/myaccesstoken'
        })

    # other
    test_oom_error: bool = field(
        default=False,
        metadata={
            'help':
            'If set to True, the train_dataset will be sorted in descending order based on max_length, '
            'enabling faster detection of OOM (Out of Memory) errors.'
        })
    use_flash_attn: Optional[bool] = None
    ignore_args_error: bool = False  # True: notebook compatibility
    logging_dir: Optional[str] = None
    report_to: Optional[List[str]] = None

    # generation config
    max_new_tokens: int = 2048
    do_sample: bool = True
    temperature: float = 0.9
    top_k: int = 20
    top_p: float = 0.9
    repetition_penalty: float = 1.05

    def __post_init__(self) -> None:
        handle_compatibility(self)
        set_model_type(self)
        register_custom_dataset(self)
        handle_path(self)
        if self.add_output_dir_suffix:
            self.output_dir = os.path.join(self.output_dir, self.model_type)
            if is_master():
                self.output_dir = add_version_to_work_dir(self.output_dir)
                logger.info(f'output_dir: {self.output_dir}')

        self.torch_dtype, self.fp16, self.bf16 = select_dtype(self)
        if is_dist():
            rank, local_rank, _, _ = get_dist_setting()
            torch.cuda.set_device(local_rank)
            self.seed += rank  # Avoid the same dropout
            if self.ddp_backend == 'gloo' and self.quantization_bit != 0:
                raise ValueError('not supported, please use `nccl`')

            # Initialize in advance
            dist.init_process_group(backend=self.ddp_backend)
            # Make sure to set the same output_dir when using DDP.
            self.output_dir = broadcast_string(self.output_dir)

        if self.sft_type in ('lora', 'longlora', 'qalora'):
            if self.learning_rate is None:
                self.learning_rate = 1e-4
            if self.only_save_model is None:
                if self.deepspeed_config_path is None:
                    self.only_save_model = False
                else:
                    self.only_save_model = True
        elif self.sft_type == 'full':
            assert self.quantization_bit == 0, 'not supported'
            assert self.dtype != 'fp16', 'please use bf16 or fp32'
            if self.learning_rate is None:
                self.learning_rate = 2e-5
            if self.only_save_model is None:
                self.only_save_model = True
        else:
            raise ValueError(f'sft_type: {self.sft_type}')

        if self.template_type is None:
            self.template_type = MODEL_MAPPING[self.model_type]['template']
            logger.info(f'Setting template_type: {self.template_type}')
        if self.dataset is None:
            self.dataset = [DatasetName.blossom_math_zh]
        assert isinstance(self.dataset, (list, tuple))

        if self.save_steps is None:
            self.save_steps = self.eval_steps
        if self.lora_target_modules is None:
            self.lora_target_modules = ['AUTO']
        if 'AUTO' in self.lora_target_modules:
            assert len(self.lora_target_modules) == 1
            self.lora_target_modules = MODEL_MAPPING[
                self.model_type]['lora_target_modules']
        self.bnb_4bit_compute_dtype, self.load_in_4bit, self.load_in_8bit = select_bnb(
            self)

        prepare_push_ms_hub(self)
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

        self.deepspeed = None
        if self.deepspeed_config_path is not None:
            with open(self.deepspeed_config_path, 'r') as f:
                self.deepspeed = json.load(f)
            logger.info(f'Using deepspeed: {self.deepspeed}')
        if self.logging_dir is None:
            self.logging_dir = f'{self.output_dir}/runs'
        if self.report_to is None:
            self.report_to == ['all']


@dataclass
class InferArguments:
    # You can specify the model by either using the model_type or model_id_or_path.
    model_type: Optional[str] = field(
        default=None,
        metadata={'help': f'model_type choices: {list(MODEL_MAPPING.keys())}'})
    model_id_or_path: Optional[str] = None
    model_revision: Optional[str] = None

    sft_type: str = field(
        default='lora',
        metadata={'choices': ['lora', 'longlora', 'qalora', 'full']})
    template_type: Optional[str] = field(
        default=None,
        metadata={
            'help': f'template_type choices: {list(TEMPLATE_MAPPING.keys())}'
        })
    ckpt_dir: Optional[str] = field(
        default=None, metadata={'help': '/path/to/your/vx_xxx/checkpoint-xxx'})
    load_args_from_ckpt_dir: bool = True
    eval_human: bool = False  # False: eval val_dataset

    seed: int = 42
    dtype: Optional[str] = field(
        default=None, metadata={'choices': ['bf16', 'fp16', 'fp32']})

    dataset: Optional[List[str]] = field(
        default=None,
        metadata={'help': f'dataset choices: {list(DATASET_MAPPING.keys())}'})
    dataset_seed: int = 42
    dataset_test_ratio: float = 0.01
    show_dataset_sample: int = 10
    system: str = 'you are a helpful assistant!'
    max_length: int = 2048  # -1: no limit
    check_dataset_strategy: str = field(
        default='none',
        metadata={'choices': ['none', 'discard', 'error', 'warning']})
    custom_train_dataset_path: Optional[List[str]] = None
    custom_val_dataset_path: Optional[List[str]] = None

    quantization_bit: int = field(default=0, metadata={'choices': [0, 4, 8]})
    bnb_4bit_comp_dtype: str = field(
        default=None, metadata={'choices': ['fp16', 'bf16', 'fp32']})
    bnb_4bit_quant_type: str = field(
        default='nf4', metadata={'choices': ['fp4', 'nf4']})
    bnb_4bit_use_double_quant: bool = True

    max_new_tokens: int = 2048
    do_sample: bool = True
    temperature: float = 0.9
    top_k: int = 20
    top_p: float = 0.9
    repetition_penalty: float = 1.05

    # other
    use_flash_attn: Optional[bool] = None
    ignore_args_error: bool = False  # True: notebook compatibility
    stream: bool = True
    merge_lora_and_save: bool = False
    overwrite_generation_config: bool = False

    def __post_init__(self) -> None:
        handle_compatibility(self)
        if self.ckpt_dir is not None and not os.path.isdir(self.ckpt_dir):
            raise ValueError(f'Please enter a valid ckpt_dir: {self.ckpt_dir}')
        logger.info(f'ckpt_dir: {self.ckpt_dir}')
        if self.ckpt_dir is None and self.load_args_from_ckpt_dir:
            self.load_args_from_ckpt_dir = False
            logger.info(
                'Due to `ckpt_dir` being `None`, `load_args_from_ckpt_dir` is set to `False`.'
            )
        if self.load_args_from_ckpt_dir:
            load_from_ckpt_dir(self)
        set_model_type(self)
        register_custom_dataset(self)
        handle_path(self)

        self.torch_dtype, _, _ = select_dtype(self)
        if self.template_type is None:
            self.template_type = MODEL_MAPPING[self.model_type]['template']
            logger.info(f'Setting template_type: {self.template_type}')
        if self.dataset is None:
            self.dataset = [DatasetName.blossom_math_zh]
        assert isinstance(self.dataset, (list, tuple))

        self.bnb_4bit_compute_dtype, self.load_in_4bit, self.load_in_8bit = select_bnb(
            self)

        if self.max_length == -1:
            self.max_length = None
        if self.ckpt_dir is None and self.overwrite_generation_config:
            self.overwrite_generation_config = False
            logger.warning('Setting overwrite_generation_config: False')


@dataclass
class RomeArguments(InferArguments):
    rome_request_file: str = field(
        default=None,
        metadata={
            'help':
            'The rome request file, please check the documentation '
            'to get the format'
        })

    def __post_init__(self) -> None:
        handle_compatibility(self)
        set_model_type(self)
        handle_path(self)

        self.torch_dtype, _, _ = select_dtype(self)
        if self.template_type is None:
            self.template_type = MODEL_MAPPING[self.model_type]['template']
            logger.info(f'Setting template_type: {self.template_type}')

        if self.max_length == -1:
            self.max_length = None


dtype_mapping_reversed = {v: k for k, v in dtype_mapping.items()}


def select_dtype(
        args: Union[SftArguments, InferArguments]) -> Tuple[Dtype, bool, bool]:
    if args.dtype is None and not torch.cuda.is_bf16_supported():
        args.dtype = 'fp16'
    if args.dtype is None and (args.model_type.endswith('int4')
                               or args.model_type.endswith('int8')):
        model_torch_dtype = MODEL_MAPPING[args.model_type]['torch_dtype']
        if model_torch_dtype is not None:
            args.dtype = dtype_mapping[model_torch_dtype]
    if args.dtype is None:
        args.dtype = 'bf16'

    torch_dtype = dtype_mapping_reversed[args.dtype]

    assert torch_dtype in {torch.float16, torch.bfloat16, torch.float32}
    if torch_dtype == torch.float16:
        if isinstance(args, SftArguments) and args.sft_type == 'full':
            torch_dtype = torch.float32
            logger.warning('Setting torch_dtype: torch.float32')
        fp16, bf16 = True, False
    elif torch_dtype == torch.bfloat16:
        support_bf16 = torch.cuda.is_bf16_supported()
        if not support_bf16:
            logger.warning(f'support_bf16: {support_bf16}')
        fp16, bf16 = False, True
    else:
        fp16, bf16 = False, False
    return torch_dtype, fp16, bf16


def select_bnb(
        args: Union[SftArguments, InferArguments]) -> Tuple[Dtype, bool, bool]:
    if args.bnb_4bit_comp_dtype is None:
        args.bnb_4bit_comp_dtype = args.dtype

    quantization_bit = args.quantization_bit
    bnb_4bit_compute_dtype = dtype_mapping_reversed[args.bnb_4bit_comp_dtype]
    assert bnb_4bit_compute_dtype in {
        torch.float16, torch.bfloat16, torch.float32
    }
    if quantization_bit == 4:
        load_in_4bit, load_in_8bit = True, False
    elif quantization_bit == 8:
        load_in_4bit, load_in_8bit = False, True
    else:
        load_in_4bit, load_in_8bit = False, False

    return bnb_4bit_compute_dtype, load_in_4bit, load_in_8bit


def handle_compatibility(args: Union[SftArguments, InferArguments]) -> None:
    if args.dataset is not None and len(
            args.dataset) == 1 and ',' in args.dataset[0]:
        args.dataset = args.dataset[0].split(',')
    if args.template_type == 'chatglm2-generation':
        args.template_type = 'chatglm-generation'
    if args.template_type == 'qwen':
        args.template_type = TemplateType.chatml


def set_model_type(args: Union[SftArguments, InferArguments]) -> None:
    assert args.model_type is None or args.model_id_or_path is None
    if args.model_id_or_path is not None:
        model_mapping_reversed = {
            v['model_id_or_path'].lower(): k
            for k, v in MODEL_MAPPING.items()
        }
        model_id_or_path = args.model_id_or_path
        model_id_or_path_lower = model_id_or_path.lower()
        if model_id_or_path_lower not in model_mapping_reversed:
            raise ValueError(f'{model_id_or_path} not in MODEL_MAPPING')
        args.model_type = model_mapping_reversed[model_id_or_path_lower]

    if args.model_type is None:
        args.model_type = ModelType.qwen_7b_chat
    model_info = MODEL_MAPPING[args.model_type]
    if args.model_revision is None:
        args.model_revision = model_info['revision']
    else:
        model_info['revision'] = args.model_revision
    args.model_id_or_path = model_info['model_id_or_path']
    requires = model_info['requires']
    for require in requires:
        require_version(require)


def prepare_push_ms_hub(args: SftArguments) -> None:
    if args.hub_model_id is None:
        args.hub_model_id = f'{args.model_type}-{args.sft_type}'
        logger.info(f'Setting hub_model_id: {args.hub_model_id}')
    if args.push_to_hub:
        api = HubApi()
        if args.hub_token is None:
            args.hub_token = os.environ.get('MODELSCOPE_API_TOKEN')
        if args.hub_token is not None:
            api.login(args.hub_token)
        else:
            assert ModelScopeConfig.get_token(
            ) is not None, 'Please enter hub_token'
        logger.info('hub login successful!')


def handle_path(args: Union[SftArguments, InferArguments]) -> None:
    for k in [
            'model_cache_dir', 'output_dir', 'ckpt_dir',
            'resume_from_checkpoint', 'deepspeed_config_path', 'logging_dir'
    ]:
        value = getattr(args, k, None)
        if isinstance(value, str):
            value = os.path.expanduser(value)
            value = os.path.abspath(value)
            setattr(args, k, value)


def register_custom_dataset(args: Union[SftArguments, InferArguments]) -> None:
    if args.custom_train_dataset_path is None:
        assert args.custom_val_dataset_path is None
        return
    register_dataset(
        '_custom_dataset',
        '_custom_dataset',
        args.custom_train_dataset_path,
        args.custom_val_dataset_path,
        get_function=get_custom_dataset)
    if args.dataset is None:
        args.dataset = ['_custom_dataset']
    else:
        args.dataset.append('_custom_dataset')


def load_from_ckpt_dir(args: InferArguments) -> None:
    sft_args_path = os.path.join(args.ckpt_dir, 'sft_args.json')
    if not os.path.exists(sft_args_path):
        logger.info(f'{sft_args_path} not found')
        return
    with open(sft_args_path, 'r') as f:
        sft_args = json.load(f)
    imported_keys = [
        'model_id_or_path', 'model_revision', 'sft_type', 'template_type',
        'dtype', 'system', 'quantization_bit', 'bnb_4bit_comp_dtype',
        'bnb_4bit_quant_type', 'bnb_4bit_use_double_quant'
    ]
    if not args.eval_human:
        imported_keys += [
            'dataset', 'dataset_seed', 'dataset_test_ratio',
            'check_dataset_strategy', 'custom_train_dataset_path',
            'custom_val_dataset_path'
        ]
    for key in imported_keys:
        setattr(args, key, sft_args.get(key))
