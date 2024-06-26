"""
    Utilities for exporting a model to our custom format.
"""

import numpy as np
import torch

from tensorrt_llm._utils import torch_to_numpy


def cpu_map_location(storage, loc):
    return storage.cpu()


def gpu_map_location(storage, loc):
    if loc.startswith("cuda"):
        training_gpu_idx = int(loc.split(":")[1])
        inference_gpu_idx = training_gpu_idx % torch.cuda.device_count()
        return storage.cuda(inference_gpu_idx)
    elif loc.startswith("cpu"):
        return storage.cpu()
    else:
        raise ValueError(f"Not handled {loc}")


def save_val(val, dir, key, tp_num=None):
    suffix = "bin" if tp_num is None else f"{tp_num}.bin"
    val.tofile(dir / f"model.{key}.{suffix}")


def save_split(split_vals, dir, key, i, split_factor):
    for j, val in enumerate(split_vals):
        save_val(val, dir, key, i * split_factor + j)


def generate_int8(weights, act_range, is_qkv=False, multi_query_mode=False):
    """
     This function has two purposes:
      - compute quantized weights, scaled either per-tensor or per-column
      - compute scaling factors

      Depending on the GEMM API (CUTLASS/CUBLAS) the required scaling factors differ.
      CUTLASS uses two sets of scaling factors. One for the activation X, one for the weight W.
      CUBLAS only has one (we can't do per-row scaling). So we must provide pre-multiplied scaling factor.

      Here is the list of what we need (T means per-tensor, C per-column):
        - scale_x_orig_quant puts fp activation into the quantized range (i.e. [-128, 127], for int8). Used before the GEMM. (T)
        - scale_y_quant_orig puts quantized activation into the fp range. Used if the GEMM outputs int8. (T)
        - scale_w_quant_orig puts weights from quant range to fp range (used with CUTLASS) (T, C)
        - scale_y_accum_quant puts the GEMM result (XW) from accumulation range (int32)
          to quant range (int8) (used for CUBLAS) (T, C)

      Note that we don't do anything special about row-parallel GEMM. Theoretically, we could have per-GPU scaling factors too,
      but then the model would change depending on the number of GPUs used.

      For QKV projection, the behavior is special. Even if we have a single matrix to perform QKV projection, we consider it
      as three different matrices: Q, K, and V. So per-tensor actually means one scaling factor for each Q, K and V.
    """

    # compute weight scaling factors for fp->int8 and int8->fp
    if is_qkv and not multi_query_mode:
        scale_w_orig_quant_t = 127. / act_range["w"].reshape(3, -1).max(
            dim=-1, keepdims=True)[0].cpu().numpy().astype(np.float32)
        scale_w_orig_quant_c = 127. / act_range["w"].reshape(3,-1).cpu().numpy().astype(np.float32)
    elif is_qkv and multi_query_mode:
        hidden_dim = weights.shape[0]
        local_dim = act_range["w"].shape[0]
        kv_dim = (local_dim - hidden_dim) // 2
        scale_w_q = act_range["w"][0:hidden_dim]
        scale_w_k = act_range["w"][hidden_dim:hidden_dim + kv_dim]
        scale_w_v = act_range["w"][-kv_dim:]

        scale_w_qkv_t = torch.concat([
            scale_w_q.max(dim=0, keepdim=True)[0],
            scale_w_k.max(dim=0, keepdim=True)[0],
            scale_w_v.max(dim=0, keepdim=True)[0]
        ])

        scale_w_orig_quant_t = 127. / scale_w_qkv_t.cpu().numpy()
        scale_w_orig_quant_c = 127. / act_range["w"].cpu().numpy()
    else:
        scale_w_orig_quant_t = 127. / act_range["w"].max().cpu().numpy().astype(np.float32)
        scale_w_orig_quant_c = 127. / act_range["w"].cpu().numpy().astype(np.float32)
    scale_w_quant_orig_t = 1.0 / scale_w_orig_quant_t
    scale_w_quant_orig_c = 1.0 / scale_w_orig_quant_c

    # compute the rest of needed scaling factors
    scale_x_orig_quant_t = np.array(127. / act_range["x"].max().item())
    scale_y_orig_quant_t = np.array(127. / act_range["y"].max().item())
    scale_y_quant_orig_t = np.array(act_range["y"].max().item() / 127.)
    scale_y_accum_quant_t = scale_y_orig_quant_t / (scale_x_orig_quant_t *
                                                    scale_w_orig_quant_t)
    scale_y_accum_quant_c = scale_y_orig_quant_t / (scale_x_orig_quant_t *
                                                    scale_w_orig_quant_c)
    if is_qkv and not multi_query_mode:
        scale_y_accum_quant_t = np.broadcast_to(scale_y_accum_quant_t,
                                                scale_w_orig_quant_c.shape)
        scale_w_quant_orig_t = np.broadcast_to(scale_w_quant_orig_t,
                                               scale_w_orig_quant_c.shape)
    if is_qkv and multi_query_mode:
        scale_q_y_accum_t = np.broadcast_to(scale_y_accum_quant_t[0],
                                            scale_w_q.shape)
        scale_k_y_accum_t = np.broadcast_to(scale_y_accum_quant_t[1],
                                            scale_w_k.shape)
        scale_v_y_accum_t = np.broadcast_to(scale_y_accum_quant_t[2],
                                            scale_w_v.shape)
        scale_y_accum_quant_t = np.concatenate(
            [scale_q_y_accum_t, scale_k_y_accum_t, scale_v_y_accum_t])
        scale_w_quant_orig_t = np.concatenate([
            np.broadcast_to(scale_w_quant_orig_t[0], scale_w_q.shape),
            np.broadcast_to(scale_w_quant_orig_t[1], scale_w_k.shape),
            np.broadcast_to(scale_w_quant_orig_t[2], scale_w_v.shape)
        ])

    to_i8 = lambda x: x.round().clip(-127, 127).astype(np.int8)
    if is_qkv and multi_query_mode:
        weight_int8 = to_i8(weights / scale_w_quant_orig_t)
    else:
        weight_int8 = to_i8(weights * scale_w_orig_quant_t)
    return {
        "weight.int8": weight_int8,
        "weight.int8.col": to_i8(weights * scale_w_orig_quant_c),
        "scale_x_orig_quant": scale_x_orig_quant_t.astype(np.float32),
        "scale_w_quant_orig": scale_w_quant_orig_t.astype(np.float32),
        "scale_w_quant_orig.col": scale_w_quant_orig_c.astype(np.float32),
        "scale_y_accum_quant": scale_y_accum_quant_t.astype(np.float32),
        "scale_y_accum_quant.col": scale_y_accum_quant_c.astype(np.float32),
        "scale_y_quant_orig": scale_y_quant_orig_t.astype(np.float32),
    }


