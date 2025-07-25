# Copyright (C) Vevo Therapeutics 2024-2025. All rights reserved.
import logging
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple, Union

import numpy as np
import torch
from composer.utils import dist
from llmfoundry.layers_registry import attention_classes, norms
from llmfoundry.models.layers.ffn import (
    resolve_ffn_act_fn,
    resolve_ffn_hidden_size,
)
from llmfoundry.models.mpt.modeling_mpt import gen_flash_attn_padding_info
from torch import Tensor, nn
from torch.nn.modules.transformer import _get_clones

from mosaicfm.utils import download_file_from_s3_url

attn_config_defaults: Dict = {
    "attn_type": "grouped_query_attention",
    "attn_pdrop": 0.0,
    "attn_impl": "torch",
    "use_attn_mask": True,
    "qk_ln": False,
    "qk_gn": False,
    "clip_qkv": None,
    "softmax_scale": None,
}

norm_config_defaults: Dict = {
    "norm_type": "low_precision_layernorm",
    "eps": 1e-5,
}

init_config_defaults: Dict = {
    "name": "kaiming_normal_",
    "fan_mode": "fan_in",
    "init_nonlinearity": "relu",
    "init_div_is_residual": True,
    "emb_init_std": None,
    "emb_init_uniform_lim": None,
    "init_std": None,
    "init_gain": 0.0,
}

gene_encoder_defaults: Dict = {
    "use_norm": False,
}

log = logging.getLogger(__name__)


