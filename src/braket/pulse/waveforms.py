# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

from __future__ import annotations

import random
import string
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Dict, List, Optional, Union

import numpy as np
from oqpy import WaveformVar, bool_, complex128, declare_waveform_generator, duration, float64
from oqpy.base import OQPyExpression

from braket.parametric.free_parameter import FreeParameter
from braket.parametric.free_parameter_expression import (
    FreeParameterExpression,
    subs_if_free_parameter,
)
from braket.parametric.parameterizable import Parameterizable


class WaveformDict(dict):
    def __init__(self, wf_dict: dict, pulse_sequence):
        for wf in wf_dict.values():
            wf._pulse_sequence = pulse_sequence
        super().__init__(wf_dict)
        self._pulse_sequence = pulse_sequence

    def __setitem__(self, key: str, value: Waveform):
        value = deepcopy(value)
        value._pulse_sequence = self._pulse_sequence
        super().__setitem__(key, value)


class Waveform(ABC):
    """
    A waveform is a time-dependent envelope that can be used to emit signals on an output port
    or receive signals from an input port. As such, when transmitting signals to the qubit, a
    frame determines time at which the waveform envelope is emitted, its carrier frequency, and
    it’s phase offset. When capturing signals from a qubit, at minimum a frame determines the
    time at which the signal is captured. See https://openqasm.com/language/openpulse.html#waveforms
    for more details.
    """

    def __init__(self) -> None:
        self._pulse_sequence = None

    def _modify_oqpy_waveform_var(self, key, value, type_=float64):
        if self._pulse_sequence is not None:
            self._pulse_sequence._program.undeclared_vars[self.id].init_expression.args[
                key
            ] = self._pulse_sequence._format_parameter_ast(value, type_)

    @abstractmethod
    def _to_oqpy_expression(self) -> OQPyExpression:
        """Returns an OQPyExpression defining this waveform."""

    @abstractmethod
    def sample(self, dt: float) -> np.ndarray:
        """Generates a sample of amplitudes for this Waveform based on the given time resolution.
        Args:
            dt (float): The time resolution.
        Returns:
            ndarray: The sample amplitudes for this waveform.
        """

    @staticmethod
    @abstractmethod
    def _from_calibration_schema(waveform_json: Dict) -> Waveform:
        """
        Parses a JSON input and returns the BDK waveform. See https://github.com/aws/amazon-braket-schemas-python/blob/main/src/braket/device_schema/pulse/native_gate_calibrations_v1.py#L104

        Args:
            waveform_json (Dict): A JSON object with the needed parameters for making the Waveform.

        Returns:
            Waveform: A Waveform object parsed from the supplied JSON.
        """  # noqa: E501


class ArbitraryWaveform(Waveform):
    """An arbitrary waveform with amplitudes at each timestep explicitly specified using
    an array."""

    def __init__(self, amplitudes: List[complex], id: Optional[str] = None):
        """
        Args:
            amplitudes (List[complex]): Array of complex values specifying the
                waveform amplitude at each timestep. The timestep is determined by the sampling rate
                of the frame to which waveform is applied to.
            id (Optional[str]): The identifier used for declaring this waveform. A random string of
                ascii characters is assigned by default.
        """
        self._amplitudes = list(amplitudes)
        self.id = id or _make_identifier_name()
        super().__init__()

    @property
    def amplitudes(self):
        return self._amplitudes

    @amplitudes.setter
    def amplitudes(self, value):
        self._amplitudes = value
        if self._pulse_sequence is not None:
            self._pulse_sequence._program.undeclared_vars[self.id].init_expression = value

    def __repr__(self) -> str:
        return f"ArbitraryWaveform('id': {self.id}, 'amplitudes': {self.amplitudes})"

    def __eq__(self, other):
        return isinstance(other, ArbitraryWaveform) and (self.amplitudes, self.id) == (
            other.amplitudes,
            other.id,
        )

    def _to_oqpy_expression(self) -> OQPyExpression:
        """Returns an OQPyExpression defining this waveform.
        Returns:
            OQPyExpression: The OQPyExpression.
        """
        return WaveformVar(init_expression=self.amplitudes, name=self.id)

    def sample(self, dt: float) -> np.ndarray:
        """Generates a sample of amplitudes for this Waveform based on the given time resolution.
        Args:
            dt (float): The time resolution.
        Returns:
            ndarray: The sample amplitudes for this waveform.
        """
        raise NotImplementedError

    @staticmethod
    def _from_calibration_schema(waveform_json: Dict) -> ArbitraryWaveform:
        wave_id = waveform_json["waveformId"]
        complex_amplitudes = [complex(i[0], i[1]) for i in waveform_json["amplitudes"]]
        return ArbitraryWaveform(complex_amplitudes, wave_id)