def write_qkv_int8(
    vals,
    dir,
    base_key,
    split_dim,
    tp_rank,
    split_factor,
    kv_cache_only=False,
    multi_query_mode=False
):
    int8_weight = vals["weight.int8"]
    local_dim = int8_weight.shape[0]
    head_size = (int8_weight.shape[-1] - local_dim) // 2
    def multi_query_split(data, tp_size, axis=-1):
        q, k, v = np.split(data, [local_dim, local_dim + head_size], axis=axis)
        q_split = np.split(q, tp_size, axis=axis)
        k_split = np.split(k, tp_size, axis=axis)
        v_split = np.split(v, tp_size, axis=axis)
        return [
            np.concatenate((q_split[ii], k_split[ii], v_split[ii]), axis=axis)
            for ii in range(tp_size)
        ]
    
    if not kv_cache_only:
        if not multi_query_mode:
            split_vals_int8 = np.split(vals["weight.int8"], split_factor, axis=split_dim)
            split_vals_int8_col = np.split(vals["weight.int8.col"], split_factor, axis=split_dim)
        else:
            split_vals_int8 = multi_query_split(vals["weight.int8"], split_factor, axis=split_dim)
            split_vals_int8_col = multi_query_split(vals["weight.int8.col"], split_factor, axis=split_dim)
        save_split(split_vals_int8, dir, f"{base_key}.weight.int8", tp_rank, split_factor)
        save_split(split_vals_int8_col, dir, f"{base_key}.weight.int8.col", tp_rank, split_factor)

    saved_keys_once = ["scale_y_quant_orig"]
    if not kv_cache_only:
        saved_keys_once += [
            "scale_x_orig_quant", "scale_w_quant_orig", "scale_y_accum_quant"
        ]
    # per-column scaling factors are loaded per-gpu for ColumnParallel GEMMs (QKV, FC1)
    if not kv_cache_only:
        if split_dim == -1:
            if not multi_query_mode:
                split_vals_orig = np.split(
                    vals["scale_w_quant_orig.col"], split_factor, axis=split_dim
                )
                split_vals_accum = np.split(
                    vals["scale_y_accum_quant.col"], split_factor, axis=split_dim
                )
            else:
                split_vals_orig = multi_query_split(
                    vals["scale_w_quant_orig.col"], split_factor, axis=split_dim
                )
                split_vals_accum = multi_query_split(
                    vals["scale_y_accum_quant.col"], split_factor, axis=split_dim
                )
                
            save_split(
                split_vals_orig,
                dir,
                f"{base_key}.scale_w_quant_orig.col", tp_rank, split_factor)
            
            save_split(
                split_vals_accum,
                dir,
                f"{base_key}.scale_y_accum_quant.col", tp_rank, split_factor)
        else:
            saved_keys_once += [
                "scale_w_quant_orig.col", "scale_y_accum_quant.col"
            ]

    if tp_rank == 0:
        for save_key in saved_keys_once:
            save_val(vals[save_key], dir, f"{base_key}.{save_key}")


