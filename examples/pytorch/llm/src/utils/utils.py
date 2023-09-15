# Copyright (c) Alibaba, Inc. and its affiliates.
# Part of the implementation is borrowed from huggingface/transformers.
import heapq
import logging
import os
import shutil
from functools import wraps
from tempfile import TemporaryDirectory
from typing import (Any, Callable, Dict, List, Mapping, Optional, Sequence,
                    Tuple, Union)

import matplotlib.pyplot as plt
import numpy as np
import requests
import torch
import torch.distributed as dist
from accelerate.utils.modeling import (get_balanced_memory,
                                       infer_auto_device_map)
from datasets import Dataset as HfDataset
from modelscope import MsDataset
from modelscope.utils.config_ds import MS_CACHE_HOME
from modelscope.utils.logger import get_logger as get_ms_logger
from torch import device as Device
from torch import dtype as Dtype
from torch.nn import Linear, Module
from torch.nn.parallel import DistributedDataParallel as DDP
from tqdm.auto import tqdm
from transformers import GenerationConfig, TextStreamer, trainer

from swift import get_logger
from swift.hub import ModelScopeConfig
from swift.utils.tb_utils import (TB_COLOR, TB_COLOR_SMOOTH,
                                  read_tensorboard_file, tensorboard_smoothing)
from .callback import DefaultFlowCallbackNew, ProgressCallbackNew

logger = get_logger()
ms_logger = get_ms_logger()

os.environ['TOKENIZERS_PARALLELISM'] = 'true'

DTYPE_MAPPING = {
    'fp16': torch.float16,
    'bf16': torch.bfloat16,
    'fp32': torch.float32
}


def get_dist_setting() -> Tuple[int, int, int, int]:
    """return rank, local_rank, world_size"""
    rank = int(os.getenv('RANK', -1))
    local_rank = int(os.getenv('LOCAL_RANK', -1))
    world_size = int(os.getenv('WORLD_SIZE', 1))
    local_world_size = int(os.getenv('LOCAL_WORLD_SIZE', 1))
    return rank, local_rank, world_size, local_world_size


def is_master():
    rank = get_dist_setting()[0]
    return rank in {-1, 0}


def is_local_master():
    local_rank = get_dist_setting()[1]
    return local_rank in {-1, 0}


def is_dist():
    """Determine if the training is distributed"""
    rank, local_rank, _, _ = get_dist_setting()
    return rank >= 0 and local_rank >= 0


def show_layers(model: Module, max_lines: Optional[int] = 20) -> None:
    named_p = list(model.named_parameters())
    for i, (n, p) in enumerate(named_p):
        if max_lines is not None and i >= max_lines:
            logger.info('...')
            break
        logger.info(
            f'[{n}]: requires_grad={p.requires_grad}, dtype={p.dtype}, device={p.device}'
        )


def plot_images(images_dir: str,
                tb_dir: str,
                smooth_key: List[str],
                smooth_val: float = 0.9,
                figsize: Tuple[int, int] = (8, 5),
                dpi: int = 100) -> None:
    """Using tensorboard's data content to plot images"""
    os.makedirs(images_dir, exist_ok=True)
    fname = [
        fname for fname in os.listdir(tb_dir)
        if os.path.isfile(os.path.join(tb_dir, fname))
    ][0]
    tb_path = os.path.join(tb_dir, fname)
    data = read_tensorboard_file(tb_path)

    for k in data.keys():
        _data = data[k]
        steps = [d['step'] for d in _data]
        values = [d['value'] for d in _data]
        if len(values) == 0:
            continue
        _, ax = plt.subplots(1, 1, squeeze=True, figsize=figsize, dpi=dpi)
        ax.set_title(k)
        if len(values) == 1:
            ax.scatter(steps, values, color=TB_COLOR_SMOOTH)
        elif k in smooth_key:
            ax.plot(steps, values, color=TB_COLOR)
            values_s = tensorboard_smoothing(values, smooth_val)
            ax.plot(steps, values_s, color=TB_COLOR_SMOOTH)
        else:
            ax.plot(steps, values, color=TB_COLOR_SMOOTH)
        fpath = os.path.join(images_dir, k.replace('/', '_'))
        plt.savefig(fpath, dpi=dpi, bbox_inches='tight')


def inference(input_ids: List[int],
              model,
              tokenizer,
              streamer: Optional[TextStreamer] = None,
              generation_config: Optional[GenerationConfig] = None,
              skip_prompt: bool = True) -> str:
    if not skip_prompt:
        print(f'[INFERENCE]{tokenizer.decode(input_ids)}', end='')
    input_ids = torch.tensor(input_ids)[None].cuda()
    attention_mask = torch.ones_like(input_ids)
    model.eval()
    generate_ids = model.generate(
        input_ids=input_ids,
        attention_mask=attention_mask,
        streamer=streamer,
        generation_config=generation_config)
    output_text = tokenizer.decode(generate_ids[0])
    return output_text


