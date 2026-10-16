"""PyTorch's variable-length attention as the runtime wraps it, against SDPA per sequence."""

from types import SimpleNamespace

import pytest
import torch
import torch.nn.functional as F

pytestmark = pytest.mark.gpu


def test_packed_attention_with_fewer_key_heads_matches_sdpa():
    if not torch.cuda.is_available() or torch.version.hip:
        pytest.skip("needs a CUDA GPU")
    varlen = pytest.importorskip("torch.nn.attention.varlen")
    from vllm_srun.accel.gpu import _wrap_varlen

    attend = _wrap_varlen(varlen.varlen_attn)
    torch.manual_seed(0)
    heads, key_heads, size, lengths = (
        24,
        4,
        256,
        [37, 300, 5],
    )  # d3's full-attention layers
    total = sum(lengths)
    q = torch.randn(total, heads, size, device="cuda", dtype=torch.bfloat16)
    k, v = (
        torch.randn(total, key_heads, size, device="cuda", dtype=torch.bfloat16)
        for _ in range(2)
    )
    starts = torch.tensor(
        [0, *torch.cumsum(torch.tensor(lengths), 0)], dtype=torch.int32
    )
    packed = SimpleNamespace(cu_seqlens_int32=starts.cuda(), max_length=max(lengths))
    got = attend(q, k, v, packed, scale=size**-0.5, groups=heads // key_heads)
    want = []
    for start, length in zip(starts.tolist(), lengths):
        rows = [t[start : start + length].transpose(0, 1)[None] for t in (q, k, v)]
        out = F.scaled_dot_product_attention(*rows, scale=size**-0.5, enable_gqa=True)
        want.append(out[0].transpose(0, 1))
    want = torch.cat(want).float()
    # two BF16 attention kernels: only their summation order separates them
    assert ((got.float() - want).norm() / want.norm()) < 5e-3