class ConstantWaveform(Waveform, Parameterizable):
    """A constant waveform which holds the supplied `iq` value as its amplitude for the
    specified length."""

    def __init__(
        self, length: Union[float, FreeParameterExpression], iq: complex, id: Optional[str] = None
    ):
        """
        Args:
            length (Union[float, FreeParameterExpression]): Value (in seconds)
                specifying the duration of the waveform.
            iq (complex): complex value specifying the amplitude of the waveform.
            id (Optional[str]): The identifier used for declaring this waveform. A random string of
                ascii characters is assigned by default.
        """
        self._length = length
        self._iq = iq
        self.id = id or _make_identifier_name()
        super().__init__()

    @property
    def iq(self):
        return self._iq

    @iq.setter
    def iq(self, value):
        self._iq = value
        self._modify_oqpy_waveform_var("iq", value)

    @property
    def length(self):
        return self._length

    @length.setter
    def length(self, value):
        self._length = value
        self._modify_oqpy_waveform_var("length", value, duration)

    def __repr__(self) -> str:
        return f"ConstantWaveform('id': {self.id}, 'length': {self.length}, 'iq': {self.iq})"

    @property
    def parameters(self) -> List[Union[FreeParameterExpression, FreeParameter, float]]:
        """Returns the parameters associated with the object, either unbound free parameter
        expressions or bound values."""
        return [self.length]

    def bind_values(self, **kwargs) -> ConstantWaveform:
        """Takes in parameters and returns an object with specified parameters
        replaced with their values.

        Returns:
            ConstantWaveform: A copy of this waveform with the requested parameters bound.
        """
        constructor_kwargs = {
            "length": subs_if_free_parameter(self.length, **kwargs),
            "iq": self.iq,
            "id": self.id,
        }
        return ConstantWaveform(**constructor_kwargs)

    def __eq__(self, other):
        return isinstance(other, ConstantWaveform) and (self.length, self.iq, self.id) == (
            other.length,
            other.iq,
            other.id,
        )

    def _to_oqpy_expression(self) -> OQPyExpression:
        """Returns an OQPyExpression defining this waveform.
        Returns:
            OQPyExpression: The OQPyExpression.
        """
        constant_generator = declare_waveform_generator(
            "constant", [("length", duration), ("iq", complex128)]
        )
        return WaveformVar(
            init_expression=constant_generator(_map_to_oqpy_type(self.length, True), self.iq),
            name=self.id,
        )

    def sample(self, dt: float) -> np.ndarray:
        """Generates a sample of amplitudes for this Waveform based on the given time resolution.
        Args:
            dt (float): The time resolution.
        Returns:
            ndarray: The sample amplitudes for this waveform.
        """
        # Amplitudes should be gated by [0:self.length]
        sample_range = np.arange(0, self.length, dt)
        samples = self.iq * np.ones_like(sample_range)
        return samples

    @staticmethod
    def _from_calibration_schema(waveform_json: Dict) -> ConstantWaveform:
        wave_id = waveform_json["waveformId"]
        length = iq = None
        for val in waveform_json["arguments"]:
            if val["name"] == "length":
                length = (
                    float(val["value"])
                    if val["type"] == "float"
                    else FreeParameterExpression(val["value"])
                )
            if val["name"] == "iq":
                iq = (
                    complex(val["value"])
                    if val["type"] == "complex"
                    else FreeParameterExpression(val["value"])
                )
        return ConstantWaveform(length=length, iq=iq, id=wave_id)


