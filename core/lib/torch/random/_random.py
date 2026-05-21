import numpy as np
import torch


@torch.jit.script
def _jit_trandom(x):
    x ^= x >> 16
    x *= 0x7FEB352D
    x ^= x >> 15
    x *= 0x846CA68B
    x ^= x >> 16
    x = x.type(torch.float32)
    x /= 0x7FFFFFFF
    return x


def uniform(shape, seed: int, device):
    """uniform float32 by seed"""
    x = torch.arange(np.prod(shape), dtype=torch.int32, device=device) + seed
    x = _jit_trandom(x).reshape(shape)
    return x
