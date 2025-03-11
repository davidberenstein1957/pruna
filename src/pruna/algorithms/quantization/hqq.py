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

import shutil
import tempfile
from typing import Any, Dict

from ConfigSpace import Constant, OrdinalHyperparameter
from transformers import AutoModelForCausalLM, HqqConfig

from pruna.algorithms.quantization import PrunaQuantizer
from pruna.config.smash_config import SmashConfigPrefixWrapper
from pruna.engine.model_checks import is_causal_lm
from pruna.engine.save import SAVE_FUNCTIONS
from pruna.logging.filter import SuppressOutput
from pruna.logging.logger import pruna_logger


class HQQQuantizer(PrunaQuantizer):
    """
    Implement HQQ using huggingface transformers and the HQQ package.

    Half-Quadratic Quantization (HQQ) leverages fast, robust optimization techniques for on-the-fly quantization,
    eliminating the need for calibration data.
    """

    algorithm_name = "hqq"
    references = {"GitHub": "https://github.com/mobiusml/hqq", "Article": "https://mobiusml.github.io/hqq_blog/"}
    save_fn = SAVE_FUNCTIONS.hqq
    tokenizer_required = False
    processor_required = False
    run_on_cpu = False
    run_on_cuda = True
    dataset_required = False
    compatible_algorithms = dict()

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
                sequence=[2, 4, 8],
                default_value=8,
                meta=dict(desc="Number of bits to use for quantization."),
            ),
            OrdinalHyperparameter(
                "group_size",
                sequence=[8, 16, 32, 64, 128],
                default_value=64,
                meta=dict(desc="Group size for quantization."),
            ),
            Constant("backend", value="torchao_int4"),
        ]

    def model_check_fn(self, model: Any) -> bool:
        """
        Check if the model is a causal language model.

        Parameters
        ----------
        model : Any
            The model to check.

        Returns
        -------
        bool
            True if the model is a causal language model, False otherwise.
        """
        return is_causal_lm(model)

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
        imported_modules = self.import_algorithm_packages()

        weight_quantization_bits = smash_config["weight_bits"]
        group_size = smash_config["group_size"]

        quant_config_hqq = imported_modules["BaseQuantizeConfig"](nbits=weight_quantization_bits, group_size=group_size)
        quant_config_hf = imported_modules["HqqConfig"](nbits=weight_quantization_bits, group_size=group_size)

        try:  # Try to quantize the model using HF specific HQQ quantization which supports specific layers
            # Create a temporary directory in a specific location
            base_temp_dir = smash_config["cache_dir"]
            temp_dir = tempfile.mkdtemp(dir=base_temp_dir)
            model.save_pretrained(temp_dir)

            smashed_model = AutoModelForCausalLM.from_pretrained(
                temp_dir,
                quantization_config=quant_config_hf,
                trust_remote_code=True,
            )
            try:
                smashed_model = smashed_model.to(smash_config["device"])
            except Exception as e:
                pruna_logger.error(f"Error casting model to device: {e}")

            # Delete the temporary directory and its contents
            shutil.rmtree(temp_dir)
        except Exception as e:  # Default to generic HQQ quantization if it fails
            pruna_logger.error(f"Error: {e}")
            smashed_model = imported_modules["AutoHQQHFModel"].quantize_model(
                model, quant_config=quant_config_hqq, device=smash_config["device"]
            )

        # Prepare the model for fast inference
        try:
            if weight_quantization_bits == 4:
                imported_modules["prepare_for_inference"](model, backend=smash_config["backend"])
        except Exception as e:
            pruna_logger.error(f"Error: {e}")
            pass

        return smashed_model

    def import_algorithm_packages(self) -> Dict[str, Any]:
        """
        Provide a algorithm packages for the algorithm.

        Returns
        -------
        Dict[str, Any]
            The algorithm packages.
        """
        try:
            with SuppressOutput():
                from hqq.core.quantize import BaseQuantizeConfig
                from hqq.models.hf.base import AutoHQQHFModel
                from hqq.utils.patching import prepare_for_inference
        except ImportError:
            pruna_logger.error(
                "You are trying to use the HQQ quantizer, but hqq is not installed. "
                "This is likely because you did not install hqq; try pip install hqq."
            )
            raise

        return dict(
            BaseQuantizeConfig=BaseQuantizeConfig,
            AutoHQQHFModel=AutoHQQHFModel,
            prepare_for_inference=prepare_for_inference,
            HqqConfig=HqqConfig,
        )