class DragGaussianWaveform(Waveform, Parameterizable):
    """A gaussian waveform with an additional gaussian derivative component and lifting applied."""

    def __init__(
        self,
        length: Union[float, FreeParameterExpression],
        sigma: Union[float, FreeParameterExpression],
        beta: Union[float, FreeParameterExpression],
        amplitude: Union[float, FreeParameterExpression] = 1,
        zero_at_edges: bool = False,
        id: Optional[str] = None,
    ):
        """
        Args:
            length (Union[float, FreeParameterExpression]): Value (in seconds)
                specifying the duration of the waveform.
            sigma (Union[float, FreeParameterExpression]): A measure (in seconds) of
                how wide or narrow the Gaussian peak is.
            beta (Union[float, FreeParameterExpression]): The correction amplitude.
            amplitude (Union[float, FreeParameterExpression]): The amplitude of the
                waveform envelope. Defaults to 1.
            zero_at_edges (bool): bool specifying whether the waveform amplitude is clipped to
                zero at the edges. Defaults to False.
            id (Optional[str]): The identifier used for declaring this waveform. A random string of
                ascii characters is assigned by default.
        """
        self._length = length
        self._sigma = sigma
        self._beta = beta
        self._amplitude = amplitude
        self._zero_at_edges = zero_at_edges
        self.id = id or _make_identifier_name()
        super().__init__()

    @property
    def length(self):
        return self._length

    @length.setter
    def length(self, value):
        self._length = value
        self._modify_oqpy_waveform_var("length", value, duration)

    @property
    def sigma(self):
        return self._sigma

    @sigma.setter
    def sigma(self, value):
        self._sigma = value
        self._modify_oqpy_waveform_var("sigma", value, duration)

    @property
    def beta(self):
        return self._beta

    @beta.setter
    def beta(self, value):
        self._beta = value
        self._modify_oqpy_waveform_var("beta", value)

    @property
    def amplitude(self):
        return self._amplitude

    @amplitude.setter
    def amplitude(self, value):
        self._amplitude = value
        self._modify_oqpy_waveform_var("amplitude", value)

    @property
    def zero_at_edges(self):
        return self._zero_at_edges

    @zero_at_edges.setter
    def zero_at_edges(self, value):
        self._zero_at_edges = value
        self._modify_oqpy_waveform_var("zero_at_edges", value)

    def __repr__(self) -> str:
        return (
            f"DragGaussianWaveform('id': {self.id}, 'length': {self.length}, "
            f"'sigma': {self.sigma}, 'beta': {self.beta}, 'amplitude': {self.amplitude}, "
            f"'zero_at_edges': {self.zero_at_edges})"
        )

    @property
    def parameters(self) -> List[Union[FreeParameterExpression, FreeParameter, float]]:
        """Returns the parameters associated with the object, either unbound free parameter
        expressions or bound values."""
        return [self.length, self.sigma, self.beta, self.amplitude]

    def bind_values(self, **kwargs) -> DragGaussianWaveform:
        """Takes in parameters and returns an object with specified parameters
        replaced with their values.

        Returns:
            DragGaussianWaveform: A copy of this waveform with the requested parameters bound.
        """
        constructor_kwargs = {
            "length": subs_if_free_parameter(self.length, **kwargs),
            "sigma": subs_if_free_parameter(self.sigma, **kwargs),
            "beta": subs_if_free_parameter(self.beta, **kwargs),
            "amplitude": subs_if_free_parameter(self.amplitude, **kwargs),
            "zero_at_edges": self.zero_at_edges,
            "id": self.id,
        }
        return DragGaussianWaveform(**constructor_kwargs)

    def __eq__(self, other):
        return isinstance(other, DragGaussianWaveform) and (
            self.length,
            self.sigma,
            self.beta,
            self.amplitude,
            self.zero_at_edges,
            self.id,
        ) == (other.length, other.sigma, other.beta, other.amplitude, other.zero_at_edges, other.id)

    def _to_oqpy_expression(self) -> OQPyExpression:
        """Returns an OQPyExpression defining this waveform.
        Returns:
            OQPyExpression: The OQPyExpression.
        """
        drag_gaussian_generator = declare_waveform_generator(
            "drag_gaussian",
            [
                ("length", duration),
                ("sigma", duration),
                ("beta", float64),
                ("amplitude", float64),
                ("zero_at_edges", bool_),
            ],
        )
        return WaveformVar(
            init_expression=drag_gaussian_generator(
                _map_to_oqpy_type(self.length, True),
                _map_to_oqpy_type(self.sigma, True),
                _map_to_oqpy_type(self.beta),
                _map_to_oqpy_type(self.amplitude),
                self.zero_at_edges,
            ),
            name=self.id,
        )

    def sample(self, dt: float) -> np.ndarray:
        """Generates a sample of amplitudes for this Waveform based on the given time resolution.
        Args:
            dt (float): The time resolution.
        Returns:
            ndarray: The sample amplitudes for this waveform.
        """
        sample_range = np.arange(0, self.length, dt)
        t0 = self.length / 2
        zero_at_edges_int = int(self.zero_at_edges)
        samples = (
            (1 - (1.0j * self.beta * ((sample_range - t0) / self.sigma**2)))
            * (
                self.amplitude
                / (1 - zero_at_edges_int * np.exp(-0.5 * ((self.length / (2 * self.sigma)) ** 2)))
            )
            * (
                np.exp(-0.5 * (((sample_range - t0) / self.sigma) ** 2))
                - zero_at_edges_int * np.exp(-0.5 * ((self.length / (2 * self.sigma)) ** 2))
            )
        )
        return samples

    @staticmethod
    def _from_calibration_schema(waveform_json: Dict) -> DragGaussianWaveform:
        waveform_parameters = {"id": waveform_json["waveformId"]}
        for val in waveform_json["arguments"]:
            waveform_parameters[val["name"]] = (
                float(val["value"])
                if val["type"] == "float"
                else FreeParameterExpression(val["value"])
            )
        return DragGaussianWaveform(**waveform_parameters)


