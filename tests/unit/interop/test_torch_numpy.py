"""Smoke tests for the locked numerical stack (torch + numpy).

torch 2.3 wheels are built against the numpy 1 ABI. With numpy 2 installed,
conversions such as ``torch.as_tensor(ndarray)`` warn or fail at runtime, which
is why requirements.in pins numpy<2. These tests fail fast if the lock drifts.
"""

from __future__ import annotations

import unittest

import numpy
import torch


class TorchNumpyInteropTest(unittest.TestCase):
    def test_round_trip(self):
        array = torch.as_tensor(numpy.zeros(3)).numpy()
        self.assertIsInstance(array, numpy.ndarray)
        self.assertEqual(array.shape, (3,))
        self.assertEqual(array.dtype, numpy.float64)
        self.assertTrue((array == 0).all())

    def test_from_numpy_shares_memory(self):
        array = numpy.arange(4.0)
        tensor = torch.from_numpy(array)
        tensor[0] = 5.0
        self.assertEqual(array[0], 5.0)

    def test_autograd(self):
        x = torch.tensor([1.0, 2.0, 3.0], requires_grad=True)
        (x**2).sum().backward()
        self.assertEqual(x.grad.tolist(), [2.0, 4.0, 6.0])

    def test_numpy_is_1x(self):
        self.assertEqual(numpy.__version__.split(".")[0], "1", numpy.__version__)

    def test_torch_is_cpu_only_build(self):
        # The lock must stay CUDA-free (see requirements.in).
        self.assertIsNone(torch.version.cuda)


if __name__ == "__main__":
    unittest.main()
