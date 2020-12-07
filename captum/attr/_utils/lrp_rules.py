#!/usr/bin/env python3

from abc import ABC, abstractmethod
from typing import Tuple, Callable

import torch

from ..._utils.common import _format_tensor_into_tuples
from ..._utils.typing import TensorOrTupleOfTensorsGeneric, Tensor, Module


class PropagationRule(ABC):
    """
    Base class for all propagation rule classes, also called Z-Rule.
    STABILITY_FACTOR is used to assure that no zero divison occurs.
    """

    STABILITY_FACTOR = 1e-9

    def forward_hook(
        self, module: Module, inputs: TensorOrTupleOfTensorsGeneric, outputs: Tensor
    ) -> Tensor:
        """Register backward hooks on input and output
        tensors of linear layers in the model."""
        inputs = _format_tensor_into_tuples(inputs)
        self._has_single_input = len(inputs) == 1
        self._handle_input_hooks = []
        self.relevance_input = []
        for input in inputs:
            if not hasattr(input, "hook_registered"):
                input_hook = self._create_backward_hook_input(input.data)
                self._handle_input_hooks.append(input.register_hook(input_hook))
                input.hook_registered = True
        output_hook = self._create_backward_hook_output(outputs.data)
        self._handle_output_hook = outputs.register_hook(output_hook)
        return outputs.clone()

    @staticmethod
    def backward_hook_activation(
        module: Module, grad_input: Tensor, grad_output: Tensor
    ) -> Tensor:
        """Backward hook to propagate relevance over non-linear activations."""
        return grad_output

    def _create_backward_hook_input(self, inputs: Tensor) -> Callable[[Tensor], Tensor]:
        def _backward_hook_input(grad: Tensor) -> Tensor:
            relevance = grad * inputs
            if self._has_single_input:
                self.relevance_input = relevance.data
            else:
                self.relevance_input.append(relevance.data)
            return relevance

        return _backward_hook_input

    def _create_backward_hook_output(
        self, outputs: Tensor
    ) -> Callable[[Tensor], Tensor]:
        def _backward_hook_output(grad: Tensor) -> Tensor:
            sign = torch.sign(outputs)
            sign[sign == 0] = 1
            relevance = grad / (outputs + sign * self.STABILITY_FACTOR)
            self.relevance_output = grad.data
            return relevance

        return _backward_hook_output

    def forward_hook_weights(
        self,
        module: Module,
        inputs: Tuple[Tensor, ...],
        outputs: TensorOrTupleOfTensorsGeneric,
    ) -> None:
        """Save initial activations a_j before modules are changed"""
        module.activations = tuple(input.data for input in inputs)
        self._manipulate_weights(module, inputs, outputs)

    @abstractmethod
    def _manipulate_weights(
        self,
        module: Module,
        inputs: TensorOrTupleOfTensorsGeneric,
        outputs: TensorOrTupleOfTensorsGeneric,
    ) -> None:
        raise NotImplementedError

    def forward_pre_hook_activations(self, module: Module, inputs: Tuple[Tensor, ...]):
        """Pass initial activations to graph generation pass"""
        for input, activation in zip(inputs, module.activations):
            input.data = activation
        return inputs


class EpsilonRule(PropagationRule):
    """
    Rule for relevance propagation using a small value of epsilon
    to avoid numerical instabilities and remove noise.

    Use for middle layers.

    Args:
        epsilon (integer, float): Value by which is added to the
        discriminator during propagation.
    """

    def __init__(self, epsilon: float = 1e-9) -> None:
        self.STABILITY_FACTOR = epsilon

    def _manipulate_weights(
        self,
        module: Module,
        inputs: TensorOrTupleOfTensorsGeneric,
        outputs: TensorOrTupleOfTensorsGeneric,
    ) -> None:
        pass


class GammaRule(PropagationRule):
    """
    Gamma rule for relevance propagation, gives more importance to
    positive relevance.

    Use for lower layers.

    Args:
        gamma (float): The gamma parameter determines by how much
        the positive relevance is increased.
    """

    def __init__(self, gamma: float = 0.25, set_bias_to_zero: bool = False) -> None:
        self.gamma = gamma
        self.set_bias_to_zero = set_bias_to_zero

    def _manipulate_weights(
        self,
        module: Module,
        inputs: TensorOrTupleOfTensorsGeneric,
        outputs: TensorOrTupleOfTensorsGeneric,
    ) -> None:
        if hasattr(module, "weight"):
            module.weight.data = (
                module.weight.data + self.gamma * module.weight.data.clamp(min=0)
            )
        if self.set_bias_to_zero and hasattr(module, "bias"):
            if module.bias is not None:
                module.bias.data = torch.zeros_like(module.bias.data)


class ZPlusRule(PropagationRule):
    """
    Z^+ rule for relevance backpropagation closely related to
    Deep-Taylor Decomposition cf. https://doi.org/10.1016/j.patcog.2016.11.008.
    Only positive relevance is propagated, resulting in stable results,
    therefore recommended as the initial choice.

    Warning: Does not work for BatchNorm modules because weight and bias
    are defined differently.

    Use for lower layers.
    """

    def __init__(self, set_bias_to_zero: bool = False) -> None:
        self.set_bias_to_zero = set_bias_to_zero

    def _manipulate_weights(
        self,
        module: Module,
        inputs: TensorOrTupleOfTensorsGeneric,
        outputs: TensorOrTupleOfTensorsGeneric,
    ) -> None:
        if hasattr(module, "weight"):
            module.weight.data = module.weight.data.clamp(min=0)
        if self.set_bias_to_zero and hasattr(module, "bias"):
            if module.bias is not None:
                module.bias.data = torch.zeros_like(module.bias.data)


class IdentityRule(EpsilonRule):
    """
    Identity rule for skipping layer manipulation and propagating the
    relevance over a layer. Only valid for modules with same dimensions for
    inputs and outputs.

    Can be used for BatchNorm2D.
    """

    def _create_backward_hook_input(self, inputs: Tensor) -> Callable[[Tensor], Tensor]:
        def _backward_hook_input(grad: Tensor) -> Tensor:
            return self.relevance_output

        return _backward_hook_input