class SCGPTBlock(nn.Module):
    r"""TransformerEncoderLayer is made up of self-attn and feedforward network.
    The class is modified from torch.nn.TransformerEncoderLayer to support the
    FlashAttention.

    Args:
        d_model: the number of expected features in the input (required).
        n_heads: the number of heads in the multiheadattention models (required).
        dim_feedforward: the dimension of the feedforward network model (default=2048).
        dropout: the dropout value (default=0.1).
        activation: the activation function of intermediate layer, relu or gelu (default=relu).
        layer_norm_eps: the eps value in layer normalization components (default=1e-5).
        batch_first: If ``True``, then the input and output tensors are provided
            as (batch, seq, feature). Default: ``False``.

    Examples::
        >>> encoder_layer = nn.TransformerEncoderLayer(d_model=512, nhead=8)
        >>> src = torch.rand(10, 32, 512)
        >>> out = encoder_layer(src)

    Alternatively, when ``batch_first`` is ``True``:
        >>> encoder_layer = nn.TransformerEncoderLayer(d_model=512, nhead=8, batch_first=True)
        >>> src = torch.rand(32, 10, 512)
        >>> out = encoder_layer(src)
    """

    def __init__(
        self,
        d_model: int,
        n_heads: int,
        expansion_ratio: int,
        attn_config: Optional[Dict] = None,
        norm_config: Optional[Dict] = None,
        dropout: Optional[float] = 0.0,
        activation: Optional[str] = "gelu",
        device: Optional[str] = None,
        dtype=None,
        norm_scheme="pre",
        use_glu: bool = False,
        **kwargs: Any,
    ) -> None:
        if attn_config is None:
            attn_config = attn_config_defaults
        if norm_config is None:
            norm_config = norm_config_defaults
        del kwargs  # unused, just to capture any extra args from the config
        super().__init__()
        factory_kwargs = {"device": device, "dtype": dtype}
        attn_class = attention_classes.get(attn_config["attn_type"])
        self.d_model = d_model
        self.n_heads = n_heads
        self.device = device
        self.self_attn = attn_class(
            d_model=d_model,
            n_heads=n_heads,
            kv_n_heads=attn_config.get("kv_n_heads", n_heads),
            attn_impl=attn_config.get("attn_impl", "triton"),
            device=device,
        )
        # Implementation of Feedforward model
        dim_feedforward = resolve_ffn_hidden_size(d_model, expansion_ratio)
        self.up_proj = nn.Linear(d_model, dim_feedforward, **factory_kwargs)
        self.down_proj = nn.Linear(dim_feedforward, d_model, **factory_kwargs)
        self.use_glu = use_glu
        if self.use_glu:
            self.gate_proj = nn.Linear(d_model, dim_feedforward, **factory_kwargs)

        # Norms
        norm_class = norms.get(norm_config["norm_type"].lower())
        self.norm1 = norm_class(
            d_model,
            device=device,
            eps=norm_config.get("eps", 1e-5),
        )
        self.norm2 = norm_class(
            d_model,
            device=device,
            eps=norm_config.get("eps", 1e-5),
        )
        self.post_sa_dropout = nn.Dropout(dropout)
        self.post_ffn_dropout = nn.Dropout(dropout)

        self.activation = self._get_activation_fn(activation)
        self.norm_scheme = norm_scheme
        if norm_scheme not in ["pre", "post"]:
            raise ValueError("norm_scheme must be either pre or post")

    @staticmethod
    def _get_activation_fn(activation):
        return resolve_ffn_act_fn({"name": activation})

    def forward(
        self,
        x: Tensor,
        attn_bias: Optional[Tensor] = None,
        flash_attn_padding_info: Optional[Tensor] = None,
    ) -> Tensor:
        r"""Pass the input through the encoder layer.

        Args:
            src: the sequence to the encoder layer (required).
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            see the docs in Transformer class.
        """

        if self.norm_scheme == "pre":
            x = x + self._sa_block(
                self.norm1(x),
                attn_bias=attn_bias,
                flash_attn_padding_info=flash_attn_padding_info,
            )
            x = x + self._ff_block(self.norm2(x))
        else:
            x = self.norm1(
                x
                + self._sa_block(
                    x,
                    attn_bias=attn_bias,
                    flash_attn_padding_info=flash_attn_padding_info,
                ),
            )
            x = self.norm2(x + self._ff_block(x))
        return x

    def _sa_block(
        self,
        x: Tensor,
        attn_bias: Optional[Tensor] = None,
        flash_attn_padding_info: Optional[Tensor] = None,
    ) -> Tensor:
        x, _, _ = self.self_attn(
            x,
            attn_bias=attn_bias,
            flash_attn_padding_info=flash_attn_padding_info,
            is_causal=False,
        )
        return self.post_sa_dropout(x)

    def _ff_block(self, x: Tensor) -> Tensor:
        if self.use_glu:
            x = self.down_proj(self.activation(self.gate_proj(x)) * self.up_proj(x))
        else:
            x = self.down_proj(self.activation(self.up_proj(x)))
        return self.post_ffn_dropout(x)


