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

from typing import Any, Dict

import torch
from ConfigSpace import Constant, OrdinalHyperparameter
from transformers import AutomaticSpeechRecognitionPipeline, pipeline
from transformers.utils import is_flash_attn_2_available

from pruna.algorithms.batching import PrunaBatcher
from pruna.algorithms.compilation.c_translate import WhisperWrapper
from pruna.config.smash_config import SmashConfigPrefixWrapper


class IFWBatcher(PrunaBatcher):
    """
    Implement IFW processing using huggingface transformers.

    Insanely Fast Whisper is an optimized version of Whisper models that significantly speeds up transcription.
    It achieves lower latency and higher throughput through low-level code optimizations and efficient batching,
    making real-time speech recognition more practical.
    """

    algorithm_name = "ifw"
    references = {"GitHub": "https://github.com/huggingface/transformers"}
    tokenizer_required = True
    processor_required = True
    run_on_cpu = False
    run_on_cuda = True
    dataset_required = False
    compatible_algorithms = dict(quantizer=["half"])

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
                sequence=[16, 32],
                default_value=16,
                meta=dict(desc="Sets the number of bits to use for weight quantization."),
            ),
            OrdinalHyperparameter(
                "batch_size",
                sequence=[1, 2, 4, 8, 16, 32, 64],
                default_value=16,
                meta=dict(desc="The batch size to use for inference. Higher is faster but needs more memory."),
            ),
            Constant(name="chunk_length", value=30),
        ]

    def model_check_fn(self, model: Any) -> bool:
        """
        Check if the model is a valid model for the algorithm.

        Parameters
        ----------
        model : Any
            The model to check.

        Returns
        -------
        bool
            True if the model is a valid model for the algorithm, False otherwise.
        """
        if isinstance(model, WhisperWrapper):
            return True
        if isinstance(model, AutomaticSpeechRecognitionPipeline):
            return True
        try:
            # requirement is that the model is compatible with the asr pipeline
            pipeline("automatic-speech-recognition", model=model)
            return True
        except Exception:
            return False

    def _apply(self, model: Any, smash_config: SmashConfigPrefixWrapper) -> Any:
        """
        Apply the IFW batcher to the model.

        Parameters
        ----------
        model : Any
            The model to apply the IFW batcher to.
        smash_config : SmashConfigPrefixWrapper
            The configuration for the batching.

        Returns
        -------
        Any
            The smashed model.
        """
        if isinstance(model, WhisperWrapper):
            model = model.whisper
        elif isinstance(model, AutomaticSpeechRecognitionPipeline):
            model = model.model

        torch_dtype = torch.float16 if smash_config["weight_bits"] == 16 else torch.float32

        # ignore mypy warnings here because we ensure beforehand that processor is not None
        pipe = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=smash_config.processor.tokenizer,  # type: ignore[attr-defined]
            feature_extractor=smash_config.processor.feature_extractor,  # type: ignore[attr-defined]
            chunk_length_s=smash_config["chunk_length"],
            batch_size=smash_config["batch_size"],
            torch_dtype=torch_dtype,
            model_kwargs=(
                {"attn_implementation": "flash_attention_2"}
                if is_flash_attn_2_available()
                else {"attn_implementation": "sdpa"}
            ),
            device=smash_config["device"],
        )
        return pipe

    def import_algorithm_packages(self) -> Dict[str, Any]:
        """
        Import the algorithm packages.

        Returns
        -------
        Dict[str, Any]
            The algorithm packages.
        """
        return dict()
