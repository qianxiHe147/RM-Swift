<div align="center">

## [NeurIPS 2023] Res-Tuning: A Flexible and Efficient Tuning Paradigm via Unbinding Tuner from Backbone

### [arXiv](https://arxiv.org/abs/2310.19859)  |  [Project Page](https://res-tuning.github.io/)

</div>

Res-Tuning is a flexible and efficient tuning paradigm. We manage to free the design of tuners from the network architecture, facilitating flexible combination of various tuning strategies and further extend a memory-efficient bypass variant, which significantly reduces the memory consumption and multi-task inference cost.

The implementation is a pluggable tuner component for [SWIFT](https://github.com/modelscope/swift), designed to be user-friendly.

### Catalog

- [x] Res-Adapter
- [x] Res-Tuning-Bypass
- [ ] Res-Prefix
- [ ] Res-Prompt

### Usage

#### Demo
- Run our interactive demo using [vision_example](https://github.com/modelscope/swift/blob/main/examples/pytorch/cv/notebook/swift_vision.ipynb).

#### Init Tuner

```Python
from swift import ResTuningConfig
config = ResTuningConfig(
    dims=768,
    root_modules=r'.*blocks.0$',
    stem_modules=r'.*blocks\.\d+$',
    target_modules=r'norm',
    tuner_cfg='res_adapter'
)
```
- dims: The dimensions of the hidden states.
- root_modules: The root module to be replaced.
- stem_modules: The stem modules to be replaced.
- target_modules: The target module to be replaced.
- tuner_cfg: The configuration of the tuning module.

#### Load Model

```Python
from swift import Swift
import timm, torch
model = timm.create_model("vit_base_patch16_224", pretrained=False, num_classes=100)
model_tune = Swift.prepare_model(model, config)
print(model_tune.get_trainable_parameters())
print(model(torch.ones(1, 3, 224, 224)).shape)
```


### Citation
```
@inproceedings{jiang2023restuning,
  title={Res-Tuning: A Flexible and Efficient Tuning Paradigm via Unbinding Tuner from Backbone},
  author={Jiang, Zeyinzi and Mao, Chaojie and Huang, Ziyuan and Ma, Ao and Lv, Yiliang and Shen, Yujun and Zhao, Deli and Zhou, Jingren},
  booktitle={Advances in Neural Information Processing Systems},
  year={2023}
}
```