class SCGPTEncoder(nn.Module):
    # takes in the set of different inputs in an mapping
    r"""TransformerEncoder is a stack of N encoder layers. Users can build the
    BERT(https://arxiv.org/abs/1810.04805) model with corresponding parameters.

    Args:
        encoder_layer: an instance of the TransformerEncoderLayer() class (required).
        num_layers: the number of sub-encoder-layers in the encoder (required).
        norm: the layer normalization component (optional).
        enable_nested_tensor: if True, input will automatically convert to nested tensor
            (and convert back on output). This will improve the overall performance of
            TransformerEncoder when padding rate is high. Default: ``True`` (enabled).

    Examples::
        >>> encoder_layer = nn.TransformerEncoderLayer(d_model=512, nhead=8)
        >>> transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=6)
        >>> src = torch.rand(10, 32, 512)
        >>> out = transformer_encoder(src)
    """

    def __init__(
        self,
        encoder_layer: SCGPTBlock,
        num_layers: int,
        use_norm: bool = False,
        norm_config: Optional[Dict] = None,
        attn_config: Optional[Dict] = None,
    ):
        super().__init__()
        self.layers = _get_clones(encoder_layer, num_layers)
        self.num_layers = num_layers
        self.use_norm = use_norm

        if attn_config is None:
            attn_config = attn_config_defaults
        self.use_attn_mask = attn_config.get("use_attn_mask", True)
        if self.use_norm:
            if norm_config is None:
                norm_config = norm_config_defaults
            norm_class = norms.get(norm_config["norm_type"].lower())
            self.norm = norm_class(
                encoder_layer.d_model,
                device=encoder_layer.device,
                eps=norm_config.get("eps", 1e-5),
            )

    def forward(
        self,
        pcpt_total_embs: Tensor,
        gen_total_embs: Optional[Tensor] = None,
        pcpt_key_padding_mask: Optional[Tensor] = None,
        gen_key_padding_mask: Optional[Tensor] = None,
    ) -> Union[Tensor, Tuple[Tensor, Tensor]]:
        if gen_total_embs is None:
            pcpt_only = True
            total_embs = pcpt_total_embs
            key_padding_mask = pcpt_key_padding_mask
        else:
            pcpt_only = False
            total_embs = torch.cat([pcpt_total_embs, gen_total_embs], dim=1)
            if pcpt_key_padding_mask is None and gen_key_padding_mask is None:
                key_padding_mask = None
            else:
                if pcpt_key_padding_mask is None:
                    pcpt_key_padding_mask = torch.ones(
                        (pcpt_total_embs.shape[0], pcpt_total_embs.shape[1]),
                        device=pcpt_total_embs.device,
                        dtype=torch.bool,
                    )  # 1 means attention is allowed
                elif gen_key_padding_mask is None:
                    gen_key_padding_mask = torch.ones(
                        (gen_total_embs.shape[0], gen_total_embs.shape[1]),
                        device=gen_total_embs.device,
                        dtype=torch.bool,
                    )  # 1 means attention is allowed
                key_padding_mask = torch.cat(
                    [pcpt_key_padding_mask, gen_key_padding_mask],
                    dim=1,
                )  # (B, S)
        p_len = pcpt_total_embs.shape[1]
        total_len = total_embs.shape[1]
        g_len = total_len - p_len
        flash_attn_padding_info = gen_flash_attn_padding_info(
            bsz=total_embs.shape[0],
            S=total_len,
            past_key_len=0,
            attention_mask=key_padding_mask,
            device=total_embs.device,
        )
        attn_bias = None
        if self.use_attn_mask:
            attention_mask = self._make_mask(p_len, g_len, total_embs.device)
            attn_bias = torch.zeros_like(
                attention_mask,
                dtype=total_embs.dtype,
                device=attention_mask.device,
                requires_grad=False,
            ).masked_fill(
                ~attention_mask,
                torch.finfo(total_embs.dtype).min,
            )  # Matrix with -inf at the place of masked values and 0 elsewhere
            attn_bias = attn_bias.unsqueeze(0).unsqueeze(
                1,
            )  # Broadcastable to (B,H, S_Q, S_K) dimensions

            if (
                key_padding_mask is not None
            ):  # NOTE: handle when key_padding_mask is None
                # Merge the key_padding_mask into attn_bias
                b_size, s_k = key_padding_mask.shape[:2]
                attn_bias = attn_bias.masked_fill(
                    ~key_padding_mask.view((b_size, 1, 1, s_k)),
                    torch.finfo(total_embs.dtype).min,
                )
        for mod in self.layers:
            total_embs = mod(
                total_embs,
                attn_bias=attn_bias,
                flash_attn_padding_info=flash_attn_padding_info,
            )

        if self.use_norm:
            total_embs = self.norm(total_embs)
        if pcpt_only:
            return total_embs
        else:
            pcpt_total_embs = total_embs[:, :p_len, :]
            gen_total_embs = total_embs[:, p_len:, :]
            return pcpt_total_embs, gen_total_embs

    @torch.no_grad()
    @lru_cache(maxsize=1)
    def _make_mask(self, p_len, g_len, device):
        # Mask follows the LLM Foundry convention
        # ie: 0 indicates no-attention, 1 indicates attention is allowed
        total_len = p_len + g_len
        attention_mask = torch.ones(
            (total_len, total_len),
            device=device,
            dtype=torch.bool,
        )  # (pcpt_len+gen_len, pcpt_len+gen_len)

        if g_len > 0:
            # pcpt genes should not see gen genes
            # Equivalent to dense self-attention on pcpt genes
            attention_mask[0:p_len, -g_len:] = False
            # gen genes can see all pcpt genes and themselves, not other gen genes.
            # make the last gen_len by gen_gen to be an identity matrix, attention allowed along the diagonal
            # Equivalent to cross-attention from pcpt genes to gen genes
            attention_mask[-g_len:, -g_len:] = torch.eye(
                g_len,
                device=device,
                dtype=torch.bool,
            )
        return attention_mask