def select_dtype(dtype: str) -> Tuple[Dtype, bool, bool]:
    """
    dtype: Literal['fp16', 'bf16', 'fp32']
    """
    torch_dtype = DTYPE_MAPPING[dtype]

    assert torch_dtype in {torch.float16, torch.bfloat16, torch.float32}
    if torch_dtype == torch.float16:
        fp16, bf16 = True, False
    elif torch_dtype == torch.bfloat16:
        support_bf16 = torch.cuda.is_bf16_supported()
        if not support_bf16:
            logger.warning(f'support_bf16: {support_bf16}')
        fp16, bf16 = False, True
    else:
        fp16, bf16 = False, False
    return torch_dtype, fp16, bf16


def select_bnb(quantization_bit: int,
               bnb_4bit_compute_dtype: str) -> Tuple[Dtype, bool, bool]:
    bnb_4bit_compute_dtype = DTYPE_MAPPING[bnb_4bit_compute_dtype]
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


def broadcast_string(string: Optional[str], buffer_size: int = 200) -> str:
    """String broadcasting in case of DDP
    string: main rank: str
        other rank: None
    return: all rank: str
    """
    assert dist.is_initialized()
    rank, local_rank, _, _ = get_dist_setting()
    assert rank >= 0
    if rank == 0:
        assert string is not None
        tensor = torch.tensor(
            [ord(c) for c in string] + [0] * (buffer_size - len(string)),
            dtype=torch.int64,
            device=local_rank)
    else:
        tensor = torch.zeros(buffer_size, dtype=torch.int64, device=local_rank)
    dist.broadcast(tensor, 0)
    first_zero = (tensor == 0).nonzero()[0].item()
    res = tensor.tolist()[:first_zero]
    return ''.join([chr(x) for x in res])


def find_all_linear_for_lora(model: Module,
                             quantization_bit: int,
                             model_type: Optional[str] = None) -> List[str]:
    """ref: https://github.com/artidoro/qlora"""
    head_module_name = 'lm_head'
    if model_type.startswith('chatglm2-6b'):
        head_module_name = 'output_layer'
    if quantization_bit == 4:
        from bitsandbytes.nn import Linear4bit
        linear_cls = Linear4bit
    elif quantization_bit == 8:
        from bitsandbytes.nn import Linear8bitLt
        linear_cls = Linear8bitLt
    else:
        linear_cls = Linear
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, linear_cls):
            module_name = name.split('.')[-1]
            if head_module_name not in module_name:
                lora_module_names.add(module_name)
    return list(lora_module_names)


