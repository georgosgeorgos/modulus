# Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Model architectures used in the paper "Diffusion models beat gans on image synthesis".
"""

from dataclasses import dataclass
from typing import List

import numpy as np
import torch
from torch.nn.functional import silu, dropout

from modulus.models.diffusion import (
    Conv2d,
    GroupNorm,
    Linear,
    PositionalEmbedding,
    UNetBlock,
)
from modulus.models.meta import ModelMetaData
from modulus.models.module import Module


@dataclass
class MetaData(ModelMetaData):
    name: str = "GuidanceNet"
    # Optimization
    jit: bool = False
    cuda_graphs: bool = False
    amp_cpu: bool = False
    amp_gpu: bool = True
    torch_fx: bool = False
    # Data type
    bf16: bool = True
    # Inference
    onnx: bool = False
    # Physics informed
    func_torch: bool = False
    auto_grad: bool = False


class GuidanceNet(Module):
    """
    Reimplementation of the ADM architecture, a U-Net variant, with optional
    self-attention.

    This model supports conditional and unconditional setups, as well as several
    options for various internal architectural choices such as encoder and decoder
    type, embedding type, etc., making it flexible and adaptable to different tasks
    and configurations.

    Parameters:
    -----------
    img_resolution : int
        The resolution of the input/output image.
    in_channels : int
        Number of channels in the input image.
    out_channels : int
        Number of channels in the output image.
    label_dim : int, optional
        Number of class labels; 0 indicates an unconditional model. By default 0.
    augment_dim : int, optional
        Dimensionality of augmentation labels; 0 means no augmentation. By default 0.
    model_channels : int, optional
        Base multiplier for the number of channels across the network, by default 192.
    channel_mult : List[int], optional
        Per-resolution multipliers for the number of channels. By default [1,2,3,4].
    channel_mult_emb : int, optional
        Multiplier for the dimensionality of the embedding vector. By default 4.
    num_blocks : int, optional
        Number of residual blocks per resolution. By default 3.
    attn_resolutions : List[int], optional
        Resolutions at which self-attention layers are applied. By default [32, 16, 8].
    dropout : float, optional
        Dropout probability applied to intermediate activations. By default 0.10.
    label_dropout : float, optional
       Dropout probability of class labels for classifier-free guidance. By default 0.0.

    Note:
    -----
    Reference: Dhariwal, P. and Nichol, A., 2021. Diffusion models beat gans on image
    synthesis. Advances in neural information processing systems, 34, pp.8780-8794.

    Note:
    -----
    Equivalent to the original implementation by Dhariwal and Nichol, available at
    https://github.com/openai/guided-diffusion

    Example:
    --------
    >>> model = GuidanceNet(img_resolution=16, in_channels=2, out_channels=2)
    >>> noise_labels = torch.randn([1])
    >>> class_labels = torch.randint(0, 1, (1, 1))
    >>> input_image = torch.ones([1, 2, 16, 16])
    >>> output_guidance = model(input_image, noise_labels, class_labels)
    """

    def __init__(
        self,
        img_resolution: int,
        in_channels: int,
        out_channels: int,
        label_dim: int = 0,
        augment_dim: int = 0,
        model_channels: int = 192,
        channel_mult: List[int] = [1, 2, 3, 4],
        channel_mult_emb: int = 4,
        num_blocks: int = 3,
        attn_resolutions: List[int] = [32, 16, 8],
        dropout: float = 0.10,
        label_dropout: float = 0.0,
        pool: str = "spatial",
    ):
        super().__init__(meta=MetaData())
        self.label_dropout = label_dropout
        emb_channels = model_channels * channel_mult_emb
        self.pool = pool
        init = dict(
            init_mode="kaiming_uniform",
            init_weight=np.sqrt(1 / 3),
            init_bias=np.sqrt(1 / 3),
        )
        init_zero = dict(init_mode="kaiming_uniform", init_weight=0, init_bias=0)
        block_kwargs = dict(
            emb_channels=emb_channels,
            channels_per_head=64,
            dropout=dropout,
            init=init,
            init_zero=init_zero,
        )

        # Mapping.
        self.map_noise = PositionalEmbedding(num_channels=model_channels)
        self.map_augment = (
            Linear(
                in_features=augment_dim,
                out_features=model_channels,
                bias=False,
                **init_zero,
            )
            if augment_dim
            else None
        )
        self.map_layer0 = Linear(
            in_features=model_channels, out_features=emb_channels, **init
        )
        self.map_layer1 = Linear(
            in_features=emb_channels, out_features=emb_channels, **init
        )
        self.map_label = (
            Linear(
                in_features=label_dim,
                out_features=emb_channels,
                bias=False,
                init_mode="kaiming_normal",
                init_weight=np.sqrt(label_dim),
            )
            if label_dim
            else None
        )

        # Encoder.
        self.enc = torch.nn.ModuleDict()
        cout = in_channels
        for level, mult in enumerate(channel_mult):
            res = img_resolution >> level
            if level == 0:
                cin = cout
                cout = model_channels * mult
                self.enc[f"{res}x{res}_conv"] = Conv2d(
                    in_channels=cin, out_channels=cout, kernel=3, **init
                )
            else:
                self.enc[f"{res}x{res}_down"] = UNetBlock(
                    in_channels=cout, out_channels=cout, down=True, **block_kwargs
                )
            for idx in range(num_blocks):
                cin = cout
                cout = model_channels * mult
                self.enc[f"{res}x{res}_block{idx}"] = UNetBlock(
                    in_channels=cin,
                    out_channels=cout,
                    attention=(res in attn_resolutions),
                    **block_kwargs,
                )
        skips = [block.out_channels for block in self.enc.values()]

        # Pooling.
        if pool == "spatial":
            self.out = torch.nn.ModuleDict()
            self.out["linear_in"] = Linear(self._feature_size, 2048)
            self.out["group_norm"] = GroupNorm(2048),
            self.out["linear_out"] = Linear(2048, self.out_channels)
        else:
            raise NotImplementedError(f"Unexpected {pool} pooling")
        
    def forward(self, x, noise_labels, class_labels, augment_labels=None):
        # Mapping.
        emb = self.map_noise(noise_labels)
        if self.map_augment is not None and augment_labels is not None:
            emb = emb + self.map_augment(augment_labels)
        emb = silu(self.map_layer0(emb))
        emb = self.map_layer1(emb)
        if self.map_label is not None:
            tmp = class_labels
            if self.training and self.label_dropout:
                tmp = tmp * (
                    torch.rand([x.shape[0], 1], device=x.device) >= self.label_dropout
                ).to(tmp.dtype)
            emb = emb + self.map_label(tmp)
        emb = silu(emb)

        # Encoder.
        skips = []
        for block in self.enc.values():
            x = block(x, emb) if isinstance(block, UNetBlock) else block(x)
            skips.append(x)
            
        # Pooling.
        ## TODO: Add support for other pooling methods
        x = self.out["linear_in"](x)
        x = self.out["group_norm"](x)
        x = silu(x)
        x = dropout(x, p=self.dropout, training=self.training)
        x = self.out["linear_out"](x)

        return x