def write_int8(vals,
               dir,
               base_key,
               split_dim,
               tp_rank,
               split_factor,
               kv_cache_only=False):
    if not kv_cache_only:
        split_vals = np.split(vals["weight.int8"], split_factor, axis=split_dim)
        save_split(split_vals, dir, f"{base_key}.weight.int8", tp_rank, split_factor)
        split_vals = np.split(vals["weight.int8.col"], split_factor, axis=split_dim)
        save_split(split_vals, dir, f"{base_key}.weight.int8.col", tp_rank, split_factor)

    saved_keys_once = ["scale_y_quant_orig"]
    if not kv_cache_only:
        saved_keys_once += [
            "scale_x_orig_quant", "scale_w_quant_orig", "scale_y_accum_quant"
        ]
    # per-column scaling factors are loaded per-gpu for ColumnParallel GEMMs (QKV, FC1)
    if not kv_cache_only:
        if split_dim == -1:
            split_vals = np.split(vals["scale_w_quant_orig.col"], split_factor, axis=split_dim)
            save_split(
                split_vals,
                dir,
                f"{base_key}.scale_w_quant_orig.col", tp_rank, split_factor)
            
            split_vals = np.split(vals["scale_y_accum_quant.col"], split_factor, axis=split_dim)
            save_split(
                split_vals,
                dir,
                f"{base_key}.scale_y_accum_quant.col", tp_rank, split_factor)
        else:
            saved_keys_once += [
                "scale_w_quant_orig.col", "scale_y_accum_quant.col"
            ]

    if tp_rank == 0:
        for save_key in saved_keys_once:
            save_val(vals[save_key], dir, f"{base_key}.{save_key}")


