# Copyright 2025 - Pruna AI GmbH. All rights reserved.
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

import tempfile
from typing import Any, Dict

import diffusers
from ConfigSpace import CategoricalHyperparameter, Constant, OrdinalHyperparameter
from diffusers import BitsAndBytesConfig as DiffusersBitsAndBytesConfig

from pruna.algorithms.quantization import PrunaQuantizer
from pruna.config.smash_config import SmashConfigPrefixWrapper
from pruna.config.smash_space import Boolean
from pruna.engine.model_checks import (
    get_diffusers_transformer_models,
    get_diffusers_unet_models,
)
from pruna.engine.utils import safe_memory_cleanup


class DiffusersInt8Quantizer(PrunaQuantizer):
    """
    Implement Int8 quantization for Image-Gen models.

    BitsAndBytes offers a simple method to quantize models to 8-bit or 4-bit precision.
    The 8-bit mode blends outlier fp16 values with int8 non-outliers to mitigate performance degradation,
    while 4-bit quantization further compresses the model and is often used with QLoRA for fine-tuning.
    This algorithm is specifically adapted for diffusers models.
    """

    algorithm_name = "diffusers_int8"
    references = {"GitHub": "https://github.com/bitsandbytes-foundation/bitsandbytes"}
    tokenizer_required = False
    processor_required = False
    dataset_required = False
    run_on_cpu = False
    run_on_cuda = True
    compatible_algorithms = dict(
        cacher=["deepcache"],
    )

    def get_hyperparameters(self) -> list:
        """
        Configure all algorithm-specific hyperparameters with ConfigSpace.

        Returns
        -------
        list
            The hyperparameters.
        """
        return [
            OrdinalHyperparameter(
                "weight_bits",
                sequence=[4, 8],
                default_value=8,
                meta=dict(desc="Number of bits to use for quantization."),
            ),
            Boolean("double_quant", meta=dict(desc="Whether to enable double quantization.")),
            Boolean("enable_fp32_cpu_offload", meta=dict(desc="Whether to enable fp32 cpu offload.")),
            Constant("has_fp16_weight", value=False),
            Constant("compute_dtype", value="bfloat16"),
            Constant("threshold", value=6.0),
            CategoricalHyperparameter(
                "quant_type",
                choices=["fp4", "nf4"],
                default_value="fp4",
                meta=dict(desc="Quantization type to use."),
            ),
        ]

    def model_check_fn(self, model: Any) -> bool:
        """
        Check if the model is a unet-based or transformer-based diffusion model.

        Parameters
        ----------
        model : Any
            The model to check.

        Returns
        -------
        bool
            True if the model is a diffusion model, False otherwise.
        """
        transformer_and_unet_models = get_diffusers_transformer_models() + get_diffusers_unet_models()

        if isinstance(model, tuple(transformer_and_unet_models)):
            return True

        if hasattr(model, "transformer"):
            if isinstance(model.transformer, tuple(transformer_and_unet_models)):
                return True

        if hasattr(model, "unet"):
            if isinstance(model.unet, tuple(transformer_and_unet_models)):
                return True

        return False

    def _apply(self, model: Any, smash_config: SmashConfigPrefixWrapper) -> Any:
        """
        Quantize the model.

        Parameters
        ----------
        model : Any
            The model to quantize.
        smash_config : SmashConfigPrefixWrapper
            The configuration for the quantization.

        Returns
        -------
        Any
            The quantized model.
        """
        with tempfile.TemporaryDirectory(prefix=smash_config["cache_dir"]) as temp_dir:
            # cast original model to CPU to free memory for smashed model
            if hasattr(model, "to"):
                input_device = model.device
                model.to("cpu")
                safe_memory_cleanup()

            # save the latent model (to be quantized) in a temp directory
            if hasattr(model, "transformer"):
                model.transformer.save_pretrained(temp_dir)
                latent_class = getattr(diffusers, type(model.transformer).__name__)
                compute_dtype = next(iter(model.transformer.parameters())).dtype
            elif hasattr(model, "unet"):
                model.unet.save_pretrained(temp_dir)
                latent_class = getattr(diffusers, type(model.unet).__name__)
                compute_dtype = next(iter(model.unet.parameters())).dtype
            else:
                model.save_pretrained(temp_dir)
                latent_class = getattr(diffusers, type(model).__name__)
                compute_dtype = next(iter(model.parameters())).dtype

            bnb_config = DiffusersBitsAndBytesConfig(
                load_in_8bit=smash_config["weight_bits"] == 8,
                load_in_4bit=smash_config["weight_bits"] == 4,
                llm_int8_threshold=float(smash_config["threshold"]),
                llm_int8_skip_modules=["lm_head"],
                llm_int8_enable_fp32_cpu_offload=smash_config["enable_fp32_cpu_offload"],
                llm_int8_has_fp16_weight=smash_config["has_fp16_weight"],
                bnb_4bit_compute_dtype=compute_dtype,
                bnb_4bit_quant_type=smash_config["quant_type"],
                bnb_4bit_use_double_quant=smash_config["double_quant"],
            )

            # re-load the latent model (with the quantization config)
            smashed_latent = latent_class.from_pretrained(
                temp_dir,
                quantization_config=bnb_config,
                torch_dtype=compute_dtype,
            )
            # replace the latent model in the pipeline
            if hasattr(model, "transformer"):
                model.transformer = smashed_latent
            elif hasattr(model, "unet"):
                model.unet = smashed_latent
            else:
                model = smashed_latent

            # move the model back to the original device
            if hasattr(model, "to"):
                model.to(input_device)
            return model

    def import_algorithm_packages(self) -> Dict[str, Any]:
        """
        Provide a algorithm packages for the algorithm.

        Returns
        -------
        Dict[str, Any]
            The algorithm packages.
        """
        return dict()
