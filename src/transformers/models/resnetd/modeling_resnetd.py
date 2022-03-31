# coding=utf-8
# Copyright 2022 Amazon Inc. and The HuggingFace Inc. team. All rights reserved.
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
""" PyTorch ResNetD model."""
from typing import Optional

import torch
import torch.utils.checkpoint
from torch import Tensor, nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss

from ...activations import ACT2FN
from ...file_utils import add_code_sample_docstrings, add_start_docstrings, add_start_docstrings_to_model_forward
from ...modeling_outputs import (
    BaseModelOutputWithNoAttention,
    BaseModelOutputWithPoolingAndNoAttention,
    ImageClassifierOutput,
)
from ...modeling_utils import PreTrainedModel
from ...utils import logging
from .configuration_resnetd import ResNetDConfig


logger = logging.get_logger(__name__)

# General docstring
_CONFIG_FOR_DOC = "ResNetDConfig"
_FEAT_EXTRACTOR_FOR_DOC = "AutoFeatureExtractor"

# Base docstring
_CHECKPOINT_FOR_DOC = "zuppif/resnetd-50"
_EXPECTED_OUTPUT_SHAPE = [1, 2048, 7, 7]

# Image classification docstring
_IMAGE_CLASS_CHECKPOINT = "zuppif/resnetd-50"
_IMAGE_CLASS_EXPECTED_OUTPUT = "'tabby, tabby cat'"

RESNETD_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "zuppif/resnet-d-50",
    # See all resnetd models at https://huggingface.co/models?filter=resnetd
]

# Copied from transformers.models.resnet.modeling_resnet.ResNetConvLayer with ResNet->ResNetD
class ResNetDConvLayer(nn.Sequential):
    def __init__(
        self, in_channels: int, out_channels: int, kernel_size: int = 3, stride: int = 1, activation: str = "relu"
    ):
        super().__init__()
        self.convolution = nn.Conv2d(
            in_channels, out_channels, kernel_size=kernel_size, stride=stride, padding=kernel_size // 2, bias=False
        )
        self.normalization = nn.BatchNorm2d(out_channels)
        self.activation = ACT2FN[activation] if activation is not None else nn.Identity()