class GeneEncoder(nn.Module):
    def __init__(
        self,
        num_embeddings: int,
        embedding_dim: int,
        padding_idx: Optional[int] = None,
        use_norm: bool = False,
        gene_encoder_cfg: Optional[Dict] = None,
    ):
        super().__init__()
        self.embedding = nn.Embedding(
            num_embeddings,
            embedding_dim,
            padding_idx=padding_idx,
        )
        self.use_norm = use_norm
        if not gene_encoder_cfg:
            gene_encoder_cfg = {}
        
        additional_embedding_cfg = gene_encoder_cfg.get("embeddings", {})
        self.extra_embeddings = nn.ModuleDict()
        self.extra_projections = nn.ModuleDict()
        self.extra_norms = nn.ModuleDict()

        log.info(f"Initializing GeneEncoder with {num_embeddings} tokens, embedding_dim={embedding_dim}")
        log.info(f"Additional embeddings config: {list(additional_embedding_cfg.keys())}")

        for name, e_cfg in additional_embedding_cfg.items():
            local, remote = e_cfg["local"], e_cfg["remote"]
            log.info(f"Loading {name} embeddings from {remote}")
            
            if dist.get_local_rank() == 0:
                download_file_from_s3_url(remote, local)
            with dist.local_rank_zero_download_and_wait(local):
                dist.barrier()

            pretrained_weight = torch.load(local, weights_only=True)["embedding.weight"]
            pretrained_vocab_size, pretrained_dim = pretrained_weight.shape
            log.info(f"Loaded {name} embeddings: vocab_size={pretrained_vocab_size}, dim={pretrained_dim}")
            
            if pretrained_vocab_size < num_embeddings:
                log.warning(
                    f"[{name}] Pretrained embedding size ({pretrained_vocab_size}) is smaller than vocab size ({num_embeddings}). "
                    f"Filling remaining {num_embeddings - pretrained_vocab_size} rows with zeros.",
                )
            weight = torch.zeros(
                num_embeddings,
                pretrained_dim,
                dtype=pretrained_weight.dtype,
            )
            weight[:pretrained_vocab_size, :] = pretrained_weight
            emb = nn.Embedding.from_pretrained(
                weight,
                padding_idx=padding_idx,
                freeze=e_cfg.get("freeze", True),
            )
            for m in emb.modules():
                m.skip_init = True
                m.skip_init_name = f"{name}_embedding"
            self.extra_embeddings[name] = emb
            
            # Project to target dimension
            proj = nn.Linear(pretrained_dim, embedding_dim, bias=False)
            proj.skip_init = True
            proj.skip_init_name = f"{name}_projection"
            self.extra_projections[name] = proj
            log.info(f"Added projection for {name}: {pretrained_dim} -> {embedding_dim}")

            if e_cfg.get("use_norm", False):
                self.extra_norms[name] = nn.LayerNorm(embedding_dim)
                log.info(f"Added LayerNorm for {name}")

        if self.extra_embeddings:
            num_embeddings_total = 1 + len(self.extra_embeddings)  # primary + extras
            concat_dim = num_embeddings_total * embedding_dim
            log.info(f"Total embeddings: {num_embeddings_total}, concat dimension: {concat_dim}")
            
            combination_strategy = gene_encoder_cfg.get("combination_strategy", "concat_and_project")
            mlp_config = gene_encoder_cfg.get("mlp", None) if combination_strategy == "concat_and_project" else None
            
            if mlp_config is not None:
                log.info(f"Building MLP projection with config: {mlp_config}")
                self.project = self._build_mlp_projection(concat_dim, embedding_dim, mlp_config)
            else:
                log.info("Using simple linear projection")
                self.project = nn.Linear(concat_dim, embedding_dim, bias=False)
                self.project.skip_init = True
                self.project.skip_init_name = "simple_concat_projection"
        else:
            self.project = nn.Identity()

        log.info(f"Final projection architecture: {self.project}")

        if self.use_norm:
            self.enc_norm = nn.LayerNorm(embedding_dim)

    def _build_mlp_projection(self, input_dim: int, output_dim: int, mlp_config: Dict) -> nn.Module:
        """Build MLP that concatenates input with hidden output before final projection."""
        
        # Parse hidden layers
        hidden_spec = mlp_config.get("hidden_layers", "1024")
        if isinstance(hidden_spec, int):
            hidden_dims = [hidden_spec]
        elif isinstance(hidden_spec, str):
            hidden_dims = [int(x) for x in hidden_spec.split(",") if x.strip()]
        else:
            log.warning(f"Invalid hidden_layers spec {hidden_spec!r}; using default [1024]")
            hidden_dims = [1024]
        
        activation_name = mlp_config.get("activation", "relu")
        use_layer_norm = mlp_config.get("use_layer_norm", False)
        
        # Get activation function once
        act_fn = resolve_ffn_act_fn({"name": activation_name})
        if callable(act_fn) and not isinstance(act_fn, nn.Module):
            class ActivationWrapper(nn.Module):
                def __init__(self, fn, name):
                    super().__init__()
                    self.fn, self.name = fn, name
                def forward(self, x): return self.fn(x)
                def __repr__(self): return f"ActivationWrapper({self.name})"
            act_class = lambda: ActivationWrapper(act_fn, activation_name)
        else:
            act_class = lambda: act_fn
        
        # Build MLP
        layers = []
        current_dim = input_dim
        for i, hidden_dim in enumerate(hidden_dims):
            linear = nn.Linear(current_dim, hidden_dim, bias=False)
            linear.skip_init = True
            linear.skip_init_name = f"concat_mlp_hidden_layer_{i}"
            layers.append(linear)
            layers.append(act_class())
            if use_layer_norm:
                layers.append(nn.LayerNorm(hidden_dim))
            current_dim = hidden_dim
        
        hidden_layers = nn.Sequential(*layers)
        final_proj = nn.Linear(input_dim + current_dim, output_dim, bias=False)
        final_proj.skip_init = True
        final_proj.skip_init_name = "final_concat_projection"
        
        class MLPWithConcat(nn.Module):
            def __init__(self, hidden, final):
                super().__init__()
                self.hidden_layers = hidden
                self.final_proj = final
            
            def forward(self, x):
                hidden = self.hidden_layers(x)
                return self.final_proj(torch.cat([x, hidden], dim=-1))
        
        return MLPWithConcat(hidden_layers, final_proj)

    def forward(self, x: Tensor) -> Tensor:
        reps = [self.embedding(x)]
        
        for name, emb in self.extra_embeddings.items():
            y = emb(x)
            y = self.extra_projections[name](y)
            if name in self.extra_norms:
                y = self.extra_norms[name](y)
            reps.append(y)
        
        x = torch.cat(reps, dim=-1) if len(reps) > 1 else reps[0]
        x = self.project(x)
        
        if self.use_norm:
            x = self.enc_norm(x)
        
        return x