# Note: in multi_query_mode, only query heads are split between multiple GPUs, while key/value head
# are not split as there is only one head per key/value.
@torch.no_grad()
def split_and_save_weight(tp_rank, saved_dir, split_factor, key, vals,
                          storage_type, act_range, config):
    use_attention_nemo_shape = config.get("use_attention_nemo_shape", False)
    split_gated_activation = config.get("split_gated_activation", False)
    num_attention_heads = config.get("num_attention_heads", 0)
    tp_size = config.get("tp_size", 1)
    int8_outputs = config.get("int8_outputs", None)
    multi_query_mode = config.get("multi_query_mode", False)
    local_dim = config.get("local_dim", None)

    save_int8 = int8_outputs == "all" or int8_outputs == "kv_cache_only"

    if not key.endswith(".smoother"):
        if not isinstance(vals, list):
            vals = [vals]

        if config.get("transpose_weights", False) and vals[0].ndim == 2:
            vals = [val.T for val in vals]
        if "layernorm.weight" in key and config.get("apply_layernorm_1p", False):
            vals = [val + 1.0 for val in vals]
        vals = [torch_to_numpy(val.cpu().to(storage_type)) for val in vals]
    else:
        vals = vals.cpu().numpy()

    if "input_layernorm.weight" in key or "ln_1.bias" in key or \
            "self_attn.o_proj.bias" in key or \
            "post_attention_layernorm.weight" in key or \
            "mlp.down_proj.bias" in key or "norm.weight" in key:
        # "final_layernorm.weight" in key or "final_layernorm.bias" in key:

        # shared weights, only need to convert the weights of rank 0
        if tp_rank == 0:
            save_val(vals[0], saved_dir, key)

    elif "self_attn.o_proj.weight" in key or "mlp.down_proj.weight" in key:
        cat_dim = 0
        val = np.concatenate(vals, axis=cat_dim)
        split_vals = np.split(val, split_factor, axis=cat_dim)
        save_split(split_vals, saved_dir, key, tp_rank, split_factor)
        if act_range is not None and int8_outputs == "all":
            base_key = key.replace(".weight", "")
            vals_i8 = generate_int8(val,
                                    act_range,
                                    multi_query_mode=multi_query_mode)
            write_int8(vals_i8, saved_dir, base_key, cat_dim, tp_rank,
                       split_factor)

    elif "mlp.gate_proj.weight" in key or "mlp.up_proj.weight" in key \
            or "mlp.gate_proj.bias" in key or "mlp.gate_proj.bias" in key:
        if split_gated_activation:
            splits = [np.split(val, 2, axis=-1) for val in vals]
            vals, gates = list(zip(*splits))
        cat_dim = -1
        val = np.concatenate(vals, axis=cat_dim)
        split_vals = np.split(val, split_factor, axis=cat_dim)
        save_split(split_vals, saved_dir, key, tp_rank, split_factor)
        if act_range is not None and int8_outputs == "all":
            base_key = key.replace(".weight", "")
            vals_i8 = generate_int8(val,
                                    act_range,
                                    multi_query_mode=multi_query_mode)
            write_int8(vals_i8, saved_dir, base_key, cat_dim, tp_rank,
                       split_factor)

        if split_gated_activation:
            assert not save_int8
            prefix, dot, suffix = key.rpartition(".")
            key = prefix + ".gate" + dot + suffix

            gate = np.concatenate(gates, axis=cat_dim)
            split_vals = np.split(gate, split_factor, axis=cat_dim)
            save_split(split_vals, saved_dir, key, tp_rank, split_factor)

    elif "self_attn.qkv.bias" in key:
        if local_dim is None:
            local_dim = vals[0].shape[-1] // 3

        if multi_query_mode:
            val = vals[0]
            # out_feature = local_dim + 2 * head_size; assumes local_dim equals to hidden_dim
            head_size = (val.shape[-1] - local_dim) // 2
            b_q, b_kv = np.split(val, [local_dim], axis=-1)
            b_k, b_v = np.split(b_kv, [head_size], axis=-1)
            b_q_split = np.split(b_q, split_factor, axis=-1)
            b_k_split = np.split(b_k, split_factor, axis=-1)
            b_v_split = np.split(b_v, split_factor, axis=-1)
            split_vals = [
                np.concatenate((b_q_split[i], b_k_split[i], b_v_split[i]), axis=-1)
                for i in range(split_factor)
            ]
        else:
            if use_attention_nemo_shape:
                head_num = num_attention_heads // tp_size
                size_per_head = local_dim // num_attention_heads
                nemo_shape = (head_num, 3, size_per_head)
                vals = [val.reshape(nemo_shape) for val in vals]
                vals = [val.transpose(1, 0, 2) for val in vals]

            vals = [val.reshape(3, local_dim) for val in vals]
            val = np.concatenate(vals, axis=-1)
            split_vals = np.split(val, split_factor, axis=-1)
        save_split(split_vals, saved_dir, key, tp_rank, split_factor)

    elif "self_attn.qkv.weight" in key:
        hidden_dim = vals[0].shape[0]
        if local_dim is None:
            local_dim = vals[0].shape[-1] // 3
        if multi_query_mode:
            val = vals[0]
            # out_feature = local_dim + 2 * head_size; assumes local_dim equals to hidden_dim
            head_size = (val.shape[-1] - local_dim) // 2
            val = val.reshape(hidden_dim, local_dim + 2 * head_size)
            w_q, w_kv = np.split(val, [local_dim], axis=-1)
            w_k, w_v = np.split(w_kv, [head_size], axis=-1)
            w_q_split = np.split(w_q, split_factor, axis=-1)
            w_k_split = np.split(w_k, split_factor, axis=-1)
            w_v_split = np.split(w_v, split_factor, axis=-1)
            split_vals = [
                np.concatenate((w_q_split[i], w_k_split[i], w_v_split[i]), axis=-1)
                for i in range(split_factor)
            ]
            cat_dim = -1
        else:
            if use_attention_nemo_shape:
                head_num = num_attention_heads // tp_size
                size_per_head = hidden_dim // num_attention_heads
                vals = [
                    val.reshape(hidden_dim, head_num, 3, size_per_head)
                    for val in vals
                ]
                vals = [val.transpose(0, 2, 1, 3) for val in vals]

            vals = [val.reshape(hidden_dim, 3, local_dim) for val in vals]
            cat_dim = -1
            val = np.concatenate(vals, axis=cat_dim)
            split_vals = np.split(val, split_factor, axis=cat_dim)
        save_split(split_vals, saved_dir, key, tp_rank, split_factor)
        if save_int8:
            base_key = key.replace(".weight", "")
            vals_i8 = generate_int8(val,
                                    act_range,
                                    is_qkv=True,
                                    multi_query_mode=multi_query_mode)
            write_qkv_int8(
                vals_i8,
                saved_dir,
                base_key,
                cat_dim,
                tp_rank,
                split_factor,
                kv_cache_only=int8_outputs == "kv_cache_only",
                multi_query_mode=multi_query_mode,
            )
    # elif ("attention.query.weight" in key or "attention.query.bias" in key
    #       or "attention.key_value.weight" in key
    #       or "attention.key_value.bias" in key):
    #     pass
    elif "self_attn.o_proj.smoother" in key or "mlp.down_proj.smoother" in key:
        split_vals = np.split(vals, split_factor, axis=0)
        save_split(split_vals, saved_dir, key, tp_rank, split_factor)
    else:
        print(f"[WARNING] {key} not handled by converter")