class ResNetDEmbeddings(nn.Sequential):
    """
    ResNetD embeddings (stem), different from the original one. The observation is that the computational cost of a
    convolution is quadratic to the kernel width or height. A `7x7` convolution is `5.4` times more expensive than a
    `3x3` convolution. So this tweak replacing the `7x7` convolution in the input stem with three conservative
    `3x3`convolution.
    """

    def __init__(self, num_channels: int, hidden_size: int = 64, activation: str = "relu"):
        super().__init__()
        self.layers = nn.Sequential(
            ResNetDConvLayer(num_channels, hidden_size // 2, kernel_size=3, stride=2, activation=activation),
            ResNetDConvLayer(hidden_size // 2, hidden_size // 2, kernel_size=3, activation=activation),
            ResNetDConvLayer(hidden_size // 2, hidden_size, kernel_size=3, activation=activation),
        )
        self.pooler = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)


class ResNetDShortCut(nn.Sequential):
    """
    ResNetD shortcut, it uses an average pooling instead to downsample the input.
    """

    def __init__(self, in_channels: int, out_channels: int, stride: int = 2):
        super().__init__()
        self.pooler = nn.AvgPool2d(kernel_size=2, stride=2, ceil_mode=True) if stride == 2 else nn.Identity()
        self.convolution = nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=False)
        self.normalization = nn.BatchNorm2d(out_channels)


# Copied from transformers.models.resnet.modeling_resnet.ResNetBasicLayer with ResNet->ResNetD
class ResNetDBasicLayer(nn.Module):
    """
    A classic ResNetD's residual layer composed by a two `3x3` convolutions.
    """

    def __init__(self, in_channels: int, out_channels: int, stride: int = 1, activation: str = "relu"):
        super().__init__()
        should_apply_shortcut = in_channels != out_channels or stride != 1
        self.shortcut = (
            ResNetDShortCut(in_channels, out_channels, stride=stride) if should_apply_shortcut else nn.Identity()
        )
        self.layer = nn.Sequential(
            ResNetDConvLayer(in_channels, out_channels, stride=stride),
            ResNetDConvLayer(out_channels, out_channels, activation=None),
        )
        self.activation = ACT2FN[activation]

    def forward(self, hidden_state):
        residual = hidden_state
        hidden_state = self.layer(hidden_state)
        residual = self.shortcut(residual)
        hidden_state += residual
        hidden_state = self.activation(hidden_state)
        return hidden_state


# Copied from transformers.models.resnet.modeling_resnet.ResNetBottleNeckLayer with ResNet->ResNetD
class ResNetDBottleNeckLayer(nn.Module):
    """
    A classic ResNetD's bottleneck layer composed by a three `3x3` convolutions.

    The first `1x1` convolution reduces the input by a factor of `reduction` in order to make the second `3x3`
    convolution faster. The last `1x1` convolution remap the reduced features to `out_channels`.
    """

    def __init__(
        self, in_channels: int, out_channels: int, stride: int = 1, activation: str = "relu", reduction: int = 4
    ):
        super().__init__()
        should_apply_shortcut = in_channels != out_channels or stride != 1
        reduces_channels = out_channels // reduction
        self.shortcut = (
            ResNetDShortCut(in_channels, out_channels, stride=stride) if should_apply_shortcut else nn.Identity()
        )
        self.layer = nn.Sequential(
            ResNetDConvLayer(in_channels, reduces_channels, kernel_size=1),
            ResNetDConvLayer(reduces_channels, reduces_channels, stride=stride),
            ResNetDConvLayer(reduces_channels, out_channels, kernel_size=1, activation=None),
        )
        self.activation = ACT2FN[activation]

    def forward(self, hidden_state):
        residual = hidden_state
        hidden_state = self.layer(hidden_state)
        residual = self.shortcut(residual)
        hidden_state += residual
        hidden_state = self.activation(hidden_state)
        return hidden_state


# Copied from transformers.models.resnet.modeling_resnet.ResNetStage with ResNet->ResNetD
class ResNetDStage(nn.Sequential):
    """
    A ResNetD stage composed by stacked layers.
    """

    def __init__(
        self,
        config: ResNetDConfig,
        in_channels: int,
        out_channels: int,
        stride: int = 2,
        depth: int = 2,
    ):
        super().__init__()

        layer = ResNetDBottleNeckLayer if config.layer_type == "bottleneck" else ResNetDBasicLayer

        self.layers = nn.Sequential(
            # downsampling is done in the first layer with stride of 2
            layer(in_channels, out_channels, stride=stride, activation=config.hidden_act),
            *[layer(out_channels, out_channels, activation=config.hidden_act) for _ in range(depth - 1)],
        )


# Copied from transformers.models.resnet.modeling_resnet.ResNetEncoder with ResNet->ResNetD
class ResNetDEncoder(nn.Module):
    def __init__(self, config: ResNetDConfig):
        super().__init__()
        self.stages = nn.ModuleList([])
        # based on `downsample_in_first_stage` the first layer of the first stage may or may not downsample the input
        self.stages.append(
            ResNetDStage(
                config,
                config.embedding_size,
                config.hidden_sizes[0],
                stride=2 if config.downsample_in_first_stage else 1,
                depth=config.depths[0],
            )
        )
        in_out_channels = zip(config.hidden_sizes, config.hidden_sizes[1:])
        for (in_channels, out_channels), depth in zip(in_out_channels, config.depths[1:]):
            self.stages.append(ResNetDStage(config, in_channels, out_channels, depth=depth))

    def forward(
        self, hidden_state: Tensor, output_hidden_states: bool = False, return_dict: bool = True
    ) -> BaseModelOutputWithNoAttention:
        hidden_states = () if output_hidden_states else None

        for stage_module in self.stages:
            if output_hidden_states:
                hidden_states = hidden_states + (hidden_state,)

            hidden_state = stage_module(hidden_state)

        if output_hidden_states:
            hidden_states = hidden_states + (hidden_state,)

        if not return_dict:
            return tuple(v for v in [hidden_state, hidden_states] if v is not None)

        return BaseModelOutputWithNoAttention(
            last_hidden_state=hidden_state,
            hidden_states=hidden_states,
        )


# Copied from transformers.models.resnet.modeling_resnet.ResNetPreTrainedModel with ResNet->ResNetD,resnet->resnetd
class ResNetDPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = ResNetDConfig
    base_model_prefix = "resnetd"
    main_input_name = "pixel_values"
    supports_gradient_checkpointing = True

    def _init_weights(self, module):
        if isinstance(module, nn.Conv2d):
            nn.init.kaiming_normal_(module.weight, mode="fan_out", nonlinearity="relu")
        elif isinstance(module, (nn.BatchNorm2d, nn.GroupNorm)):
            nn.init.constant_(module.weight, 1)
            nn.init.constant_(module.bias, 0)

    def _set_gradient_checkpointing(self, module, value=False):
        if isinstance(module, ResNetDModel):
            module.gradient_checkpointing = value


RESNETD_START_DOCSTRING = r"""
    This model is a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass. Use it
    as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage and
    behavior.

    Parameters:
        config ([`ResNetDConfig`]): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""

RESNETD_INPUTS_DOCSTRING = r"""
    Args:
        pixel_values (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`):
            Pixel values. Pixel values can be obtained using [`AutoFeatureExtractor`]. See
            [`AutoFeatureExtractor.__call__`] for details.

        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~file_utils.ModelOutput`] instead of a plain tuple.
"""


@add_start_docstrings(
    "The bare ResNetD model outputting raw features without any specific head on top.",
    RESNETD_START_DOCSTRING,
)
# Copied from transformers.models.resnet.modeling_resnet.ResNetModel with RESNET->RESNETD,ResNet->ResNetD
class ResNetDModel(ResNetDPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.config = config
        self.embedder = ResNetDEmbeddings(config.num_channels, config.embedding_size, config.hidden_act)
        self.encoder = ResNetDEncoder(config)
        self.pooler = nn.AdaptiveAvgPool2d((1, 1))
        # Initialize weights and apply final processing
        self.post_init()

    @add_start_docstrings_to_model_forward(RESNETD_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        processor_class=_FEAT_EXTRACTOR_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=BaseModelOutputWithPoolingAndNoAttention,
        config_class=_CONFIG_FOR_DOC,
        modality="vision",
        expected_output=_EXPECTED_OUTPUT_SHAPE,
    )
    def forward(
        self, pixel_values: Tensor, output_hidden_states: Optional[bool] = None, return_dict: Optional[bool] = None
    ) -> BaseModelOutputWithPoolingAndNoAttention:
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        embedding_output = self.embedder(pixel_values)

        encoder_outputs = self.encoder(
            embedding_output, output_hidden_states=output_hidden_states, return_dict=return_dict
        )

        last_hidden_state = encoder_outputs[0]

        pooled_output = self.pooler(last_hidden_state)

        if not return_dict:
            return (last_hidden_state, pooled_output) + encoder_outputs[1:]

        return BaseModelOutputWithPoolingAndNoAttention(
            last_hidden_state=last_hidden_state,
            pooler_output=pooled_output,
            hidden_states=encoder_outputs.hidden_states,
        )


@add_start_docstrings(
    """
    ResNetD Model with an image classification head on top (a linear layer on top of the pooled features), e.g. for
    ImageNet.
    """,
    RESNETD_START_DOCSTRING,
)
# Copied from transformers.models.resnet.modeling_resnet.ResNetForImageClassification with RESNET->RESNETD,ResNet->ResNetD,resnet->resnetd
class ResNetDForImageClassification(ResNetDPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.resnetd = ResNetDModel(config)
        # classification head
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(config.hidden_sizes[-1], config.num_labels) if config.num_labels > 0 else nn.Identity(),
        )
        # initialize weights and apply final processing
        self.post_init()

    @add_start_docstrings_to_model_forward(RESNETD_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        processor_class=_FEAT_EXTRACTOR_FOR_DOC,
        checkpoint=_IMAGE_CLASS_CHECKPOINT,
        output_type=ImageClassifierOutput,
        config_class=_CONFIG_FOR_DOC,
        expected_output=_IMAGE_CLASS_EXPECTED_OUTPUT,
    )
    def forward(
        self,
        pixel_values: Tensor = None,
        labels: Tensor = None,
        output_hidden_states: bool = None,
        return_dict: bool = None,
    ) -> ImageClassifierOutput:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the image classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.resnetd(pixel_values, output_hidden_states=output_hidden_states, return_dict=return_dict)

        pooled_output = outputs.pooler_output if return_dict else outputs[1]

        logits = self.classifier(pooled_output)

        loss = None

        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"
            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)

        if not return_dict:
            output = (logits,) + outputs[2:]
            return (loss,) + output if loss is not None else output

        return ImageClassifierOutput(loss=loss, logits=logits, hidden_states=outputs.hidden_states)