class ChemEncoder(nn.Module):
    def __init__(
        self,
        drug_fps_path: dict,
        d_out: int,
        padding_idx: int = 0,
        activation: str = "leaky_relu",
        use_norm: bool = True,
        freeze: bool = False,
    ):
        super().__init__()

        # download pretrained drug embeddings - morgan fingerprints
        if dist.get_local_rank() == 0:
            download_file_from_s3_url(
                s3_url=drug_fps_path["remote"],
                local_file_path=drug_fps_path["local"],
            )
        with dist.local_rank_zero_download_and_wait(drug_fps_path["local"]):
            dist.barrier()

        drug_fps = torch.as_tensor(np.load(drug_fps_path["local"]), dtype=torch.float32)
        embedding_dim = drug_fps.shape[1]

        self.embedding = nn.Embedding.from_pretrained(
            drug_fps,
            padding_idx=padding_idx,
            freeze=freeze,
        )
        for m in self.embedding.modules():
            m.skip_init = True
        self.fc = nn.Linear(embedding_dim, d_out)
        self.activation = resolve_ffn_act_fn({"name": activation})
        self.proj = nn.Linear(d_out, d_out)

        self.use_norm = use_norm
        if self.use_norm:
            self.norm = nn.LayerNorm(d_out)

    def forward(self, x: Tensor) -> Tensor:
        x = self.embedding(x)  # (batch, d_out)
        x = self.activation(self.fc(x))
        x = self.proj(x)  # (batch, d_out)

        if self.use_norm:
            x = self.norm(x)
        return x