def download_dataset(model_id: str,
                     files: List[str],
                     force_download: bool = False) -> str:
    url = f'http://www.modelscope.cn/api/v1/datasets/{model_id}/repo?Revision=master&FilePath={{fpath}}'
    cache_dir = os.path.join(MS_CACHE_HOME, 'datasets', model_id, 'master')
    local_dir = os.path.join(cache_dir, 'raw')
    tmp_dir = os.path.join(cache_dir, 'tmp')
    os.makedirs(local_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    cookies = ModelScopeConfig.get_cookies()
    with TemporaryDirectory(dir=tmp_dir) as temp_dir:
        for remote_fpath in files:
            url = url.format(fpath=remote_fpath)
            temp_fpath = os.path.join(temp_dir, remote_fpath)
            local_fpath = os.path.join(local_dir, remote_fpath)
            if not force_download and os.path.exists(local_fpath):
                continue
            download_files(url, temp_fpath, cookies)
            shutil.copy2(temp_fpath, local_fpath)

    return local_dir


def download_files(url: str, local_path: str, cookies) -> None:
    resp = requests.get(url, cookies=cookies, stream=True)
    with open(local_path, 'wb') as f:
        for data in tqdm(resp.iter_lines()):
            f.write(data)


def sort_by_max_length(dataset: HfDataset, num_dataset: int) -> HfDataset:
    dataset_len = [len(d['input_ids']) for d in tqdm(dataset)]
    idx = heapq.nlargest(
        num_dataset, range(len(dataset_len)), key=lambda i: dataset_len[i])
    input_ids = []
    labels = []
    for i in tqdm(idx):
        input_ids.append(dataset[i]['input_ids'])
        labels.append(dataset[i]['labels'])
    return HfDataset.from_dict({'input_ids': input_ids, 'labels': labels})


def check_json_format(obj: Any) -> Any:
    if obj is None or isinstance(
            obj, (int, float, str, complex)):  # bool is a subclass of int
        return obj

    if isinstance(obj, Sequence):
        res = []
        for x in obj:
            res.append(check_json_format(x))
    elif isinstance(obj, Mapping):
        res = {}
        for k, v in obj.items():
            res[k] = check_json_format(v)
    else:
        res = repr(obj)  # e.g. function
    return res


_old_msdataset_load = MsDataset.load


@wraps(_old_msdataset_load)
def _msdataset_ddp_load(*args, **kwargs):
    if is_dist() and not is_local_master():
        dist.barrier()
    dataset = _old_msdataset_load(*args, **kwargs)
    if is_dist() and is_local_master():
        dist.barrier()

    if is_dist():
        dist.barrier()
    return dataset


def is_ddp_plus_mp() -> bool:
    if not is_dist():
        return False
    n_gpu = torch.cuda.device_count()
    local_world_size = get_dist_setting()[3]
    assert n_gpu % local_world_size == 0
    if n_gpu // local_world_size >= 2:
        logger.info('Using DDP + MP(device_map)')
        return True
    return False


def _get_max_memory(device_ids: List[int]) -> Dict[Union[int, str], int]:
    """add feat in accelerate to support DDP + MP"""
    import psutil
    # Make sure CUDA is initialized on each GPU to have the right memory info.
    for i in device_ids:
        _ = torch.tensor([0], device=i)

    device_ids_set = set(device_ids)
    max_memory = {}
    for i in range(torch.cuda.device_count()):
        max_memory[i] = 0
        if i in device_ids_set:
            max_memory[i] = torch.cuda.mem_get_info(i)[0]
    max_memory['cpu'] = psutil.virtual_memory().available
    return max_memory


def _sync_max_memory(
        max_memory: Dict[Union[int, str], int]) -> Dict[Union[int, str], int]:
    """Make sure that the model structure of MP(device_map) is the same, when using DDP."""
    max_memory_list = [
        v for k, v in max_memory.items() if (v > 0 and k != 'cpu')
    ]
    _, local_rank, world_size, _ = get_dist_setting()
    src_tensor = torch.tensor(max_memory_list).to(local_rank)
    tgt_tensor_list = [torch.zeros_like(src_tensor) for _ in range(world_size)]
    dist.all_gather(tgt_tensor_list, src_tensor)
    tgt_tensor = torch.stack(tgt_tensor_list, dim=0)
    new_max_memory_iter = iter(tgt_tensor.min(dim=0)[0].tolist())
    new_max_memory = {}
    for k, v in max_memory.items():
        new_max_memory[k] = v
        if v > 0 and k != 'cpu':
            new_max_memory[k] = next(new_max_memory_iter)
    return new_max_memory


@wraps(infer_auto_device_map)
def _infer_auto_device_map_patch(
        model: Module,
        max_memory: Optional[Dict[Union[int, str], Union[int, str]]] = None,
        **kwargs) -> Dict[str, Union[int, str, Device]]:
    """The auxiliary function for supports DDP+MP. Monkey Patching.
    add feat in accelerate to support DDP + MP"""
    verbose = kwargs.pop('verbose', False)
    n_gpu = torch.cuda.device_count()
    _, local_rank, _, local_world_size = get_dist_setting()
    device_ids = list(range(local_rank, n_gpu, local_world_size))
    max_memory = _get_max_memory(device_ids)
    max_memory = _sync_max_memory(max_memory)
    max_memory = get_balanced_memory(
        model, max_memory, low_zero=False, **kwargs)
    max_memory = {k: v for k, v in max_memory.items() if v > 0}
    return infer_auto_device_map(model, max_memory, verbose=verbose, **kwargs)


def dataset_map(
    dataset: HfDataset, preprocess_func: Callable[[Dict[str, Any]],
                                                  Dict[str,
                                                       Optional[List[int]]]]
) -> HfDataset:
    # faster than dataset.map
    input_ids = []
    labels = []
    for d in tqdm(dataset):
        d = preprocess_func(d)
        if d['input_ids'] is None:
            continue
        input_ids.append(d['input_ids'])
        labels.append(d['labels'])
    return HfDataset.from_dict({'input_ids': input_ids, 'labels': labels})


logger_format = logging.Formatter('[%(levelname)s:%(name)s] %(message)s')

logger.handlers[0].setFormatter(logger_format)
ms_logger.handlers[0].setFormatter(logger_format)
if is_master():
    logger.setLevel(logging.INFO)
    ms_logger.setLevel(logging.INFO)
else:
    logger.setLevel(logging.ERROR)
    ms_logger.setLevel(logging.ERROR)

# monkey patching
trainer.DEFAULT_PROGRESS_CALLBACK = ProgressCallbackNew
trainer.DEFAULT_CALLBACKS = [DefaultFlowCallbackNew]
MsDataset.load = _msdataset_ddp_load
if is_ddp_plus_mp():
    import transformers
    import accelerate
    _old_ddp_init = DDP.__init__
    accelerate.accelerator.torch.nn.parallel.DistributedDataParallel.__init__ = (
        lambda self, model, device_ids, output_device, *args, **kwargs:
        _old_ddp_init(self, model, *args, **kwargs))
    transformers.modeling_utils.get_balanced_memory = lambda *args, **kwargs: None
    transformers.modeling_utils.infer_auto_device_map = _infer_auto_device_map_patch
    _old_accelerator_init = trainer.Accelerator.__init__
    trainer.Accelerator.__init__ = (
        lambda self, device_placement=False, *args, **kwargs:
        _old_accelerator_init(
            self, device_placement=device_placement, *args, **kwargs))
    trainer.Accelerator.verify_device_map = lambda *args, **kwargs: False
