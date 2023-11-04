# Copyright (c) Alibaba, Inc. and its affiliates.
import os
from functools import partial
from types import MethodType
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, Type

import torch
import torch.distributed as dist
import torch.nn.functional as F
from modelscope import (AutoConfig, AutoModelForCausalLM, AutoTokenizer,
                        BitsAndBytesConfig, GenerationConfig, GPTQConfig,
                        Model, read_config, snapshot_download)
from torch import Tensor
from torch import dtype as Dtype
from transformers import (PretrainedConfig, PreTrainedModel,
                          PreTrainedTokenizerBase)
from transformers.models.auto.auto_factory import _BaseAutoModelClass
from transformers.utils.versions import require_version

from swift import get_logger
from swift.utils import is_dist, is_local_master
from .template import TemplateType

logger = get_logger()

# Model Home: 'https://modelscope.cn/models/{model_id_or_path}/summary'
MODEL_MAPPING: Dict[str, Dict[str, Any]] = {}


class ModelType:
    # qwen
    qwen_7b = 'qwen-7b'
    qwen_7b_chat = 'qwen-7b-chat'
    qwen_14b = 'qwen-14b'
    qwen_14b_chat = 'qwen-14b-chat'
    qwen_7b_chat_int4 = 'qwen-7b-chat-int4'
    qwen_14b_chat_int4 = 'qwen-14b-chat-int4'
    qwen_7b_chat_int8 = 'qwen-7b-chat-int8'
    qwen_14b_chat_int8 = 'qwen-14b-chat-int8'
    # qwen-vl
    qwen_vl = 'qwen-vl'
    qwen_vl_chat = 'qwen-vl-chat'
    qwen_vl_chat_int4 = 'qwen-vl-chat-int4'
    # baichuan
    baichuan_7b = 'baichuan-7b'
    baichuan_13b = 'baichuan-13b'
    baichuan_13b_chat = 'baichuan-13b-chat'
    baichuan2_7b = 'baichuan2-7b'
    baichuan2_7b_chat = 'baichuan2-7b-chat'
    baichuan2_13b = 'baichuan2-13b'
    baichuan2_13b_chat = 'baichuan2-13b-chat'
    baichuan2_7b_chat_int4 = 'baichuan2-7b-chat-int4'
    baichuan2_13b_chat_int4 = 'baichuan2-13b-chat-int4'
    # chatglm
    chatglm2_6b = 'chatglm2-6b'
    chatglm2_6b_32k = 'chatglm2-6b-32k'
    chatglm3_6b_base = 'chatglm3-6b-base'
    chatglm3_6b = 'chatglm3-6b'
    chatglm3_6b_32k = 'chatglm3-6b-32k'
    # llama2
    llama2_7b = 'llama2-7b'
    llama2_13b = 'llama2-13b'
    llama2_70b = 'llama2-70b'
    llama2_7b_chat = 'llama2-7b-chat'
    llama2_13b_chat = 'llama2-13b-chat'
    llama2_70b_chat = 'llama2-70b-chat'
    # openbuddy
    openbuddy_llama2_13b_chat = 'openbuddy-llama2-13b-chat'
    openbuddy_llama2_65b_chat = 'openbuddy-llama-65b-chat'
    openbuddy_llama2_70b_chat = 'openbuddy-llama2-70b-chat'
    openbuddy_mistral_7b_chat = 'openbuddy-mistral-7b-chat'
    # internlm
    internlm_7b = 'internlm-7b'
    internlm_7b_chat = 'internlm-7b-chat'
    internlm_7b_chat_8k = 'internlm-7b-chat-8k'
    internlm_20b = 'internlm-20b'
    internlm_20b_chat = 'internlm-20b-chat'
    # xverse
    xverse_7b = 'xverse-7b'
    xverse_7b_chat = 'xverse-7b-chat'
    xverse_13b = 'xverse-13b'
    xverse_13b_chat = 'xverse-13b-chat'
    # mistral
    mistral_7b = 'mistral-7b'
    mistral_7b_chat = 'mistral-7b-chat'
    # ziya
    ziya2_13b = 'ziya2-13b'
    ziya2_13b_chat = 'ziya2-13b-chat'
    # skywork
    skywork_13b_chat = 'skywork-13b-chat'
    skywork_13b = 'skywork-13b'
    # other
    polylm_13b = 'polylm-13b'
    seqgpt_560m = 'seqgpt-560m'
    yi_6b = 'yi-6b'
    yi_34b = 'yi-34b'