class ContinuousValueEncoder(nn.Module):
    """Encode real number values to a vector using neural nets projection."""

    def __init__(
        self,
        d_model: int,
        dropout: float = 0.1,
        max_value: int = 512,
        activation: str = "relu",
        use_norm: bool = False,
    ):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)
        self.linear1 = nn.Linear(1, d_model)
        self.activation = resolve_ffn_act_fn({"name": activation})
        self.linear2 = nn.Linear(d_model, d_model)
        self.use_norm = use_norm
        if self.use_norm:
            self.norm = nn.LayerNorm(d_model)
        self.max_value = max_value

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: Tensor, shape [batch_size, seq_len]
        """
        # expand last dimension
        x = x.unsqueeze(-1)
        # clip x to [-inf, max_value]
        x = torch.clamp(x, max=self.max_value)
        x = self.activation(self.linear1(x))
        x = self.linear2(x)
        if self.use_norm:
            x = self.norm(x)
        return self.dropout(x)


class CategoryValueEncoder(nn.Module):
    def __init__(
        self,
        num_embeddings: int,
        embedding_dim: int,
        padding_idx: Optional[int] = None,
        use_norm: bool = False,
    ):
        super().__init__()
        self.embedding = nn.Embedding(
            num_embeddings,
            embedding_dim,
            padding_idx=padding_idx,
        )
        self.use_norm = use_norm
        if self.use_norm:
            self.enc_norm = nn.LayerNorm(embedding_dim)

    def forward(self, x: Tensor) -> Tensor:
        x = x.long()
        x = self.embedding(x)  # (batch, seq_len, embsize)
        if self.use_norm:
            x = self.enc_norm(x)
        return x


class ExprDecoder(nn.Module):
    """Consists of three linear functions and leaky-relu as an activation
    function."""

    def __init__(
        self,
        d_model: int,
        n_outputs: int = 1,
        n_layers: int = 2,
        activation: str = "leaky_relu",
    ):
        super().__init__()
        d_in = d_model
        self.activation = resolve_ffn_act_fn({"name": activation})
        self.linear_layers = nn.ModuleList(
            [nn.Linear(d_in, d_model) for _ in range(n_layers)],
        )
        self.out_proj = nn.Linear(d_model, n_outputs)

    def forward(self, x: Tensor) -> Dict[str, Tensor]:
        """X is the output of the transformer, (batch, seq_len, d_model)"""
        for layer in self.linear_layers:
            x = self.activation(layer(x))
        pred_value = self.out_proj(x)  # (batch, seq_len, n_outputs)
        if pred_value.shape[-1] == 1:
            pred_value = pred_value.squeeze(-1)
        return {"pred": pred_value}


class AffineExprDecoder(torch.nn.Module):
    def __init__(
        self,
        d_model: int,
        explicit_zero_prob: bool = False,
        activation: Optional[str] = None,
        tanh_coeff: bool = False,
        adaptive_bias: bool = False,
    ):
        """Predict the expression value of each gene in an affine like form of
        Ax + b. This decoder takes two ExprDecoder intrinsically to genrate the
        coefficient A and bias b.

        Args:
            d_model: The embedding dimension.
            explicit_zero_prob: If True, predict the probability of each gene being
                zero.
            activation: The activation function for the coefficient A and bias b.
            tanh_coeff: If True, use tanh activation for the coefficient A.
            adaptive_bias: If True, use a learnable bias for the bias b.
        """
        super().__init__()
        self.explicit_zero_prob = explicit_zero_prob
        self.tanh_coeff = tanh_coeff
        self.adaptive_bias = adaptive_bias
        self.coeff_decoder = ExprDecoder(d_model)
        self.bias_decoder = ExprDecoder(d_model)

        self.activation = activation
        if activation is not None:
            assert hasattr(nn, activation), f"Unknown activation: {activation}"
            self.activation = getattr(nn, activation)()

    def forward(self, x: torch.Tensor, values: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: Tensor, shape [batch_size, seq_len, embsize]
            values: Tensor, shape [batch_size, seq_len]

        Returns:
            output Tensor of shape [batch_size, seq_len]
        """
        coeff = self.coeff_decoder(x)
        bias = self.bias_decoder(x)

        if self.activation is not None:
            coeff["pred"] = self.activation(coeff["pred"])
            bias["pred"] = self.activation(bias["pred"])

        if self.adaptive_bias:
            non_zero_value_mean = values.sum(dim=1, keepdim=True) / (values != 0).sum(
                dim=1,
                keepdim=True,
            )
            bias["pred"] = bias["pred"] * non_zero_value_mean

        if self.explicit_zero_prob:
            return {
                "pred": coeff["pred"] * values + bias["pred"],
                "zero_probs": coeff["zero_probs"],
            }

        return {"pred": coeff["pred"] * values + bias["pred"]}