class GaussianWaveform(Waveform, Parameterizable):
    """A waveform with amplitudes following a gaussian distribution for the specified parameters."""

    def __init__(
        self,
        length: Union[float, FreeParameterExpression],
        sigma: Union[float, FreeParameterExpression],
        amplitude: Union[float, FreeParameterExpression] = 1,
        zero_at_edges: bool = False,
        id: Optional[str] = None,
    ):
        """
        Args:
            length (Union[float, FreeParameterExpression]): Value (in seconds) specifying the
                duration of the waveform.
            sigma (Union[float, FreeParameterExpression]): A measure (in seconds) of how wide
                or narrow the Gaussian peak is.
            amplitude (Union[float, FreeParameterExpression]): The amplitude of the waveform
                envelope. Defaults to 1.
            zero_at_edges (bool): bool specifying whether the waveform amplitude is clipped to
                zero at the edges. Defaults to False.
            id (Optional[str]): The identifier used for declaring this waveform. A random string of
                ascii characters is assigned by default.
        """
        self._length = length
        self._sigma = sigma
        self._amplitude = amplitude
        self._zero_at_edges = zero_at_edges
        self.id = id or _make_identifier_name()
        super().__init__()

    @property
    def length(self):
        return self._length

    @length.setter
    def length(self, value):
        self._length = value
        self._modify_oqpy_waveform_var("length", value, duration)

    @property
    def sigma(self):
        return self._sigma

    @sigma.setter
    def sigma(self, value):
        self._sigma = value
        self._modify_oqpy_waveform_var("sigma", value, duration)

    @property
    def amplitude(self):
        return self._amplitude

    @amplitude.setter
    def amplitude(self, value):
        self._amplitude = value
        self._modify_oqpy_waveform_var("amplitude", value)

    @property
    def zero_at_edges(self):
        return self._zero_at_edges

    @zero_at_edges.setter
    def zero_at_edges(self, value):
        self._zero_at_edges = value
        if self._pulse_sequence is not None:
            self._pulse_sequence._program.undeclared_vars[self.id].init_expression.args[
                "zero_at_edges"
            ] = value

    def __repr__(self) -> str:
        return (
            f"GaussianWaveform('id': {self.id}, 'length': {self.length}, 'sigma': {self.sigma}, "
            f"'amplitude': {self.amplitude}, 'zero_at_edges': {self.zero_at_edges})"
        )

    @property
    def parameters(self) -> List[Union[FreeParameterExpression, FreeParameter, float]]:
        """Returns the parameters associated with the object, either unbound free parameter
        expressions or bound values."""
        return [self.length, self.sigma, self.amplitude]

    def bind_values(self, **kwargs) -> GaussianWaveform:
        """Takes in parameters and returns an object with specified parameters
        replaced with their values.

        Returns:
            GaussianWaveform: A copy of this waveform with the requested parameters bound.
        """
        constructor_kwargs = {
            "length": subs_if_free_parameter(self.length, **kwargs),
            "sigma": subs_if_free_parameter(self.sigma, **kwargs),
            "amplitude": subs_if_free_parameter(self.amplitude, **kwargs),
            "zero_at_edges": self.zero_at_edges,
            "id": self.id,
        }
        return GaussianWaveform(**constructor_kwargs)

    def __eq__(self, other):
        return isinstance(other, GaussianWaveform) and (
            self.length,
            self.sigma,
            self.amplitude,
            self.zero_at_edges,
            self.id,
        ) == (other.length, other.sigma, other.amplitude, other.zero_at_edges, other.id)

    def _to_oqpy_expression(self) -> OQPyExpression:
        """Returns an OQPyExpression defining this waveform.
        Returns:
            OQPyExpression: The OQPyExpression.
        """
        gaussian_generator = declare_waveform_generator(
            "gaussian",
            [
                ("length", duration),
                ("sigma", duration),
                ("amplitude", float64),
                ("zero_at_edges", bool_),
            ],
        )
        return WaveformVar(
            init_expression=gaussian_generator(
                _map_to_oqpy_type(self.length, True),
                _map_to_oqpy_type(self.sigma, True),
                _map_to_oqpy_type(self.amplitude),
                self.zero_at_edges,
            ),
            name=self.id,
        )

    def sample(self, dt: float) -> np.ndarray:
        """Generates a sample of amplitudes for this Waveform based on the given time resolution.
        Args:
            dt (float): The time resolution.
        Returns:
            ndarray: The sample amplitudes for this waveform.
        """
        sample_range = np.arange(0, self.length, dt)
        t0 = self.length / 2
        zero_at_edges_int = int(self.zero_at_edges)
        samples = (
            self.amplitude
            / (1 - zero_at_edges_int * np.exp(-0.5 * ((self.length / (2 * self.sigma)) ** 2)))
        ) * (
            np.exp(-0.5 * (((sample_range - t0) / self.sigma) ** 2))
            - zero_at_edges_int * np.exp(-0.5 * ((self.length / (2 * self.sigma)) ** 2))
        )
        return samples

    @staticmethod
    def _from_calibration_schema(waveform_json: Dict) -> GaussianWaveform:
        waveform_parameters = {"id": waveform_json["waveformId"]}
        for val in waveform_json["arguments"]:
            waveform_parameters[val["name"]] = (
                float(val["value"])
                if val["type"] == "float"
                else FreeParameterExpression(val["value"])
            )
        return GaussianWaveform(**waveform_parameters)


def _make_identifier_name() -> str:
    return "".join([random.choice(string.ascii_letters) for _ in range(10)])


def _map_to_oqpy_type(
    parameter: Union[FreeParameterExpression, float], is_duration_type: bool = False
) -> Union[FreeParameterExpression, OQPyExpression]:
    return (
        FreeParameterExpression(parameter, duration)
        if isinstance(parameter, FreeParameterExpression) and is_duration_type
        else parameter
    )


def _parse_waveform_from_calibration_schema(waveform: Dict) -> Waveform:
    waveform_names = {
        "arbitrary": ArbitraryWaveform._from_calibration_schema,
        "drag_gaussian": DragGaussianWaveform._from_calibration_schema,
        "gaussian": GaussianWaveform._from_calibration_schema,
        "constant": ConstantWaveform._from_calibration_schema,
    }
    if "amplitudes" in waveform.keys():
        waveform["name"] = "arbitrary"
    if waveform["name"] in waveform_names:
        return waveform_names[waveform["name"]](waveform)
    else:
        id = waveform["waveformId"]
        raise ValueError(f"The waveform {id} of cannot be constructed")