class LoRATM(NamedTuple):
    # default lora target modules. qkv
    baichuan = ['W_pack']
    chatglm = ['query_key_value']
    llama2 = ['q_proj', 'k_proj', 'v_proj']
    qwen = ['c_attn']
    polylm = ['c_attn']
    bloom = ['query_key_value']
    internlm = ['q_proj', 'k_proj', 'v_proj']
    xverse = ['q_proj', 'k_proj', 'v_proj']
    mistral = ['q_proj', 'k_proj', 'v_proj']
    ziya = ['q_proj', 'k_proj', 'v_proj']
    yi = ['q_proj', 'k_proj', 'v_proj']


GetModelTokenizerFunction = Callable[..., Tuple[Optional[PreTrainedModel],
                                                PreTrainedTokenizerBase]]


def register_model(
    model_type: str,
    model_id_or_path: str,
    lora_target_modules: Optional[List[str]] = None,
    template: str = TemplateType.default,
    get_function: Optional[GetModelTokenizerFunction] = None,
    *,
    requires: Optional[List[str]] = None,
    torch_dtype: Optional[Dtype] = None,
    automodel_class: Type[_BaseAutoModelClass] = AutoModelForCausalLM,
    revision: str = 'master',
    ignore_file_pattern: Optional[List[str]] = None,
    max_length: Optional[int] = None,
    function_kwargs: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Optional[Callable[[GetModelTokenizerFunction],
                       GetModelTokenizerFunction]]:
    if model_type in MODEL_MAPPING:
        raise ValueError(
            f'The `{model_type}` has already been registered in the MODEL_MAPPING.'
        )
    if requires is None:
        requires = []
    if function_kwargs is None:
        function_kwargs = {}
    model_info = {
        'model_id_or_path': model_id_or_path,
        'lora_target_modules': lora_target_modules,
        'template': template,
        'requires': requires,
        'torch_dtype': torch_dtype,
        'automodel_class': automodel_class,
        'ignore_file_pattern': ignore_file_pattern,
        'revision': revision,
        'max_length': max_length,
        **kwargs
    }

    if get_function is not None:
        if len(function_kwargs) > 0:
            get_function = partial(get_function, **function_kwargs)
        model_info['get_function'] = get_function
        MODEL_MAPPING[model_type] = model_info
        return

    def _register_model(
            get_function: GetModelTokenizerFunction
    ) -> GetModelTokenizerFunction:
        if len(function_kwargs) > 0:
            get_function = partial(get_function, **function_kwargs)
        model_info['get_function'] = get_function
        MODEL_MAPPING[model_type] = model_info
        return get_function

    return _register_model


@register_model(ModelType.yi_34b, '01ai/Yi-34B', LoRATM.yi,
                TemplateType.default_generation)
@register_model(ModelType.yi_6b, '01ai/Yi-6B', LoRATM.yi,
                TemplateType.default_generation)
@register_model(ModelType.seqgpt_560m, 'damo/nlp_seqgpt-560m', LoRATM.bloom,
                TemplateType.default_generation)
@register_model(ModelType.ziya2_13b_chat, 'Fengshenbang/Ziya2-13B-Chat',
                LoRATM.ziya, TemplateType.ziya)
@register_model(ModelType.ziya2_13b, 'Fengshenbang/Ziya2-13B-Base',
                LoRATM.ziya, TemplateType.default_generation)
@register_model(
    ModelType.mistral_7b_chat,
    'AI-ModelScope/Mistral-7B-Instruct-v0.1',
    LoRATM.mistral,
    TemplateType.llama,
    requires=['transformers>=4.34'])
@register_model(
    ModelType.mistral_7b,
    'AI-ModelScope/Mistral-7B-v0.1',
    LoRATM.mistral,
    TemplateType.default_generation,
    requires=['transformers>=4.34'])
@register_model(ModelType.xverse_13b_chat, 'xverse/XVERSE-13B-Chat',
                LoRATM.xverse, TemplateType.xverse)
@register_model(ModelType.xverse_13b, 'xverse/XVERSE-13B', LoRATM.xverse,
                TemplateType.default_generation)
@register_model(ModelType.xverse_7b_chat, 'xverse/XVERSE-7B-Chat',
                LoRATM.xverse, TemplateType.xverse)
@register_model(ModelType.xverse_7b, 'xverse/XVERSE-7B', LoRATM.xverse,
                TemplateType.default_generation)
@register_model(ModelType.internlm_20b_chat,
                'Shanghai_AI_Laboratory/internlm-chat-20b', LoRATM.internlm,
                TemplateType.internlm)
@register_model(ModelType.internlm_20b, 'Shanghai_AI_Laboratory/internlm-20b',
                LoRATM.internlm, TemplateType.default_generation)
@register_model(ModelType.internlm_7b_chat_8k,
                'Shanghai_AI_Laboratory/internlm-chat-7b-8k', LoRATM.internlm,
                TemplateType.internlm)
@register_model(ModelType.internlm_7b_chat,
                'Shanghai_AI_Laboratory/internlm-chat-7b-v1_1',
                LoRATM.internlm, TemplateType.internlm)
@register_model(ModelType.internlm_7b, 'Shanghai_AI_Laboratory/internlm-7b',
                LoRATM.internlm, TemplateType.default_generation)
@register_model(
    ModelType.openbuddy_mistral_7b_chat,
    'OpenBuddy/openbuddy-mistral-7b-v13.1',
    LoRATM.mistral,
    TemplateType.openbuddy,
    requires=['transformers>=4.34'])
@register_model(ModelType.openbuddy_llama2_70b_chat,
                'OpenBuddy/openbuddy-llama2-70b-v10.1-bf16', LoRATM.llama2,
                TemplateType.openbuddy)
@register_model(ModelType.openbuddy_llama2_65b_chat,
                'OpenBuddy/openbuddy-llama-65b-v8-bf16', LoRATM.llama2,
                TemplateType.openbuddy)
@register_model(ModelType.openbuddy_llama2_13b_chat,
                'OpenBuddy/openbuddy-llama2-13b-v8.1-fp16', LoRATM.llama2,
                TemplateType.openbuddy)
@register_model(
    ModelType.llama2_7b_chat,
    'modelscope/Llama-2-7b-chat-ms',
    LoRATM.llama2,
    TemplateType.llama,
    ignore_file_pattern=[r'.+\.bin$'])
@register_model(
    ModelType.llama2_70b,
    'modelscope/Llama-2-70b-ms',
    LoRATM.llama2,
    TemplateType.default_generation,
    ignore_file_pattern=[r'.+\.bin$'])
@register_model(
    ModelType.llama2_7b,
    'modelscope/Llama-2-7b-ms',
    LoRATM.llama2,
    TemplateType.default_generation,
    ignore_file_pattern=[r'.+\.bin$'])
@register_model(
    ModelType.baichuan_13b_chat,
    'baichuan-inc/Baichuan-13B-Chat',
    LoRATM.baichuan,
    TemplateType.baichuan,
    requires=['transformers<4.34'])
@register_model(
    ModelType.baichuan_7b,
    'baichuan-inc/baichuan-7B',
    LoRATM.baichuan,
    TemplateType.default_generation,
    requires=['transformers<4.34'])
def get_model_tokenizer_from_repo(model_dir: str,
                                  torch_dtype: Dtype,
                                  model_kwargs: Dict[str, Any],
                                  load_model: bool = True,
                                  model_config=None,
                                  tokenizer=None,
                                  automodel_class=AutoModelForCausalLM,
                                  **kwargs):
    """load from an independent repository"""
    if model_config is None:
        model_config = AutoConfig.from_pretrained(
            model_dir, trust_remote_code=True)
    model_config.torch_dtype = torch_dtype
    logger.info(f'model_config: {model_config}')
    if tokenizer is None:
        tokenizer = AutoTokenizer.from_pretrained(
            model_dir, trust_remote_code=True)
    model = None
    if load_model:
        model = automodel_class.from_pretrained(
            model_dir,
            config=model_config,
            torch_dtype=torch_dtype,
            trust_remote_code=True,
            **model_kwargs)
    return model, tokenizer


def get_model_tokenizer_from_sdk(
        config_class: Type[PretrainedConfig],
        tokenizer_class: Type[PreTrainedTokenizerBase],
        model_dir: str,
        torch_dtype: Dtype,
        model_kwargs: Dict[str, Any],
        load_model: bool = True,
        model_config=None,
        **kwargs):
    """load from ms library"""
    config = read_config(model_dir)
    logger.info(config)
    if model_config is None:
        model_config = config_class.from_pretrained(model_dir)
    model_config.torch_dtype = torch_dtype
    logger.info(model_config)
    tokenizer = tokenizer_class.from_pretrained(model_dir)
    model = None
    if load_model:
        model = Model.from_pretrained(
            model_dir,
            cfg_dict=config,
            config=model_config,
            torch_dtype=torch_dtype,
            **model_kwargs)
    return model, tokenizer


@register_model(
    ModelType.baichuan_13b,
    'baichuan-inc/Baichuan-13B-Base',
    LoRATM.baichuan,
    TemplateType.default_generation,
    requires=['transformers<4.34'])
def get_model_tokenizer_baichuan_13b(model_dir: str,
                                     torch_dtype: Dtype,
                                     model_kwargs: Dict[str, Any],
                                     load_model: bool = True,
                                     **kwargs):
    model, tokenizer = get_model_tokenizer_from_repo(model_dir, torch_dtype,
                                                     model_kwargs, load_model,
                                                     **kwargs)
    # baichuan-13b does not implement the `get_input_embeddings` function
    # fix gradient_checkpointing bug
    try:
        model.get_input_embeddings()
    except NotImplementedError:
        model.__class__.get_input_embeddings = lambda self: self.model.embed_tokens
    return model, tokenizer


@register_model(ModelType.baichuan2_13b_chat,
                'baichuan-inc/Baichuan2-13B-Chat', LoRATM.baichuan,
                TemplateType.baichuan)
@register_model(ModelType.baichuan2_13b, 'baichuan-inc/Baichuan2-13B-Base',
                LoRATM.baichuan, TemplateType.default_generation)
def get_model_tokenizer_baichuan2_13b(model_dir: str,
                                      torch_dtype: Dtype,
                                      model_kwargs: Dict[str, Any],
                                      load_model: bool = True,
                                      **kwargs):
    # patch: baichuan2_13b configuration_baichuan.py bug
    model_config = AutoConfig.from_pretrained(
        model_dir, trust_remote_code=True)
    gradient_checkpointing = model_config.gradient_checkpointing
    if isinstance(gradient_checkpointing, (tuple, list)):
        model_config.gradient_checkpointing = gradient_checkpointing[0]
    return get_model_tokenizer_baichuan2(model_dir, torch_dtype, model_kwargs,
                                         load_model, model_config, **kwargs)


def patch_baichuan2_lm_head_forward(self, hidden_states: Tensor) -> Tensor:
    # patch: baichuan2 lm_head (fp32 bug)
    if self.training:
        norm_weight = F.normalize(self.weight).to(self.weight.dtype)
    elif self.first_flag:
        self.first_flag = False
        self.weight.data = F.normalize(self.weight).to(self.weight.dtype)
        norm_weight = self.weight
    else:
        norm_weight = self.weight
    return F.linear(hidden_states, norm_weight)


@register_model(ModelType.baichuan2_7b_chat, 'baichuan-inc/Baichuan2-7B-Chat',
                LoRATM.baichuan, TemplateType.baichuan)
@register_model(ModelType.baichuan2_7b, 'baichuan-inc/Baichuan2-7B-Base',
                LoRATM.baichuan, TemplateType.default_generation)
def get_model_tokenizer_baichuan2(model_dir: str,
                                  torch_dtype: Dtype,
                                  model_kwargs: Dict[str, Any],
                                  load_model: bool = True,
                                  model_config=None,
                                  **kwargs):
    model, tokenizer = get_model_tokenizer_from_repo(model_dir, torch_dtype,
                                                     model_kwargs, load_model,
                                                     model_config, **kwargs)
    if model is not None:
        model.lm_head.forward = MethodType(patch_baichuan2_lm_head_forward,
                                           model.lm_head)

    return model, tokenizer


@register_model(
    ModelType.baichuan2_13b_chat_int4,
    'baichuan-inc/Baichuan2-13B-Chat-4bits',
    LoRATM.baichuan,
    TemplateType.baichuan,
    torch_dtype=torch.bfloat16)
@register_model(
    ModelType.baichuan2_7b_chat_int4,
    'baichuan-inc/Baichuan2-7B-Chat-4bits',
    LoRATM.baichuan,
    TemplateType.baichuan,
    torch_dtype=torch.bfloat16)
def get_model_tokenizer_baichuan2_int4(model_dir: str,
                                       torch_dtype: Dtype,
                                       model_kwargs: Dict[str, Any],
                                       load_model: bool = True,
                                       **kwargs):
    logger.info('use `model_config.quantization_config`, ignore bnb arguments')
    model_kwargs.pop('quantization_config', None)

    # fix device_map bug
    import accelerate
    _old_infer_auto_device_map = accelerate.infer_auto_device_map
    device_map = model_kwargs.get('device_map', None)
    if device_map != 'auto':
        accelerate.infer_auto_device_map = lambda *args, **kwargs: device_map
    model, tokenizer = get_model_tokenizer_baichuan2(model_dir, torch_dtype,
                                                     model_kwargs, load_model,
                                                     **kwargs)
    if device_map != 'auto':
        accelerate.infer_auto_device_map = _old_infer_auto_device_map
    if model is not None:
        model.train()
        model._is_quantized_training_enabled = True
        model.is_loaded_in_4bit = True
    return model, tokenizer


@register_model(ModelType.chatglm3_6b_32k, 'ZhipuAI/chatglm3-6b-32k',
                LoRATM.chatglm, TemplateType.chatglm3)
@register_model(ModelType.chatglm3_6b, 'ZhipuAI/chatglm3-6b', LoRATM.chatglm,
                TemplateType.chatglm3)
@register_model(ModelType.chatglm3_6b_base, 'ZhipuAI/chatglm3-6b-base',
                LoRATM.chatglm, TemplateType.chatglm_generation)
@register_model(ModelType.chatglm2_6b_32k, 'ZhipuAI/chatglm2-6b-32k',
                LoRATM.chatglm, TemplateType.chatglm2)
@register_model(ModelType.chatglm2_6b, 'ZhipuAI/chatglm2-6b', LoRATM.chatglm,
                TemplateType.chatglm2)
def get_model_tokenizer_chatglm2(model_dir: str,
                                 torch_dtype: Dtype,
                                 model_kwargs: Dict[str, Any],
                                 load_model: bool = True,
                                 **kwargs):
    if model_kwargs.get('quantization_config') is not None:
        model_kwargs['quantization_config'].llm_int8_skip_modules = [
            'output_layer'
        ]
    model, tokenizer = get_model_tokenizer_from_repo(model_dir, torch_dtype,
                                                     model_kwargs, load_model,
                                                     **kwargs)
    if model is not None:
        from torch.nn import CrossEntropyLoss
        _old_forward = CrossEntropyLoss.forward

        def cross_entropy_forward(self, inputs: Tensor,
                                  target: Tensor) -> Tensor:
            target = target.to(device=inputs.device)
            return _old_forward(self, inputs, target)

        CrossEntropyLoss.forward = cross_entropy_forward
    return model, tokenizer


@register_model(
    ModelType.llama2_70b_chat,
    'modelscope/Llama-2-70b-chat-ms',
    LoRATM.llama2,
    TemplateType.llama,
    ignore_file_pattern=[r'.+\.bin$'])
@register_model(
    ModelType.llama2_13b_chat,
    'modelscope/Llama-2-13b-chat-ms',
    LoRATM.llama2,
    TemplateType.llama,
    ignore_file_pattern=[r'.+\.bin$'])
@register_model(
    ModelType.llama2_13b,
    'modelscope/Llama-2-13b-ms',
    LoRATM.llama2,
    TemplateType.default_generation,
    ignore_file_pattern=[r'.+\.bin$'])
def get_model_tokenizer_llama2(model_dir: str,
                               torch_dtype: Dtype,
                               model_kwargs: Dict[str, Any],
                               load_model: bool = True,
                               **kwargs):
    model_config = AutoConfig.from_pretrained(
        model_dir, trust_remote_code=True)
    model_config.pretraining_tp = 1
    return get_model_tokenizer_from_repo(model_dir, torch_dtype, model_kwargs,
                                         load_model, model_config, **kwargs)


@register_model(ModelType.polylm_13b, 'damo/nlp_polylm_13b_text_generation',
                LoRATM.polylm, TemplateType.default_generation)
def get_model_tokenizer_polylm(model_dir: str,
                               torch_dtype: Dtype,
                               model_kwargs: Dict[str, Any],
                               load_model: bool = True,
                               **kwargs):
    tokenizer = AutoTokenizer.from_pretrained(
        model_dir, trust_remote_code=True, use_fast=False, legacy=True)
    return get_model_tokenizer_from_repo(
        model_dir,
        torch_dtype,
        model_kwargs,
        load_model,
        tokenizer=tokenizer,
        **kwargs)


dtype_mapping = {
    torch.float16: 'fp16',
    torch.bfloat16: 'bf16',
    torch.float32: 'fp32'
}


@register_model(ModelType.qwen_14b_chat, 'qwen/Qwen-14B-Chat', LoRATM.qwen,
                TemplateType.chatml)
@register_model(ModelType.qwen_14b, 'qwen/Qwen-14B', LoRATM.qwen,
                TemplateType.default_generation)
@register_model(ModelType.qwen_7b_chat, 'qwen/Qwen-7B-Chat', LoRATM.qwen,
                TemplateType.chatml)
@register_model(
    ModelType.qwen_7b,
    'qwen/Qwen-7B',
    LoRATM.qwen,
    TemplateType.default_generation,
)
def get_model_tokenizer_qwen(model_dir: str,
                             torch_dtype: Dtype,
                             model_kwargs: Dict[str, Any],
                             load_model: bool = True,
                             **kwargs):
    model_config = AutoConfig.from_pretrained(
        model_dir, trust_remote_code=True)
    k_true = dtype_mapping[torch_dtype]
    for k in dtype_mapping.values():
        v = False
        if k == k_true:
            v = True
        setattr(model_config, k, v)

    use_flash_attn = kwargs.pop('use_flash_attn', None)
    if use_flash_attn is None:
        use_flash_attn = 'auto'
    model_config.use_flash_attn = use_flash_attn
    model, tokenizer = get_model_tokenizer_from_repo(model_dir, torch_dtype,
                                                     model_kwargs, load_model,
                                                     model_config, **kwargs)
    try:
        # fix mp+ddp bug
        model.transformer.registered_causal_mask = model.transformer.registered_causal_mask.cuda(
        )
        logger.info('registered_causal_mask to cuda')
    except AttributeError:
        pass
    tokenizer.eos_token_id = tokenizer.eod_id
    return model, tokenizer


@register_model(ModelType.qwen_vl_chat, 'qwen/Qwen-VL-Chat', LoRATM.qwen,
                TemplateType.chatml)
@register_model(ModelType.qwen_vl, 'qwen/Qwen-VL', LoRATM.qwen,
                TemplateType.default_generation)
def get_model_tokenizer_qwen_vl(model_dir: str,
                                torch_dtype: Dtype,
                                model_kwargs: Dict[str, Any],
                                load_model: bool = True,
                                **kwargs):
    if (model_kwargs.get('quantization_config') is not None and isinstance(
            model_kwargs['quantization_config'], BitsAndBytesConfig)):
        # https://github.com/pytorch/pytorch/issues/58969
        model_kwargs['quantization_config'].llm_int8_skip_modules = [
            'lm_head', 'attn_pool.attn'
        ]
    model, tokenizer = get_model_tokenizer_qwen(model_dir, torch_dtype,
                                                model_kwargs, load_model,
                                                **kwargs)
    if model is not None:
        first_drop = model.transformer.drop
        if first_drop.p == 0.:
            # fix gradient_checkpointing bug
            _old_forward = first_drop.forward
            if not hasattr(_old_forward, '_patching'):
                first_drop.forward = lambda *args, **kwargs: _old_forward(
                    *args, **kwargs).clone()
                first_drop.forward._patching = True
    return model, tokenizer


@register_model(
    ModelType.qwen_vl_chat_int4,
    'qwen/Qwen-VL-Chat-Int4',
    LoRATM.qwen,
    TemplateType.chatml,
    requires=['auto_gptq>=0.4.2'],
    torch_dtype=torch.float16,
    function_kwargs={'get_qwen_function': get_model_tokenizer_qwen_vl})
@register_model(
    ModelType.qwen_14b_chat_int8,
    'qwen/Qwen-14B-Chat-Int8',
    LoRATM.qwen,
    TemplateType.chatml,
    requires=['auto_gptq>=0.4.2'],
    torch_dtype=torch.float16)
@register_model(
    ModelType.qwen_7b_chat_int8,
    'qwen/Qwen-7B-Chat-Int8',
    LoRATM.qwen,
    TemplateType.chatml,
    requires=['auto_gptq>=0.4.2'],
    torch_dtype=torch.float16)
@register_model(
    ModelType.qwen_14b_chat_int4,
    'qwen/Qwen-14B-Chat-Int4',
    LoRATM.qwen,
    TemplateType.chatml,
    requires=['auto_gptq>=0.4.2'],
    torch_dtype=torch.float16)
@register_model(
    ModelType.qwen_7b_chat_int4,
    'qwen/Qwen-7B-Chat-Int4',
    LoRATM.qwen,
    TemplateType.chatml,
    requires=['auto_gptq>=0.4.2'],
    torch_dtype=torch.float16)
def get_model_tokenizer_qwen_intx(model_dir: str,
                                  torch_dtype: Dtype,
                                  model_kwargs: Dict[str, Any],
                                  load_model: bool = True,
                                  **kwargs):

    logger.info('use gptq, ignore bnb arguments')
    model_kwargs['quantization_config'] = GPTQConfig(
        bits=4, disable_exllama=True)

    # fix quantlinear bug
    from auto_gptq.nn_modules.qlinear.qlinear_cuda_old import QuantLinear
    _old_qlinear_init = QuantLinear.__init__
    if not hasattr(_old_qlinear_init, '_patching'):
        QuantLinear.__init__ = (lambda *args, **kwargs: _old_qlinear_init(
            *args, kernel_switch_threshold=1, **kwargs))
        QuantLinear.__init__._patching = True
    get_qwen_function = kwargs.pop('get_qwen_function',
                                   get_model_tokenizer_qwen)
    model, tokenizer = get_qwen_function(model_dir, torch_dtype, model_kwargs,
                                         load_model, **kwargs)
    tokenizer.eos_token_id = tokenizer.eod_id
    return model, tokenizer


register_model(ModelType.skywork_13b, 'skywork/Skywork-13B-base',
               LoRATM.llama2, TemplateType.default_generation,
               get_model_tokenizer_from_repo)


@register_model(ModelType.skywork_13b_chat, 'skywork/Skywork-13B-chat',
                LoRATM.llama2, TemplateType.skywork)
def get_skywork_model_tokenizer(model_dir: str,
                                torch_dtype: Dtype,
                                model_kwargs: Dict[str, Any],
                                load_model: bool = True,
                                **kwargs):
    model, tokenizer = get_model_tokenizer_from_repo(model_dir, torch_dtype,
                                                     model_kwargs, load_model,
                                                     **kwargs)
    tokenizer.add_tokens('[USER]')
    tokenizer.add_tokens('[BOT]')
    tokenizer.add_tokens('[SEP]')
    return model, tokenizer


def get_model_tokenizer(
        model_type: str,
        torch_dtype: Optional[Dtype] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        load_model: bool = True,
        **kwargs) -> Tuple[Optional[PreTrainedModel], PreTrainedTokenizerBase]:
    model_info = MODEL_MAPPING[model_type]
    requires = model_info['requires']
    for require in requires:
        require_version(require)

    model_id_or_path = model_info['model_id_or_path']
    get_function = model_info['get_function']
    ignore_file_pattern = model_info['ignore_file_pattern']
    if model_info.get('torch_dtype') is not None:
        model_torch_dtype = model_info['torch_dtype']
        if torch_dtype is None:
            torch_dtype = model_torch_dtype
        else:
            assert torch_dtype == model_torch_dtype, f'please use `{model_torch_dtype}`'
    elif torch_dtype is None:
        torch_dtype = torch.float16
    if model_kwargs is None:
        model_kwargs = {}
    if 'device_map' not in model_kwargs:
        model_kwargs['device_map'] = 'auto'

    model_dir = kwargs.pop('model_dir', None)
    if model_dir is None:
        if is_dist() and not is_local_master():
            dist.barrier()
        model_dir = model_id_or_path
        if not os.path.exists(model_id_or_path):
            revision = model_info['revision']
            model_dir = snapshot_download(
                model_id_or_path,
                revision,
                ignore_file_pattern=ignore_file_pattern)
        if is_dist() and is_local_master():
            dist.barrier()
    else:
        model_dir = os.path.expanduser(model_dir)
        assert os.path.isdir(model_dir)

    kwargs['automodel_class'] = model_info['automodel_class']
    model, tokenizer = get_function(model_dir, torch_dtype, model_kwargs,
                                    load_model, **kwargs)
    assert tokenizer.eos_token is not None
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    if model is not None:
        generation_config_path = os.path.join(model_dir,
                                              'generation_config.json')
        generation_config = getattr(model, 'generation_config', None)
        if os.path.isfile(
                generation_config_path) and generation_config is None:
            model.generation_config = GenerationConfig.from_pretrained(
                model_dir)
    return model, tokenizer