class MVCDecoder(nn.Module):
    """Decoder for the masked value prediction for cell embeddings."""

    def __init__(
        self,
        d_model: int,
        arch_style: str = "inner product",
        query_activation: str = "sigmoid",
        scaled_dot_product: bool = False,
    ) -> None:
        """
        Args:
            d_model (:obj:`int`): dimension of the gene embedding.
            arch_style (:obj:`str`): architecture style of the decoder, choice from
                1. "inner product" or 2. "concat query" or 3. "sum query".
            query_activation (:obj:`nn.Module`): activation function for the query
                vectors.
            hidden_activation (:obj:`nn.Module`): activation function for the hidden
                layers.
        """
        super().__init__()
        d_in = d_model
        self.scaled_dot_product = scaled_dot_product
        if arch_style == "inner product":
            self.gene2query = nn.Linear(d_model, d_model)
            self.query_activation = resolve_ffn_act_fn({"name": query_activation})
            self.W = nn.Linear(d_model, d_in, bias=False)
        else:
            raise ValueError(f"Unknown arch_style: {arch_style}")

        self.arch_style = arch_style

    def forward(
        self,
        cell_emb: Tensor,
        gene_embs: Tensor,
    ) -> Union[Tensor, Dict[str, Tensor]]:
        """
        Args:
            cell_emb: Tensor, shape (batch, embsize=d_model)
            gene_embs: Tensor, shape (batch, seq_len, embsize=d_model)
        """
        if self.arch_style == "inner product":
            query_vecs = self.query_activation(
                self.gene2query(gene_embs),
            )  # (batch, seq_len, embsize)
            inner_product_dimension = query_vecs.shape[-1]
            cell_emb = cell_emb.unsqueeze(2)  # (batch, embsize, 1)
            pred_value = torch.bmm(self.W(query_vecs), cell_emb).squeeze(
                2,
            )  # (batch, seq_len)
            if self.scaled_dot_product:
                pred_value = pred_value / torch.sqrt(
                    torch.tensor(inner_product_dimension, dtype=pred_value.dtype),
                )
            return {"pred": pred_value}
        else:
            raise ValueError(f"Unknown arch_style: {self.arch_style}")
